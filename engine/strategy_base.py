"""
engine/strategy_base.py — Base class strategy scoring/tracking, market-agnostic

Diekstrak dari spot/strategy_spot.py (2026-07-11), ditemukan sebagai gap
arsitektur saat verifikasi menyeluruh apakah future/ benar-benar independen
dari spot/ (jawabannya: TIDAK, sampai ekstraksi ini dilakukan).

Berisi SEMUA logic yang genuinely market-agnostic: PositionTracker (sudah
side-aware sejak awal sesi), BaseStrategy (interface abstrak), dan
VolumetricBreakoutStrategyBase (scoring pipeline v7: observer->classifier->
scorer->validator, semua sudah side-aware sejak perbaikan bias long-only).

YANG SENGAJA TIDAK ADA DI SINI (tetap di spot/strategy_spot.py, TIDAK
diekstrak, karena genuinely long-only/legacy dan TIDAK dipakai future/ sama
sekali -- dikonfirmasi future/main_future.py hanya memanggil
get_scored_signal() langsung dari run_gate3_worker, tidak pernah memanggil
generate_signals()):
- generate_signals(), _generate_signals_v7(), _generate_signals_legacy():
  pipeline "legacy" yang hardcode SignalType.BUY, dipanggil hanya dari
  spot/main_spot.py::run_strategy_loop() (task terpisah yang TIDAK ada di
  future/main_future.py sama sekali)
- _detect_exit_mode(), _compute_sl_tp_quick(), _compute_sl_tp_wave():
  cuma dipakai oleh _generate_signals_legacy, hardcode formula long-only
  (sl = close - dist, bukan mirror utk short)
- _compute_confidence(): cuma dipakai _generate_signals_v7/legacy

Subclass (VolumetricBreakoutStrategy di spot/ dan future/) WAJIB
mengimplementasikan generate_signals() sendiri (abstract method di BaseStrategy).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING
import time as _time

from engine.constants import APP_VERSION, COL_EMA9, COL_EMA21, COL_EMA50, COL_RSI, COL_ATR, REQUIRED_INDICATOR_COLS
from engine.profiles.base_profile import CoinProfile, AdaptiveParams, StrategyProfile
from engine.profiles.registry import get_coin_profile
from engine.core.models import SignalType, SignalEvent, ExitMode

if TYPE_CHECKING:
    from engine.core.models import ScoredSignal, ObservationReport
    from engine.intelligence.observer import MarketObserver
    from engine.intelligence.classifier import MarketClassifier
    from engine.intelligence.scorer import SignalScorer
    from engine.intelligence.validator import SignalValidator

log = logging.getLogger("strategy_base")

try:
    import engine.ta_compat
    import pandas as pd
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False
    import pandas as pd

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

_UNIVERSAL_DEFAULTS: Dict = {
    "lookback":                20,
    "volume_multiplier":       1.3,
    "volume_spike_threshold":  3.0,
    "rsi_min":                 45,
    "rsi_max":                 77,
    "rsi_golden_cross_min":    45,
    "min_breakout_pct":        0.10,
    "atr_sl_mult":             2.0,
    "atr_tp_mult":             3.5,
    "atr_pct_threshold":       0.8,
    "quick_sl_pct":            1.20,
    "quick_tp_pct":            1.75,
    "trailing_activation_pct": 1.50,
    "trailing_gap_pct":        0.50,
    "max_hold_seconds":        0,
    "min_candles":             60,
    "sentiment_enabled":       True,
    "use_quote_volume":        True,
    "_adaptive_mode":          "N/A",
    "_atr_ratio":              1.0,
}


@dataclass
class PositionTracker:
    symbol:           str
    entry_price:      float
    entry_time:       datetime
    exit_mode:        ExitMode
    highest_price:    float
    # [FUTURES-READY] Field baru: arah posisi ("long"/"short"). Default "long"
    # supaya seluruh perilaku bot spot yang sudah berjalan (100% long) TIDAK
    # berubah sama sekali -- ini murni scaffolding untuk dukungan short/futures
    # nanti, tanpa mengubah behavior spot saat ini.
    side:             str   = "long"
    trailing_active:  bool  = False
    quick_tp_pct:     float = 1.75
    quick_sl_pct:     float = 1.20
    atr_sl_mult:      float = 2.0
    trailing_gap_pct: float = 0.50
    activation_pct:   float = 1.50
    max_hold_seconds: int   = 0
    candles_held:     int   = 0
    profile_name:     str   = "universal"
    entry_score:    float = 0.0
    entry_regime:   str   = "undefined"
    sl_tightened:            bool             = False
    sl_relaxed:              bool             = False
    last_regime_action:      str              = ""
    last_regime_action_time: Optional[datetime] = None
    regime_stability_count:  int              = 0
    last_seen_regime:        str              = ""
    regime_action_log:       list             = field(default_factory=list)
    # [TAMBAHAN] Referensi tetap quick_sl_pct SAAT POSISI DIBUKA (sebelum
    # ada modifikasi tighten/relax apapun). Dipakai oleh HOLD_RELAX_SL agar
    # selalu kembali persis ke nilai ini — bukan melipatgandakan dari nilai
    # saat ini, yang sebelumnya menyebabkan SL menyusut progresif kalau
    # tighten/relax terjadi berulang (lihat _handle_regime_transition).
    # __post_init__ mengisi ini otomatis dari quick_sl_pct kalau tidak
    # diisi manual saat construct, supaya semua caller existing tidak perlu
    # diubah satu per satu.
    original_quick_sl_pct: Optional[float] = None

    def __post_init__(self) -> None:
        if self.original_quick_sl_pct is None:
            self.original_quick_sl_pct = self.quick_sl_pct

    def increment_hold(self) -> None:
        self.candles_held += 1

    def is_overtime(self) -> bool:
        if self.max_hold_seconds <= 0:
            return False
        elapsed = (_utcnow() - self.entry_time).total_seconds()
        return elapsed >= self.max_hold_seconds

_sentiment_lock: asyncio.Lock = asyncio.Lock()
_sentiment_cache: Dict = {"score": 0.0, "ts": 0.0}
_SENTIMENT_TTL = 300

async def check_market_sentiment(symbol: str) -> float:
    global _sentiment_cache

    async with _sentiment_lock:
        now     = _time.monotonic()
        cached  = _sentiment_cache.copy()
    if cached["ts"] == 0.0:
        log.debug("Sentiment cache kosong (fresh start) — pakai neutral 0.0")
    elif now - cached["ts"] < _SENTIMENT_TTL:
        return cached["score"]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    log.debug("Sentiment API error: HTTP %d", resp.status)
                    async with _sentiment_lock:
                        return _sentiment_cache["score"]

                data = await resp.json()
                value_str = data["data"][0]["value"]
                fng = int(value_str)

        if fng <= 25:
            score = -0.8
        elif fng <= 45:
            score = -0.3
        elif fng <= 55:
            score = 0.0
        elif fng <= 75:
            score = 0.3
        else:
            score = 0.5

        async with _sentiment_lock:
            now = _time.monotonic()
            _sentiment_cache = {"score": score, "ts": now}

        log.info(
            "Sentiment update: F&G=%d → score=%.1f (%s)",
            fng, score,
            data["data"][0].get("value_classification", "?"),
        )
        return score

    except asyncio.TimeoutError:
        async with _sentiment_lock:
            cached = _sentiment_cache["score"]
        log.debug("Sentiment API timeout — pakai cache: %.1f", cached)
        return cached
    except Exception as e:
        async with _sentiment_lock:
            cached_score = _sentiment_cache["score"]
            cached_ts    = _sentiment_cache["ts"]
        fallback = cached_score if cached_ts > 0.0 else 0.0
        log.debug("Sentiment API error: %s — pakai fallback: %.1f", e, fallback)
        return fallback


class BaseStrategy(ABC):

    def __init__(
        self,
        name:      str,
        symbols:   List[str],
        timeframe: str,
        params:    Dict,
    ) -> None:
        self.name       = name
        self.symbols    = symbols
        self.timeframe  = timeframe
        self.params     = params
        self._is_active = True
        log.info(
            "Strategy [%s] init | symbols=%s tf=%s", name, symbols, timeframe
        )

    @abstractmethod
    async def generate_signals(
        self, symbol: str, df: pd.DataFrame
    ) -> List[SignalEvent]:
        ...

    @abstractmethod
    async def get_scored_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        confirmation_df: Optional[pd.DataFrame] = None,
        confirmation_timeframe: Optional[str] = None,
        ob_data: Optional[dict] = None,
    ) -> Optional["ScoredSignal"]:
        ...

    def sync_position_state(
        self,
        open_symbols:   Set[str],
        open_positions: List = None,
    ) -> None:
        pass

    @property
    def is_active(self) -> bool:
        return self._is_active

    def pause(self) -> None:
        self._is_active = False
        log.warning("Strategy [%s] paused.", self.name)

    def resume(self) -> None:
        self._is_active = True
        log.info("Strategy [%s] resumed.", self.name)

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        if not _TA_AVAILABLE:
            log.error("pandas_ta tidak tersedia — tidak bisa enrich dataframe.")
            return pd.DataFrame()

        df.ta.ema(length=9,  append=True)
        df.ta.ema(length=21, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)

        try:
            df.ta.vwap(anchor="D", append=True)
        except Exception:
            try:
                df.ta.vwap(append=True)
            except Exception:
                pass

        return df.dropna()

    @staticmethod
    def _get_vwap(bar: pd.Series) -> Optional[float]:
        for col in ("VWAP_D", "VWAP", "vwap"):
            if col in bar.index:
                val = bar[col]
                if pd.notna(val) and float(val) > 0:
                    return float(val)
        return None

    @staticmethod
    def _validate_cols(
        df: pd.DataFrame, required: List[str], ctx: str = ""
    ) -> bool:
        missing = [c for c in required if c not in df.columns]
        if missing:
            log.debug("[%s] Missing columns: %s", ctx, missing)
            return False
        return True


class VolumetricBreakoutStrategyBase(BaseStrategy):
    """
    Berisi SEMUA bagian VolumetricBreakoutStrategy yang genuinely
    market-agnostic. Subclass (spot & future) menambahkan generate_signals()
    dan method legacy-only lainnya sesuai kebutuhan masing-masing.
    """

    _COL_EMA9  = COL_EMA9
    _COL_EMA21 = COL_EMA21
    _COL_EMA50 = COL_EMA50
    _COL_RSI   = COL_RSI
    _COL_ATR   = COL_ATR
    _REQUIRED_COLS = REQUIRED_INDICATOR_COLS

    def __init__(
        self,
        symbols:   List[str],
        timeframe: str  = "15m",
        params:    Dict = None,
    ) -> None:
        if params is None: params = {}
        merged = {**_UNIVERSAL_DEFAULTS, **params}
        super().__init__(
            name="VolumetricBreakout",
            symbols=symbols,
            timeframe=timeframe,
            params=merged,
        )

        self._in_position:  Dict[str, bool]               = {s: False for s in symbols}
        self._pos_trackers: Dict[str, PositionTracker]    = {}
        self._profiles:     Dict[str, Optional[CoinProfile]] = {}
        # [BUG-FIX KRITIS -- DEADLOCK] Sebelumnya threading.Lock() biasa
        # (non-reentrant). sync_position_state() memegang self._lock (baris
        # ~554) lalu, MASIH DI DALAM lock yang sama, memanggil
        # self._resolve_params() (baris ~575) yang JUGA mencoba
        # `with self._lock:` -- thread yang sama mencoba mengambil lock yang
        # sudah dipegangnya sendiri -> DEADLOCK PERMANEN (thread menunggu
        # dirinya sendiri melepas lock, tidak akan pernah terjadi).
        # sync_position_state dipanggil SYNCHRONOUS (bukan lewat executor,
        # bukan di-await) langsung dari main.py setelah strategy dikonstruksi
        # -- setiap kali bot restart dgn MINIMAL SATU posisi terbuka yang
        # py_data-nya tersedia dari DB, seluruh proses startup bot akan
        # menggantung selamanya di baris ini, bot TIDAK PERNAH benar-benar
        # mulai jalan. Dibuktikan via eksperimen: thread terpisah yang
        # memanggil sync_position_state() dgn 1 posisi terbuka tidak pernah
        # selesai setelah 5 detik (deadlock nyata, bukan lambat). Fix:
        # threading.RLock() (reentrant) -- thread yang sama boleh
        # mengambil lock berkali-kali tanpa deadlock, tetap melindungi dari
        # race antar-thread berbeda (worker thread Gate3 vs main thread).
        self._lock = threading.RLock()
        self._pending_entry: Set[str] = set()
        self._last_entry_params: Dict[str, Dict] = {}
        self._last_regime: Dict[str, str] = {}
        self._notifier = None  # akan diinject dari main.py
        self._db = None       # akan diinject dari main.py
        self._ws_feed = None  # akan diinject dari main.py (untuk auto-classify profile)

        # Intelligence pipeline components (lazy init)
        self._observer:   Optional[object] = None
        self._classifier: Optional[object] = None
        self._scorer:     Optional[object] = None
        self._validator:  Optional[object] = None
        self._pipeline_ready: bool = False

        self._load_profiles(symbols)
        self._try_init_pipeline()

    def refresh_profiles(self) -> None:
        """Re-classify semua profil setelah WS feed punya data ticker."""
        from engine.profiles.registry import _PROFILE_CACHE, _COIN_PROFILE_MAP
        # Clear cache profil yang di-load saat startup (ticker belum ada)
        _PROFILE_CACHE.clear()
        # Hapus entry auto-classify lama dari map (bukan manual entries)
        manual_keys = {"BTC","ETH","SOL","BNB","AVAX","XRP","ADA","DOT","LINK",
                       "ATOM","LTC","NEAR","APT","SUI","FET","INJ","OP","ARB",
                       "AIGENSYN","BIO","HYPER","UNI","AAVE","SNX","SPK",
                       "PEPE","POL","DOGE","SHIB","FLOKI","WIF","BONK"}
        for k in list(_COIN_PROFILE_MAP.keys()):
            if k not in manual_keys:
                del _COIN_PROFILE_MAP[k]
        # Re-load dengan ticker data yang sudah ada
        with self._lock:
            symbols = list(self._profiles.keys())
        self._load_profiles(symbols)
        log.info("refresh_profiles: %d profil di-reload dengan data ticker live", len(symbols))

    def update_symbols(self, new_symbols: list) -> None:
        """Hot-reload daftar simbol yang dipantau strategy."""
        with self._lock:
            # Tambah koin baru ke _in_position
            for sym in new_symbols:
                if sym not in self._in_position:
                    self._in_position[sym] = False

            # Bersihkan koin yang dihapus dari watchlist
            # tapi HANYA kalau tidak sedang punya posisi terbuka
            removed = [s for s in self.symbols if s not in new_symbols]
            for sym in removed:
                if not self._in_position.get(sym, False):
                    self._in_position.pop(sym, None)
                    self._profiles.pop(sym, None)
                    self._pos_trackers.pop(sym, None)
                    self._last_regime.pop(sym, None)
                else:
                    log.debug(
                        "update_symbols: %s dihapus dari watchlist tapi masih punya posisi — pertahankan tracker",
                        sym,
                    )

            # Merge: pertahankan koin yang masih punya posisi terbuka
            active_with_position = [
                s for s in self.symbols
                if s not in new_symbols and self._in_position.get(s, False)
            ]
            self.symbols = list(new_symbols) + active_with_position

        # Load profile untuk koin baru saja (bukan semua)
        # Akses _profiles di dalam lock untuk hindari race condition
        with self._lock:
            new_only = [s for s in new_symbols if s not in self._profiles]
        if new_only:
            self._load_profiles(new_only)
        log.info(
            "VolumetricBreakoutStrategy symbols updated: %d koin "
            "(+%d baru, %d dihapus, %d retained karena posisi aktif)",
            len(self.symbols), len(new_only),
            len(removed), len(active_with_position),
        )

    def _try_init_pipeline(self) -> None:
        try:
            from engine.intelligence.observer import MarketObserver
            from engine.intelligence.classifier import MarketClassifier
            from engine.intelligence.scorer import SignalScorer
            from engine.intelligence.validator import SignalValidator

            self._observer   = MarketObserver()
            self._classifier = MarketClassifier()
            self._scorer     = SignalScorer(db_manager=self._db)
            self._validator  = SignalValidator()
            self._pipeline_ready = True
            log.info(
                "Intelligence pipeline READY — "
                "observer, classifier, scorer, validator loaded."
            )
        except ImportError as e:
            log.warning(
                "Intelligence pipeline tidak tersedia (%s) — "
                "fallback ke mode legacy (logika lama).", e
            )
            self._pipeline_ready = False

    def _load_profiles(self, symbols: List[str]) -> None:
        from engine.profiles.registry import auto_classify_profile
        for sym in symbols:
            try:
                base = sym.split("/")[0]
                # Cek apakah koin ada di map — kalau tidak, auto-classify
                from engine.profiles.registry import _COIN_PROFILE_MAP
                if base not in _COIN_PROFILE_MAP and self._ws_feed is not None:
                    try:
                        ticker     = self._ws_feed.live_tickers.get(sym, {})
                        spread_pct = self._ws_feed.get_current_spread_pct(sym) or 0.0
                        auto_classify_profile(base, ticker, spread_pct)
                        log.info("AutoClassify selesai untuk %s — lanjut load profile", sym)
                    except Exception as ac_err:
                        log.debug("AutoClassify gagal untuk %s: %s — pakai conservative", sym, ac_err)

                profile = get_coin_profile(sym)
                with self._lock:
                    self._profiles[sym] = profile
                log.info(
                    "Profile: %s → %s | TF=%s SL=%.1f%% TP=%.1f%% vol=%.1fx",
                    sym, profile.profile.value, profile.timeframe,
                    profile.quick_sl_pct, profile.quick_tp_pct,
                    profile.volume_mult,
                )
            except Exception as exc:
                log.warning(
                    "Profile load gagal untuk %s: %s — pakai universal defaults",
                    sym, exc,
                )
                self._profiles[sym] = None

    def get_profile(self, symbol: str) -> Optional[CoinProfile]:
        with self._lock:
            return self._profiles.get(symbol)

    def get_symbol_timeframe(self, symbol: str) -> str:
        # [BUG-FIX] Sebelumnya: akses self._profiles tanpa self._lock,
        # tidak konsisten dengan get_profile/_resolve_params yang selalu
        # pakai lock untuk akses dict yang sama. Risiko race ringan (baca
        # data usang sepersekian detik saat _profiles sedang ditulis oleh
        # thread lain, misal _load_profiles/refresh_profiles). Tambah lock
        # agar konsisten dan defensif.
        with self._lock:
            profile = self._profiles.get(symbol)
        if profile is not None:
            return profile.timeframe
        return self.timeframe

    def _resolve_params(
        self,
        symbol:    str,
        close:     float,
        atr:       float,
        vol_ratio: float,
        rsi:       float,
    ) -> Dict:
        with self._lock:
            profile = self._profiles.get(symbol)

        if profile is None:
            return dict(self.params)

        base: Dict = {
            "lookback":                _UNIVERSAL_DEFAULTS["lookback"],
            "volume_multiplier":       profile.volume_mult,
            "volume_spike_threshold":  profile.volume_spike,
            "rsi_min":                 profile.rsi_min,
            "rsi_max":                 profile.rsi_max,
            "rsi_golden_cross_min":    profile.rsi_gc_min,
            "min_breakout_pct":        profile.min_breakout_pct,
            "atr_sl_mult":             profile.atr_sl_mult,
            "atr_tp_mult":             profile.atr_tp_mult,
            "atr_pct_threshold":       profile.atr_pct_threshold,
            "quick_sl_pct":            profile.quick_sl_pct,
            "quick_tp_pct":            profile.quick_tp_pct,
            "trailing_activation_pct": profile.trailing_act_pct,
            "trailing_gap_pct":        profile.trailing_gap_pct,
            "max_hold_seconds":        profile.max_hold_seconds,
            "min_candles":             self.params.get("min_candles", 60),
            "sentiment_enabled":       self.params.get("sentiment_enabled", True),
            "use_quote_volume":        self.params.get("use_quote_volume", True),
            "_adaptive_mode":          "N/A",
            "_atr_ratio":              1.0,
        }

        try:
            adaptive = AdaptiveParams.adjust_for_market(
                profile=profile,
                cur_atr=atr,
                cur_price=close,
                cur_vol_ratio=vol_ratio,
                cur_rsi=rsi,
            )
            base["rsi_min"]           = adaptive["rsi_min"]
            base["rsi_max"]           = adaptive["rsi_max"]
            base["volume_multiplier"] = adaptive["vol_threshold"]
            base["atr_sl_mult"]       = adaptive["sl_mult"]
            base["atr_tp_mult"]       = adaptive["tp_mult"]
            base["_adaptive_mode"]    = adaptive.get("adaptive_mode", "NORMAL")
            base["_atr_ratio"]        = adaptive.get("atr_ratio", 1.0)
        except Exception as exc:
            log.debug("[%s] AdaptiveParams gagal: %s — pakai profile base", symbol, exc)

        return base

    def sync_position_state(
        self,
        open_symbols:   Set[str],
        open_positions: List = None,
    ) -> None:
        with self._lock:
            for sym in self.symbols:
                was = self._in_position.get(sym, False)
                now = sym in open_symbols
                self._in_position[sym] = now

                if was and not now:
                    self._pos_trackers.pop(sym, None)
                    self._last_entry_params.pop(sym, None)
                    log.info("sync: %s posisi ditutup dari luar (SL/TP exchange)", sym)
                elif now and sym not in self._pos_trackers:
                    pos_data = None
                    if open_positions:
                        pos_data = next(
                            (p for p in open_positions if p.symbol == sym), None
                        )

                    if pos_data:
                        # [BUG-FIX -- ditemukan lewat eksperimen putaran 2]
                        # Sebelumnya: entry_price = float(pos_data.entry_price
                        # or 0) -- kalau pos_data.entry_price None/0/corrupt
                        # dari DB, tracker tetap dibuat dgn entry_price=0.0.
                        # Tracker ini lalu dipakai di banyak tempat hilir
                        # (check_trailing_exit, _handle_regime_transition,
                        # _generate_signals_v7/_legacy) yang menghitung
                        # profit_pct = (price - tracker.entry_price) /
                        # tracker.entry_price * 100 TANPA guard entry_price<=0
                        # -- ZeroDivisionError nyata (dibuktikan via eksperimen:
                        # sync_position_state dgn pos_data.entry_price=None ->
                        # tracker.entry_price=0.0 -> check_trailing_exit crash).
                        # Karena run_sl_tp_monitor di main.py membungkus SATU
                        # try/except di LUAR seluruh for-loop posisi (bukan per
                        # posisi), crash pada SATU posisi dgn data korup akan
                        # menghentikan pemrosesan SL/TP utk SEMUA posisi lain
                        # di siklus itu -- data korup pada 1 record bisa
                        # mengganggu proteksi SL/TP posisi sehat lainnya. Fix
                        # root-cause: JANGAN buat tracker kalau entry_price
                        # tidak valid -- log warning jelas & skip, supaya
                        # operator sadar ada data korup yang perlu dibenahi,
                        # daripada diam-diam membuat tracker rusak yang crash
                        # nanti di tempat lain.
                        raw_entry_price = float(pos_data.entry_price or 0)
                        if raw_entry_price <= 0:
                            log.error(
                                "sync: %s punya entry_price tidak valid dari DB "
                                "(%s) — tracker TIDAK dibuat (posisi ini tidak "
                                "akan dikawal trailing/regime-transition sampai "
                                "data diperbaiki manual). Cek record posisi di DB.",
                                sym, pos_data.entry_price,
                            )
                        else:
                            entry_price  = raw_entry_price
                            profile = self._profiles.get(sym)
                            atr_at_entry = float(pos_data.atr_at_entry or 0)
                            p = self._resolve_params(sym, entry_price, atr_at_entry, 1.0, 55.0)

                            tracker = PositionTracker(
                                symbol=sym,
                                entry_price=entry_price,
                                entry_time=pos_data.entry_time or _utcnow(),
                                exit_mode=ExitMode.QUICK_PROFIT,
                                highest_price=float(
                                    pos_data.current_price or pos_data.entry_price
                                ),
                                # [BUG-FIX] Sebelumnya side TIDAK diteruskan di sini
                                # -- tracker hasil restore SELALU dapat default
                                # side="long" dari PositionTracker, apapun side
                                # asli posisi di DB. Tidak berdampak nyata di spot
                                # (semua posisi memang selalu long), TAPI akan jadi
                                # bug nyata kalau future/main_future.py suatu saat
                                # memanggil sync_position_state() untuk posisi short
                                # yang di-restore dari DB (mis. setelah bot restart).
                                side=str(getattr(pos_data, "side", None) or "long"),
                                trailing_active=False,
                                quick_tp_pct=p.get("quick_tp_pct", 1.75),
                                quick_sl_pct=p.get("quick_sl_pct", 1.20),
                                atr_sl_mult=p.get("atr_sl_mult", 2.0),
                                trailing_gap_pct=p.get("trailing_gap_pct", 0.50),
                                activation_pct=p.get("trailing_activation_pct", 1.50),
                                max_hold_seconds=p.get("max_hold_seconds", 0),
                                profile_name=profile.profile.value if profile else "universal",
                                entry_regime=str(getattr(pos_data, "entry_regime", None) or "undefined"),
                            )
                            self._pos_trackers[sym] = tracker
                            log.info(
                                "sync: %s posisi lama di-restore ke tracker "
                                "(entry=%.6f max_hold_secs=%d)",
                                sym, entry_price, tracker.max_hold_seconds,
                            )
                    elif not was:
                        log.info(
                            "sync: %s posisi dibuka dari luar (tidak ada data DB)", sym
                        )

    def check_trailing_exit(
        self, symbol: str, current_price: float
    ) -> Optional[str]:
        with self._lock:
            tracker = self._pos_trackers.get(symbol)

        if not tracker or tracker.exit_mode != ExitMode.RIDE_THE_WAVE:
            return None

        # [BUG-FIX -- HARDENING] Guard defensif tambahan: kalau entry_price
        # entah bagaimana tetap <=0 (mis. register_position dipanggil dgn
        # entry_price salah di masa depan), jangan crash ZeroDivisionError --
        # root-cause utama sudah difix di sync_position_state (tidak lagi
        # membuat tracker dgn entry_price<=0), ini cuma jaring pengaman kedua.
        if tracker.entry_price <= 0:
            log.error(
                "check_trailing_exit: %s tracker.entry_price tidak valid (%s) "
                "— skip perhitungan trailing (data tracker korup).",
                symbol, tracker.entry_price,
            )
            return None

        # [FUTURES-READY] Side-aware. Untuk side="long" (default & SATU-SATUNYA
        # kondisi yang pernah berjalan di produksi sampai saat ini), seluruh
        # formula di bawah IDENTIK PERSIS dengan versi sebelumnya -- tidak ada
        # perubahan behavior. Cabang "short" disiapkan untuk futures nanti,
        # tapi belum pernah dieksekusi karena tidak ada tracker yang side-nya
        # "short" sampai saat ini (register_position() selalu default "long").
        is_long = tracker.side != "short"

        with self._lock:
            if is_long:
                if current_price > tracker.highest_price:
                    tracker.highest_price = current_price
            else:
                # Untuk short, "highest_price" field yang sama dipakai untuk
                # menyimpan harga TERENDAH yang pernah tercapai (favorable
                # extreme untuk posisi short) -- nama field dipertahankan
                # agar skema tracker tidak berubah, maknanya kontekstual
                # sesuai side.
                if current_price < tracker.highest_price:
                    tracker.highest_price = current_price

            if not tracker.trailing_active:
                if is_long:
                    profit_pct = (
                        (current_price - tracker.entry_price)
                        / tracker.entry_price * 100
                    )
                else:
                    profit_pct = (
                        (tracker.entry_price - current_price)
                        / tracker.entry_price * 100
                    )
                if profit_pct >= tracker.activation_pct:
                    tracker.trailing_active = True
                    log.info(
                        "Trailing AKTIF: %s profit=%.2f%% ≥ %.1f%% | high=%.6f",
                        symbol, profit_pct, tracker.activation_pct, current_price,
                    )

            if not tracker.trailing_active:
                return None

            if is_long:
                trail_sl = tracker.highest_price * (1 - tracker.trailing_gap_pct / 100)
            else:
                trail_sl = tracker.highest_price * (1 + tracker.trailing_gap_pct / 100)

        hit_trail = (
            (current_price <= trail_sl) if is_long else (current_price >= trail_sl)
        )
        if hit_trail:
            if is_long:
                profit_pct = (
                    (current_price - tracker.entry_price) / tracker.entry_price * 100
                )
            else:
                profit_pct = (
                    (tracker.entry_price - current_price) / tracker.entry_price * 100
                )
            reason = (
                f"TrailingExit("
                f"high={tracker.highest_price:.6f},"
                f"trail_sl={trail_sl:.6f},"
                f"gap={tracker.trailing_gap_pct:.1f}%,"
                f"profit={profit_pct:+.2f}%)"
            )
            log.info("TRAILING EXIT: %s @ %.6f | %s", symbol, current_price, reason)
            return reason

        return None

    def _handle_regime_transition(
        self,
        tracker:        "PositionTracker",
        current_regime: str,
    ) -> str:
        """Engine utama transisi regime untuk posisi open.
        Return: HOLD | HOLD_TIGHTEN_SL | HOLD_RELAX_SL | EXIT
        """
        from engine.constants import REGIME_ACTION_COOLDOWN_SECS, REGIME_STABILITY_MIN_CYCLES
        from engine.intelligence.commander import should_exit_on_regime_change
        from engine.core.models import MarketRegime

        with self._lock:
            symbol       = tracker.symbol
            entry_regime = tracker.entry_regime
            profile_name = tracker.profile_name
            now          = _utcnow()

            # Lapis 3: cooldown — jangan aksi terlalu sering
            if tracker.last_regime_action_time is not None:
                elapsed = (now - tracker.last_regime_action_time).total_seconds()
                if elapsed < REGIME_ACTION_COOLDOWN_SECS:
                    return "HOLD"

            # Lapis 2: stability — regime baru harus stabil dulu
            # Reset counter kalau regime berubah dari yang terakhir dilihat
            if current_regime != tracker.last_seen_regime:
                tracker.last_seen_regime       = current_regime
                tracker.regime_stability_count = 1
                return "HOLD"
            if tracker.regime_stability_count < REGIME_STABILITY_MIN_CYCLES:
                tracker.regime_stability_count += 1
                return "HOLD"

        # Konsultasi matrix (di luar lock — tidak modifikasi state)
        try:
            entry_mr   = MarketRegime(entry_regime)
            current_mr = MarketRegime(current_regime)
        except ValueError:
            return "HOLD"

        _, reason, action = should_exit_on_regime_change(
            symbol=symbol,
            current_regime=current_mr,
            entry_regime=entry_mr,
            profile_name=profile_name,
        )

        with self._lock:
            if action in ("HOLD_TIGHTEN_SL", "HOLD_RELAX_SL", "EXIT"):
                tracker.last_regime_action      = action
                tracker.last_regime_action_time = now
                tracker.regime_stability_count  = 0
                tracker.regime_action_log.append({
                    "time":    now.isoformat(),
                    "from":    entry_regime,
                    "to":      current_regime,
                    "action":  action,
                    "reason":  reason,
                })
                log.info("[%s] Regime transition action: %s (%s)", symbol, action, reason)

            if action == "HOLD_TIGHTEN_SL" and not tracker.sl_tightened:
                profile = self._profiles.get(symbol)
                tighten_pct = getattr(profile, "regime_transition_sl_tighten_pct", 0.30) if profile else 0.30
                tracker.quick_sl_pct = max(
                    tracker.quick_sl_pct * (1.0 - tighten_pct), 0.30
                )
                tracker.sl_tightened = True
                tracker.sl_relaxed   = False

            elif action == "HOLD_RELAX_SL" and not tracker.sl_relaxed:
                # [BUG-FIX] Sebelumnya: relax melipatgandakan quick_sl_pct
                # SAAT INI dengan (1+relax_pct). Karena tighten memakai
                # (1-tighten_pct) dan relax memakai (1+relax_pct), hasil
                # kali kedua faktor itu (cth 0.7 x 1.2 = 0.84) TIDAK PERNAH
                # persis 1.0 — jadi tiap siklus tighten→relax bergantian,
                # quick_sl_pct menyusut progresif sampai mengenai floor
                # 0.30%, walau kondisi terakhir adalah RELAX (regime sudah
                # balik mendukung posisi). Ditemukan lewat simulasi: 7
                # siklus tighten-relax bergantian sudah membuat SL mengenai
                # floor — realistis terjadi karena cooldown cuma 120 detik.
                # Sekarang: relax SELALU mengembalikan persis ke
                # original_quick_sl_pct (SL saat posisi pertama dibuka),
                # bukan menghitung ulang dari nilai saat ini. Predictable:
                # "regime balik mendukung" = "SL balik ke rencana awal",
                # tidak bergantung riwayat berapa kali tighten/relax sudah
                # terjadi sebelumnya.
                tracker.quick_sl_pct = tracker.original_quick_sl_pct
                tracker.sl_relaxed   = True
                tracker.sl_tightened = False

        return action

    def register_position(
        self,
        symbol:      str,
        entry_price: float,
        exit_mode:   ExitMode,
        p:           Dict,
        entry_score: float = 0.0,
        entry_regime: str  = "undefined",
        side:        str   = "long",
    ) -> None:
        profile      = self._profiles.get(symbol)
        profile_name = profile.profile.value if profile else "universal"

        tracker = PositionTracker(
            symbol=symbol,
            entry_price=entry_price,
            entry_time=_utcnow(),
            exit_mode=exit_mode,
            highest_price=entry_price,
            side=side,
            trailing_active=False,
            quick_tp_pct=p["quick_tp_pct"],
            quick_sl_pct=p["quick_sl_pct"],
            atr_sl_mult=p["atr_sl_mult"],
            trailing_gap_pct=p["trailing_gap_pct"],
            activation_pct=p["trailing_activation_pct"],
            max_hold_seconds=p.get("max_hold_seconds", 0),
            candles_held=0,
            profile_name=profile_name,
            entry_score=entry_score,
            entry_regime=entry_regime,
        )

        with self._lock:
            self._pos_trackers[symbol] = tracker
            self._in_position[symbol]  = True
            self._pending_entry.discard(symbol)

        log.info(
            "Position registered: %s @ %.6f | mode=%s profile=%s "
            "score=%.1f regime=%s max_hold_secs=%d",
            symbol, entry_price, exit_mode.value, profile_name,
            entry_score, entry_regime, tracker.max_hold_seconds,
        )

    def unregister_position(self, symbol: str) -> None:
        with self._lock:
            self._pos_trackers.pop(symbol, None)
            self._in_position[symbol] = False
            self._last_entry_params.pop(symbol, None)
            self._pending_entry.discard(symbol)

    def get_exit_mode(self, symbol: str) -> Optional[ExitMode]:
        with self._lock:
            tracker = self._pos_trackers.get(symbol)
        return tracker.exit_mode if tracker else None

    def get_tracker(self, symbol: str) -> Optional[PositionTracker]:
        with self._lock:
            return self._pos_trackers.get(symbol)

    async def get_scored_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        confirmation_df: Optional[pd.DataFrame] = None,
        confirmation_timeframe: Optional[str] = None,
        ob_data: Optional[dict] = None,
        side: str = "long",
        # [FUTURES-READY] side="long" default -- IDENTIK PERSIS dgn sebelum
        # parameter ini ada. Diteruskan ke scorer & validator (keduanya sudah
        # side-aware sejak perbaikan bias long-only) supaya scoring untuk
        # kandidat short bisa benar-benar dievaluasi, bukan cuma dari sisi long.
    ) -> Optional["ScoredSignal"]:
        if not self._pipeline_ready:
            log.debug(
                "[%s] Pipeline tidak ready — get_scored_signal return None", symbol
            )
            return None

        try:
            profile = self._profiles.get(symbol)
            if profile is None:
                log.debug("[%s] Tidak ada profile — skip pipeline", symbol)
                return None

            observation = await asyncio.get_running_loop().run_in_executor(
                None,
                self._observer.observe,
                symbol,
                df,
                profile,
                confirmation_df,
                confirmation_timeframe,
                ob_data,
            )

            if observation is None or not observation.is_tradeable():
                log.debug(
                    "[%s] ObservationReport tidak tradeable — skip scoring", symbol
                )
                return None

            _db = getattr(self, '_db', None)
            regime, regime_confidence = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._classifier.classify(observation, db_manager=None),
            )
            # Simpan regime ke DB langsung di async context
            if _db is not None:
                try:
                    iset = observation.primary_tf_indicators
                    await _db.save_market_regime(
                        symbol=symbol,
                        timeframe=iset.timeframe if iset else self.get_symbol_timeframe(symbol),
                        regime=regime,
                        regime_confidence=regime_confidence,
                        adx_value=iset.strength.adx if iset else None,
                        atr_pct=iset.volatility.atr_pct if iset else None,
                        bb_width=iset.volatility.bb_width if iset else None,
                        ema_stack_score=iset.trend.ema_stack_score if iset else None,
                    )
                except Exception as _re:
                    log.debug("Gagal simpan regime ke DB: %s", _re)

            # Deteksi perubahan regime dan kirim notifikasi
            prev_regime = self._last_regime.get(symbol)
            if prev_regime and prev_regime != "undefined" and regime != prev_regime and regime != "undefined":
                log.info("[%s] Regime change: %s → %s (confidence=%.2f)", symbol, prev_regime, regime, regime_confidence)
                if hasattr(self, '_notifier') and self._notifier:
                    try:
                        await self._notifier.notify_regime_change(symbol=symbol, old_regime=prev_regime, new_regime=regime, confidence=regime_confidence)
                    except Exception as _re:
                        log.debug("notify_regime_change gagal: %s", _re)
            self._last_regime[symbol] = regime

            # Inject adaptive RSI ke profile sebelum scoring
            try:
                import copy
                adaptive_profile = copy.copy(profile)
                _atr = observation.primary_tf_indicators.volatility.atr if observation.primary_tf_indicators else None
                _price = observation.primary_tf_indicators.current_price if observation.primary_tf_indicators else None
                _vol = observation.primary_tf_indicators.strength.volume_ratio if observation.primary_tf_indicators else None
                _rsi = observation.primary_tf_indicators.momentum.rsi if observation.primary_tf_indicators else None
                if all(v is not None for v in [_atr, _price, _vol, _rsi]):
                    from engine.profiles.base_profile import AdaptiveParams
                    _adaptive = AdaptiveParams.adjust_for_market(
                        profile=profile,
                        cur_atr=_atr,
                        cur_price=_price,
                        cur_vol_ratio=_vol,
                        cur_rsi=_rsi,
                    )
                    adaptive_profile.rsi_min = _adaptive["rsi_min"]
                    adaptive_profile.rsi_max = _adaptive["rsi_max"]
                    log.debug(
                        "[%s] Adaptive RSI inject: rsi_min %.1f→%.1f rsi_max %.1f→%.1f mode=%s",
                        symbol, profile.rsi_min, adaptive_profile.rsi_min,
                        profile.rsi_max, adaptive_profile.rsi_max,
                        _adaptive.get("adaptive_mode", "?")
                    )
            except Exception as _e:
                adaptive_profile = profile
                log.debug("[%s] Adaptive inject gagal: %s — pakai profile statis", symbol, _e)

            # [BUG-FIX] self._scorer.score() (-> score_signal() ->
            # _save_score_to_db()) dijalankan lewat run_in_executor di WORKER
            # THREAD -- di sana asyncio.get_event_loop() di dalam
            # _save_score_to_db selalu RuntimeError sehingga penyimpanan
            # signal_scores utk jalur ini (SKIP_INVALID_DATA/REJECT_BEAR_REGIME/
            # NO_TRIGGER/EXECUTE_CANDIDATE/HOLD) selalu gagal diam-diam
            # (dibuktikan via eksperimen). Fix root-cause: oper referensi loop
            # yang benar (didapat di main thread, sebelum masuk executor) supaya
            # _save_score_to_db bisa menjadwalkan _persist() dgn benar dari
            # thread manapun via run_coroutine_threadsafe.
            _main_loop = asyncio.get_running_loop()
            scored = await _main_loop.run_in_executor(
                None,
                self._scorer.score,
                observation,
                adaptive_profile,
                regime,
                regime_confidence,
                _main_loop,
                side,
            )

            if scored is None:
                return None

            # Hard MTF gate: if confirmation data exists, enforce score/valid threshold.
            if confirmation_df is not None and confirmation_timeframe:
                if (not observation.confirmation_tf_valid) or (
                    float(observation.confirmation_tf_score or 0.0) < float(profile.confirmation_min_score)
                ):
                    log.debug(
                        "[%s] MTF gate blocked: conf_valid=%s conf_score=%.1f < min=%.1f",
                        symbol,
                        observation.confirmation_tf_valid,
                        float(observation.confirmation_tf_score or 0.0),
                        float(profile.confirmation_min_score),
                    )
                    return None

            # [BUG-FIX] validate_and_apply() sebelumnya dipanggil TANPA
            # db_manager (default None) -- akibatnya _check_consecutive_losses()
            # di dalam validate_signal() SELALU return di baris pertama
            # (if db_manager is None: return), sehingga fitur "consecutive
            # losses fatigue penalty" tidak pernah aktif di jalur produksi
            # sama sekali walau sudah diimplementasikan lengkap & benar.
            # Fix: oper _db yang sudah didapat di atas. asyncio.run() di dalam
            # _check_consecutive_losses AMAN dipanggil dari worker thread
            # (beda kasus dgn asyncio.get_event_loop() di scorer.py yg sudah
            # difix terpisah) -- sudah diverifikasi tidak ada masalah runtime,
            # cuma nambah 1 query DB per simbol per siklus scoring (trade-off
            # yg disetujui user).
            from engine.intelligence.validator import validate_and_apply
            loop = asyncio.get_running_loop()
            scored, _vr = await loop.run_in_executor(
                None,
                validate_and_apply,
                scored,
                _db,
                side,
            )

            # [TAMBAHAN] Logging pasif sentiment_score untuk pipeline v7.
            # Sebelumnya: sentiment (Fear & Greed Index) HANYA dipakai
            # sebagai gate biner di mode legacy — pipeline v7 (jalur utama)
            # sama sekali tidak mempertimbangkan sentiment makro dalam
            # keputusan apapun. Setelah analisis (resolusi data F&G adalah
            # harian-makro vs 8 kategori scoring v7 yang granular per-candle,
            # berisiko jadi noise; juga kemungkinan redundan dengan regime
            # classifier yang sudah menangkap kondisi bearish/bullish dari
            # indikator teknikal sendiri), keputusan SAAT INI adalah TIDAK
            # menyambungkan sentiment ke scoring/gate — risiko mengencerkan
            # sistem yang sudah dikalibrasi cermat tanpa bukti manfaat.
            # Sebagai gantinya: kumpulkan data dulu (logging murni, TIDAK
            # memengaruhi total_score/trigger_met/keputusan apapun) supaya
            # nanti bisa dianalisis apakah ada korelasi nyata antara
            # sentiment saat entry dan win rate, sebelum keputusan lebih
            # jauh diambil berdasarkan data, bukan asumsi.
            _sentiment_for_log: Optional[float] = None
            try:
                _sentiment_for_log = await asyncio.wait_for(
                    check_market_sentiment(symbol), timeout=3.0
                )
            except Exception as _sent_err:
                log.debug("[%s] Sentiment fetch utk logging gagal: %s", symbol, _sent_err)

            # Simpan ke signal_scores dari event loop utama (bukan dari thread)
            if self._db is not None and scored is not None:
                try:
                    await self._db.save_signal_score(
                        symbol=scored.symbol,
                        strategy_profile=scored.strategy_profile,
                        total_score=scored.total_score,
                        trend_score=scored.score_breakdown.trend_raw if scored.score_breakdown else None,
                        momentum_score=scored.score_breakdown.momentum_raw if scored.score_breakdown else None,
                        strength_score=scored.score_breakdown.strength_raw if scored.score_breakdown else None,
                        volatility_score=scored.score_breakdown.volatility_raw if scored.score_breakdown else None,
                        pattern_score=scored.score_breakdown.pattern_raw if scored.score_breakdown else None,
                        oscillator_score=scored.score_breakdown.oscillator_raw if scored.score_breakdown else None,
                        structure_score=scored.score_breakdown.structure_raw if scored.score_breakdown else None,
                        orderbook_score=scored.score_breakdown.orderbook_raw if scored.score_breakdown else None,
                        threshold_used=scored.threshold_used,
                        regime=scored.regime.value if scored.regime else "undefined",
                        regime_confidence=getattr(scored, "regime_confidence", None),
                        trigger_met=scored.trigger_met,
                        signal_type=scored.signal_type,
                        action_taken="PIPELINE",
                        current_price=getattr(scored.observation.primary_tf_indicators, "current_price", None) if scored.observation and scored.observation.primary_tf_indicators else None,
                        suggested_sl=scored.suggested_sl,
                        suggested_tp=scored.suggested_tp,
                        signal_confidence=getattr(scored, "confidence", None),
                        sentiment_score=_sentiment_for_log,
                    )
                except Exception as _se:
                    log.debug("Gagal save signal_score dari strategy: %s", _se)
            return scored

        except Exception as exc:
            log.error(
                "Intelligence pipeline error [%s]: %s",
                symbol, exc, exc_info=True,
            )
            return None

    def get_position_summary(self) -> List[Dict]:
        summary = []
        with self._lock:
            for sym, tracker in self._pos_trackers.items():
                now     = _utcnow()
                elapsed = (now - tracker.entry_time).total_seconds() / 3600
                summary.append({
                    "symbol":          sym,
                    "profile":         tracker.profile_name,
                    "entry_price":     tracker.entry_price,
                    "highest_price":   tracker.highest_price,
                    "exit_mode":       tracker.exit_mode.value,
                    "trailing_active": tracker.trailing_active,
                    "candles_held":    tracker.candles_held,
                    "max_hold_secs":   tracker.max_hold_seconds,
                    "hold_hours":      round(elapsed, 2),
                    "overtime":        tracker.is_overtime(),
                    "entry_score":     tracker.entry_score,
                    "entry_regime":    tracker.entry_regime,
                })
        return summary

    def print_profile_summary(self) -> None:
        log.info("=" * 70)
        log.info(
            "  SYMBOL PROFILES — VolumetricBreakout v%s | pipeline=%s",
            APP_VERSION,
            "READY" if self._pipeline_ready else "LEGACY",
        )
        log.info("=" * 70)
        for sym in self.symbols:
            profile = self._profiles.get(sym)
            if profile:
                log.info(
                    "  %-14s | %-22s | TF=%-4s | SL=%.1f%% TP=%.1f%% "
                    "vol=%.1fx max_hold_secs=%d",
                    sym, profile.profile.value, profile.timeframe,
                    profile.quick_sl_pct, profile.quick_tp_pct,
                    profile.volume_mult, profile.max_hold_seconds,
                )
            else:
                log.info(
                    "  %-14s | universal defaults | SL=%.1f%% TP=%.1f%%",
                    sym,
                    _UNIVERSAL_DEFAULTS["quick_sl_pct"],
                    _UNIVERSAL_DEFAULTS["quick_tp_pct"],
                )
        log.info("=" * 70)

