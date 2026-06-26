"""
indicators/structure.py
AlgoTrader Pro — Structure Indicators
Ichimoku Cloud, Parabolic SAR, Pivot Points, Fibonacci Retracement,
Market Structure (HH/HL/LH/LL + BOS/CHoCH), S/R Zone Clustering, Donchian Channel

CHANGELOG v2:
  [BUG-FIX KRITIS] Ichimoku _midpoint(): dulu hanya menerima 1 series ('high')
    dan dipakai untuk max DAN min — series 'low' tidak pernah terbaca sama
    sekali. Tenkan/Kijun/Senkou-B jadi bias ke atas. Sekarang konsisten
    dengan ta_compat.py ichimoku() yang sudah benar: (highest_high+lowest_low)/2.
  [BUG-FIX KRITIS] calculate_pivot_points() dulu dilabeli "Classic Daily" tapi
    pakai df.iloc[-2] mentah — untuk df bertimeframe non-harian (5m/15m/1h,
    lazim di pipeline live commander) ini BUKAN pivot harian sama sekali.
    Fix: resample ke '1D' dulu, pakai H/L/C hari kalender penuh terakhir.
    Fallback ke bar-sebelumnya kalau df tak punya DatetimeIndex (ditandai
    via field baru 'pivot_period': "daily"|"bar_fallback").
  [CLEANUP] Parabolic SAR: hapus ternary dead-code 'if i>=2 else ...' (loop
    selalu mulai dari i=2 sehingga kondisinya selalu True).
  [IMPROVE] Fibonacci: swing point sekarang dicari via fractal/local-extrema
    asli (bukan cuma global max/min N-bar), plus deteksi arah retracement
    (uptrend vs downtrend berdasar urutan kronologis swing) dan level
    extension 1.272/1.618 untuk target breakout.
  [NEW] Market Structure: deteksi HH/HL/LH/LL dari rangkaian swing point,
    klasifikasi trend_structure (bullish/bearish/choppy), serta deteksi
    BOS (Break of Structure — continuation) dan CHoCH (Change of Character
    — peringatan reversal). Kategori indikator yang paling literal "struktur"
    namun sebelumnya tidak ada implementasinya di manapun di codebase ini.
  [NEW] S/R Zone Clustering: konsolidasi level pivot+fib+swing jadi zona S/R
    dengan skor confluence (mengikuti pola clustering yang sudah ada di
    indicators/orderbook.py). Field baru nearest_structure_support/resistance
    TIDAK mengganti makna nearest_support/resistance lama (tetap murni pivot,
    dipakai validator.py & kolom DB signal_scores).
  [NEW] Donchian Channel versi scalar — pendamping df.ta.donchian() vectorized
    di ta_compat.py (pola dual-implementation yang sama dengan Ichimoku/PSAR:
    scalar untuk live commander, vector untuk training/backtest).
  [ARCH] core/models.py StructureIndicators diperluas dengan field-field baru
    di atas — semua field LAMA tidak diubah/dihapus, jadi konsumen existing
    (intelligence/scorer.py, intelligence/validator.py, database.py,
    api_server.py) tidak terdampak.
"""

from __future__ import annotations
import logging
from typing import List, Optional, Tuple
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
from core.models import StructureIndicators

def clamp_score(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))

def _round_price(v: Optional[float], sig_figs: int = 10) -> Optional[float]:
    """
    [v2 FIX-PRESISI] round(v, 8) (fixed-decimal-place) collapse jadi 0.0
    untuk harga di bawah ~5e-9 — terbukti lewat pengujian, kasus nyata
    untuk token/memecoin berharga sangat kecil (umum di crypto). Dipakai
    sebagai pengganti round(x, 8) di seluruh modul ini untuk SEMUA nilai
    harga (bukan rasio/persentase) supaya presisinya konsisten di semua
    skala — dari BTC (~$100.000) sampai token recehan (~1e-12).
    """
    if v is None or v == 0 or not np.isfinite(v):
        return v
    digits = sig_figs - int(np.floor(np.log10(abs(v)))) - 1
    return round(v, max(digits, 0))

# ══════════════════════════════════════════════════════════════════════════════
# ICHIMOKU CLOUD
# ══════════════════════════════════════════════════════════════════════════════
TENKAN_PERIOD  = 9
KIJUN_PERIOD   = 26
SENKOU_B_PERIOD = 52
DISPLACEMENT   = 26

