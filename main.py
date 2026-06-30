"""
main.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2 (final sweep — sambungkan Kelly sizing ke eksekusi nyata):
  [BUG-FIX KRITIS] Gate 4.5 (commander.py) menghitung position_size_pct
           penuh (Kelly criterion + quality_mult + consec_mult + correlation
           penalty) tapi hasilnya HANYA pernah dibaca untuk log — tidak
           pernah disambungkan ke SignalEvent/eksekusi. _handle_buy() selalu
           pakai max_position_size_pct flat dari config; risk.py menghitung
           size final via ATR fixed-fractional risk, sama sekali lepas dari
           Kelly. Sekarang _kelly_size_pct ditangkap di Gate 4.5 (hanya saat
           commander BENAR2 approve, bukan di jalur fallback Entry 2),
           diteruskan via SignalEvent.metadata, dan diterapkan di
           _handle_buy() sebagai CEILING TAMBAHAN setelah risk_manager
           approve — Kelly cuma bisa MENGURANGI assessment.approved_size,
           tidak pernah menaikkannya di atas ATR-sizing/max_pct yang sudah
           ada. Kalau Kelly unavailable (commander None/error), behavior
           identik dgn sebelum fix (tidak ada perubahan default).
"""

from __future__ import annotations
from intelligence.position_sync import run_position_sync

import asyncio
import logging
import logging.handlers
import os
import signal
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import uvicorn
from dotenv import load_dotenv
try:
    from constants import APP_VERSION, MAX_CANDLE_CACHE
except ImportError:
    APP_VERSION = "7.0"

from profiles.registry import get_profile_summary, set_profile_override, get_coin_profile
from database import DatabaseManager
from exchange import ExchangeConnector, WebSocketFeed
from strategy import get_strategy, SignalType, SignalEvent, ExitMode, PositionTracker
from risk import RiskManager, RiskAssessment, RiskDecision, HaltReason
from execution import OrderExecutionManager
from api_server import create_app
from notifications import NotificationManager
from indicators.orderbook import WhaleDetector  # [v2] dipindah dari main.py ke indicators/orderbook.py

load_dotenv()

log = logging.getLogger("main")


class BotStartupError(Exception):
    pass

