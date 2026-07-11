"""
constants.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

COL_EMA9   = "EMA_9"
COL_EMA21  = "EMA_21"
COL_EMA50  = "EMA_50"
COL_EMA100 = "EMA_100"
COL_EMA200 = "EMA_200"
COL_RSI = "RSI_14"
COL_ATR = "ATRr_14"
COL_ATR_PCT = "ATRr_14_pct"
COL_MACD_LINE   = "MACD_12_26_9"
COL_MACD_SIGNAL = "MACDs_12_26_9"
COL_MACD_HIST   = "MACDh_12_26_9"
COL_STOCH_K = "STOCHRSIk_14_14_3_3"
COL_STOCH_D = "STOCHRSId_14_14_3_3"
COL_ADX      = "ADX_14"
COL_PLUS_DI  = "DMP_14"
COL_MINUS_DI = "DMN_14"
COL_OBV = "OBV"
COL_MFI = "MFI_14"
COL_BB_UPPER    = "BBU_20_2.0"
COL_BB_MIDDLE   = "BBM_20_2.0"
COL_BB_LOWER    = "BBL_20_2.0"
COL_BB_WIDTH    = "BBB_20_2.0"
COL_BB_POSITION = "BBP_20_2.0"
COL_KC_UPPER  = "KCUe_20_2"
COL_KC_LOWER  = "KCLe_20_2"
COL_KC_MIDDLE = "KCBe_20_2"
COL_SQUEEZE = "SQZ_20_2.0_20_1.5"
COL_SUPERTREND     = "SUPERT_7_3.0"
COL_SUPERTREND_DIR = "SUPERTd_7_3.0"
COL_VWAP         = "VWAP_D"
COL_VWAP_UPPER_1 = "VWAP_D_upper_1"
COL_VWAP_LOWER_1 = "VWAP_D_lower_1"
COL_VWAP_UPPER_2 = "VWAP_D_upper_2"
COL_VWAP_LOWER_2 = "VWAP_D_lower_2"
COL_RESISTANCE   = "_resistance"
COL_VOL_MA       = "_vol_ma"
COL_QVOL_MA      = "_qvol_ma"
COL_OBV_MA       = "_obv_ma"
COL_ATR_PCTILE   = "_atr_percentile"
COL_ATR_PCTILE_100 = "_atr_percentile_100"

# ── Kolom v4/v5 — indikator tambahan enrich_production ────────────────────────
# Williams %R
COL_WILLR       = "WILLR_14"

# Rate of Change
COL_ROC         = "ROC_9"
COL_ROC_SLOPE   = "ROC_SLOPE_9_5"

# RSI slope & divergence
COL_RSI_SLOPE   = "RSI_14_slope"
COL_RSI_DIV     = "RSI_DIV_14"

# EMA crossover signal (+1 bullish, -1 bearish, 0 none)
COL_EMAXS_9_21  = "EMAXS_9_21"
COL_EMAXS_21_50 = "EMAXS_21_50"

# CCI
COL_CCI         = "CCI_20"

# Donchian Channel
COL_DCU         = "DCU_20"
COL_DCM         = "DCM_20"
COL_DCL         = "DCL_20"

# Chaikin Money Flow
COL_CMF         = "CMF_20"

# Parabolic SAR
COL_PSAR        = "PSAR"
COL_PSAR_DIR    = "PSAR_DIR"    # +1 = bullish trend, -1 = bearish trend
COL_PSAR_REV    = "PSAR_REV"    # 1 = reversal terjadi di bar ini, 0 = tidak

# Ichimoku
COL_ICH_TENKAN  = "ICH_TENKAN"
COL_ICH_KIJUN   = "ICH_KIJUN"
COL_ICH_SPAN_A  = "ICH_SPAN_A"
COL_ICH_SPAN_B  = "ICH_SPAN_B"
COL_ICH_CHIKOU  = "ICH_CHIKOU"

# VWMA
COL_VWMA        = "VWMA_20"

# EMA stack alignment score (0–100)
COL_EMA_STACK_SCORE = "_ema_stack_score"

REQUIRED_INDICATOR_COLS: Tuple[str, ...] = (
    COL_EMA9,
    COL_EMA21,
    COL_EMA50,
    COL_RSI,
    COL_ATR,
)

# [CATATAN] PRODUCTION_INDICATOR_COLS ini adalah referensi dokumentasi kolom
# yang dihasilkan enrich_production() — namun saat ini TIDAK DIPAKAI di manapun
# dalam codebase (grep: tidak ada caller selain definisi ini sendiri).
# Komentar "Referensikan via konstanta ini, BUKAN hardcode string literal" tidak
# akurat karena kode aktual tidak pernah menggunakan list ini untuk validasi
# atau filtering — setiap indikator mengakses kolomnya sendiri langsung.
# Juga: beberapa kolom penting yang di-generate ta_compat.py tidak ada di sini
# (DMP_14, DMN_14, VWMA_20, ICH_*, OBV_MA, VOL_MA, dll) — list ini tidak lengkap.
# Dipertahankan sebagai dokumentasi referensi saja, bukan untuk dipakai di runtime.
PRODUCTION_INDICATOR_COLS: Tuple[str, ...] = (
    # EMA stack
    COL_EMA9, COL_EMA21, COL_EMA50, COL_EMA100, COL_EMA200,
    COL_EMA_STACK_SCORE, COL_EMAXS_9_21, COL_EMAXS_21_50,
    # Momentum
    COL_RSI, COL_RSI_SLOPE, COL_RSI_DIV,
    COL_MACD_LINE, COL_MACD_SIGNAL, COL_MACD_HIST,
    COL_STOCH_K, COL_STOCH_D,
    COL_CCI, COL_WILLR, COL_ROC, COL_ROC_SLOPE,
    # Volatility
    COL_ATR, COL_ATR_PCT, COL_ATR_PCTILE_100,
    COL_BB_UPPER, COL_BB_MIDDLE, COL_BB_LOWER, COL_BB_WIDTH, COL_BB_POSITION,
    COL_KC_UPPER, COL_KC_MIDDLE, COL_KC_LOWER,
    COL_SQUEEZE, COL_ADX,
    # Structure
    COL_SUPERTREND, COL_SUPERTREND_DIR,
    COL_DCU, COL_DCM, COL_DCL,
    COL_PSAR, COL_PSAR_DIR, COL_PSAR_REV,
    # Strength
    COL_OBV, COL_MFI, COL_CMF,
    # VWAP
    COL_VWAP, COL_VWAP_UPPER_1, COL_VWAP_LOWER_1,
    COL_VWAP_UPPER_2, COL_VWAP_LOWER_2,
)

TREND_REQUIRED_COLS: Tuple[str, ...] = (
    COL_EMA9,
    COL_EMA21,
    COL_EMA50,
)

TREND_FULL_COLS: Tuple[str, ...] = (
    COL_EMA9,
    COL_EMA21,
    COL_EMA50,
    COL_EMA100,
    COL_EMA200,
    COL_SUPERTREND,
    COL_SUPERTREND_DIR,
    COL_VWAP,
)

MOMENTUM_REQUIRED_COLS: Tuple[str, ...] = (
    COL_RSI,
)

MOMENTUM_FULL_COLS: Tuple[str, ...] = (
    COL_RSI,
    COL_MACD_LINE,
    COL_MACD_SIGNAL,
    COL_MACD_HIST,
    COL_STOCH_K,
    COL_STOCH_D,
)

STRENGTH_REQUIRED_COLS: Tuple[str, ...] = (
    "volume",
)

STRENGTH_FULL_COLS: Tuple[str, ...] = (
    "volume",
    COL_ADX,
    COL_PLUS_DI,
    COL_MINUS_DI,
    COL_OBV,
    COL_MFI,
)

VOLATILITY_REQUIRED_COLS: Tuple[str, ...] = (
    COL_ATR,
    COL_BB_UPPER,
    COL_BB_LOWER,
    COL_BB_MIDDLE,
)

VOLATILITY_FULL_COLS: Tuple[str, ...] = (
    COL_ATR,
    COL_BB_UPPER,
    COL_BB_MIDDLE,
    COL_BB_LOWER,
    COL_BB_WIDTH,
    COL_BB_POSITION,
    COL_KC_UPPER,
    COL_KC_LOWER,
    COL_KC_MIDDLE,
    COL_SQUEEZE,
)

PATTERN_REQUIRED_COLS: Tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
)

ALL_INDICATOR_COLS: Tuple[str, ...] = (
    *TREND_FULL_COLS,
    *MOMENTUM_FULL_COLS,
    *STRENGTH_FULL_COLS,
    *VOLATILITY_FULL_COLS,
)

SCORE_MIN     = 0.0
SCORE_MAX     = 100.0
SCORE_NEUTRAL = 50.0
RSI_OVERSOLD_EXTREME  = 20.0
RSI_OVERSOLD          = 30.0
RSI_BULL_ZONE_LOW     = 40.0
RSI_BULL_ZONE_CENTER  = 55.0
RSI_OVERBOUGHT        = 70.0
RSI_OVERBOUGHT_EXTREME = 80.0
RSI_DIVERGENCE_THRESHOLD = 5.0
RSI_SLOPE_STRONG_UP   = 2.0
RSI_SLOPE_STRONG_DOWN = -2.0
MACD_HIST_STRONG_POSITIVE = 0.0
MACD_HIST_REVERSAL_MIN_BARS = 1
STOCH_OVERSOLD   = 20.0
STOCH_OVERBOUGHT = 80.0
STOCH_CROSS_ZONE_BONUS = 15.0
ADX_WEAK_TREND    = 20.0
ADX_MODERATE_TREND = 25.0
ADX_STRONG_TREND  = 35.0
ADX_VERY_STRONG   = 50.0

EMA_STACK_PAIRS = (
    (COL_EMA9,  COL_EMA21),
    (COL_EMA21, COL_EMA50),
    (COL_EMA50, COL_EMA100),
    (COL_EMA100, COL_EMA200),
)

EMA_STACK_WEIGHTS: Dict[int, float] = {
    0: 30.0,
    1: 30.0,
    2: 20.0,
    3: 20.0,
}

EMA_GAP_BONUS_MAX = 10.0
SUPERTREND_BULL_SCORE = 85.0
SUPERTREND_BEAR_SCORE = 15.0
VOLUME_RATIO_WEAK     = 0.8
VOLUME_RATIO_NORMAL   = 1.0
VOLUME_RATIO_ELEVATED = 1.5
VOLUME_RATIO_STRONG   = 2.0
VOLUME_RATIO_SPIKE    = 3.0
VOLUME_RATIO_CLIMAX   = 5.0
VOLUME_CLIMAX_PENALTY = 20.0
BB_WIDTH_SQUEEZE    = 0.02
BB_WIDTH_NORMAL     = 0.05
BB_WIDTH_EXPANSION  = 0.10
BB_POS_BUY_ZONE     = 0.35
BB_POS_NEUTRAL_LOW  = 0.35
BB_POS_NEUTRAL_HIGH = 0.65
BB_POS_SELL_ZONE    = 0.65
ATR_PERCENTILE_LOW    = 25.0
ATR_PERCENTILE_NORMAL = 50.0
ATR_PERCENTILE_HIGH   = 75.0
ATR_PERCENTILE_VERY_HIGH = 90.0
PATTERN_BASE_SCORE_ENGULFING   = 75.0
PATTERN_BASE_SCORE_HAMMER      = 70.0
PATTERN_BASE_SCORE_DOJI        = 55.0
PATTERN_BASE_SCORE_MARUBOZU    = 80.0
PATTERN_VOLUME_CONFIRM_BONUS   = 15.0
PATTERN_CONTEXT_SUPPORT_BONUS  = 15.0
PATTERN_HIGHER_TF_ALIGN_BONUS  = 10.0
PATTERN_NO_CONFIRM_PENALTY     = -20.0
PATTERN_MIN_BODY_PCT_ENGULFING = 0.40
PATTERN_MIN_BODY_PCT_HAMMER    = 0.10
PATTERN_DOJI_MAX_BODY_PCT      = 0.05
PATTERN_HAMMER_SHADOW_RATIO    = 2.0
PATTERN_HAMMER_UPPER_SHADOW_MAX = 0.5
MFI_OVERSOLD   = 20.0
MFI_OVERBOUGHT = 80.0

REGIME_SCORE_MODIFIERS: Dict[str, float] = {
    "trending_bull":      1.0,
    "trending_bear":      0.0,
    "ranging":            0.85,
    "volatile_expansion": 0.70,
    "undefined":          0.75,
}


MAX_CANDLE_CACHE = 200
MIN_CANDLES_FOR_INDICATORS = 60
MIN_CANDLES_FOR_EMA200 = 210
CANDLE_POLL_INTERVAL   = 10
SL_TP_CHECK_INTERVAL   = 5
SNAPSHOT_INTERVAL      = 60
DAILY_SUMMARY_HOUR     = 23
DAILY_SUMMARY_MIN      = 55
OBSERVATION_CACHE_TTL_SECONDS = 60
OBSERVATION_STALE_THRESHOLD_SECONDS = 120

TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m":  60,
    "3m":  180,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "2h":  7200,
    "4h":  14400,
    "6h":  21600,
    "8h":  28800,
    "12h": 43200,
    "1d":  86400,
    "3d":  259200,
    "1w":  604800,
}

EXCHANGE_RETRY_COUNT    = 3
EXCHANGE_RETRY_DELAY    = 1.5
SENTIMENT_API_TIMEOUT   = 5
SENTIMENT_CACHE_TTL     = 300
DIAGNOSA_API_TIMEOUT    = 60
MIN_PORTFOLIO_REFRESH_INTERVAL = 5.0
MAX_DB_SNAPSHOTS        = 10_000
MAX_DB_API_METRICS      = 1_000
MAX_DB_LOGS             = 5_000
MAX_DB_MARKET_REGIMES   = 30_000
MAX_DB_SIGNAL_SCORES    = 50_000
MAX_DB_PARAMETER_HISTORY = 1_000
SNAPSHOT_DEDUP_SECS     = 55
WS_MAX_STALE_SECS         = 30
WS_RECONNECT_DELAY        = 5
WS_MAX_RETRIES            = 10
WS_POLL_INTERVAL          = 10
ICEBERG_THRESHOLD_PCT  = 3.0
ICEBERG_CHUNK_COUNT    = 4
FILL_TIMEOUT_SECS      = 30
FILL_POLL_INTERVAL     = 2.0
MAX_SLIPPAGE_DEFAULT   = 0.5
SPREAD_LIMIT_DEFAULT   = 0.15
SIGNAL_ORIGIN_MAX_LEN  = 490

MIN_SAMPLE_FOR_ATTRIBUTION     = 30
MIN_SAMPLE_FOR_INDICATOR_STATS = 20
ANALYTICS_REFRESH_INTERVAL_S   = 3600
ANALYTICS_TRIGGER_ON_N_TRADES  = 50
WIN_RATE_ROLLING_LONG  = 30
WIN_RATE_ROLLING_SHORT = 10
INSIGHT_MIN_WIN_RATE_DIFF   = 20.0
INSIGHT_MIN_SAMPLE_SIZE     = 30
INDICATOR_PREDICTIVE_THRESHOLD = 15.0
META_LEARNER_ENABLED_DEFAULT    = False
META_LEARNER_MIN_SAMPLE         = 50
META_LEARNER_MAX_THRESHOLD_CHANGE = 10.0
META_LEARNER_COOLING_OFF_DAYS   = 14
META_LEARNER_APPROVAL_WINDOW_H  = 24
META_WIN_RATE_LOW_THRESHOLD     = 45.0
META_WIN_RATE_HIGH_THRESHOLD    = 75.0

META_PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "entry_threshold":         (55.0, 85.0),
    "volume_multiplier":       (1.0, 5.0),
    "rsi_min":                 (20.0, 60.0),  # mean_revert butuh rsi_min sampai 20.0
    "rsi_max":                 (60.0, 90.0),
    "atr_sl_mult":             (1.0, 4.0),
    "atr_tp_mult":             (1.5, 10.0),
    "trailing_activation_pct": (0.5, 5.0),
    "trailing_gap_pct":        (0.2, 3.0),
    "quick_sl_pct":            (0.5, 5.0),
    "quick_tp_pct":            (1.0, 10.0),
}

META_MIN_PROJECTED_IMPROVEMENT = 3.0
META_TRACK_TRADES_AFTER_APPLY = 30
META_REVERT_WIN_RATE_DROP = 10.0

REGIME_VOLATILE_ATR_PERCENTILE_MIN = 70.0
REGIME_VOLATILE_BB_WIDTH_MIN       = 0.06
REGIME_TRENDING_ADX_MIN            = 22.0
REGIME_TRENDING_STRONG_ADX         = 30.0
REGIME_BULL_EMA_REQUIRED_PAIRS     = 2
REGIME_BEAR_EMA_REQUIRED_PAIRS     = 2
REGIME_RANGING_ADX_MAX             = 25.0
REGIME_RANGING_BB_WIDTH_MAX        = 0.06
REGIME_HYSTERESIS_BARS             = 3
REGIME_CONFIDENCE_HIGH_ADX         = 40.0
REGIME_CONFIDENCE_LOW_ADX          = 25.0
REGIME_MIN_CONFIDENCE_TO_TRADE     = 0.40
REGIME_ACTION_COOLDOWN_SECS        = 120   # detik cooldown setelah aksi transisi
REGIME_STABILITY_MIN_CYCLES        = 2     # cycle minimum regime stabil sebelum aksi
ENGULFING_MIN_BODY_RATIO    = 0.40
ENGULFING_COVERAGE_RATIO    = 1.05
HAMMER_LOWER_SHADOW_MULT    = 2.0
HAMMER_UPPER_SHADOW_MAX_MULT = 0.5
HAMMER_MIN_BODY_PCT         = 0.05
DOJI_MAX_BODY_PCT           = 0.05
DOJI_BALANCED_SHADOW_RATIO  = 2.0
MARUBOZU_MIN_BODY_PCT       = 0.80
PATTERN_NEAR_SUPPORT_PCT    = 0.015
PATTERN_NEAR_RESISTANCE_PCT = 0.015
PATTERN_VOLUME_CONFIRM_RATIO = 1.2

KELLY_MIN_SAMPLE            = 30
KELLY_FRACTION              = 0.5
KELLY_LOOKBACK_TRADES       = 50
KELLY_MAX_SIZE_PCT          = 10.0
KELLY_MIN_SIZE_PCT          = 0.5
CORRELATED_POSITION_PENALTY = 0.50
CORRELATION_HIGH_THRESHOLD  = 0.75

CORRELATION_GROUPS: Dict[str, FrozenSet[str]] = {
    "crypto_majors":  frozenset({"BTC", "ETH"}),
    "large_cap_alt":  frozenset({"SOL", "BNB", "AVAX", "DOT", "LINK", "ADA"}),
    "mid_cap_defi":   frozenset({"UNI", "AAVE", "SNX"}),
    "high_beta_alt":  frozenset({"NEAR", "APT", "SUI", "FET", "INJ", "OP", "ARB"}),
    "meme_tokens":    frozenset({"PEPE", "DOGE", "SHIB", "FLOKI", "WIF", "BONK"}),
    "payment_l1":     frozenset({"XRP", "LTC"}),
    "polygon_eco":    frozenset({"POL"}),
    "cosmos_eco":     frozenset({"ATOM"}),
}

def get_correlation_group(base: str) -> str:
    for group_name, members in CORRELATION_GROUPS.items():
        if base.upper() in members:
            return group_name
    return ""

SCORE_DISPLAY_HIGH   = 80.0
SCORE_DISPLAY_MEDIUM = 60.0
TG_MAX_MESSAGE_LEN = 3800
DASHBOARD_API_KEY_MIN_LEN = 16
LOG_ROTATION_MAX_BYTES = 50 * 1024 * 1024
LOG_ROTATION_BACKUP_COUNT = 30

APP_VERSION = "7.0"
APP_NAME    = "AlgoTrader Pro"
APP_CODENAME = "The Intelligence Pipeline"

def _validate_constants() -> None:

    assert SCORE_MIN < SCORE_NEUTRAL < SCORE_MAX, \
        f"Score range tidak valid: {SCORE_MIN} < {SCORE_NEUTRAL} < {SCORE_MAX}"

    assert (RSI_OVERSOLD_EXTREME < RSI_OVERSOLD < RSI_BULL_ZONE_LOW
            < RSI_BULL_ZONE_CENTER < RSI_OVERBOUGHT < RSI_OVERBOUGHT_EXTREME), \
        "RSI boundaries harus ascending"

    assert (ADX_WEAK_TREND < ADX_MODERATE_TREND < ADX_STRONG_TREND < ADX_VERY_STRONG), \
        "ADX boundaries harus ascending"

    assert STOCH_OVERSOLD < STOCH_OVERBOUGHT, \
        "Stoch oversold harus lebih kecil dari overbought"

    assert 0.0 < KELLY_FRACTION <= 1.0, \
        f"KELLY_FRACTION harus (0, 1], got: {KELLY_FRACTION}"
    assert KELLY_MIN_SIZE_PCT < KELLY_MAX_SIZE_PCT, \
        "Kelly min size harus lebih kecil dari max size"

    assert REGIME_HYSTERESIS_BARS >= 1, \
        "Regime hysteresis minimal 1 bar"

    ema_weight_sum = sum(EMA_STACK_WEIGHTS.values())
    assert abs(ema_weight_sum - 100.0) < 0.001, \
        f"EMA_STACK_WEIGHTS harus sum ke 100, got: {ema_weight_sum}"

    assert REGIME_SCORE_MODIFIERS["trending_bear"] == 0.0, \
        "REGIME_SCORE_MODIFIERS['trending_bear'] HARUS 0.0"

    for param, (lo, hi) in META_PARAM_BOUNDS.items():
        assert lo < hi, f"META_PARAM_BOUNDS['{param}']: min {lo} >= max {hi}"

    for tf, secs in TIMEFRAME_SECONDS.items():
        assert secs > 0, f"TIMEFRAME_SECONDS['{tf}'] harus positif"

    seen: set = set()
    for group_name, members in CORRELATION_GROUPS.items():
        for m in members:
            assert m not in seen, \
                f"'{m}' muncul di lebih dari satu CORRELATION_GROUPS group"
            seen.add(m)

    # [TAMBAHAN] Validasi konsistensi SIGNAL_CONFIRMATION_MATRIX vs REGIME_SCORE_MODIFIERS
    # Kedua dict harus punya key yang sama persis — kalau ada regime baru ditambahkan
    # ke salah satu, harus ditambahkan ke keduanya juga.
    scm_keys = set(SIGNAL_CONFIRMATION_MATRIX.keys())
    rsm_keys = set(REGIME_SCORE_MODIFIERS.keys())
    assert scm_keys == rsm_keys, (
        f"SIGNAL_CONFIRMATION_MATRIX dan REGIME_SCORE_MODIFIERS harus punya "
        f"key yang sama. SCM extra: {scm_keys - rsm_keys}, RSM extra: {rsm_keys - scm_keys}"
    )
    assert SIGNAL_CONFIRMATION_MATRIX.get("trending_bear", 0) >= 100, \
        "SIGNAL_CONFIRMATION_MATRIX['trending_bear'] harus sangat besar (>=100) untuk blokir buy"

# [BUG-FIX] Sebelumnya: SIGNAL_CONFIRMATION_MATRIX didefinisikan DUA KALI
# (di atas APP_CODENAME dan di bawah _validate_constants). Definisi pertama
# selalu ditimpa definisi kedua — kode yang membingungkan dan rawan bug kalau
# suatu saat dua versi tidak sengaja dibuat berbeda. Definisi pertama dihapus,
# hanya satu definisi yang tersisa (di bawah, langsung sebelum _validate_constants()).
# Jumlah konfirmasi BUY berturut-turut yang dibutuhkan per regime
SIGNAL_CONFIRMATION_MATRIX: dict = {
    "trending_bull":      5,  # Trending = butuh 5x konfirmasi (75 menit di 15m)
    "volatile_expansion": 3,  # Volatile = cepat tangkap momentum
    "ranging":            4,  # Ranging = sedang
    "undefined":          6,  # Undefined = paling ketat
    "trending_bear":    999,  # Bear = tidak pernah buy
}

_validate_constants()