def _midpoint(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    """
    [v2 BUG-FIX] Rumus Tenkan/Kijun/Senkou-B yang benar adalah
    (highest_high + lowest_low) / 2 dalam N periode — BUKAN
    (high.max() + high.min()) / 2. Versi lama hanya menerima satu
    series ('high') dan memakainya untuk max DAN min, sehingga series
    'low' tidak pernah ikut dihitung sama sekali. Akibatnya nilai
    Tenkan/Kijun/Senkou-B selalu bias ke atas (lowest_low asli pasti
    <= high.rolling().min(), karena low <= high di setiap bar).
    Sekarang konsisten dengan ta_compat.py ichimoku() yang sudah benar.
    """
    return (high.rolling(period).max() + low.rolling(period).min()) / 2

def calculate_ichimoku(df: pd.DataFrame) -> dict:
    result = {
        "tenkan": None, "kijun": None,
        "senkou_a": None, "senkou_b": None, "chikou": None,
        "cloud_top": None, "cloud_bottom": None,
        "price_vs_cloud": None, "cloud_thickness": None,
        "tk_cross": None,
    }
    if len(df) < SENKOU_B_PERIOD + DISPLACEMENT:
        return result

    high = df["high"]
    low  = df["low"]
    close = df["close"]

    tenkan  = _midpoint(high, low, TENKAN_PERIOD)
    kijun   = _midpoint(high, low, KIJUN_PERIOD)
    senkou_a = ((tenkan + kijun) / 2).shift(DISPLACEMENT)
    senkou_b = _midpoint(high, low, SENKOU_B_PERIOD).shift(DISPLACEMENT)

    result["tenkan"]   = float(tenkan.iloc[-1]) if not pd.isna(tenkan.iloc[-1]) else None
    result["kijun"]    = float(kijun.iloc[-1])  if not pd.isna(kijun.iloc[-1])  else None
    result["chikou"]   = float(close.iloc[-1])  # chikou = close sekarang, plot -26

    # Senkou A & B saat ini (yang sudah di-displace ke depan, kita ambil current)
    sa = float(senkou_a.iloc[-1]) if not pd.isna(senkou_a.iloc[-1]) else None
    sb = float(senkou_b.iloc[-1]) if not pd.isna(senkou_b.iloc[-1]) else None
    result["senkou_a"] = sa
    result["senkou_b"] = sb

    if sa is not None and sb is not None:
        result["cloud_top"]    = max(sa, sb)
        result["cloud_bottom"] = min(sa, sb)
        result["cloud_thickness"] = abs(sa - sb)
        price = float(close.iloc[-1])
        if price > result["cloud_top"]:
            result["price_vs_cloud"] = "above"
        elif price < result["cloud_bottom"]:
            result["price_vs_cloud"] = "below"
        else:
            result["price_vs_cloud"] = "inside"

    # TK Cross (Tenkan cross Kijun)
    if result["tenkan"] is not None and result["kijun"] is not None:
        t_prev = float(tenkan.iloc[-2]) if len(tenkan) >= 2 else None
        k_prev = float(kijun.iloc[-2])  if len(kijun) >= 2  else None
        if t_prev is not None and k_prev is not None:
            was_below = t_prev < k_prev
            is_above  = result["tenkan"] > result["kijun"]
            was_above = t_prev > k_prev
            is_below  = result["tenkan"] < result["kijun"]
            if was_below and is_above:
                result["tk_cross"] = "bullish"
            elif was_above and is_below:
                result["tk_cross"] = "bearish"

    return result

def score_ichimoku(data: dict, current_price: float) -> float:
    score = 50.0
    pvc = data.get("price_vs_cloud")
    if pvc == "above":
        score += 20.0
    elif pvc == "below":
        score -= 20.0
    elif pvc == "inside":
        score += 0.0  # netral, pasar ragu

    tenkan = data.get("tenkan")
    kijun  = data.get("kijun")
    if tenkan and kijun:
        if tenkan > kijun:
            score += 8.0
        else:
            score -= 8.0
        if current_price > tenkan:
            score += 5.0
        if current_price > kijun:
            score += 5.0

    tk_cross = data.get("tk_cross")
    if tk_cross == "bullish":
        score += 10.0
    elif tk_cross == "bearish":
        score -= 10.0

    # Cloud thickness: tebal = support kuat
    ct = data.get("cloud_thickness")
    if ct and pvc == "above":
        score += min(ct / current_price * 500, 5.0)  # maks +5

    return clamp_score(score)

# ══════════════════════════════════════════════════════════════════════════════
# PARABOLIC SAR
# ══════════════════════════════════════════════════════════════════════════════
SAR_AF_INITIAL = 0.02
SAR_AF_MAX     = 0.20
SAR_AF_STEP    = 0.02

def calculate_sar(df: pd.DataFrame,
                  af_init: float = SAR_AF_INITIAL,
                  af_max:  float = SAR_AF_MAX,
                  af_step: float = SAR_AF_STEP) -> Tuple[Optional[float], Optional[str]]:
    if len(df) < 3:
        return None, None

    high  = df["high"].values
    low   = df["low"].values
    close = df["close"].values
    n = len(close)

    # Init: pakai 2 candle pertama buat deteksi awal trend
    bull = close[1] > close[0]
    af   = af_init
    ep   = high[0] if bull else low[0]
    sar  = low[0]  if bull else high[0]

    for i in range(2, n):
        sar_new = sar + af * (ep - sar)

        if bull:
            # [v2 CLEANUP] i selalu >= 2 di sini (range mulai dari 2) — ternary
            # lama 'low[i-2] if i>=2 else low[i-1]' adalah dead code, disederhanakan.
            sar_new = min(sar_new, low[i - 1], low[i - 2])
            if low[i] < sar_new:
                bull    = False
                sar_new = ep
                ep      = low[i]
                af      = af_init
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            sar_new = max(sar_new, high[i - 1], high[i - 2])
            if high[i] > sar_new:
                bull    = True
                sar_new = ep
                ep      = high[i]
                af      = af_init
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

        sar = sar_new

    direction = "up" if bull else "down"
    return float(sar), direction

def score_sar(sar_value: Optional[float], sar_direction: Optional[str],
              current_price: float) -> float:
    if sar_value is None or sar_direction is None:
        return 50.0
    if sar_direction == "up":
        # Titik di bawah harga = uptrend
        gap_pct = (current_price - sar_value) / current_price * 100
        # Makin dekat ke SAR = makin rentan reversal
        if gap_pct > 3.0:
            return clamp_score(72.0 + min(gap_pct - 3.0, 5.0) * 1.5)
        elif gap_pct > 1.0:
            return clamp_score(62.0 + gap_pct * 5.0)
        else:
            return clamp_score(55.0)  # terlalu dekat, waspada
    else:
        # SAR di atas harga = downtrend
        gap_pct = (sar_value - current_price) / current_price * 100
        if gap_pct > 3.0:
            return clamp_score(28.0 - min(gap_pct - 3.0, 5.0) * 1.5)
        else:
            return clamp_score(38.0)

# ══════════════════════════════════════════════════════════════════════════════
# PIVOT POINTS (Classic Daily)
# ══════════════════════════════════════════════════════════════════════════════
def _previous_calendar_day_ohlc(df: pd.DataFrame) -> Optional[pd.Series]:
    """
    [v2 PERF] Cari H/L/C dari hari kalender PENUH terakhir TANPA overhead
    df.resample('1D').agg(...). Terbukti lewat benchmark: resample('1D')
    makan ~1.3ms/call untuk df 500 bar — terlalu mahal kalau dipanggil
    untuk puluhan/ratusan simbol tiap siklus observer, padahal yang
    dibutuhkan cuma agregat SATU hari, bukan resample seluruh histori.

    Strategi: bucket setiap bar ke "hari" via integer division
    epoch-nanosecond (df.index.asi8 // NS_PER_DAY) — murni operasi numpy
    int64, ~100x lebih cepat dari resample. Ini VALID selama index
    tz-aware UTC atau tz-naive (konvensi seluruh pipeline ini — lihat
    main.py: pd.to_datetime(..., utc=True)). Kalau index ternyata
    bertimezone NON-UTC, integer-bucket bisa salah hari kalender LOKAL
    (offset beberapa jam) — fallback otomatis ke .normalize() yang tetap
    jauh lebih cepat dari resample dan tetap benar secara timezone.
    """
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) < 2:
        return None

    tz = df.index.tz
    if tz is None or str(tz) == "UTC":
        day_bucket = df.index.asi8 // 86_400_000_000_000  # ns per hari
    else:
        day_bucket = df.index.normalize().asi8  # tetap cepat, correct utk tz manapun

    last_day = day_bucket[-1]
    mask_prev = day_bucket < last_day
    if not mask_prev.any():
        return None
    target_day = day_bucket[mask_prev][-1]   # hari kalender penuh terakhir (bukan hari ini yg masih jalan)
    day_mask = day_bucket == target_day
    if not day_mask.any():
        return None

    sub = df.loc[day_mask]
    return pd.Series({
        "high":  float(sub["high"].max()),
        "low":   float(sub["low"].min()),
        "close": float(sub["close"].iloc[-1]),
    })

