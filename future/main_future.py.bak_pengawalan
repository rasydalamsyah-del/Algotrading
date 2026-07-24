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
- run_coin_swap_loop(): TIDAK diikutsertakan sama sekali -- sistem ini
  sudah dikonfirmasi deprecated permanen di spot, tidak relevan di futures.
- Funding rate settlement (future/funding.py) belum disambungkan ke loop
  manapun di sini -- perhitungan tersedia tapi belum ada loop periodik yang
  memanggilnya untuk update realized_funding di Trade/Position.
- api_server_future.py belum dibangun (item terakhir roadmap) -- main()
  di file ini sudah siap memakainya (via ImportError fallback graceful),
  tinggal dibangun.

✅ SUDAH DIKERJAKAN & DIVERIFIKASI (update dari draft sebelumnya):
- run_position_sync_loop(): position_sync_futures.py sudah dibangun &
  diverifikasi end-to-end (deteksi posisi orphan via fetch_positions(),
  adopt dengan side/leverage/margin_mode/liquidation_price yang benar).
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
from engine.exchange_base import ReduceOnlyRejected
from engine.profiles.registry import get_coin_profile
from engine import reentry_cooldown
from engine.database import DatabaseManager
from engine.event_bus import EventBus, ThrottledTickerPublisher, KeyedLogThrottle
from future.exchange_future import FutureExchangeConnector
from spot.exchange_spot import WebSocketFeed  # [CATATAN] WebSocketFeed belum
    # diekstrak ke engine/ (lihat engine/execution_base.py) -- market-agnostic
    # secara konsep (streaming ticker/orderbook), reuse langsung dari spot/
    # untuk saat ini, bukan duplikasi baru.
