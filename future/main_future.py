"""
future/main_future.py — TradingBot orchestrator untuk Binance USDT-M Futures

Diadaptasi dari spot/main_spot.py, TAPI BUKAN sekadar copy-paste ganti nama.
Perbedaan MENDASAR yang ditemukan & diperbaiki saat membangun file ini
(didokumentasikan di sini supaya jelas apa yang genuinely baru vs reuse):

1. Gate 3 (basic filter) di main_spot.py PUNYA hard filter long-only
   tersembunyi (EMA9>EMA21, harga>=VWAP) SEBELUM sinyal sampai ke Gate 4
   intelligence pipeline yang sudah side-aware. Di sini Gate 3 dijalankan
   DUA ARAH (cek kandidat long DAN short per simbol).
2. _refresh_portfolio(): formula equity spot (free_balance + notional value
   posisi) SALAH TOTAL untuk futures -- diganti jadi
   (free_margin + used_margin + unrealized_pnl).
3. _handle_entry() (ganti _handle_buy): dispatch ke risk_future.RiskManager
   yang punya signature berbeda (leverage, existing_position_side).
4. run_sl_tp_monitor(): DITAMBAH pengecekan proximity liquidation (konsep
   yang sama sekali tidak ada di spot).
5. _do_close_position(): SignalType.CLOSE_LONG/CLOSE_SHORT sesuai pos.side
   (bukan SignalType.SELL hardcoded), evaluate_order dgn existing_position_side.

⚠️ BELUM DIKERJAKAN (di luar scope sesi ini, terdokumentasi jelas):
- run_position_sync_loop(): position_sync_futures.py BELUM DIBANGUN (item
  terpisah di roadmap) -- loop ini DIHILANGKAN dari task list run(), bukan
  di-stub diam-diam. Rekonsiliasi posisi orphan di Binance Futures TIDAK
  akan berjalan sampai file itu dibangun.
- run_coin_swap_loop(): TIDAK diikutsertakan sama sekali -- sistem ini
  sudah dikonfirmasi deprecated permanen di spot, tidak relevan di futures.
- Funding rate settlement (future/funding.py) belum disambungkan ke loop
  manapun di sini -- perhitungan tersedia tapi belum ada loop periodik yang
  memanggilnya untuk update realized_funding di Trade/Position.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import uvicorn
from dotenv import load_dotenv

from engine.constants import APP_VERSION
from engine.profiles.registry import get_coin_profile
from engine.database import DatabaseManager
from future.exchange_future import FutureExchangeConnector
from spot.exchange_spot import WebSocketFeed  # [CATATAN] WebSocketFeed belum
    # diekstrak ke engine/ (lihat engine/execution_base.py) -- market-agnostic
    # secara konsep (streaming ticker/orderbook), reuse langsung dari spot/
    # untuk saat ini, bukan duplikasi baru.
from spot.strategy_spot import get_strategy, PositionTracker
from engine.core.models import SignalType, SignalEvent, ExitMode
from future.risk_future import RiskManager
from engine.risk_base import RiskAssessment, RiskDecision, HaltReason
from future.execution_future import OrderExecutionManager
from shared_service.notifications import NotificationManager
from engine.indicators.orderbook import WhaleDetector

load_dotenv()

log = logging.getLogger("main_future")


class BotStartupError(Exception):
    pass


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s"
    ))
    root.addHandler(handler)


class TradingBot:
    """
    Orchestrator futures. Struktur loop (scanner/gate3/portfolio/sl_tp/dst)
    mengikuti pola spot/main_spot.py yang sudah terbukti jalan, TAPI setiap
    method di bawah sudah diperiksa satu-satu apakah genuinely reusable atau
    perlu logic baru -- lihat komentar [FUTURES-SPECIFIC] di titik-titik yang
    berbeda dari spot.
    """

    SNAPSHOT_INTERVAL    = 900
    CANDLE_POLL_INTERVAL = 10
    SL_TP_CHECK_INTERVAL = 5
    DAILY_SUMMARY_HOUR   = 23
    DAILY_SUMMARY_MIN    = 55
    # [FUTURES-SPECIFIC] Ambang peringatan proximity liquidation -- kalau
    # harga sudah masuk zona ini (persentase dari jarak entry->liquidation),
    # posisi ditutup PAKSA sebagai emergency exit, TIDAK menunggu SL normal
    # (SL normal seharusnya sudah lebih longgar dari ini kalau risk_future.py
    # bekerja benar, tapi ini lapis pengaman kedua/independen).
    LIQUIDATION_EMERGENCY_PROXIMITY_PCT = 10.0

    def __init__(self) -> None:
        self.config = self._load_config()

        self.is_running = False
        self.start_time: Optional[datetime] = None

        self.portfolio_state: Dict = {
            "total_equity":     0.0,
            "free_margin":      0.0,
            "used_margin":      0.0,
            "unrealized_pnl":   0.0,
            "daily_pnl":        0.0,
            "daily_pnl_pct":    0.0,
        }

        self.db:           Optional[DatabaseManager]         = None
        self.exchange:     Optional[FutureExchangeConnector] = None
        self.ws_feed:      Optional[WebSocketFeed]           = None
        self.risk_manager: Optional[RiskManager]              = None
        self.strategy:     Optional[object]                   = None
        self.executor:     Optional[OrderExecutionManager]    = None
        self.notifier:     Optional[NotificationManager]      = None

        self._commander    = None
        self._analytics     = None
        self._meta_learner  = None
        self._tasks:              List[asyncio.Task] = []
        self._daily_summary_sent: bool               = False
        self._closing_lock:    asyncio.Lock = asyncio.Lock()
        self._equity_lock:     asyncio.Lock = asyncio.Lock()
        self._closing_symbols: Set[str]     = set()
        self._close_retry_count: Dict[str, int] = {}
        self._last_refresh_time: float = 0.0
        self._whale_detectors:     Dict[str, WhaleDetector] = {}

        self._pipeline_active:      Set[str]        = set()
        self._queued_symbols:       Set[str]        = set()
        self._invalidation_signals: Dict[str, Dict] = {}
        self._gate3_queue:          asyncio.Queue   = asyncio.Queue()
        self._volume_ma:            Dict[str, float] = {}
        self._price_buffer:         Dict[str, list]  = {}
        self._last_candle_ts:       Dict[tuple, int]  = {}

    @property
    def commander(self):
        return self._commander

    @property
    def analytics(self):
        return self._analytics

    @property
    def meta_learner(self):
        return self._meta_learner

    @staticmethod
    def _load_config() -> Dict:
        raw_universe = os.getenv("UNIVERSE_WATCHLIST_FUTURES", os.getenv("UNIVERSE_WATCHLIST", "BTC/USDT,ETH/USDT"))
        return {
            "exchange_id":           os.getenv("EXCHANGE_ID", "binance"),
            "api_key":               os.getenv("API_KEY", ""),
            "api_secret":            os.getenv("API_SECRET", ""),
            "api_passphrase":        os.getenv("API_PASSPHRASE", ""),
            "testnet":               os.getenv("TESTNET", "true").lower() == "true",
            "paper_trading_mode":    os.getenv("PAPER_TRADING_MODE", "false").lower() == "true",
            "quote_currency":        os.getenv("QUOTE_CURRENCY", "USDT"),
            "initial_capital":       float(os.getenv("INITIAL_CAPITAL_FUTURES", os.getenv("INITIAL_CAPITAL", "1000"))),
            "max_open_positions":    int(os.getenv("MAX_OPEN_POSITIONS_FUTURES", os.getenv("MAX_OPEN_POSITIONS", "3"))),
            "universe_watchlist":    [s.strip() for s in raw_universe.split(",") if s.strip()],
            "strategy":              os.getenv("STRATEGY", "volumetric_breakout"),
            "timeframe":             os.getenv("TIMEFRAME", "15m"),
            "lookback_candles":      int(os.getenv("LOOKBACK_CANDLES", "200")),
            "database_url":          os.getenv(
                "DATABASE_URL_FUTURES",
                "sqlite+aiosqlite:///./data/trading_bot_futures.db",
            ),
            "api_host":              os.getenv("API_HOST_FUTURES", os.getenv("API_HOST", "0.0.0.0")),
            "api_port":              int(os.getenv("API_PORT_FUTURES", "8001")),
            "max_drawdown_pct":      float(os.getenv("MAX_DRAWDOWN_PCT",      "15")),
            "max_position_size_pct": float(os.getenv("MAX_POSITION_SIZE_PCT", "10")),
            "stop_loss_pct":         float(os.getenv("STOP_LOSS_PCT",         "2.5")),
            "take_profit_pct":       float(os.getenv("TAKE_PROFIT_PCT",       "5.0")),
            "atr_multiplier_sl":     float(os.getenv("ATR_MULTIPLIER_SL",     "2.0")),
            "atr_multiplier_tp":     float(os.getenv("ATR_MULTIPLIER_TP",     "3.5")),
            "daily_loss_limit_pct":  float(os.getenv("DAILY_LOSS_LIMIT_PCT",  "10.0")),
            "risk_per_trade_pct":    float(os.getenv("RISK_PER_TRADE_PCT",    "1.0")),
            "max_slippage_pct":      float(os.getenv("MAX_SLIPPAGE_PCT",      "0.5")),
            "trailing_atr_mult":     float(os.getenv("TRAILING_ATR_MULT",     "1.5")),
            "use_trailing_stop":     os.getenv("USE_TRAILING_STOP", "true").lower() == "true",
            "min_order_value_usdt":  float(os.getenv("MIN_ORDER_VALUE_USDT",  "10.0")),
            "sentiment_enabled":     os.getenv("SENTIMENT_ENABLED", "true").lower() == "true",
            "volume_multiplier":       float(os.getenv("VOLUME_MULTIPLIER",       "1.3")),
            "volume_spike_threshold":  float(os.getenv("VOLUME_SPIKE_THRESHOLD",  "3.0")),
            "rsi_min":                 int(os.getenv("RSI_MIN",                   "45")),
            "rsi_max":                 int(os.getenv("RSI_MAX",                   "77")),
            "rsi_golden_cross_min":    int(os.getenv("RSI_GOLDEN_CROSS_MIN",      "45")),
            "atr_pct_threshold":       float(os.getenv("ATR_PCT_THRESHOLD",       "0.8")),
            "telegram_enabled":      os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
            "telegram_bot_token":    os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id":      os.getenv("TELEGRAM_CHAT_ID", ""),
            "email_enabled":         os.getenv("EMAIL_ENABLED", "false").lower() == "true",
            "smtp_host":             os.getenv("SMTP_HOST", "smtp.gmail.com"),
            "smtp_port":             int(os.getenv("SMTP_PORT", "587")),
            "smtp_user":             os.getenv("SMTP_USER", ""),
            "smtp_password":         os.getenv("SMTP_PASSWORD", ""),
            "email_from":            os.getenv("EMAIL_FROM", ""),
            "email_to":              os.getenv("EMAIL_TO", ""),
            "intelligence_enabled":       os.getenv("INTELLIGENCE_ENABLED", "true").lower() == "true",
            "confirmation_tf_enabled":    os.getenv("CONFIRMATION_TIMEFRAME_ENABLED", "true").lower() == "true",
            "analytics_enabled":          os.getenv("ANALYTICS_ENABLED", "true").lower() == "true",
            "analytics_refresh_interval": int(os.getenv("ANALYTICS_REFRESH_INTERVAL", "3600")),
            # [FUTURES-SPECIFIC] Parameter yang tidak ada sama sekali di spot.
            "default_leverage":            int(os.getenv("DEFAULT_LEVERAGE", "10")),
            "max_leverage":                int(os.getenv("MAX_LEVERAGE", "20")),
            "margin_mode":                 os.getenv("MARGIN_MODE", "isolated"),
            "maintenance_margin_rate":     float(os.getenv("MAINTENANCE_MARGIN_RATE", "0.005")),
            "min_liquidation_safety_pct":  float(os.getenv("MIN_LIQUIDATION_SAFETY_PCT", "20.0")),
            "enable_short":                os.getenv("ENABLE_SHORT", "true").lower() == "true",
        }

    async def start(self) -> None:
        setup_logging()
        log.info("=" * 70)
        log.info("  AlgoTrader Pro Futures v%s — Starting", APP_VERSION)
        log.info("=" * 70)

        Path("data").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)

        self.notifier = NotificationManager(self.config)

        self.db = DatabaseManager(self.config["database_url"])
        await self.db.init_db()
        log.info("Database ready: %s", self.config["database_url"])

        try:
            from engine.profiles.registry import load_all_overrides_from_db
            _n_overrides = await load_all_overrides_from_db(self.db)
            log.info("Parameter overrides dari DB di-restore: %d symbol.", _n_overrides)
        except Exception as _ovr_err:
            log.warning("Gagal load parameter overrides dari DB (non-fatal): %s", _ovr_err)

        self.exchange = FutureExchangeConnector(
            exchange_id=self.config["exchange_id"],
            api_key=self.config["api_key"],
            api_secret=self.config["api_secret"],
            api_passphrase=self.config.get("api_passphrase", ""),
            testnet=self.config["testnet"],
            db=self.db,
            paper_trading=self.config.get("paper_trading_mode", False),
            initial_capital=self.config["initial_capital"],
            quote_currency=self.config["quote_currency"],
            default_leverage=self.config["default_leverage"],
            default_margin_mode=self.config["margin_mode"],
            default_mmr=self.config["maintenance_margin_rate"],
        )
        if self.config.get("paper_trading_mode", False):
            log.warning(
                "=" * 70 + "\n"
                "📝 PAPER TRADING MODE (FUTURES) — data pasar ASLI, order TIDAK "
                "dikirim ke exchange.\n⚠️ Liquidation price APPROXIMATE, lihat "
                "future/liquidation.py.\n" + "=" * 70
            )
        connected = await self.exchange.connect()
        if not connected:
            log.critical("Exchange connection FAILED.")
            await self.notifier.notify_error(
                "startup", "Exchange connection FAILED — bot tidak bisa start.",
            )
            raise BotStartupError("Exchange connection FAILED.")

        if not self.config["testnet"] and not self.config.get("paper_trading_mode", False):
            await self._live_preflight()
        elif self.config.get("paper_trading_mode", False):
            log.info(
                "[startup] Paper trading aktif — live preflight di-skip, "
                "modal virtual %.2f %s dipakai sepenuhnya.",
                self.config["initial_capital"], self.config["quote_currency"],
            )

        self.ws_feed = WebSocketFeed(
            exchange_id=self.config["exchange_id"],
            api_key=self.config["api_key"],
            api_secret=self.config["api_secret"],
            api_passphrase=self.config.get("api_passphrase", ""),
            symbols=self.config["universe_watchlist"],
            testnet=self.config["testnet"],
        )
        await self.ws_feed.start()
        log.info("WebSocketFeed subscribe ke %d koin universe futures", len(self.config["universe_watchlist"]))

        await self._initialize_intelligence_pipeline()

        self.executor = OrderExecutionManager(
            exchange=self.exchange, db=self.db,
            on_trade_executed=self._on_trade_executed,
            max_slippage_pct=self.config["max_slippage_pct"],
            ws_feed=self.ws_feed,
        )

        self.risk_manager = RiskManager(config=self.config, db=self.db)

        self.is_running = True
        self.start_time = _utcnow_dt()
        log.info("Bot futures started — leverage default=%dx margin_mode=%s",
                  self.config["default_leverage"], self.config["margin_mode"])

    async def _initialize_intelligence_pipeline(self) -> None:
        if not self.config.get("intelligence_enabled", True):
            return
        try:
            self.strategy = get_strategy(
                name=self.config["strategy"],
                config=self.config,
                db=self.db,
                ws_feed=self.ws_feed,
                notifier=self.notifier,
            )
            if hasattr(self.strategy, "refresh_profiles"):
                self.strategy.refresh_profiles()
            from engine.intelligence.commander import IntelligenceCommander
            self._commander = IntelligenceCommander(config=self.config)
            log.info("Intelligence pipeline (futures) siap.")
        except Exception as e:
            log.error("Intelligence pipeline init gagal: %s", e, exc_info=True)

    async def stop(self) -> None:
        log.info("Shutting down (futures)...")
        self.is_running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.ws_feed:
            await self.ws_feed.stop()
        if self.exchange:
            await self.exchange.disconnect()
        if self.db:
            await self.db.save_log("INFO", "main_future", f"AlgoTrader Futures v{APP_VERSION} stopped cleanly.")
            await self.db.close()
        log.info("Shutdown selesai (futures).")

    async def _live_preflight(self) -> None:
        log.warning("=" * 50)
        log.warning("  LIVE MODE (FUTURES) — REAL FUNDS AT RISK, LEVERAGE AKTIF")
        log.warning("=" * 50)
        try:
            balance = await self.exchange.fetch_balance()
            quote   = self.config["quote_currency"]
            free    = float(balance.get("free", {}).get(quote, 0) or 0)
            _min_abs = float(os.getenv("MIN_BALANCE_USDT", "10.0"))
            required = max(self.config["initial_capital"] * 0.1, _min_abs)
            if free < required:
                msg = f"LIVE PREFLIGHT FAIL: Free margin {quote} {free:.2f} < required {required:.2f}."
                log.critical(msg)
                await self.notifier.notify_error("live_preflight", msg)
                raise BotStartupError(msg)
            log.info("Live preflight (futures): Free margin %s = %.2f [OK]", quote, free)
            await self.notifier.notify_bot_resumed()
        except BotStartupError:
            raise
        except Exception as e:
            log.critical("LIVE PREFLIGHT ERROR: %s", e)
            await self.notifier.notify_error("live_preflight", str(e))
            raise BotStartupError(f"Live preflight error: {e}") from e

    async def _on_trade_executed(self, trade) -> None:
        pass  # hook tersedia utk logic tambahan pasca-fill kalau diperlukan nanti

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = self.ws_feed.live_tickers.get(symbol, {}) if self.ws_feed else {}
            last = float(ticker.get("last") or 0)
            if last > 0:
                return last
        except Exception:
            pass
        try:
            t = await self.exchange.fetch_ticker(symbol)
            last = float(t.get("last") or 0)
            return last if last > 0 else None
        except Exception as e:
            log.warning("REST price fallback gagal untuk %s: %s", symbol, e)
            return None

    async def _handle_entry(self, signal: SignalEvent, side: str) -> None:
        """
        [FUTURES-SPECIFIC -- BARU, ganti _handle_buy] side="long"->SignalType.BUY,
        side="short"->SignalType.OPEN_SHORT (sudah ditentukan caller di
        run_gate3_worker). Dispatch ke risk_future.RiskManager.evaluate_order()
        yang punya signature berbeda dari spot (leverage, existing_position_side).
        """
        symbol = signal.symbol
        price  = signal.price
        equity = self.portfolio_state.get("total_equity", 0.0)
        atr    = signal.metadata.get("atr")

        def _reset_position_flag():
            if hasattr(self.strategy, "_in_position"):
                with self.strategy._lock:
                    self.strategy._in_position[symbol] = False
                    self.strategy._pending_entry.discard(symbol)

        if equity <= 0:
            log.warning("Equity=0 — skip entry %s untuk %s", side, symbol)
            _reset_position_flag()
            return

        leverage = self.config.get("default_leverage", 10)
        margin_mode = self.config.get("margin_mode", "isolated")

        # Order level exchange: buka long="buy", buka short="sell"
        order_side = "buy" if side == "long" else "sell"

        assessment = await self.risk_manager.evaluate_order(
            symbol=symbol, side=order_side, price=price,
            quantity=(equity * self.config["max_position_size_pct"] / 100) / price if price > 0 else 0,
            leverage=leverage, existing_position_side=None,
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            atr=atr, margin_mode=margin_mode,
        )

        if not assessment.is_approved:
            log.info("Risk REJECTED %s (%s): %s", symbol, side, assessment.reason)
            _reset_position_flag()
            return

        _kelly_size_pct = signal.metadata.get("kelly_size_pct") if signal.metadata else None
        if _kelly_size_pct and _kelly_size_pct > 0 and assessment.approved_size and price > 0:
            _kelly_max_qty = (equity * _kelly_size_pct / 100) / price
            if _kelly_max_qty < assessment.approved_size:
                assessment.approved_size = _kelly_max_qty

        async with self._equity_lock:
            trade = await self.executor.execute_signal(signal, assessment)
            if trade is None:
                log.error("EXECUTE %s GAGAL untuk %s — posisi tidak dibuka.", side, symbol)
                _reset_position_flag()
                return

            try:
                await self.db.upsert_position(symbol, {
                    "entry_time": _utcnow_dt(),
                    "entry_price": trade.executed_price,
                    "amount": trade.filled or trade.amount,
                    "side": side,
                    "is_open": True,
                    "stop_loss_price": assessment.stop_loss,
                    "take_profit_price": assessment.take_profit,
                    "atr_at_entry": atr,
                    "highest_price": trade.executed_price,
                    "strategy_name": signal.strategy,
                    "strategy_profile": signal.metadata.get("coin_profile", "") if signal.metadata else "",
                    "entry_score": signal.total_score,
                    "entry_regime": str(signal.regime) if signal.regime else "undefined",
                    "market_type": "futures",
                    "leverage": assessment.leverage,
                    "margin_mode": assessment.margin_mode,
                    "liquidation_price": assessment.liquidation_price,
                    "mark_price_at_entry": trade.executed_price,
                })
            except Exception as e:
                log.critical(
                    "upsert_position GAGAL untuk %s setelah order berhasil — "
                    "posisi TIDAK tertracking di DB! %s", symbol, e,
                )
                await self.db.save_log("CRITICAL", "main_future",
                                        f"upsert_position gagal {symbol} — trailing/liquidation monitor tidak aktif!")

        log.info(
            "POSISI DIBUKA (futures): %s %s | entry=%.6f amount=%.8f SL=%s TP=%s "
            "leverage=%dx liq_price≈%s",
            side.upper(), symbol, trade.executed_price, trade.filled or trade.amount,
            assessment.stop_loss, assessment.take_profit,
            assessment.leverage or leverage, assessment.liquidation_price,
        )
        if self.notifier:
            try:
                await self.notifier.notify_trade_opened(
                    symbol=symbol, side=side, entry_price=trade.executed_price,
                    amount=trade.filled or trade.amount,
                    stop_loss=assessment.stop_loss, take_profit=assessment.take_profit,
                    atr=atr,
                )
            except Exception as e:
                log.debug("notify_trade_opened gagal: %s", e)

    async def _handle_close(self, signal: SignalEvent) -> None:
        async with self._closing_lock:
            is_closing = signal.symbol in self._closing_symbols
        if is_closing:
            return
        positions = await self.db.get_open_positions()
        for pos in positions:
            if pos.symbol == signal.symbol:
                reason = signal.metadata.get("exit_reason", "Strategy exit signal") if signal.metadata else "Strategy exit signal"
                await self._close_position_market(pos, signal.price, reason)

    async def _close_position_market(self, pos, exit_price: float, reason: str) -> None:
        async with self._closing_lock:
            if pos.symbol in self._closing_symbols:
                return
            self._closing_symbols.add(pos.symbol)
        try:
            await self.db.mark_position_closing(pos.symbol)
        except Exception as e:
            log.warning("mark_position_closing gagal untuk %s: %s", pos.symbol, e)
        try:
            await self._do_close_position(pos, exit_price, reason)
        finally:
            async with self._closing_lock:
                self._closing_symbols.discard(pos.symbol)

    async def _do_close_position(self, pos, exit_price: float, reason: str) -> None:
        """
        [FUTURES-SPECIFIC] SignalType.CLOSE_LONG/CLOSE_SHORT sesuai pos.side
        (bukan SignalType.SELL hardcoded spt spot). evaluate_order dgn
        existing_position_side=pos.side supaya risk_future.py tau ini
        reduce/close (bypass sizing cap), bukan buka posisi baru.
        """
        existing = await self.db.get_open_position_by_symbol(pos.symbol)
        if not existing:
            log.warning("Position %s sudah tidak open di DB — skip close (reason=%s)", pos.symbol, reason)
            return

        close_signal_type = SignalType.CLOSE_LONG if pos.side == "long" else SignalType.CLOSE_SHORT
        close_signal = SignalEvent(
            symbol=pos.symbol, signal_type=close_signal_type, price=exit_price,
            timestamp=_utcnow_dt(), strategy=pos.strategy_name or "risk_monitor",
            metadata={"exit_reason": reason},
        )

        # order_side level-exchange: tutup long="sell", tutup short="buy"
        order_side = "sell" if pos.side == "long" else "buy"
        close_assessment_raw = await self.risk_manager.evaluate_order(
            symbol=pos.symbol, side=order_side, price=exit_price, quantity=pos.amount,
            existing_position_side=pos.side,
        )
        close_assessment = RiskAssessment(
            decision=RiskDecision.APPROVED, reason=reason,
            approved_size=(close_assessment_raw.approved_size
                           if close_assessment_raw.is_approved and close_assessment_raw.approved_size
                           else pos.amount),
            stop_loss=None, take_profit=None,
        )

        trade = await self.executor.execute_signal(close_signal, close_assessment)

        if trade is None:
            log.error("CLOSE ORDER GAGAL untuk %s — posisi TETAP terbuka di DB! Tutup manual di Binance Futures.", pos.symbol)
            await self.db.save_log("CRITICAL", "main_future", f"CLOSE GAGAL: {pos.symbol} — posisi masih terbuka! Tutup manual.")
            retry = self._close_retry_count.get(pos.symbol, 0) + 1
            self._close_retry_count[pos.symbol] = retry
            if retry >= 3 and self.notifier:
                await self.notifier.notify_error(
                    "close_position",
                    f"CLOSE ORDER GAGAL {retry}x untuk {pos.symbol} — INTERVENSI MANUAL DIPERLUKAN!",
                )
            return

        self._close_retry_count.pop(pos.symbol, None)

        # [FUTURES-SPECIFIC] Hitung realized_pnl manual dari entry vs exit
        # price (memperhitungkan side) -- Trade.realized_pnl TIDAK otomatis
        # terisi oleh execute_signal()/_process_fill() (kolom itu ada di
        # skema tapi memang diisi terpisah, bukan saat insert trade).
        entry_price = float(pos.entry_price or 0)
        amount      = float(pos.amount or 0)
        if pos.side == "long":
            realized_pnl = (trade.executed_price - entry_price) * amount
        else:
            realized_pnl = (entry_price - trade.executed_price) * amount

        try:
            await self.db.close_position(pos.symbol, exit_price=trade.executed_price, realized_pnl=realized_pnl)
        except Exception as e:
            log.critical("close_position (DB) GAGAL untuk %s setelah order sukses: %s", pos.symbol, e)

        try:
            self.risk_manager.record_symbol_loss(pos.symbol, realized_pnl)
        except Exception:
            pass

        log.info("POSISI DITUTUP (futures): %s %s @ %.6f | realized_pnl=%+.4f | reason=%s",
                  pos.side.upper(), pos.symbol, trade.executed_price, realized_pnl, reason)
        if self.notifier:
            try:
                await self.notifier.notify_trade_closed(trade)
            except Exception as e:
                log.debug("notify_trade_closed gagal: %s", e)


    async def run_scanner_loop(self) -> None:
        """
        [REUSE VERBATIM dari main_spot.py] Gate 1 & Gate 2 -- whale/volume
        detection genuinely direction-agnostic (volume spike & whale wall
        besar/kecil tidak peduli arah, cuma soal "ada aktivitas signifikan
        atau tidak"). TIDAK ADA perubahan logic dari versi spot.

        [KETERBATASAN TERDOKUMENTASI] whale_sell_genuine di bawah cuma
        mendeteksi invalidasi utk kandidat LONG (ask wall besar = sinyal
        jangan long). Mirror utk short (bid wall besar = sinyal jangan
        short) BELUM diimplementasikan -- enhancement masa depan, bukan
        blocker untuk short bisa jalan (Gate 3/4/5 di bawah tetap independen
        mengevaluasi kandidat short, cuma tanpa lapis invalidasi whale
        tambahan yang spesifik untuk short).
        """
        import time as _time

        GATE1_VOLUME_RATIO_MIN  = 1.5
        GATE1_PRICE_CHANGE_MIN  = 0.3
        GATE1_MIN_VOLUME_USDT   = float(os.getenv("GATE1_MIN_VOLUME_USDT", "500000"))
        GATE1_LOOP_INTERVAL     = 2.0
        PRICE_BUFFER_SIZE       = 5
        VOLUME_MA_ALPHA         = 0.1

        log.info("Scanner loop (futures) dimulai — universe=%d koin", len(self.config["universe_watchlist"]))

        while self.is_running:
            try:
                universe = self.config["universe_watchlist"]
                now      = _time.time()

                try:
                    _open_pos = await self.db.get_open_positions()
                    _open_pos_symbols = {p.symbol for p in _open_pos}
                except Exception:
                    _open_pos_symbols = set()

                for symbol in universe:
                    try:
                        ticker = self.ws_feed.live_tickers.get(symbol, {})
                        ob     = self.ws_feed.live_orderbooks.get(symbol, {})
                        if not ticker or not ticker.get("last"):
                            continue
                        last         = float(ticker.get("last") or 0)
                        quote_volume = float(ticker.get("quote_volume") or 0)
                        if last <= 0:
                            continue

                        if symbol not in self._volume_ma:
                            self._volume_ma[symbol] = quote_volume
                        else:
                            self._volume_ma[symbol] = (
                                VOLUME_MA_ALPHA * quote_volume + (1 - VOLUME_MA_ALPHA) * self._volume_ma[symbol]
                            )
                        vol_ma    = self._volume_ma[symbol]
                        vol_ratio = quote_volume / vol_ma if vol_ma > 0 else 0.0

                        if symbol not in self._price_buffer:
                            self._price_buffer[symbol] = []
                        buf = self._price_buffer[symbol]
                        buf.append(last)
                        if len(buf) > PRICE_BUFFER_SIZE:
                            buf.pop(0)
                        price_change = (
                            abs(last - buf[0]) / buf[0] * 100 if len(buf) >= 2 and buf[0] > 0 else 0.0
                        )

                        gate1_ok = (
                            quote_volume >= GATE1_MIN_VOLUME_USDT
                            and (vol_ratio >= GATE1_VOLUME_RATIO_MIN or price_change >= GATE1_PRICE_CHANGE_MIN)
                        )

                        in_pipeline = symbol in self._pipeline_active
                        if symbol in _open_pos_symbols:
                            continue
                        if not gate1_ok and not in_pipeline:
                            continue

                        bids = ob.get("bids", [])
                        asks = ob.get("asks", [])
                        if not bids or not asks:
                            continue

                        if symbol not in self._whale_detectors:
                            self._whale_detectors[symbol] = WhaleDetector()
                        wd  = self._whale_detectors[symbol]
                        res = wd.analyze(symbol, bids, asks, {})
                        ratio, confidence, thr_sell = res["ratio"], res["confidence"], res["thr_sell"]

                        danger_level = self._get_ob_danger_level(symbol, bids, asks, ratio, confidence)
                        whale_sell_genuine = (ratio < thr_sell and confidence >= 0.5 and danger_level <= 4)

                        if whale_sell_genuine:
                            action = "skip_all" if danger_level <= 2 else "skip_gate3_only"
                            self._invalidation_signals[symbol] = {
                                "reason": "whale_sell_genuine", "level": danger_level,
                                "confidence": confidence, "ratio": ratio,
                                "action": action, "source": "gate2", "timestamp": now,
                            }
                            continue

                        if symbol in self._invalidation_signals:
                            age = now - self._invalidation_signals[symbol].get("timestamp", 0)
                            if age > 60:
                                del self._invalidation_signals[symbol]

                        if gate1_ok and not in_pipeline:
                            await self._maybe_enqueue_gate3(symbol)

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.debug("[Scanner] error koin %s: %s", symbol, e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Scanner loop error: %s", e, exc_info=True)

            await asyncio.sleep(GATE1_LOOP_INTERVAL)

    def _get_ob_danger_level(self, symbol: str, bids: list, asks: list, ratio: float, confidence: float) -> int:
        """[REUSE VERBATIM] Murni matematika orderbook, genuinely netral arah."""
        if not bids or not asks:
            return 10
        try:
            best_bid = float(bids[0][0]) if bids else 0
            best_ask = float(asks[0][0]) if asks else 0
            mid      = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
            if mid <= 0:
                return 10
            max_ask_qty   = max((float(a[1]) for a in asks[:20]), default=0)
            max_ask_price = next((float(a[0]) for a in asks[:20] if float(a[1]) == max_ask_qty), best_ask)
            wall_dist_pct = abs(max_ask_price - mid) / mid * 100
            if wall_dist_pct < 0.1: return 1
            elif wall_dist_pct < 0.3: return 2
            elif wall_dist_pct < 0.5: return 3
            elif wall_dist_pct < 1.0: return 4
            elif wall_dist_pct < 1.5: return 5
            elif wall_dist_pct < 2.0: return 6
            elif wall_dist_pct < 3.0: return 7
            else: return 8
        except Exception:
            return 10

    async def _maybe_enqueue_gate3(self, symbol: str) -> None:
        """[REUSE VERBATIM]"""
        if symbol in self._pipeline_active:
            return
        if symbol in self._invalidation_signals:
            return
        import time as _t
        _tf = self.config.get("timeframe", "15m")
        _cache_key = (symbol, _tf)
        _last_ts = self._last_candle_ts.get(_cache_key)
        if _last_ts is not None:
            _tf_seconds = {"1m":60,"3m":180,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}
            _tf_ms = _tf_seconds.get(_tf, 900) * 1000
            _now_ms = int(_t.time() * 1000)
            if _now_ms < _last_ts + _tf_ms:
                return

        open_pos_symbols = set()
        try:
            positions = await self.db.get_open_positions()
            open_pos_symbols = {p.symbol for p in positions}
        except Exception:
            pass
        if symbol in open_pos_symbols:
            return
        if symbol in self._queued_symbols:
            return

        self._pipeline_active.add(symbol)
        self._queued_symbols.add(symbol)
        await self._gate3_queue.put(symbol)
        log.debug("[Gate2→Gate3] %s masuk antrian futures (queue size=%d)", symbol, self._gate3_queue.qsize())

    async def _check_gate3_direction(
        self, symbol: str, df: "pd.DataFrame", tf: str, side: str, profile,
    ) -> bool:
        """
        [FUTURES-SPECIFIC -- BARU] Gate 3 basic filter, dipanggil terpisah
        untuk tiap arah. Mirror dari filter long-only tersembunyi yang
        ditemukan di main_spot.py (EMA9 vs EMA21, posisi vs VWAP), TAPI
        di sini eksplisit dua cabang side="long"/"short".
        """
        bar   = df.iloc[-2]
        close = float(bar["close"])
        ema9  = float(bar.get("EMA_9",  0))
        ema21 = float(bar.get("EMA_21", 0))
        ema50 = float(bar.get("EMA_50", 0))
        rsi   = float(bar.get("RSI_14", 50))
        atr   = float(bar.get("ATRr_14", 0))

        if close <= 0 or atr <= 0:
            return False
        if ema9 <= 0 or ema21 <= 0 or ema50 <= 0:
            return False

        is_long = side != "short"
        ema_ok = (ema9 > ema21) if is_long else (ema9 < ema21)
        if not ema_ok:
            return False

        try:
            rsi_min, rsi_max = profile.rsi_min, profile.rsi_max
        except Exception:
            rsi_min = self.config.get("rsi_min", 45)
            rsi_max = self.config.get("rsi_max", 77)
        if not (rsi_min <= rsi <= rsi_max):
            return False

        if tf not in ("1d", "3d", "1w"):
            for vwap_col in ("VWAP_D", "VWAP", "vwap"):
                if vwap_col in bar.index:
                    vwap_val = bar.get(vwap_col)
                    if vwap_val and float(vwap_val) > 0:
                        if is_long and close < float(vwap_val):
                            return False
                        if not is_long and close > float(vwap_val):
                            return False
                        break
        return True

    async def run_gate3_worker(self) -> None:
        """
        [FUTURES-SPECIFIC -- DIBANGUN ULANG] Gate 3,4,4.5,5 dijalankan DUA
        ARAH per simbol (cek kandidat long DAN short secara independen),
        beda dari main_spot.py yang cuma pernah cek 1 arah (long, hardcoded).
        """
        _base_workers = int(os.getenv('GATE3_WORKERS_FUTURES', os.getenv('GATE3_WORKERS', '3')))
        GATE3_WORKERS = _base_workers
        log.info("Gate3 worker (futures) dimulai (%d workers, bidirectional)", GATE3_WORKERS)

        async def _process_one(symbol: str) -> None:
            try:
                try:
                    _existing_pos = await self.db.get_open_position_by_symbol(symbol)
                    if _existing_pos is not None:
                        return
                except Exception:
                    pass

                async with self._closing_lock:
                    if symbol in self._closing_symbols:
                        return

                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all", "skip_gate3_only"):
                    return
                threshold_mult = 1.2 if (inv and inv.get("action") == "monitor") else 1.0

                from engine.profiles.registry import get_coin_profile, auto_classify_profile, _COIN_PROFILE_MAP, _PROFILE_CACHE
                _base = symbol.split("/")[0]
                if _base not in _COIN_PROFILE_MAP:
                    try:
                        _ticker     = self.ws_feed.live_tickers.get(symbol, {})
                        _spread_pct = self.ws_feed.get_current_spread_pct(symbol) or 0.0
                        auto_classify_profile(_base, _ticker, _spread_pct)
                        _PROFILE_CACHE.pop(_base, None)
                    except Exception:
                        pass
                try:
                    profile = get_coin_profile(symbol)
                    tf      = profile.timeframe
                except Exception:
                    profile = None
                    tf = self.config.get("timeframe", "15m")

                try:
                    bars = await self.exchange.fetch_ohlcv(symbol, tf, limit=self.config["lookback_candles"])
                except Exception as e:
                    log.debug("[Gate3] fetch OHLCV gagal %s: %s", symbol, e)
                    return
                if not bars or len(bars) < 60:
                    return

                confirmed_ts = bars[-2][0]
                cache_key    = (symbol, tf)
                if self._last_candle_ts.get(cache_key) == confirmed_ts:
                    return
                self._last_candle_ts[cache_key] = confirmed_ts
                if len(self._last_candle_ts) > 500:
                    oldest = sorted(self._last_candle_ts, key=lambda k: self._last_candle_ts[k])
                    for k in oldest[:250]:
                        del self._last_candle_ts[k]

                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all", "skip_gate3_only"):
                    return

                cols = ["timestamp", "open", "high", "low", "close", "volume"]
                df   = pd.DataFrame(bars, columns=cols)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)

                try:
                    import engine.ta_compat  # noqa
                    df.ta.ema(length=9,  append=True)
                    df.ta.ema(length=21, append=True)
                    df.ta.ema(length=50, append=True)
                    df.ta.rsi(length=14, append=True)
                    df.ta.atr(length=14, append=True)
                    df.ta.vwap(anchor="D", append=True)
                    df = df.dropna()
                except Exception as e:
                    log.debug("[Gate3] indikator gagal %s: %s", symbol, e)
                    return
                if len(df) < 5:
                    return

                # [FUTURES-SPECIFIC] Cek KEDUA arah, independen.
                candidate_sides = []
                if await self._check_gate3_direction(symbol, df, tf, "long", profile):
                    candidate_sides.append("long")
                if self.config.get("enable_short", True) and await self._check_gate3_direction(symbol, df, tf, "short", profile):
                    candidate_sides.append("short")

                if not candidate_sides:
                    return

                bar   = df.iloc[-2]
                close = float(bar["close"])
                atr   = float(bar.get("ATRr_14", 0))
                log.info("[Gate3→Gate4] %s lolos arah=%s", symbol, candidate_sides)

                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all",):
                    return
                if not self.strategy or not hasattr(self.strategy, "get_scored_signal"):
                    return

                confirmation_df = None
                confirmation_tf = None
                if self.config.get("confirmation_tf_enabled", True):
                    try:
                        confirmation_tf = getattr(profile, "effective_confirmation_tf", None)
                        if confirmation_tf and confirmation_tf != tf:
                            conf_bars = await self.exchange.fetch_ohlcv(symbol, confirmation_tf, limit=self.config["lookback_candles"])
                            if conf_bars and len(conf_bars) >= 20:
                                cdf = pd.DataFrame(conf_bars, columns=cols)
                                cdf["timestamp"] = pd.to_datetime(cdf["timestamp"], unit="ms", utc=True)
                                cdf.set_index("timestamp", inplace=True)
                                confirmation_df = cdf
                    except Exception as e:
                        log.debug("[Gate4] confirmation TF gagal %s: %s", symbol, e)

                ob = self.ws_feed.live_orderbooks.get(symbol, {})
                ticker = self.ws_feed.live_tickers.get(symbol, {})
                qv = ticker.get("quote_volume")
                if qv and float(qv) > 0 and close > 0:
                    df["quote_volume"] = df["volume"] * df["close"]
                    df.loc[df.index[-1], "quote_volume"] = float(qv)

                # [FUTURES-SPECIFIC] Proses tiap arah kandidat secara independen.
                for cand_side in candidate_sides:
                    try:
                        scored = await self.strategy.get_scored_signal(
                            symbol=symbol, df=df, confirmation_df=confirmation_df,
                            confirmation_timeframe=confirmation_tf, ob_data=ob,
                            side=cand_side,
                        )
                    except Exception as e:
                        log.debug("[Gate4] scored signal error %s (%s): %s", symbol, cand_side, e)
                        continue
                    if scored is None:
                        continue

                    total_score = float(getattr(scored, "total_score", 0) or 0)
                    try:
                        from engine.profiles.thresholds import get_dynamic_threshold
                        _regime_val = scored.regime.value if scored.regime else "undefined"
                        base_threshold = get_dynamic_threshold(profile.profile.value, _regime_val)
                    except Exception:
                        base_threshold = float(getattr(scored, "threshold_used", 65) or 65)
                    effective_threshold = base_threshold * threshold_mult
                    if total_score < effective_threshold:
                        continue

                    log.info("[Gate4→Gate5] %s (%s) lolos | score=%.1f threshold=%.1f",
                             symbol, cand_side, total_score, effective_threshold)

                    _kelly_size_pct: Optional[float] = None
                    if self._commander is not None:
                        try:
                            from engine.intelligence.commander import decide as _cmd_decide
                            _open_syms = []
                            try:
                                _open_pos = await self.db.get_open_positions()
                                _open_syms = [p.symbol for p in _open_pos]
                            except Exception:
                                pass
                            _cmd_decision = await _cmd_decide(
                                signal=scored, open_positions=_open_syms,
                                portfolio_value=self.portfolio_state.get("total_equity", 0.0),
                                base_risk_pct=self.config.get("risk_per_trade_pct", 1.0),
                                exchange_connector=self.ws_feed, risk_manager=self.risk_manager,
                                db_manager=self.db, side=cand_side,
                            )
                            if _cmd_decision.is_executable:
                                _kelly_size_pct = _cmd_decision.position_size_pct
                            else:
                                log.info("[Gate4.5] Commander reject %s (%s): %s",
                                         symbol, cand_side, _cmd_decision.rejection_reason)
                                continue
                        except Exception as _cmd_err:
                            log.warning("[Gate4.5] Commander error %s (%s): %s — lanjut tanpa full gate",
                                        symbol, cand_side, _cmd_err)

                    inv = self._invalidation_signals.get(symbol)
                    if inv and inv.get("action") == "skip_all":
                        continue

                    _live_ticker = self.ws_feed.live_tickers.get(symbol, {})
                    _live_price  = float(_live_ticker.get("last") or 0)
                    _exec_price  = _live_price if _live_price > 0 else close

                    signal_type = SignalType.BUY if cand_side == "long" else SignalType.OPEN_SHORT
                    signal = SignalEvent(
                        symbol=symbol, signal_type=signal_type, price=_exec_price,
                        timestamp=_utcnow_dt(), strategy="scanner_pipeline_futures",
                        confidence=float(getattr(scored, "confidence", 0.5) or 0.5),
                        stop_loss=getattr(scored, "stop_loss", None),
                        take_profit=getattr(scored, "take_profit", None),
                        metadata={
                            "atr": atr, "coin_profile": getattr(profile, "profile", "universal"),
                            "pipeline_mode": "combined_stream_futures", "total_score": total_score,
                            "kelly_size_pct": _kelly_size_pct, "side": cand_side,
                        },
                        total_score=total_score, regime=getattr(scored, "regime", "undefined"),
                        score_breakdown=getattr(scored, "score_breakdown", {}),
                        scoring_narrative=getattr(scored, "narrative", ""),
                    )
                    await self._handle_entry(signal, cand_side)
                    # Begitu satu arah entry berhasil diproses, symbol ini
                    # sudah "dipakai" siklus ini -- tidak proses arah lain
                    # utk symbol yang sama di siklus yang sama.
                    break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("[Gate3Worker] error %s: %s", symbol, e, exc_info=True)
            finally:
                self._pipeline_active.discard(symbol)
                self._queued_symbols.discard(symbol)

        async def _worker(worker_id: int) -> None:
            while self.is_running:
                try:
                    symbol = await asyncio.wait_for(self._gate3_queue.get(), timeout=5.0)
                    await _process_one(symbol)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("[Gate3 worker-%d] error: %s", worker_id, e)

        workers = [asyncio.create_task(_worker(i)) for i in range(GATE3_WORKERS)]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for w in workers:
                w.cancel()
            raise



    async def _refresh_portfolio(self) -> None:
        """
        [FUTURES-SPECIFIC -- FORMULA DIBANGUN ULANG] BEDA MENDASAR dari spot:
        equity = free_margin + used_margin + unrealized_pnl (BUKAN
        free_balance + notional_value_posisi seperti spot -- itu salah total
        untuk futures leverage-based margin).
        """
        import time as _t
        self._last_refresh_time = _t.monotonic()
        async with self._equity_lock:
            try:
                balance = await self.exchange.fetch_balance()
                quote   = self.config["quote_currency"]
                free_margin = float(balance.get("free", {}).get(quote, 0) or 0)
                used_margin = float(balance.get("used", {}).get(quote, 0) or 0)

                positions = await self.db.get_open_positions()

                unrealized_pnl = 0.0
                current_prices: Dict[str, float] = {}
                for p in positions:
                    price = await self._get_current_price(p.symbol)
                    if price is None or price <= 0:
                        price = float(p.current_price or p.entry_price or 0.0)
                    current_prices[p.symbol] = price
                    entry = float(p.entry_price or 0.0)
                    amount = float(p.amount or 0.0)
                    if price > 0 and entry > 0 and amount > 0:
                        if p.side == "long":
                            unrealized_pnl += (price - entry) * amount
                        else:
                            unrealized_pnl += (entry - price) * amount

                # [FUTURES-SPECIFIC] equity = margin total (free+used) + unrealized PnL.
                # margin total merepresentasikan modal yang benar-benar
                # dipertaruhkan (locked sbg margin posisi + yang masih bebas),
                # BUKAN notional value penuh posisi (itu logic spot yg salah
                # kalau dipakai di sini -- notional bisa berkali lipat dari
                # margin sebenarnya kalau leverage tinggi).
                total_eq = free_margin + used_margin + unrealized_pnl

                log.debug(
                    "Equity calc (futures): free_margin=%.4f used_margin=%.4f "
                    "unrealized_pnl=%.4f -> total_eq=%.4f",
                    free_margin, used_margin, unrealized_pnl, total_eq,
                )

                prev_eq = self.portfolio_state.get("total_equity", 0.0)
                if prev_eq > 0:
                    change_pct = abs(total_eq - prev_eq) / prev_eq * 100
                    if change_pct > 20:
                        log.warning(
                            "Equity jump mencurigakan (futures): %.4f → %.4f (%.1f%%) — periksa kalkulasi",
                            prev_eq, total_eq, change_pct,
                        )

                eq_day_start = self.risk_manager.equity_at_day_start
                if eq_day_start > 0:
                    daily_pnl     = total_eq - eq_day_start
                    daily_pnl_pct = daily_pnl / eq_day_start * 100
                else:
                    daily_pnl, daily_pnl_pct = 0.0, 0.0

                self.portfolio_state = {
                    "total_equity":   round(total_eq, 4),
                    "free_margin":    round(free_margin, 4),
                    "used_margin":    round(used_margin, 4),
                    "unrealized_pnl": round(unrealized_pnl, 4),
                    "daily_pnl":      round(daily_pnl, 4),
                    "daily_pnl_pct":  round(daily_pnl_pct, 4),
                }
                self.config["portfolio_value"] = total_eq

                prev_halted = self.risk_manager.is_halted
                avg_atr_pct = 0.0
                if positions:
                    atr_vals = [
                        (p.atr_at_entry / p.entry_price * 100)
                        for p in positions if p.atr_at_entry and p.entry_price and p.entry_price > 0
                    ]
                    avg_atr_pct = sum(atr_vals) / len(atr_vals) if atr_vals else 0.0

                # [FUTURES-SPECIFIC] free_balance yg diteruskan ke risk_manager
                # = free_margin (bukan notional/cash spot) -- inilah yg dicek
                # risk_future.py sbg "margin tersedia utk posisi baru".
                self.risk_manager.update_portfolio_state(
                    equity=total_eq, initial_equity=self.config["initial_capital"],
                    free_balance=free_margin, open_positions_count=len(positions),
                    atr_pct=avg_atr_pct,
                )

                if not prev_halted and self.risk_manager.is_halted and self.notifier:
                    await self.notifier.notify_bot_halted(
                        reason=self.risk_manager._halt_reason.value,
                        detail=self.risk_manager._halt_detail,
                    )

                await self.db.save_snapshot({
                    "timestamp": datetime.now(timezone.utc).replace(tzinfo=None),
                    "total_equity": round(total_eq, 4),
                    "free_balance": round(free_margin, 4),
                    "locked_balance": round(used_margin, 4),
                    "open_pnl": round(unrealized_pnl, 4),
                    "daily_pnl": round(daily_pnl, 4),
                    "daily_pnl_pct": round(daily_pnl_pct, 4),
                    "drawdown_pct": round(self.risk_manager.current_drawdown_pct, 4),
                })

                # [FUTURES-SPECIFIC] Update current_price & unrealized_pnl tiap
                # posisi di DB (dipakai dashboard, dan oleh check liquidation
                # proximity di run_sl_tp_monitor).
                for p in positions:
                    price = current_prices.get(p.symbol)
                    if price and price > 0 and p.entry_price:
                        entry = float(p.entry_price)
                        amount = float(p.amount or 0)
                        if p.side == "long":
                            pos_pnl = (price - entry) * amount
                        else:
                            pos_pnl = (entry - price) * amount
                        pos_pnl_pct = (pos_pnl / (entry * amount) * 100) if (entry * amount) > 0 else 0.0
                        try:
                            await self.db.update_position_price(p.symbol, price, pos_pnl, pos_pnl_pct)
                        except Exception:
                            pass

            except Exception as e:
                log.error("Portfolio refresh error (futures): %s", e, exc_info=True)

    async def run_sl_tp_monitor(self) -> None:
        """
        [REUSE mayoritas dari main_spot.py -- SUDAH side-aware sejak
        perbaikan bias long-only] check_breakeven_sl/check_trailing_sl/
        check_atg semuanya baca pos.side dgn benar. TAMBAHAN BARU khusus
        futures: cek proximity liquidation SEBELUM cek SL/TP normal --
        kalau harga sudah masuk zona bahaya (LIQUIDATION_EMERGENCY_PROXIMITY_PCT
        dari liquidation_price), tutup paksa sbg emergency exit.
        """
        from engine.profiles.registry import get_coin_profile
        while self.is_running:
            try:
                positions = await self.db.get_open_positions()
                for pos in positions:
                    async with self._closing_lock:
                        is_closing = pos.symbol in self._closing_symbols
                    if is_closing:
                        continue

                    price = await self._get_current_price(pos.symbol)
                    if price is None or price <= 0:
                        log.warning("Tidak bisa ambil harga untuk %s — skip cycle ini.", pos.symbol)
                        continue

                    # ══════════════════════════════════════════════════════
                    # [FUTURES-SPECIFIC -- BARU] Cek proximity liquidation
                    # DULU, sebelum apapun lain. Ini lapis pengaman independen
                    # dari SL normal -- kalau entah kenapa SL gagal ter-trigger
                    # tepat waktu (network lag, dst), ini jaring pengaman kedua.
                    # ══════════════════════════════════════════════════════
                    if pos.liquidation_price:
                        liq = float(pos.liquidation_price)
                        entry = float(pos.entry_price or 0)
                        if entry > 0:
                            if pos.side == "long":
                                dist_to_liq_pct = (price - liq) / liq * 100 if liq > 0 else 999
                                in_danger = dist_to_liq_pct <= self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT
                            else:
                                dist_to_liq_pct = (liq - price) / liq * 100 if liq > 0 else 999
                                in_danger = dist_to_liq_pct <= self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT

                            if in_danger:
                                log.critical(
                                    "⚠️ LIQUIDATION PROXIMITY DARURAT: %s %s | price=%.6f "
                                    "liq_price≈%.6f (APPROXIMATE) | jarak=%.2f%% <= ambang %.1f%% "
                                    "— EMERGENCY CLOSE.",
                                    pos.side.upper(), pos.symbol, price, liq,
                                    dist_to_liq_pct, self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT,
                                )
                                if self.notifier:
                                    try:
                                        await self.notifier.notify_error(
                                            "liquidation_proximity",
                                            f"{pos.symbol} ({pos.side}) mendekati liquidation "
                                            f"({dist_to_liq_pct:.2f}%% dari estimasi liq_price) "
                                            f"— emergency close dipicu.",
                                        )
                                    except Exception:
                                        pass
                                await self._close_position_market(pos, price, "liquidation_proximity_emergency")
                                continue

                    if pos.side == "long" and price > (pos.highest_price or 0):
                        await self.db.update_position_highest_price(pos.symbol, price)
                        pos.highest_price = price
                    elif pos.side == "short" and (pos.highest_price is None or price < pos.highest_price):
                        await self.db.update_position_highest_price(pos.symbol, price)
                        pos.highest_price = price

                    new_sl = self.risk_manager.check_breakeven_sl(
                        entry_price=pos.entry_price, current_price=price,
                        current_sl=pos.stop_loss_price, take_profit=pos.take_profit_price,
                        side=pos.side,
                    )
                    if new_sl is not None and (pos.stop_loss_price is None or new_sl != pos.stop_loss_price):
                        log.info("BREAKEVEN SL | %s | %.6f → %.6f", pos.symbol, pos.stop_loss_price, new_sl)
                        await self.db.update_position_sl(pos.symbol, new_sl)
                        pos.stop_loss_price = new_sl

                    current_atr = pos.atr_at_entry
                    try:
                        _mon_profile = get_coin_profile(pos.symbol, override_profile=pos.strategy_profile)
                        _mon_tf = _mon_profile.effective_confirmation_tf
                        candles = await self.exchange.fetch_ohlcv(pos.symbol, _mon_tf, limit=20)
                        if candles and len(candles) >= 15:
                            df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume"])
                            df = df.astype({"high": float, "low": float, "close": float})
                            df.ta.atr(length=14, append=True)
                            atr_col = [c for c in df.columns if "ATRr" in c or "ATR" in c]
                            if atr_col:
                                atr_live = df[atr_col[0]].dropna().iloc[-1]
                                if atr_live > 0:
                                    current_atr = float(atr_live)
                    except Exception as _e:
                        log.debug("ATR live gagal untuk %s: %s", pos.symbol, _e)

                    if current_atr and current_atr > 0 and pos.stop_loss_price is not None:
                        _coin_profile = None
                        if hasattr(self, 'strategy') and self.strategy:
                            _coin_profile = self.strategy.get_profile(pos.symbol)
                        _profile_name = _coin_profile.profile.value if _coin_profile else ""

                        new_trailing_sl = self.risk_manager.check_trailing_sl(
                            entry_price=pos.entry_price, current_price=price,
                            current_sl=pos.stop_loss_price, atr=current_atr,
                            side=pos.side, strategy_profile=_profile_name,
                        )
                        if new_trailing_sl is not None and new_trailing_sl != pos.stop_loss_price:
                            log.info("TRAILING SL | %s | %.6f → %.6f", pos.symbol, pos.stop_loss_price, new_trailing_sl)
                            await self.db.update_position_sl(pos.symbol, new_trailing_sl)
                            pos.stop_loss_price = new_trailing_sl

                    hit_sl = (
                        pos.stop_loss_price is not None and (
                            (pos.side == "long" and price <= pos.stop_loss_price)
                            or (pos.side == "short" and price >= pos.stop_loss_price)
                        )
                    )
                    hit_tp = (
                        pos.take_profit_price is not None and (
                            (pos.side == "long" and price >= pos.take_profit_price)
                            or (pos.side == "short" and price <= pos.take_profit_price)
                        )
                    )

                    # ── Adaptive Trade Guardian (ATG) ──
                    try:
                        from engine.intelligence.trade_guardian import check_atg
                        _atg_df = None
                        try:
                            _atg_profile = get_coin_profile(pos.symbol, override_profile=pos.strategy_profile)
                            _atg_tf = _atg_profile.effective_confirmation_tf
                            _atg_candles = await self.exchange.fetch_ohlcv(pos.symbol, _atg_tf, limit=50)
                            if _atg_candles and len(_atg_candles) >= 15:
                                _atg_df = pd.DataFrame(
                                    _atg_candles, columns=["ts","open","high","low","close","volume"]
                                ).astype({"high":float,"low":float,"close":float,"volume":float})
                        except Exception as _atg_fe:
                            log.debug("ATG fetch candles gagal [%s]: %s", pos.symbol, _atg_fe)

                        _atg_regime = pos.entry_regime or "trending_bull"
                        _atg_side   = pos.side or "long"
                        _atg_result = check_atg(
                            entry_price=pos.entry_price or 0.0, current_price=price,
                            highest_price=pos.highest_price or max(price, pos.entry_price or price),
                            current_sl=pos.stop_loss_price, df=_atg_df, symbol=pos.symbol,
                            regime=_atg_regime, side=_atg_side,
                        )
                        if _atg_result.new_sl is not None:
                            _atg_is_long = _atg_side != "short"
                            _sl_improved = (
                                pos.stop_loss_price is None
                                or (_atg_is_long and _atg_result.new_sl > pos.stop_loss_price)
                                or (not _atg_is_long and _atg_result.new_sl < pos.stop_loss_price)
                            )
                            if _sl_improved:
                                await self.db.update_position_sl(pos.symbol, _atg_result.new_sl)
                                pos.stop_loss_price = _atg_result.new_sl
                    except Exception as _atg_err:
                        log.debug("ATG error [%s]: %s", pos.symbol, _atg_err)

                    trailing_reason = None
                    if self.strategy and hasattr(self.strategy, "check_trailing_exit"):
                        trailing_reason = self.strategy.check_trailing_exit(pos.symbol, price)

                    if hit_sl:
                        await self._close_position_market(pos, price, f"Stop-loss hit @ {pos.stop_loss_price:.6f}")
                    elif hit_tp:
                        await self._close_position_market(pos, price, f"Take-profit hit @ {pos.take_profit_price:.6f}")
                    elif trailing_reason:
                        await self._close_position_market(pos, price, trailing_reason)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SL/TP monitor error (futures): %s", e, exc_info=True)

            await asyncio.sleep(self.SL_TP_CHECK_INTERVAL)

    async def run_portfolio_monitor(self) -> None:
        while self.is_running:
            try:
                await self._refresh_portfolio()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Portfolio monitor error: %s", e)
            await asyncio.sleep(30)

    async def run_daily_summary(self) -> None:
        while self.is_running:
            try:
                now = _utcnow_dt()
                if (now.hour == self.DAILY_SUMMARY_HOUR and now.minute >= self.DAILY_SUMMARY_MIN
                        and not self._daily_summary_sent):
                    if self.notifier:
                        try:
                            await self.notifier.notify_daily_summary(self.portfolio_state, self.risk_manager)
                        except Exception as e:
                            log.debug("notify_daily_summary gagal: %s", e)
                    self._daily_summary_sent = True
                elif now.hour == 0:
                    self._daily_summary_sent = False
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Daily summary error: %s", e)
            await asyncio.sleep(60)

    async def run_analytics_loop(self) -> None:
        if not self.config.get("analytics_enabled", True):
            return
        interval = self.config.get("analytics_refresh_interval", 3600)
        while self.is_running:
            try:
                await asyncio.sleep(interval)
                if self._analytics and hasattr(self._analytics, "refresh"):
                    await self._analytics.refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Analytics loop error: %s", e)

    async def run_config_watcher(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(15)
                if self.db is None:
                    continue
                try:
                    overrides = await self.db.get_bot_config_overrides()
                except Exception:
                    overrides = None
                if overrides:
                    self.config.update(overrides)
                    if self.risk_manager:
                        self.risk_manager._update_config(self.config)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Config watcher error: %s", e)

    async def run(self) -> None:
        await self.start()

        try:
            await self._refresh_portfolio()
            log.info("Portfolio startup refresh (futures): equity=%.4f", self.portfolio_state.get("total_equity", 0))
        except Exception as _pe:
            log.warning("Portfolio startup refresh gagal: %s", _pe)

        # [CATATAN] run_position_sync_loop() SENGAJA TIDAK diikutsertakan --
        # position_sync_futures.py belum dibangun (item terpisah di roadmap).
        # run_coin_swap_loop() TIDAK diikutsertakan -- sistem itu deprecated
        # permanen, tidak relevan sama sekali di futures.
        self._tasks = [
            asyncio.create_task(self.run_scanner_loop(),      name="task_scanner_futures"),
            asyncio.create_task(self.run_gate3_worker(),      name="task_gate3_worker_futures"),
            asyncio.create_task(self.run_portfolio_monitor(), name="task_portfolio_futures"),
            asyncio.create_task(self.run_sl_tp_monitor(),     name="task_sl_tp_futures"),
            asyncio.create_task(self.run_daily_summary(),     name="task_daily_summary_futures"),
            asyncio.create_task(self.run_analytics_loop(),    name="task_analytics_futures"),
            asyncio.create_task(self.run_config_watcher(),    name="task_config_watcher_futures"),
        ]

        if self.notifier is not None and self.config.get('telegram_enabled', False):
            self._tasks.append(
                asyncio.create_task(self.notifier.start_background_tasks(), name="task_notifier_background_futures")
            )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()


async def main() -> None:
    bot = TradingBot()

    try:
        from future.api_server_future import create_app
        app = create_app(lambda: bot)
    except ImportError:
        app = None
        log.warning("future/api_server_future.py belum dibangun -- dashboard API futures tidak tersedia.")

    loop = asyncio.get_running_loop()

    def _on_shutdown_signal():
        log.info("Shutdown signal diterima (futures) — menghentikan dengan graceful...")
        for task in bot._tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_shutdown_signal)
        except NotImplementedError:
            pass

    try:
        if app is not None:
            server = uvicorn.Server(uvicorn.Config(
                app=app, host=bot.config["api_host"], port=bot.config["api_port"],
                log_level="warning", access_log=False, loop="asyncio",
            ))
            log.info("Dashboard API (futures): http://%s:%d", bot.config["api_host"], bot.config["api_port"])
            await asyncio.gather(bot.run(), server.serve())
        else:
            await bot.run()
    except BotStartupError as e:
        log.critical("Bot futures startup FAILED: %s", e)
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