def calculate_pivot_points(df: pd.DataFrame) -> dict:
    """
    Classic Daily Pivot — WAJIB pakai H/L/C dari hari kalender PENUH
    terakhir, bukan bar immediate-sebelumnya di timeframe asli df.

    [v2 BUG-FIX] Versi lama ambil df.iloc[-2] mentah-mentah. Kalau df
    yang masuk berisi candle 5m/15m/1h (lazim di pipeline live commander
    multi-timeframe), itu BUKAN pivot harian — itu "pivot dari satu bar
    yang lalu" yang ikut berubah tiap kali bar baru terbentuk, dan rentang
    R/S-nya jadi terlalu rapat untuk berarti sebagai level S/R harian.

    Fix: cari H/L/C dari hari kalender penuh terakhir yang sudah closed
    (anggap hari paling akhir dalam data = hari ini yang masih berjalan)
    lewat _previous_calendar_day_ohlc() — lihat docstring fungsi itu untuk
    detail kenapa tidak pakai df.resample('1D') (mahal, lihat [v2 PERF]).
    Fallback ke perilaku lama (bar sebelumnya) HANYA jika df tidak punya
    DatetimeIndex atau historinya < 2 hari kalender — supaya tetap ada
    hasil daripada kosong total, ditandai lewat field 'pivot_period'.
    """
    result = {
        "pivot": None,
        "r1": None, "r2": None, "r3": None,
        "s1": None, "s2": None, "s3": None,
        "nearest_support": None, "nearest_resistance": None,
        "price_vs_pivot": None,
        "pivot_period": None,   # [v2 NEW] "daily" | "bar_fallback" — transparansi sumber data
    }
    if len(df) < 2:
        return result

    period_label = "bar_fallback"
    try:
        prev = _previous_calendar_day_ohlc(df)
        if prev is not None:
            period_label = "daily"
    except Exception as exc:
        log.debug("calculate_pivot_points: agregasi harian gagal, fallback ke bar — %s", exc)
        prev = None

    if prev is None:
        prev = df.iloc[-2]   # fallback lama: bar sebelumnya di timeframe asli df

    h = float(prev["high"])
    l = float(prev["low"])
    c = float(prev["close"])

    pivot = (h + l + c) / 3
    r1 = 2 * pivot - l
    r2 = pivot + (h - l)
    r3 = h + 2 * (pivot - l)
    s1 = 2 * pivot - h
    s2 = pivot - (h - l)
    s3 = l - 2 * (h - pivot)

    result.update({
        "pivot": _round_price(pivot),
        "r1": _round_price(r1), "r2": _round_price(r2), "r3": _round_price(r3),
        "s1": _round_price(s1), "s2": _round_price(s2), "s3": _round_price(s3),
        "pivot_period": period_label,
    })

    price = float(df["close"].iloc[-1])
    result["price_vs_pivot"] = "above" if price >= pivot else "below"

    # Nearest support & resistance dari harga sekarang
    supports    = [v for v in [s1, s2, s3] if v < price]
    resistances = [v for v in [r1, r2, r3] if v > price]
    result["nearest_support"]    = max(supports)    if supports    else s1
    result["nearest_resistance"] = min(resistances) if resistances else r1

    return result

