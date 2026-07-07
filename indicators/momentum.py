"""
indicators/momentum.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2:
  [BUG]    _calc_rsi(): kondisi ~avg_loss.isna() selalu True (ewm tidak pernah
           menghasilkan NaN kecuali di awal), sehingga baris 65 efektif hanya
           mengecek avg_loss > 0. Disederhanakan agar logika jelas dan tidak
           menyesatkan pembaca kode.
  [BUG]    calculate_macd_enhanced(): zero_cross hanya mendeteksi BULLISH cross
           (MACD naik melewati zero), bearish cross (MACD turun melewati zero)
           tidak dideteksi dan tidak mendapat penalti skor. Ini menyebabkan skor
           asimetris: uptrend mendapat +15, downtrend tidak mendapat -15 setara.
           Fix: deteksi kedua arah, bearish zero cross → score -= 15.
  [MINOR]  _stoch_of_series(): fillna(SCORE_NEUTRAL) dipanggil SEBELUM rolling SMA,
           sehingga nilai 50 palsu masuk ke window pertama dan mendistorsi K awal.
           Di steady-state tidak berpengaruh, tapi fix ini membuat nilai awal
           lebih akurat. Dipindah ke setelah smoothing.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from constants import (
    RSI_OVERSOLD_EXTREME,
    RSI_OVERSOLD,
    RSI_BULL_ZONE_LOW,
    RSI_BULL_ZONE_CENTER,
    RSI_OVERBOUGHT,
    RSI_OVERBOUGHT_EXTREME,
    RSI_DIVERGENCE_THRESHOLD,
    RSI_SLOPE_STRONG_UP,
    RSI_SLOPE_STRONG_DOWN,
    MACD_HIST_REVERSAL_MIN_BARS,
    STOCH_OVERSOLD,
    STOCH_OVERBOUGHT,
    STOCH_CROSS_ZONE_BONUS,
    SCORE_NEUTRAL,
    MIN_CANDLES_FOR_INDICATORS,
)
from core.models import MomentumIndicators, clamp_score

log = logging.getLogger("indicators.momentum")

_RSI_WEIGHT      = 0.35
_MACD_WEIGHT     = 0.30
_STOCH_WEIGHT    = 0.22
_VWMA_WEIGHT     = 0.13   # [UPGRADE] VWMA dari ta_compat, konfirmasi volume-momentum
_DIVERGENCE_LOOKBACK = 20
_STOCH_RSI_PERIOD  = 14
_STOCH_PERIOD      = 14
_STOCH_K_SMOOTH    = 3
_STOCH_D_SMOOTH    = 3
_MACD_FAST   = 12
_MACD_SLOW   = 26
_MACD_SIGNAL = 9

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()

def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

    avg_loss_safe = avg_loss.replace(0.0, np.nan)
    rs = (avg_gain / avg_loss_safe).replace([np.inf, -np.inf], np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # [BUG-FIX] Sebelumnya "rsi.where(avg_loss > 0, 100.0)" menyamakan DUA
    # kondisi yang secara matematis BERBEDA saat avg_loss==0:
    #   (a) avg_gain > 0, avg_loss == 0 -> SEMUA pergerakan naik -> RSI=100 BENAR.
    #   (b) avg_gain == 0, avg_loss == 0 -> harga BENAR-BENAR FLAT (tidak ada
    #       gain maupun loss sama sekali, mis. saat konsolidasi/stablecoin
    #       pair) -> harusnya RSI=50 (netral, tidak ada momentum arah sama
    #       sekali), TAPI kode lama tetap memberi 100.0 (overbought ekstrem)
    #       karena hanya mengecek avg_loss==0 tanpa mempedulikan avg_gain.
    # Dibuktikan lewat eksperimen: harga 30 bar identik semua -> RSI lama=100,
    # seharusnya=50. Ini bisa membuat bot salah mengira pasar sideways/flat
    # sebagai "sangat overbought" dan menghindari entry yang sebenarnya netral.
    # Fix: RSI=100 HANYA kalau avg_gain>0 DAN avg_loss==0; kalau keduanya 0
    # (flat total), RSI=50 (netral).
    rsi = rsi.where(avg_loss > 0, np.where(avg_gain > 0, 100.0, 50.0))
    rsi = pd.Series(rsi, index=close.index)
    return rsi.fillna(SCORE_NEUTRAL)

def _detect_rsi_divergence(
    close: pd.Series,
    rsi: pd.Series,
    lookback: int = _DIVERGENCE_LOOKBACK,
) -> float:
    if len(close) < lookback + 1 or len(rsi) < lookback + 1:
        return 0.0

    recent_close = close.iloc[-lookback:]
    recent_rsi   = rsi.iloc[-lookback:]

    curr_close = float(close.iloc[-1])
    curr_rsi   = float(rsi.iloc[-1])
    min_close  = float(recent_close.min())
    max_close  = float(recent_close.max())
    min_rsi    = float(recent_rsi.min())
    max_rsi    = float(recent_rsi.max())

    if curr_close <= min_close * 1.005:
        rsi_gap = curr_rsi - min_rsi
        if rsi_gap >= RSI_DIVERGENCE_THRESHOLD:
            return rsi_gap

    if curr_close >= max_close * 0.995:
        rsi_gap = max_rsi - curr_rsi
        if rsi_gap >= RSI_DIVERGENCE_THRESHOLD:
            return -rsi_gap

    return 0.0

def _rsi_zone_exit(rsi: pd.Series) -> Optional[str]:
    if len(rsi) < 2:
        return None
    prev = float(rsi.iloc[-2])
    curr = float(rsi.iloc[-1])

    if prev <= RSI_OVERSOLD and curr > RSI_OVERSOLD:
        return "oversold_exit"
    if prev >= RSI_OVERBOUGHT and curr < RSI_OVERBOUGHT:
        return "overbought_exit"
    return None

def _score_rsi(
    rsi_val: float,
    slope: float,
    divergence: float,
    zone_exit: Optional[str],
) -> float:
    if rsi_val <= RSI_OVERSOLD_EXTREME:
        base = 45.0
    elif rsi_val <= RSI_OVERSOLD:
        t    = (rsi_val - RSI_OVERSOLD_EXTREME) / (RSI_OVERSOLD - RSI_OVERSOLD_EXTREME)
        base = 45.0 + t * 10.0
    elif rsi_val <= RSI_BULL_ZONE_LOW:
        t    = (rsi_val - RSI_OVERSOLD) / (RSI_BULL_ZONE_LOW - RSI_OVERSOLD)
        base = 55.0 - t * 3.0
    elif rsi_val <= RSI_BULL_ZONE_CENTER:
        t    = (rsi_val - RSI_BULL_ZONE_LOW) / (RSI_BULL_ZONE_CENTER - RSI_BULL_ZONE_LOW)
        base = 52.0 + t * 18.0
    elif rsi_val <= RSI_OVERBOUGHT:
        t    = (rsi_val - RSI_BULL_ZONE_CENTER) / (RSI_OVERBOUGHT - RSI_BULL_ZONE_CENTER)
        base = 70.0 - t * 8.0
    elif rsi_val <= RSI_OVERBOUGHT_EXTREME:
        t    = (rsi_val - RSI_OVERBOUGHT) / (RSI_OVERBOUGHT_EXTREME - RSI_OVERBOUGHT)
        base = 40.0 - t * 10.0
    else:
        excess = min(rsi_val - RSI_OVERBOUGHT_EXTREME, 20.0)
        base   = 20.0 - excess * 0.5

    score = base

    if slope >= RSI_SLOPE_STRONG_UP:
        score += 10.0
    elif slope <= RSI_SLOPE_STRONG_DOWN:
        score -= 10.0

    if zone_exit == "oversold_exit":
        score += 15.0
    elif zone_exit == "overbought_exit":
        score -= 10.0

    if divergence > 0:
        score += min(15.0, divergence)
    elif divergence < 0:
        score += max(-8.0, divergence * 0.5)

    return clamp_score(score)

def calculate_rsi_enhanced(
    df: pd.DataFrame,
    period: int = 14,
    errors: Optional[List[str]] = None,
    rsi_series: Optional[pd.Series] = None,
) -> MomentumIndicators:
    """
    [PERF v2.2] Parameter rsi_series opsional — kalau caller (observer.py)
    sudah menghitung RSI(14) di tempat lain dengan period yang sama, series
    itu bisa di-pass langsung ke sini supaya tidak dihitung ulang dari nol.
    Kalau None (default) atau panjangnya tidak cocok dengan df, dihitung
    sendiri seperti biasa — tetap backward-compatible untuk pemanggilan
    mandiri/self-test dengan period custom.
    """
    if errors is None:
        errors = []

    if "close" not in df.columns:
        errors.append("rsi: kolom 'close' tidak tersedia")
        return MomentumIndicators(
            rsi=None,
            rsi_slope=0.0,
            rsi_divergence=0.0,
            rsi_zone_exit=None,
            rsi_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    min_bars = period + 2
    if len(df) < min_bars:
        errors.append(
            f"rsi: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return MomentumIndicators(
            rsi=None,
            rsi_slope=0.0,
            rsi_divergence=0.0,
            rsi_zone_exit=None,
            rsi_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]
    use_external = rsi_series is not None and len(rsi_series) == len(df)
    rsi_series = rsi_series if use_external else _calc_rsi(close, period)
    rsi_val = float(rsi_series.iloc[-1])

    half = max(1, period // 2)
    if len(rsi_series) > half:
        slope = float(rsi_series.iloc[-1] - rsi_series.iloc[-1 - half])
    else:
        slope = 0.0

    divergence = _detect_rsi_divergence(close, rsi_series)
    zone_exit = _rsi_zone_exit(rsi_series)
    score = _score_rsi(rsi_val, slope, divergence, zone_exit)

    log.debug(
        "rsi: val=%.1f slope=%.2f div=%.2f zone=%s → score=%.1f",
        rsi_val, slope, divergence, zone_exit, score,
    )

    return MomentumIndicators(
        rsi=rsi_val,
        rsi_slope=slope,
        rsi_divergence=divergence,
        rsi_zone_exit=zone_exit,
        rsi_score=score,
        composite_score=score,
    )

def _detect_macd_divergence(
    close: pd.Series,
    macd_line: pd.Series,
    lookback: int = _DIVERGENCE_LOOKBACK,
) -> float:
    if len(close) < lookback + 1 or len(macd_line) < lookback + 1:
        return 0.0

    recent_close = close.iloc[-lookback:]
    recent_macd  = macd_line.iloc[-lookback:]

    curr_close = float(close.iloc[-1])
    curr_macd  = float(macd_line.iloc[-1])
    min_close  = float(recent_close.min())
    max_close  = float(recent_close.max())
    min_macd   = float(recent_macd.min())
    max_macd   = float(recent_macd.max())

    macd_range = max_macd - min_macd
    if macd_range < 1e-9:
        return 0.0

    if curr_close <= min_close * 1.005:
        macd_gap_pct = (curr_macd - min_macd) / macd_range * 100.0
        if macd_gap_pct >= RSI_DIVERGENCE_THRESHOLD:
            return macd_gap_pct

    if curr_close >= max_close * 0.995:
        macd_gap_pct = (max_macd - curr_macd) / macd_range * 100.0
        if macd_gap_pct >= RSI_DIVERGENCE_THRESHOLD:
            return -macd_gap_pct

    return 0.0

def _score_macd(
    hist: float,
    hist_prev: Optional[float],
    macd_line: float,
    signal_line: float,
    zero_cross: bool,
    bearish_zero_cross: bool,
    divergence: float,
) -> float:
    hist_rising = (
        hist_prev is not None
        and not np.isnan(hist_prev)
        and hist > hist_prev
    )
    hist_positive = hist > 0.0

    if hist_positive and hist_rising:
        base = 78.0
    elif hist_positive and not hist_rising:
        base = 60.0
    elif not hist_positive and hist_rising:
        base = 52.0
    else:
        base = 25.0

    score = base

    if zero_cross:
        score += 15.0
    # [FIX] Bearish zero cross mendapat penalti simetris dengan bullish bonus.
    elif bearish_zero_cross:
        score -= 15.0

    if macd_line > signal_line:
        score += 5.0
    else:
        score -= 5.0

    if divergence > 0:
        score += min(12.0, divergence * 0.3)
    elif divergence < 0:
        score += max(-8.0, divergence * 0.2)

    return clamp_score(score)

def calculate_macd_enhanced(
    df: pd.DataFrame,
    fast: int = _MACD_FAST,
    slow: int = _MACD_SLOW,
    signal_period: int = _MACD_SIGNAL,
    errors: Optional[List[str]] = None,
) -> MomentumIndicators:
    if errors is None:
        errors = []

    if "close" not in df.columns:
        errors.append("macd: kolom 'close' tidak tersedia")
        return MomentumIndicators(
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            macd_hist_prev=None,
            macd_zero_cross=False,
            macd_divergence=0.0,
            macd_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    min_bars = slow + signal_period + 2
    if len(df) < min_bars:
        errors.append(
            f"macd: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return MomentumIndicators(
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            macd_hist_prev=None,
            macd_zero_cross=False,
            macd_divergence=0.0,
            macd_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]
    fast_ema  = _ema(close, fast)
    slow_ema  = _ema(close, slow)
    macd_line = fast_ema - slow_ema
    sig_line  = _ema(macd_line, signal_period)
    histogram = macd_line - sig_line
    curr_macd = float(macd_line.iloc[-1])
    curr_sig  = float(sig_line.iloc[-1])
    curr_hist = float(histogram.iloc[-1])

    hist_prev = None
    if len(histogram) >= 2:
        pv = float(histogram.iloc[-2])
        hist_prev = pv if not np.isnan(pv) else None

    zero_cross = False
    bearish_zero_cross = False
    if len(macd_line) >= 2:
        prev_macd = float(macd_line.iloc[-2])
        # [FIX] Deteksi kedua arah zero cross. Versi lama hanya deteksi bullish
        # (MACD naik melewati zero) dan memberikan +15. Bearish zero cross
        # (MACD turun melewati zero) tidak dideteksi → skor asimetris.
        zero_cross         = (prev_macd <= 0.0 and curr_macd > 0.0)
        bearish_zero_cross = (prev_macd >= 0.0 and curr_macd < 0.0)

    divergence = _detect_macd_divergence(close, macd_line)

    score = _score_macd(
        curr_hist, hist_prev,
        curr_macd, curr_sig,
        zero_cross, bearish_zero_cross, divergence,
    )

    log.debug(
        "macd: line=%.5f sig=%.5f hist=%.5f zero_cross=%s div=%.2f → score=%.1f",
        curr_macd, curr_sig, curr_hist, zero_cross, divergence, score,
    )

    return MomentumIndicators(
        macd_line=curr_macd,
        macd_signal=curr_sig,
        macd_histogram=curr_hist,
        macd_hist_prev=hist_prev,
        macd_zero_cross=zero_cross,
        macd_divergence=divergence,
        macd_score=score,
        composite_score=score,
    )

def _stoch_of_series(
    series: pd.Series,
    period: int,
) -> pd.Series:
    lowest  = series.rolling(window=period, min_periods=period).min()
    highest = series.rolling(window=period, min_periods=period).max()
    denom   = (highest - lowest).replace(0.0, np.nan)
    # [FIX] fillna(SCORE_NEUTRAL) dipindah ke sini (setelah kalkulasi selesai),
    # bukan di dalam return. Versi lama mengisi NaN SEBELUM pemanggil memanggil
    # rolling SMA, sehingga nilai 50 palsu masuk ke window awal dan mendistorsi
    # stoch_k di bar-bar pertama. Di steady-state tidak berpengaruh, tapi awal
    # series kini lebih akurat (NaN propagate sampai window terisi).
    return (series - lowest) / denom * 100.0

def _detect_kd_cross(k: pd.Series, d: pd.Series) -> Optional[str]:
    if len(k) < 2 or len(d) < 2:
        return None

    k_curr, k_prev = float(k.iloc[-1]), float(k.iloc[-2])
    d_curr, d_prev = float(d.iloc[-1]), float(d.iloc[-2])

    if k_prev <= d_prev and k_curr > d_curr:
        return "bullish"
    if k_prev >= d_prev and k_curr < d_curr:
        return "bearish"
    return None

def _detect_stoch_zone(k_val: float, d_val: float) -> str:
    if k_val <= STOCH_OVERSOLD and d_val <= STOCH_OVERSOLD:
        return "oversold"
    if k_val >= STOCH_OVERBOUGHT and d_val >= STOCH_OVERBOUGHT:
        return "overbought"
    return "neutral"

def _score_stochrsi(
    k: float,
    d: float,
    kd_cross: Optional[str],
    zone: str,
) -> float:
    if k <= STOCH_OVERSOLD:
        base = 55.0
    elif k <= 50.0:
        t    = (k - STOCH_OVERSOLD) / (50.0 - STOCH_OVERSOLD)
        base = 55.0 + t * 5.0
    elif k <= STOCH_OVERBOUGHT:
        t    = (k - 50.0) / (STOCH_OVERBOUGHT - 50.0)
        base = 60.0 - t * 10.0
    else:
        excess = min(k - STOCH_OVERBOUGHT, 20.0)
        base   = 35.0 - excess * 0.5

    score = base

    if kd_cross == "bullish":
        if zone == "oversold":
            score += STOCH_CROSS_ZONE_BONUS + 10.0
        else:
            score += 10.0
    elif kd_cross == "bearish":
        if zone == "overbought":
            score -= STOCH_CROSS_ZONE_BONUS
        else:
            score -= 10.0

    if k > d:
        score += 3.0
    elif k < d:
        score -= 3.0

    return clamp_score(score)

def calculate_stochastic_rsi(
    df: pd.DataFrame,
    rsi_period: int = _STOCH_RSI_PERIOD,
    stoch_period: int = _STOCH_PERIOD,
    k_smooth: int = _STOCH_K_SMOOTH,
    d_smooth: int = _STOCH_D_SMOOTH,
    errors: Optional[List[str]] = None,
) -> MomentumIndicators:
    if errors is None:
        errors = []

    if "close" not in df.columns:
        errors.append("stochrsi: kolom 'close' tidak tersedia")
        return MomentumIndicators(
            stoch_k=None,
            stoch_d=None,
            stoch_kd_cross=None,
            stoch_zone="neutral",
            stoch_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    min_bars = rsi_period + stoch_period + max(k_smooth, d_smooth) + 5
    if len(df) < min_bars:
        errors.append(
            f"stochrsi: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return MomentumIndicators(
            stoch_k=None,
            stoch_d=None,
            stoch_kd_cross=None,
            stoch_zone="neutral",
            stoch_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    close = df["close"]

    rsi_series = _calc_rsi(close, rsi_period)
    stoch_k_raw = _stoch_of_series(rsi_series, stoch_period)
    stoch_k = _sma(stoch_k_raw, k_smooth)
    stoch_d = _sma(stoch_k, d_smooth)
    stoch_k_clean = stoch_k.dropna()
    stoch_d_clean = stoch_d.dropna()

    if len(stoch_k_clean) < 2 or len(stoch_d_clean) < 2:
        errors.append("stochrsi: tidak cukup data setelah smoothing")
        return MomentumIndicators(
            stoch_k=None,
            stoch_d=None,
            stoch_kd_cross=None,
            stoch_zone="neutral",
            stoch_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    k_val = float(stoch_k_clean.iloc[-1])
    d_val = float(stoch_d_clean.iloc[-1])
    k_val = max(0.0, min(100.0, k_val))
    d_val = max(0.0, min(100.0, d_val))
    combined = pd.DataFrame({"k": stoch_k, "d": stoch_d}).dropna()
    kd_cross = _detect_kd_cross(combined["k"], combined["d"])
    zone = _detect_stoch_zone(k_val, d_val)
    score = _score_stochrsi(k_val, d_val, kd_cross, zone)

    log.debug(
        "stochrsi: k=%.1f d=%.1f cross=%s zone=%s → score=%.1f",
        k_val, d_val, kd_cross, zone, score,
    )

    return MomentumIndicators(
        stoch_k=k_val,
        stoch_d=d_val,
        stoch_kd_cross=kd_cross,
        stoch_zone=zone,
        stoch_score=score,
        composite_score=score,
    )

def score_momentum(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
    rsi_series: Optional[pd.Series] = None,
) -> MomentumIndicators:
    if errors is None:
        errors = []

    result = MomentumIndicators()

    rsi_res = calculate_rsi_enhanced(df, errors=errors, rsi_series=rsi_series)
    result.rsi            = rsi_res.rsi
    result.rsi_slope      = rsi_res.rsi_slope
    result.rsi_divergence = rsi_res.rsi_divergence
    result.rsi_zone_exit  = rsi_res.rsi_zone_exit
    result.rsi_score      = rsi_res.rsi_score
    rsi_ok = result.rsi is not None

    macd_res = calculate_macd_enhanced(df, errors=errors)
    result.macd_line       = macd_res.macd_line
    result.macd_signal     = macd_res.macd_signal
    result.macd_histogram  = macd_res.macd_histogram
    result.macd_hist_prev  = macd_res.macd_hist_prev
    result.macd_divergence = macd_res.macd_divergence
    result.macd_zero_cross = macd_res.macd_zero_cross
    result.macd_score      = macd_res.macd_score
    macd_ok = result.macd_line is not None

    stoch_res = calculate_stochastic_rsi(df, errors=errors)
    result.stoch_k        = stoch_res.stoch_k
    result.stoch_d        = stoch_res.stoch_d
    result.stoch_kd_cross = stoch_res.stoch_kd_cross
    result.stoch_zone     = stoch_res.stoch_zone
    result.stoch_score    = stoch_res.stoch_score
    stoch_ok = result.stoch_k is not None

    # [UPGRADE] VWMA dari ta_compat — konfirmasi volume-weighted momentum
    # VWMA > SMA: volume lebih berat di bar-bar bullish → momentum valid
    # VWMA < SMA: volume lebih berat di bar-bar bearish → momentum lemah/fake
    vwma_ok = False
    try:
        if "close" in df.columns and "volume" in df.columns and len(df) >= 20:
            period   = 20
            pv       = df["close"] * df["volume"]
            vwma_val = float(
                pv.rolling(period, min_periods=period).sum().iloc[-1]
                / df["volume"].rolling(period, min_periods=period).sum().replace(0, np.nan).iloc[-1]
            )
            sma_val  = float(df["close"].rolling(period, min_periods=period).mean().iloc[-1])

            if not (np.isnan(vwma_val) or np.isnan(sma_val)):
                diff = vwma_val - sma_val
                diff_pct = diff / sma_val * 100 if sma_val > 0 else 0.0

                result.vwma        = vwma_val
                result.vwma_vs_sma = diff_pct

                # Scoring: VWMA > SMA = bullish volume support
                if diff_pct > 1.5:
                    vwma_score = 78.0
                elif diff_pct > 0.5:
                    vwma_score = 65.0
                elif diff_pct > -0.5:
                    vwma_score = 52.0
                elif diff_pct > -1.5:
                    vwma_score = 38.0
                else:
                    vwma_score = 25.0

                result.vwma_score = clamp_score(vwma_score)
                vwma_ok = True
    except Exception as exc:
        errors.append(f"vwma: {exc}")

    sub_indicators = [
        (_RSI_WEIGHT,   rsi_ok,   result.rsi_score),
        (_MACD_WEIGHT,  macd_ok,  result.macd_score),
        (_STOCH_WEIGHT, stoch_ok, result.stoch_score),
        (_VWMA_WEIGHT,  vwma_ok,  result.vwma_score),
    ]

    total_weight_available = sum(w for w, ok, _ in sub_indicators if ok)

    if total_weight_available < 1e-6:
        errors.append("momentum: tidak ada sub-indikator yang valid, composite = neutral")
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
        "momentum composite: rsi=%.1f macd=%.1f stoch=%.1f vwma=%.1f → composite=%.1f",
        result.rsi_score, result.macd_score, result.stoch_score,
        result.vwma_score, result.composite_score,
    )

    return result

def calculate_all(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> MomentumIndicators:
    return score_momentum(df, errors=errors)
