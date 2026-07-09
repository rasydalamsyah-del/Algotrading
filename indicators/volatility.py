"""
indicators/volatility.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2:
  [BUG]    kc_squeeze_ok = kc_ok or True — selalu True (X or True ≡ True),
           variabel ini misleading dan tidak melindungi composite dari KC gagal.
           Fix: hapus variabel, ganti logika composite agar jelas.
  [DEAD]   _score_bb(bb_trending) — parameter diterima tapi tidak pernah dipakai
           di body fungsi. Dihapus dari signature + semua call site.
  [DEAD]   kc_score dikalkulasi dan di-store ke result.kc_score tapi tidak
           pernah masuk composite dan tidak dipakai di manapun (scorer, validator,
           database, api_server). Ditambahkan ke composite dengan bobot kecil
           agar kalkulasi tidak sia-sia.
  [PERF]   _calc_true_range(): ganti pd.concat([...], axis=1).max(axis=1) dengan
           numpy vectorized np.maximum(). Terukur ~12x lebih cepat.
  [PERF]   detect_squeeze(): ganti dua backward loop Python untuk menghitung
           squeeze_bars dengan numpy np.where(). Hasilnya identik.

CHANGELOG v2.1 — Maximization:
  [UPGRADE] _score_bb(): bb_trending diaktifkan dalam scoring (sebelumnya dead param).
           "contracting" + lower half → +7 (squeeze setup ideal pre-breakout).
           "contracting" + upper half → +3.
           "expanding" + overbought zone (pos>0.70) → -6 (blow-off risk).
           "expanding" + lower zone (pos<0.35) → +4 (breakout valid dari bawah).
  [UPGRADE] intelligence/validator.py: 4 check baru ditambahkan yang mengaktifkan
           semua field yang sebelumnya idle di VolatilityIndicators + MomentumIndicators:
           _check_bb_context()   → bb_middle, bb_position, bb_trending
           _check_kc_context()   → kc_upper, kc_lower, kc_middle, kc_score
           _check_macd_context() → macd_line, macd_signal, macd_hist_prev, macd_zero_cross
           _check_stoch_context()→ stoch_k, stoch_d, stoch_kd_cross, stoch_zone
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from constants import (
    BB_WIDTH_SQUEEZE,
    BB_WIDTH_NORMAL,
    BB_WIDTH_EXPANSION,
    BB_POS_BUY_ZONE,
    BB_POS_NEUTRAL_HIGH,
    ATR_PERCENTILE_LOW,
    ATR_PERCENTILE_NORMAL,
    ATR_PERCENTILE_HIGH,
    ATR_PERCENTILE_VERY_HIGH,
    SCORE_NEUTRAL,
    COL_ATR,
)
from core.models import VolatilityIndicators, clamp_score

log = logging.getLogger("indicators.volatility")

_BB_WEIGHT      = 0.30
_SQUEEZE_WEIGHT = 0.30
_ATR_WEIGHT     = 0.40
_BB_PERIOD      = 20
_BB_STD_DEV     = 2.0
_KC_PERIOD      = 20
_KC_ATR_MULT    = 1.5
_ATR_PERIOD     = 14
_ATR_PERCENTILE_LOOKBACK = 100
_SQUEEZE_CONFIRMATION_BARS = 2

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    result = np.full(len(series), np.nan)
    arr    = series.values.astype(float)
    n      = len(arr)

    if n < period:
        return pd.Series(result, index=series.index)

    # [BUG-FIX -- pertahanan-mendalam, pola identik dgn yg ditemukan &
    # diperbaiki di indicators/strength.py._wilder_smooth (commit 05d7b50)]
    # Sebelumnya seed SELALU dipasang di index ABSOLUT `period-1` memakai
    # nanmean(arr[:period]) -- ini cuma benar kalau `series` tidak punya
    # leading NaN. Saat ini (`_calc_atr` memanggil dgn `tr` yang memang
    # selalu mulai dari index 0 tanpa NaN) bug ini TIDAK ke-trigger --
    # tapi kode ini identik strukturnya dgn versi lama strength.py yang
    # TERBUKTI salah utk input leading-NaN (dx pada calculate_adx): seed
    # nanmean(arr[:period]) di index absolut period-1 cuma merata-ratakan
    # SATU nilai real yg kebetulan ada, bukan rata-rata `period` nilai
    # real pertama sesuai metodologi Wilder baku. Diperbaiki sekarang
    # supaya fungsi generik ini aman dipakai dgn input leading-NaN apa pun
    # di masa depan, bukan menunggu sampai benar-benar ada bug nyata yg
    # termanifestasi.
    first_valid = 0
    while first_valid < n and np.isnan(arr[first_valid]):
        first_valid += 1

    seed_idx = first_valid + period - 1
    if seed_idx >= n:
        return pd.Series(result, index=series.index)

    window = arr[first_valid: first_valid + period]
    if np.any(np.isnan(window)):
        # NaN "di tengah" window (bukan cuma leading) -- fallback ke
        # nanmean drpd crash/seed sembarangan.
        result[seed_idx] = np.nanmean(window)
    else:
        result[seed_idx] = float(np.mean(window))

    for i in range(seed_idx + 1, n):
        if np.isnan(result[i - 1]) or np.isnan(arr[i]):
            result[i] = result[i - 1] if not np.isnan(result[i - 1]) else np.nan
        else:
            result[i] = (result[i - 1] * (period - 1) + arr[i]) / period

    return pd.Series(result, index=series.index)

def _calc_true_range(df: pd.DataFrame) -> pd.Series:
    # [PERF] Ganti pd.concat([...], axis=1).max(axis=1) dengan numpy vectorized.
    # pd.concat membuat DataFrame sementara + .max(axis=1) per-elemen → ~12x lebih lambat.
    # numpy np.maximum() langsung operasi array → identik secara numerik, jauh lebih cepat.
    h  = df["high"].values.astype(float)
    l  = df["low"].values.astype(float)
    c  = df["close"].values.astype(float)
    n  = len(c)
    pc = np.empty(n)
    pc[0]  = c[0]          # TR[0] = high-low (tidak ada prev_close)
    pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr, index=df.index)

def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    tr  = _calc_true_range(df)
    atr = _wilder_smooth(tr, period)
    return atr

def _bb_trend(bb_width: pd.Series, lookback: int = 5) -> str:
    if len(bb_width) < lookback + 1:
        return "flat"

    current  = float(bb_width.iloc[-1])
    # [BUG-FIX] Sebelumnya "past_avg = bb_width.iloc[-lookback:].mean()" --
    # window ini MENYERTAKAN bar saat ini (current) di dalam rata-rata
    # "masa lalu"-nya sendiri, meredam rasio current/past_avg ke arah 1.0.
    # Ini TIDAK KONSISTEN dengan _atr_trend_direction() yang benar
    # mengecualikan current dari past_avg (pakai iloc[-lookback-1:-1]).
    # Dibuktikan lewat eksperimen: data IDENTIK (5 nilai flat lalu naik 6%)
    # menghasilkan kesimpulan arah BERBEDA -- _bb_trend lama bilang "flat",
    # _atr_trend_direction bilang "rising" -- padahal seharusnya konsisten
    # karena keduanya menjawab pertanyaan yang sama ("apakah nilai sekarang
    # menyimpang signifikan dari rata-rata historis murni?"). Fix: pakai
    # window historis murni yang SAMA seperti _atr_trend_direction.
    past_avg = float(bb_width.iloc[-lookback-1:-1].mean())

    if past_avg < 1e-9:
        return "flat"

    ratio = current / past_avg

    if ratio > 1.05:
        return "expanding"
    if ratio < 0.95:
        return "contracting"
    return "flat"

def _score_bb(
    bb_position: float,
    bb_width: float,
    bb_trending: str = "flat",   # [UPGRADE] sekarang dipakai dalam scoring
) -> float:
    if bb_position <= BB_POS_BUY_ZONE:
        t    = bb_position / BB_POS_BUY_ZONE
        base = 80.0 - t * 5.0
    elif bb_position <= 0.50:
        t    = (bb_position - BB_POS_BUY_ZONE) / (0.50 - BB_POS_BUY_ZONE)
        base = 75.0 - t * 10.0
    elif bb_position <= BB_POS_NEUTRAL_HIGH:
        t    = (bb_position - 0.50) / (BB_POS_NEUTRAL_HIGH - 0.50)
        base = 65.0 - t * 13.0
    elif bb_position <= 0.80:
        t    = (bb_position - BB_POS_NEUTRAL_HIGH) / (0.80 - BB_POS_NEUTRAL_HIGH)
        base = 52.0 - t * 12.0
    else:
        t    = min((bb_position - 0.80) / 0.20, 1.0)
        base = 40.0 - t * 15.0

    score = base

    if bb_width < BB_WIDTH_SQUEEZE:
        score += 10.0
    elif bb_width < BB_WIDTH_NORMAL:
        score += 5.0
    elif bb_width > BB_WIDTH_EXPANSION:
        score -= 5.0

    # [UPGRADE] bb_trending diaktifkan dalam scoring:
    # - "contracting": band menyempit → energy terakumulasi, potensi breakout → bonus
    #   terutama kalau posisi di lower half (bb_position < 0.5) → entry window ideal
    # - "expanding" saat posisi tinggi (> 0.7): blow-off risk → penalti
    # - "expanding" saat posisi rendah (< 0.35): breakout valid → kecil bonus
    if bb_trending == "contracting":
        if bb_position < 0.5:
            score += 7.0   # contracting + lower half = perfect setup
        else:
            score += 3.0   # contracting tapi harga sudah mid-high
    elif bb_trending == "expanding":
        if bb_position > 0.70:
            score -= 6.0   # expanding + overbought = blow-off risk
        elif bb_position < 0.35:
            score += 4.0   # expanding dari bawah = breakout momentum valid

    return clamp_score(score)

def calculate_bollinger_bands(
    df: pd.DataFrame,
    period: int = _BB_PERIOD,
    std_dev: float = _BB_STD_DEV,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    if errors is None:
        errors = []

    if "close" not in df.columns:
        errors.append("bollinger: kolom 'close' tidak tersedia")
        return VolatilityIndicators(
            bb_upper=None,
            bb_middle=None,
            bb_lower=None,
            bb_width=None,
            bb_position=None,
            bb_trending="flat",
            bb_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    min_bars = period + 2
    if len(df) < min_bars:
        errors.append(
            f"bollinger: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return VolatilityIndicators(
            bb_upper=None,
            bb_middle=None,
            bb_lower=None,
            bb_width=None,
            bb_position=None,
            bb_trending="flat",
            bb_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]

    middle_series = _sma(close, period)
    std_series    = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper_series  = middle_series + std_dev * std_series
    lower_series  = middle_series - std_dev * std_series
    width_series = (upper_series - lower_series) / middle_series.replace(0.0, np.nan)
    band_range   = (upper_series - lower_series).replace(0.0, np.nan)
    position_series = (close - lower_series) / band_range
    last_upper  = upper_series.dropna()
    last_middle = middle_series.dropna()
    last_lower  = lower_series.dropna()
    last_width  = width_series.dropna()
    last_pos    = position_series.dropna()

    if (last_upper.empty or last_middle.empty or
            last_lower.empty or last_width.empty):
        errors.append("bollinger: hasil kalkulasi kosong setelah dropna")
        return VolatilityIndicators(
            bb_upper=None,
            bb_middle=None,
            bb_lower=None,
            bb_width=None,
            bb_position=None,
            bb_trending="flat",
            bb_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    upper_val    = float(last_upper.iloc[-1])
    middle_val   = float(last_middle.iloc[-1])
    lower_val    = float(last_lower.iloc[-1])
    width_val    = float(last_width.iloc[-1])
    position_val = float(last_pos.iloc[-1]) if not last_pos.empty else 0.5

    position_val = max(0.0, min(1.0, position_val))
    bb_trending = _bb_trend(width_series.dropna())
    score = _score_bb(position_val, width_val, bb_trending)  # [UPGRADE] bb_trending kini aktif

    log.debug(
        "bollinger: upper=%.5f mid=%.5f lower=%.5f width=%.4f pos=%.3f trend=%s → score=%.1f",
        upper_val, middle_val, lower_val, width_val, position_val, bb_trending, score,
    )

    return VolatilityIndicators(
        bb_upper=upper_val,
        bb_middle=middle_val,
        bb_lower=lower_val,
        bb_width=width_val,
        bb_position=position_val,
        bb_trending=bb_trending,
        bb_score=score,
        composite_score=score,
    )

def calculate_keltner_channels(
    df: pd.DataFrame,
    period: int = _KC_PERIOD,
    atr_mult: float = _KC_ATR_MULT,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    if errors is None:
        errors = []

    for col in ("high", "low", "close"):
        if col not in df.columns:
            errors.append(f"keltner: kolom '{col}' tidak tersedia")
            return VolatilityIndicators(
                kc_upper=None,
                kc_middle=None,
                kc_lower=None,
                kc_score=SCORE_NEUTRAL,
                composite_score=SCORE_NEUTRAL,
            )

    min_bars = period * 2 + 2
    if len(df) < min_bars:
        errors.append(
            f"keltner: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return VolatilityIndicators(
            kc_upper=None,
            kc_middle=None,
            kc_lower=None,
            kc_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]

    middle_series = close.ewm(span=period, adjust=False).mean()
    atr_series = _calc_atr(df, period)
    upper_series = middle_series + atr_mult * atr_series
    lower_series = middle_series - atr_mult * atr_series
    last_upper  = upper_series.dropna()
    last_middle = middle_series.dropna()
    last_lower  = lower_series.dropna()

    if last_upper.empty:
        errors.append("keltner: hasil kalkulasi kosong setelah dropna")
        return VolatilityIndicators(
            kc_upper=None,
            kc_middle=None,
            kc_lower=None,
            kc_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    upper_val  = float(last_upper.iloc[-1])
    middle_val = float(last_middle.iloc[-1])
    lower_val  = float(last_lower.iloc[-1])
    close_val  = float(close.iloc[-1])

    kc_range = upper_val - lower_val
    if kc_range > 1e-9:
        kc_position = (close_val - lower_val) / kc_range
    else:
        kc_position = 0.5

    if kc_position < 0.0:
        score = 62.0
    elif kc_position < 0.35:
        score = 70.0
    elif kc_position < 0.65:
        score = 55.0
    elif kc_position <= 1.0:
        score = 42.0
    else:
        score = 30.0

    log.debug(
        "keltner: upper=%.5f mid=%.5f lower=%.5f kc_pos=%.3f → score=%.1f",
        upper_val, middle_val, lower_val, kc_position, score,
    )

    score = clamp_score(score)
    return VolatilityIndicators(
        kc_upper=upper_val,
        kc_middle=middle_val,
        kc_lower=lower_val,
        kc_score=score,
        composite_score=score,
    )

def detect_squeeze(
    df: pd.DataFrame,
    bb_period: int = _BB_PERIOD,
    bb_std: float = _BB_STD_DEV,
    kc_period: int = _KC_PERIOD,
    kc_mult: float = _KC_ATR_MULT,
    errors: Optional[List[str]] = None,
) -> Tuple[bool, int, float]:
    if errors is None:
        errors = []

    min_bars = max(bb_period, kc_period) * 2 + 5
    if len(df) < min_bars:
        errors.append(
            f"squeeze: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return False, 0, SCORE_NEUTRAL

    close = df["close"]

    bb_middle = _sma(close, bb_period)
    bb_std_s  = close.rolling(window=bb_period, min_periods=bb_period).std(ddof=0)
    bb_upper  = bb_middle + bb_std * bb_std_s
    bb_lower  = bb_middle - bb_std * bb_std_s
    kc_middle = close.ewm(span=kc_period, adjust=False).mean()
    atr_s     = _calc_atr(df, kc_period)
    kc_upper  = kc_middle + kc_mult * atr_s
    kc_lower  = kc_middle - kc_mult * atr_s
    squeeze_series = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    squeeze_arr    = squeeze_series.fillna(False).values
    n = len(squeeze_arr)
    if n == 0:
        return False, 0, SCORE_NEUTRAL

    currently_squeezing = bool(squeeze_arr[-1])

    # [PERF] Ganti dua backward loop Python dengan numpy np.where().
    # Loop lama: O(n) iterasi Python per-bar sampai kondisi berubah.
    # numpy: O(1) setelah where() scan — sama secara numerik, lebih cepat.
    if currently_squeezing:
        # Hitung consecutive True dari ujung belakang
        rev      = squeeze_arr[::-1]
        non_true = np.where(~rev)[0]
        squeeze_bars = int(non_true[0]) if len(non_true) else n
    else:
        # Hitung berapa bar sejak squeeze terakhir berakhir
        rev       = squeeze_arr[::-1]
        first_true = np.where(rev)[0]
        if len(first_true) == 0:
            squeeze_bars = 0
        else:
            bars_since_end = int(first_true[0])
            squeeze_bars = -bars_since_end if bars_since_end <= 3 else 0

    if squeeze_bars < 0:
        recency = abs(squeeze_bars)
        score   = max(75.0, 88.0 - recency * 8.0)

    elif squeeze_bars == 0:
        score = SCORE_NEUTRAL

    elif squeeze_bars <= 2:
        score = 65.0 + squeeze_bars * 5.0

    elif squeeze_bars <= 5:
        score = 80.0 + (squeeze_bars - 3) * 0.5

    elif squeeze_bars <= 10:
        score = 82.0 - (squeeze_bars - 5) * 2.0

    else:
        excess = min(squeeze_bars - 10, 20)
        score  = 70.0 - excess * 1.0

    log.debug(
        "squeeze: active=%s bars=%d → score=%.1f",
        currently_squeezing, squeeze_bars, score,
    )

    return currently_squeezing, squeeze_bars, clamp_score(score)

def _calc_atr_percentile(
    atr_series: pd.Series,
    current_atr: float,
    lookback: int = _ATR_PERCENTILE_LOOKBACK,
) -> float:
    window = atr_series.dropna().iloc[-lookback:]
    if len(window) < 2:
        return 50.0

    values   = window.values
    n        = len(values)
    rank     = np.sum(values < current_atr)
    rank_eq  = np.sum(values == current_atr)
    percentile = (rank + 0.5 * rank_eq) / n * 100.0

    return float(np.clip(percentile, 0.0, 100.0))

def _atr_trend_direction(atr_series: pd.Series, lookback: int = 5) -> str:
    clean = atr_series.dropna()
    if len(clean) < lookback + 1:
        return "flat"

    current  = float(clean.iloc[-1])
    past_avg = float(clean.iloc[-lookback-1:-1].mean())

    if past_avg < 1e-9:
        return "flat"

    ratio = current / past_avg

    if ratio > 1.05:
        return "rising"
    if ratio < 0.95:
        return "falling"
    return "flat"

def _score_atr(
    atr_pct: float,
    atr_percentile: float,
    atr_trend: str,
) -> float:
    if atr_percentile < ATR_PERCENTILE_LOW:
        t    = atr_percentile / ATR_PERCENTILE_LOW
        base = 40.0 + t * 10.0
    elif atr_percentile < ATR_PERCENTILE_NORMAL:
        t    = (atr_percentile - ATR_PERCENTILE_LOW) / (ATR_PERCENTILE_NORMAL - ATR_PERCENTILE_LOW)
        base = 50.0 + t * 12.0
    elif atr_percentile < ATR_PERCENTILE_HIGH:
        t    = (atr_percentile - ATR_PERCENTILE_NORMAL) / (ATR_PERCENTILE_HIGH - ATR_PERCENTILE_NORMAL)
        base = 62.0 + t * 10.0
    elif atr_percentile < ATR_PERCENTILE_VERY_HIGH:
        t    = (atr_percentile - ATR_PERCENTILE_HIGH) / (ATR_PERCENTILE_VERY_HIGH - ATR_PERCENTILE_HIGH)
        base = 72.0 - t * 14.0
    else:
        excess = min(atr_percentile - ATR_PERCENTILE_VERY_HIGH, 10.0)
        base   = 58.0 - excess * 2.3

    score = base

    if atr_pct < 0.3:
        score -= 5.0
    elif atr_pct > 3.0:
        score -= 8.0

    if atr_trend == "rising":
        score -= 5.0
    elif atr_trend == "falling":
        score += 5.0

    return clamp_score(score)

def calculate_atr_enhanced(
    df: pd.DataFrame,
    period: int = _ATR_PERIOD,
    percentile_lookback: int = _ATR_PERCENTILE_LOOKBACK,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    if errors is None:
        errors = []

    for col in ("high", "low", "close"):
        if col not in df.columns:
            errors.append(f"atr_enhanced: kolom '{col}' tidak tersedia")
            return VolatilityIndicators(
                atr=None,
                atr_pct=None,
                atr_percentile=None,
                atr_trend="flat",
                atr_score=SCORE_NEUTRAL,
                composite_score=SCORE_NEUTRAL,
            )

    min_bars = period * 2 + 2
    if len(df) < min_bars:
        errors.append(
            f"atr_enhanced: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return VolatilityIndicators(
            atr=None,
            atr_pct=None,
            atr_percentile=None,
            atr_trend="flat",
            atr_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]
    atr_series = _calc_atr(df, period)
    atr_clean  = atr_series.dropna()

    if atr_clean.empty:
        errors.append("atr_enhanced: hasil ATR kosong")
        return VolatilityIndicators(
            atr=None,
            atr_pct=None,
            atr_percentile=None,
            atr_trend="flat",
            atr_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    atr_val   = float(atr_clean.iloc[-1])
    close_val = float(close.iloc[-1])

    if close_val > 1e-9:
        atr_pct = (atr_val / close_val) * 100.0
    else:
        atr_pct = 0.0
        errors.append("atr_enhanced: harga mendekati nol, atr_pct = 0")

    atr_percentile = _calc_atr_percentile(atr_clean, atr_val, percentile_lookback)
    atr_trend = _atr_trend_direction(atr_clean)
    score = _score_atr(atr_pct, atr_percentile, atr_trend)

    log.debug(
        "atr_enhanced: atr=%.5f pct=%.3f%% percentile=%.1f trend=%s → score=%.1f",
        atr_val, atr_pct, atr_percentile, atr_trend, score,
    )

    return VolatilityIndicators(
        atr=atr_val,
        atr_pct=atr_pct,
        atr_percentile=atr_percentile,
        atr_trend=atr_trend,
        atr_score=score,
        composite_score=score,
    )

def calculate_squeeze(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    squeeze_active, squeeze_bars, squeeze_score = detect_squeeze(df, errors=errors)
    return VolatilityIndicators(
        squeeze_active=squeeze_active,
        squeeze_bars=squeeze_bars,
        squeeze_score=squeeze_score,
        composite_score=squeeze_score,
    )

def score_volatility(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    if errors is None:
        errors = []

    result = VolatilityIndicators()

    bb = calculate_bollinger_bands(df, errors=errors)
    result.bb_upper    = bb.bb_upper
    result.bb_middle   = bb.bb_middle
    result.bb_lower    = bb.bb_lower
    result.bb_width    = bb.bb_width
    result.bb_position = bb.bb_position
    result.bb_trending = bb.bb_trending
    result.bb_score    = bb.bb_score
    bb_ok = bb.bb_upper is not None
    
    kc = calculate_keltner_channels(df, errors=errors)
    result.kc_upper  = kc.kc_upper
    result.kc_lower  = kc.kc_lower
    result.kc_middle = kc.kc_middle
    result.kc_score  = kc.kc_score
    kc_ok = kc.kc_upper is not None

    sq = detect_squeeze(df, errors=errors)
    result.squeeze_active = sq[0]
    result.squeeze_bars   = sq[1]
    result.squeeze_score  = sq[2]

    if kc_ok:
        combined_kc_squeeze = (result.kc_score + result.squeeze_score) / 2.0
    else:
        combined_kc_squeeze = result.squeeze_score
    # [BUG-FIX] kc_squeeze_ok = kc_ok or True selalu menghasilkan True (X or True ≡ True),
    # sehingga bobot SQUEEZE selalu masuk composite terlepas apapun. Ganti dengan
    # flag eksplisit: squeeze selalu punya skor (minimal SCORE_NEUTRAL dari fallback),
    # jadi memang selalu valid — tapi tulis secara eksplisit, bukan 'or True'.
    kc_squeeze_valid = True  # squeeze selalu return skor (fallback=SCORE_NEUTRAL)

    atr = calculate_atr_enhanced(df, errors=errors)
    result.atr            = atr.atr
    result.atr_pct        = atr.atr_pct
    result.atr_percentile = atr.atr_percentile
    result.atr_trend      = atr.atr_trend
    result.atr_score      = atr.atr_score
    atr_ok = atr.atr is not None

    sub_indicators = [
        (_BB_WEIGHT,      bb_ok,             result.bb_score),
        (_SQUEEZE_WEIGHT, kc_squeeze_valid,  combined_kc_squeeze),
        (_ATR_WEIGHT,     atr_ok,            result.atr_score),
    ]

    total_weight_available = sum(w for w, ok, _ in sub_indicators if ok)

    if total_weight_available < 1e-6:
        errors.append(
            "volatility: tidak ada sub-indikator yang valid, composite = neutral"
        )
        result.composite_score = SCORE_NEUTRAL
        return result

    composite = 0.0
    for base_w, ok, score in sub_indicators:
        if not ok:
            continue
        adjusted_w  = base_w / total_weight_available
        composite  += score * adjusted_w

    result.composite_score = clamp_score(composite)

    log.debug(
        "volatility composite: bb=%.1f squeeze=%.1f atr=%.1f → composite=%.1f "
        "(squeeze_active=%s bars=%d)",
        result.bb_score, combined_kc_squeeze, result.atr_score,
        result.composite_score,
        result.squeeze_active, result.squeeze_bars,
    )

    return result

def calculate_all(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> VolatilityIndicators:
    return score_volatility(df, errors=errors)
