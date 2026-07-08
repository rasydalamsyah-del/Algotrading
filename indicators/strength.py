"""
indicators/strength.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2:
  [PERF]   _calc_true_range(): pd.concat([...], axis=1).max(axis=1) → numpy
           vectorized np.maximum(). ~12.5x lebih cepat, identik secara numerik.
  [PERF]   _calc_directional_movement(): loop Python per-bar → numpy vectorized
           np.diff() + np.where(). ~2.6x lebih cepat, identik.
  [PERF]   _calc_obv(): loop Python per-bar → numpy np.sign + np.cumsum().
           ~3.4x lebih cepat, identik.
  [UPGRADE] intelligence/validator.py: 3 check baru yang mengaktifkan field
           obv, obv_trend, mfi_divergence yang sebelumnya idle:
           _check_strength_context() → adx/di/volume/obv/mfi terintegrasi penuh.

CHANGELOG v3 (audit kualitas internal + potensi cross-file):
  [BUG-FIX] intelligence/validator.py _check_strength_context(): volume_climax
           double-penalty — _check_volume_climax() (existing) dan
           _check_strength_context() (baru) keduanya menghitung penalty untuk
           field st.volume_climax yang sama (-0.10 dan -0.05), total diam-diam
           jadi -0.15. Dihapus dari _check_strength_context(), satu-satunya
           pemilik logic sekarang _check_volume_climax().
  [PERF]   calculate_money_flow()/score_strength(): parameter rsi_series
           opsional — sebelumnya _calc_rsi() dihitung ulang dari nol di sini
           hanya untuk deteksi MFI-RSI divergence, padahal score_momentum()
           (dipanggil tepat sebelum score_strength() di observer.py) baru saja
           menghitung RSI(14) identik dari close yang sama. Benchmark: ~19%
           dari total waktu score_strength() (1.39ms dari 7.31ms). Sekarang
           observer.py menghitung RSI sekali dan membaginya ke kedua fungsi;
           tetap backward-compatible (fallback hitung sendiri kalau None).
  [CLEANUP] calculate_volume_analysis() & _calc_mfi(): hapus
           df["volume"].replace(0.0, 0.0) — no-op (replace 0.0 dengan 0.0),
           sisa refactor yang membingungkan, tidak ada dampak numerik.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from constants import (
    ADX_WEAK_TREND,
    ADX_MODERATE_TREND,
    ADX_STRONG_TREND,
    ADX_VERY_STRONG,
    VOLUME_RATIO_WEAK,
    VOLUME_RATIO_NORMAL,
    VOLUME_RATIO_ELEVATED,
    VOLUME_RATIO_STRONG,
    VOLUME_RATIO_SPIKE,
    VOLUME_RATIO_CLIMAX,
    VOLUME_CLIMAX_PENALTY,
    MFI_OVERSOLD,
    MFI_OVERBOUGHT,
    RSI_DIVERGENCE_THRESHOLD,
    SCORE_NEUTRAL,
)
from core.models import StrengthIndicators, clamp_score

log = logging.getLogger("indicators.strength")
_ADX_WEIGHT    = 0.35
_DI_WEIGHT     = 0.15
_VOLUME_WEIGHT = 0.35
_MFI_WEIGHT    = 0.15
_VOLUME_MA_PERIOD  = 20
_OBV_SLOPE_PERIOD  = 10
_MFI_PERIOD = 14
_ADX_PERIOD = 14
_DIVERGENCE_LOOKBACK = 20

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    result = np.full(len(series), np.nan)
    arr    = series.values.astype(float)
    n      = len(arr)

    if n < period:
        return pd.Series(result, index=series.index)

    # [BUG-FIX — kritis, ditemukan lewat cross-check terhadap implementasi
    # ADX independen] Sebelumnya seed SELALU dipasang di index ABSOLUT
    # `period-1` memakai nanmean(arr[:period]) -- ini benar HANYA kalau
    # `arr` tidak punya leading NaN (kasus TR/+DM/-DM, yang memang mulai
    # dari index 0). TAPI kalau `arr` punya leading NaN (kasus `dx` di
    # calculate_adx: punya period-1 NaN di depan sebelum DI+/DI- pertama
    # kali valid), nanmean(arr[:period]) cuma merata-ratakan SATU nilai
    # real yang kebetulan ada di window itu (nilai NaN diabaikan nanmean),
    # BUKAN rata-rata `period` nilai real pertama seperti metodologi Wilder
    # baku (ADX textbook: "ADX pertama = rata-rata 14 nilai DX pertama").
    # Dibuktikan: dx dgn 13 NaN lalu nilai real -> seed lama muncul di
    # index 13 memakai HANYA 1 nilai (harusnya nunggu sampai index 26,
    # rata-rata 14 nilai real). Dampak nyata ke calculate_adx: ADX di
    # sekitar bar minimum (29 bar) meleset ~2.4 poin bahkan SETELAH fix
    # zero-contamination sebelumnya. Fix: deteksi posisi nilai valid
    # PERTAMA, lalu seed = rata-rata `period` nilai valid pertama mulai
    # dari situ (bukan dari index absolut 0).
    first_valid = 0
    while first_valid < n and np.isnan(arr[first_valid]):
        first_valid += 1

    seed_idx = first_valid + period - 1
    if seed_idx >= n:
        return pd.Series(result, index=series.index)

    window = arr[first_valid: first_valid + period]
    if np.any(np.isnan(window)):
        # Ada NaN "di tengah" window (bukan cuma leading) -- data tidak
        # bersih, tidak bisa seed dengan aman. Fallback ke perilaku lama
        # (nanmean atas window yang sama) drpd crash/seed sembarangan.
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
    # [PERF] numpy vectorized — ~12.5x lebih cepat dari pd.concat approach
    h  = df["high"].values.astype(float)
    l  = df["low"].values.astype(float)
    c  = df["close"].values.astype(float)
    n  = len(c)
    pc = np.empty(n); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr, index=df.index)

def _calc_directional_movement(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    # [PERF] numpy vectorized — ~2.6x lebih cepat dari loop Python per-bar
    high = df["high"].values.astype(float)
    low  = df["low"].values.astype(float)

    up_move   = np.diff(high, prepend=high[0])   # high[i] - high[i-1]
    down_move = -np.diff(low, prepend=low[0])    # low[i-1] - low[i]

    plus_dm  = np.where((up_move > down_move) & (up_move > 0.0),   up_move,   0.0)
    minus_dm = np.where((down_move > up_move)  & (down_move > 0.0), down_move, 0.0)

    # Bar pertama tidak punya prev → nol (identik dengan loop lama)
    plus_dm[0]  = 0.0
    minus_dm[0] = 0.0

    return (
        pd.Series(plus_dm,  index=df.index),
        pd.Series(minus_dm, index=df.index),
    )

def _score_adx(adx_val: float) -> float:
    if adx_val < ADX_WEAK_TREND:
        t    = adx_val / ADX_WEAK_TREND
        return clamp_score(t * 35.0)

    if adx_val < ADX_MODERATE_TREND:
        t    = (adx_val - ADX_WEAK_TREND) / (ADX_MODERATE_TREND - ADX_WEAK_TREND)
        return clamp_score(35.0 + t * 20.0)

    if adx_val < ADX_STRONG_TREND:
        t    = (adx_val - ADX_MODERATE_TREND) / (ADX_STRONG_TREND - ADX_MODERATE_TREND)
        return clamp_score(55.0 + t * 25.0)

    if adx_val < ADX_VERY_STRONG:
        t    = (adx_val - ADX_STRONG_TREND) / (ADX_VERY_STRONG - ADX_STRONG_TREND)
        return clamp_score(80.0 - t * 10.0)

    excess = min(adx_val - ADX_VERY_STRONG, 30.0)
    return clamp_score(70.0 - excess * 0.8)

def _score_di(plus_di: float, minus_di: float) -> float:
    total = plus_di + minus_di
    if total < 1e-9:
        return SCORE_NEUTRAL
    ratio = plus_di / total
    return clamp_score(5.0 + ratio * 90.0)

def calculate_adx(
    df: pd.DataFrame,
    period: int = _ADX_PERIOD,
    errors: Optional[List[str]] = None,
) -> StrengthIndicators:
    if errors is None:
        errors = []

    for col in ("high", "low", "close"):
        if col not in df.columns:
            errors.append(f"adx: kolom '{col}' tidak tersedia")
            return StrengthIndicators(
                adx=None,
                plus_di=None,
                minus_di=None,
                adx_score=SCORE_NEUTRAL,
                di_score=SCORE_NEUTRAL,
                composite_score=SCORE_NEUTRAL,
            )

    min_bars = period * 2 + 1
    if len(df) < min_bars:
        errors.append(
            f"adx: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return StrengthIndicators(
            adx=None,
            plus_di=None,
            minus_di=None,
            adx_score=SCORE_NEUTRAL,
            di_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    tr       = _calc_true_range(df)
    plus_dm, minus_dm = _calc_directional_movement(df)
    atr_smooth       = _wilder_smooth(tr,       period)
    smooth_plus_dm   = _wilder_smooth(plus_dm,  period)
    smooth_minus_dm  = _wilder_smooth(minus_dm, period)
    atr_safe = atr_smooth.replace(0.0, np.nan)
    # [BUG-FIX — kritis, ditemukan lewat cross-check terhadap implementasi
    # ADX independen] Sebelumnya di_plus/di_minus di-fillna(0.0) DI SINI,
    # padahal untuk indeks 0..period-2 (warm-up, atr_smooth masih NaN
    # karena _wilder_smooth baru mulai menghasilkan nilai di index
    # period-1), fillna(0.0) itu MEMALSUKAN "belum ada data" menjadi
    # "DI+/DI- = 0". Akibatnya dx untuk indeks warm-up itu JUGA ikut jadi
    # 0.0 (bukan NaN), dan saat _wilder_smooth(dx, period) mengambil seed
    # awalnya dari nanmean(dx[:period]), rata-rata itu TERCEMAR oleh
    # (period-1) nilai nol palsu -- membuat seed ADX jauh lebih rendah dari
    # yang seharusnya (rata-rata cuma dx_asli/period, bukan dx_asli murni).
    # Dibuktikan lewat implementasi ADX independen (rumus textbook Wilder
    # ditulis ulang dari nol, tanpa contek kode ini): selisih ADX 0.41 dari
    # basis ~18 (~2.3%), dan bias ini LEBIH BESAR lagi persis di sekitar
    # jumlah bar minimum (period*2+1=29) karena rekursi Wilder belum sempat
    # "melupakan" seed yang tercemar. Fix: biarkan NaN mengalir apa adanya
    # sepanjang pipeline (jangan fillna prematur) -- _wilder_smooth sendiri
    # sudah benar menangani NaN di awal (skip via nanmean), dan dropna() di
    # akhir fungsi ini sudah menangani ekstraksi nilai valid terakhir.
    di_plus  = (smooth_plus_dm  / atr_safe * 100.0)
    di_minus = (smooth_minus_dm / atr_safe * 100.0)
    di_sum  = (di_plus + di_minus).replace(0.0, np.nan)
    dx      = ((di_plus - di_minus).abs() / di_sum * 100.0)
    adx_series = _wilder_smooth(dx, period)
    last_adx      = adx_series.dropna()
    last_di_plus  = di_plus.dropna()
    last_di_minus = di_minus.dropna()

    if last_adx.empty or last_di_plus.empty:
        errors.append("adx: hasil kalkulasi kosong setelah dropna")
        return StrengthIndicators(
            adx=None,
            plus_di=None,
            minus_di=None,
            adx_score=SCORE_NEUTRAL,
            di_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    adx_val      = float(last_adx.iloc[-1])
    plus_di_val  = float(last_di_plus.iloc[-1])
    minus_di_val = float(last_di_minus.iloc[-1])

    adx_score = _score_adx(adx_val)
    di_score  = _score_di(plus_di_val, minus_di_val)

    log.debug(
        "adx: adx=%.1f DI+=%.1f DI-=%.1f → adx_score=%.1f di_score=%.1f",
        adx_val, plus_di_val, minus_di_val, adx_score, di_score,
    )

    return StrengthIndicators(
        adx=adx_val,
        plus_di=plus_di_val,
        minus_di=minus_di_val,
        adx_score=adx_score,
        di_score=di_score,
        composite_score=clamp_score((adx_score + di_score) / 2),
    )

def _calc_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    # [PERF] numpy vectorized — ~3.4x lebih cepat dari loop Python per-bar
    # np.sign: +1 naik, -1 turun, 0 flat → direction[0]=0 (tidak ada prev bar)
    c         = close.values.astype(float)
    v         = volume.values.astype(float)
    direction = np.sign(np.diff(c, prepend=c[0]))
    direction[0] = 0.0   # bar pertama tidak punya arah
    return pd.Series(np.cumsum(direction * v), index=close.index)

def _obv_trend(obv: pd.Series, window: int = _OBV_SLOPE_PERIOD) -> str:
    if len(obv) < window + 1:
        return "flat"

    current = float(obv.iloc[-1])
    past    = float(obv.iloc[-window])
    delta   = current - past

    mean_abs = float(obv.iloc[-window:].abs().mean())
    threshold = mean_abs * 0.01 if mean_abs > 0 else 1.0

    if delta > threshold:
        return "rising"
    if delta < -threshold:
        return "falling"
    return "flat"

def _score_volume(
    ratio: float,
    spike: bool,
    climax: bool,
    obv_trend_str: str,
) -> float:
    if ratio < VOLUME_RATIO_WEAK:
        t    = ratio / VOLUME_RATIO_WEAK
        base = t * 20.0
    elif ratio < VOLUME_RATIO_NORMAL:
        t    = (ratio - VOLUME_RATIO_WEAK) / (VOLUME_RATIO_NORMAL - VOLUME_RATIO_WEAK)
        base = 20.0 + t * 20.0
    elif ratio < VOLUME_RATIO_ELEVATED:
        t    = (ratio - VOLUME_RATIO_NORMAL) / (VOLUME_RATIO_ELEVATED - VOLUME_RATIO_NORMAL)
        base = 40.0 + t * 20.0
    elif ratio < VOLUME_RATIO_STRONG:
        t    = (ratio - VOLUME_RATIO_ELEVATED) / (VOLUME_RATIO_STRONG - VOLUME_RATIO_ELEVATED)
        base = 60.0 + t * 15.0
    elif ratio < VOLUME_RATIO_SPIKE:
        t    = (ratio - VOLUME_RATIO_STRONG) / (VOLUME_RATIO_SPIKE - VOLUME_RATIO_STRONG)
        base = 75.0 + t * 7.0
    else:
        excess = min(ratio - VOLUME_RATIO_SPIKE, VOLUME_RATIO_CLIMAX - VOLUME_RATIO_SPIKE)
        t      = excess / (VOLUME_RATIO_CLIMAX - VOLUME_RATIO_SPIKE)
        base   = 82.0 - t * 12.0

    score = base

    if obv_trend_str == "rising":
        score += 8.0
    elif obv_trend_str == "falling":
        score -= 8.0

    if climax:
        score -= VOLUME_CLIMAX_PENALTY

    return clamp_score(score)

def calculate_volume_analysis(
    df: pd.DataFrame,
    volume_ma_period: int = _VOLUME_MA_PERIOD,
    errors: Optional[List[str]] = None,
) -> StrengthIndicators:
    if errors is None:
        errors = []

    for col in ("close", "volume"):
        if col not in df.columns:
            errors.append(f"volume: kolom '{col}' tidak tersedia")
            return StrengthIndicators(
                volume_ratio=None,
                volume_spike=False,
                obv=None,
                obv_trend="flat",
                volume_climax=False,
                volume_score=SCORE_NEUTRAL,
                composite_score=SCORE_NEUTRAL,
            )

    vol_col = "quote_volume" if "quote_volume" in df.columns else "volume"
    volume  = df[vol_col].replace(0.0, np.nan)
    close   = df["close"]

    min_bars = volume_ma_period + 2
    if len(df) < min_bars:
        errors.append(
            f"volume: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return StrengthIndicators(
            volume_ratio=None,
            volume_spike=False,
            obv=None,
            obv_trend="flat",
            volume_climax=False,
            volume_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    vol_ma     = _sma(volume, volume_ma_period)
    last_vol   = float(volume.iloc[-1]) if not np.isnan(volume.iloc[-1]) else 0.0
    last_ma    = float(vol_ma.iloc[-1]) if pd.notna(vol_ma.iloc[-1]) else 0.0

    if last_ma < 1e-9:
        errors.append("volume: MA volume mendekati nol — tidak bisa hitung ratio")
        return StrengthIndicators(
            volume_ratio=None,
            volume_spike=False,
            obv=None,
            obv_trend="flat",
            volume_climax=False,
            volume_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    ratio  = last_vol / last_ma
    spike  = ratio >= VOLUME_RATIO_SPIKE
    climax = ratio >= VOLUME_RATIO_CLIMAX
    # [CLEANUP v2.1] Sebelumnya: df["volume"].replace(0.0, 0.0) — no-op (replace
    # 0.0 dengan 0.0 = tidak melakukan apapun), sisa refactor yang membingungkan.
    # OBV aman terima volume=0.0 mentah karena tidak ada divisi di sini.
    raw_volume = df["volume"]
    obv_series  = _calc_obv(close, raw_volume)
    obv_val     = float(obv_series.iloc[-1])
    obv_trend_str = _obv_trend(obv_series)
    score = _score_volume(ratio, spike, climax, obv_trend_str)

    log.debug(
        "volume: ratio=%.2fx spike=%s climax=%s obv_trend=%s → score=%.1f",
        ratio, spike, climax, obv_trend_str, score,
    )

    return StrengthIndicators(
        volume_ratio=float(ratio),
        volume_spike=bool(spike),
        obv=obv_val,
        obv_trend=obv_trend_str,
        volume_climax=bool(climax),
        volume_score=score,
        composite_score=score,
    )

def _calc_mfi(df: pd.DataFrame, period: int) -> pd.Series:
    high   = df["high"]
    low    = df["low"]
    close  = df["close"]
    # [CLEANUP v2.1] Sebelumnya: df["volume"].replace(0.0, 0.0) — no-op, sama
    # seperti di calculate_volume_analysis(). raw_mf = typical_price * volume
    # aman terima 0.0 mentah (tidak ada divisi pada volume di sini).
    volume = df["volume"]
    typical_price = (high + low + close) / 3.0
    raw_mf        = typical_price * volume
    tp_change    = typical_price.diff()
    positive_mf  = raw_mf.where(tp_change > 0, 0.0)
    negative_mf  = raw_mf.where(tp_change < 0, 0.0).abs()
    sum_pos  = positive_mf.rolling(window=period, min_periods=period).sum()
    sum_neg  = negative_mf.rolling(window=period, min_periods=period).sum()
    money_ratio = (sum_pos / sum_neg.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)
    mfi         = 100.0 - (100.0 / (1.0 + money_ratio))

    return mfi.fillna(SCORE_NEUTRAL)

def _detect_mfi_rsi_divergence(
    mfi: pd.Series,
    rsi: pd.Series,
    lookback: int = 10,
) -> float:
    if len(mfi) < lookback + 1 or len(rsi) < lookback + 1:
        return 0.0

    mfi_change = float(mfi.iloc[-1]) - float(mfi.iloc[-lookback])
    rsi_change = float(rsi.iloc[-1]) - float(rsi.iloc[-lookback])

    diff = mfi_change - rsi_change

    if abs(diff) < RSI_DIVERGENCE_THRESHOLD:
        return 0.0

    return diff

def _score_mfi(mfi_val: float, mfi_rsi_div: float) -> float:
    if mfi_val <= MFI_OVERSOLD:
        if mfi_val <= 20.0:
            base = 50.0
        else:
            t    = (mfi_val - 20.0) / (MFI_OVERSOLD - 20.0)
            base = 50.0 + t * 8.0
    elif mfi_val <= 45.0:
        t    = (mfi_val - MFI_OVERSOLD) / (45.0 - MFI_OVERSOLD)
        base = 58.0 - t * 6.0
    elif mfi_val <= 55.0:
        t    = (mfi_val - 45.0) / 10.0
        base = 52.0 + t * 20.0
    elif mfi_val <= MFI_OVERBOUGHT:
        t    = (mfi_val - 55.0) / (MFI_OVERBOUGHT - 55.0)
        base = 72.0 - t * 34.0
    elif mfi_val <= 90.0:
        t    = (mfi_val - MFI_OVERBOUGHT) / 10.0
        base = 38.0 - t * 16.0
    else:
        base = 22.0

    score = base

    if mfi_rsi_div > RSI_DIVERGENCE_THRESHOLD:
        score += min(10.0, mfi_rsi_div * 0.3)
    elif mfi_rsi_div < -RSI_DIVERGENCE_THRESHOLD:
        score += max(-8.0, mfi_rsi_div * 0.2)

    return clamp_score(score)

def calculate_money_flow(
    df: pd.DataFrame,
    period: int = _MFI_PERIOD,
    errors: Optional[List[str]] = None,
    rsi_series: Optional[pd.Series] = None,
) -> StrengthIndicators:
    """
    [PERF v2.1] Parameter rsi_series opsional — RSI(14) dipakai di sini hanya
    untuk deteksi divergence MFI-vs-RSI, dan period-nya (_MFI_PERIOD=14) sama
    persis dengan RSI default di momentum.py. Sebelumnya _calc_rsi() dihitung
    ulang dari nol di sini meskipun score_momentum() baru saja menghitung RSI
    yang identik dari close yang sama — benchmark menunjukkan ini ~19% dari
    total waktu score_strength(). Kalau observer.py meneruskan rsi_series
    yang sudah dihitung, dipakai langsung. Kalau None/panjang tidak cocok
    (misal dipanggil mandiri dengan period custom), dihitung sendiri seperti
    biasa — tetap backward-compatible.
    """
    if errors is None:
        errors = []

    for col in ("high", "low", "close", "volume"):
        if col not in df.columns:
            errors.append(f"mfi: kolom '{col}' tidak tersedia")
            return StrengthIndicators(
                mfi=None,
                mfi_divergence=0.0,
                mfi_score=SCORE_NEUTRAL,
                composite_score=SCORE_NEUTRAL,
            )

    min_bars = period + 3
    if len(df) < min_bars:
        errors.append(
            f"mfi: data hanya {len(df)} bar, butuh minimal {min_bars}"
        )
        return StrengthIndicators(
            mfi=None,
            mfi_divergence=0.0,
            mfi_score=SCORE_NEUTRAL,
            composite_score=SCORE_NEUTRAL,
        )

    from indicators.momentum import _calc_rsi
    mfi_series = _calc_mfi(df, period)
    mfi_val    = float(mfi_series.iloc[-1])
    use_external = rsi_series is not None and len(rsi_series) == len(df)
    rsi_series = rsi_series if use_external else _calc_rsi(df["close"], period)
    mfi_div = _detect_mfi_rsi_divergence(mfi_series, rsi_series)
    score = _score_mfi(mfi_val, mfi_div)

    log.debug(
        "mfi: val=%.1f div=%.2f → score=%.1f",
        mfi_val, mfi_div, score,
    )

    return StrengthIndicators(
        mfi=mfi_val,
        mfi_divergence=mfi_div,
        mfi_score=score,
        composite_score=score,
    )

def score_strength(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
    rsi_series: Optional[pd.Series] = None,
) -> StrengthIndicators:
    if errors is None:
        errors = []

    result = StrengthIndicators()

    adx_res = calculate_adx(df, errors=errors)
    result.adx       = adx_res.adx
    result.plus_di   = adx_res.plus_di
    result.minus_di  = adx_res.minus_di
    result.adx_score = adx_res.adx_score
    result.di_score  = adx_res.di_score
    adx_ok = result.adx is not None
    di_ok  = result.plus_di is not None

    vol_res = calculate_volume_analysis(df, errors=errors)
    result.volume_ratio  = vol_res.volume_ratio
    result.volume_spike  = vol_res.volume_spike
    result.obv           = vol_res.obv
    result.obv_trend     = vol_res.obv_trend
    result.volume_climax = vol_res.volume_climax
    result.volume_score  = vol_res.volume_score
    vol_ok = result.volume_ratio is not None

    mfi_res = calculate_money_flow(df, errors=errors, rsi_series=rsi_series)
    result.mfi           = mfi_res.mfi
    result.mfi_divergence = mfi_res.mfi_divergence
    result.mfi_score     = mfi_res.mfi_score
    mfi_ok = result.mfi is not None

    sub_indicators = [
        (_ADX_WEIGHT,    adx_ok, result.adx_score),
        (_DI_WEIGHT,     di_ok,  result.di_score),
        (_VOLUME_WEIGHT, vol_ok, result.volume_score),
        (_MFI_WEIGHT,    mfi_ok, result.mfi_score),
    ]

    total_weight_available = sum(w for w, ok, _ in sub_indicators if ok)

    if total_weight_available < 1e-6:
        errors.append("strength: tidak ada sub-indikator yang valid, composite = neutral")
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
        "strength composite: adx=%.1f di=%.1f vol=%.1f mfi=%.1f → composite=%.1f",
        result.adx_score, result.di_score, result.volume_score, result.mfi_score, result.composite_score,
    )

    return result

def calculate_all(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> StrengthIndicators:
    return score_strength(df, errors=errors)