from future.strategy_future import get_strategy
from engine.strategy_base import PositionTracker
from engine.core.models import SignalType, SignalEvent, ExitMode
from future.risk_future import RiskManager
from engine.risk_base import RiskAssessment, RiskDecision, HaltReason
from future.execution_future import OrderExecutionManager
from future import capital_allocator
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
        # [AUDIT ITEM #8 -- push/SSE] Selalu diinstansiasi (bukan Optional
        # spt DatabaseManager.event_bus) -- bus kosong (0 subscriber) genuinely
        # no-op murah. Instance IN-PROCESS milik proses futures ini saja --
        # TIDAK dishare dgn proses spot (2 port terpisah, 2 instance
        # terpisah, dikonfirmasi user).
        self.event_bus = EventBus()
        # [#23 -- audit fungsional] Rate-limit log INFO Gate4 ([ScoreThreshold])
        # per (simbol, side) -- lihat komentar "gate4_score_reject_log_interval"
        # di _load_config() utk latar belakang lengkap. Key tuple (symbol, side)
        # -- BEDA dari spot yang cuma symbol -- krn futures bisa evaluasi long
        # & short utk simbol yang sama dlm satu siklus (pola sama dgn
        # self._invalidation_signals.get((symbol, cand_side)) di bawah), kalau
        # di-key symbol saja reject long akan diam-diam menahan visibilitas
        # reject short (silent cross-side contamination, kelas bug sama persis
        # dgn item #25 _SIGNAL_CONFIRM_BUFFER).
        self._gate4_reject_log_throttle = KeyedLogThrottle(
            self.config["gate4_score_reject_log_interval"]
        )
        self._tasks:              List[asyncio.Task] = []
        self._daily_summary_sent: bool               = False
        self._closing_lock:    asyncio.Lock = asyncio.Lock()
        self._equity_lock:     asyncio.Lock = asyncio.Lock()
        self._closing_symbols: Set[str]     = set()
        self._close_retry_count: Dict[str, int] = {}
        self._last_refresh_time: float = 0.0
        self._whale_detectors:     Dict[str, WhaleDetector] = {}
        # [ITEM #4 -- audit fungsional] Debounce phantom-position detection --
        # symbol -> jumlah siklus run_position_sync() berturut-turut simbol
        # itu terdeteksi is_open=True di DB tapi absen di exchange. Lihat
        # future/position_sync_futures.py::_process_phantom_candidates().
        self._phantom_suspects: Dict[str, int] = {}
        # [#36 -- audit fungsional] Debounce amount-mismatch detection --
        # TERPISAH dari _phantom_suspects (symbol bisa punya kedua masalah
        # independen). Lihat future/position_sync_futures.py::
        # _process_amount_mismatch_candidates().
        self._amount_mismatch_suspects: Dict[str, int] = {}

        self._pipeline_active:      Set[str]        = set()
        self._queued_symbols:       Set[str]        = set()
        # [BIAS-FIX -- whale invalidation per-side] Sebelumnya Dict[str, Dict]
        # (key symbol saja) -- whale_sell_genuine (tekanan jual) memblokir
        # SELURUH simbol dari Gate3, termasuk kandidat SHORT yang justru
        # seharusnya DIKONFIRMASI oleh tekanan jual itu. Sekarang key
        # (symbol, side) -- invalidasi long dan short independen.
        self._invalidation_signals: Dict[Tuple[str, str], Dict] = {}
        self._gate3_queue:          asyncio.Queue   = asyncio.Queue()
        self._volume_ma:            Dict[str, float] = {}
        self._price_buffer:         Dict[str, list]  = {}
        self._last_candle_ts:       Dict[tuple, int]  = {}
        # [CAPITAL-ALLOCATOR] Registry kandidat tertunda krn kehabisan
        # kapasitas modal (slot/margin), di-key per symbol -- lihat
        # future/capital_allocator.py utk desain lengkap.
        self._pending_candidates:   Dict[str, capital_allocator.PendingCandidate] = {}
        self._reconcile_lock:       asyncio.Lock = asyncio.Lock()

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
            # [FUTURES -- audit item #18] Key TERPISAH dari spot
            # (META_LEARNER_ENABLED_FUTURES dkk, bukan META_LEARNER_ENABLED
            # polos) -- konsisten pola DASHBOARD_API_KEY_FUTURES/
            # ALLOWED_ORIGINS_FUTURES/auto_scan_universe_futures, supaya
            # meta-learner futures bisa di-toggle independen dari spot.
            # Default SAMA persis dgn spot (enabled=false, mode=advisory) --
            # wiring ini TIDAK mengaktifkan apa pun secara diam-diam.
            "meta_learner_enabled":       os.getenv("META_LEARNER_ENABLED_FUTURES", "false").lower() == "true",
            "meta_learner_mode":          os.getenv("META_LEARNER_MODE_FUTURES", "advisory"),
            "meta_learner_min_sample":    int(os.getenv("META_LEARNER_MIN_SAMPLE_FUTURES", "50")),
            "meta_learner_max_change":    int(os.getenv("META_LEARNER_MAX_THRESHOLD_CHANGE_FUTURES", "10")),
            # [ITEM #6] Interval refresh cache market ccxt (self.exchange._markets)
            # via reload_markets() -- sebelumnya cuma di-set sekali di connect().
            "market_cache_refresh_interval": int(os.getenv("MARKET_CACHE_REFRESH_INTERVAL", "3600")),
            # [#23 -- audit fungsional] Interval rate-limit (detik) utk log
            # INFO Gate4 ([ScoreThreshold]) per (simbol, side) -- Gate4 adalah
            # titik reject volume tertinggi di pipeline, jadi TIDAK di-bump ke
            # INFO polos spt Gate4.5/5 (risiko banjir log). log.debug() detail
            # penuh TETAP ada tanpa berubah; log.info() throttled ini TAMBAHAN
            # supaya tetap terlihat tanpa DEBUG logging, maks 1x per (simbol,
            # side) per interval ini. Key SAMA persis dgn spot (tidak pakai
            # suffix _FUTURES) -- ini murni knob observability, bukan feature
            # toggle yang perlu independen per bot (pola sama dgn
            # market_cache_refresh_interval di atas, BUKAN meta_learner_enabled
            # yang genuinely butuh toggle terpisah).
            "gate4_score_reject_log_interval": int(os.getenv("GATE4_SCORE_REJECT_LOG_INTERVAL", "600")),
            # [FUTURES-SPECIFIC] Parameter yang tidak ada sama sekali di spot.
            "default_leverage":            int(os.getenv("DEFAULT_LEVERAGE", "10")),
            "max_leverage":                int(os.getenv("MAX_LEVERAGE", "20")),
            "adaptive_leverage_enabled":    os.getenv("ADAPTIVE_LEVERAGE_ENABLED", "true").lower() == "true",
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
        # [AUDIT ITEM #8] Injeksi pasca-konstruksi (pola sama dgn injeksi
        # dependency lain di file ini) -- Tier 1 write sekarang publish ke
        # event_bus ini.
        self.db.event_bus = self.event_bus

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

        # ── Auto-scan universe dari Binance FUTURES ──
        # [BARU] Sebelumnya TIDAK ADA sama sekali -- ditemukan sebagai gap
        # saat dibandingkan dengan spot/main_spot.py yang punya mekanisme
        # ini (baris 349-354 di sana). Pola identik: cek flag DB
        # 'auto_scan_universe_futures', kalau true baru scan Binance
        # FUTURES (bukan spot), simpan ke universe_futures.json.
        from future.exchange_future import auto_scan_and_populate_futures
        # [FIX -- insiden EVAA/USDT 13 Juli 2026, rencana perbaikan #1 dari 3]
        # Refresh cache market ccxt SEBELUM scan+validasi di bawah -- cache
        # lama (dari load_markets() di connect(), baris ~293) bisa ketinggalan
        # kalau ada simbol baru listing setelah itu. scan_binance_futures_
        # universe() sendiri hit REST mentah (independen dari cache ini), tapi
        # is_symbol_supported() di bawah baca cache ccxt -- reload_markets()
        # memastikan validasi itu pakai data paling baru yang tersedia.
        await self.exchange.reload_markets()
        # [FIX] Validasi simbol hasil scan terhadap ccxt SEBELUM ditulis ke
        # universe_futures.json/universe_overrides -- scan_binance_futures_
        # universe() hit REST mentah, independen dari ccxt yang benar-benar
        # dipakai bot utk OHLCV/ticker/order. Pakai is_symbol_supported()
        # (resolusi pintar ex.market()), BUKAN get_market_info() (raw dict
        # lookup) -- terbukti lewat pengujian get_market_info salah di kedua
        # arah (reject simbol valid spt EVAA/USDT, terima simbol invalid spt
        # BONK/USDT yg cuma ada di spot).
        scanned = await auto_scan_and_populate_futures(
            self.db,
            is_valid_symbol=self.exchange.is_symbol_supported,
        )
        if scanned:
            self.config["universe_watchlist"] = scanned
            log.info("universe_watchlist (futures) diupdate dari auto_scan: %d koin", len(scanned))

        self.ws_feed = WebSocketFeed(
            exchange_id=self.config["exchange_id"],
            api_key=self.config["api_key"],
            api_secret=self.config["api_secret"],
            api_passphrase=self.config.get("api_passphrase", ""),
            symbols=self.config["universe_watchlist"],
            testnet=self.config["testnet"],
            # [FIX] WebSocketFeed di-reuse dari spot/ (lihat catatan impor di
            # atas) yang defaultnya defaultType="spot" -- tanpa ini, feed
            # ticker/orderbook futures diam-diam query market SPOT (banyak
            # simbol kebetulan match nama, memberi kesan "jalan" padahal data
            # salah pasar), dan simbol futures-only (mis. EVAA/USDT, tidak
            # ada listing spot) gagal total dgn BadSymbol.
            default_type="future",
            # [AUDIT ITEM #8] Hook on_ticker SUDAH ADA sejak awal di
            # WebSocketFeed, TIDAK PERNAH disambungkan ke apa pun sebelum
            # ini. Throttled per symbol -- lihat
            # engine/event_bus.py::ThrottledTickerPublisher.
            on_ticker=ThrottledTickerPublisher(self.event_bus, market_type="futures"),
        )
        await self.ws_feed.start()
        log.info("WebSocketFeed subscribe ke %d koin universe futures", len(self.config["universe_watchlist"]))

        # [BUG-FIX] Urutan sebelumnya SALAH: _initialize_intelligence_pipeline()
        # dipanggil SEBELUM risk_manager dibuat, padahal pipeline butuh
        # inject_dependencies(risk_manager=...) ke commander -- risk_manager
        # masih None saat itu terjadi. Urutan benar (sama seperti spot):
        # risk_manager -> executor -> intelligence pipeline.
        self.risk_manager = RiskManager(config=self.config, db=self.db)

        self.executor = OrderExecutionManager(
            exchange=self.exchange, db=self.db,
            on_trade_executed=self._on_trade_executed,
            max_slippage_pct=self.config["max_slippage_pct"],
            ws_feed=self.ws_feed,
        )

        await self._initialize_intelligence_pipeline()

        # [ITEM #15 -- Temuan B, Opsi B2] INVARIAN WAJIB, TITIK BERPASANGAN
        # dgn komentar di run() (baris pembuatan self._tasks) -- pola SAMA
        # PERSIS dgn spot::start()/_reconcile_positions_on_startup() (item
        # #27 audit fungsional). Titik ini HARUS dieksekusi SEBELUM task
        # periodik manapun dibuat (asyncio.create_task(...) di run(), SEMUA
        # SETELAH start() ini selesai) -- _reconcile_phantom_positions_on_
        # startup() auto-close phantom TANPA debounce, aman HANYA karena
        # nol aktivitas trading konkuren berjalan pada titik ini. JANGAN
        # pindahkan pemanggilan ini ke setelah task periodik dibuat tanpa
        # meninjau ulang invarian ini.
        assert not self._tasks, (
            "_reconcile_phantom_positions_on_startup() HARUS dipanggil sebelum "
            "task periodik dibuat (self._tasks masih kosong) -- lihat komentar "
            "[#15 Temuan B] di sini & di run(). Kalau ini gagal, urutan startup "
            "sudah berubah & asumsi keamanan auto-close-tanpa-debounce perlu "
            "ditinjau ulang."
        )
        await self._reconcile_phantom_positions_on_startup()

        self.is_running = True
        self.start_time = _utcnow_dt()
        log.info("Bot futures started — leverage default=%dx margin_mode=%s",
                  self.config["default_leverage"], self.config["margin_mode"])

    async def _reconcile_phantom_positions_on_startup(self) -> None:
        """
        [ITEM #15 -- Temuan B, Opsi B2 MINIMAL] Futures sebelumnya TIDAK
        PUNYA padanan spot::_reconcile_positions_on_startup() sama sekali
        (dikonfirmasi grep -- nol referensi). Gap ini ditemukan investigasi
        item #15 Tahap 0: kombinasi "is_closing stuck forever setelah retry
        exhausted" (Temuan A, SUDAH diperbaiki di close_position_with_retry())
        + "futures nol mekanisme reconcile startup" berarti SEBELUM fix ini,
        phantom position futures TIDAK PERNAH self-heal bahkan lewat restart
        (beda dari spot, yang punya jaring pengaman ini).

        SENGAJA MINIMAL (keputusan eksplisit pemilik proyek) -- HANYA
        menangani `phantom_candidates` (posisi DB is_open=True TAPI TIDAK
        ADA di exchange), auto-close TANPA debounce. Aman dengan alasan
        IDENTIK spot: dipanggil SEBELUM task periodik manapun dibuat (lihat
        assertion di start()), jadi race yang jadi alasan debounce di jalur
        periodik (posisi genuinely sedang closing normal, belum sempat
        is_closing=True) SECARA STRUKTURAL tidak bisa terjadi di sini.

        `untracked` & `amount_mismatches` (juga dikembalikan
        find_untracked_positions() yang sama) SENGAJA DIABAIKAN di sini --
        di luar scope Opsi B2. Tetap ditangani lewat jalur periodik existing
        (item #10/#36) dengan lag beberapa menit yang bisa diterima.

        Reuse find_untracked_positions() ASLI (future/position_sync_futures.py,
        BUKAN reimplementasi) -- fungsi ini genuinely bekerja baik utk paper
        (_paper_positions via fetch_positions() yang di-override
        FutureExchangeConnector) maupun live (fetch_positions() ccxt asli).

        [PENTING -- penanganan error fetch, DIKOREKSI dari asumsi awal
        setelah baca ulang kode find_untracked_positions() persis]
        fetch_binance_futures_positions() SENGAJA RAISE kalau fetch gagal
        (item #4 lama). TAPI find_untracked_positions() SENDIRI (pembungkus
        yang dipanggil di sini) MENANGKAP exception itu secara internal dan
        mengembalikan dict fail-safe `{"phantom_candidates": [], ...,
        "fetch_failed": True}` -- BUKAN meneruskan raise ke pemanggil. Jadi
        penanganan yang benar di sini adalah CEK FLAG `fetch_failed`, bukan
        try/except semata (try/except di bawah tetap dipertahankan sbg
        backstop tambahan utk exception TAK TERDUGA lain, mis.
        db_manager.get_open_positions() di dalam find_untracked_positions()
        sendiri TIDAK dibungkus try/except & BISA raise kalau DB genuinely
        error). Kedua jalur (flag True ATAU exception) sama-sama harus:
        log warning + skip reconciliation kali ini + LANJUT start() (BUKAN
        crash total) -- exchange API down/DB error saat startup tidak boleh
        mencegah bot hidup sama sekali.
        """
        from future.position_sync_futures import find_untracked_positions

        # [HYDRATION FIX] Isi ulang paper state dari DB SEBELUM reconciliation
        # -- tanpa ini reconciliation menghapus semua posisi paper tiap restart
        # (insiden WIF/AIGENSYN 2026-07-20 21:48).
        try:
            _open_for_hydrate = await self.db.get_open_positions()
            _n_hydrated = self.exchange.hydrate_from_positions(_open_for_hydrate)
            if _n_hydrated:
                log.info("Paper hydration (futures): %d posisi direkonstruksi dari DB.", _n_hydrated)
        except Exception as _hy_err:
            log.error("Paper hydration (futures) gagal (reconciliation bisa salah menutup posisi!): %s", _hy_err)

        log.info("Reconciliation (futures): cek phantom position DB vs exchange...")

        try:
            result = await find_untracked_positions(self.exchange, self.db)
        except Exception as e:
            log.warning(
                "Reconciliation (futures) startup: find_untracked_positions() "
                "error tak terduga — skip reconciliation kali ini, lanjut "
                "startup normal: %s", e,
            )
            return

        if result.get("fetch_failed"):
            log.warning(
                "Reconciliation (futures) startup: fetch posisi exchange gagal "
                "— skip reconciliation kali ini, lanjut startup normal."
            )
            return

        phantom_candidates = result["phantom_candidates"]
        if not phantom_candidates:
            log.info("Reconciliation (futures): tidak ada phantom position di startup.")
            return

        for symbol in phantom_candidates:
            try:
                pos = await self.db.get_open_position_by_symbol(symbol)
                if not pos:
                    continue  # sudah closed lewat jalur lain di antara fetch & titik ini

                exit_price = float(pos.current_price or pos.entry_price or 0)
                try:
                    mark_price = await self.exchange.fetch_mark_price(symbol)
                    if mark_price and mark_price > 0:
                        exit_price = float(mark_price)
                except Exception:
                    pass

                realized_pnl = 0.0
                if pos.entry_price and pos.amount:
                    if pos.side == "long":
                        realized_pnl = (exit_price - pos.entry_price) * pos.amount
                    else:
                        realized_pnl = (pos.entry_price - exit_price) * pos.amount

                log.warning(
                    "Reconciliation (futures): %s TIDAK ada di exchange — menutup "
                    "di DB (startup, auto-close TANPA debounce, aman krn nol "
                    "aktivitas trading konkuren pada titik ini). Est PnL=%+.4f",
                    symbol, realized_pnl,
                )
                await self.db.close_position(symbol, exit_price, realized_pnl)
                await self.db.save_log(
                    "WARNING", "reconcile_futures",
                    f"Posisi {symbol} di-close via reconciliation startup (tidak "
                    f"ada di exchange). Est PnL={realized_pnl:+.4f}",
                )
                if self.notifier:
                    try:
                        await self.notifier.notify_trade_closed(
                            symbol=symbol, side=pos.side,
                            entry_price=float(pos.entry_price or 0),
                            exit_price=exit_price,
                            amount=float(pos.amount or 0),
                            realized_pnl=realized_pnl,
                            reason="Reconciliation startup (futures) — posisi hilang di exchange",
                        )
                    except Exception:
                        pass
            except Exception as e:
                log.error("Reconciliation (futures) error untuk %s: %s", symbol, e)

    async def _initialize_intelligence_pipeline(self) -> None:
        """
        [BUG-FIX] Sebelumnya memanggil get_strategy(name=, config=, db=,
        ws_feed=, notifier=) -- signature yang SAMA SEKALI TIDAK ADA di
        get_strategy() manapun (spot maupun future). get_strategy() yang
        benar cuma terima (name, symbols, timeframe, params) -- db/ws_feed/
        notifier di-INJECT sebagai atribut SETELAH konstruksi (pola yang
        sama dgn spot/main_spot.py). Bug ini akan langsung TypeError kalau
        bot betulan dijalankan -- ditemukan & diperbaiki saat verifikasi
        independensi future/ dari spot/ (baca future/strategy_future.py
        untuk konteks lengkap kenapa ekstraksi ini dilakukan).
        """
        if not self.config.get("intelligence_enabled", True):
            return
        try:
            self.strategy = get_strategy(
                name=self.config["strategy"],
                symbols=self.config["universe_watchlist"],
                timeframe=self.config["timeframe"],
                params={
                    "atr_sl_mult":             self.config["atr_multiplier_sl"],
                    "atr_tp_mult":             self.config["atr_multiplier_tp"],
                    "sentiment_enabled":       self.config["sentiment_enabled"],
                    "volume_multiplier":       self.config.get("volume_multiplier", 1.3),
                    "volume_spike_threshold":  self.config.get("volume_spike_threshold", 3.0),
                    "rsi_min":                 self.config.get("rsi_min", 45),
                    "rsi_max":                 self.config.get("rsi_max", 77),
                    "rsi_golden_cross_min":    self.config.get("rsi_golden_cross_min", 45),
                    "atr_pct_threshold":       self.config.get("atr_pct_threshold", 0.8),
                },
            )
            # Inject dependency SETELAH konstruksi -- pola yang sama persis
            # dengan spot/main_spot.py.
            if hasattr(self.strategy, "_notifier"):
                self.strategy._notifier = self.notifier
            if hasattr(self.strategy, "_db"):
                self.strategy._db = self.db
            if hasattr(self.strategy, "_scorer") and self.strategy._scorer is not None:
                self.strategy._scorer._db = self.db
            if hasattr(self.strategy, "_ws_feed"):
                self.strategy._ws_feed = self.ws_feed
            if hasattr(self.strategy, "_validator") and self.strategy._validator is not None:
                self.strategy._validator._db = self.db

            if hasattr(self.strategy, "refresh_profiles"):
                self.strategy.refresh_profiles()
            # [BUG-FIX] Sebelumnya IntelligenceCommander(config=self.config)
            # -- db parameter (WAJIB, positional/keyword pertama) TIDAK
            # diteruskan sama sekali, DAN inject_dependencies() (yang
            # menyuntikkan exchange_connector & risk_manager, dipakai di
            # dalam decide() utk cek spread/korelasi) tidak pernah dipanggil.
            # Bug ini akan TypeError langsung kalau bot dijalankan --
            # ditemukan & diperbaiki saat verifikasi independensi future/.
            from engine.intelligence.commander import IntelligenceCommander
            self._commander = IntelligenceCommander(db=self.db, config=self.config)
            self._commander.inject_dependencies(
                exchange_connector=self.ws_feed,
                risk_manager=self.risk_manager,
            )
            log.info("Intelligence pipeline (futures) siap.")
        except Exception as e:
            log.error("Intelligence pipeline init gagal: %s", e, exc_info=True)

        # [FUTURES -- audit item #18] Sebelumnya self._analytics/
        # self._meta_learner SELALU None -- tidak pernah diinstansiasi di
        # mana pun (dikonfirmasi grep). Wiring ini mirror pola
        # spot/main_spot.py::_initialize_intelligence_pipeline() persis,
        # DENGAN 1 penyesuaian krusial: MetaLearner(..., market_type=
        # "futures") -- lihat guard di engine/learning/meta_learner.py
        # _apply_suggestion() yang memblokir suggestion tipe weight_*
        # secara teknis (bukan cuma dokumentasi), karena weights.py adalah
        # file shared dgn spot. Suggestion tipe threshold tetap jalan
        # normal (per-bot, aman). Data source (get_trades_with_regime,
        # get_score_vs_outcome, dst di engine/database.py) genuinely
        # generik/market-agnostic -- diverifikasi nihil filter side/market
        # apa pun, aman dipakai apa adanya untuk futures.
        if self.config.get("analytics_enabled", True):
            try:
                from engine.learning.analytics import PerformanceAnalytics
                self._analytics = PerformanceAnalytics(db=self.db, config=self.config)

                await self._analytics.load_persistent_parameters()
                log.info("Performance analytics (futures): AKTIF")
            except ImportError:
                log.info("learning/analytics.py belum tersedia — analytics di-skip.")
            except Exception as e:
                log.warning("Analytics init gagal (futures): %s — analytics di-skip.", e)

        if self.config.get("meta_learner_enabled", False) and self._analytics:
            try:
                from engine.learning.meta_learner import MetaLearner
                self._meta_learner = MetaLearner(
                    db_manager=self.db,
                    analytics_engine=self._analytics,
                    mode=self.config.get("meta_learner_mode", "advisory"),
                    min_sample=int(self.config.get("meta_learner_min_sample", 50)),
                    max_threshold_change=float(self.config.get("meta_learner_max_change", 10)),
                    market_type="futures",
                )
                mode = self.config.get("meta_learner_mode", "advisory")
                log.info("Meta-learner (futures): AKTIF (mode=%s, weight-suggestion diblokir)", mode)
                await self._meta_learner.initialize()
                await self.db.save_log(
                    "INFO", "main_future",
                    f"Meta-learner (futures) aktif | mode={mode} | "
                    f"min_sample={self.config['meta_learner_min_sample']} | "
                    f"weight-suggestion diblokir (shared weights.py guard)",
                )
            except ImportError:
                log.info("learning/meta_learner.py belum tersedia — meta-learner di-skip.")
            except Exception as e:
                log.warning("Meta-learner init gagal (futures): %s — meta-learner di-skip.", e)
        elif self.config.get("meta_learner_enabled", False):
            log.warning(
                "META_LEARNER_ENABLED_FUTURES=true tapi analytics tidak aktif — "
                "meta-learner membutuhkan analytics. Meta-learner di-skip."
            )

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

        # [RE-ENTRY COOLDOWN -- opsi B, kejadian PARTI/MANTRA 2026-07-20]
        # Ditolak DI SINI (funnel otoritatif, menutup jalur run_gate3_worker
        # MAUPUN capital_allocator.reconcile_pending) kalau simbol baru saja
        # exit negatif (SL/ATG/loss) dan masih dlm masa tunggu 1x timeframe
        # profile-nya. Exit positif (TP/trailing profit) TIDAK memblokir.
        _cd_rem = reentry_cooldown.registry.blocked(symbol)
        if _cd_rem > 0:
            log.info(
                "Entry %s (%s) ditolak: re-entry cooldown %.0fs tersisa (pasca-exit negatif).",
                symbol, side, _cd_rem,
            )
            _reset_position_flag()
            return

        # [BARU] Leverage ADAPTIF -- sebelumnya SELALU flat dari config,
        # sekarang menyesuaikan volatilitas (ATR%), regime, profile koin,
        # dan skor confidence sinyal. Bisa dimatikan via ADAPTIVE_LEVERAGE_ENABLED
        # (fallback ke nilai flat config["default_leverage"] apa adanya).
        base_leverage = self.config.get("default_leverage", 10)
        if self.config.get("adaptive_leverage_enabled", True):
            _atr_pct = (atr / price * 100) if (atr and price > 0) else None
            _profile_name = signal.metadata.get("coin_profile") if signal.metadata else None
            leverage = self.risk_manager.compute_adaptive_leverage(
                base_leverage=base_leverage, atr_pct=_atr_pct,
                regime=str(signal.regime) if signal.regime else None,
                profile_name=str(_profile_name) if _profile_name else None,
                score=signal.total_score,
            )
            if leverage != base_leverage:
                log.info(
                    "Leverage adaptif %s: %dx -> %dx (atr_pct=%s regime=%s profile=%s score=%s)",
                    symbol, base_leverage, leverage, _atr_pct, signal.regime, _profile_name, signal.total_score,
                )
        else:
            leverage = base_leverage
        margin_mode = self.config.get("margin_mode", "isolated")

        # [FUTURES-SPECIFIC -- BARU] set_leverage() SEBELUMNYA TIDAK PERNAH
        # dipanggil sama sekali -- untuk paper trading tidak berdampak
        # (simulasi internal pakai leverage yg kita tentukan sendiri), TAPI
        # untuk exchange ASLI ini krusial: Binance memakai leverage yang
        # TERAKHIR di-set untuk symbol itu (bisa jadi beda dari
        # config["default_leverage"] kalau pernah diubah manual/sesi lain).
        # Tanpa memanggil ini, kalkulasi margin/liquidation kita bisa TIDAK
        # SINKRON dengan leverage yang benar-benar dipakai exchange.
        try:
            await self.exchange.set_leverage(symbol, leverage)
        except Exception as e:
            log.error(
                "set_leverage(%s, %dx) gagal — entry %s DIBATALKAN (leverage "
                "exchange tidak terkonfirmasi, tidak aman melanjutkan): %s",
                symbol, leverage, side, e,
            )
            _reset_position_flag()
            return

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
            # [CAPITAL-ALLOCATOR] Real risk-check (ukuran Kelly sungguhan,
            # bukan probe 1% di commander G4) gagal krn kapasitas -- ini
            # checkpoint KEDUA registrasi (checkpoint pertama di
            # run_gate3_worker menangani kasus gagal di G4 probe SEBELUM
            # sampai ke sini sama sekali). Registrasi di sini otoritatif
            # krn pakai ukuran order yang benar2 akan dieksekusi.
            if assessment.decision == RiskDecision.REJECTED_INSUFFICIENT_CAPITAL:
                # [CATATAN] candle_ts bisa None kalau signal ini berasal
                # dari capital_allocator.reconcile_pending() (bukan siklus
                # gate3 normal) -- AMAN krn simbol ini pasti SUDAH ada di
                # registry dgn baseline asli (reconcile cuma re-attempt
                # kandidat existing), jadi register_or_refresh() lewat
                # jalur REFRESH (side sama) yang mengabaikan candle_ts
                # baru ini sama sekali, bukan jalur REPLACE.
                _default_tf = self.config.get("timeframe", "15m")
                capital_allocator.register_or_refresh(
                    self._pending_candidates, symbol, side,
                    profile_name=str(signal.metadata.get("coin_profile", "universal")) if signal.metadata else "universal",
                    profile_timeframe=str(signal.metadata.get("profile_timeframe") or _default_tf) if signal.metadata else _default_tf,
                    candle_ts=int(signal.metadata.get("candle_ts") or 0) if signal.metadata else 0,
                    price=price, atr=float(atr or 0), score=float(signal.total_score or 0),
                    reason=assessment.reason,
                )
            log.info("Risk REJECTED %s (%s): %s", symbol, side, assessment.reason)
            _reset_position_flag()
            return

        _slot_reserved_pending_release = True
        try:
            _kelly_size_pct = signal.metadata.get("kelly_size_pct") if signal.metadata else None
            if _kelly_size_pct and _kelly_size_pct > 0 and assessment.approved_size and price > 0:
                _kelly_max_qty = (equity * _kelly_size_pct / 100) / price
                if _kelly_max_qty < assessment.approved_size:
                    assessment.approved_size = _kelly_max_qty

            async with self._equity_lock:
                # [ITEM #1 -- audit fungsional] Re-check DB TEPAT SEBELUM
                # execute_signal() -- BUKAN sebelum upsert_position() di bawah,
                # itu sudah terlambat krn order NYATA sudah tereksekusi di
                # exchange pada titik itu. Di dalam _equity_lock yang SAMA
                # yang sudah dipegang KEDUA jalur entry (run_gate3_worker()
                # via _process_one(), MAUPUN capital_allocator.reconcile_
                # pending() via _rescore_candidate() -> _handle_entry() ini
                # juga) -- keduanya mengambil snapshot db.get_open_positions()
                # SENDIRI-SENDIRI sebelum G0_ALREADY_OPEN (commander.py), jadi
                # bisa sama-sama lolos utk simbol yang sama sebelum salah satu
                # commit. Re-check di sini menutup race itu krn kedua jalur
                # funnel ke _handle_entry() yang sama, jadi ke _equity_lock
                # yang sama juga -- tidak perlu lock baru/koordinasi eksplisit
                # antara run_gate3_worker & capital_allocator.
                # [SINERGI ITEM #2] Abort di sini terjadi SEBELUM
                # _slot_reserved_pending_release di-set False (baris di bawah)
                # -- reservasi _open_positions_count yang sudah dibuat
                # evaluate_order() (Opsi 1 item #2) otomatis DILEPAS lewat
                # finally yang sudah ada, tanpa kode tambahan.
                _existing_now = await self.db.get_open_position_by_symbol(symbol)
                if _existing_now is not None:
                    log.warning(
                        "Entry %s %s dibatalkan -- posisi SUDAH terbuka "
                        "(terdeteksi tepat sebelum execute_signal(); race dua "
                        "jalur entry run_gate3_worker/capital_allocator "
                        "berhasil dicegah).",
                        side, symbol,
                    )
                    _reset_position_flag()
                    return

                trade = await self.executor.execute_signal(signal, assessment)
                if trade is None:
                    log.error("EXECUTE %s GAGAL untuk %s — posisi tidak dibuka.", side, symbol)
                    _reset_position_flag()
                    return

                _slot_reserved_pending_release = False  # posisi genuinely terbuka di exchange

                # [BUG-FIX] Sebelumnya pakai assessment.liquidation_price (dihitung
                # dari SIGNAL price, sebelum slippage) -- sedikit beda dari
                # liquidation_price yang exchange hitung sendiri (dari EXECUTED
                # price, sesudah slippage). Recompute di sini pakai executed_price
                # aktual supaya DB record konsisten PERSIS dengan tracking internal
                # exchange (penting utk akurasi monitoring liquidation proximity).
                from future.liquidation import calculate_liquidation_price
                _final_leverage = assessment.leverage or leverage
                try:
                    _liq_recompute = calculate_liquidation_price(
                        entry_price=trade.executed_price, leverage=_final_leverage,
                        side=side, mmr=self.config.get("maintenance_margin_rate", 0.005),
                        margin_mode=margin_mode,
                    ).liquidation_price
                except Exception:
                    _liq_recompute = assessment.liquidation_price  # fallback ke estimasi kalau recompute gagal

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
                        "leverage": _final_leverage,
                        "margin_mode": assessment.margin_mode,
                        "liquidation_price": _liq_recompute,
                        "mark_price_at_entry": trade.executed_price,
                    })
                except Exception as e:
                    log.critical(
                        "upsert_position GAGAL untuk %s setelah order berhasil — "
                        "posisi TIDAK tertracking di DB! %s", symbol, e,
                    )
                    await self.db.save_log("CRITICAL", "main_future",
                                            f"upsert_position gagal {symbol} — trailing/liquidation monitor tidak aktif!")
        finally:
            if _slot_reserved_pending_release:
                self.risk_manager.release_position_slot()
                log.warning(
                    "[Opsi 1 -- audit item #2] Slot open_positions dilepas -- entry %s %s "
                    "gagal setelah disetujui risk_manager (execute_signal tidak menghasilkan "
                    "trade -- lihat log error di atas).",
                    side, symbol,
                )

        # [Opsi 2 -- audit item #2] Refresh portfolio (fetch DB) SEGERA
        # setelah entry sukses -- mirror pola _do_close_position() (futures
        # sudah punya komentar eksplisit soal ini di sana). Mengecilkan
        # window race dari sampai 900 detik (SNAPSHOT_INTERVAL) menjadi
        # durasi satu _handle_entry() -- TIDAK menghilangkan race utk worker
        # lain yang genuinely konkuren dalam window itu (Opsi 1 di atas yang
        # menutup race secara menyeluruh).
        try:
            await self._refresh_portfolio()
        except Exception as _rp_err:
            log.warning("Refresh portfolio pasca-entry gagal untuk %s: %s", symbol, _rp_err)

        # [FEE-FUNDING FIX -- realized_pnl futures TIDAK PERNAH mengurangi fee
        # (terbukti: trade APE gross=0 tapi fee_cost 2x $0.02 -- net sebenarnya
        # -$0.04, bukan $0.00 spt tercatat). Simpan entry_fee_actual sekarang,
        # mirror pola spot/main_spot.py -- dipakai _do_close_position() nanti.]
        try:
            _entry_fee_actual = float(getattr(trade, "fee_cost", 0) or 0)
            if _entry_fee_actual > 0:
                await self.db.update_position_entry_fee(symbol, _entry_fee_actual)
        except Exception as _fee_err:
            log.warning("Gagal simpan entry fee futures %s: %s", symbol, _fee_err)

        log.info(
            "POSISI DIBUKA (futures): %s %s | entry=%.6f amount=%.8f SL=%s TP=%s "
            "leverage=%dx liq_price≈%s",
            side.upper(), symbol, trade.executed_price, trade.filled or trade.amount,
            assessment.stop_loss, assessment.take_profit,
            _final_leverage, _liq_recompute,
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

    async def _reconcile_pending_candidates(self) -> Dict:
        """[CAPITAL-ALLOCATOR] Wrapper tipis: pegang _reconcile_lock supaya
        dua trigger (close-event & polling fallback) yang hampir bersamaan
        tidak sama-sama re-score kandidat yang sama secara paralel (sia-sia,
        bukan soal keamanan data -- _handle_entry sudah aman thd double-entry
        lewat _existing_pos check & _equity_lock sendiri)."""
        async with self._reconcile_lock:
            try:
                return await capital_allocator.reconcile_pending(self)
            except Exception as e:
                log.error("[CapitalAllocator] reconcile_pending error: %s", e, exc_info=True)
                return {"purged": 0, "attempted": None, "opened": False}

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

    async def _close_position_market(
        self, pos, exit_price: float, reason: str, close_amount: Optional[float] = None,
    ) -> None:
        """
        [FUTURES-READY -- close_amount BARU] Kalau close_amount diberikan
        dan < pos.amount, ini PARTIAL close (scale-out) -- posisi TETAP
        terbuka dengan amount berkurang. Kalau None (default) atau >=
        pos.amount, tetap full close seperti sebelumnya (behavior TIDAK
        BERUBAH untuk semua caller existing yang tidak menyertakan
        parameter ini -- SL/TP/liquidation/panic semuanya tetap full close).
        """
        async with self._closing_lock:
            if pos.symbol in self._closing_symbols:
                return
            self._closing_symbols.add(pos.symbol)
        try:
            await self.db.mark_position_closing(pos.symbol)
        except Exception as e:
            log.warning("mark_position_closing gagal untuk %s: %s", pos.symbol, e)
        try:
            await self._do_close_position(pos, exit_price, reason, close_amount=close_amount)
        finally:
            async with self._closing_lock:
                self._closing_symbols.discard(pos.symbol)

    async def _verify_position_exists_at_exchange(self, symbol: str, side: str) -> bool:
        """
        [ITEM #15 -- Temuan C, Opsi C2 verify-before-send] Cek LANGSUNG ke
        exchange (paper ATAU live, transparan lewat fetch_positions() yang
        sudah di-override FutureExchangeConnector utk paper mode -- baca
        _paper_positions internal) apakah posisi `symbol` dengan `side`
        yang diminta MASIH genuinely ada. Dipanggil dari _do_close_position()
        SEBELUM mengirim order close apa pun.

        Root cause yang ditutup: _paper_positions/exchange asli bisa sudah
        genuinely flat (attempt close SEBELUMNYA sukses di exchange, tapi
        gagal tercatat di DB -- item #15 Temuan A) padahal DB masih percaya
        posisi terbuka. Order close BARU yang dikirim ke posisi yang sudah
        tidak ada disalahartikan sbg MEMBUKA posisi baru arah berlawanan
        (Temuan C) -- verify-before-send ini mencegah order itu dikirim
        SAMA SEKALI kalau memang tidak ada apa-apa lagi yang perlu ditutup.

        [Keputusan fail-safe -- PENTING] Kalau fetch GAGAL (network/rate-
        limit/dst) -- return True (anggap posisi MASIH ada), BUKAN False.
        Alasan: False akan membuat caller SKIP order close & langsung tulis
        DB "closed" TANPA order beneran terkirim -- kalau ternyata posisi
        ASLI masih ada (fetch cuma gagal network, bukan genuinely tidak
        ada), ini MENCIPTAKAN phantom arah sebaliknya (DB bilang closed
        padahal exchange masih punya posisi terbuka tak terkelola) --
        persis kebalikan dari yang mau dicegah. Fail ke True (proses close
        seperti biasa, order tetap dikirim) jauh lebih aman: worst-case
        cuma balik ke perilaku LAMA sebelum fix ini (Opsi C1 reduce-only,
        Tahap 2, tetap jadi backstop TOCTOU kalau di titik pengiriman order
        ternyata posisi genuinely sudah tidak ada).
        """
        try:
            positions = await self.exchange.fetch_positions([symbol])
        except Exception as e:
            log.warning(
                "_verify_position_exists_at_exchange(%s): fetch gagal, "
                "fail-safe anggap posisi MASIH ada (proses close spt biasa): %s",
                symbol, e,
            )
            return True

        for p in positions:
            if p.get("symbol") != symbol:
                continue
            amount = float(p.get("amount") or p.get("contracts") or 0)
            if amount <= 0:
                continue
            if p.get("side", "long") == side:
                return True
        return False

    async def _sync_db_close_without_order(
        self, pos, exit_price: float, reason: str, full_amount: float,
    ) -> None:
        """
        [ITEM #15 -- Temuan C, Opsi C2] Dipanggil HANYA dari _do_close_
        position() saat _verify_position_exists_at_exchange() memastikan
        exchange sudah genuinely flat -- selaraskan DB langsung TANPA
        mengirim order apa pun (tidak ada apa-apa lagi yang perlu ditutup
        di exchange). Pakai exit_price yang diteruskan caller ("harga
        terakhir yang diketahui", sesuai instruksi eksplisit -- BUKAN
        fetch ulang, krn posisi sudah tidak ada, tidak ada fill price baru
        yang genuinely relevan).

        Selalu close PENUH (bukan reduce_position_amount) -- kalau exchange
        menunjukkan NOL posisi utk symbol+side ini, tidak ada sisa yang bisa
        "dikurangi sebagian" secara logis, terlepas apakah caller aslinya
        minta partial atau full close.
        """
        entry_price = float(pos.entry_price or 0)
        if pos.side == "long":
            realized_pnl = (exit_price - entry_price) * full_amount
        else:
            realized_pnl = (entry_price - exit_price) * full_amount

        try:
            await self.db.close_position_with_retry(
                pos.symbol, exit_price=exit_price, realized_pnl=realized_pnl,
            )
            log.warning(
                "_do_close_position(%s): posisi TIDAK ditemukan di exchange "
                "(sudah closed duluan -- kemungkinan attempt close sebelumnya "
                "sukses di exchange tapi gagal tercatat DB, item #15). DB "
                "diselaraskan TANPA kirim order baru, exit_price=%.8f "
                "(harga terakhir diketahui) realized_pnl=%+.4f.",
                pos.symbol, exit_price, realized_pnl,
            )
            if self.notifier:
                try:
                    await self.notifier.notify_trade_closed(
                        symbol=pos.symbol, side=pos.side,
                        entry_price=entry_price, exit_price=exit_price,
                        amount=full_amount, realized_pnl=realized_pnl,
                        reason=f"{reason} (disinkronkan tanpa order — exchange "
                               f"sudah flat, item #15 Temuan C)",
                    )
                except Exception:
                    pass
        except Exception as e:
            msg = (
                f"close_position (DB) GAGAL untuk {pos.symbol} saat sinkronisasi "
                f"verify-before-send (exchange sudah genuinely flat, TAPI DB "
                f"tetap gagal ditulis setelah retry): {e}. Posisi is_open=True "
                f"akan nyangkut -- akan terdeteksi otomatis via phantom detector "
                f"(item #10, is_closing sudah direset via item #15 Temuan A)."
            )
            log.critical(msg)
            try:
                await self.db.save_log("CRITICAL", "main_future", msg)
            except Exception as _sl_err:
                log.error("save_log (verify-before-send sync gagal) gagal: %s", _sl_err)
            if self.notifier:
                try:
                    await self.notifier.notify_error("close_position_verify_sync_failed", msg)
                except Exception as _ne_err:
                    log.error("notify_error (verify-before-send sync gagal) gagal: %s", _ne_err)

    async def _do_close_position(
        self, pos, exit_price: float, reason: str, close_amount: Optional[float] = None,
    ) -> None:
        """
        [FUTURES-SPECIFIC] SignalType.CLOSE_LONG/CLOSE_SHORT sesuai pos.side
        (bukan SignalType.SELL hardcoded spt spot). evaluate_order dgn
        existing_position_side=pos.side supaya risk_future.py tau ini
        reduce/close (bypass sizing cap), bukan buka posisi baru.

        [BARU] close_amount opsional -- kalau diisi dan < pos.amount, ini
        PARTIAL close (posisi tetap terbuka dgn amount berkurang, pakai
        db.reduce_position_amount() bukan db.close_position()).
        """
        existing = await self.db.get_open_position_by_symbol(pos.symbol)
        if not existing:
            log.warning("Position %s sudah tidak open di DB — skip close (reason=%s)", pos.symbol, reason)
            return

        full_amount = float(pos.amount or 0)
        amount_to_close = float(close_amount) if close_amount is not None else full_amount
        if amount_to_close <= 0 or amount_to_close > full_amount + 1e-9:
            log.error(
                "close_amount tidak valid untuk %s: %.8f (posisi hanya %.8f) — dibatalkan.",
                pos.symbol, amount_to_close, full_amount,
            )
            return
        is_partial = amount_to_close < full_amount - 1e-9

        # [ITEM #15 -- Temuan C, Opsi C2] Verify-before-send: cek exchange
        # SEBELUM mengirim order close apa pun. Kalau sudah genuinely tidak
        # ada, selaraskan DB langsung & STOP di sini -- tidak lanjut ke
        # risk_manager.evaluate_order()/execute_signal() sama sekali.
        position_exists = await self._verify_position_exists_at_exchange(pos.symbol, pos.side)
        if not position_exists:
            await self._sync_db_close_without_order(pos, exit_price, reason, full_amount)
            return

        close_signal_type = SignalType.CLOSE_LONG if pos.side == "long" else SignalType.CLOSE_SHORT
        close_signal = SignalEvent(
            symbol=pos.symbol, signal_type=close_signal_type, price=exit_price,
            timestamp=_utcnow_dt(), strategy=pos.strategy_name or "risk_monitor",
            # [ITEM #15 -- Temuan C, Opsi C1] reduce_only=True -- backstop
            # TOCTOU thd Opsi C2 (verify-before-send di atas). Dibaca
            # engine/execution_base.py::_reduce_only_params() -> diteruskan
            # sbg params={"reduceOnly": True} ke create_order() (native utk
            # live) & reduce_only= ke _simulate_order_fill() (paper).
            metadata={"exit_reason": reason, "partial": is_partial, "reduce_only": True},
        )

        # order_side level-exchange: tutup long="sell", tutup short="buy"
        order_side = "sell" if pos.side == "long" else "buy"
        close_assessment_raw = await self.risk_manager.evaluate_order(
            symbol=pos.symbol, side=order_side, price=exit_price, quantity=amount_to_close,
            existing_position_side=pos.side,
        )
        close_assessment = RiskAssessment(
            decision=RiskDecision.APPROVED, reason=reason,
            approved_size=(close_assessment_raw.approved_size
                           if close_assessment_raw.is_approved and close_assessment_raw.approved_size
                           else amount_to_close),
            stop_loss=None, take_profit=None,
        )

        # [DOUBLE-COUNT FIX -- mirror spot/main_spot.py::_do_close_position,
        # kejadian nyata MANTRA/USDT 2026-07-20] Fill close mengkredit
        # margin_released + realized_pnl ke paper margin balance SEKETIKA di
        # dalam execute_signal() (exchange_future.py), jauh sebelum
        # close_position_with_retry() commit is_open=0. _refresh_portfolio()
        # futures (equity = free+used+unrealized_pnl posisi open DB) yang
        # menyelinap di celah itu menghitung PnL DOBEL -> peak_equity/drawdown
        # palsu. _equity_lock membungkus fill sampai commit DB (termasuk
        # handler ReduceOnlyRejected -> _sync_db_close_without_order, sudah
        # diverifikasi TIDAK menyentuh lock/refresh -- bebas deadlock TOWNS).
        # update_trade_pnl/notifikasi/_refresh_portfolio tetap DI LUAR lock.
        async with self._equity_lock:
            try:
                trade = await self.executor.execute_signal(close_signal, close_assessment)
            except ReduceOnlyRejected as e:
                # [ITEM #15 -- Temuan C, Opsi C1] Backstop TOCTOU: order ditolak
                # KARENA reduce-only, bukan kegagalan biasa -- sinyal PASTI
                # "sudah closed duluan" (posisi berubah tepat di celah antara
                # verify-before-send di atas & order ini benar-benar terkirim).
                # JANGAN retry order biasa (_close_retry_count dkk) -- langsung
                # sinkron DB via jalur yang SAMA dgn Opsi C2 (reuse, bukan
                # jalur baru), pakai exit_price yang sama (harga terakhir
                # diketahui, TIDAK ada fill price baru krn order ditolak).
                log.warning(
                    "_do_close_position(%s): order reduce-only DITOLAK exchange "
                    "(%s) -- celah TOCTOU sisa verify-before-send, posisi "
                    "berubah tepat sebelum order terkirim. Sinkron DB langsung, "
                    "TANPA retry order.", pos.symbol, e,
                )
                await self._sync_db_close_without_order(pos, exit_price, reason, full_amount)
                return

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
            # price (memperhitungkan side DAN amount_to_close, bukan selalu
            # full_amount) -- Trade.realized_pnl TIDAK otomatis terisi oleh
            # execute_signal()/_process_fill() (kolom itu ada di skema tapi
            # memang diisi terpisah, bukan saat insert trade).
            entry_price = float(pos.entry_price or 0)
            if pos.side == "long":
                gross_pnl = (trade.executed_price - entry_price) * amount_to_close
            else:
                gross_pnl = (entry_price - trade.executed_price) * amount_to_close

            # [FEE-FUNDING FIX] realized_pnl SEBELUMNYA = gross_pnl mentah,
            # tanpa fee (kolom fee_cost SUDAH benar dipotong ke saldo margin
            # via exchange connector, tapi tidak pernah dikurangi dari angka
            # yang disimpan ke DB -- 100% trade "impas" (SAHARA/W/APE) di
            # produksi ternyata LOSS tipis kalau dihitung benar). Mirror pola
            # spot: entry_fee dari kolom tersimpan (fallback estimasi taker
            # kalau kosong -- data lama/race), exit_fee dari fill riil.
            # Funding DITAMBAHKAN (bukan dikurangi) krn payment sudah bertanda
            # (+/-) sesuai arah bayar/terima -- lihat future/funding.py.
            _taker_fee_rate = self.exchange.get_taker_fee(pos.symbol)
            if getattr(pos, "entry_fee_actual", None) and pos.entry_fee_actual > 0:
                _entry_fee = float(pos.entry_fee_actual)
            else:
                _entry_fee = entry_price * amount_to_close * _taker_fee_rate
                log.warning(
                    "PnL calc futures %s: entry_fee_actual tidak ada -- fallback estimasi=%.8f",
                    pos.symbol, _entry_fee,
                )
            _exit_fee = float(
                trade.fee_cost
                if getattr(trade, "fee_cost", None) is not None and float(trade.fee_cost) > 0
                else trade.executed_price * amount_to_close * _taker_fee_rate
            )
            _funding_total = float(getattr(pos, "funding_paid_total", 0) or 0)
            realized_pnl = gross_pnl - _entry_fee - _exit_fee + _funding_total

            try:
                if is_partial:
                    # [#28 -- audit fungsional, pola sama persis dgn item #4]
                    # reduce_position_amount_with_retry() (bukan reduce_
                    # position_amount() polos) -- retry-backoff 3x utk kegagalan
                    # TRANSIEN (lock SQLite/hiccup koneksi). Order partial-close
                    # SUDAH sukses tereksekusi di exchange di titik ini (urutan
                    # tidak bisa dibalik) -- kalau SEMUA retry tetap gagal, DB
                    # nyangkut `amount` LAMA (terlalu besar) padahal exchange
                    # sudah lebih kecil, ditangani di except block di bawah
                    # dengan pesan yang BEDA dari full-close (lihat docstring
                    # reduce_position_amount_with_retry() di database.py --
                    # phantom detector run_position_sync_loop() TIDAK bisa
                    # mendeteksi mismatch amount ini, cuma mismatch keberadaan).
                    await self.db.reduce_position_amount_with_retry(
                        pos.symbol, reduce_amount=amount_to_close,
                        realized_pnl_partial=realized_pnl, exit_price=trade.executed_price,
                    )
                else:
                    # [ITEM #4 -- mitigasi root-cause phantom position]
                    # close_position_with_retry() (bukan close_position() polos)
                    # -- retry-backoff 3x utk kegagalan TRANSIEN (lock SQLite/
                    # hiccup koneksi). Order SUDAH sukses tereksekusi di exchange
                    # di titik ini (urutan ini tidak bisa dibalik) -- kalau
                    # SEMUA retry tetap gagal, itu genuinely phantom-risk,
                    # ditangani di except block di bawah (bukan cuma log.critical
                    # telanjang spt sebelumnya).
                    await self.db.close_position_with_retry(
                        pos.symbol, exit_price=trade.executed_price, realized_pnl=realized_pnl,
                    )
            except Exception as e:
                if is_partial:
                    # [#28] BEDA dari full-close di bawah: is_open TIDAK berubah
                    # (tetap True di kedua sisi), jadi run_position_sync_loop()
                    # TIDAK akan pernah mendeteksi ini -- phantom detector cuma
                    # bandingkan keberadaan simbol, bukan amount. Mismatch amount
                    # ini SENYAP & TIDAK self-heal sampai direview manual.
                    msg = (
                        f"reduce_position_amount (DB) GAGAL untuk {pos.symbol} setelah order "
                        f"partial-close sukses tereksekusi & {3} percobaan retry: {e}. Amount "
                        f"di DB akan tetap {full_amount:.8f} (TERLALU BESAR, seharusnya "
                        f"{full_amount - amount_to_close:.8f}) padahal exchange sudah "
                        f"berkurang -- TIDAK terdeteksi otomatis oleh run_position_sync_loop() "
                        f"(phantom detector cuma cek keberadaan posisi, bukan amount). "
                        f"Perlu review & koreksi manual amount di DB."
                    )
                else:
                    msg = (
                        f"close_position (DB) GAGAL untuk {pos.symbol} setelah order sukses "
                        f"tereksekusi & {3} percobaan retry: {e}. Posisi is_open=True akan "
                        f"nyangkut di DB walau exchange sudah flat -- akan terdeteksi otomatis "
                        f"via run_position_sync_loop() (debounce 2 siklus, ~5-10 menit) kalau "
                        f"tidak diperbaiki manual lebih cepat."
                    )
                log.critical(msg)
                try:
                    await self.db.save_log("CRITICAL", "main_future", msg)
                except Exception as _sl_err:
                    log.error("save_log (close_position gagal) gagal: %s", _sl_err)
                if self.notifier:
                    try:
                        await self.notifier.notify_error("close_position_db_failed", msg)
                    except Exception as _ne_err:
                        log.error("notify_error (close_position gagal) gagal: %s", _ne_err)

        # [FIX] Backfill Trade.realized_pnl -- kolom ini ADA di skema tapi
        # tidak pernah otomatis terisi oleh execute_signal()/_process_fill()
        # (trade_data dict di sana tidak menyertakannya). update_trade_pnl()
        # sudah tersedia di database.py tapi TIDAK PERNAH dipanggil di
        # manapun (termasuk spot/main_spot.py -- gap pre-existing, bukan
        # yang baru muncul di futures). Ditemukan & diperbaiki di sini saat
        # verifikasi siklus penuh entry->close.
        realized_pnl_pct = (
            (realized_pnl / (entry_price * amount_to_close) * 100) if (entry_price * amount_to_close) > 0 else 0.0
        )
        try:
            await self.db.update_trade_pnl(
                order_id=trade.order_id, realized_pnl=realized_pnl, realized_pnl_pct=realized_pnl_pct,
            )
        except Exception as e:
            log.warning("update_trade_pnl gagal untuk %s (non-fatal, posisi tetap tercatat benar): %s", pos.symbol, e)

        try:
            self.risk_manager.record_symbol_loss(pos.symbol, realized_pnl)
        except Exception:
            pass

        # [RE-ENTRY COOLDOWN -- opsi B] Registrasi HANYA utk exit negatif;
        # durasi 1x timeframe profile posisi (fallback 900s kalau kosong).
        if reentry_cooldown.is_negative_exit(reason, realized_pnl):
            reentry_cooldown.registry.register(
                pos.symbol,
                reentry_cooldown.duration_for_profile(getattr(pos, "strategy_profile", None)),
                reason,
            )

        log.info(
            "POSISI %s (futures): %s %s @ %.6f | amount=%.8f/%.8f | realized_pnl=%+.4f | reason=%s",
            "DIKURANGI SEBAGIAN" if is_partial else "DITUTUP PENUH",
            pos.side.upper(), pos.symbol, trade.executed_price,
            amount_to_close, full_amount, realized_pnl, reason,
        )
        if self.notifier:
            try:
                await self.notifier.notify_trade_closed(trade)
            except Exception as e:
                log.debug("notify_trade_closed gagal: %s", e)

        # [CAPITAL-ALLOCATOR PRASYARAT F -- WAJIB] Sebelum ini,
        # _do_close_position() TIDAK PERNAH memperbarui risk_manager.
        # _free_balance sama sekali -- itu cuma ter-update di
        # run_portfolio_monitor() tiap SNAPSHOT_INTERVAL (900 detik).
        # Trigger event-driven di bawah PERCUMA tanpa refresh eksplisit ini
        # dulu, krn reconcile_pending() akan membaca margin yang masih basi
        # sampai 15 menit kemudian. _do_close_position() adalah funnel
        # TUNGGAL utk semua jalur close (strategy exit, SL/TP normal,
        # liquidation-proximity emergency) via _close_position_market(),
        # jadi satu hook ini menutup semua jalur sekaligus.
        await self._refresh_portfolio()
        await self._reconcile_pending_candidates()

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
                        ratio, confidence = res["ratio"], res["confidence"]
                        thr_sell, thr_buy = res["thr_sell"], res["thr_buy"]

                        danger_level = self._get_ob_danger_level(symbol, bids, asks, ratio, confidence)
                        # [BIAS-FIX] whale_buy_genuine BARU -- thr_buy sudah
                        # lama dihitung oleh WhaleDetector._dynamic_threshold()
                        # tapi tidak pernah dibaca caller manapun. Mirror
                        # persis whale_sell_genuine, arah dibalik (ratio>thr_buy
                        # = bid-wall dominan = tekanan beli/akumulasi whale).
                        whale_sell_genuine = (ratio < thr_sell and confidence >= 0.5 and danger_level <= 4)
                        whale_buy_genuine  = (ratio > thr_buy  and confidence >= 0.5 and danger_level <= 4)

                        # [BIAS-FIX] Key (symbol, side) -- whale_sell_genuine
                        # (tekanan jual) HANYA memblokir kandidat LONG (masuk
                        # akal: jual dominan = buruk utk long), whale_buy_genuine
                        # HANYA memblokir kandidat SHORT. Sebelumnya satu
                        # whale_sell_genuine memblokir simbol utuh, termasuk
                        # short yang justru dikonfirmasi oleh sinyal itu.
                        if whale_sell_genuine:
                            action = "skip_all" if danger_level <= 2 else "skip_gate3_only"
                            self._invalidation_signals[(symbol, "long")] = {
                                "reason": "whale_sell_genuine", "level": danger_level,
                                "confidence": confidence, "ratio": ratio,
                                "action": action, "source": "gate2", "timestamp": now,
                            }
                        if whale_buy_genuine:
                            action = "skip_all" if danger_level <= 2 else "skip_gate3_only"
                            self._invalidation_signals[(symbol, "short")] = {
                                "reason": "whale_buy_genuine", "level": danger_level,
                                "confidence": confidence, "ratio": ratio,
                                "action": action, "source": "gate2", "timestamp": now,
                            }
                        if whale_sell_genuine or whale_buy_genuine:
                            continue

                        for _side_key in ((symbol, "long"), (symbol, "short")):
                            sig = self._invalidation_signals.get(_side_key)
                            if sig and (now - sig.get("timestamp", 0)) > 60:
                                del self._invalidation_signals[_side_key]

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

    def _invalidation_blocks_side(self, symbol: str, side: str) -> bool:
        """[BIAS-FIX helper] True kalau (symbol, side) sedang diinvalidasi
        dgn action skip_all/skip_gate3_only. Dipakai di beberapa titik
        _process_one utk cek per-side, bukan per-symbol."""
        sig = self._invalidation_signals.get((symbol, side))
        return bool(sig and sig.get("action") in ("skip_all", "skip_gate3_only"))

    def _both_sides_blocked(self, symbol: str) -> bool:
        """[BIAS-FIX helper] True hanya kalau LONG *dan* SHORT sama-sama
        diinvalidasi -- dipakai utk fast-skip murah SEBELUM fetch OHLCV
        (kalau cuma salah satu sisi yg diblokir, sisi lain tetap harus
        dievaluasi, jadi tidak boleh skip total di sini)."""
        return (
            self._invalidation_blocks_side(symbol, "long")
            and self._invalidation_blocks_side(symbol, "short")
        )

    async def _maybe_enqueue_gate3(self, symbol: str) -> None:
        """[BIAS-FIX] Sebelumnya skip enqueue kalau symbol PUNYA invalidation
        APAPUN (long ATAU short) -- sekarang cuma skip kalau KEDUA sisi
        diblokir, supaya sisi yang masih valid tetap sampai ke Gate3."""
        if symbol in self._pipeline_active:
            return
        if self._both_sides_blocked(symbol):
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
            log.debug(
                "[Gate3] %s (%s) EMA tidak searah (ema9=%.6f ema21=%.6f) — skip",
                symbol, side, ema9, ema21,
            )
            return False

        try:
            rsi_min, rsi_max = profile.rsi_min, profile.rsi_max
        except Exception:
            rsi_min = self.config.get("rsi_min", 45)
            rsi_max = self.config.get("rsi_max", 77)
        if not (rsi_min <= rsi <= rsi_max):
            log.debug(
                "[Gate3] %s (%s) RSI=%.1f di luar range [%d,%d] — skip",
                symbol, side, rsi, rsi_min, rsi_max,
            )
            return False

        if tf not in ("1d", "3d", "1w"):
            for vwap_col in ("VWAP_D", "VWAP", "vwap"):
                if vwap_col in bar.index:
                    vwap_val = bar.get(vwap_col)
                    if vwap_val and float(vwap_val) > 0:
                        if is_long and close < float(vwap_val):
                            log.debug(
                                "[Gate3] %s (%s) di bawah VWAP (close=%.6f vwap=%.6f) — skip",
                                symbol, side, close, float(vwap_val),
                            )
                            return False
                        if not is_long and close > float(vwap_val):
                            log.debug(
                                "[Gate3] %s (%s) di atas VWAP (close=%.6f vwap=%.6f) — skip",
                                symbol, side, close, float(vwap_val),
                            )
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

                # [BIAS-FIX] Fast-skip murah SEBELUM fetch OHLCV -- cuma
                # kalau KEDUA sisi diblokir (bukan symbol-level lagi).
                # threshold_mult per-side dipindah ke dalam loop cand_side
                # di bawah (dulu dihitung sekali di sini pakai `inv` yg
                # sekarang sudah tidak simbol-level).
                if self._both_sides_blocked(symbol):
                    return

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

                if self._both_sides_blocked(symbol):
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
                # [BIAS-FIX] Tambah filter _invalidation_blocks_side per sisi
                # -- sisi yg sedang diblokir whale (mis. whale_sell_genuine
                # utk "long") tidak masuk candidate_sides SAMA SEKALI,
                # TANPA ikut membatalkan sisi lain yg tidak diblokir.
                candidate_sides = []
                if (
                    not self._invalidation_blocks_side(symbol, "long")
                    and await self._check_gate3_direction(symbol, df, tf, "long", profile)
                ):
                    candidate_sides.append("long")
                if (
                    self.config.get("enable_short", True)
                    and not self._invalidation_blocks_side(symbol, "short")
                    and await self._check_gate3_direction(symbol, df, tf, "short", profile)
                ):
                    candidate_sides.append("short")

                if not candidate_sides:
                    return

                bar   = df.iloc[-2]
                close = float(bar["close"])
                atr   = float(bar.get("ATRr_14", 0))
                log.info("[Gate3→Gate4] %s lolos arah=%s", symbol, candidate_sides)

                # [BIAS-FIX] Re-check per-side (bukan symbol-level) -- jaring
                # pengaman race-condition (run_scanner_loop jalan sbg task
                # terpisah, bisa menulis invalidation baru di antara
                # candidate_sides selesai dihitung dan titik ini). Filter,
                # bukan return blanket -- sisi lain yg masih valid tetap lanjut.
                candidate_sides = [
                    s for s in candidate_sides
                    if (self._invalidation_signals.get((symbol, s)) or {}).get("action") != "skip_all"
                ]
                if not candidate_sides:
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
                    # [BIAS-FIX] threshold_mult dipindah ke sini (per cand_side)
                    # -- dulu dihitung sekali di awal fungsi dari `inv` symbol-
                    # level, sekarang dari invalidation (symbol, cand_side)
                    # yang genuinely relevan utk sisi ini.
                    _side_inv = self._invalidation_signals.get((symbol, cand_side))
                    threshold_mult = 1.2 if (_side_inv and _side_inv.get("action") == "monitor") else 1.0
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
                        log.debug("[Gate4] %s (%s) skor tidak tersedia — skip", symbol, cand_side)
                        continue

                    total_score = float(getattr(scored, "total_score", 0) or 0)
                    try:
                        from engine.profiles.thresholds import get_dynamic_threshold
                        _regime_val = scored.regime.value if scored.regime else "undefined"
                        # [BIAS-FIX] side=cand_side -- sebelumnya selalu matrix
                        # long apapun cand_side-nya, akar penyebab short tidak
                        # pernah lolos checkpoint ini walau lolos Gate3.
                        base_threshold = get_dynamic_threshold(profile.profile.value, _regime_val, side=cand_side)
                    except Exception:
                        base_threshold = float(getattr(scored, "threshold_used", 65) or 65)
                    effective_threshold = base_threshold * threshold_mult
                    if total_score < effective_threshold:
                        log.debug("[ScoreThreshold] %s (%s) skor %.1f < threshold %.1f — skip",
                                  symbol, cand_side, total_score, effective_threshold)
                        # [#23 -- audit fungsional] INFO throttled (maks 1x per
                        # (symbol, side) per gate4_score_reject_log_interval) --
                        # key tuple (BUKAN symbol saja) supaya reject long tidak
                        # menahan visibilitas reject short & sebaliknya.
                        if self._gate4_reject_log_throttle.allow((symbol, cand_side)):
                            log.info("[ScoreThreshold] %s (%s) skor %.1f < threshold %.1f — skip",
                                     symbol, cand_side, total_score, effective_threshold)
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
                            elif _cmd_decision.capital_constrained:
                                # [CAPITAL-ALLOCATOR] Checkpoint PERTAMA --
                                # gagal di G4 probe (ukuran ~1%, bukan ukuran
                                # Kelly sungguhan) krn kapasitas. _handle_entry
                                # tidak akan pernah terpanggil siklus ini utk
                                # cand_side ini -- registrasi di sini supaya
                                # kandidat tidak hilang begitu saja.
                                capital_allocator.register_or_refresh(
                                    self._pending_candidates, symbol, cand_side,
                                    profile_name=profile.profile.value if profile else "universal",
                                    profile_timeframe=tf, candle_ts=confirmed_ts,
                                    price=close, atr=atr, score=total_score,
                                    reason=_cmd_decision.rejection_reason,
                                )
                                continue
                            else:
                                log.info("[Gate4.5] Commander reject %s (%s): %s",
                                         symbol, cand_side, _cmd_decision.rejection_reason)
                                continue
                        except Exception as _cmd_err:
                            log.warning("[Gate4.5] Commander error %s (%s): %s — lanjut tanpa full gate",
                                        symbol, cand_side, _cmd_err)

                    # [BIAS-FIX] key (symbol, cand_side) -- ini sudah pakai
                    # `continue` (skip cand_side ini saja), bukan `return`,
                    # jadi cuma perlu fix key-nya jadi per-side.
                    inv = self._invalidation_signals.get((symbol, cand_side))
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
                            # [CAPITAL-ALLOCATOR PRASYARAT] Dipakai _handle_entry()
                            # utk registrasi PendingCandidate kalau ternyata gagal
                            # di real risk-check karena kapasitas -- pakai `tf`
                            # (bukan profile.timeframe langsung) krn `tf` sudah
                            # resolve fallback dgn benar walau profile lookup
                            # gagal (profile bisa None, lihat try/except di atas).
                            "profile_timeframe": tf, "candle_ts": confirmed_ts,
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

                # [AUDIT ITEM #8 -- Tier 2, PENYESUAIAN STRUKTURAL futures]
                # BEDA dari spot: futures TIDAK update_position_price() di
                # run_sl_tp_monitor() (dicek langsung, nihil) -- update harga
                # posisi futures terjadi DI SINI, di dalam _refresh_portfolio()
                # (dipanggil dari run_portfolio_monitor() tiap SNAPSHOT_INTERVAL
                # 900 detik, PLUS event-triggered post-entry/post-close/
                # _on_trade_executed throttled >=5s -- cadence berbeda dari
                # spot yg 5 detik tetap). Titik publish teragregasi yang benar
                # utk futures HARUS di sini, BUKAN meniru run_sl_tp_monitor()
                # spot mentah-mentah -- itu akan salah sama sekali (loop itu
                # di futures tidak pernah update harga posisi).
                if positions:
                    try:
                        fresh_positions = await self.db.get_open_positions()
                        if fresh_positions:
                            self.event_bus.publish(
                                "positions_snapshot", fresh_positions, market_type="futures",
                            )
                    except Exception as _snap_err:
                        log.debug("Portfolio refresh (futures): gagal publish positions_snapshot: %s", _snap_err)

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
                    try:
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
                        #
                        # [FIX] Binance men-trigger liquidation berdasar MARK PRICE,
                        # BUKAN last traded price -- sebelumnya cek ini pakai `price`
                        # (dari _get_current_price/last price) yang bisa BEDA dari
                        # mark price (terutama saat funding/basis spread melebar).
                        # Sekarang pakai fetch_mark_price() (sudah ada di
                        # FutureExchangeConnector, sebelumnya dibangun tapi tidak
                        # pernah dipanggil di manapun). SL/TP normal TETAP pakai
                        # `price` (last/trade price) di bawah -- itu memang benar
                        # menggunakan harga transaksi, cuma liquidation spesifik
                        # yang perlu mark price.
                        # ══════════════════════════════════════════════════════
                        if pos.liquidation_price:
                            try:
                                mark_price = await self.exchange.fetch_mark_price(pos.symbol)
                            except Exception as _mp_err:
                                log.warning(
                                    "fetch_mark_price(%s) gagal, fallback ke last price "
                                    "utk cek liquidation (kurang akurat): %s",
                                    pos.symbol, _mp_err,
                                )
                                mark_price = price
                            if not mark_price or mark_price <= 0:
                                mark_price = price

                            liq = float(pos.liquidation_price)
                            entry = float(pos.entry_price or 0)
                            if entry > 0:
                                if pos.side == "long":
                                    dist_to_liq_pct = (mark_price - liq) / liq * 100 if liq > 0 else 999
                                    in_danger = dist_to_liq_pct <= self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT
                                else:
                                    dist_to_liq_pct = (liq - mark_price) / liq * 100 if liq > 0 else 999
                                    in_danger = dist_to_liq_pct <= self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT

                                if in_danger:
                                    log.critical(
                                        "⚠️ LIQUIDATION PROXIMITY DARURAT: %s %s | mark_price=%.6f "
                                        "liq_price≈%.6f (APPROXIMATE) | jarak=%.2f%% <= ambang %.1f%% "
                                        "— EMERGENCY CLOSE.",
                                        pos.side.upper(), pos.symbol, mark_price, liq,
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

                        # [ITEM #3 -- proteksi per-langkah] check_breakeven_sl() &
                        # db.update_position_sl() sebelumnya TIDAK dibungkus -- data
                        # posisi korup (mis. entry_price non-numerik) bisa melempar
                        # exception di sini dan (sebelum fix ini) membatalkan
                        # pengecekan SL/TP untuk SEMUA posisi setelahnya di siklus
                        # yang sama. Sekarang: gagal di langkah ini TIDAK menghentikan
                        # trailing/hit_sl/hit_tp check untuk POSISI YANG SAMA di
                        # siklus yang sama (graceful degradation), dan backstop
                        # per-posisi di luar tetap ada sbg jaring pengaman kedua.
                        try:
                            new_sl = self.risk_manager.check_breakeven_sl(
                                entry_price=pos.entry_price, current_price=price,
                                current_sl=pos.stop_loss_price, take_profit=pos.take_profit_price,
                                side=pos.side,
                                strategy_profile=str(getattr(pos, "strategy_profile", "") or ""),
                            )
                            if new_sl is not None and (pos.stop_loss_price is None or new_sl != pos.stop_loss_price):
                                log.info("BREAKEVEN SL | %s | %.6f → %.6f", pos.symbol, pos.stop_loss_price, new_sl)
                                await self.db.update_position_sl(pos.symbol, new_sl)
                                pos.stop_loss_price = new_sl
                        except Exception as _be_err:
                            log.error(
                                "Breakeven SL check gagal [%s] (entry_price=%r stop_loss_price=%r "
                                "take_profit_price=%r): %s",
                                pos.symbol, pos.entry_price, pos.stop_loss_price,
                                pos.take_profit_price, _be_err, exc_info=True,
                            )

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
                            # [ITEM #3] check_trailing_sl() & db.update_position_sl()
                            # sebelumnya TIDAK dibungkus -- lihat catatan di blok
                            # breakeven di atas, pola & alasan sama persis.
                            try:
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
                            except Exception as _tr_err:
                                log.error(
                                    "Trailing SL check gagal [%s] (current_atr=%r stop_loss_price=%r): %s",
                                    pos.symbol, current_atr, pos.stop_loss_price, _tr_err, exc_info=True,
                                )

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

                        # [ATG-EXIT-WIRING] trailing_reason dipindah ke SINI (sebelum
                        # blok ATG, bukan sesudahnya spt sebelumnya) -- supaya guard
                        # "not trailing_reason" di bawah bisa dipakai ATG utk cek
                        # prioritas, persis pola spot/main_spot.py baris 1995-1997.
                        # Logic-nya sendiri TIDAK berubah, cuma posisinya digeser.
                        trailing_reason = None
                        if self.strategy and hasattr(self.strategy, "check_trailing_exit"):
                            trailing_reason = self.strategy.check_trailing_exit(pos.symbol, price)

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

                            # [ATG-EXIT-WIRING -- BARU] Layer 1 Composite Exit Score.
                            # Sebelumnya _atg_result.should_exit/exit_reason dihitung
                            # tapi TIDAK PERNAH dibaca di sini (beda dari spot/main_spot.py
                            # baris 2052) -- ATG cuma pernah memperbaiki SL (Layer 2),
                            # tidak pernah memicu close sendiri. Guard identik dgn spot:
                            # SL/TP/trailing yang sudah aktif di siklus yang sama menang
                            # duluan (ATG tidak override sinyal yang lebih "keras").
                            # [KEPUTUSAN, beda dari spot secara sengaja] TIDAK memanggil
                            # notify_sl_tp_hit() di sini -- 3 cabang sibling futures
                            # (hit_sl/hit_tp/trailing_reason di bawah) juga tidak
                            # memanggilnya, konsisten dgn pola futures yang sudah ada
                            # (notifikasi generik notify_trade_closed() di
                            # _do_close_position() tetap jalan otomatis utk semua kasus).
                            if _atg_result.should_exit and not trailing_reason and not hit_sl and not hit_tp:
                                log.info(
                                    "ATG EXIT [%s] @ %.6f | %s",
                                    pos.symbol, price, _atg_result.exit_reason,
                                )
                                await self._close_position_market(pos, price, _atg_result.exit_reason)
                                continue
                        except Exception as _atg_err:
                            log.debug("ATG error [%s]: %s", pos.symbol, _atg_err)

                        # [ITEM #3] _close_position_market() untuk hit_sl/hit_tp/
                        # trailing_reason sebelumnya TIDAK dibungkus -- ini titik
                        # PALING kritis dari audit (order close bisa gagal karena
                        # exchange error, DB error, dst). Gagal di sini TIDAK
                        # boleh membatalkan pengecekan posisi lain di siklus ini.
                        try:
                            if hit_sl:
                                await self._close_position_market(pos, price, f"Stop-loss hit @ {pos.stop_loss_price:.6f}")
                            elif hit_tp:
                                await self._close_position_market(pos, price, f"Take-profit hit @ {pos.take_profit_price:.6f}")
                            elif trailing_reason:
                                await self._close_position_market(pos, price, trailing_reason)
                        except Exception as _close_err:
                            log.error(
                                "Close posisi gagal [%s] (hit_sl=%s hit_tp=%s trailing_reason=%r): %s",
                                pos.symbol, hit_sl, hit_tp, trailing_reason, _close_err, exc_info=True,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # [ITEM #3 -- backstop per-posisi] Jaring pengaman TERAKHIR:
                        # kalau ada langkah di badan loop ini yang GAGAL dibungkus
                        # try/except spesifik (termasuk kode BARU yang ditambahkan
                        # nanti tanpa dibungkus), tangkap di sini supaya SATU posisi
                        # bermasalah tidak pernah menghentikan pengecekan SL/TP untuk
                        # posisi LAIN di siklus yang sama. Kalau log ini menyala,
                        # artinya ada kegagalan yang TIDAK tertangkap try/except
                        # spesifik manapun -- layak diselidiki, bukan sekadar noise.
                        log.error(
                            "SL/TP monitor (futures): gagal proses posisi %s — "
                            "lanjut ke posisi berikutnya: %s",
                            pos.symbol, e, exc_info=True,
                        )
                        continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SL/TP monitor error (futures): %s", e, exc_info=True)

            await asyncio.sleep(self.SL_TP_CHECK_INTERVAL)

    async def run_portfolio_monitor(self) -> None:
        while self.is_running:
            try:
                await self._refresh_portfolio()
                # [CAPITAL-ALLOCATOR] Fallback/safety-net -- mekanisme UTAMA
                # tetap trigger event-driven di _do_close_position(). Ini
                # jaring pengaman kalau event-driven pernah gagal (crash
                # antara close & reconcile, jalur close lain yg terlewat,
                # dst) -- registry tetap ter-reconcile dlm waktu terburuk
                # SNAPSHOT_INTERVAL (900 detik), bukan tak terbatas.
                await self._reconcile_pending_candidates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Portfolio monitor error: %s", e)
            await asyncio.sleep(self.SNAPSHOT_INTERVAL)

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
        """[BUG-FIX -- audit item #18] Sebelumnya method ini memanggil
        `self._analytics.refresh()` -- method itu TIDAK PERNAH ADA di
        PerformanceAnalytics (dikonfirmasi grep "async def" di
        engine/learning/analytics.py, nama aslinya refresh_snapshots()).
        Guard `hasattr(self._analytics, "refresh")` akan SELALU False --
        loop ini berjalan selamanya tanpa melakukan apa pun dan TANPA
        error (silent no-op), bahkan setelah self._analytics genuinely
        diinstansiasi (lihat _initialize_intelligence_pipeline()). Method
        ini JUGA tidak pernah punya blok meta-learner (run_full_cycle())
        sama sekali -- padahal versi spot (acuan) punya. Fix: panggil
        refresh_snapshots() yang benar, tambahkan blok meta-learner
        (mirror spot). TIDAK menambahkan blok cross-learning/coin-swap --
        futures dikonfirmasi tidak punya fitur itu sama sekali (grep
        "cross_learn_enabled\\|CoinSwap" nihil di file ini)."""
        if not self._analytics:
            log.debug("Analytics (futures) tidak aktif — run_analytics_loop di-skip.")
            return

        log.info("Analytics loop (futures) dimulai.")
        await asyncio.sleep(min(self.config.get("analytics_refresh_interval", 3600), 600))

        while self.is_running:
            interval = self.config.get("analytics_refresh_interval", 3600)
            try:
                log.info("Analytics (futures): memperbarui performance snapshots...")
                await self._analytics.refresh_snapshots()

                if self._meta_learner:
                    log.info("Meta-learner (futures): mengevaluasi suggestions...")
                    suggestions = await self._meta_learner.run_full_cycle()
                    if suggestions:
                        log.info(
                            "Meta-learner (futures) menghasilkan %d suggestion(s) baru.",
                            len(suggestions),
                        )
                        for sug in suggestions:
                            await self.db.save_log(
                                "INFO", "meta_learner",
                                f"Suggestion: {sug.symbol} | {sug.parameter_name} "
                                f"{sug.current_value} → {sug.suggested_value} | {sug.reasoning[:100]}",
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Analytics loop error (futures): %s", e)

            await asyncio.sleep(interval)

    async def run_market_cache_refresh(self) -> None:
        """[ITEM #6 -- sama kelas masalah dgn insiden EVAA/USDT, scope lebih
        luas] self.exchange._markets sebelumnya cuma di-set sekali di
        connect() (dan sekali lagi manual sebelum auto-scan universe futures,
        lihat reload_markets() call di start()) -- tidak ada refresh
        periodik sepanjang sisa umur proses. Loop ini memanggil ulang
        reload_markets() yang sudah ada (bukan reimplementasi), tiap
        market_cache_refresh_interval detik (default 3600 = 1 jam -- cukup
        utk skala perubahan listing/fee Binance, dampak rate-limit
        diabaikan dibanding OHLCV/ticker loop yg sudah jalan tiap siklus).
        Pola identik run_analytics_loop/run_portfolio_monitor di file ini."""
        interval = self.config.get("market_cache_refresh_interval", 3600)
        log.info("Market cache refresh loop (futures) dimulai (interval=%ds).", interval)
        while self.is_running:
            try:
                await asyncio.sleep(interval)
                await self.exchange.reload_markets()
                log.info(
                    "Market cache refresh: %d markets di-reload.",
                    len(getattr(self.exchange, "_markets", {}) or {}),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Market cache refresh error: %s", e)

    async def run_config_watcher(self) -> None:
        """[REUSE pola spot] Baca 'config_update_futures' dari bot_state
        (ditulis oleh POST /api/config/update di api_server_future.py)."""
        import json
        log.info("Config watcher (futures) dimulai (interval=30s).")
        while self.is_running:
            try:
                raw = await self.db.get_bot_state("config_update_futures")
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
                            if self.ws_feed:
                                await self.ws_feed.add_symbols(self.config["universe_watchlist"])
                        if self.risk_manager:
                            self.risk_manager._update_config(self.config)
                        await self.db.clear_bot_state("config_update_futures")
                        log.info("Config futures diupdate via dashboard: %s", applied)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Config watcher (futures) error: %s", e)
            await asyncio.sleep(30)

    async def run_position_sync_loop(self) -> None:
        """
        [SEKARANG TERSEDIA] position_sync_futures.py sudah dibangun (langkah
        8/9) -- loop ini bisa diaktifkan, beda dari versi sebelumnya yang
        sengaja dihilangkan dari run().
        """
        from future.position_sync_futures import run_position_sync
        log.info("Position Sync loop (futures) dimulai — interval 5 menit")
        await asyncio.sleep(30)
        while self.is_running:
            try:
                result = await run_position_sync(
                    self.exchange, self.db,
                    notifier=self.notifier, phantom_suspects=self._phantom_suspects,
                    amount_mismatch_suspects=self._amount_mismatch_suspects,
                )
                if result["adopted"] > 0:
                    log.info(
                        "PositionSync (futures): %d diadopsi | %d ditolak | %d error",
                        result["adopted"], result["rejected"], result["errors"],
                    )
                if result.get("phantom_confirmed", 0) > 0:
                    log.warning(
                        "PositionSync (futures): %d phantom position terkonfirmasi "
                        "-- lihat log CRITICAL di atas, butuh review manual.",
                        result["phantom_confirmed"],
                    )
                if result.get("amount_mismatch_confirmed", 0) > 0:
                    log.warning(
                        "PositionSync (futures): %d amount mismatch terkonfirmasi "
                        "-- lihat log CRITICAL di atas, butuh review manual.",
                        result["amount_mismatch_confirmed"],
                    )
            except Exception as e:
                log.error("run_position_sync_loop (futures) error: %s", e)
            await asyncio.sleep(300)

    async def run_funding_settlement_loop(self) -> None:
        """
        [FUTURES-SPECIFIC -- BARU] Sebelumnya funding.py (calculate_funding_payment,
        fetch_funding_rate) sudah ada & teruji terpisah, TAPI tidak pernah
        disambungkan ke loop manapun -- posisi futures yang ditahan lama
        tidak pernah kena potongan/dapat funding di paper trading kita.

        Binance mem-settle funding tiap 00:00, 08:00, 16:00 UTC. Loop ini
        cek tiap 5 menit apakah sudah melewati boundary funding baru sejak
        terakhir diproses (dibandingkan via "funding slot" = tanggal + jam//8),
        lalu terapkan payment ke SEMUA posisi terbuka saat itu.

        ⚠️ CATATAN: real Binance funding rate berubah tiap interval (bukan
        konstan) -- kita fetch rate TERKINI persis sebelum settlement,
        bukan memprediksi. Untuk paper trading, payment diterapkan ke
        _paper_margin_balance (via exchange.apply_funding_payment) dan
        Position.funding_paid_total (akumulasi, via db.update_position_funding).
        """
        from future.funding import calculate_funding_payment
        last_funding_slot: Optional[str] = None

        while self.is_running:
            try:
                now = _utcnow_dt()
                current_slot = f"{now.date()}_{now.hour // 8}"

                if last_funding_slot is None:
                    # Startup: catat slot saat ini sbg baseline, JANGAN langsung
                    # settle (supaya tidak salah kena funding utk periode yg
                    # sebagian besar sudah lewat sebelum bot ini hidup).
                    last_funding_slot = current_slot
                elif current_slot != last_funding_slot:
                    last_funding_slot = current_slot
                    positions = await self.db.get_open_positions()
                    if positions:
                        log.info(
                            "Funding settlement window (futures): slot baru %s, "
                            "%d posisi terbuka.", current_slot, len(positions),
                        )
                    for pos in positions:
                        try:
                            funding_data = await self.exchange.fetch_funding_rate(pos.symbol)
                            funding_rate = float(
                                funding_data.get("fundingRate")
                                or funding_data.get("info", {}).get("lastFundingRate", 0)
                                or 0
                            )
                            price = await self._get_current_price(pos.symbol) or float(pos.current_price or pos.entry_price or 0)
                            notional = float(pos.amount or 0) * price
                            if notional <= 0:
                                continue

                            payment = calculate_funding_payment(notional, funding_rate, pos.side)

                            new_margin_balance = self.exchange.apply_funding_payment(pos.symbol, payment)
                            await self.db.update_position_funding(pos.symbol, payment)

                            log.info(
                                "Funding settled (futures): %s %s | rate=%.6f%% notional=$%.2f "
                                "payment=%+.4f USDT | margin_balance baru=%.4f",
                                pos.side.upper(), pos.symbol, funding_rate * 100,
                                notional, payment, new_margin_balance,
                            )
                        except Exception as e:
                            log.warning("Funding settlement gagal utk %s (non-fatal): %s", pos.symbol, e)

                    # Equity berubah akibat funding -- refresh supaya risk_manager
                    # & dashboard lihat angka terkini.
                    if positions:
                        try:
                            await self._refresh_portfolio()
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Funding settlement loop error: %s", e, exc_info=True)

            await asyncio.sleep(300)  # cek tiap 5 menit

    async def run(self) -> None:
        await self.start()

        try:
            await self._refresh_portfolio()
            log.info("Portfolio startup refresh (futures): equity=%.4f", self.portfolio_state.get("total_equity", 0))
        except Exception as _pe:
            log.warning("Portfolio startup refresh gagal: %s", _pe)

        # [UPDATE] run_position_sync_loop() SEKARANG DIIKUTSERTAKAN --
        # position_sync_futures.py sudah dibangun & diverifikasi end-to-end.
        # run_coin_swap_loop() TETAP TIDAK diikutsertakan -- sistem itu
        # deprecated permanen, tidak relevan sama sekali di futures.
        #
        # [#15 Temuan B] TITIK BERPASANGAN dgn assertion di start() (baris
        # pemanggilan _reconcile_phantom_positions_on_startup()). self._tasks
        # PERTAMA KALI terisi di sini -- SETELAH await self.start() di atas
        # sudah selesai penuh, termasuk reconciliation di dalamnya. JANGAN
        # pindahkan create_task() manapun di bawah ini ke DALAM start()/
        # sebelum start() selesai -- itu melanggar invarian "nol aktivitas
        # trading konkuren saat reconciliation" yang jadi dasar
        # auto-close-tanpa-debounce genuinely aman. Assertion di start()
        # akan gagal loud kalau ini dilanggar.
        self._tasks = [
            asyncio.create_task(self.run_scanner_loop(),       name="task_scanner_futures"),
            asyncio.create_task(self.run_gate3_worker(),       name="task_gate3_worker_futures"),
            asyncio.create_task(self.run_portfolio_monitor(),  name="task_portfolio_futures"),
            asyncio.create_task(self.run_sl_tp_monitor(),      name="task_sl_tp_futures"),
            asyncio.create_task(self.run_daily_summary(),      name="task_daily_summary_futures"),
            asyncio.create_task(self.run_analytics_loop(),     name="task_analytics_futures"),
            asyncio.create_task(self.run_market_cache_refresh(), name="task_market_cache_refresh_futures"),
            asyncio.create_task(self.run_config_watcher(),     name="task_config_watcher_futures"),
            asyncio.create_task(self.run_position_sync_loop(), name="task_position_sync_futures"),
            asyncio.create_task(self.run_funding_settlement_loop(), name="task_funding_settlement_futures"),
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
            # [SIGTERM FIX -- mirror spot/main_spot.py] serve() menginstal
            # handler uvicorn yang MENIMPA add_signal_handler bot -- SIGTERM
            # ditelan, loop trading tuli. Cabut instalasi handler uvicorn.
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            await asyncio.gather(bot.run(), server.serve())
        else:
            await bot.run()
    except BotStartupError as e:
        log.critical("Bot futures startup FAILED: %s", e)
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

