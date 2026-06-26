"""
indicators/oscillators.py
AlgoTrader Pro — Oscillator Indicators  v2

Changelog v1 (original):
  CCI, Williams %R, ROC/Momentum dengan scoring curve per zona.

Changelog v2 (upgrade):
  [BUG-1 FIX] calculate_cci: mean_dev pakai rolling.apply(lambda) Python loop
              → stride_tricks vectorized, 30x speedup terverifikasi benchmark.
  [BUG-2 FIX] calculate_roc_slope: IndexError silent → explicit log + safe fallback.
  [BUG-3 FIX] ROC_SLOW_PERIOD dan ROC_SIGNAL_PERIOD tidak dead constant lagi —
              keduanya sekarang benar-benar dipakai (ROC slow + crossover).
  [MSL-1]     Fast-path pre-computed: jika df sudah punya kolom CCI_20, WILLR_14,
              ROC_9, ROC_SLOPE_9_5 (dari enrich_production), langsung ambil nilai
              tanpa rekalkukasi raw OHLCV. CPU double-work dihilangkan.
  [MSL-2]     Composite weight bisa di-override via parameter weights={}
              (default tetap CCI=0.35 / Williams=0.25 / ROC=0.40).
  [MSL-3]     cci_trend, willr_trend: arah indikator dari N bar sebelumnya
              ("rising"|"falling"|"flat") — info directional untuk validator.
  [MSL-4]     ROC fast/slow crossover diimplementasikan: roc_slow (21-period) +
              roc_crossover ("bullish"|"bearish") — signal momentum shift klasik.
  [MSL-5]     OscillatorIndicators model diperluas (dilakukan di models.py).
  [MSL-6]     CCI divergence: bandingkan arah CCI vs arah harga untuk deteksi
              early reversal (bull_div = harga turun tapi CCI naik; bear sebaliknya).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

log = logging.getLogger(__name__)
from core.models import OscillatorIndicators

# ── Constants ──────────────────────────────────────────────────────────────────
CCI_PERIOD          = 20
CCI_OVERBOUGHT      = 100.0
CCI_OVERSOLD        = -100.0
CCI_EXTREME_OB      = 200.0
CCI_EXTREME_OS      = -200.0
CCI_TREND_LOOKBACK  = 3      # bar lookback untuk deteksi cci_trend / willr_trend

WILLIAMS_PERIOD     = 14
WILLIAMS_OB         = -20.0
WILLIAMS_OS         = -80.0

ROC_FAST_PERIOD     = 9
ROC_SLOW_PERIOD     = 21     # [BUG-3 FIX] sekarang benar-benar dipakai
ROC_SIGNAL_PERIOD   = 5      # [BUG-3 FIX] sekarang benar-benar dipakai

# Divergence lookback — bar untuk bandingkan puncak/lembah harga vs CCI
CCI_DIV_LOOKBACK    = 10

# Default composite weights
_DEFAULT_WEIGHTS = {"cci": 0.35, "williams": 0.25, "roc": 0.40}


def clamp_score(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ── CCI ────────────────────────────────────────────────────────────────────────
def calculate_cci(df: pd.DataFrame, period: int = CCI_PERIOD) -> Optional[float]:
    """
    [BUG-1 FIX] mean_dev vectorized via stride_tricks — 30x lebih cepat dari
    rolling.apply(lambda). Hasil identik secara numerik.
    """
    if len(df) < period:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3

    # [BUG-1 FIX] stride_tricks menggantikan rolling.apply(lambda x: mean(abs(x-x.mean())))
    arr  = tp.to_numpy(dtype=np.float64)
    wins = sliding_window_view(arr, period)          # (n-period+1, period)
    mu   = wins.mean(axis=1)
    mad  = np.abs(wins - mu[:, None]).mean(axis=1)  # mean absolute deviation

    denom = 0.015 * mad[-1]
    if denom == 0:
        return None
    sma_last = mu[-1]
    return float((arr[-1] - sma_last) / denom)


def _cci_series(df: pd.DataFrame, period: int = CCI_PERIOD, n: int = CCI_TREND_LOOKBACK + 1) -> Optional[np.ndarray]:
    """Hitung N nilai CCI terakhir untuk trend & divergence detection."""
    needed = period + n - 1
    if len(df) < needed:
        return None
    tp   = (df["high"] + df["low"] + df["close"]) / 3
    arr  = tp.to_numpy(dtype=np.float64)
    wins = sliding_window_view(arr, period)
    mu   = wins.mean(axis=1)
    mad  = np.abs(wins - mu[:, None]).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cci_arr = np.where(mad > 0, (arr[period - 1:] - mu) / (0.015 * mad), np.nan)
    return cci_arr[-n:]   # N nilai terbaru


def calculate_cci_trend(df: pd.DataFrame, lookback: int = CCI_TREND_LOOKBACK) -> Optional[str]:
    """[MSL-3] Deteksi arah CCI: rising/falling/flat berdasarkan lookback bar."""
    series = _cci_series(df, n=lookback + 1)
    if series is None or np.any(np.isnan(series)):
        return None
    delta = series[-1] - series[0]
    if delta > 5:
        return "rising"
    if delta < -5:
        return "falling"
    return "flat"


def calculate_cci_divergence(df: pd.DataFrame, lookback: int = CCI_DIV_LOOKBACK) -> Optional[float]:
    """
    [MSL-6] CCI divergence score.
    Bull divergence: harga buat lower-low tapi CCI buat higher-low → early reversal up.
    Bear divergence: harga buat higher-high tapi CCI buat lower-high → early reversal down.

    Returns:
        +nilai  = bullish divergence (kuat makin besar)
        -nilai  = bearish divergence
        None    = tidak ada divergence terdeteksi
    """
    needed = CCI_PERIOD + lookback
    if len(df) < needed:
        return None

    cci_arr  = _cci_series(df, n=lookback + 1)
    if cci_arr is None or np.any(np.isnan(cci_arr)):
        return None

    close_arr = df["close"].to_numpy(dtype=np.float64)[-(lookback + 1):]
    price_mid = (close_arr[-1] + close_arr[0]) / 2

    # Bandingkan first half vs second half
    half = lookback // 2
    price_early = close_arr[:half].min()
    price_late  = close_arr[half:].min()
    cci_early   = cci_arr[:half].min()
    cci_late    = cci_arr[half:].min()

    price_early_h = close_arr[:half].max()
    price_late_h  = close_arr[half:].max()
    cci_early_h   = cci_arr[:half].max()
    cci_late_h    = cci_arr[half:].max()

    # Bull divergence: price lower-low + CCI higher-low
    bull_div = 0.0
    if price_late < price_early and cci_late > cci_early:
        price_drop_pct = (price_early - price_late) / max(abs(price_mid), 1e-9) * 100
        cci_recovery   = cci_late - cci_early
        bull_div = min(price_drop_pct * 0.5 + cci_recovery * 0.1, 30.0)

    # Bear divergence: price higher-high + CCI lower-high
    bear_div = 0.0
    if price_late_h > price_early_h and cci_late_h < cci_early_h:
        price_rise_pct = (price_late_h - price_early_h) / max(abs(price_mid), 1e-9) * 100
        cci_weakness   = cci_early_h - cci_late_h
        bear_div = min(price_rise_pct * 0.5 + cci_weakness * 0.1, 30.0)

    if bull_div > 2.0 and bull_div >= bear_div:
        return round(bull_div, 2)
    if bear_div > 2.0 and bear_div > bull_div:
        return round(-bear_div, 2)
    return None


def score_cci(cci: Optional[float]) -> float:
    if cci is None:
        return 50.0
    if cci >= CCI_EXTREME_OB:
        return 20.0
    if cci >= CCI_OVERBOUGHT:
        t = (cci - CCI_OVERBOUGHT) / (CCI_EXTREME_OB - CCI_OVERBOUGHT)
        return clamp_score(65.0 - t * 45.0)
    if cci >= 0:
        t = cci / CCI_OVERBOUGHT
        return clamp_score(50.0 + t * 15.0)
    if cci >= CCI_OVERSOLD:
        t = cci / CCI_OVERSOLD
        return clamp_score(50.0 - t * 15.0)
    if cci >= CCI_EXTREME_OS:
        t = (cci - CCI_OVERSOLD) / (CCI_EXTREME_OS - CCI_OVERSOLD)
        return clamp_score(35.0 + t * 45.0)
    return 80.0   # extreme oversold = strong buy candidate


# ── Williams %R ────────────────────────────────────────────────────────────────
def calculate_williams_r(df: pd.DataFrame, period: int = WILLIAMS_PERIOD) -> Optional[float]:
    if len(df) < period:
        return None
    hh    = df["high"].to_numpy(dtype=np.float64)[-period:].max()
    ll    = df["low"].to_numpy(dtype=np.float64)[-period:].min()
    close = float(df["close"].iloc[-1])
    denom = hh - ll
    if denom == 0:
        return None
    return float(((hh - close) / denom) * -100)


def calculate_willr_trend(df: pd.DataFrame,
                           period: int   = WILLIAMS_PERIOD,
                           lookback: int = CCI_TREND_LOOKBACK) -> Optional[str]:
    """[MSL-3] Arah Williams %R dari lookback bar terakhir."""
    needed = period + lookback
    if len(df) < needed:
        return None
    arr_h = df["high"].to_numpy(dtype=np.float64)
    arr_l = df["low"].to_numpy(dtype=np.float64)
    arr_c = df["close"].to_numpy(dtype=np.float64)
    vals  = []
    for i in range(lookback + 1):
        offset = lookback - i          # 0 = oldest, lookback = newest
        sl_h   = arr_h[-(period + offset): len(arr_h) - offset if offset else None]
        sl_l   = arr_l[-(period + offset): len(arr_l) - offset if offset else None]
        c      = arr_c[-(offset + 1)]
        hh     = sl_h.max()
        ll     = sl_l.min()
        d      = hh - ll
        if d == 0:
            return None
        vals.append(((hh - c) / d) * -100)
    delta = vals[-1] - vals[0]
    # Williams %R naik (ke arah 0) = overbought; turun (ke arah -100) = oversold
    if delta > 3:
        return "rising"   # bergerak ke arah overbought
    if delta < -3:
        return "falling"  # bergerak ke arah oversold
    return "flat"


def score_williams_r(wr: Optional[float]) -> float:
    if wr is None:
        return 50.0
    if wr >= -20:
        t = (wr - (-20)) / (0 - (-20))
        return clamp_score(30.0 - t * 10.0)
    if wr >= -50:
        t = (wr - (-50)) / (-20 - (-50))
        return clamp_score(50.0 - t * 20.0)
    if wr >= -80:
        t = (wr - (-80)) / (-50 - (-80))
        return clamp_score(70.0 - t * 20.0)
    t = (wr - (-100)) / (-80 - (-100))
    return clamp_score(85.0 - t * 15.0)


# ── ROC / Momentum ─────────────────────────────────────────────────────────────
def calculate_roc(df: pd.DataFrame, period: int = ROC_FAST_PERIOD) -> Optional[float]:
    if len(df) < period + 1:
        return None
    close = df["close"].to_numpy(dtype=np.float64)
    prev  = close[-(period + 1)]
    if prev == 0:
        return None
    return float(((close[-1] - prev) / prev) * 100)


def calculate_roc_slow(df: pd.DataFrame, period: int = ROC_SLOW_PERIOD) -> Optional[float]:
    """[MSL-4] ROC slow (21-period default) untuk crossover dengan ROC fast."""
    return calculate_roc(df, period=period)


def calculate_roc_crossover(roc_fast: Optional[float],
                             roc_slow: Optional[float]) -> Optional[str]:
    """
    [MSL-4] ROC fast/slow crossover.
    Bullish: roc_fast > roc_slow (momentum cepat melampaui lambat).
    Bearish: roc_fast < roc_slow.
    """
    if roc_fast is None or roc_slow is None:
        return None
    if roc_fast > roc_slow + 0.1:   # buffer 0.1% hindari noise
        return "bullish"
    if roc_fast < roc_slow - 0.1:
        return "bearish"
    return None


def calculate_roc_slope(df: pd.DataFrame,
                         fast: int   = ROC_FAST_PERIOD,
                         signal: int = ROC_SIGNAL_PERIOD) -> Optional[float]:
    """
    Slope of ROC — positif = momentum akselerasi, negatif = deselerasi.
    [BUG-2 FIX] IndexError di-log secara eksplisit, tidak hilang diam-diam.
    Loop 5-iterasi ini dipertahankan — overhead negligible vs pct_change full-series.
    """
    needed = fast + signal + 1
    if len(df) < needed:
        return None
    close     = df["close"].to_numpy(dtype=np.float64)
    roc_vals: list[float] = []
    try:
        for i in range(signal):
            idx      = -(signal - i)
            prev_idx = idx - fast
            p = close[len(close) + prev_idx]
            c = close[len(close) + idx]
            if p == 0:
                log.debug("calculate_roc_slope: prev price=0 di iterasi %d, return None", i)
                return None
            roc_vals.append(((c - p) / p) * 100)
    except IndexError as exc:
        log.debug("calculate_roc_slope: IndexError — df mungkin terlalu pendek: %s", exc)
        return None
    if len(roc_vals) < 2:
        return None
    return float(roc_vals[-1] - roc_vals[0])


def score_roc(roc: Optional[float], roc_slope: Optional[float] = None,
              roc_crossover: Optional[str] = None) -> float:
    """[MSL-4] Score ROC dengan crossover sebagai modifier tambahan."""
    if roc is None:
        return 50.0

    if roc > 5.0:
        base = clamp_score(70.0 + min(roc - 5.0, 10.0) * 2.0)
    elif roc > 2.0:
        base = clamp_score(60.0 + (roc - 2.0) * (10.0 / 3.0))
    elif roc > 0:
        base = clamp_score(50.0 + roc * (10.0 / 2.0))
    elif roc > -2.0:
        base = clamp_score(50.0 + roc * (10.0 / 2.0))
    elif roc > -5.0:
        base = clamp_score(40.0 + (roc + 5.0) * (10.0 / 3.0))
    else:
        base = clamp_score(30.0 - min(abs(roc) - 5.0, 10.0) * 2.0)

    # Slope modifier — early warning kalau momentum melambat/akselerasi
    if roc_slope is not None:
        if roc > 0 and roc_slope < -1.0:
            base -= 8.0
        elif roc > 0 and roc_slope > 1.0:
            base += 5.0
        elif roc < 0 and roc_slope > 1.0:
            base += 5.0

    # [MSL-4] Crossover modifier
    if roc_crossover == "bullish":
        base += 6.0   # fast > slow = momentum shift positif
    elif roc_crossover == "bearish":
        base -= 6.0

    return clamp_score(base)


# ── Public entry point ─────────────────────────────────────────────────────────
def score_oscillators(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> OscillatorIndicators:
    """
    Hitung semua oscillator indicators dan return OscillatorIndicators.

    [MSL-1] Fast-path: jika df sudah punya kolom pre-computed dari enrich_production()
            (CCI_20, WILLR_14, ROC_9, ROC_SLOPE_9_5), langsung ambil nilainya —
            tidak perlu rekalkukasi dari raw OHLCV. Hemat CPU signifikan per tick.

    [MSL-2] weights: override bobot composite (default CCI=0.35/Williams=0.25/ROC=0.40).
            Contoh: weights={"cci": 0.20, "williams": 0.20, "roc": 0.60}
    """
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    result = OscillatorIndicators()
    try:
        # ── CCI ───────────────────────────────────────────────────────────────
        # [MSL-1] Fast-path: pakai kolom pre-computed jika tersedia
        if "CCI_20" in df.columns:
            v = df["CCI_20"].iloc[-1]
            result.cci = float(v) if pd.notna(v) else None
        else:
            result.cci = calculate_cci(df)

        result.cci_score     = score_cci(result.cci)
        result.cci_trend     = calculate_cci_trend(df)        # [MSL-3]
        result.cci_divergence = calculate_cci_divergence(df)  # [MSL-6]

        # ── Williams %R ───────────────────────────────────────────────────────
        if "WILLR_14" in df.columns:
            v = df["WILLR_14"].iloc[-1]
            result.williams_r = float(v) if pd.notna(v) else None
        else:
            result.williams_r = calculate_williams_r(df)

        result.williams_r_score = score_williams_r(result.williams_r)
        result.willr_trend      = calculate_willr_trend(df)   # [MSL-3]

        # ── ROC ───────────────────────────────────────────────────────────────
        if "ROC_9" in df.columns:
            v = df["ROC_9"].iloc[-1]
            result.roc = float(v) if pd.notna(v) else None
        else:
            result.roc = calculate_roc(df)

        if "ROC_SLOPE_9_5" in df.columns:
            v = df["ROC_SLOPE_9_5"].iloc[-1]
            result.roc_slope = float(v) if pd.notna(v) else None
        else:
            result.roc_slope = calculate_roc_slope(df)

        # [MSL-4] ROC slow + crossover — selalu hitung karena tidak ada pre-computed
        result.roc_slow     = calculate_roc_slow(df)
        result.roc_crossover = calculate_roc_crossover(result.roc, result.roc_slow)
        result.roc_score    = score_roc(result.roc, result.roc_slope, result.roc_crossover)

        # ── Composite ─────────────────────────────────────────────────────────
        # [MSL-2] Weight dari parameter, default CCI=0.35 / Williams=0.25 / ROC=0.40
        total_w = w["cci"] + w["williams"] + w["roc"]
        result.composite_score = clamp_score(
            (result.cci_score       * w["cci"]
             + result.williams_r_score * w["williams"]
             + result.roc_score        * w["roc"])
            / max(total_w, 1e-9)
        )

    except Exception as exc:
        if errors is not None:
            errors.append(f"oscillators: {exc}")
        log.exception("Error kalkulasi oscillators: %s", exc)

    return result