def setup_logging() -> None:
    # Windows consoles may default to cp1252; ensure logs don't crash on unicode.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file  = os.getenv("LOG_FILE", "logs/trading_bot.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-16s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Guard: jangan tambah handler kalau sudah ada (hindari double log)
    if root.handlers:
        return
    # StreamHandler hanya untuk console langsung (bukan redirect)
    # Cek apakah stdout adalah terminal — kalau redirect, skip StreamHandler
    if sys.stdout.isatty():
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(log_level)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(log_level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    for noisy in ("ccxt", "aiohttp", "asyncio", "websockets", "urllib3", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

def _utcnow_dt():
    return datetime.now(timezone.utc).replace(tzinfo=None)



class TradingBot:

    SNAPSHOT_INTERVAL    = 900  # 15 menit — cukup untuk analisis tren
    CANDLE_POLL_INTERVAL = 10
    SL_TP_CHECK_INTERVAL = 5
    DAILY_SUMMARY_HOUR   = 23
    DAILY_SUMMARY_MIN    = 55

    def __init__(self) -> None:
        self.config = self._load_config()

        self.is_running = False
        self.start_time: Optional[datetime] = None

        self.portfolio_state: Dict = {
            "total_equity":   0.0,
            "free_balance":   0.0,
            "locked_balance": 0.0,
            "open_pnl":       0.0,
            "daily_pnl":      0.0,
            "daily_pnl_pct":  0.0,
        }

        self.db:           Optional[DatabaseManager]       = None
        self.exchange:     Optional[ExchangeConnector]     = None
        self.ws_feed:      Optional[WebSocketFeed]         = None
        self.risk_manager: Optional[RiskManager]           = None
        self.strategy:     Optional[object]                = None
        self.executor:     Optional[OrderExecutionManager] = None
        self.notifier:     Optional[NotificationManager]   = None

        self._commander    = None
        self._analytics    = None
        self._meta_learner = None
        self._tasks:              List[asyncio.Task] = []
        self._daily_summary_sent: bool               = False
        self._closing_lock:    asyncio.Lock = asyncio.Lock()
        self._equity_lock:     asyncio.Lock = asyncio.Lock()
        self._closing_symbols: Set[str]     = set()
        self._close_retry_count: Dict[str, int] = {}
        self._last_refresh_time: float = 0.0
        self._ob_snapshot_history: Dict[str, Dict] = {}
        self._ob_wall_first_seen:  Dict[str, float] = {}
        self._whale_detectors:     Dict[str, WhaleDetector] = {}
        self._MIN_REFRESH_INTERVAL: float = 5.0

        # ── Combined Stream / Corong 200 Koin ──────────────────────────────
        self._pipeline_active:      Set[str]        = set()
        self._queued_symbols:       Set[str]        = set()  # track simbol di gate3_queue
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
        raw_universe = os.getenv("UNIVERSE_WATCHLIST", "BTC/USDT,ETH/USDT")
        return {
            "exchange_id":           os.getenv("EXCHANGE_ID", "binance"),
            "api_key":               os.getenv("API_KEY", ""),
            "api_secret":            os.getenv("API_SECRET", ""),
            "api_passphrase":        os.getenv("API_PASSPHRASE", ""),
            "testnet":               os.getenv("TESTNET", "true").lower() == "true",
            "quote_currency":        os.getenv("QUOTE_CURRENCY", "USDT"),
            "initial_capital":       float(os.getenv("INITIAL_CAPITAL", "1000")),
            "max_open_positions":    int(os.getenv("MAX_OPEN_POSITIONS", "3")),
            "universe_watchlist":     [s.strip() for s in raw_universe.split(",") if s.strip()],
            "coin_swap_enabled":         os.getenv("COIN_SWAP_ENABLED", "false").lower() == "true",
            "cross_learn_enabled":        os.getenv("CROSS_LEARN_ENABLED", "false").lower() == "true",
            "strategy":              os.getenv("STRATEGY", "volumetric_breakout"),
            "timeframe":             os.getenv("TIMEFRAME", "15m"),
            "lookback_candles":      int(os.getenv("LOOKBACK_CANDLES", "200")),
            "database_url":          os.getenv(
                "DATABASE_URL",
                "sqlite+aiosqlite:///./data/trading_bot.db",
            ),
            "api_host":              os.getenv("API_HOST", "0.0.0.0"),
            "api_port":              int(os.getenv("API_PORT", "8000")),
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
            "quick_tp_pct":            float(os.getenv("QUICK_TP_PCT",            "1.75")),
            "quick_sl_pct":            float(os.getenv("QUICK_SL_PCT",            "1.20")),
            "trailing_activation_pct": float(os.getenv("TRAILING_ACTIVATION_PCT", "1.50")),
            "trailing_gap_pct":        float(os.getenv("TRAILING_GAP_PCT",        "0.50")),
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
            "regime_detection_enabled":   os.getenv("REGIME_DETECTION_ENABLED", "true").lower() == "true",
            "min_score_override":         os.getenv("MIN_SCORE_OVERRIDE", ""),
            "confirmation_tf_enabled":    os.getenv("CONFIRMATION_TIMEFRAME_ENABLED", "true").lower() == "true",
            "analytics_enabled":          os.getenv("ANALYTICS_ENABLED", "true").lower() == "true",
            "meta_learner_enabled":       os.getenv("META_LEARNER_ENABLED", "false").lower() == "true",
            "meta_learner_mode":          os.getenv("META_LEARNER_MODE", "advisory"),
            "meta_learner_min_sample":    int(os.getenv("META_LEARNER_MIN_SAMPLE", "50")),
            "meta_learner_max_change":    int(os.getenv("META_LEARNER_MAX_THRESHOLD_CHANGE", "10")),
            "analytics_refresh_interval": int(os.getenv("ANALYTICS_REFRESH_INTERVAL", "3600")),
        }

    async def start(self) -> None:
        setup_logging()
        log.info("=" * 70)
        log.info("  AlgoTrader Pro v%s — Starting", APP_VERSION)
        log.info("=" * 70)

        Path("data").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)

        self.notifier = NotificationManager(self.config)

        self.db = DatabaseManager(self.config["database_url"])
        await self.db.init_db()
        log.info("Database ready: %s", self.config["database_url"])

        _exid       = self.config["exchange_id"]
        _passphrase = self.config.get("api_passphrase", "")
        _exchanges_need_passphrase = {"okx", "kucoin", "bitget"}
        _exchanges_no_passphrase   = {"binance", "bybit"}
        if _exid in _exchanges_need_passphrase and not _passphrase:
            msg = f"Exchange {_exid.upper()} membutuhkan API_PASSPHRASE — isi dulu di .env atau /setconfig api_passphrase"
            log.critical(msg)
            await self.notifier.notify_error("startup", msg)
            raise BotStartupError(msg)
        if _exid in _exchanges_no_passphrase and _passphrase:
            log.info("[startup] Exchange %s tidak pakai passphrase — field diabaikan.", _exid)
            self.config["api_passphrase"] = ""

        self.exchange = ExchangeConnector(
            exchange_id=self.config["exchange_id"],
            api_key=self.config["api_key"],
            api_secret=self.config["api_secret"],
            api_passphrase=self.config.get("api_passphrase", ""),
            testnet=self.config["testnet"],
            db=self.db,
        )
        connected = await self.exchange.connect()
        if not connected:
            log.critical("Exchange connection FAILED.")
            await self.notifier.notify_error(
                "startup",
                "Exchange connection FAILED — bot tidak bisa start.",
            )
            raise BotStartupError("Exchange connection FAILED.")

        if not self.config["testnet"]:
            await self._live_preflight()

        # ── Auto-scan universe dari Binance ──
        from exchange import auto_scan_and_populate
        scanned = await auto_scan_and_populate(self.db)
        if scanned:
            self.config["universe_watchlist"] = scanned
            log.info("universe_watchlist diupdate dari auto_scan: %d koin", len(scanned))

        self.ws_feed = WebSocketFeed(
            exchange_id=self.config["exchange_id"],
            api_key=self.config["api_key"],
            api_secret=self.config["api_secret"],
            api_passphrase=self.config.get("api_passphrase", ""),
            symbols=self.config["universe_watchlist"],
            testnet=self.config["testnet"],
        )
        await self.ws_feed.start()
        log.info(
            "WebSocketFeed subscribe ke %d koin universe (universe aktif: %d)",
            len(self.config["universe_watchlist"]),
            len(self.config["universe_watchlist"]),
        )
        # Refresh PROFILE_CACHE sekali setelah WS ready (30 detik)
        async def _delayed_refresh():
            await asyncio.sleep(30)
            if self.strategy:
                from profiles.registry import _PROFILE_CACHE
                _PROFILE_CACHE.clear()
                log.info("Profile cache di-clear — akan di-rebuild dengan data ticker real")
        asyncio.create_task(_delayed_refresh(), name="refresh_profiles")

        self.risk_manager = RiskManager(self.config, db=self.db)

        saved_halt = await self.db.get_bot_state("halt_state")
        if saved_halt:
            parts      = saved_halt.split("|||", 1)
            reason_str = parts[0]
            detail     = parts[1] if len(parts) > 1 else ""
            try:
                reason = HaltReason(reason_str)
                if reason in (HaltReason.MAX_DRAWDOWN, HaltReason.PANIC_BUTTON):
                    self.risk_manager.halt_trading(reason, detail)
                    log.critical(
                        "STARTUP: Bot di-halt dari session sebelumnya [%s]: %s "
                        "— Review manual diperlukan sebelum resume.",
                        reason.value, detail,
                    )
                    await self.notifier.notify_bot_halted(reason.value, detail)
                else:
                    log.info(
                        "Halt state sebelumnya [%s] tidak di-restore (bukan MAX_DRAWDOWN/PANIC).",
                        reason_str,
                    )
                    await self.db.clear_bot_state("halt_state")
            except ValueError:
                log.warning("Halt state tidak dikenal di DB: %s — diabaikan.", reason_str)
                await self.db.clear_bot_state("halt_state")

        self.strategy = get_strategy(
            name=self.config["strategy"],
            symbols=self.config["universe_watchlist"],
            timeframe=self.config["timeframe"],
            params={
                "atr_sl_mult":             self.config["atr_multiplier_sl"],
                "atr_tp_mult":             self.config["atr_multiplier_tp"],
                "sentiment_enabled":       self.config["sentiment_enabled"],
                "volume_multiplier":       self.config.get("volume_multiplier",       1.3),
                "volume_spike_threshold":  self.config.get("volume_spike_threshold",  3.0),
                "rsi_min":                 self.config.get("rsi_min",                 45),
                "rsi_max":                 self.config.get("rsi_max",                 77),
                "rsi_golden_cross_min":    self.config.get("rsi_golden_cross_min",    45),
                "atr_pct_threshold":       self.config.get("atr_pct_threshold",       0.8),
                "quick_tp_pct":            self.config.get("quick_tp_pct",            1.75),
                "quick_sl_pct":            self.config.get("quick_sl_pct",            1.20),
                "trailing_activation_pct": self.config.get("trailing_activation_pct", 1.50),
                "trailing_gap_pct":        self.config.get("trailing_gap_pct",        0.50),
            },
        )

        # Inject notifier ke strategy untuk regime change notification
        if hasattr(self.strategy, '_notifier'):
            self.strategy._notifier = self.notifier
            log.info("Notifier injected ke strategy ✅")
        # Inject db ke strategy untuk save regime
        if hasattr(self.strategy, '_db'):
            self.strategy._db = self.db
            log.info("DB injected ke strategy ✅")
        if hasattr(self.strategy, '_scorer') and self.strategy._scorer is not None:
            self.strategy._scorer._db = self.db
            log.info("DB injected ke scorer ✅")
        # Inject ws_feed ke strategy untuk auto-classify profile koin baru
        if hasattr(self.strategy, '_ws_feed'):
            self.strategy._ws_feed = self.ws_feed
            log.info("WS Feed injected ke strategy ✅")
        if hasattr(self.strategy, '_validator') and self.strategy._validator is not None:
            self.strategy._validator._db = self.db
            log.info("DB injected ke validator ✅")

        if hasattr(self.strategy, "print_profile_summary"):
            self.strategy.print_profile_summary()

        log.info(get_profile_summary(self.config["universe_watchlist"]))

        open_positions = await self.db.get_open_positions()
        open_symbols: Set[str] = {p.symbol for p in open_positions}

        closing_positions = await self.db.get_closing_positions()
        async with self._closing_lock:
            self._closing_symbols = {p.symbol for p in closing_positions}
        if self._closing_symbols:
            log.warning(
                "Startup: %d symbol masih dalam status closing dari session sebelumnya: %s",
                len(self._closing_symbols), self._closing_symbols,
            )

        await self._reconcile_positions_on_startup()

        open_positions = await self.db.get_open_positions()
        open_symbols   = {p.symbol for p in open_positions}

        self.strategy.sync_position_state(open_symbols, open_positions)
        log.info(
            "Posisi disinkronisasi: %d terbuka — %s",
            len(open_symbols), open_symbols or "tidak ada",
        )

        self.executor = OrderExecutionManager(
            exchange=self.exchange,
            db=self.db,
            on_trade_executed=self._on_trade_executed,
            max_slippage_pct=self.config["max_slippage_pct"],
            ws_feed=self.ws_feed,
        )

        await self._initialize_intelligence_pipeline()

        await self._refresh_portfolio()

        self.is_running = True
        self.start_time = datetime.now(timezone.utc).replace(tzinfo=None)

        await self.db.save_log(
            "INFO", "main",
            f"AlgoTrader Pro v{APP_VERSION} started | "
            f"mode={'TESTNET' if self.config['testnet'] else 'LIVE'} | "
            f"universe={self.config['universe_watchlist']} | "
            f"strategy={self.config['strategy']} | "
            f"intelligence={'ON' if self.config['intelligence_enabled'] else 'OFF'} | "
            f"analytics={'ON' if self.config['analytics_enabled'] else 'OFF'}",
        )
        log.info(
            "Bot started | mode=%s | symbols=%s | strategy=%s v%s",
            "TESTNET" if self.config["testnet"] else "LIVE",
            self.config["universe_watchlist"],
            self.config["strategy"],
            APP_VERSION,
        )

    async def _initialize_intelligence_pipeline(self) -> None:
        if not self.config.get("intelligence_enabled", True):
            log.info("Intelligence pipeline di-nonaktifkan via config (INTELLIGENCE_ENABLED=false).")
            return

        try:
            from intelligence.commander import IntelligenceCommander
            self._commander = IntelligenceCommander(
                db=self.db,
                config=self.config,
            )
            self._commander.inject_dependencies(
                exchange_connector=self.ws_feed,
                risk_manager=self.risk_manager,
            )
            log.info("Intelligence commander: AKTIF")
            try:
                from intelligence.classifier import restore_regimes_from_db
                await restore_regimes_from_db(self.config["universe_watchlist"], self.db)
            except Exception as _re:
                log.warning("Regime restore gagal: %s", _re)
        except ImportError:
            log.info(
                "intelligence/commander.py belum tersedia — "
                "menggunakan pipeline strategy lama."
            )

        if self.config.get("analytics_enabled", True):
            try:
                from learning.analytics import PerformanceAnalytics
                self._analytics = PerformanceAnalytics(db=self.db, config=self.config)

                await self._analytics.load_persistent_parameters()
                log.info("Performance analytics: AKTIF")
            except ImportError:
                log.info("learning/analytics.py belum tersedia — analytics di-skip.")
            except Exception as e:
                log.warning("Analytics init gagal: %s — analytics di-skip.", e)

        if self.config.get("meta_learner_enabled", False) and self._analytics:
            try:
                from learning.meta_learner import MetaLearner
                self._meta_learner = MetaLearner(
                    db_manager=self.db,
                    analytics_engine=self._analytics,
                    mode=self.config.get("meta_learner_mode", "advisory"),
                    min_sample=int(self.config.get("meta_learner_min_sample", 50)),
                    max_threshold_change=float(self.config.get("meta_learner_max_change", 10)),
                )
                mode = self.config.get("meta_learner_mode", "advisory")
                log.info("Meta-learner: AKTIF (mode=%s)", mode)
                await self.db.save_log(
                    "INFO", "main",
                    f"Meta-learner aktif | mode={mode} | "
                    f"min_sample={self.config['meta_learner_min_sample']}",
                )
            except ImportError:
                log.info("learning/meta_learner.py belum tersedia — meta-learner di-skip.")
            except Exception as e:
                log.warning("Meta-learner init gagal: %s — meta-learner di-skip.", e)
        elif self.config.get("meta_learner_enabled", False):
            log.warning(
                "META_LEARNER_ENABLED=true tapi analytics tidak aktif — "
                "meta-learner membutuhkan analytics. Meta-learner di-skip."
            )

        # Cross-learning: CoinSwapEngine
        self._coin_swap = None
        try:
            from learning.coin_swap import CoinSwapEngine
            self._coin_swap = CoinSwapEngine(
                config=self.config,
                notifier=getattr(self, "notifier", None),
            )
            log.info("CoinSwapEngine: AKTIF")
        except Exception as e:
            log.info("CoinSwapEngine tidak tersedia: %s", e)

        # Cross-learning: CrossLearnReader summary
        try:
            from learning.cross_learn import get_cross_learn_reader
            reader = get_cross_learn_reader()
            summary = reader.get_summary()
            if summary.get("enabled"):
                log.info(
                    "CrossLearn: AKTIF | peer trades_30d=%s scores_30d=%s",
                    summary.get("trades_30d", 0),
                    summary.get("scores_30d", 0),
                )
            else:
                log.info("CrossLearn: tidak aktif (CROSS_LEARN_ENABLED=false)")
        except Exception as e:
            log.debug("CrossLearn init info error: %s", e)

    async def stop(self) -> None:
        log.info("Shutting down...")
        self.is_running = False

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.ws_feed:
            await self.ws_feed.stop()
        if self.exchange:
            await self.exchange.disconnect()
        if self.db:
            await self.db.save_log(
                "INFO", "main",
                f"AlgoTrader Pro v{APP_VERSION} stopped cleanly.",
            )
            await self.db.close()

        log.info("Shutdown selesai.")

    async def _live_preflight(self) -> None:
        log.warning("=" * 50)
        log.warning("  LIVE MODE — REAL FUNDS AT RISK")
        log.warning("=" * 50)

        try:
            balance  = await self.exchange.fetch_balance()
            quote    = self.config["quote_currency"]
            free, _, _ = self.exchange.parse_balance(balance, quote)
            _min_abs  = float(os.getenv("MIN_BALANCE_USDT", "10.0"))
            required  = max(self.config["initial_capital"] * 0.1, _min_abs)

            if free < required:
                msg = (
                    f"LIVE PREFLIGHT FAIL: Free {quote} balance {free:.2f} "
                    f"< required {required:.2f}."
                )
                log.critical(msg)
                await self.notifier.notify_error("live_preflight", msg)
                raise BotStartupError(msg)

            log.info("Live preflight: Free %s = %.2f [OK]", quote, free)

            for symbol in self.config["universe_watchlist"]:
                info = self.exchange.get_market_info(symbol)
                if not info.get("active", False):
                    msg = f"LIVE PREFLIGHT FAIL: Market {symbol} tidak aktif."
                    log.critical(msg)
                    await self.notifier.notify_error("live_preflight", msg)
                    raise BotStartupError(msg)
                log.info("Live preflight: %s aktif [OK]", symbol)

            await self.notifier.notify_bot_resumed()

        except BotStartupError:
            raise
        except Exception as e:
            log.critical("LIVE PREFLIGHT ERROR: %s", e)
            await self.notifier.notify_error("live_preflight", str(e))
            raise BotStartupError(f"Live preflight error: {e}") from e

    async def _reconcile_positions_on_startup(self) -> None:
        log.info("Reconciliation: cek posisi DB vs exchange...")

        try:
            db_positions = await self.db.get_open_positions()
            if not db_positions:
                log.info("Reconciliation: tidak ada posisi terbuka di DB.")
                return

            for pos in db_positions:
                try:
                    balance    = await self.exchange.fetch_balance()
                    base_coin  = pos.symbol.split("/")[0]
                    free_qty, _, total_qty = self.exchange.parse_balance(balance, base_coin)
                    actual_qty = free_qty or total_qty

                    log.info(
                        "Reconcile %s: DB amount=%.8f | exchange actual=%.8f",
                        pos.symbol, pos.amount, actual_qty,
                    )

                    threshold = pos.amount * 0.01
                    if actual_qty < threshold:
                        log.warning(
                            "Reconcile: %s tidak ditemukan di exchange "
                            "(actual=%.8f < threshold=%.8f) — "
                            "kemungkinan sudah di-close eksternal. "
                            "Menutup di DB.",
                            pos.symbol, actual_qty, threshold,
                        )

                        exit_price = pos.current_price or pos.entry_price
                        try:
                            tk = await self.exchange.fetch_ticker(pos.symbol)
                            exit_price = float(
                                tk.get("last") or pos.current_price or pos.entry_price
                            )
                        except Exception:
                            pass

                        realized_pnl = (
                            (exit_price - pos.entry_price) * pos.amount
                            if pos.entry_price and pos.amount
                            else 0.0
                        )
                        await self.db.close_position(pos.symbol, exit_price, realized_pnl)
                        if self.strategy:
                            self.strategy.unregister_position(pos.symbol)

                        await self.db.save_log(
                            "WARNING", "reconcile",
                            f"Posisi {pos.symbol} di-close via reconciliation startup "
                            f"(tidak ada di exchange). Est PnL={realized_pnl:+.4f}",
                        )
                        await self.notifier.notify_trade_closed(
                            symbol=pos.symbol,
                            side=pos.side,
                            entry_price=float(pos.entry_price),
                            exit_price=float(exit_price),
                            amount=float(pos.amount),
                            realized_pnl=realized_pnl,
                            reason="Reconciliation startup — posisi hilang di exchange",
                        )

                    elif abs(actual_qty - pos.amount) / pos.amount > 0.05:
                        log.warning(
                            "Reconcile: %s amount mismatch "
                            "DB=%.8f vs exchange=%.8f — update DB.",
                            pos.symbol, pos.amount, actual_qty,
                        )
                        from sqlalchemy import update as sa_update
                        from database import Position
                        async with self.db._session() as s:
                            await s.execute(
                                sa_update(Position)
                                .where(
                                    Position.symbol == pos.symbol,
                                    Position.is_open == True,
                                )
                                .values(amount=round(actual_qty, 8))
                            )
                            await s.commit()
                        await self.db.save_log(
                            "INFO", "reconcile",
                            f"Amount {pos.symbol} diupdate: "
                            f"{pos.amount:.8f} → {actual_qty:.8f}",
                        )

                    else:
                        log.info("Reconcile %s: OK (dalam toleransi)", pos.symbol)

                        if self.strategy and hasattr(self.strategy, "_pos_trackers"):
                            with self.strategy._lock:
                                tracker_exists = pos.symbol in self.strategy._pos_trackers

                            if not tracker_exists:
                                log.warning(
                                    "Reconcile: %s posisi OK tapi tracker tidak ada "
                                    "— restore trailing stop dari DB.",
                                    pos.symbol,
                                )
                                try:
                                    atr_val       = float(pos.atr_at_entry or 0)
                                    entry_price   = float(pos.entry_price or 0)
                                    current_price = float(pos.current_price or pos.entry_price or 0)

                                    p = self.strategy._resolve_params(
                                        pos.symbol, entry_price, atr_val, 1.0, 55.0
                                    )

                                    from strategy import ExitMode
                                    prof    = get_coin_profile(pos.symbol)
                                    atr_pct = (atr_val / entry_price * 100) if entry_price > 0 else 0.0
                                    exit_mode = (
                                        ExitMode.RIDE_THE_WAVE
                                        if atr_pct >= prof.atr_pct_threshold
                                        else ExitMode.QUICK_PROFIT
                                    )

                                    tracker = PositionTracker(
                                        symbol=pos.symbol,
                                        entry_price=entry_price,
                                        entry_time=pos.entry_time or _utcnow_dt(),
                                        exit_mode=exit_mode,
                                        highest_price=max(entry_price, current_price),
                                        trailing_active=False,
                                        quick_tp_pct=p.get("quick_tp_pct", 1.75),
                                        quick_sl_pct=p.get("quick_sl_pct", 1.20),
                                        atr_sl_mult=p.get("atr_sl_mult", 2.0),
                                        trailing_gap_pct=p.get("trailing_gap_pct", 0.50),
                                        activation_pct=p.get("trailing_activation_pct", 1.50),
                                        max_hold_seconds=p.get("max_hold_seconds", 0),
                                        profile_name=prof.profile.value,
                                    )

                                    with self.strategy._lock:
                                        self.strategy._pos_trackers[pos.symbol] = tracker
                                        self.strategy._in_position[pos.symbol]  = True

                                    log.info(
                                        "Reconcile: trailing stop di-restore untuk %s "
                                        "(mode=%s max_hold_secs=%d)",
                                        pos.symbol, exit_mode.value, tracker.max_hold_seconds,
                                    )
                                    await self.db.save_log(
                                        "INFO", "reconcile",
                                        f"Trailing stop restored untuk {pos.symbol} "
                                        f"via reconciliation (mode={exit_mode.value})",
                                    )
                                except Exception as re_err:
                                    log.error(
                                        "Reconcile: gagal restore tracker untuk %s: %s",
                                        pos.symbol, re_err,
                                    )
                                    await self.db.save_log(
                                        "WARNING", "reconcile",
                                        f"Trailing stop GAGAL di-restore untuk {pos.symbol}: {re_err}",
                                    )

                except Exception as e:
                    log.error("Reconcile error untuk %s: %s", pos.symbol, e)

        except Exception as e:
            log.error("Reconciliation startup gagal: %s", e, exc_info=True)

    async def run_scanner_loop(self) -> None:
        """
        Gate 1 & Gate 2 — Combined Stream Scanner.
        Berjalan terus menerus tiap 2 detik.
        Memantau semua koin di universe_watchlist.
        Gate 1 : activity screen (volume/price).
        Gate 2 : whale/orderbook check.
        Juga memantau koin yang sudah di pipeline
        dan mengirim invalidation signal kalau bermasalah.
        """
        import time as _time

        GATE1_VOLUME_RATIO_MIN  = 1.5
        GATE1_PRICE_CHANGE_MIN  = 0.3
        GATE1_MIN_VOLUME_USDT   = float(os.getenv("GATE1_MIN_VOLUME_USDT", "500000"))
        GATE1_LOOP_INTERVAL     = 2.0
        PRICE_BUFFER_SIZE       = 5
        VOLUME_MA_ALPHA         = 0.1

        log.info("Scanner loop dimulai (Gate 1 & 2) — universe=%d koin",
                 len(self.config["universe_watchlist"]))

        while self.is_running:
            try:
                universe = self.config["universe_watchlist"]
                now      = _time.time()

                # ── Hot-reload universe dari DB overrides ──
                try:
                    db_overrides = await self.db.get_active_universe_overrides()
                    if db_overrides:
                        merged = list(universe)
                        added  = []
                        for sym in db_overrides:
                            if sym not in merged:
                                merged.append(sym)
                                added.append(sym)
                        if added:
                            log.info(
                                "Scanner: +%d koin dari DB overrides: %s",
                                len(added), added,
                            )
                            # Subscribe ws_feed ke koin baru dari DB overrides
                            if self.ws_feed:
                                await self.ws_feed.add_symbols(added)
                            # Update strategy untuk koin baru dari DB overrides
                            if self.strategy and hasattr(self.strategy, "update_symbols"):
                                self.strategy.update_symbols(merged)
                        universe = merged
                except Exception as _ov_err:
                    log.debug("Scanner: gagal baca DB overrides: %s", _ov_err)

                # ── Ambil open positions untuk has_position check ──
                try:
                    _open_pos = await self.db.get_open_positions()
                    _open_pos_symbols = {p.symbol for p in _open_pos}
                except Exception:
                    _open_pos_symbols = set()

                for symbol in universe:
                    try:
                        # ── Ambil data dari WebSocket (memory, no I/O) ──
                        ticker = self.ws_feed.live_tickers.get(symbol, {})
                        ob     = self.ws_feed.live_orderbooks.get(symbol, {})

                        if not ticker or not ticker.get("last"):
                            continue

                        last         = float(ticker.get("last") or 0)
                        quote_volume = float(ticker.get("quote_volume") or 0)

                        if last <= 0:
                            continue

                        # ── Update volume MA (rolling exponential) ──
                        if symbol not in self._volume_ma:
                            self._volume_ma[symbol] = quote_volume
                        else:
                            self._volume_ma[symbol] = (
                                VOLUME_MA_ALPHA * quote_volume
                                + (1 - VOLUME_MA_ALPHA) * self._volume_ma[symbol]
                            )
                        vol_ma    = self._volume_ma[symbol]
                        vol_ratio = quote_volume / vol_ma if vol_ma > 0 else 0.0

                        # ── Update price buffer (5 tick terakhir) ──
                        if symbol not in self._price_buffer:
                            self._price_buffer[symbol] = []
                        buf = self._price_buffer[symbol]
                        buf.append(last)
                        if len(buf) > PRICE_BUFFER_SIZE:
                            buf.pop(0)
                        price_change = (
                            abs(last - buf[0]) / buf[0] * 100
                            if len(buf) >= 2 and buf[0] > 0
                            else 0.0
                        )

                        # ════════════════════════════════════════════
                        # GATE 1 — Activity Screen
                        # ════════════════════════════════════════════
                        gate1_ok = (
                            quote_volume >= GATE1_MIN_VOLUME_USDT
                            and (
                                vol_ratio  >= GATE1_VOLUME_RATIO_MIN
                                or price_change >= GATE1_PRICE_CHANGE_MIN
                            )
                        )

                        # ── Pantau koin yang sudah di pipeline ──
                        in_pipeline = symbol in self._pipeline_active
                        if symbol in _open_pos_symbols:
                            continue  # Sudah punya posisi — skip enqueue
                        if in_pipeline and not gate1_ok:
                            # Kondisi memburuk — Gate 2 akan handle via orderbook
                            pass

                        if not gate1_ok and not in_pipeline:
                            continue  # Koin tidak aktif, skip

                        # ════════════════════════════════════════════
                        # GATE 2 — Whale & Orderbook Check
                        # ════════════════════════════════════════════
                        bids = ob.get("bids", [])
                        asks = ob.get("asks", [])

                        if not bids or not asks:
                            # Orderbook kosong — tidak bisa validasi market, skip
                            log.debug('[Gate2] %s orderbook kosong — skip', symbol)
                            continue

                        # Jalankan WhaleDetector
                        if symbol not in self._whale_detectors:
                            self._whale_detectors[symbol] = WhaleDetector()
                        wd  = self._whale_detectors[symbol]
                        res = wd.analyze(
                            symbol, bids, asks, self._ob_wall_first_seen
                        )

                        ratio      = res["ratio"]
                        confidence = res["confidence"]
                        thr_sell   = res["thr_sell"]

                        # Tentukan level bahaya orderbook
                        danger_level = self._get_ob_danger_level(
                            symbol, bids, asks, ratio, confidence
                        )

                        whale_sell_genuine = (
                            ratio      < thr_sell
                            and confidence >= 0.5
                            and danger_level <= 4
                        )

                        if whale_sell_genuine:
                            # ── Notifikasi whale ke Telegram ──
                            if self.notifier:
                                asyncio.create_task(
                                    self.notifier.notify_whale(
                                        symbol     = symbol,
                                        direction  = "SELL",
                                        ratio      = ratio,
                                        confidence = confidence,
                                        mode       = "LIVE" if not self.config.get("testnet") else "TESTNET",
                                    )
                                )
                            # ── Kirim invalidation signal ──
                            action = "skip_all" if danger_level <= 2 else "skip_gate3_only"
                            self._invalidation_signals[symbol] = {
                                "reason":      "whale_sell_genuine",
                                "level":       danger_level,
                                "confidence":  confidence,
                                "ratio":       ratio,
                                "action":      action,
                                "source":      "gate2",
                                "timestamp":   now,
                            }
                            if in_pipeline:
                                log.info(
                                    "[Gate2] INVALIDASI %s | level=%d conf=%.2f "
                                    "ratio=%.3f action=%s",
                                    symbol, danger_level, confidence,
                                    ratio, action,
                                )
                            continue

                        # ── Bersihkan invalidation lama (> 60 detik) ──
                        if symbol in self._invalidation_signals:
                            age = now - self._invalidation_signals[symbol].get(
                                "timestamp", 0
                            )
                            if age > 60:
                                del self._invalidation_signals[symbol]
                                log.debug(
                                    "[Gate2] Invalidation %s expired — cleared", symbol
                                )

                        # ── Gate 2 lolos — enqueue ke Gate 3 ──
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

    def _get_ob_danger_level(
        self,
        symbol:     str,
        bids:       list,
        asks:       list,
        ratio:      float,
        confidence: float,
    ) -> int:
        """
        Tentukan level bahaya orderbook (1-10).
        Level 1-2 : bahaya nyata, invalidasi semua gate.
        Level 3-4 : waspada tinggi.
        Level 5-7 : monitor.
        Level 8+  : aman.
        """
        if not bids or not asks:
            return 10

        try:
            best_bid = float(bids[0][0]) if bids else 0
            best_ask = float(asks[0][0]) if asks else 0
            mid      = (best_bid + best_ask) / 2 if best_bid and best_ask else 0

            if mid <= 0:
                return 10

            # Cari wall terbesar di ask side
            max_ask_qty   = max((float(a[1]) for a in asks[:20]), default=0)
            max_ask_price = next(
                (float(a[0]) for a in asks[:20]
                 if float(a[1]) == max_ask_qty),
                best_ask,
            )

            wall_dist_pct = abs(max_ask_price - mid) / mid * 100

            if wall_dist_pct < 0.1:
                return 1
            elif wall_dist_pct < 0.3:
                return 2
            elif wall_dist_pct < 0.5:
                return 3
            elif wall_dist_pct < 1.0:
                return 4
            elif wall_dist_pct < 1.5:
                return 5
            elif wall_dist_pct < 2.0:
                return 6
            elif wall_dist_pct < 3.0:
                return 7
            else:
                return 8

        except Exception:
            return 10

    async def _maybe_enqueue_gate3(self, symbol: str) -> None:
        """
        Masukkan koin ke antrian Gate 3 kalau belum ada
        di pipeline, antrian, atau posisi terbuka.
        """
        if symbol in self._pipeline_active:
            return
        if symbol in self._invalidation_signals:
            return

        # ── Early-exit: skip kalau candle belum baru ──
        # Hindari push ke queue kalau timeframe belum habis sejak candle terakhir
        import time as _t
        _tf = self.config.get("timeframe", "15m")
        _cache_key = (symbol, _tf)
        _last_ts = self._last_candle_ts.get(_cache_key)
        if _last_ts is not None:
            _tf_seconds = {
                "1m":60,"3m":180,"5m":300,"15m":900,
                "30m":1800,"1h":3600,"4h":14400,"1d":86400
            }
            _tf_ms = _tf_seconds.get(_tf, 900) * 1000
            _now_ms = int(_t.time() * 1000)
            if _now_ms < _last_ts + _tf_ms:
                return  # Candle belum baru — skip tanpa sentuh queue/DB

        open_pos_symbols = set()
        try:
            positions = await self.db.get_open_positions()
            open_pos_symbols = {p.symbol for p in positions}
        except Exception:
            pass

        if symbol in open_pos_symbols:
            return

        # Cek apakah sudah di queue — pakai set terpisah, bukan akses internal asyncio
        if symbol in self._queued_symbols:
            return

        self._pipeline_active.add(symbol)
        self._queued_symbols.add(symbol)
        await self._gate3_queue.put(symbol)
        log.debug("[Gate2→Gate3] %s masuk antrian (queue size=%d)",
                  symbol, self._gate3_queue.qsize())

    async def run_gate3_worker(self) -> None:
        """
        Gate 3, 4, 5 — Worker yang memproses antrian dari Gate 2.
        Mengambil koin dari gate3_queue satu per satu,
        menjalankan OHLCV + indikator + intelligence + risk.
        Semua alat yang dipakai sudah ada di bot.
        """
        # Dynamic workers berdasarkan queue size
        # Base: 3 workers, tambah 1 per 10 koin di antrian, max 8
        _base_workers = int(os.getenv('GATE3_WORKERS', '3'))
        GATE3_WORKERS = _base_workers
        log.info("Gate3 worker dimulai (%d workers)", GATE3_WORKERS)

        async def _process_one(symbol: str) -> None:
            try:
                # ════════════════════════════════════════════
                # GUARD — skip kalau simbol sudah punya posisi terbuka
                # ════════════════════════════════════════════
                try:
                    _existing_pos = await self.db.get_open_position_by_symbol(symbol)
                    if _existing_pos is not None:
                        log.debug("[Gate3] %s sudah punya posisi terbuka — skip", symbol)
                        return
                except Exception:
                    pass

                # ════════════════════════════════════════════
                # CEK CLOSING — skip kalau sedang close
                # ════════════════════════════════════════════
                async with self._closing_lock:
                    if symbol in self._closing_symbols:
                        log.debug("[Gate3] %s sedang closing — skip", symbol)
                        return

                # ════════════════════════════════════════════
                # CEK INVALIDASI — sebelum apapun
                # ════════════════════════════════════════════
                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all", "skip_gate3_only"):
                    log.debug("[Gate3] %s diinvalidasi sebelum proses — skip", symbol)
                    return

                threshold_mult = 1.2 if (inv and inv.get("action") == "monitor") else 1.0

                # ════════════════════════════════════════════
                # GATE 3 — Fetch OHLCV + Indikator Dasar
                # ════════════════════════════════════════════
                from profiles.registry import get_coin_profile, auto_classify_profile, _COIN_PROFILE_MAP, _PROFILE_CACHE
                # Auto-classify hanya kalau belum ada di map (hindari classify ulang)
                _base = symbol.split("/")[0]
                if _base not in _COIN_PROFILE_MAP:
                    try:
                        _ticker     = self.ws_feed.live_tickers.get(symbol, {})
                        _spread_pct = self.ws_feed.get_current_spread_pct(symbol) or 0.0
                        auto_classify_profile(_base, _ticker, _spread_pct)
                        # Invalidate cache agar profil baru dipakai
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
                    bars = await self.exchange.fetch_ohlcv(
                        symbol, tf,
                        limit=self.config["lookback_candles"]
                    )
                except Exception as e:
                    log.debug("[Gate3] fetch OHLCV gagal %s: %s", symbol, e)
                    return

                if not bars or len(bars) < 60:
                    log.debug("[Gate3] %s data tidak cukup (%d bar)", symbol, len(bars) if bars else 0)
                    return

                # Candle cache — skip kalau candle belum berubah
                confirmed_ts = bars[-2][0]
                cache_key    = (symbol, tf)
                if self._last_candle_ts.get(cache_key) == confirmed_ts:
                    log.debug("[Gate3] %s candle belum baru — skip", symbol)
                    return
                self._last_candle_ts[cache_key] = confirmed_ts

                # Pruning cache kalau terlalu besar
                if len(self._last_candle_ts) > 500:
                    oldest = sorted(self._last_candle_ts, key=lambda k: self._last_candle_ts[k])
                    for k in oldest[:250]:
                        del self._last_candle_ts[k]

                # Cek invalidasi lagi setelah fetch
                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all", "skip_gate3_only"):
                    log.debug("[Gate3] %s diinvalidasi saat fetch — skip", symbol)
                    return

                import pandas as pd
                cols = ["timestamp", "open", "high", "low", "close", "volume"]
                df   = pd.DataFrame(bars, columns=cols)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)

                # Hitung indikator dasar via ta_compat
                try:
                    import ta_compat  # noqa
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

                bar   = df.iloc[-2]
                close = float(bar["close"])
                ema9  = float(bar.get("EMA_9",  0))
                ema21 = float(bar.get("EMA_21", 0))
                ema50 = float(bar.get("EMA_50", 0))
                rsi   = float(bar.get("RSI_14", 50))
                atr   = float(bar.get("ATRr_14", 0))

                if close <= 0 or atr <= 0:
                    return

                # EMA stack check
                if ema9 <= 0 or ema21 <= 0 or ema50 <= 0:
                    return
                if not (ema9 > ema21):
                    log.debug("[Gate3] %s EMA bearish — skip", symbol)
                    return

                # RSI range check
                try:
                    rsi_min = profile.rsi_min
                    rsi_max = profile.rsi_max
                except Exception:
                    rsi_min = self.config.get("rsi_min", 45)
                    rsi_max = self.config.get("rsi_max", 77)

                if not (rsi_min <= rsi <= rsi_max):
                    log.debug("[Gate3] %s RSI=%.1f di luar range [%d,%d] — skip",
                              symbol, rsi, rsi_min, rsi_max)
                    return

                # VWAP check (hanya intraday TF)
                if tf not in ("1d", "3d", "1w"):
                    for vwap_col in ("VWAP_D", "VWAP", "vwap"):
                        if vwap_col in bar.index:
                            vwap_val = bar.get(vwap_col)
                            if vwap_val and float(vwap_val) > 0:
                                if close < float(vwap_val):
                                    log.debug("[Gate3] %s below VWAP — skip", symbol)
                                    return
                                break

                log.info("[Gate3→Gate4] %s lolos | EMA=%.4f/%.4f/%.4f RSI=%.1f ATR=%.6f",
                         symbol, ema9, ema21, ema50, rsi, atr)

                # ════════════════════════════════════════════
                # GATE 4 — Intelligence Pipeline
                # ════════════════════════════════════════════

                # Cek invalidasi sebelum intelligence
                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") in ("skip_all",):
                    log.debug("[Gate4] %s diinvalidasi — skip", symbol)
                    return

                if not self.strategy or not hasattr(self.strategy, "get_scored_signal"):
                    return

                # Fetch confirmation TF kalau enabled
                confirmation_df = None
                confirmation_tf = None
                if self.config.get("confirmation_tf_enabled", True):
                    try:
                        confirmation_tf = getattr(profile, "effective_confirmation_tf", None)
                        if confirmation_tf and confirmation_tf != tf:
                            conf_bars = await self.exchange.fetch_ohlcv(
                                symbol, confirmation_tf,
                                limit=self.config["lookback_candles"]
                            )
                            if conf_bars and len(conf_bars) >= 20:
                                cdf = pd.DataFrame(conf_bars, columns=cols)
                                cdf["timestamp"] = pd.to_datetime(
                                    cdf["timestamp"], unit="ms", utc=True
                                )
                                cdf.set_index("timestamp", inplace=True)
                                confirmation_df = cdf
                    except Exception as e:
                        log.debug("[Gate4] confirmation TF gagal %s: %s", symbol, e)

                # Tambah quote_volume ke df
                ob = self.ws_feed.live_orderbooks.get(symbol, {})
                ticker = self.ws_feed.live_tickers.get(symbol, {})
                qv = ticker.get("quote_volume")
                if qv and float(qv) > 0 and close > 0:
                    df["quote_volume"] = df["volume"] * df["close"]
                    df.loc[df.index[-1], "quote_volume"] = float(qv)

                # Jalankan scored signal via intelligence pipeline
                try:
                    scored = await self.strategy.get_scored_signal(
                        symbol          = symbol,
                        df              = df,
                        confirmation_df = confirmation_df,
                        confirmation_timeframe = confirmation_tf,
                        ob_data         = ob,
                    )
                except Exception as e:
                    log.debug("[Gate4] scored signal error %s: %s", symbol, e)
                    return

                if scored is None:
                    log.debug("[Gate4] %s skor tidak cukup — skip", symbol)
                    return

                # Cek threshold dengan multiplier (kalau ada monitor flag)
                total_score = float(getattr(scored, "total_score", 0) or 0)
                try:
                    from profiles.thresholds import get_dynamic_threshold
                    _regime_val = scored.regime.value if scored.regime else "undefined"
                    base_threshold = get_dynamic_threshold(profile.profile.value, _regime_val)
                except Exception:
                    base_threshold = float(getattr(scored, "threshold_used", 65) or 65)
                effective_threshold = base_threshold * threshold_mult

                if total_score < effective_threshold:
                    log.debug("[Gate4] %s skor %.1f < threshold %.1f — skip",
                              symbol, total_score, effective_threshold)
                    return

                log.info("[Gate4→Gate5] %s lolos | score=%.1f threshold=%.1f",
                         symbol, total_score, effective_threshold)

                # ════════════════════════════════════════════
                # GATE 4.5 — IntelligenceCommander Full Decision
                # (Kelly Sizing, Correlation Penalty, Spread Check, Regime)
                # ════════════════════════════════════════════
                # [BUG-FIX v2] _kelly_size_pct sebelumnya dihitung penuh
                # (Kelly criterion + quality_mult + consec_mult + correlation
                # penalty) tapi position_size_pct hasilnya HANYA dipakai utk
                # log — tidak pernah disambungkan ke SignalEvent/eksekusi nyata.
                # _handle_buy() selalu pakai max_position_size_pct flat dari
                # config, terlepas dari hasil Kelly. Sekarang ditangkap di sini
                # dan diteruskan via metadata, dipakai _handle_buy() sebagai
                # CEILING TAMBAHAN (cuma bisa mengurangi size, tidak pernah
                # menambah di atas ATR-sizing/max_pct yang sudah ada).
                _kelly_size_pct: Optional[float] = None
                if self._commander is not None:
                    try:
                        from intelligence.commander import decide as _cmd_decide
                        _open_syms = []
                        try:
                            _open_pos = await self.db.get_open_positions()
                            _open_syms = [p.symbol for p in _open_pos]
                        except Exception:
                            pass

                        _cmd_decision = await _cmd_decide(
                            signal           = scored,
                            open_positions   = _open_syms,
                            portfolio_value  = self.portfolio_state.get("total_equity", 0.0),
                            base_risk_pct    = self.config.get("risk_per_trade_pct", 1.0),
                            exchange_connector = self.ws_feed,
                            risk_manager     = self.risk_manager,
                            db_manager       = self.db,
                        )
                        if _cmd_decision.is_executable:
                            _kelly_size_pct = _cmd_decision.position_size_pct
                        if not _cmd_decision.is_executable:
                            log.info(
                                "[Gate4.5] Commander reject %s: %s | gates_failed=%s",
                                symbol,
                                _cmd_decision.rejection_reason,
                                _cmd_decision.gates_failed,
                            )
                            # ── Jalur Entry 2: cek transisi regime ──
                            try:
                                from intelligence.classifier import is_regime_transition
                                _is_trans, _from_r, _to_r = is_regime_transition(symbol)
                                _coin_prof = self.strategy._profiles.get(symbol)
                                _allow_trans = (
                                    _coin_prof is not None
                                    and getattr(_coin_prof, "allowed_entry_on_transition", False)
                                )
                                if _is_trans and _allow_trans:
                                    _trans_mult = getattr(_coin_prof, "transition_size_mult", 0.5)
                                    log.info(
                                        "[Gate4.5] Jalur Entry 2 AKTIF %s | "
                                        "transisi %s->%s | size_mult=%.1f",
                                        symbol, _from_r, _to_r, _trans_mult,
                                    )
                                    scored._transition_entry  = True
                                    scored._transition_size_mult = _trans_mult
                                else:
                                    return
                            except Exception as _te_err:
                                log.debug("Jalur Entry 2 error %s: %s", symbol, _te_err)
                                return
                            # ── End Jalur Entry 2 ──
                        log.info(
                            "[Gate4.5] Commander APPROVE %s | kelly=%.2f%% gates_passed=%s",
                            symbol,
                            _cmd_decision.position_size_pct or 0.0,
                            _cmd_decision.gates_passed,
                        )
                    except Exception as _cmd_err:
                        log.warning(
                            "[Gate4.5] Commander error %s: %s — lanjut tanpa full gate",
                            symbol, _cmd_err,
                        )

                # ════════════════════════════════════════════
                # GATE 5 — Risk Manager & Eksekusi
                # ════════════════════════════════════════════

                # Cek invalidasi terakhir sebelum order
                inv = self._invalidation_signals.get(symbol)
                if inv and inv.get("action") == "skip_all":
                    log.info("[Gate5] %s diinvalidasi detik terakhir — batalkan", symbol)
                    return

                # Buat SignalEvent dari scored signal
                from strategy import SignalEvent, SignalType
                import time as _t

                # Pakai live ticker price untuk kurangi slippage
                # Fallback ke candle close kalau ticker tidak tersedia
                _live_ticker = self.ws_feed.live_tickers.get(symbol, {})
                _live_price  = float(_live_ticker.get("last") or 0)
                _exec_price  = _live_price if _live_price > 0 else close
                log.debug(
                    "[Gate5] %s exec_price=%.6f (live=%.6f close=%.6f drift=%.2f%%)",
                    symbol, _exec_price, _live_price, close,
                    abs(_exec_price - close) / close * 100 if close > 0 else 0
                )

                signal = SignalEvent(
                    symbol      = symbol,
                    signal_type = SignalType.BUY,
                    price       = _exec_price,
                    timestamp   = _utcnow_dt(),
                    strategy    = "scanner_pipeline",
                    confidence  = float(getattr(scored, "confidence", 0.5) or 0.5),
                    stop_loss   = getattr(scored, "stop_loss", None),
                    take_profit = getattr(scored, "take_profit", None),
                    metadata    = {
                        "atr":           atr,
                        "coin_profile":  getattr(profile, "profile", "universal"),
                        "pipeline_mode": "combined_stream",
                        "total_score":   total_score,
                        "kelly_size_pct": _kelly_size_pct,
                    },
                    total_score      = total_score,
                    regime           = getattr(scored, "regime", "undefined"),
                    score_breakdown  = getattr(scored, "score_breakdown", {}),
                    scoring_narrative = getattr(scored, "narrative", ""),
                )

                # Kirim ke _handle_buy yang sudah ada
                await self._handle_buy(signal)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("[Gate3Worker] error %s: %s", symbol, e, exc_info=True)
            finally:
                # Selalu cleanup pipeline_active DAN queued_symbols
                self._pipeline_active.discard(symbol)
                self._queued_symbols.discard(symbol)


        # ── Jalankan workers paralel ──
        async def _worker(worker_id: int) -> None:
            log.debug("Gate3 worker-%d siap", worker_id)
            while self.is_running:
                try:
                    symbol = await asyncio.wait_for(
                        self._gate3_queue.get(), timeout=5.0
                    )
                    log.debug("[Worker-%d] memproses %s", worker_id, symbol)
                    await _process_one(symbol)
                    self._gate3_queue.task_done()
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error("Gate3 worker-%d error: %s", worker_id, e)
                    if self.db:
                        await self.db.save_log("ERROR", "gate3_worker", str(e)[:500])
                    if "exchange" in str(e).lower() or "connection" in str(e).lower():
                        await self.notifier.notify_error("gate3_worker", str(e)[:300])

        workers = [
            asyncio.create_task(_worker(i), name=f"gate3_worker_{i}")
            for i in range(GATE3_WORKERS)
        ]

        async def _dynamic_scaler() -> None:
            """Spawn worker tambahan kalau antrian menumpuk dan masih aman."""
            current_workers = GATE3_WORKERS
            max_workers = int(os.getenv('GATE3_MAX_WORKERS', '8'))
            while self.is_running:
                await asyncio.sleep(10)
                qsize = self._gate3_queue.qsize()
                # Tambah worker kalau antrian > 10 dan belum capai max
                if qsize > 10 and current_workers < max_workers:
                    new_id = current_workers
                    t = asyncio.create_task(_worker(new_id), name=f"gate3_worker_{new_id}")
                    workers.append(t)
                    current_workers += 1
                    log.info("[Gate3] Dynamic scale UP: %d workers (queue=%d)", current_workers, qsize)
                # Scale down kalau antrian kosong dan ada worker dinamis
                elif qsize == 0 and current_workers > GATE3_WORKERS:
                    t = workers.pop()
                    t.cancel()
                    current_workers -= 1
                    log.info("[Gate3] Dynamic scale DOWN: %d workers (queue=%d)", current_workers, qsize)

        scaler_task = asyncio.create_task(_dynamic_scaler(), name="gate3_scaler")
        await asyncio.gather(*workers, scaler_task, return_exceptions=True)

    async def run_strategy_loop(self) -> None:
        """
        Strategy loop — menangani CLOSE_LONG untuk posisi terbuka.
        BUY signal sepenuhnya dihandle oleh corong (scanner + gate3_worker).
        Loop ini fokus pada exit management posisi yang sudah ada.
        """
        last_candle_ts: Dict[Tuple[str, str], int] = {}

        while self.is_running:
            try:
                # Pruning cache
                if len(last_candle_ts) > MAX_CANDLE_CACHE:
                    sorted_keys = sorted(last_candle_ts, key=lambda k: last_candle_ts[k])
                    for old_key in sorted_keys[:len(sorted_keys) // 2]:
                        del last_candle_ts[old_key]

                # Ambil posisi terbuka — hanya koin yang ada posisi
                open_positions = await self.db.get_open_positions()
                if not open_positions:
                    await asyncio.sleep(self.CANDLE_POLL_INTERVAL)
                    continue

                for pos in open_positions:
                    symbol = pos.symbol
                    if not self.strategy.is_active:
                        continue

                    async with self._closing_lock:
                        is_closing = symbol in self._closing_symbols
                    if is_closing:
                        log.debug("Strategy skip %s — sedang dalam proses close.", symbol)
                        continue

                    tf   = self.strategy.get_symbol_timeframe(symbol)
                    bars = await self.exchange.fetch_ohlcv(
                        symbol, tf, limit=self.config["lookback_candles"],
                    )

                    if not bars or len(bars) < 20:
                        continue

                    confirmed_ts = bars[-2][0]
                    cache_key    = (symbol, tf)
                    if last_candle_ts.get(cache_key) == confirmed_ts:
                        continue
                    last_candle_ts[cache_key] = confirmed_ts

                    cols = ["timestamp", "open", "high", "low", "close", "volume"]
                    df   = pd.DataFrame(bars, columns=cols)
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                    df.set_index("timestamp", inplace=True)

                    # quote_volume
                    if len(bars[0]) > 6:
                        df["quote_volume"] = [
                            float(b[6]) if len(b) > 6 and b[6] is not None
                            else float(b[4]) * float(b[5])
                            for b in bars
                        ]
                    elif self.ws_feed:
                        ticker = self.ws_feed.live_tickers.get(symbol, {})
                        qv     = ticker.get("quote_volume")
                        if qv and float(qv) > 0:
                            last_price = df["close"].iloc[-1]
                            if last_price > 0:
                                df["quote_volume"] = df["volume"] * df["close"]
                                df.loc[df.index[-1], "quote_volume"] = float(qv)

                    # Generate signals — hanya proses CLOSE_LONG
                    try:
                        if self._commander is not None:
                            signals = await self._commander.process(
                                symbol, df, self.strategy,
                                confirmation_df=None,
                                confirmation_timeframe=None,
                            )
                        else:
                            signals = await self.strategy.generate_signals(symbol, df)
                    except Exception as cmd_err:
                        log.warning("Strategy loop error [%s]: %s", symbol, cmd_err)
                        signals = []

                    for sig in signals:
                        # BUY dari sini diabaikan — sudah dihandle corong
                        if sig.signal_type == SignalType.BUY:
                            continue
                        # Guard double-close: skip kalau sudah dalam proses closing
                        async with self._closing_lock:
                            if sig.symbol in self._closing_symbols:
                                log.debug(
                                    "Strategy loop: skip CLOSE_LONG %s — "
                                    "sudah dalam proses closing",
                                    sig.symbol,
                                )
                                continue
                        await self._handle_signal(sig)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Strategy loop error: %s", e, exc_info=True)
                if self.db:
                    await self.db.save_log("ERROR", "strategy_loop", str(e)[:500])
                if "exchange" in str(e).lower() or "connection" in str(e).lower():
                    await self.notifier.notify_error("strategy_loop", str(e)[:300])

            await asyncio.sleep(self.CANDLE_POLL_INTERVAL)

    async def run_portfolio_monitor(self) -> None:
        while self.is_running:
            try:
                await self._refresh_portfolio()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Portfolio monitor error: %s", e, exc_info=True)
            await asyncio.sleep(self.SNAPSHOT_INTERVAL)

    async def run_daily_summary(self) -> None:
        while self.is_running:
            try:
                now = datetime.now(timezone.utc)
                if (
                    now.hour == self.DAILY_SUMMARY_HOUR
                    and now.minute >= self.DAILY_SUMMARY_MIN
                    and not self._daily_summary_sent
                ):
                    self._daily_summary_sent = True
                    ps           = self.portfolio_state
                    trades_today = await self.db.get_today_trade_count()
                    today_start  = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    closed       = await self.db.get_recent_trades(limit=200, since=today_start)
                    closed_pnl   = [
                        float(t.realized_pnl)
                        for t in closed
                        if t.realized_pnl is not None
                    ]
                    win_rate = (
                        self.risk_manager.compute_win_rate(closed_pnl)
                        if closed_pnl else 0.0
                    )

                    await self.notifier.notify_daily_summary(
                        total_equity=ps.get("total_equity",   0),
                        daily_pnl=ps.get("daily_pnl",         0),
                        daily_pnl_pct=ps.get("daily_pnl_pct", 0),
                        total_trades=trades_today,
                        win_rate=win_rate,
                        drawdown_pct=self.risk_manager.current_drawdown_pct,
                    )
                    log.info("Daily summary notifikasi terkirim.")

                if now.hour == 0 and now.minute == 0:
                    self._daily_summary_sent = False
                    # Auto cleanup DB setiap tengah malam
                    try:
                        deleted = await self.db.cleanup_old_data()
                        log.info("Auto DB cleanup: %s", deleted)
                    except Exception as _ce:
                        log.warning("Auto DB cleanup gagal: %s", _ce)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Daily summary error: %s", e)

            await asyncio.sleep(60)

    async def run_coin_swap_loop(self) -> None:
        """Loop periodik untuk cross-learning coin swap."""
        if not self._coin_swap:
            log.debug("CoinSwap tidak aktif — run_coin_swap_loop di-skip.")
            return
        # Gunakan self.config agar hot-reload via /setconfig bekerja
        if not self.config.get("coin_swap_enabled", False):
            log.debug("COIN_SWAP_ENABLED=false — skip.")
            return

        log.info("CoinSwap loop dimulai.")
        # Tunggu dulu 10 menit setelah bot start sebelum cek pertama
        await asyncio.sleep(600)

        while True:
            try:
                if self._coin_swap.should_run():
                    log.info("CoinSwap: menjalankan siklus evaluasi...")
                    swaps = await self._coin_swap.run_cycle(bot_instance=self)
                    if swaps:
                        log.info("CoinSwap: %d swap dilakukan.", len(swaps))
                    else:
                        log.info("CoinSwap: tidak ada swap diperlukan.")
                else:
                    log.debug("CoinSwap: belum waktunya, skip.")
            except Exception as e:
                log.error("CoinSwap loop error: %s", e)

            # [MSL-A FIX] Bersihkan state orderbook yang sudah tidak aktif > 1 jam
            try:
                from indicators.orderbook import cleanup_stale_states
                n = cleanup_stale_states()
                if n:
                    log.info("Orderbook: %d stale state dibersihkan.", n)
            except Exception as e:
                log.debug("Orderbook cleanup error (non-fatal): %s", e)

            # Cek setiap 1 jam
            await asyncio.sleep(3600)

    async def run_analytics_loop(self) -> None:
        if not self._analytics:
            log.debug("Analytics tidak aktif — run_analytics_loop di-skip.")
            return

        log.info("Analytics loop dimulai.")
        await asyncio.sleep(min(self.config.get("analytics_refresh_interval", 3600), 600))

        while self.is_running:
            interval = self.config.get("analytics_refresh_interval", 3600)
            try:
                log.info("Analytics: memperbarui performance snapshots...")
                await self._analytics.refresh_snapshots()

                if self._meta_learner:
                    log.info("Meta-learner: mengevaluasi suggestions...")
                    suggestions = await self._meta_learner.run_full_cycle()
                    if suggestions:
                        log.info(
                            "Meta-learner menghasilkan %d suggestion(s) baru.",
                            len(suggestions),
                        )
                        for sug in suggestions:
                            await self.db.save_log(
                                "INFO", "meta_learner",
                                f"Suggestion: {sug.symbol} | {sug.parameter_name} "
                                f"{sug.current_value} → {sug.suggested_value} | {sug.reasoning[:100]}",
                            )


                # Cross-learning analysis (jika aktif)
                try:
                    import os
                    if (self._analytics
                            and self.config.get("cross_learn_enabled", False)):
                        cross_results = await self._analytics.run_cross_analysis(
                            lookback_days=30,
                        )
                        if cross_results:
                            log.info(
                                "CrossLearn analysis: %d reports dihasilkan.",
                                len(cross_results),
                            )
                        # Jalankan juga cross cycle di meta_learner
                        if self._meta_learner:
                            await self._meta_learner.run_cross_cycle(lookback_days=30)
                except Exception as e:
                    log.error("CrossLearn analysis error (non-fatal): %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Analytics loop error: %s", e, exc_info=True)
                if self.db:
                    await self.db.save_log("ERROR", "analytics_loop", str(e)[:500])
            await asyncio.sleep(interval)

    async def run_sl_tp_monitor(self) -> None:
        while self.is_running:
            try:
                positions = await self.db.get_open_positions()

                for pos in positions:
                    async with self._closing_lock:
                        is_closing = pos.symbol in self._closing_symbols
                    if is_closing:
                        log.debug(
                            "SL/TP monitor skip %s — sedang dalam proses close.",
                            pos.symbol,
                        )
                        continue

                    price = await self._get_current_price(pos.symbol)
                    if price is None or price <= 0:
                        log.warning(
                            "Tidak bisa ambil harga untuk %s — skip cycle ini.",
                            pos.symbol,
                        )
                        continue

                    # Track highest_price untuk trailing ATG yang akurat
                    if pos.side == "long" and price > (pos.highest_price or 0):
                        await self.db.update_position_highest_price(pos.symbol, price)
                        pos.highest_price = price
                    elif pos.side == "short" and (pos.highest_price is None or price < pos.highest_price):
                        await self.db.update_position_highest_price(pos.symbol, price)
                        pos.highest_price = price

                    new_sl = self.risk_manager.check_breakeven_sl(
                        entry_price=pos.entry_price,
                        current_price=price,
                        current_sl=pos.stop_loss_price,
                        take_profit=pos.take_profit_price,
                        side=pos.side,
                    )
                    if new_sl is not None and (
                        pos.stop_loss_price is None
                        or new_sl != pos.stop_loss_price
                    ):
                        log.info(
                            "BREAKEVEN SL | %s | %.6f → %.6f",
                            pos.symbol, pos.stop_loss_price, new_sl,
                        )
                        await self.db.update_position_sl(pos.symbol, new_sl)
                        pos.stop_loss_price = new_sl

                    # Ambil ATR current untuk trailing stop yang adaptif
                    current_atr = pos.atr_at_entry  # fallback ke ATR entry
                    try:
                        _mon_profile = get_coin_profile(pos.symbol, override_profile=pos.strategy_profile)
                        _mon_tf = _mon_profile.effective_confirmation_tf
                        candles = await self.exchange.fetch_ohlcv(
                            pos.symbol, _mon_tf, limit=20
                        )
                        if candles and len(candles) >= 15:
                            import pandas as pd
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

                    if (
                        current_atr
                        and current_atr > 0
                        and pos.stop_loss_price is not None
                    ):
                        # Ambil strategy profile coin untuk progressive trailing
                        _coin_profile = None
                        if hasattr(self, 'strategy') and self.strategy:
                            _coin_profile = self.strategy.get_profile(pos.symbol)
                        _profile_name = _coin_profile.profile.value if _coin_profile else ""

                        new_trailing_sl = self.risk_manager.check_trailing_sl(
                            entry_price=pos.entry_price,
                            current_price=price,
                            current_sl=pos.stop_loss_price,
                            atr=current_atr,
                            side=pos.side,
                            strategy_profile=_profile_name,
                        )
                        if (
                            new_trailing_sl is not None
                            and new_trailing_sl != pos.stop_loss_price
                        ):
                            log.info(
                                "TRAILING SL | %s | %.6f → %.6f",
                                pos.symbol, pos.stop_loss_price, new_trailing_sl,
                            )
                            await self.db.update_position_sl(pos.symbol, new_trailing_sl)
                            pos.stop_loss_price = new_trailing_sl

                    hit_sl = (
                        pos.stop_loss_price is not None and (
                            (pos.side == "long"  and price <= pos.stop_loss_price)
                            or (pos.side == "short" and price >= pos.stop_loss_price)
                        )
                    )
                    hit_tp = (
                        pos.take_profit_price is not None and (
                            (pos.side == "long"  and price >= pos.take_profit_price)
                            or (pos.side == "short" and price <= pos.take_profit_price)
                        )
                    )

                    trailing_reason = None
                    if self.strategy and hasattr(self.strategy, "check_trailing_exit"):
                        trailing_reason = self.strategy.check_trailing_exit(pos.symbol, price)

                    # ── Adaptive Trade Guardian (ATG) ──────────────────────
                    try:
                        from intelligence.trade_guardian import check_atg
                        _atg_df = None
                        try:
                            import pandas as pd
                            _atg_profile = get_coin_profile(pos.symbol, override_profile=pos.strategy_profile)
                            _atg_tf = _atg_profile.effective_confirmation_tf
                            _atg_candles = await self.exchange.fetch_ohlcv(
                                pos.symbol, _atg_tf, limit=50
                            )
                            if _atg_candles and len(_atg_candles) >= 15:
                                _atg_df = pd.DataFrame(
                                    _atg_candles,
                                    columns=["ts","open","high","low","close","volume"]
                                ).astype({"high":float,"low":float,"close":float,"volume":float})
                        except Exception as _atg_fe:
                            log.debug("ATG fetch candles gagal [%s]: %s", pos.symbol, _atg_fe)

                        _atg_regime = pos.entry_regime or "trending_bull"
                        _atg_result = check_atg(
                            entry_price=pos.entry_price or 0.0,
                            current_price=price,
                            highest_price=pos.highest_price or max(price, pos.entry_price or price),
                            current_sl=pos.stop_loss_price,
                            df=_atg_df,
                            symbol=pos.symbol,
                            regime=_atg_regime,
                        )

                        # Update SL ke profit zone kalau lebih baik
                        if _atg_result.new_sl is not None:
                            if (pos.stop_loss_price is None
                                    or _atg_result.new_sl > pos.stop_loss_price):
                                log.info(
                                    "ATG ProfitZone SL | %s | %.6f → %.6f",
                                    pos.symbol, pos.stop_loss_price or 0, _atg_result.new_sl,
                                )
                                await self.db.update_position_sl(pos.symbol, _atg_result.new_sl)
                                pos.stop_loss_price = _atg_result.new_sl

                        # ATG composite exit
                        if _atg_result.should_exit and not trailing_reason and not hit_sl and not hit_tp:
                            log.info(
                                "ATG EXIT [%s] @ %.6f | %s",
                                pos.symbol, price, _atg_result.exit_reason,
                            )
                            est_pnl = 0.0
                            if pos.entry_price and pos.amount:
                                est_pnl = (price - pos.entry_price) * pos.amount
                            await self.notifier.notify_sl_tp_hit(
                                symbol=pos.symbol, trigger="take_profit",
                                price=price, entry_price=pos.entry_price, pnl=est_pnl,
                            )
                            await self._close_position_market(pos, price, _atg_result.exit_reason)
                            continue
                    except Exception as _atg_err:
                        log.debug("ATG error [%s]: %s", pos.symbol, _atg_err)
                    # ── End ATG ────────────────────────────────────────────

                    # ── Early Exit Confirmation ──────────────────────────
                    try:
                        if (
                            not hit_sl and not hit_tp
                            and pos.entry_score
                            and pos.stop_loss_price
                            and pos.entry_price
                        ):
                            _latest_score = await self.db.get_latest_signal_score(pos.symbol)
                            if _latest_score is not None:
                                _score_drop = pos.entry_score - _latest_score.total_score
                                _sl_dist    = abs(price - pos.stop_loss_price)
                                _en_dist    = abs(pos.entry_price - pos.stop_loss_price)
                                _danger     = _en_dist > 0 and (_sl_dist / _en_dist) < 0.5
                                _regime_chg = (
                                    _latest_score.regime is not None
                                    and pos.entry_regime is not None
                                    and _latest_score.regime != pos.entry_regime
                                )
                                if _score_drop > 20 and _danger and _regime_chg:
                                    log.info(
                                        "EARLY EXIT [%s] @ %.6f | score drop %.1f->%.1f | regime %s->%s | danger %.1f%%",
                                        pos.symbol, price, pos.entry_score, _latest_score.total_score,
                                        pos.entry_regime, _latest_score.regime,
                                        (_sl_dist / _en_dist * 100) if _en_dist > 0 else 0,
                                    )
                                    est_pnl = 0.0
                                    if pos.entry_price and pos.amount:
                                        est_pnl = (
                                            (price - pos.entry_price) * pos.amount
                                            if pos.side == "long"
                                            else (pos.entry_price - price) * pos.amount
                                        )
                                    await self.notifier.notify_sl_tp_hit(
                                        symbol=pos.symbol, trigger="stop_loss",
                                        price=price, entry_price=pos.entry_price, pnl=est_pnl,
                                    )
                                    await self._close_position_market(pos, price, "early_exit_score_drop")
                                    continue
                    except Exception as _ee_err:
                        log.debug("Early exit check error [%s]: %s", pos.symbol, _ee_err)
                    # ── End Early Exit ────────────────────────────────────

                    # ── Regime Transition Handler ─────────────────────────────────
                    try:
                        if (
                            not hit_sl and not hit_tp
                            and self.strategy
                            and hasattr(self.strategy, "_handle_regime_transition")
                        ):
                            _tracker = self.strategy.get_tracker(pos.symbol)
                            _cur_regime = None
                            try:
                                _latest_sig = await self.db.get_latest_signal_score(pos.symbol)
                                if _latest_sig is not None:
                                    _cur_regime = (
                                        _latest_sig.regime.value
                                        if hasattr(_latest_sig.regime, "value")
                                        else str(_latest_sig.regime)
                                    )
                            except Exception:
                                pass
                            if _tracker and _cur_regime:
                                _rth_action = self.strategy._handle_regime_transition(
                                    _tracker, _cur_regime
                                )
                                if _rth_action == "EXIT":
                                    log.info(
                                        "REGIME TRANSITION EXIT [%s] @ %.6f | %s->%s",
                                        pos.symbol, price,
                                        _tracker.entry_regime, _cur_regime,
                                    )
                                    est_pnl = 0.0
                                    if pos.entry_price and pos.amount:
                                        est_pnl = (price - pos.entry_price) * pos.amount
                                    await self.notifier.notify_sl_tp_hit(
                                        symbol=pos.symbol, trigger="stop_loss",
                                        price=price, entry_price=pos.entry_price, pnl=est_pnl,
                                    )
                                    await self._close_position_market(
                                        pos, price, f"regime_transition_exit:{_tracker.entry_regime}->{_cur_regime}"
                                    )
                                    continue
                                elif _rth_action == "HOLD_TIGHTEN_SL":
                                    log.info(
                                        "REGIME TIGHTEN SL [%s] | new quick_sl_pct=%.2f%%",
                                        pos.symbol, _tracker.quick_sl_pct,
                                    )
                                elif _rth_action == "HOLD_RELAX_SL":
                                    log.info(
                                        "REGIME RELAX SL [%s] | new quick_sl_pct=%.2f%%",
                                        pos.symbol, _tracker.quick_sl_pct,
                                    )
                    except Exception as _rth_err:
                        log.debug("Regime transition handler error [%s]: %s", pos.symbol, _rth_err)
                    # ── End Regime Transition Handler ────────────────────────

                    if trailing_reason and (hit_sl or hit_tp):
                        log.warning(
                            "DUAL TRIGGER [%s]: trailing=%s AND sl_hit=%s tp_hit=%s "
                            "— prioritaskan SL/TP",
                            pos.symbol, trailing_reason[:60], hit_sl, hit_tp,
                        )
                        trailing_reason = None

                    if trailing_reason:
                        log.info(
                            "TRAILING EXIT: %s @ %.6f | %s",
                            pos.symbol, price, trailing_reason,
                        )
                        est_pnl = 0.0
                        if pos.entry_price and pos.amount:
                            est_pnl = (price - pos.entry_price) * pos.amount
                        await self.notifier.notify_sl_tp_hit(
                            symbol=pos.symbol, trigger="take_profit",
                            price=price, entry_price=pos.entry_price, pnl=est_pnl,
                        )
                        await self._close_position_market(pos, price, trailing_reason)
                        continue

                    if hit_sl or hit_tp:
                        trigger = "stop_loss" if hit_sl else "take_profit"
                        reason  = "Stop-loss hit" if hit_sl else "Take-profit hit"
                        log.info(
                            "%s | %s @ %.6f | SL=%s TP=%s entry=%.6f",
                            trigger.upper(), pos.symbol, price,
                            pos.stop_loss_price, pos.take_profit_price, pos.entry_price,
                        )

                        est_pnl = 0.0
                        if pos.entry_price and pos.amount:
                            est_pnl = (
                                (price - pos.entry_price) * pos.amount
                                if pos.side == "long"
                                else (pos.entry_price - price) * pos.amount
                            )

                        await self.notifier.notify_sl_tp_hit(
                            symbol=pos.symbol, trigger=trigger,
                            price=price, entry_price=pos.entry_price, pnl=est_pnl,
                        )
                        await self._close_position_market(pos, price, reason)
                        continue

                    if pos.entry_price and pos.entry_price > 0 and pos.amount:
                        upnl = (
                            (price - pos.entry_price) * pos.amount
                            if pos.side == "long"
                            else (pos.entry_price - price) * pos.amount
                        )
                        cost     = pos.entry_price * pos.amount
                        upnl_pct = (upnl / cost * 100) if cost > 0 else 0.0
                        await self.db.update_position_price(
                            pos.symbol, price, upnl, upnl_pct
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SL/TP monitor error: %s", e, exc_info=True)

            await asyncio.sleep(self.SL_TP_CHECK_INTERVAL)

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        if self.ws_feed and self.ws_feed.is_feed_healthy(symbol):
            price = self.ws_feed.get_price(symbol)
            if price and price > 0:
                return price
        try:
            tk  = await self.exchange.fetch_ticker(symbol)
            bid = tk.get("bid")
            ask = tk.get("ask")
            if bid and ask and float(bid) > 0 and float(ask) > 0:
                return (float(bid) + float(ask)) / 2.0
            last = tk.get("last")
            return float(last) if last else None
        except Exception as e:
            log.warning("REST price fallback gagal untuk %s: %s", symbol, e)
            return None

    async def _try_shadow_trade(self, signal: SignalEvent) -> None:
        """
        Saat algotrader pintu tutup (max open positions), titipkan sinyal
        ke algotrader_test agar dicoba secara demo (Cara B):
          1. Cek slot algotrader_test — kalau penuh, shadow ditangguhkan
          2. Tulis shadow position ke DB algotrader_test (penanda titipan)
          3. Tambahkan koin ke WATCHLIST algotrader_test sementara
          4. algotrader_test scan & beli sendiri pakai saldo demo
        Penanda: strategy_name = "SHADOW:algotrader:<symbol>"
        """
        peer_db_path = os.getenv("CROSS_LEARN_DB", "")
        peer_env     = os.getenv("PEER_BOT_ENV",  "")
        peer_dir     = os.getenv("PEER_BOT_DIR",  "")

        if not peer_db_path or not peer_env or not peer_dir:
            log.debug("ShadowTrade: PEER_BOT tidak dikonfigurasi — skip.")
            return

        symbol     = signal.symbol
        shadow_tag = "SHADOW:algotrader"

        try:
            # --- Load DB algotrader_test ---
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_peer_db", os.path.join(peer_dir, "database.py")
            )
            peer_db_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(peer_db_mod)

            peer_db = peer_db_mod.DatabaseManager(
                f"sqlite+aiosqlite:///{peer_db_path}"
            )
            await peer_db.init_db()

            # --- Cek duplikat posisi ---
            existing = await peer_db.get_open_position_by_symbol(symbol)
            if existing:
                log.debug(
                    "ShadowTrade: %s sudah ada posisi di algotrader_test — skip.",
                    symbol,
                )
                await peer_db.close()
                return

            # --- Baca .env algotrader_test ---
            peer_env_data = {}
            try:
                with open(peer_env, "r") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            peer_env_data[k.strip()] = v.strip()
            except Exception as e:
                log.warning("ShadowTrade: gagal baca peer .env: %s", e)
                await peer_db.close()
                return

            peer_universe_raw = peer_env_data.get("UNIVERSE_WATCHLIST", "")
            peer_universe     = [s.strip() for s in peer_universe_raw.split(",") if s.strip()]
            peer_max           = int(peer_env_data.get("MAX_OPEN_POSITIONS", "5"))

            # --- Cek: koin sudah ada di watchlist algotrader_test ---
            if symbol in peer_universe:
                log.debug(
                    "ShadowTrade: %s sudah di universe algotrader_test — skip.",
                    symbol,
                )
                await peer_db.close()
                return

            # --- Cek: koin sudah dalam shadow list ---
            peer_positions = await peer_db.get_open_positions()
            shadow_syms    = {
                p.symbol for p in peer_positions
                if (p.strategy_name or "").startswith(shadow_tag)
            }
            if symbol in shadow_syms:
                log.debug(
                    "ShadowTrade: %s sudah dalam shadow list — skip.", symbol
                )
                await peer_db.close()
                return

            # Shadow pakai slot ekstra — tidak dibatasi MAX_OPEN_POSITIONS test
            # Hitung info shadow aktif untuk log
            regular_open = [
                p for p in peer_positions
                if not (p.strategy_name or "").startswith(shadow_tag)
            ]
            shadow_open  = [
                p for p in peer_positions
                if (p.strategy_name or "").startswith(shadow_tag)
            ]

            # --- Tulis shadow position ke DB test ---
            shadow_name = f"{shadow_tag}:{symbol}"
            price       = signal.price or 0.0
            atr         = signal.metadata.get("atr")

            await peer_db.upsert_position(symbol, {
                "entry_time":        datetime.now(timezone.utc).replace(tzinfo=None),
                "entry_price":       round(price, 8),
                "current_price":     round(price, 8),
                "amount":            0.0,
                "side":              "long",
                "is_open":           True,
                "is_closing":        False,
                "stop_loss_price":   signal.stop_loss,
                "take_profit_price": signal.take_profit,
                "atr_at_entry":      round(float(atr), 8) if atr else None,
                "strategy_name":     shadow_name,
                "entry_order_id":    f"SHADOW_{symbol}_{int(datetime.now().timestamp())}",
            })
            await peer_db.close()

            # --- Inject ke universe_overrides DB algotrader_test (tanpa restart) ---
            try:
                import importlib.util as _ilu
                _spec2 = _ilu.spec_from_file_location("_peer_db2", peer_dir + "/database.py")
                _pmod2 = _ilu.module_from_spec(_spec2)
                _spec2.loader.exec_module(_pmod2)
                _pdb2  = _pmod2.DatabaseManager(f"sqlite+aiosqlite:///{peer_db_path}")
                await _pdb2.init_db()
                await _pdb2.upsert_universe_override(
                    symbol = symbol,
                    source = "shadow",
                    notes  = f"SHADOW:algotrader — titipan saat pintu tutup",
                )
                await _pdb2.close()
                log.info(
                    "ShadowTrade: %s diinjeksi ke universe_overrides algotrader_test (tanpa restart).",
                    symbol,
                )
            except Exception as e:
                log.warning("ShadowTrade: gagal inject universe_overrides: %s", e)

            log.info(
                "ShadowTrade: [%s] berhasil dititipkan ke algotrader_test. "
                "Shadow slots: %d | Regular slots: %d/%d",
                symbol, len(shadow_open) + 1, len(regular_open), peer_max,
            )

            if self.notifier:
                try:
                    msg = (
                        f"\U0001f504 *Shadow Trade Aktif*\n"
                        f"Koin *{symbol}* dititipkan ke algotrader_test\n"
                        f"_(pintu algotrader sedang tutup)_\n"
                        f"Entry ref: `{price:.6f}` | SL: `{signal.stop_loss or 0:.6f}`\n"
                        f"Slot test: {len(regular_open)}/{peer_max} | "
                        f"Shadow aktif: {len(shadow_open) + 1}"
                    )
                    await self.notifier.notify_info(msg)
                except Exception as e:
                    log.warning("ShadowTrade: gagal kirim notif: %s", e)
                    log.warning("ShadowTrade: gagal kirim notif: %s", e)

        except Exception as e:
            log.warning(
                "ShadowTrade: error saat titip koin %s: %s", symbol, e, exc_info=True
            )

    async def _cleanup_shadow_trade(self, symbol: str) -> None:
        """
        Dipanggil saat algotrader berhasil buka posisi baru:
          1. Tutup shadow position di DB algotrader_test
          2. Hapus koin dari WATCHLIST algotrader_test
          3. Restart algotrader_test agar baca universe terbaru
        """
        peer_db_path = os.getenv("CROSS_LEARN_DB", "")
        peer_env     = os.getenv("PEER_BOT_ENV",  "")
        peer_dir     = os.getenv("PEER_BOT_DIR",  "")

        if not peer_db_path or not peer_env or not peer_dir:
            return

        shadow_tag = "SHADOW:algotrader"

        try:
            # --- Load DB algotrader_test ---
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_peer_db", os.path.join(peer_dir, "database.py")
            )
            peer_db_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(peer_db_mod)

            peer_db = peer_db_mod.DatabaseManager(
                f"sqlite+aiosqlite:///{peer_db_path}"
            )
            await peer_db.init_db()

            existing = await peer_db.get_open_position_by_symbol(symbol)

            # Hanya cleanup kalau memang shadow position (bukan posisi regular test)
            if not existing or not (existing.strategy_name or "").startswith(shadow_tag):
                await peer_db.close()
                return

            # --- Tutup shadow position di DB ---
            from sqlalchemy import update as _sa_update
            async with peer_db._session() as s:
                await s.execute(
                    _sa_update(peer_db_mod.Position)
                    .where(peer_db_mod.Position.symbol == symbol)
                    .where(peer_db_mod.Position.is_open == True)
                    .where(peer_db_mod.Position.strategy_name.like(f"{shadow_tag}%"))
                    .values(
                        is_open      = False,
                        exit_time    = datetime.now(timezone.utc).replace(tzinfo=None),
                        realized_pnl = 0.0,
                    )
                )
                await s.commit()

            await peer_db.close()
            log.info(
                "ShadowTrade cleanup: shadow position %s ditutup di DB algotrader_test.",
                symbol,
            )

            # --- Deactivate dari universe_overrides DB algotrader_test (tanpa restart) ---
            try:
                import importlib.util as _ilu
                _spec3 = _ilu.spec_from_file_location("_peer_db3", peer_dir + "/database.py")
                _pmod3 = _ilu.module_from_spec(_spec3)
                _spec3.loader.exec_module(_pmod3)
                _pdb3  = _pmod3.DatabaseManager(f"sqlite+aiosqlite:///{peer_db_path}")
                await _pdb3.init_db()
                await _pdb3.deactivate_universe_override(symbol)
                await _pdb3.close()
                log.info(
                    "ShadowTrade cleanup: %s dinonaktifkan dari universe_overrides algotrader_test (tanpa restart).",
                    symbol,
                )
            except Exception as e:
                log.warning("ShadowTrade cleanup: gagal deactivate universe_overrides: %s", e)

            if self.notifier:
                try:
                    msg = (
                        "\U00002705 *Shadow Trade Selesai*\n"
                        f"Koin *{symbol}* berhasil dibeli algotrader.\n"
                        "Shadow di algotrader_test sudah dibersihkan.\n"
                        "Watchlist test kembali normal."
                    )
                    await self.notifier.notify_info(msg)
                except Exception as e:
                    log.warning("ShadowTrade cleanup: gagal kirim notif: %s", e)

        except Exception as e:
            log.warning(
                "ShadowTrade cleanup: error untuk %s: %s", symbol, e, exc_info=True
            )

    async def _handle_signal(self, signal: SignalEvent) -> None:
        log.info("SIGNAL: %s", signal)
        if signal.signal_type == SignalType.BUY:
            await self._handle_buy(signal)
        elif signal.signal_type in (SignalType.CLOSE_LONG, SignalType.SELL):
            await self._handle_close(signal)

    async def _handle_buy(self, signal: SignalEvent) -> None:
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
            log.warning("Equity=0 — skip BUY signal untuk %s", symbol)
            _reset_position_flag()
            return

        # Jalur Entry 2: kurangi size kalau entry saat transisi regime
        _trans_mult = getattr(signal, "_transition_size_mult", None)
        _effective_size_pct = self.config["max_position_size_pct"]
        if _trans_mult is not None:
            _effective_size_pct = _effective_size_pct * float(_trans_mult)
            log.info(
                "[Entry2] %s transition size: %.1f%% x %.1f = %.1f%%",
                symbol, self.config["max_position_size_pct"], _trans_mult, _effective_size_pct,
            )

        # [TAMBAHAN] Confidence-based sizing — KHUSUS mode legacy.
        # Sebelumnya: signal.confidence (dihitung _compute_confidence di
        # strategy.py, 4 komponen tertimbang: breakout strength, volume,
        # RSI, trend) dihasilkan dengan cermat tapi HANYA dipakai untuk
        # notifikasi/log — sama sekali tidak memengaruhi ukuran posisi.
        # Sinyal confidence 0.95 dan confidence 0.35 mendapat ukuran posisi
        # identik (flat max_position_size_pct). Mode pipeline v7 sudah
        # punya Kelly sizing sendiri berbasis score (lihat Kelly Ceiling di
        # bawah) — jadi confidence sizing ini HANYA diterapkan kalau sinyal
        # berasal dari mode legacy (fallback, pipeline_mode="legacy" di
        # metadata), supaya tidak tumpang tindih/dobel-kurangi dengan Kelly.
        # Formula floor 50%: size = max_pct x (0.5 + 0.5 x confidence).
        # confidence=1.0 -> 100% dari max_pct. confidence=0.0 -> 50% dari
        # max_pct (floor, bukan nol — confidence belum divalidasi terhadap
        # data trading nyata, jadi pengaruhnya dibatasi, bukan drastis).
        if signal.metadata and signal.metadata.get("pipeline_mode") == "legacy":
            _conf = max(0.0, min(1.0, float(signal.confidence or 0.0)))
            _conf_mult = 0.5 + 0.5 * _conf
            _before_conf = _effective_size_pct
            _effective_size_pct = _effective_size_pct * _conf_mult
            log.info(
                "[ConfidenceSizing] %s legacy mode: %.1f%% x conf_mult(%.3f, "
                "confidence=%.3f) = %.1f%%",
                symbol, _before_conf, _conf_mult, _conf, _effective_size_pct,
            )

        rough_qty = (
            (equity * _effective_size_pct / 100) / price
            if price > 0 else 0
        )

        assessment = await self.risk_manager.evaluate_order(
            symbol=symbol,
            side="buy",
            price=price,
            quantity=rough_qty,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            atr=atr,
        )

        if not assessment.is_approved:
            log.info("Risk REJECTED %s: %s", symbol, assessment.reason)
            # Jika ditolak karena slot penuh → titipkan ke algotrader_test
            if "Max open positions" in (assessment.reason or ""):
                await self._try_shadow_trade(signal)
            _reset_position_flag()
            return

        # [BUG-FIX v2] Kelly sizing dari Gate 4.5 (commander.py) sebelumnya
        # dihitung lengkap (Kelly criterion + quality_mult + consec_mult +
        # correlation penalty) tapi position_size_pct hasilnya tidak pernah
        # disambungkan ke eksekusi nyata — assessment.approved_size SELALU
        # murni dari ATR-sizing/max_pct di risk.py, terlepas dari hasil Kelly.
        # Diterapkan di sini sbg CEILING TAMBAHAN saja (bukan pengganti
        # ATR-sizing) — Kelly cuma bisa MENGURANGI approved_size, tidak
        # pernah menaikkannya di atas yang sudah disetujui risk_manager.
        _kelly_size_pct = signal.metadata.get("kelly_size_pct") if signal.metadata else None
        if (
            _kelly_size_pct
            and _kelly_size_pct > 0
            and assessment.approved_size
            and price > 0
        ):
            _kelly_max_qty = (equity * _kelly_size_pct / 100) / price
            if _kelly_max_qty < assessment.approved_size:
                log.info(
                    "[Kelly Ceiling] %s: approved_size %.8f -> %.8f (Kelly cap %.2f%% equity)",
                    symbol, assessment.approved_size, _kelly_max_qty, _kelly_size_pct,
                )
                assessment.approved_size = _kelly_max_qty

        trade = await self.executor.execute_signal(signal, assessment)

        if trade is None:
            log.warning("execute_signal gagal untuk %s — posisi TIDAK dibuka.", symbol)
            _reset_position_flag()
            return

        actual_amount = float(trade.filled or trade.amount)
        if trade.notes and "iceberg_actual_filled=" in (trade.notes or ""):
            try:
                tag    = next(p for p in trade.notes.split("|") if p.strip().startswith("iceberg_actual_filled="))
                parsed = float(tag.split("=")[1])
                if parsed > 0:
                    actual_amount = parsed
                    log.info("Iceberg actual_amount diambil dari notes: %s = %.8f", trade.symbol, actual_amount)
            except (StopIteration, ValueError, IndexError) as e:
                log.warning("Gagal parse iceberg_actual_filled: %s — fallback %.8f", e, actual_amount)

        try:
            await self.db.upsert_position(symbol, {
                "entry_time":        datetime.now(timezone.utc).replace(tzinfo=None),
                "entry_price":       round(trade.executed_price, 8),
                "current_price":     round(trade.executed_price, 8),
                "amount":            round(actual_amount, 8),
                "side":              "long",
                "is_open":           True,
                "is_closing":        False,
                "stop_loss_price":   assessment.stop_loss,
                "take_profit_price": assessment.take_profit,
                "atr_at_entry":      round(float(atr), 8) if atr else None,
                "strategy_name":     signal.strategy,
                "entry_order_id":    trade.order_id,
                "entry_regime":      signal.regime.value if hasattr(signal.regime, "value") else str(signal.regime or "undefined"),
            })

            entry_fee_actual = float(trade.fee_cost or 0)
            if entry_fee_actual > 0:
                try:
                    await self.db.update_position_entry_fee(symbol, entry_fee_actual)
                    log.info("Entry fee aktual disimpan: %s fee=%.8f", symbol, entry_fee_actual)
                except Exception as e:
                    log.warning("Gagal simpan entry fee aktual %s: %s", symbol, e)

        except Exception as e:
            log.error("upsert_position gagal untuk %s: %s — reset flag", symbol, e)
            _reset_position_flag()
            raise

        with self.strategy._lock:
            last_params = dict(getattr(self.strategy, "_last_entry_params", {}))
        entry_info = last_params.get(symbol, {})
        exit_mode  = entry_info.get("exit_mode", ExitMode.QUICK_PROFIT)
        p          = entry_info.get("p", {})

        if not p:
            log.warning("Entry params cache kosong untuk %s — re-compute dari profile.", symbol)
            atr_val = float(atr) if atr else 0.0
            try:
                p = self.strategy._resolve_params(
                    symbol, float(trade.executed_price), atr_val, 1.0, 55.0,
                )
                atr_pct = (atr_val / float(trade.executed_price) * 100) if trade.executed_price else 0.0
                prof = get_coin_profile(symbol)
                exit_mode = (
                    ExitMode.RIDE_THE_WAVE
                    if atr_pct >= prof.atr_pct_threshold
                    else ExitMode.QUICK_PROFIT
                )
                log.info("Re-computed params untuk %s: exit_mode=%s", symbol, exit_mode.value)
            except Exception as re_err:
                log.error("Re-compute params gagal untuk %s: %s", symbol, re_err)

        p_obj = None
        if p:
            try:
                from database import Position
                p_obj = await self.db.get_open_position_by_symbol(symbol)
            except Exception:
                pass

        if p_obj:
            # Guard: cek apakah tracker sudah ada dari commander path
            tracker_already_set = False
            with self.strategy._lock:
                tracker_already_set = symbol in self.strategy._pos_trackers

            if tracker_already_set:
                # Update entry_price ke harga fill aktual (bukan harga signal)
                with self.strategy._lock:
                    tracker = self.strategy._pos_trackers.get(symbol)
                    if tracker:
                        tracker.entry_price   = float(trade.executed_price)
                        tracker.highest_price = float(trade.executed_price)
                log.debug(
                    "register_position skip %s — tracker sudah ada dari commander. "
                    "entry_price diupdate ke harga fill aktual: %.6f",
                    symbol, float(trade.executed_price),
                )
            else:
                self.strategy.register_position(
                    symbol=symbol,
                    entry_price=float(trade.executed_price),
                    exit_mode=exit_mode,
                    p=p,
                )
        else:
            log.critical(
                "CRITICAL: register_position dilewati untuk %s — "
                "trailing stop tidak akan aktif! Pantau posisi manual.",
                symbol,
            )
            await self.db.save_log(
                "CRITICAL", "main",
                f"register_position gagal {symbol} — trailing stop tidak aktif!"
            )

        # Bersihkan shadow position di algotrader_test jika ada
        await self._cleanup_shadow_trade(symbol)

        log.info(
            "POSISI DIBUKA: %s | entry=%.6f amount=%.8f SL=%.6f TP=%.6f",
            symbol, trade.executed_price, float(trade.filled or trade.amount),
            assessment.stop_loss or 0, assessment.take_profit or 0,
        )
        await self.db.save_log(
            "INFO", "main",
            f"Posisi dibuka: {symbol} @ {trade.executed_price:.6f} "
            f"SL={assessment.stop_loss or 0:.6f} "
            f"TP={assessment.take_profit or 0:.6f}",
        )

        meta = signal.metadata
        await self.notifier.notify_trade_opened(
            symbol=symbol,
            side="buy",
            entry_price=float(trade.executed_price),
            amount=float(trade.filled or trade.amount),
            stop_loss=assessment.stop_loss,
            take_profit=assessment.take_profit,
            atr=float(atr) if atr else None,
            confidence=signal.confidence,
            coin_profile=meta.get("coin_profile",   "universal"),
            exit_mode=meta.get("exit_mode",          ""),
            adaptive_mode=meta.get("adaptive_mode",  ""),
        )
        # Proyeksi trade — dikirim setelah notif entry
        try:
            await self.notifier.notify_projection(
                symbol=symbol,
                side="buy",
                entry_price=float(trade.executed_price),
                amount=float(trade.filled or trade.amount),
                stop_loss=assessment.stop_loss,
                take_profit=assessment.take_profit,
                atr=float(atr) if atr else None,
                confidence=signal.confidence,
                total_score=signal.total_score or 0.0,
                score_breakdown=signal.score_breakdown or {},
                regime=signal.regime or "",
                narrative=signal.scoring_narrative or "",
            )
        except Exception as proj_err:
            log.warning("notify_projection gagal: %s", proj_err)

    async def _handle_close(self, signal: SignalEvent) -> None:
        async with self._closing_lock:
            is_closing = signal.symbol in self._closing_symbols
        if is_closing:
            log.debug("_handle_close skip %s — sudah dalam proses close.", signal.symbol)
            return

        positions = await self.db.get_open_positions()
        for pos in positions:
            if pos.symbol == signal.symbol:
                reason = signal.metadata.get("exit_reason", "Strategy exit signal")
                await self._close_position_market(pos, signal.price, reason)

    async def _close_position_market(self, pos, exit_price: float, reason: str) -> None:
        async with self._closing_lock:
            if pos.symbol in self._closing_symbols:
                log.warning(
                    "SKIP double-close: %s sudah dalam proses closing (reason=%s)",
                    pos.symbol, reason,
                )
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
        existing = await self.db.get_open_position_by_symbol(pos.symbol)
        if not existing:
            log.warning(
                "Position %s sudah tidak open di DB — skip close (reason=%s)",
                pos.symbol, reason,
            )
            return

        close_signal = SignalEvent(
            symbol=pos.symbol,
            signal_type=SignalType.SELL,
            price=exit_price,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            strategy=pos.strategy_name or "risk_monitor",
            metadata={"exit_reason": reason},
        )

        # [BUG-FIX] Sebelumnya: close_assessment dibuat manual dengan
        # approved_size=pos.amount tanpa cek apakah saldo riil koin di
        # exchange benar-benar cukup untuk dijual sebanyak itu (selisih
        # kecil bisa muncul dari fee entry yang dipotong dalam bentuk
        # koin, dll). evaluate_order(side="sell") di risk.py punya guard
        # untuk ini tapi tidak pernah dipanggil dari sini.
        # Sekarang: fetch saldo riil dan lewatkan ke evaluate_order supaya
        # guard itu benar-benar terpakai. Kalau fetch gagal (network dll),
        # fallback ke approved_size=pos.amount seperti sebelumnya — tidak
        # membuat close flow lebih rapuh dari sebelumnya.
        free_coin_balance: Optional[float] = None
        try:
            base_asset = pos.symbol.split('/')[0]
            balance = await self.exchange.fetch_balance()
            free_coin_balance = float(balance.get(base_asset, {}).get('free', 0) or 0)
        except Exception as e:
            log.warning(
                "Gagal fetch saldo %s untuk close guard — pakai pos.amount: %s",
                pos.symbol, e,
            )

        if free_coin_balance is not None:
            sell_assessment = await self.risk_manager.evaluate_order(
                symbol=pos.symbol,
                side="sell",
                price=exit_price,
                quantity=pos.amount,
                free_coin_balance=free_coin_balance,
            )
            close_assessment = RiskAssessment(
                decision=RiskDecision.APPROVED,
                reason=reason,
                approved_size=(
                    sell_assessment.approved_size
                    if sell_assessment.is_approved and sell_assessment.approved_size
                    else pos.amount
                ),
                stop_loss=None,
                take_profit=None,
            )
        else:
            close_assessment = RiskAssessment(
                decision=RiskDecision.APPROVED,
                reason=reason,
                approved_size=pos.amount,
                stop_loss=None,
                take_profit=None,
            )

        trade = await self.executor.execute_signal(close_signal, close_assessment)

        if trade is None:
            log.error(
                "CLOSE ORDER GAGAL untuk %s — posisi TETAP terbuka di DB! "
                "Tutup manual di Binance.",
                pos.symbol,
            )
            await self.db.save_log(
                "CRITICAL", "main",
                f"CLOSE GAGAL: {pos.symbol} — posisi masih terbuka! Tutup manual.",
            )

            retry = self._close_retry_count.get(pos.symbol, 0) + 1
            self._close_retry_count[pos.symbol] = retry
            if retry >= 3:
                await self.notifier.notify_error(
                    "close_position",
                    f"CLOSE ORDER GAGAL {retry}x untuk {pos.symbol} — "
                    "INTERVENSI MANUAL DIPERLUKAN! Tutup di Binance.",
                )
            else:
                await self.notifier.notify_error(
                    "close_position",
                    f"CLOSE ORDER GAGAL untuk {pos.symbol} — "
                    f"attempt {retry}/3. Retry otomatis.",
                )
            return

        self._close_retry_count.pop(pos.symbol, None)

        # [UPGRADE] Reset orderbook state tracking untuk simbol ini setelah posisi
        # ditutup — agar absorption/spoofing history tidak terbawa ke trade berikutnya.
        # reset_state() ada di orderbook.py tapi belum pernah dipanggil dari manapun.
        try:
            from indicators.orderbook import reset_state as _ob_reset
            _ob_reset(pos.symbol)
        except Exception:
            pass  # non-critical, jangan crash close flow

        taker_fee = self.exchange.get_taker_fee(pos.symbol)

        # [BUG-FIX] PnL dihitung pakai pos.amount (catatan DB) & exit_price
        # (harga sinyal saat keputusan close dibuat) — bukan jumlah & harga
        # yang BENAR-BENAR tereksekusi di exchange.
        # Sebelumnya: gross_pnl/entry_fee/exit_fee semua pakai pos.amount dan
        # exit_price parameter. trade.filled & trade.executed_price (hasil
        # riil dari _process_fill di execution.py, lebih akurat karena
        # berasal langsung dari respons order exchange) tidak pernah dipakai.
        # Akibatnya kalau ada selisih (fee dalam koin, partial fill,
        # slippage harga), PnL yang tercatat di DB salah secara diam-diam,
        # terus-menerus, dan terakumulasi.
        # Sekarang: pakai trade.filled & trade.executed_price sebagai sumber
        # utama, dengan fallback ke pos.amount/exit_price kalau trade tidak
        # menyediakannya (misal trade lama / field None) — supaya tidak
        # crash dan tetap backward-compatible.
        actual_amount = (
            float(trade.filled)
            if trade.filled is not None and float(trade.filled) > 0
            else pos.amount
        )
        actual_exit_price = (
            float(trade.executed_price)
            if trade.executed_price is not None and float(trade.executed_price) > 0
            else exit_price
        )
        if actual_amount != pos.amount or actual_exit_price != exit_price:
            log.info(
                "PnL pakai nilai riil eksekusi %s | amount: %.8f→%.8f price: %.6f→%.6f",
                pos.symbol, pos.amount, actual_amount, exit_price, actual_exit_price,
            )

        if pos.entry_price and actual_amount:
            gross_pnl = (
                (actual_exit_price - pos.entry_price) * actual_amount
                if pos.side == "long"
                else (pos.entry_price - actual_exit_price) * actual_amount
            )

            if pos.entry_fee_actual is not None and pos.entry_fee_actual > 0:
                entry_fee = pos.entry_fee_actual
            else:
                entry_fee = pos.entry_price * actual_amount * taker_fee
                log.warning(
                    "PnL calc %s: entry_fee_actual tidak ada — fallback estimasi=%.8f",
                    pos.symbol, entry_fee,
                )

            exit_fee = float(
                trade.fee_cost
                if trade.fee_cost is not None and float(trade.fee_cost) > 0
                else actual_exit_price * actual_amount * taker_fee
            )

            realized_pnl = gross_pnl - entry_fee - exit_fee
        else:
            realized_pnl = 0.0

        if realized_pnl < 0:
            self.risk_manager.record_symbol_loss(pos.symbol, realized_pnl)

        # [BUG-FIX] simpan actual_exit_price (harga eksekusi riil) ke DB,
        # bukan exit_price (harga sinyal/parameter saat keputusan close
        # dibuat) — konsisten dengan realized_pnl yang sudah dihitung
        # dari actual_exit_price di atas.
        await self.db.close_position(pos.symbol, actual_exit_price, realized_pnl)

        async with self._equity_lock:
            current_eq = self.portfolio_state.get("total_equity", 0.0)
            self.portfolio_state["total_equity"] = max(0.0, current_eq + realized_pnl)
            self.portfolio_state["open_pnl"] = max(
                0.0,
                self.portfolio_state.get("open_pnl", 0.0) - (pos.unrealized_pnl or 0.0),
            )

        log.info(
            "POSISI DITUTUP: %s | entry=%.6f exit=%.6f pnl=%+.4f | reason=%s",
            pos.symbol, pos.entry_price, actual_exit_price, realized_pnl, reason,
        )
        await self.db.save_log(
            "INFO", "main",
            f"Posisi ditutup: {pos.symbol} @ {actual_exit_price:.6f} "
            f"PnL={realized_pnl:+.4f} | {reason}",
        )

        if self.strategy:
            self.strategy.unregister_position(pos.symbol)

        # [BUG-FIX] notifikasi pakai actual_exit_price & actual_amount
        # (nilai riil tereksekusi) — sebelumnya pakai exit_price & pos.amount
        # sehingga notifikasi Telegram bisa menampilkan jumlah/harga yang
        # sedikit berbeda dari yang sebenarnya terjadi di exchange.
        await self.notifier.notify_trade_closed(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=float(pos.entry_price),
            exit_price=float(actual_exit_price),
            amount=float(actual_amount),
            realized_pnl=realized_pnl,
            reason=reason,
        )

        await self._refresh_portfolio()

    async def _refresh_portfolio(self) -> None:
        import time as _t
        self._last_refresh_time = _t.monotonic()
        try:
            balance = await self.exchange.fetch_balance()
            quote   = self.config["quote_currency"]

            free_bal, locked_bal, total_bal = self.exchange.parse_balance(balance, quote)
            positions  = await self.db.get_open_positions()

            position_value_in_quote = 0.0
            open_pnl = 0.0
            for p in positions:
                current_price = float(p.current_price or p.entry_price or 0.0)
                if current_price > 0 and p.amount:
                    position_value_in_quote += float(p.amount) * current_price
                open_pnl += p.unrealized_pnl or 0.0

            total_eq = free_bal + position_value_in_quote

            log.debug(
                "Equity calc: USDT_total=%.4f pos_value=%.4f → total_eq=%.4f "
                "(free=%.4f locked=%.4f open_pnl=%.4f)",
                total_bal, position_value_in_quote, total_eq,
                free_bal, locked_bal, open_pnl,
            )

            prev_eq = self.portfolio_state.get("total_equity", 0.0)
            if prev_eq > 0:
                change_pct = abs(total_eq - prev_eq) / prev_eq * 100
                if change_pct > 20:
                    log.warning(
                        "Equity jump mencurigakan: %.4f → %.4f (%.1f%%) — "
                        "periksa kalkulasi balance",
                        prev_eq, total_eq, change_pct,
                    )

            eq_day_start = self.risk_manager.equity_at_day_start
            if eq_day_start > 0:
                daily_pnl     = total_eq - eq_day_start
                daily_pnl_pct = daily_pnl / eq_day_start * 100
            else:
                daily_pnl     = 0.0
                daily_pnl_pct = 0.0

            self.portfolio_state = {
                "total_equity":   round(total_eq,      4),
                "free_balance":   round(free_bal,      4),
                "locked_balance": round(locked_bal,    4),
                "open_pnl":       round(open_pnl,      4),
                "daily_pnl":      round(daily_pnl,     4),
                "daily_pnl_pct":  round(daily_pnl_pct, 4),
            }

            self.config["portfolio_value"] = total_eq
            prev_halted = self.risk_manager.is_halted
            avg_atr_pct = 0.0
            if positions:
                atr_vals = [
                    (p.atr_at_entry / p.entry_price * 100)
                    for p in positions
                    if p.atr_at_entry and p.entry_price and p.entry_price > 0
                ]
                avg_atr_pct = sum(atr_vals) / len(atr_vals) if atr_vals else 0.0

            self.risk_manager.update_portfolio_state(
                equity=total_eq,
                initial_equity=self.config["initial_capital"],
                free_balance=free_bal,
                open_positions_count=len(positions),
                atr_pct=avg_atr_pct,
            )

            if not prev_halted and self.risk_manager.is_halted:
                await self.notifier.notify_bot_halted(
                    reason=self.risk_manager._halt_reason.value,
                    detail=self.risk_manager._halt_detail,
                )

            await self.db.save_snapshot({
                "timestamp":      datetime.now(timezone.utc).replace(tzinfo=None),
                "total_equity":   round(total_eq,      4),
                "free_balance":   round(free_bal,      4),
                "locked_balance": round(locked_bal,    4),
                "open_pnl":       round(open_pnl,      4),
                "daily_pnl":      round(daily_pnl,     4),
                "daily_pnl_pct":  round(daily_pnl_pct, 4),
                "drawdown_pct":   round(self.risk_manager.current_drawdown_pct, 4),
            })

            # Rolling balance check
            _min_abs = float(os.getenv("MIN_BALANCE_USDT", "10.0"))
            _required = max(self.config["initial_capital"] * 0.1, _min_abs)
            if free_bal < _required:
                _warn = (
                    f"[PortfolioRefresh] WARNING: Free balance ${free_bal:.2f} "
                    f"< minimum ${_required:.2f} — bot mungkin tidak bisa entry order baru."
                )
                log.warning(_warn)
                await self.notifier.notify_error("portfolio_refresh", _warn)

        except Exception as e:
            log.error("Portfolio refresh error: %s", e, exc_info=True)

    async def _on_trade_executed(self, trade) -> None:
        import time as _t
        now = _t.monotonic()
        if now - self._last_refresh_time >= self._MIN_REFRESH_INTERVAL:
            await self._refresh_portfolio()
        else:
            log.debug(
                "Portfolio refresh di-skip (terlalu cepat dari refresh terakhir: %.1fs lalu).",
                now - self._last_refresh_time,
            )

    async def run_config_watcher(self) -> None:
        import json
        log.info("Config watcher dimulai (interval=30s).")
        while self.is_running:
            try:
                raw = await self.db.get_bot_state("config_update")
                if raw:
                    updates = json.loads(raw)
                    applied = []
                    for key, value in updates.items():
                        if key in self.config:
                            self.config[key] = value
                            applied.append(key)
                    if applied:
                        if "universe_watchlist" in applied and self.strategy:
                            self.strategy.update_symbols(self.config["universe_watchlist"])
                            # Update ws_feed untuk subscribe koin baru
                            if self.ws_feed:
                                await self.ws_feed.add_symbols(self.config["universe_watchlist"])
                        if any(k in applied for k in ["max_drawdown_pct","risk_per_trade_pct","max_open_positions"]):
                            self.risk_manager._update_config(self.config)
                        if any(k in applied for k in ["telegram_enabled","telegram_bot_token","telegram_chat_id"]):
                            self.notifier._update_config(self.config)
                        if any(k in applied for k in ["api_key","api_secret","exchange_id","testnet"]):
                            try:
                                log.info("[ConfigWatcher] Validasi credential baru sebelum reinit...")
                                _test_exchange = ExchangeConnector(
                                    exchange_id=self.config["exchange_id"],
                                    api_key=self.config["api_key"],
                                    api_secret=self.config["api_secret"],
                                    api_passphrase=self.config.get("api_passphrase", ""),
                                    testnet=self.config["testnet"],
                                    db=self.db,
                                )
                                _test_ok = await _test_exchange.connect()
                                if not _test_ok:
                                    log.error("[ConfigWatcher] Credential baru INVALID — reinit dibatalkan, pakai credential lama.")
                                    await self.db.save_log("ERROR","config_watcher","Credential baru invalid — reinit dibatalkan.")
                                    await _test_exchange.disconnect()
                                    continue
                                await _test_exchange.disconnect()
                                log.info("[ConfigWatcher] Credential baru valid — lanjut reinit exchange: %s", self.config["exchange_id"])
                                await self.exchange.disconnect()
                                self.exchange = ExchangeConnector(
                                    exchange_id=self.config["exchange_id"],
                                    api_key=self.config["api_key"],
                                    api_secret=self.config["api_secret"],
                                    api_passphrase=self.config.get("api_passphrase", ""),
                                    testnet=self.config["testnet"],
                                    db=self.db,
                                )
                                connected = await self.exchange.connect()
                                if connected:
                                    log.info("[ConfigWatcher] Exchange reinit OK.")
                                    await self.db.save_log("INFO","config_watcher","Exchange reinit OK.")
                                    try:
                                        if self.ws_feed:
                                            await self.ws_feed.stop()
                                        self.ws_feed = WebSocketFeed(
                                            exchange_id=self.config["exchange_id"],
                                            api_key=self.config["api_key"],
                                            api_secret=self.config["api_secret"],
                                            api_passphrase=self.config.get("api_passphrase", ""),
                                            symbols=self.config["universe_watchlist"],
                                            testnet=self.config["testnet"],
                                        )
                                        await self.ws_feed.start()
                                        log.info("[ConfigWatcher] WS feed reinit OK.")
                                        await self.db.save_log("INFO","config_watcher","WS feed reinit OK.")
                                    except Exception as ws_ex:
                                        log.error("[ConfigWatcher] WS feed reinit error: %s", ws_ex)
                                    # Validasi watchlist per exchange baru
                                    invalid_symbols = []
                                    for sym in self.config["universe_watchlist"]:
                                        info = self.exchange.get_market_info(sym)
                                        if not info:
                                            invalid_symbols.append(sym)
                                    if invalid_symbols:
                                        warn_msg = (
                                            f"[ConfigWatcher] Watchlist WARNING: "
                                            f"{invalid_symbols} tidak ditemukan di "
                                            f"{self.config['exchange_id'].upper()}. "
                                            f"Hapus pair tersebut via /setconfig universe_watchlist"
                                        )
                                        log.warning(warn_msg)
                                        await self.notifier.notify_error("config_watcher", warn_msg)
                                    else:
                                        log.info("[ConfigWatcher] Semua universe_watchlist valid di %s.", self.config["exchange_id"])
                                else:
                                    log.error("[ConfigWatcher] Exchange reinit FAILED.")
                                    await self.db.save_log("ERROR","config_watcher","Exchange reinit FAILED.")
                            except Exception as ex:
                                log.error("[ConfigWatcher] Exchange reinit error: %s", ex)
                        await self.db.clear_bot_state("config_update")
                        await self.db.save_log("INFO","config_watcher",f"Config updated: {', '.join(applied)}")
                        log.info("[ConfigWatcher] Applied: %s", applied)
            except Exception as e:
                log.debug("Config watcher error: %s", e)
            await asyncio.sleep(30)

    async def run(self) -> None:
        await self.start()

        # Refresh portfolio sekali saat startup agar equity tidak 0
        try:
            await self._refresh_portfolio()
            log.info("Portfolio startup refresh: equity=%.4f",
                     self.portfolio_state.get("total_equity", 0))
        except Exception as _pe:
            log.warning("Portfolio startup refresh gagal: %s", _pe)
        self._tasks = [
            asyncio.create_task(self.run_scanner_loop(),      name="task_scanner"),
            asyncio.create_task(self.run_gate3_worker(),       name="task_gate3_worker"),
            asyncio.create_task(self.run_portfolio_monitor(), name="task_portfolio"),
            asyncio.create_task(self.run_sl_tp_monitor(),     name="task_sl_tp"),
            asyncio.create_task(self.run_daily_summary(),     name="task_daily_summary"),
            asyncio.create_task(self.run_analytics_loop(),    name="task_analytics"),
            asyncio.create_task(self.run_coin_swap_loop(),   name="task_coin_swap"),
            asyncio.create_task(self.run_config_watcher(),    name="task_config_watcher"),
            asyncio.create_task(self.run_strategy_loop(),     name="task_strategy_loop"),
            asyncio.create_task(self.run_position_sync_loop(), name="task_position_sync"),
        ]

        # Start WhaleNotifier delete loop kalau Telegram enabled
        if (
            self.notifier is not None
            and hasattr(self.notifier, '_whale_notifier')
            and self.notifier._whale_notifier is not None
            and self.config.get('telegram_enabled', False)
        ):
            self._tasks.append(
                asyncio.create_task(
                    self.notifier._whale_notifier.start_delete_loop(),
                    name="task_whale_delete_loop",
                )
            )
            log.info("WhaleNotifier delete loop started")

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def run_position_sync_loop(self) -> None:
        """Periodik cek posisi Binance yang tidak tertracking, adopt & kawal."""
        import asyncio
        log.info("Position Sync loop dimulai — interval 5 menit")
        await asyncio.sleep(30)  # Tunggu bot fully started
        while True:
            try:
                result = await run_position_sync(self.exchange, self.db)
                if result["adopted"] > 0:
                    log.info(
                        "PositionSync: %d diadopsi | %d ditolak | %d error",
                        result["adopted"], result["rejected"], result["errors"],
                    )
            except Exception as e:
                log.error("run_position_sync_loop error: %s", e)
            await asyncio.sleep(300)  # Cek setiap 5 menit


async def main() -> None:
    bot = TradingBot()
    app = create_app(lambda: bot)

    server = uvicorn.Server(uvicorn.Config(
        app=app,
        host=bot.config["api_host"],
        port=bot.config["api_port"],
        log_level="warning",
        access_log=False,
        loop="asyncio",
    ))

    loop = asyncio.get_running_loop()

    def _on_shutdown_signal():
        log.info("Shutdown signal diterima — menghentikan dengan graceful...")
        for task in bot._tasks:
            task.cancel()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_shutdown_signal)
        except NotImplementedError:
            pass

    log.info(
        "Dashboard API: http://%s:%d",
        bot.config["api_host"],
        bot.config["api_port"],
    )

    try:
        await asyncio.gather(bot.run(), server.serve())
    except BotStartupError as e:
        log.critical("Bot startup FAILED: %s", e)
        await bot.stop()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