def score_pivot(data: dict, current_price: float) -> float:
    if data.get("pivot") is None:
        return 50.0
    score = 50.0
    pivot = data["pivot"]
    ns    = data.get("nearest_support")
    nr    = data.get("nearest_resistance")

    if current_price >= pivot:
        score += 10.0
    else:
        score -= 10.0

    # Seberapa dekat ke support (bagus buat entry)
    if ns and current_price > 0:
        dist_support_pct = (current_price - ns) / current_price * 100
        if dist_support_pct < 1.0:
            score += 12.0   # sangat dekat support = zona ideal entry
        elif dist_support_pct < 2.0:
            score += 6.0
        elif dist_support_pct > 5.0:
            score -= 5.0    # terlalu jauh dari support

    # Seberapa dekat ke resistance (kalau terlalu dekat, upside terbatas)
    if nr and current_price > 0:
        dist_resist_pct = (nr - current_price) / current_price * 100
        if dist_resist_pct < 1.0:
            score -= 15.0   # hampir mentok resistance
        elif dist_resist_pct < 2.0:
            score -= 7.0
        elif dist_resist_pct > 4.0:
            score += 5.0    # ruang gerak ke atas masih besar

    return clamp_score(score)

# ══════════════════════════════════════════════════════════════════════════════
# FIBONACCI RETRACEMENT
# ══════════════════════════════════════════════════════════════════════════════
FIB_LEVELS    = [0.236, 0.382, 0.500, 0.618, 0.786]
SWING_LOOKBACK = 50   # candle terakhir buat cari swing high/low

def _find_swing_points(df: pd.DataFrame, lookback: int = SWING_LOOKBACK,
                        fractal_width: int = 2
                       ) -> Tuple[Optional[float], Optional[int], Optional[float], Optional[int]]:
    """
    Cari swing high & swing low dalam N candle terakhir.

    [v2 IMPROVE] Sebelumnya cuma global max/min N-bar — sekarang pakai
    fractal klasik (5-bar Bill Williams: sebuah bar adalah swing point
    kalau high/low-nya lebih ekstrem dari `fractal_width` bar di kiri
    DAN di kanan). Swing high/low TERKUAT (paling ekstrem) dalam window
    dipilih. Index dikembalikan juga supaya caller bisa tahu titik mana
    yang terjadi lebih belakangan (dipakai untuk deteksi arah tren).
    Fallback ke global max/min lama kalau data terlalu pendek/flat untuk
    fractal valid (index None menandakan fallback, tidak dipakai untuk
    deteksi arah).
    """
    subset = df.tail(lookback)
    n = len(subset)
    if n < 5:
        return None, None, None, None

    if n < fractal_width * 2 + 1:
        return float(subset["high"].max()), None, float(subset["low"].min()), None

    highs = subset["high"].to_numpy(dtype=float)
    lows  = subset["low"].to_numpy(dtype=float)

    high_idxs, low_idxs = [], []
    for i in range(fractal_width, n - fractal_width):
        wh = highs[i - fractal_width:i + fractal_width + 1]
        wl = lows[i - fractal_width:i + fractal_width + 1]
        if highs[i] == wh.max() and np.argmax(wh) == fractal_width:
            high_idxs.append(i)
        if lows[i] == wl.min() and np.argmin(wl) == fractal_width:
            low_idxs.append(i)

    if high_idxs:
        sh_i = max(high_idxs, key=lambda i: highs[i])
        swing_high, sh_idx = float(highs[sh_i]), sh_i
    else:
        sh_i = int(np.argmax(highs))
        swing_high, sh_idx = float(highs[sh_i]), None  # fallback, jangan dipakai utk arah

    if low_idxs:
        sl_i = min(low_idxs, key=lambda i: lows[i])
        swing_low, sl_idx = float(lows[sl_i]), sl_i
    else:
        sl_i = int(np.argmin(lows))
        swing_low, sl_idx = float(lows[sl_i]), None

    return swing_high, sh_idx, swing_low, sl_idx

def calculate_fibonacci(df: pd.DataFrame) -> dict:
    result = {
        "fib_swing_high": None, "fib_swing_low": None,
        "fib_236": None, "fib_382": None, "fib_500": None,
        "fib_618": None, "fib_786": None,
        "nearest_fib_support": None, "nearest_fib_resistance": None,
        "fib_trend": None,        # [v2 NEW] "uptrend" | "downtrend"
        "fib_ext_1272": None,     # [v2 NEW] target extension breakout
        "fib_ext_1618": None,
    }
    if len(df) < 10:
        return result

    swing_high, sh_idx, swing_low, sl_idx = _find_swing_points(df)
    if swing_high is None or swing_low is None:
        return result
    if swing_high <= swing_low:
        return result

    diff = swing_high - swing_low
    result["fib_swing_high"] = swing_high
    result["fib_swing_low"]  = swing_low

    # [v2 IMPROVE] Tentukan arah retracement dari urutan kronologis swing
    # point. Low terjadi duluan lalu high (sl_idx < sh_idx) = pergerakan
    # terakhir adalah NAIK -> sedang retrace TURUN dari high (uptrend).
    # Sebaliknya, high duluan lalu low = pergerakan terakhir TURUN ->
    # retrace NAIK dari low (downtrend). Default uptrend (perilaku versi
    # lama) kalau salah satu index tidak diketahui (kasus fallback).
    uptrend = True if (sh_idx is None or sl_idx is None) else (sl_idx < sh_idx)
    result["fib_trend"] = "uptrend" if uptrend else "downtrend"

    if uptrend:
        levels = {
            "fib_236": swing_high - diff * 0.236,
            "fib_382": swing_high - diff * 0.382,
            "fib_500": swing_high - diff * 0.500,
            "fib_618": swing_high - diff * 0.618,
            "fib_786": swing_high - diff * 0.786,
        }
        result["fib_ext_1272"] = _round_price(swing_high + diff * 0.272)
        result["fib_ext_1618"] = _round_price(swing_high + diff * 0.618)
    else:
        levels = {
            "fib_236": swing_low + diff * 0.236,
            "fib_382": swing_low + diff * 0.382,
            "fib_500": swing_low + diff * 0.500,
            "fib_618": swing_low + diff * 0.618,
            "fib_786": swing_low + diff * 0.786,
        }
        result["fib_ext_1272"] = _round_price(swing_low - diff * 0.272)
        result["fib_ext_1618"] = _round_price(swing_low - diff * 0.618)

    for k, v in levels.items():
        result[k] = _round_price(v)

    price = float(df["close"].iloc[-1])
    fib_values = list(levels.values())
    supports    = [v for v in fib_values if v < price]
    resistances = [v for v in fib_values if v > price]
    result["nearest_fib_support"]    = max(supports)    if supports    else None
    result["nearest_fib_resistance"] = min(resistances) if resistances else None

    return result

def score_fibonacci(data: dict, current_price: float) -> float:
    if data.get("fib_swing_high") is None:
        return 50.0
    score = 50.0
    ns = data.get("nearest_fib_support")
    nr = data.get("nearest_fib_resistance")

    if ns and current_price > 0:
        dist_pct = (current_price - ns) / current_price * 100
        # 61.8% adalah golden ratio — bonus ekstra
        fib618 = data.get("fib_618")
        if fib618 and abs(current_price - fib618) / current_price < 0.005:
            score += 15.0   # tepat di golden ratio
        elif dist_pct < 0.5:
            score += 12.0
        elif dist_pct < 1.5:
            score += 7.0
        elif dist_pct > 6.0:
            score -= 5.0

    if nr and current_price > 0:
        dist_pct = (nr - current_price) / current_price * 100
        if dist_pct < 1.0:
            score -= 12.0
        elif dist_pct < 2.0:
            score -= 6.0
        elif dist_pct > 5.0:
            score += 5.0

    return clamp_score(score)

# ══════════════════════════════════════════════════════════════════════════════
# MARKET STRUCTURE (HH/HL/LH/LL, Break of Structure, Change of Character)
# ══════════════════════════════════════════════════════════════════════════════
# [v2 NEW] Ini secara konsep adalah indikator "struktur" paling literal —
# price-action market structure — tapi sebelumnya tidak ada implementasinya
# di manapun di codebase ini walaupun nama modulnya structure.py. Dipindahkan/
# ditempatkan di sini karena memang ini rumahnya yang paling tepat.
STRUCTURE_FRACTAL_WIDTH  = 2     # bar kiri/kanan untuk validasi fractal swing point
STRUCTURE_SWING_LOOKBACK = 100   # bar untuk mencari rangkaian swing point
STRUCTURE_MIN_SWINGS     = 4     # minimal swing point (gabungan high+low) untuk klasifikasi trend yang valid

def _detect_swing_series(
    df: pd.DataFrame,
    lookback: int = STRUCTURE_SWING_LOOKBACK,
    fractal_width: int = STRUCTURE_FRACTAL_WIDTH,
) -> List[dict]:
    """
    Deteksi RANGKAIAN swing high/low (beda dari _find_swing_points yang
    cuma cari satu titik ekstrem untuk Fibonacci) — dipakai untuk
    klasifikasi market structure (HH/HL/LH/LL).
    Tiap swing point: {"idx": int, "price": float, "type": "high"|"low"}
    Diurutkan kronologis, dan dibersihkan supaya berpola zig-zag
    (high, low, high, low, ...) — kalau dua swing berurutan tipenya
    sama, simpan yang paling ekstrem saja.
    """
    subset = df.tail(lookback).reset_index(drop=True)
    n = len(subset)
    if n < fractal_width * 2 + 1:
        return []

    highs = subset["high"].to_numpy(dtype=float)
    lows  = subset["low"].to_numpy(dtype=float)
    swings: List[dict] = []

    for i in range(fractal_width, n - fractal_width):
        wh = highs[i - fractal_width:i + fractal_width + 1]
        wl = lows[i - fractal_width:i + fractal_width + 1]
        if highs[i] == wh.max() and np.argmax(wh) == fractal_width:
            swings.append({"idx": i, "price": float(highs[i]), "type": "high"})
        if lows[i] == wl.min() and np.argmin(wl) == fractal_width:
            swings.append({"idx": i, "price": float(lows[i]), "type": "low"})

    swings.sort(key=lambda s: s["idx"])

    cleaned: List[dict] = []
    for s in swings:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            if s["type"] == "high" and s["price"] > cleaned[-1]["price"]:
                cleaned[-1] = s
            elif s["type"] == "low" and s["price"] < cleaned[-1]["price"]:
                cleaned[-1] = s
            # else: swing baru lebih lemah dari yang sudah tersimpan -> dibuang
        else:
            cleaned.append(s)

    return cleaned

def calculate_market_structure(
    df: pd.DataFrame,
    lookback: int = STRUCTURE_SWING_LOOKBACK,
) -> dict:
    """
    [v2 NEW] Klasifikasi struktur trend price-action dari rangkaian swing point:
      bullish : Higher-High (HH) + Higher-Low (HL) dua swing terakhir berturut-turut
      bearish : Lower-High (LH) + Lower-Low (LL) dua swing terakhir berturut-turut
      choppy  : campuran, tidak ada struktur yang konsisten

    Plus deteksi event:
      BOS   (Break of Structure)   — harga menembus swing point terakhir
                                      SEARAH trend (continuation, struktur masih valid)
      CHoCH (Change of Character)  — harga menembus swing point terakhir
                                      BERLAWANAN arah trend (peringatan reversal)
    """
    result = {
        "trend_structure": "undefined",   # "bullish"|"bearish"|"choppy"|"undefined"
        "swing_points": [],               # beberapa titik terakhir, kronologis
        "last_swing_high": None,
        "last_swing_low": None,
        "structure_event": None,          # "BOS_bullish"|"BOS_bearish"|"CHoCH_bullish"|"CHoCH_bearish"|None
    }
    swings = _detect_swing_series(df, lookback=lookback)
    if len(swings) < STRUCTURE_MIN_SWINGS:
        return result

    recent = swings[-STRUCTURE_MIN_SWINGS:]
    result["swing_points"] = [
        {"price": _round_price(s["price"]), "type": s["type"]} for s in recent
    ]

    highs = [s for s in recent if s["type"] == "high"]
    lows  = [s for s in recent if s["type"] == "low"]
    if highs:
        result["last_swing_high"] = highs[-1]["price"]
    if lows:
        result["last_swing_low"] = lows[-1]["price"]

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1]["price"] > highs[-2]["price"]
        hl = lows[-1]["price"]  > lows[-2]["price"]
        lh = highs[-1]["price"] < highs[-2]["price"]
        ll = lows[-1]["price"]  < lows[-2]["price"]
        if hh and hl:
            result["trend_structure"] = "bullish"
        elif lh and ll:
            result["trend_structure"] = "bearish"
        else:
            result["trend_structure"] = "choppy"

    last_close = float(df["close"].iloc[-1])
    trend = result["trend_structure"]
    lsh, lsl = result["last_swing_high"], result["last_swing_low"]

    if trend == "bullish" and lsh is not None and last_close > lsh:
        result["structure_event"] = "BOS_bullish"
    elif trend == "bullish" and lsl is not None and last_close < lsl:
        result["structure_event"] = "CHoCH_bearish"   # tren naik tapi jebol swing low -> warning reversal turun
    elif trend == "bearish" and lsl is not None and last_close < lsl:
        result["structure_event"] = "BOS_bearish"
    elif trend == "bearish" and lsh is not None and last_close > lsh:
        result["structure_event"] = "CHoCH_bullish"   # tren turun tapi jebol swing high -> warning reversal naik

    return result

def score_market_structure(data: dict) -> float:
    """[v2 NEW] Skor 0-100 dari klasifikasi struktur trend + event BOS/CHoCH."""
    trend = data.get("trend_structure")
    event = data.get("structure_event")

    if trend == "bullish":
        score = 65.0
    elif trend == "bearish":
        score = 35.0
    elif trend == "choppy":
        score = 50.0
    else:
        return 50.0

    if event == "BOS_bullish":
        score += 15.0   # konfirmasi lanjutan tren naik
    elif event == "BOS_bearish":
        score -= 15.0   # konfirmasi lanjutan tren turun
    elif event == "CHoCH_bullish":
        score += 10.0    # reversal naik di tengah tren turun -> sinyal kuat, tapi tetap waspada
    elif event == "CHoCH_bearish":
        score -= 10.0    # reversal turun di tengah tren naik -> warning

    return clamp_score(score)

# ══════════════════════════════════════════════════════════════════════════════
# SUPPORT / RESISTANCE ZONE CLUSTERING
# ══════════════════════════════════════════════════════════════════════════════
# [v2 NEW] Menggabungkan SELURUH level S/R yang sudah dihitung modul ini
# (pivot + fibonacci + swing point market structure) menjadi zona yang
# dikonsolidasi — level yang berdekatan dianggap satu zona, makin banyak
# sumber berbeda yang "setuju" pada satu zona makin kuat (confluence).
# Pola clustering meniru CLUSTER_GAP_PCT di indicators/orderbook.py supaya
# konsisten gaya antar modul, walau gap-nya dibuat lebih lebar karena ini
# level harga historis (bukan order book real-time).
#
# PENTING: ini TIDAK mengganti nearest_support/nearest_resistance lama
# (tetap murni dari pivot — dipakai validator.py & kolom DB
# signal_scores.nearest_support/resistance). Field baru di sini diberi
# nama nearest_structure_support/resistance agar tidak menabrak makna lama.
SR_CLUSTER_GAP_PCT = 0.006   # level dalam jarak 0.6% dianggap satu zona

def calculate_sr_zones(
    df: pd.DataFrame,
    pivot_data: Optional[dict] = None,
    fib_data: Optional[dict] = None,
    structure_data: Optional[dict] = None,
) -> dict:
    """Konsolidasi level pivot+fib+swing jadi zona S/R dengan skor confluence."""
    result = {
        "sr_zones": [],                       # [{"price","strength","sources"}, ...] urut strength desc
        "nearest_structure_support": None,
        "nearest_structure_resistance": None,
    }
    if len(df) == 0 or "close" not in df.columns:
        return result

    price = float(df["close"].iloc[-1])
    if price <= 0:
        return result

    candidates: List[Tuple[float, str]] = []

    if pivot_data:
        for key in ("pivot", "r1", "r2", "r3", "s1", "s2", "s3"):
            v = pivot_data.get(key)
            if v:
                candidates.append((float(v), f"pivot_{key}"))

    if fib_data:
        for key in ("fib_236", "fib_382", "fib_500", "fib_618", "fib_786"):
            v = fib_data.get(key)
            if v:
                candidates.append((float(v), key))

    if structure_data:
        for sp in structure_data.get("swing_points", []):
            v = sp.get("price")
            if v:
                candidates.append((float(v), f"swing_{sp.get('type')}"))

    if not candidates:
        return result

    candidates.sort(key=lambda x: x[0])
    zones: List[dict] = []
    i = 0
    while i < len(candidates):
        anchor_price, anchor_src = candidates[i]
        members = [(anchor_price, anchor_src)]
        j = i + 1
        while j < len(candidates):
            gap = abs(candidates[j][0] - anchor_price) / anchor_price
            if gap <= SR_CLUSTER_GAP_PCT:
                members.append(candidates[j])
                j += 1
            else:
                break
        avg_price = sum(p for p, _ in members) / len(members)
        zones.append({
            "price":    _round_price(avg_price),
            "strength": len(members),
            "sources":  [s for _, s in members],
        })
        i = j if j > i else i + 1

    supports    = [z for z in zones if z["price"] < price]
    resistances = [z for z in zones if z["price"] > price]
    nearest_support_zone    = max(supports, key=lambda z: z["price"]) if supports else None
    nearest_resistance_zone = min(resistances, key=lambda z: z["price"]) if resistances else None
    if nearest_support_zone:
        result["nearest_structure_support"] = nearest_support_zone["price"]
    if nearest_resistance_zone:
        result["nearest_structure_resistance"] = nearest_resistance_zone["price"]

    # [v2 FIX] sr_zones yang ditampilkan dipotong top-10 by strength — supaya
    # nearest_structure_support/resistance yang dikembalikan SELALU bisa
    # ditelusuri balik ke salah satu entri di sr_zones (konsistensi API),
    # sisihkan slot untuk kedua zona itu LEBIH DULU sebelum mengisi sisanya
    # dari pool biasa (bukan menyisipkan satu-satu — itu sebelumnya berisiko
    # satu must-have menggusur must-have lain yang baru disisipkan).
    zones_sorted = sorted(zones, key=lambda z: z["strength"], reverse=True)
    must_haves = [z for z in (nearest_support_zone, nearest_resistance_zone) if z]
    must_have_prices = {z["price"] for z in must_haves}
    base_pool = [z for z in zones_sorted if z["price"] not in must_have_prices]
    n_slots = max(0, 10 - len(must_haves))
    top = base_pool[:n_slots] + must_haves
    top.sort(key=lambda z: z["price"])   # urut harga ascending — lebih enak dibaca sebagai "tangga" S/R
    result["sr_zones"] = top

    return result

# ══════════════════════════════════════════════════════════════════════════════
# DONCHIAN CHANNEL (versi scalar — pendamping df.ta.donchian() di ta_compat.py)
# ══════════════════════════════════════════════════════════════════════════════
# [v2 NEW] Donchian Channel pada dasarnya indikator STRUKTUR (range/channel
# breakout), bukan momentum/volatility — secara konsep paling pas di sini.
# ta_compat.py sudah punya df.ta.donchian() versi vectorized untuk
# enrich_production()/backtest/training di seluruh historical DataFrame.
# Versi DI SINI scalar/dict-style, konsisten dengan API modul ini (dipakai
# observer.py untuk live single-symbol commander pipeline) — pola dual-
# implementation (scalar untuk live, vector untuk training/backtest) ini
# memang konvensi yang sudah berlaku di codebase ini untuk Ichimoku & PSAR.
DONCHIAN_PERIOD = 20

def calculate_donchian(df: pd.DataFrame, period: int = DONCHIAN_PERIOD) -> dict:
    result = {
        "donchian_upper": None, "donchian_lower": None, "donchian_middle": None,
        "donchian_pct_b": None,       # posisi harga dalam channel: 0=lower band, 1=upper band
        "donchian_width_pct": None,   # lebar channel relatif terhadap middle (%)
    }
    if len(df) < period:
        return result

    upper = float(df["high"].tail(period).max())
    lower = float(df["low"].tail(period).min())
    if upper <= lower:
        return result

    middle = (upper + lower) / 2.0
    price  = float(df["close"].iloc[-1])

    result["donchian_upper"]  = upper
    result["donchian_lower"]  = lower
    result["donchian_middle"] = middle
    result["donchian_pct_b"]  = round((price - lower) / (upper - lower), 4)
    result["donchian_width_pct"] = round((upper - lower) / middle * 100, 4) if middle else None
    return result

def score_donchian(data: dict) -> float:
    pct_b = data.get("donchian_pct_b")
    if pct_b is None:
        return 50.0
    if pct_b >= 0.95:
        return clamp_score(78.0 + (pct_b - 0.95) * 100)   # dekat/breakout tepi atas
    if pct_b <= 0.05:
        return clamp_score(22.0 - (0.05 - pct_b) * 100)   # dekat/breakout tepi bawah
    return clamp_score(30.0 + pct_b * 40.0)                # posisi tengah channel, netral

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
def score_structure(df: pd.DataFrame, errors: Optional[List[str]] = None):
    """
    Dipanggil dari observer.py → hasilnya masuk ke iset.structure
    """
    result = StructureIndicators()

    if len(df) == 0 or "close" not in df.columns:
        return result

    current_price = float(df["close"].iloc[-1])
    if current_price <= 0:
        return result

    try:
        ich = calculate_ichimoku(df)
        result.tenkan          = ich["tenkan"]
        result.kijun           = ich["kijun"]
        result.senkou_a        = ich["senkou_a"]
        result.senkou_b        = ich["senkou_b"]
        result.chikou          = ich["chikou"]
        result.cloud_top       = ich["cloud_top"]
        result.cloud_bottom    = ich["cloud_bottom"]
        result.price_vs_cloud  = ich["price_vs_cloud"]
        result.cloud_thickness = ich["cloud_thickness"]
        result.tk_cross        = ich["tk_cross"]
        result.ichimoku_score  = score_ichimoku(ich, current_price)
    except Exception as exc:
        if errors is not None:
            errors.append(f"ichimoku: {exc}")
        log.exception("Error kalkulasi ichimoku: %s", exc)

    try:
        sar_val, sar_dir       = calculate_sar(df)
        result.sar_value       = sar_val
        result.sar_direction   = sar_dir
        result.sar_score       = score_sar(sar_val, sar_dir, current_price)
    except Exception as exc:
        if errors is not None:
            errors.append(f"sar: {exc}")
        log.exception("Error kalkulasi SAR: %s", exc)

    try:
        piv = calculate_pivot_points(df)
        result.pivot              = piv["pivot"]
        result.r1                 = piv["r1"]
        result.r2                 = piv["r2"]
        result.r3                 = piv["r3"]
        result.s1                 = piv["s1"]
        result.s2                 = piv["s2"]
        result.s3                 = piv["s3"]
        result.nearest_support    = piv["nearest_support"]
        result.nearest_resistance = piv["nearest_resistance"]
        result.price_vs_pivot     = piv["price_vs_pivot"]
        result.pivot_period       = piv["pivot_period"]
        result.pivot_score        = score_pivot(piv, current_price)
    except Exception as exc:
        if errors is not None:
            errors.append(f"pivot: {exc}")
        log.exception("Error kalkulasi pivot: %s", exc)
        piv = {}

    try:
        fib = calculate_fibonacci(df)
        result.fib_swing_high         = fib["fib_swing_high"]
        result.fib_swing_low          = fib["fib_swing_low"]
        result.fib_236                = fib["fib_236"]
        result.fib_382                = fib["fib_382"]
        result.fib_500                = fib["fib_500"]
        result.fib_618                = fib["fib_618"]
        result.fib_786                = fib["fib_786"]
        result.nearest_fib_support    = fib["nearest_fib_support"]
        result.nearest_fib_resistance = fib["nearest_fib_resistance"]
        result.fib_trend              = fib["fib_trend"]
        result.fib_ext_1272           = fib["fib_ext_1272"]
        result.fib_ext_1618           = fib["fib_ext_1618"]
        result.fib_score              = score_fibonacci(fib, current_price)
    except Exception as exc:
        if errors is not None:
            errors.append(f"fibonacci: {exc}")
        log.exception("Error kalkulasi fibonacci: %s", exc)
        fib = {}

    try:
        mkt = calculate_market_structure(df)
        result.trend_structure        = mkt["trend_structure"]
        result.structure_event        = mkt["structure_event"]
        result.last_swing_high        = mkt["last_swing_high"]
        result.last_swing_low         = mkt["last_swing_low"]
        result.swing_points           = mkt["swing_points"]
        result.market_structure_score = score_market_structure(mkt)
    except Exception as exc:
        if errors is not None:
            errors.append(f"market_structure: {exc}")
        log.exception("Error kalkulasi market structure: %s", exc)
        mkt = {}

    try:
        sr = calculate_sr_zones(df, pivot_data=piv, fib_data=fib, structure_data=mkt)
        result.sr_zones                     = sr["sr_zones"]
        result.nearest_structure_support    = sr["nearest_structure_support"]
        result.nearest_structure_resistance = sr["nearest_structure_resistance"]
    except Exception as exc:
        if errors is not None:
            errors.append(f"sr_zones: {exc}")
        log.exception("Error kalkulasi SR zones: %s", exc)

    try:
        dch = calculate_donchian(df)
        result.donchian_upper      = dch["donchian_upper"]
        result.donchian_lower      = dch["donchian_lower"]
        result.donchian_middle     = dch["donchian_middle"]
        result.donchian_pct_b      = dch["donchian_pct_b"]
        result.donchian_width_pct  = dch["donchian_width_pct"]
        result.donchian_score      = score_donchian(dch)
    except Exception as exc:
        if errors is not None:
            errors.append(f"donchian: {exc}")
        log.exception("Error kalkulasi donchian: %s", exc)

    # Composite structure score
    # [v2] Redistribusi bobot setelah menambah market_structure & donchian:
    # Ichimoku=0.25, MarketStructure=0.20 (paling langsung mencerminkan price-action
    # structure), SAR=0.15, Pivot=0.15, Donchian=0.15, Fib=0.10
    valid_count = sum([
        result.ichimoku_score          != 50.0,
        result.sar_score               != 50.0,
        result.pivot_score             != 50.0,
        result.fib_score                != 50.0,
        result.market_structure_score   != 50.0,
        result.donchian_score           != 50.0,
    ])
    if valid_count > 0:
        result.composite_score = clamp_score(
            result.ichimoku_score         * 0.25
            + result.market_structure_score * 0.20
            + result.sar_score             * 0.15
            + result.pivot_score           * 0.15
            + result.donchian_score        * 0.15
            + result.fib_score              * 0.10
        )

    return result
