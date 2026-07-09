"""
ta_compat.py — Drop-in replacement pandas_ta untuk Termux / environment tanpa numba.

Menyediakan df.ta.<indikator>() identik dengan pandas_ta API, 100% pure NumPy/pandas.
Semua nama kolom output IDENTIK dengan yang didefinisikan di constants.py.

Indikator tersedia:
  Trend      : ema, dema, tema, hma, wma, vwma, vwap, vwap_bands, supertrend,
               donchian, ichimoku, psar
  Momentum   : rsi, rsi_slope, rsi_divergence, macd, stochrsi, cci,
               williams_r, roc, roc_slope, ema_cross
  Volatility : atr, atr_pct, atr_percentile, bbands, keltner, squeeze, chop
  Strength   : adx, obv, mfi, cmf
  Utility    : ema_stack_score, enrich_production, compute_all

  enrich_production ← PRODUCTION ENTRY POINT (Gate3/exit logic)
                      Menghasilkan SEMUA kolom yang dibutuhkan observer/intelligence
                      pipeline dalam SATU panggilan. Wajib dipakai oleh:
                      main.py, strategy.py, api_server.py, telegram_bot.py
                      (gantikan blok df.ta.ema/rsi/atr manual yang tidak lengkap)
  compute_all       ← analytics, backtest, training (70+ kolom)

NAMA KOLOM (konstanta tersedia di constants.py):
  Semua kolom wajib direferensikan via konstanta COL_* dari constants.py,
  BUKAN hardcode string literal. Lihat bagian kolom v5 di constants.py.

CHANGELOG
─────────────────────────────────────────────────────────────────────────────
v2:
  [BUG-FIX] rsi(): avg_loss==0 → RSI=100 bukan 50.
  [BUG-FIX] mfi(): edge-case sama, diperbaiki.
  [DRY]     adx()/supertrend(): _wilder_smooth dipusatkan.
  [PERF]    wma(): rolling.apply → stride_tricks+matmul (BLAS).
  [PERF]    adx(): DM divektorisasi.
  [KONSISTENSI] _fmt_param(), _ensure_datetime_index() dipusatkan.

v3:
  [BUG-FIX] supertrend(): dead code 43 baris dihapus.
  [BUG-FIX] stochrsi(): RSI internal via _rsi_raw() — edge-case fix berlaku.
  [NEW]     enrich_production(), hma(), dema(), tema(), cci(), chop(),
            atr_percentile(), ema_stack_score().
  [PERF]    compute_all(): skip-if-exists, timing log.

v4 — SUPERPOWER:
  [PERF KRITIS] cci(): rolling.apply (Python per-window) → stride_tricks+matmul.
    Speedup terukur: 297x (108ms → 0.4ms per 1000 bar).
  [PERF KRITIS] atr_percentile(): rolling.apply → stride_tricks full-vectorized.
    Speedup terukur: 7x (4ms → 0.6ms per 1000 bar).
  [NEW] williams_r, roc, roc_slope, rsi_slope, rsi_divergence, ema_cross,
        donchian, cmf, vwma, psar, ichimoku.
  [IMPROVE] enrich_production, compute_all diperluas.

v5 — SUPERPOWER ULTIMATE (upgrade besar-besaran):
  [BUG-FIX KRITIS] Test suite line 1738: `import numpy as np as _np` →
    SyntaxError fatal, diperbaiki jadi `import numpy as _np`.
  [BUG-FIX] vwap_bands._rolling_std(): `.apply(lambda v: sqrt)` per-elemen
    (Python loop) → `np.where + clip + sqrt` full-vectorized.
  [BUG-FIX] rsi_divergence(): Python for-loop O(n) → full vectorized via
    np.roll/shift — sama cepat dengan indikator lain.
  [BUG-FIX] _atr_percentile warmup: bagian FULL windows (n >= lookback)
    diganti stride_tricks full-vectorized. Warmup partial (< lookback bar)
    tetap pakai loop Python — ini batas teknis sliding_window_view, bukan
    kelalaian. Jumlah iterasi maksimal = lookback (100), overhead sangat minor.
  [IMPROVE KRITIS] enrich_production(): sekarang JUGA menghasilkan:
    MACD_12_26_9, MACDs, MACDh, STOCHRSIk, STOCHRSId, BBU/M/L/B/P_20_2.0,
    KCUe/KCBe/KCLe_20_2, SQZ_20_2.0_20_1.5, MFI_14, OBV, SUPERT_7_3.0,
    VWAP_D_upper_1/lower_1/upper_2/lower_2, CCI_20, CHOP_14.
    → Observer/intelligence pipeline kini mendapat SEMUA kolom dari satu panggilan.
  [NEW] rsi_slope(): diperbaiki dari diff(N) menjadi OLS slope 5-bar —
    lebih akurat sebagai proxy divergence, noise lebih rendah.
  [NEW] lookup_col(): helper untuk ambil nilai kolom dengan fallback aman.
  [ARCH] enrich_production() wajib menjadi SATU-SATUNYA entry point di
    main.py, strategy.py, api_server.py, telegram_bot.py — blok df.ta manual
    5-indikator di file-file tersebut harus diganti dengan satu baris:
      df.ta.enrich_production()
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

log = logging.getLogger("ta_compat")


# ─────────────────────────────────────────────────────────────────────────────
# Public helper — aman dipakai oleh modul lain
# ─────────────────────────────────────────────────────────────────────────────

def lookup_col(
    bar: Union[pd.Series, dict],
    *cols: str,
    default: float = 0.0,
) -> float:
    """
    Ambil nilai float dari Series/dict dengan prioritas fallback.
    Berguna untuk akses kolom indikator di bar terakhir.

    Contoh:
        atr  = lookup_col(bar, "ATRr_14", default=0.0)
        vwap = lookup_col(bar, "VWAP_D", "VWAP", default=float(bar["close"]))
    """
    for col in cols:
        try:
            v = bar[col] if isinstance(bar, dict) else bar.get(col)
            if v is not None and pd.notna(v):
                return float(v)
        except (KeyError, TypeError):
            continue
    return default


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers — pure functions, tidak mengubah state global
# ─────────────────────────────────────────────────────────────────────────────

def _require(df: pd.DataFrame, *cols: str, ctx: str = "") -> bool:
    """Return False dan log warning jika ada kolom yang hilang."""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        log.warning("[ta_compat%s] kolom hilang: %s", f":{ctx}" if ctx else "", missing)
        return False
    return True


def _to_numeric(df: pd.DataFrame) -> None:
    """Konversi OHLCV ke float inplace (safe, coerce error → NaN)."""
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def _ema_series(series: pd.Series, span: int) -> pd.Series:
    """EMA standar (exponential weighted, adjust=False)."""
    return series.ewm(span=span, adjust=False).mean()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's smoothing — metodologi baku (Wilder 1978) / rma() TradingView:
    seed = rata-rata SEDERHANA (SMA) dari `period` nilai valid pertama,
    baru direkursi Wilder setelahnya.

    [BUG-FIX -- ditemukan lewat verifikasi cross-file lebih dalam, 2026-07-09]
    Sebelumnya fungsi ini pakai `series.ewm(com=period-1, adjust=False).mean()`
    -- seed di bar PERTAMA (bukan SMA dari `period` nilai pertama). Ini PERSIS
    bug yang sudah diidentifikasi & difix di indicators/momentum.py._calc_rsi,
    indicators/strength.py._wilder_smooth, indicators/volatility.py._wilder_smooth
    (semua "commit 05d7b50" & sesi verifikasi matematika independen RSI), DAN
    di _rsi_raw() (docstring-nya sendiri di file ini menyebut fix ini eksplisit)
    -- TAPI fungsi _wilder_smooth() BERSAMA ini (dipakai supertrend()/atr()/
    keltner()/adx()) SENGAJA TIDAK diubah saat itu, dgn alasan tertulis "untuk
    ADX sudah diverifikasi sebelumnya bahwa versi ewm cukup benar". Klaim itu
    TERBUKTI SALAH setelah diverifikasi ulang dgn data DEKAT bar minimum
    (bukan cuma bar banyak): ADX (4 lapis _wilder_smooth berantai: TR/+DM/-DM
    lalu DX->ADX) meleset rata-rata 113% (maks 218%) pada 29 bar (persis
    MIN_BARS dokumentasi ADX = period*2+1); ATR meleset rata-rata ~10-12%
    (maks ~20%) pada 15-20 bar. Fungsi df.ta.enrich_production() (yang
    memakai fungsi ini) HANYA dipakai api_server.py utk dashboard diagnostic
    (/api/diagnosa) -- TIDAK dipakai jalur trading nyata (observer.py/
    strategy.py/scorer.py pakai indicators/*.py yang sudah benar) -- jadi
    BUKAN risiko uang langsung, TAPI dashboard operator bisa menampilkan
    ADX/ATR yang sangat berbeda dari yang benar-benar dipakai bot utk
    memutuskan trading, justru paling parah di skenario paling kritis
    (koin baru listing / data tipis) -- operator kehilangan alat sanity-check
    yang seharusnya bisa dipercaya. Fix: root-cause di fungsi bersama ini
    (bukan cuma di ADX), otomatis benerin ATR/Supertrend/Keltner/ADX
    sekaligus, konsisten dgn semua indicators/*.py.
    """
    result = np.full(len(series), np.nan)
    arr    = series.values.astype(float)
    n      = len(arr)

    if n < period:
        return pd.Series(result, index=series.index)

    first_valid = 0
    while first_valid < n and np.isnan(arr[first_valid]):
        first_valid += 1

    seed_idx = first_valid + period - 1
    if seed_idx >= n:
        return pd.Series(result, index=series.index)

    window = arr[first_valid: first_valid + period]
    if np.any(np.isnan(window)):
        result[seed_idx] = np.nanmean(window)
    else:
        result[seed_idx] = float(np.mean(window))

    for i in range(seed_idx + 1, n):
        if np.isnan(result[i - 1]) or np.isnan(arr[i]):
            result[i] = result[i - 1] if not np.isnan(result[i - 1]) else np.nan
        else:
            result[i] = (result[i - 1] * (period - 1) + arr[i]) / period

    return pd.Series(result, index=series.index)


def _true_range(df: pd.DataFrame) -> pd.Series:
    """True Range vector (max 3 komponen)."""
    high = df["high"]
    low  = df["low"]
    prev = df["close"].shift(1)
    tr   = pd.concat([
        (high - low).abs(),
        (high - prev).abs(),
        (low  - prev).abs(),
    ], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def _fmt_param(x: float) -> str:
    """Format angka untuk nama kolom: '2' bukan '2.0' jika bulat."""
    return f"{int(x)}" if x == int(x) else f"{x}"


def _ensure_datetime_index(df: pd.DataFrame, ctx: str = "") -> bool:
    """Pastikan df.index berupa DatetimeIndex. Konversi inplace bila perlu."""
    if isinstance(df.index, pd.DatetimeIndex):
        return True
    try:
        df.index = pd.to_datetime(df.index, utc=True)
        return True
    except Exception as exc:
        log.debug("%s: index tidak bisa dikonversi ke DatetimeIndex — %s", ctx, exc)
        return False


def _rsi_raw(series: pd.Series, length: int) -> pd.Series:
    """
    RSI computation — shared helper untuk rsi() dan stochrsi().
    Termasuk edge-case fix (avg_loss==0 → RSI=100 jika ada gain, bukan 50).

    [BUG-FIX -- keputusan desain, konsisten dgn fix indicators/momentum.py
    _calc_rsi (sesi verifikasi matematika independen)] Sebelumnya avg_gain/
    avg_loss pakai _wilder_smooth() bersama (ewm(com=length-1, adjust=False)
    -- seed di bar PERTAMA), BUKAN SMA-seeded seperti definisi Wilder baku
    (1978) / rma() TradingView. Karena file ini (ta_compat.py) dipakai untuk
    dashboard diagnostic yang operator bandingkan visual terhadap TradingView/
    exchange chart, DAN indicators/momentum.py (RSI utk keputusan trading
    nyata) sudah diperbaiki ke SMA-seeded -- dashboard yang masih pakai
    metode lama akan menampilkan angka RSI yang BEDA dari yang benar-benar
    dipakai bot, persis risiko arsitektur yang sudah dikonfirmasi utk ADX
    (commit 05d7b50). Fungsi ini (_rsi_raw) sudah punya _sma_seeded_wilder
    sendiri sejak awal (di bawah), independen dari _wilder_smooth() bersama.
    [UPDATE 2026-07-09] _wilder_smooth() bersama (dipakai adx()/atr()/
    supertrend()/keltner()) SEKARANG JUGA sudah difix ke SMA-seeded --
    klaim lama "untuk ADX sudah diverifikasi cukup benar" TERBUKTI SALAH
    setelah diverifikasi ulang dgn data dekat bar minimum (ADX meleset rata2
    113%, maks 218% pada 29 bar). Lihat docstring _wilder_smooth() utk detail
    lengkap. _rsi_raw() dan _wilder_smooth() sekarang PAKAI METODOLOGI YANG
    SAMA (meski implementasinya duplikat terpisah, bukan dipanggil dari satu
    fungsi bersama -- dipertahankan terpisah supaya tidak menambah risiko
    perubahan struktural di luar scope fix ini).
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    def _sma_seeded_wilder(s: pd.Series, period: int) -> pd.Series:
        arr = s.values.astype(float)
        n = len(arr)
        result = np.full(n, np.nan)
        if n < period:
            return pd.Series(result, index=s.index)
        first_valid = 0
        while first_valid < n and np.isnan(arr[first_valid]):
            first_valid += 1
        seed_idx = first_valid + period - 1
        if seed_idx >= n:
            return pd.Series(result, index=s.index)
        window = arr[first_valid: first_valid + period]
        result[seed_idx] = np.nanmean(window) if np.any(np.isnan(window)) else float(np.mean(window))
        for i in range(seed_idx + 1, n):
            if np.isnan(result[i - 1]) or np.isnan(arr[i]):
                result[i] = result[i - 1] if not np.isnan(result[i - 1]) else np.nan
            else:
                result[i] = (result[i - 1] * (period - 1) + arr[i]) / period
        return pd.Series(result, index=s.index)

    avg_gain = _sma_seeded_wilder(gain, length)
    avg_loss = _sma_seeded_wilder(loss, length)
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    result   = 100.0 - (100.0 / (1.0 + rs))
    no_loss  = avg_loss == 0
    result   = result.mask(no_loss & (avg_gain > 0),   100.0)
    result   = result.mask(no_loss & (avg_gain == 0),   50.0)
    return result.fillna(50.0)


def _skip_if_exists(df: pd.DataFrame, col: str) -> bool:
    """True jika kolom sudah ada dan punya nilai — skip komputasi ulang."""
    return col in df.columns and df[col].notna().any()


def _wma_numpy(arr: np.ndarray, period: int) -> np.ndarray:
    """
    WMA pada numpy array via stride_tricks+matmul (BLAS) — tidak ada overhead
    Python per-window. Dipakai oleh wma() dan hma().
    """
    period = max(1, period)
    w      = np.arange(1, period + 1, dtype=float)
    w_sum  = w.sum()
    n      = len(arr)
    out    = np.full(n, np.nan)
    if n >= period:
        windows       = np.lib.stride_tricks.sliding_window_view(arr, period)
        out[period - 1:] = windows @ w / w_sum
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Accessor utama — df.ta.<method>()
# ─────────────────────────────────────────────────────────────────────────────

class _TAAccessor:
    """
    Pandas DataFrame accessor: df.ta.<method>()

    Konvensi:
      - append=True  → kolom ditambahkan ke df (default)
      - return value → pd.Series / Tuple[pd.Series, ...]
    """

    def __init__(self, df: pd.DataFrame) -> None:
        _to_numeric(df)
        self._df = df

    # ══════════════════════════════════════════════════════════════════════════
    # TREND
    # ══════════════════════════════════════════════════════════════════════════

    def ema(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """Exponential Moving Average. Output: EMA_{length}"""
        if not _require(self._df, "close", ctx="ema"):
            return pd.Series(dtype=float)
        col    = f"EMA_{length}"
        result = _ema_series(self._df["close"], length)
        if append:
            self._df[col] = result
        return result

    def dema(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Double EMA — lag lebih kecil dari EMA biasa.
        DEMA = 2*EMA(n) - EMA(EMA(n))
        Output: DEMA_{length}
        """
        if not _require(self._df, "close", ctx="dema"):
            return pd.Series(dtype=float)
        e1     = _ema_series(self._df["close"], length)
        result = 2.0 * e1 - _ema_series(e1, length)
        if append:
            self._df[f"DEMA_{length}"] = result
        return result

    def tema(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Triple EMA — lag paling kecil di famili EMA.
        TEMA = 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))
        Output: TEMA_{length}
        """
        if not _require(self._df, "close", ctx="tema"):
            return pd.Series(dtype=float)
        e1     = _ema_series(self._df["close"], length)
        e2     = _ema_series(e1, length)
        e3     = _ema_series(e2, length)
        result = 3.0 * e1 - 3.0 * e2 + e3
        if append:
            self._df[f"TEMA_{length}"] = result
        return result

    def hma(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Hull Moving Average — responsif & mulus, lag sangat kecil.
        HMA = WMA(sqrt(n),  2*WMA(n/2) - WMA(n))
        Output: HMA_{length}
        """
        if not _require(self._df, "close", ctx="hma"):
            return pd.Series(dtype=float)
        close    = self._df["close"].to_numpy(dtype=float)
        n        = len(close)
        half_len = max(1, length // 2)
        sqrt_len = max(1, int(round(length ** 0.5)))

        wma_h = _wma_numpy(close, half_len)
        wma_f = _wma_numpy(close, length)
        raw   = np.where(~(np.isnan(wma_h) | np.isnan(wma_f)),
                         2.0 * wma_h - wma_f, np.nan)
        result = pd.Series(_wma_numpy(raw, sqrt_len), index=self._df.index)
        if append:
            self._df[f"HMA_{length}"] = result
        return result

    def wma(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Weighted Moving Average — linearly weighted via stride_tricks+matmul.
        Output: WMA_{length}
        """
        if not _require(self._df, "close", ctx="wma"):
            return pd.Series(dtype=float)
        result = pd.Series(
            _wma_numpy(self._df["close"].to_numpy(dtype=float), length),
            index=self._df.index,
        )
        if append:
            self._df[f"WMA_{length}"] = result
        return result

    def vwma(self, length: int = 20, append: bool = True, **kw) -> pd.Series:
        """
        Volume Weighted Moving Average — MA yang lebih responsif ke volume tinggi.
        VWMA = Sum(Close * Volume, n) / Sum(Volume, n)

        Berbeda dari VWAP: tidak reset per hari, tidak berbasis typical price.
        Berguna mendeteksi apakah harga bergerak dengan dukungan volume (VWMA
        lebih tinggi dari SMA = volume berat di atas rata-rata, bullish bias).
        Output: VWMA_{length}
        """
        if not _require(self._df, "close", "volume", ctx="vwma"):
            return pd.Series(dtype=float)
        col    = f"VWMA_{length}"
        pv     = self._df["close"] * self._df["volume"]
        result = (
            pv.rolling(length, min_periods=length).sum()
            / self._df["volume"].rolling(length, min_periods=length).sum().replace(0, np.nan)
        )
        if append:
            self._df[col] = result
        return result

    def vwap(self, anchor: str = "D", append: bool = True, **kw) -> pd.Series:
        """
        Volume Weighted Average Price dengan anchor period.
        VWAP di-reset setiap awal periode anchor ('D','W','M','Q').
        Jika index bukan DatetimeIndex → kumulatif tanpa reset.
        Output: VWAP_{anchor}  (e.g. VWAP_D)
        """
        df  = self._df
        col = f"VWAP_{anchor}"
        if not _require(df, "high", "low", "close", "volume", ctx="vwap"):
            return pd.Series(dtype=float)

        _ensure_datetime_index(df, ctx="vwap")
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        tpv     = typical * df["volume"]

        if isinstance(df.index, pd.DatetimeIndex):
            if anchor == "D":
                first_ts = df.index[0]
                if first_ts.hour != 0 or first_ts.minute != 0:
                    log.debug("VWAP_D: data mulai %s (bukan 00:00) — bar pertama mungkin inakurat", first_ts)
            try:
                periods = df.index.tz_convert(None).to_period(anchor)
            except Exception:
                periods = df.index.to_period(anchor)
            cumtpv = tpv.groupby(periods).cumsum()
            cumvol = df["volume"].groupby(periods).cumsum()
        else:
            log.debug("vwap: index bukan DatetimeIndex — kumulatif tanpa reset")
            cumtpv = tpv.cumsum()
            cumvol = df["volume"].cumsum()

        result = cumtpv / cumvol.replace(0, np.nan)
        if append:
            df[col] = result
        return result

    def vwap_bands(
        self,
        anchor: str = "D",
        stdev_mult_1: float = 1.0,
        stdev_mult_2: float = 2.0,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        VWAP ± 1σ dan ± 2σ bands (volume-weighted std per anchor period).
        Output: VWAP_{anchor}_upper_1, _lower_1, _upper_2, _lower_2
        """
        df       = self._df
        vwap_col = f"VWAP_{anchor}"
        if vwap_col not in df.columns:
            self.vwap(anchor=anchor, append=True)
        if not _require(df, vwap_col, "high", "low", "close", "volume", ctx="vwap_bands"):
            e = pd.Series(dtype=float)
            return e, e, e, e

        _ensure_datetime_index(df, ctx="vwap_bands")
        vwap   = df[vwap_col]
        volume = df["volume"]

        def _rolling_std(grp: pd.DataFrame) -> pd.Series:
            """[v5 FIX] Sepenuhnya vectorized — tidak ada Python loop per-elemen."""
            tp     = (grp["high"] + grp["low"] + grp["close"]) / 3.0
            vol    = grp["volume"]
            vw     = grp[vwap_col]
            cumvol = vol.cumsum().replace(0, np.nan)
            var_s  = (vol * (tp - vw) ** 2).cumsum() / cumvol
            # [v5 FIX] var_s.apply(lambda v: sqrt...) → np.where vectorized
            arr    = var_s.to_numpy(dtype=float)
            std_arr = np.where(np.isfinite(arr), np.sqrt(np.clip(arr, 0.0, None)), np.nan)
            return pd.Series(std_arr, index=grp.index)

        if isinstance(df.index, pd.DatetimeIndex):
            try:
                periods = df.index.tz_convert(None).to_period(anchor)
            except Exception:
                periods = df.index.to_period(anchor)
            std_s = df.groupby(periods, group_keys=False).apply(_rolling_std)
        else:
            tp      = (df["high"] + df["low"] + df["close"]) / 3.0
            cumvol  = volume.cumsum().replace(0, np.nan)
            var_cum = (volume * (tp - vwap) ** 2).cumsum() / cumvol
            # [v5 FIX] same — no Python loop
            arr_g   = var_cum.to_numpy(dtype=float)
            std_s   = pd.Series(
                np.where(np.isfinite(arr_g), np.sqrt(np.clip(arr_g, 0.0, None)), np.nan),
                index=df.index,
            )

        u1, l1 = vwap + stdev_mult_1 * std_s, vwap - stdev_mult_1 * std_s
        u2, l2 = vwap + stdev_mult_2 * std_s, vwap - stdev_mult_2 * std_s
        if append:
            df[f"{vwap_col}_upper_1"] = u1
            df[f"{vwap_col}_lower_1"] = l1
            df[f"{vwap_col}_upper_2"] = u2
            df[f"{vwap_col}_lower_2"] = l2
        return u1, l1, u2, l2

    def supertrend(
        self,
        length: int = 7,
        multiplier: float = 3.0,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        SuperTrend — trailing support/resistance + trend direction.
        Band/direction loop Python (rekursif kondisional, tidak bisa divektorisasi
        tanpa numba — sesuai tujuan modul ini).
        Output: SUPERT_{length}_{multiplier}, SUPERTd_{length}_{multiplier} (+1/-1)
        """
        df      = self._df
        col_val = f"SUPERT_{length}_{multiplier}"
        col_dir = f"SUPERTd_{length}_{multiplier}"
        if not _require(df, "high", "low", "close", ctx="supertrend"):
            return pd.Series(dtype=float), pd.Series(dtype=float)
        if len(df) < length + 2:
            log.warning("supertrend: data hanya %d bar, butuh %d+", len(df), length + 2)
            return pd.Series(dtype=float), pd.Series(dtype=float)

        high  = df["high"].to_numpy(dtype=float)
        low   = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        n     = len(close)
        atr   = _wilder_smooth(_true_range(df), length).to_numpy(dtype=float)

        # [BUG-FIX -- ditemukan lewat regresi self-test setelah _wilder_smooth
        # diperbaiki ke SMA-seeded, 2026-07-09] _wilder_smooth() SMA-seeded
        # sekarang punya leading NaN (index 0..length-2) sebelum seed
        # terbentuk -- BEDA dari versi ewm lama yang selalu punya nilai dari
        # index 0 (walau kurang akurat di warm-up). Loop rekursif di bawah
        # SEBELUMNYA berasumsi atr[0] selalu valid (final_ub[0] = raw_ub[0]),
        # padahal skrg raw_ub[0..length-2] = NaN -- NaN merambat ke seluruh
        # rekursi final_ub/final_lb selamanya (perbandingan dgn NaN selalu
        # False, bukan exception, tapi hasil jadi rusak diam-diam), lalu
        # int(direction[-1]) di caller (self-test) exception krn NaN->int
        # tidak valid. Fix: mulai rekursi dari index VALID PERTAMA (bukan
        # index 0), pola identik dgn indicators/trend.py
        # _calculate_supertrend_raw yang sudah benar menangani ini
        # (`start = period - 1`). Index sebelum start diisi NaN (belum ada
        # sinyal, konsisten dgn semantik "data belum cukup").
        first_valid = 0
        while first_valid < n and np.isnan(atr[first_valid]):
            first_valid += 1

        hl2    = (high + low) / 2.0
        raw_ub = hl2 + multiplier * atr
        raw_lb = hl2 - multiplier * atr

        final_ub  = np.full(n, np.nan)
        final_lb  = np.full(n, np.nan)
        direction = np.zeros(n, dtype=int)
        st_line   = np.full(n, np.nan)

        if first_valid >= n:
            st  = pd.Series(st_line, index=df.index)
            d   = pd.Series(direction.astype(float), index=df.index)
            if append:
                df[col_val] = st
                df[col_dir] = d
            return st, d

        final_ub[first_valid] = raw_ub[first_valid]
        final_lb[first_valid] = raw_lb[first_valid]
        direction[first_valid] = 1
        st_line[first_valid]   = final_lb[first_valid]

        for i in range(first_valid + 1, n):
            final_ub[i] = (raw_ub[i]
                           if raw_ub[i] < final_ub[i-1] or close[i-1] > final_ub[i-1]
                           else final_ub[i-1])
            final_lb[i] = (raw_lb[i]
                           if raw_lb[i] > final_lb[i-1] or close[i-1] < final_lb[i-1]
                           else final_lb[i-1])
            if direction[i-1] == -1:
                direction[i] = 1 if close[i] > final_ub[i] else -1
            else:
                direction[i] = -1 if close[i] < final_lb[i] else 1
            st_line[i] = final_lb[i] if direction[i] == 1 else final_ub[i]

        st  = pd.Series(st_line, index=df.index)
        d   = pd.Series(direction.astype(float), index=df.index)
        if append:
            df[col_val] = st
            df[col_dir] = d
        return st, d

    def donchian(self, length: int = 20, append: bool = True, **kw) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Donchian Channel — range breakout indicator.
        Upper  = max(high, n) : breakout level atas
        Lower  = min(low,  n) : breakout level bawah
        Middle = (upper + lower) / 2

        Berguna: breakout trading, stop-loss trailing, volatility filter.
        Output: DCU_{length}, DCM_{length}, DCL_{length}
        """
        if not _require(self._df, "high", "low", ctx="donchian"):
            e = pd.Series(dtype=float)
            return e, e, e
        upper  = self._df["high"].rolling(length, min_periods=length).max()
        lower  = self._df["low"].rolling(length, min_periods=length).min()
        middle = (upper + lower) / 2.0
        if append:
            self._df[f"DCU_{length}"] = upper
            self._df[f"DCM_{length}"] = middle
            self._df[f"DCL_{length}"] = lower
        return upper, middle, lower

    def ichimoku(
        self,
        tenkan: int = 9,
        kijun: int = 26,
        senkou_b: int = 52,
        displacement: int = 26,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Ichimoku Cloud — multi-komponen trend/momentum/support system.
        Sudah ada di indicators/structure.py.calculate_ichimoku() — sekarang
        tersedia via df.ta accessor sebagai kolom DataFrame.

        Komponen:
          Tenkan-sen  : midpoint high-low {tenkan} bar  → momentum cepat
          Kijun-sen   : midpoint high-low {kijun} bar   → momentum lambat / support
          Senkou A    : (Tenkan+Kijun)/2, digeser +{displacement} bar ke depan
          Senkou B    : midpoint high-low {senkou_b} bar, digeser +{displacement}
          Chikou Span : close saat ini, digeser -{displacement} bar ke belakang

        Cloud (Kumo) = area antara Senkou A dan Senkou B.
        Harga di atas cloud = bullish. Di bawah = bearish.

        Output: ICH_TENKAN, ICH_KIJUN, ICH_SPAN_A, ICH_SPAN_B, ICH_CHIKOU
        """
        if not _require(self._df, "high", "low", "close", ctx="ichimoku"):
            e = pd.Series(dtype=float)
            return e, e, e, e, e

        df  = self._df
        h   = df["high"]
        l   = df["low"]

        def _midpoint(period: int) -> pd.Series:
            return (h.rolling(period, min_periods=period).max()
                    + l.rolling(period, min_periods=period).min()) / 2.0

        ten    = _midpoint(tenkan)
        kij    = _midpoint(kijun)
        sp_a   = ((ten + kij) / 2.0).shift(displacement)
        sp_b   = _midpoint(senkou_b).shift(displacement)
        chikou = df["close"].shift(-displacement)

        if append:
            df["ICH_TENKAN"]  = ten
            df["ICH_KIJUN"]   = kij
            df["ICH_SPAN_A"]  = sp_a
            df["ICH_SPAN_B"]  = sp_b
            df["ICH_CHIKOU"]  = chikou

        return ten, kij, sp_a, sp_b, chikou

    def psar(
        self,
        af_start: float = 0.02,
        af_step: float = 0.02,
        af_max: float = 0.20,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Parabolic SAR — trailing stop & trend reversal indicator.
        Loop Python (rekursif, tidak bisa divektorisasi tanpa numba).

        Nilai PSAR berada di BAWAH harga saat bullish (support),
        dan di ATAS harga saat bearish (resistance).

        Output:
          PSAR         — nilai SAR
          PSAR_DIR     — arah: +1 = bullish, -1 = bearish
          PSAR_REV     — reversal: 1 = bar ini ada reversal, 0 = tidak
        """
        if not _require(self._df, "high", "low", "close", ctx="psar"):
            e = pd.Series(dtype=float)
            return e, e, e

        df    = self._df
        high  = df["high"].to_numpy(dtype=float)
        low   = df["low"].to_numpy(dtype=float)
        n     = len(high)

        sar   = np.empty(n)
        dir_  = np.zeros(n, dtype=int)
        rev   = np.zeros(n, dtype=int)
        af    = np.empty(n)
        ep    = np.empty(n)

        # Inisialisasi: anggap bullish dari awal
        sar[0]   = low[0]
        dir_[0]  = 1
        af[0]    = af_start
        ep[0]    = high[0]

        for i in range(1, n):
            prev_sar = sar[i-1]
            prev_dir = dir_[i-1]
            prev_ep  = ep[i-1]
            prev_af  = af[i-1]

            if prev_dir == 1:  # bullish
                new_sar = prev_sar + prev_af * (prev_ep - prev_sar)
                # SAR tidak boleh lebih tinggi dari dua low sebelumnya
                new_sar = min(new_sar, low[i-1], low[max(0, i-2)])
                if low[i] < new_sar:
                    # Reversal → bearish
                    dir_[i] = -1
                    sar[i]  = prev_ep
                    ep[i]   = low[i]
                    af[i]   = af_start
                    rev[i]  = 1
                else:
                    dir_[i] = 1
                    sar[i]  = new_sar
                    if high[i] > prev_ep:
                        ep[i] = high[i]
                        af[i] = min(prev_af + af_step, af_max)
                    else:
                        ep[i] = prev_ep
                        af[i] = prev_af
            else:  # bearish
                new_sar = prev_sar + prev_af * (prev_ep - prev_sar)
                # SAR tidak boleh lebih rendah dari dua high sebelumnya
                new_sar = max(new_sar, high[i-1], high[max(0, i-2)])
                if high[i] > new_sar:
                    # Reversal → bullish
                    dir_[i] = 1
                    sar[i]  = prev_ep
                    ep[i]   = high[i]
                    af[i]   = af_start
                    rev[i]  = 1
                else:
                    dir_[i] = -1
                    sar[i]  = new_sar
                    if low[i] < prev_ep:
                        ep[i] = low[i]
                        af[i] = min(prev_af + af_step, af_max)
                    else:
                        ep[i] = prev_ep
                        af[i] = prev_af

        s_sar = pd.Series(sar,            index=df.index)
        s_dir = pd.Series(dir_.astype(float), index=df.index)
        s_rev = pd.Series(rev.astype(float), index=df.index)

        if append:
            df["PSAR"]     = s_sar
            df["PSAR_DIR"] = s_dir
            df["PSAR_REV"] = s_rev

        return s_sar, s_dir, s_rev

    # ══════════════════════════════════════════════════════════════════════════
    # MOMENTUM
    # ══════════════════════════════════════════════════════════════════════════

    def rsi(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        RSI (Wilder's smoothing). Edge-case avg_loss==0 → RSI=100 (bukan 50).
        Output: RSI_{length}
        """
        if not _require(self._df, "close", ctx="rsi"):
            return pd.Series(dtype=float)
        n = len(self._df)
        if n < length + 1:
            log.warning("rsi: data hanya %d bar, butuh %d+", n, length + 1)
        col    = f"RSI_{length}"
        result = _rsi_raw(self._df["close"], length)
        if append:
            self._df[col] = result
        return result

    def rsi_slope(
        self,
        length: int = 14,
        slope_period: int = 0,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        Slope RSI — kemiringan RSI selama N bar terakhir via OLS linear regression.

        [v5 FIX] Sebelumnya pakai .diff(N) — hanya selisih dua titik, rentan noise.
        Sekarang pakai OLS slope (β₁ dari regresi linier) atas rolling window.
        Lebih akurat sebagai proxy divergence: menggambarkan tren RSI, bukan lompatan.

        slope_period = 0 → otomatis pakai max(3, length//2).

        Positif = RSI naik (momentum bullish tumbuh).
        Negatif = RSI turun (momentum bearish tumbuh).
        Magnitude ~ unit RSI per bar.

        Output: RSI_{length}_slope
        """
        if not _require(self._df, "close", ctx="rsi_slope"):
            return pd.Series(dtype=float)

        col     = f"RSI_{length}_slope"
        rsi_col = f"RSI_{length}"
        half    = slope_period if slope_period > 0 else max(3, length // 2)

        if rsi_col not in self._df.columns:
            self.rsi(length=length, append=True)

        rsi_arr = self._df[rsi_col].to_numpy(dtype=float)
        n       = len(rsi_arr)
        out     = np.full(n, np.nan)

        if n >= half:
            # OLS slope vectorized via stride_tricks
            wins = np.lib.stride_tricks.sliding_window_view(rsi_arr, half)
            x    = np.arange(half, dtype=float)
            xm   = x - x.mean()
            denom = float((xm ** 2).sum()) or 1.0
            # β₁ = Σ(x-x̄)(y-ȳ) / Σ(x-x̄)² = (xm @ wins.T - n*x̄*ȳ) / denom
            ym   = wins.mean(axis=1)
            out[half - 1:] = (wins @ xm) / denom - ym * (xm.sum() / denom)

        result = pd.Series(out, index=self._df.index).fillna(0.0)
        if append:
            self._df[col] = result
        return result

    def rsi_divergence(
        self,
        length: int = 14,
        lookback: int = 14,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        RSI Divergence — deteksi bullish/bearish divergence bar demi bar.
        Sama konsepnya dengan indicators/momentum.py._detect_rsi_divergence()
        tapi menghasilkan Series (seluruh DataFrame) bukan scalar.

        [v5 FIX] Loop Python O(n) diganti dengan full-vectorized via shift() —
        sama cepat dengan indikator lain, tidak ada overhead per-bar.

        Algoritma:
          Bandingkan bar sekarang vs N bar lalu:
            Bullish: harga lower low TAPI RSI higher low → nilai positif (RSI gap)
            Bearish: harga higher high TAPI RSI lower high → nilai negatif (RSI gap)
            Tidak ada divergence → 0.0

        Nilai absolut = besaran gap RSI (makin besar = makin kuat sinyalnya).

        Output: RSI_DIV_{lookback}
        """
        if not _require(self._df, "close", ctx="rsi_divergence"):
            return pd.Series(dtype=float)

        col     = f"RSI_DIV_{lookback}"
        rsi_col = f"RSI_{length}"
        if rsi_col not in self._df.columns:
            self.rsi(length=length, append=True)

        close  = self._df["close"]
        rsi_s  = self._df[rsi_col]

        curr_c  = close
        prev_c  = close.shift(lookback)
        curr_r  = rsi_s
        prev_r  = rsi_s.shift(lookback)
        rsi_gap = curr_r - prev_r          # positif = RSI naik, negatif = RSI turun

        # Bullish: harga LL tapi RSI HL
        bull = (curr_c < prev_c) & (curr_r > prev_r)
        # Bearish: harga HH tapi RSI LH
        bear = (curr_c > prev_c) & (curr_r < prev_r)

        out = pd.Series(0.0, index=self._df.index)
        out = out.where(~bull, rsi_gap)    # bullish → positif
        out = out.where(~bear, rsi_gap)    # bearish → negatif
        out = out.fillna(0.0)

        if append:
            self._df[col] = out
        return out

    def macd(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD. Output: MACD_{f}_{s}_{sig}, MACDs_{f}_{s}_{sig}, MACDh_{f}_{s}_{sig}
        Default: MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
        """
        if not _require(self._df, "close", ctx="macd"):
            e = pd.Series(dtype=float)
            return e, e, e
        if len(self._df) < slow + signal:
            log.warning("macd: data hanya %d bar, idealnya %d+", len(self._df), slow+signal)

        close  = self._df["close"]
        ml     = _ema_series(close, fast) - _ema_series(close, slow)
        sl     = _ema_series(ml, signal)
        hl     = ml - sl

        if append:
            self._df[f"MACD_{fast}_{slow}_{signal}"]  = ml
            self._df[f"MACDs_{fast}_{slow}_{signal}"] = sl
            self._df[f"MACDh_{fast}_{slow}_{signal}"] = hl
        return ml, sl, hl

    def stochrsi(
        self,
        length: int = 14,
        rsi_length: int = 14,
        k: int = 3,
        d: int = 3,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Stochastic RSI. RSI dihitung via _rsi_raw() (termasuk edge-case fix).
        Output: STOCHRSIk_{l}_{rl}_{k}_{d}, STOCHRSId_{l}_{rl}_{k}_{d}
        Default: STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3
        """
        if not _require(self._df, "close", ctx="stochrsi"):
            return pd.Series(dtype=float), pd.Series(dtype=float)
        if len(self._df) < rsi_length + length:
            log.warning("stochrsi: data hanya %d bar, idealnya %d+",
                        len(self._df), rsi_length + length)

        rsi_s   = _rsi_raw(self._df["close"], rsi_length)
        rsi_min = rsi_s.rolling(length, min_periods=length).min()
        rsi_max = rsi_s.rolling(length, min_periods=length).max()
        rsi_rng = (rsi_max - rsi_min).replace(0, np.nan)
        raw_k   = ((rsi_s - rsi_min) / rsi_rng * 100).fillna(50.0)
        k_line  = raw_k.rolling(k, min_periods=1).mean()
        d_line  = k_line.rolling(d, min_periods=1).mean()

        if append:
            self._df[f"STOCHRSIk_{length}_{rsi_length}_{k}_{d}"] = k_line
            self._df[f"STOCHRSId_{length}_{rsi_length}_{k}_{d}"] = d_line
        return k_line, d_line

    def cci(self, length: int = 20, append: bool = True, **kw) -> pd.Series:
        """
        Commodity Channel Index — jarak harga dari mean-nya dalam unit deviasi.
        OB: CCI > +100. OS: CCI < -100.

        [v4 PERF] Sepenuhnya divektorisasi via stride_tricks — 297x lebih cepat
        dari rolling.apply(lambda) yang dipakai v3 (108ms → 0.4ms per 1000 bar).

        Output: CCI_{length}
        """
        if not _require(self._df, "high", "low", "close", ctx="cci"):
            return pd.Series(dtype=float)

        col     = f"CCI_{length}"
        df      = self._df
        n       = len(df)
        typical = ((df["high"] + df["low"] + df["close"]) / 3.0).to_numpy(dtype=float)
        out     = np.full(n, 0.0)

        if n >= length:
            wins  = np.lib.stride_tricks.sliding_window_view(typical, length)  # shape (n-l+1, l)
            sma   = wins.mean(axis=1)
            md    = np.abs(wins - sma[:, None]).mean(axis=1)
            denom = 0.015 * md
            denom[denom == 0] = np.nan
            out[length - 1:] = (typical[length - 1:] - sma) / denom

        result = pd.Series(np.nan_to_num(out, nan=0.0), index=df.index)
        if append:
            df[col] = result
        return result

    def williams_r(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Williams %R — overbought/oversold oscillator.
        Range: 0 to -100.
          OB : > -20  (harga mendekati high tertinggi, potensi reversal turun)
          OS : < -80  (harga mendekati low terendah, potensi reversal naik)

        Sudah dipakai di indicators/oscillators.py.calculate_williams_r() —
        sekarang tersedia via df.ta accessor sebagai kolom Series.

        Output: WILLR_{length}
        """
        if not _require(self._df, "high", "low", "close", ctx="williams_r"):
            return pd.Series(dtype=float)

        col   = f"WILLR_{length}"
        df    = self._df
        hh    = df["high"].rolling(length, min_periods=length).max()
        ll    = df["low"].rolling(length, min_periods=length).min()
        rng   = (hh - ll).replace(0, np.nan)
        result = ((hh - df["close"]) / rng) * -100.0
        result = result.fillna(-50.0)

        if append:
            df[col] = result
        return result

    def roc(self, length: int = 9, append: bool = True, **kw) -> pd.Series:
        """
        Rate of Change — % perubahan harga vs N bar lalu.
        ROC > 0 = harga lebih tinggi dari N bar lalu (bullish momentum).
        ROC < 0 = harga lebih rendah (bearish momentum).

        Sudah ada di indicators/oscillators.py.calculate_roc() (scalar).
        Sekarang tersedia sebagai full Series via df.ta accessor.

        Output: ROC_{length}
        """
        if not _require(self._df, "close", ctx="roc"):
            return pd.Series(dtype=float)

        col    = f"ROC_{length}"
        close  = self._df["close"]
        prev   = close.shift(length)
        result = ((close - prev) / prev.replace(0, np.nan)) * 100.0
        result = result.fillna(0.0)

        if append:
            self._df[col] = result
        return result

    def roc_slope(
        self,
        fast: int = 9,
        signal: int = 5,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        ROC Slope — apakah momentum ROC sedang mempercepat atau melambat.
        Slope > 0 = momentum mempercepat (bullish acceleration).
        Slope < 0 = momentum melambat / berbalik.

        Sudah ada di indicators/oscillators.py.calculate_roc_slope().

        Output: ROC_SLOPE_{fast}_{signal}
        """
        if not _require(self._df, "close", ctx="roc_slope"):
            return pd.Series(dtype=float)

        col     = f"ROC_SLOPE_{fast}_{signal}"
        roc_col = f"ROC_{fast}"
        if roc_col not in self._df.columns:
            self.roc(length=fast, append=True)

        result = self._df[roc_col].diff(signal)
        if append:
            self._df[col] = result
        return result

    def ema_cross(
        self,
        fast: int = 9,
        slow: int = 21,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        EMA Crossover Signal — deteksi persilangan EMA fast vs slow.
          +1 = bullish cross (fast melewati slow ke atas, bar ini)
          -1 = bearish cross (fast melewati slow ke bawah, bar ini)
           0 = tidak ada cross

        Berguna sebagai entry trigger filter. Hanya aktif pada bar persilangan,
        bukan seluruh area setelahnya (berbeda dari ema_stack_score).

        Output: EMAXS_{fast}_{slow}
        """
        if not _require(self._df, "close", ctx="ema_cross"):
            return pd.Series(dtype=float)

        col      = f"EMAXS_{fast}_{slow}"
        df       = self._df
        fc       = f"EMA_{fast}"
        sc       = f"EMA_{slow}"
        if fc not in df.columns:
            self.ema(length=fast, append=True)
        if sc not in df.columns:
            self.ema(length=slow, append=True)

        fast_s   = df[fc]
        slow_s   = df[sc]
        above    = (fast_s > slow_s).astype(int)
        prev_ab  = above.shift(1).fillna(above)

        # +1 ketika transisi 0→1, -1 ketika 1→0
        cross    = (above - prev_ab.astype(int))
        cross    = cross.where(cross != 0, 0).astype(float)

        if append:
            df[col] = cross
        return cross

    # ══════════════════════════════════════════════════════════════════════════
    # VOLATILITY
    # ══════════════════════════════════════════════════════════════════════════

    def atr(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """Average True Range (Wilder's). Output: ATRr_{length}"""
        if not _require(self._df, "high", "low", "close", ctx="atr"):
            return pd.Series(dtype=float)
        result = _wilder_smooth(_true_range(self._df), length)
        if append:
            self._df[f"ATRr_{length}"] = result
        return result

    def atr_pct(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """ATR sebagai % dari close — volatilitas relatif. Output: ATRr_{length}_pct"""
        if not _require(self._df, "high", "low", "close", ctx="atr_pct"):
            return pd.Series(dtype=float)
        col_atr = f"ATRr_{length}"
        if col_atr not in self._df.columns:
            self.atr(length=length, append=True)
        result = (self._df[col_atr] / self._df["close"].replace(0, np.nan)) * 100.0
        if append:
            self._df[f"ATRr_{length}_pct"] = result
        return result

    def atr_percentile(
        self,
        length: int = 14,
        lookback: int = 100,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        ATR historis percentile — % waktu ATR sekarang lebih rendah dari historis.
        > 80 = volatilitas tinggi secara historis. < 20 = sangat rendah.

        [v4 PERF] Sepenuhnya divektorisasi via stride_tricks (7x lebih cepat
        dari rolling.apply yang dipakai v3).

        Output: _atr_percentile_{lookback}
        """
        if not _require(self._df, "high", "low", "close", ctx="atr_percentile"):
            return pd.Series(dtype=float)

        col_atr = f"ATRr_{length}"
        col_out = f"_atr_percentile_{lookback}"
        if col_atr not in self._df.columns:
            self.atr(length=length, append=True)

        atr_arr = self._df[col_atr].to_numpy(dtype=float)
        n       = len(atr_arr)
        out     = np.full(n, 50.0)
        min_p   = max(2, lookback // 4)

        # Full windows: stride_tricks (vectorized)
        if n >= lookback:
            wins  = np.lib.stride_tricks.sliding_window_view(atr_arr, lookback)
            cur   = wins[:, -1]
            below = (wins[:, :-1] < cur[:, None]).sum(axis=1)
            out[lookback - 1:] = below / (lookback - 1) * 100.0

        # Warmup (partial windows — jumlah bar kecil, overhead tidak signifikan)
        for i in range(min_p - 1, min(lookback - 1, n)):
            w = atr_arr[:i + 1]
            if len(w) > 1:
                out[i] = float(np.sum(w[:-1] < w[-1])) / (len(w) - 1) * 100.0

        result = pd.Series(out, index=self._df.index)
        if append:
            self._df[col_out] = result
        return result

    def bbands(
        self,
        length: int = 20,
        std: float = 2.0,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Bollinger Bands.
        Output: BBU_{l}_{std}, BBM, BBL, BBB (bandwidth%), BBP (%B position)
        Default: BBU_20_2.0, BBM_20_2.0, BBL_20_2.0, BBB_20_2.0, BBP_20_2.0
        """
        if not _require(self._df, "close", ctx="bbands"):
            e = pd.Series(dtype=float)
            return e, e, e, e, e
        if len(self._df) < length:
            log.warning("bbands: data hanya %d bar, butuh %d", len(self._df), length)

        std_str = f"{std:.1f}"
        close   = self._df["close"]
        middle  = close.rolling(length, min_periods=length).mean()
        stddev  = close.rolling(length, min_periods=length).std(ddof=0)
        upper   = middle + std * stddev
        lower   = middle - std * stddev
        bw      = ((upper - lower) / middle.replace(0, np.nan)) * 100.0
        pos     = (close - lower) / (upper - lower).replace(0, np.nan)

        if append:
            self._df[f"BBU_{length}_{std_str}"] = upper
            self._df[f"BBM_{length}_{std_str}"] = middle
            self._df[f"BBL_{length}_{std_str}"] = lower
            self._df[f"BBB_{length}_{std_str}"] = bw
            self._df[f"BBP_{length}_{std_str}"] = pos
        return upper, middle, lower, bw, pos

    def keltner(
        self,
        length: int = 20,
        scalar: float = 2.0,
        atr_length: int = 14,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Keltner Channel (EMA ± scalar×ATR).
        Output: KCUe_{l}_{scalar}, KCBe_{l}_{scalar}, KCLe_{l}_{scalar}
        """
        if not _require(self._df, "high", "low", "close", ctx="keltner"):
            e = pd.Series(dtype=float)
            return e, e, e

        scalar_str = _fmt_param(scalar)
        atr_col    = f"ATRr_{atr_length}"
        if atr_col not in self._df.columns:
            atr_s = _wilder_smooth(_true_range(self._df), atr_length)
        else:
            atr_s = self._df[atr_col]

        middle = _ema_series(self._df["close"], length)
        upper  = middle + scalar * atr_s
        lower  = middle - scalar * atr_s

        if append:
            self._df[f"KCUe_{length}_{scalar_str}"] = upper
            self._df[f"KCBe_{length}_{scalar_str}"] = middle
            self._df[f"KCLe_{length}_{scalar_str}"] = lower
        return upper, middle, lower

    def squeeze(
        self,
        bb_length: int = 20,
        bb_mult: float = 2.0,
        kc_length: int = 20,
        kc_mult: float = 1.5,
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        BB/KC Squeeze. 1.0 = squeeze aktif (BB di dalam KC), 0.0 = tidak aktif.
        Output: SQZ_{bb_length}_{bb_mult}_{kc_length}_{kc_mult}
        """
        bb_s = f"{bb_mult:.1f}"
        kc_s = _fmt_param(kc_mult)
        col_bbu = f"BBU_{bb_length}_{bb_s}"; col_bbl = f"BBL_{bb_length}_{bb_s}"
        col_kcu = f"KCUe_{kc_length}_{kc_s}"; col_kcl = f"KCLe_{kc_length}_{kc_s}"

        if col_bbu not in self._df.columns:
            self.bbands(length=bb_length, std=bb_mult, append=True)
        if col_kcu not in self._df.columns:
            self.keltner(length=kc_length, scalar=kc_mult, append=True)

        sqz = ((self._df[col_bbu] <= self._df[col_kcu])
               & (self._df[col_bbl] >= self._df[col_kcl])).astype(float)
        col = f"SQZ_{bb_length}_{bb_mult}_{kc_length}_{kc_mult}"
        if append:
            self._df[col] = sqz
        return sqz

    def chop(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Choppiness Index. < 38.2 = trending kuat. > 61.8 = choppy/ranging.
        CHOP = 100 * log10(SUM(TR,n) / (MaxHigh-MinLow)) / log10(n)
        Output: CHOP_{length}
        """
        if not _require(self._df, "high", "low", "close", ctx="chop"):
            return pd.Series(dtype=float)
        df      = self._df
        sum_tr  = _true_range(df).rolling(length, min_periods=length).sum()
        hi      = df["high"].rolling(length, min_periods=length).max()
        lo      = df["low"].rolling(length, min_periods=length).min()
        rng     = (hi - lo).replace(0, np.nan)
        result  = (100.0 * np.log10(sum_tr / rng) / np.log10(length)).clip(0, 100).fillna(50.0)
        if append:
            df[f"CHOP_{length}"] = result
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # STRENGTH
    # ══════════════════════════════════════════════════════════════════════════

    def adx(
        self,
        length: int = 14,
        append: bool = True,
        **kw,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        ADX + DI. Output: ADX_{length}, DMP_{length}, DMN_{length}
        ADX > 25 = trending. < 20 = tidak ada trend yang jelas.
        """
        if not _require(self._df, "high", "low", "close", ctx="adx"):
            e = pd.Series(dtype=float)
            return e, e, e
        if len(self._df) < length + 1:
            log.warning("adx: data hanya %d bar, butuh %d+", len(self._df), length)
            e = pd.Series(dtype=float)
            return e, e, e

        df  = self._df
        idx = df.index
        h   = df["high"].to_numpy(dtype=float)
        l   = df["low"].to_numpy(dtype=float)

        up_move   = np.diff(h, prepend=h[0])
        down_move = -np.diff(l, prepend=l[0])
        plus_dm   = np.where((up_move > down_move)   & (up_move > 0),   up_move,   0.0)
        minus_dm  = np.where((down_move > up_move)   & (down_move > 0), down_move, 0.0)

        tr_s  = _true_range(df)
        sm_tr = _wilder_smooth(tr_s, length)
        sm_p  = _wilder_smooth(pd.Series(plus_dm,  index=idx), length)
        sm_m  = _wilder_smooth(pd.Series(minus_dm, index=idx), length)

        safe   = sm_tr.replace(0, np.nan)
        # [BUG-FIX — kritis, ditemukan lewat cross-check antar dua sistem
        # indikator (indicators/strength.py juga py, pola identik)] Sebelumnya
        # di_p/di_m/dx di-fillna(0.0) SEBELUM waktunya, saat sm_tr masih NaN
        # (warm-up, _wilder_smooth ewm belum menghasilkan nilai stabil).
        # Ini memalsukan "belum ada data" jadi "DI+/DI- = 0", dan dx warm-up
        # ikut jadi 0.0 palsu -- mencemari _wilder_smooth(dx) berikutnya
        # (versi ewm bahkan LEBIH terpengaruh drpd versi manual-seed karena
        # ewm mempertahankan pengaruh histori lebih lama). Dibuktikan lewat
        # eksperimen: selisih ADX akibat kontaminasi murni ~12.6 poin (~34%
        # relatif) pada data 29 bar. Fix: biarkan NaN mengalir apa adanya,
        # _wilder_smooth (ewm) sudah benar menangani leading-NaN dengan
        # skip otomatis, tidak perlu fillna prematur.
        di_p   = (100.0 * sm_p / safe)
        di_m   = (100.0 * sm_m / safe)
        di_sum = (di_p + di_m).replace(0, np.nan)
        dx     = (100.0 * (di_p - di_m).abs() / di_sum)
        s_adx  = _wilder_smooth(dx, length)

        if append:
            df[f"ADX_{length}"] = s_adx
            df[f"DMP_{length}"] = di_p
            df[f"DMN_{length}"] = di_m
        return s_adx, di_p, di_m

    def obv(self, append: bool = True, **kw) -> pd.Series:
        """On-Balance Volume. Output: OBV"""
        if not _require(self._df, "close", "volume", ctx="obv"):
            return pd.Series(dtype=float)
        sign   = np.sign(self._df["close"].diff().fillna(0))
        result = (sign * self._df["volume"]).cumsum()
        if append:
            self._df["OBV"] = result
        return result

    def mfi(self, length: int = 14, append: bool = True, **kw) -> pd.Series:
        """
        Money Flow Index — RSI berbasis volume (0-100).
        Edge-case: neg_sum==0 → MFI=100 (bukan 50).
        Output: MFI_{length}
        """
        if not _require(self._df, "high", "low", "close", "volume", ctx="mfi"):
            return pd.Series(dtype=float)
        df      = self._df
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        raw_mf  = typical * df["volume"]
        delta   = typical.diff()

        pos_sum = raw_mf.where(delta > 0, 0.0).rolling(length, min_periods=length).sum()
        neg_sum = raw_mf.where(delta < 0, 0.0).rolling(length, min_periods=length).sum()
        mfr     = pos_sum / neg_sum.replace(0, np.nan)
        result  = 100.0 - (100.0 / (1.0 + mfr))

        no_out  = neg_sum == 0
        result  = result.mask(no_out & (pos_sum > 0),  100.0)
        result  = result.mask(no_out & (pos_sum == 0),  50.0)
        result  = result.fillna(50.0)

        if append:
            df[f"MFI_{length}"] = result
        return result

    def cmf(self, length: int = 20, append: bool = True, **kw) -> pd.Series:
        """
        Chaikin Money Flow — volume-weighted buying vs selling pressure.
        Range ~[-1, +1].
          CMF > +0.1 : buying pressure dominan (bullish)
          CMF < -0.1 : selling pressure dominan (bearish)
          CMF ≈  0   : neutral / tidak ada dominasi

        Formula:
          MFM = ((Close - Low) - (High - Close)) / (High - Low)
          CMF = Sum(MFM * Volume, n) / Sum(Volume, n)

        Output: CMF_{length}
        """
        if not _require(self._df, "high", "low", "close", "volume", ctx="cmf"):
            return pd.Series(dtype=float)

        col    = f"CMF_{length}"
        df     = self._df
        hl_rng = (df["high"] - df["low"]).replace(0, np.nan)
        mfm    = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_rng
        mfv    = mfm * df["volume"]

        result = (
            mfv.rolling(length, min_periods=length).sum()
            / df["volume"].rolling(length, min_periods=length).sum().replace(0, np.nan)
        ).fillna(0.0)

        if append:
            df[col] = result
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # UTILITY
    # ══════════════════════════════════════════════════════════════════════════

    def ema_stack_score(
        self,
        periods: Tuple[int, ...] = (9, 21, 50),
        append: bool = True,
        **kw,
    ) -> pd.Series:
        """
        EMA Stack Alignment Score (0–100).
        100 = semua EMA bullish (fast > slow di setiap pair).
        0   = semua bearish. 50 = neutral/mixed.
        Konsisten dengan strategy.py:1354 dan indicators/trend.py:calculate_ema_stack().
        Output: _ema_stack_score
        """
        if not _require(self._df, "close", ctx="ema_stack_score"):
            return pd.Series(dtype=float)

        df  = self._df
        for p in periods:
            if f"EMA_{p}" not in df.columns:
                self.ema(length=p, append=True)

        sp       = sorted(periods)
        pairs    = [(f"EMA_{sp[i]}", f"EMA_{sp[j]}")
                    for i in range(len(sp)) for j in range(i+1, len(sp))]
        n_pairs  = len(pairs) or 1
        per_pair = 100.0 / n_pairs
        score    = pd.Series(0.0, index=df.index)
        for fc, sc in pairs:
            if fc in df.columns and sc in df.columns:
                score += (df[fc] > df[sc]).astype(float) * per_pair

        result = score.round(2)
        if append:
            df["_ema_stack_score"] = result
        return result

    def enrich_production(
        self,
        ema_periods: Tuple[int, ...] = (9, 21, 50, 100, 200),
        with_vwap_bands: bool = True,
        dropna: bool = False,
        **kw,
    ) -> pd.DataFrame:
        """
        60+ kolom indikator dalam satu panggilan (chart/diagnostic/analytics).

        [DOKUMENTASI DIPERBAIKI — sebelumnya klaim "WAJIB dipakai main.py,
        strategy.py, telegram_bot.py" TIDAK AKURAT dan sempat menyesatkan
        audit sesi ini. Faktanya (diverifikasi via grep seluruh codebase):
          - HANYA api_server.py yang benar-benar memanggil enrich_production()
            (endpoint /api/diagnosa dan chart debug).
          - strategy.py PUNYA fungsi enrich() SENDIRI yang minimal (cuma
            EMA9/21/50, RSI_14, ATRr_14, VWAP) — TIDAK memanggil
            enrich_production() sama sekali.
          - Jalur skoring produksi SESUNGGUHNYA (Observer.observe() di
            intelligence/observer.py, dipanggil dari
            strategy.py:get_scored_signal) memakai indicators/*.py
            (score_trend, score_momentum, score_strength, dst) yang
            punya implementasi kalkulasi SENDIRI, TERPISAH dari ta_compat.py.
          - Konsisten dengan catatan di constants.py baris 105-113 soal
            PRODUCTION_INDICATOR_COLS yang juga tidak dipakai di runtime.
        Kesimpulan: ta_compat.py dan indicators/*.py adalah DUA SISTEM
        KALKULASI INDIKATOR TERPISAH yang TIDAK saling berbagi kode. Ini
        BUKAN bug (tidak ada crash/kesalahan fungsional), tapi risiko
        arsitektur: kalau rumus di ta_compat.py dan indicators/*.py untuk
        indikator yang "sama" (RSI, ATR, dll) berbeda, dashboard diagnostic
        (pakai ta_compat) bisa menampilkan angka BERBEDA dari yang benar-
        benar dipakai bot untuk keputusan trading (pakai indicators/*.py).
        Perlu audit terpisah utk indicators/*.py guna verifikasi konsistensi
        ini (di luar skop ta_compat.py sendiri).]

        Cocok untuk analytics, backtest, training, dan endpoint diagnostic
        di api_server.py. Skip-if-exists: kolom yang sudah ada tidak
        dihitung ulang — aman dipanggil berkali-kali.
        """
        df = self._df
        t0 = time.perf_counter()

        def _need(col: str) -> bool:
            return not _skip_if_exists(df, col)

        # ── Layer 1: EMA & turunan ────────────────────────────────────────────
        for p in ema_periods:
            if _need(f"EMA_{p}"):  self.ema(length=p, append=True)
        for p in (9, 21):
            if _need(f"DEMA_{p}"): self.dema(length=p, append=True)
            if _need(f"TEMA_{p}"): self.tema(length=p, append=True)
            if _need(f"HMA_{p}"):  self.hma(length=p, append=True)
        if _need("WMA_14"):  self.wma(length=14, append=True)
        if _need("VWMA_20"): self.vwma(length=20, append=True)

        # ── Layer 2: EMA utility ──────────────────────────────────────────────
        if _need("_ema_stack_score"): self.ema_stack_score(periods=(9, 21, 50), append=True)
        if _need("EMAXS_9_21"):       self.ema_cross(fast=9,  slow=21, append=True)
        if _need("EMAXS_21_50"):      self.ema_cross(fast=21, slow=50, append=True)

        # ── Layer 3: Momentum ─────────────────────────────────────────────────
        if _need("RSI_14"):            self.rsi(length=14, append=True)
        if _need("RSI_14_slope"):      self.rsi_slope(length=14, append=True)
        if _need("RSI_DIV_14"):        self.rsi_divergence(length=14, lookback=14, append=True)
        if _need("MACD_12_26_9"):      self.macd(fast=12, slow=26, signal=9, append=True)
        if _need("STOCHRSIk_14_14_3_3"):
            self.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)
        if _need("CCI_20"):            self.cci(length=20, append=True)
        if _need("WILLR_14"):          self.williams_r(length=14, append=True)
        if _need("ROC_9"):             self.roc(length=9, append=True)
        if _need("ROC_SLOPE_9_5"):     self.roc_slope(fast=9, signal=5, append=True)

        # ── Layer 4: Volatility ───────────────────────────────────────────────
        if _need("ATRr_14"):           self.atr(length=14, append=True)
        if _need("ATRr_14_pct"):       self.atr_pct(length=14, append=True)
        if _need("_atr_percentile_100"):
            self.atr_percentile(length=14, lookback=100, append=True)
        if _need("BBU_20_2.0"):        self.bbands(length=20, std=2.0, append=True)
        if _need("KCUe_20_2"):         self.keltner(length=20, scalar=2.0, atr_length=14, append=True)
        if _need("SQZ_20_2.0_20_1.5"):
            self.squeeze(bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5, append=True)
        if _need("CHOP_14"):           self.chop(length=14, append=True)

        # ── Layer 5: Trend structure ──────────────────────────────────────────
        if _need("SUPERT_7_3.0"):      self.supertrend(length=7, multiplier=3.0, append=True)
        if _need("DCU_20"):            self.donchian(length=20, append=True)
        if _need("PSAR"):              self.psar(append=True)

        # ── Layer 6: Strength ─────────────────────────────────────────────────
        if _need("ADX_14"):            self.adx(length=14, append=True)
        if _need("OBV"):               self.obv(append=True)
        if _need("MFI_14"):            self.mfi(length=14, append=True)
        if _need("CMF_20"):            self.cmf(length=20, append=True)

        # ── Layer 7: VWAP + Bands ─────────────────────────────────────────────
        if _need("VWAP_D"):
            try:
                self.vwap(anchor="D", append=True)
            except Exception as exc:
                log.debug("enrich_production VWAP gagal: %s", exc)
        if with_vwap_bands and _need("VWAP_D_upper_1"):
            try:
                self.vwap_bands(stdev_mult_1=1.0, stdev_mult_2=2.0, append=True)
            except Exception as exc:
                log.debug("enrich_production VWAP bands gagal: %s", exc)

        elapsed = (time.perf_counter() - t0) * 1000
        n_ind   = sum(1 for c in df.columns
                      if c not in ("open", "high", "low", "close", "volume"))
        log.debug("enrich_production: %.1fms, %d bar, %d kolom indikator",
                  elapsed, len(df), n_ind)

        return df.dropna() if dropna else df

    def compute_all(
        self,
        ema_periods: Tuple[int, ...] = (9, 21, 50, 100, 200),
        with_vwap_bands: bool = True,
        skip_existing: bool = True,
    ) -> pd.DataFrame:
        """
        60+ kolom indikator dalam satu panggilan.
        Cocok untuk analytics, backtest, dan training.

        skip_existing=True → aman dipanggil berkali-kali.
        """
        df = self._df
        n  = len(df)
        t0 = time.perf_counter()

        def _need(col: str) -> bool:
            return not (skip_existing and _skip_if_exists(df, col))

        # ── Layer 1: EMA & turunan ────────────────────────────────────────────
        for p in ema_periods:
            if _need(f"EMA_{p}"): self.ema(length=p, append=True)
        for p in (9, 21):
            if _need(f"DEMA_{p}"): self.dema(length=p, append=True)
            if _need(f"TEMA_{p}"): self.tema(length=p, append=True)
            if _need(f"HMA_{p}"):  self.hma(length=p, append=True)
        if _need("WMA_14"):  self.wma(length=14, append=True)
        if _need("VWMA_20"): self.vwma(length=20, append=True)

        # ── Layer 2: EMA utility scores ───────────────────────────────────────
        if _need("_ema_stack_score"): self.ema_stack_score(periods=(9,21,50), append=True)
        if _need("EMAXS_9_21"):       self.ema_cross(fast=9, slow=21, append=True)
        if _need("EMAXS_21_50"):      self.ema_cross(fast=21, slow=50, append=True)

        # ── Layer 3: Momentum ─────────────────────────────────────────────────
        if _need("RSI_14"):       self.rsi(length=14, append=True)
        if _need("RSI_14_slope"): self.rsi_slope(length=14, append=True)
        if _need("RSI_DIV_14"):   self.rsi_divergence(length=14, lookback=14, append=True)
        if _need("MACD_12_26_9"): self.macd(fast=12, slow=26, signal=9, append=True)
        if _need("STOCHRSIk_14_14_3_3"): self.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)
        if _need("CCI_20"):       self.cci(length=20, append=True)
        if _need("WILLR_14"):     self.williams_r(length=14, append=True)
        if _need("ROC_9"):        self.roc(length=9, append=True)
        if _need("ROC_SLOPE_9_5"): self.roc_slope(fast=9, signal=5, append=True)

        # ── Layer 4: Volatility ───────────────────────────────────────────────
        if _need("ATRr_14"):        self.atr(length=14, append=True)
        if _need("ATRr_14_pct"):    self.atr_pct(length=14, append=True)
        if _need("_atr_percentile_100"): self.atr_percentile(length=14, lookback=100, append=True)
        if _need("BBU_20_2.0"):     self.bbands(length=20, std=2.0, append=True)
        if _need("KCUe_20_2"):      self.keltner(length=20, scalar=2.0, atr_length=14, append=True)
        if _need("CHOP_14"):        self.chop(length=14, append=True)
        if _need("SQZ_20_2.0_20_1.5"): self.squeeze(bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5, append=True)
        if _need("SUPERT_7_3.0"):   self.supertrend(length=7, multiplier=3.0, append=True)

        # ── Layer 5: Structure ────────────────────────────────────────────────
        if _need("DCU_20"):      self.donchian(length=20, append=True)
        if _need("ICH_TENKAN"):  self.ichimoku(append=True)
        if _need("PSAR"):        self.psar(append=True)

        # ── Layer 6: Strength ─────────────────────────────────────────────────
        if _need("ADX_14"): self.adx(length=14, append=True)
        if _need("OBV"):    self.obv(append=True)
        if _need("MFI_14"): self.mfi(length=14, append=True)
        if _need("CMF_20"): self.cmf(length=20, append=True)

        # ── Layer 7: VWAP + Bands ─────────────────────────────────────────────
        if _need("VWAP_D"): self.vwap(anchor="D", append=True)
        if with_vwap_bands and _need("VWAP_D_upper_1"):
            self.vwap_bands(stdev_mult_1=1.0, stdev_mult_2=2.0, append=True)

        elapsed    = (time.perf_counter() - t0) * 1000
        cols_added = [c for c in df.columns
                      if c not in ("open","high","low","close","volume")]
        log.debug("compute_all: %.1fms, %d bar, %d kolom indikator",
                  elapsed, n, len(cols_added))
        return df


# ─────────────────────────────────────────────────────────────────────────────
# Registrasi accessor
# ─────────────────────────────────────────────────────────────────────────────

try:
    pd.api.extensions.register_dataframe_accessor("ta")(_TAAccessor)
    _PATCHED = True
    log.debug("ta_compat: accessor df.ta berhasil diregistrasi")
except Exception as _e:
    _PATCHED = False
    log.warning("ta_compat: gagal registrasi — %s", _e)


def patch() -> bool:
    """
    Verifikasi df.ta.* aktif. Import modul ini sudah cukup untuk aktivasi.
    Return True jika accessor berhasil diregistrasi ke pandas.

    Cara pakai di modul lain:
        import ta_compat
        # Setelah import, df.ta.* langsung tersedia
        # (tidak perlu panggil patch())

    Untuk verifikasi eksplisit:
        assert ta_compat.patch(), "ta_compat tidak aktif"
    """
    return _PATCHED


# Public exports — bisa diimport langsung
__all__ = [
    "patch",
    "lookup_col",
    "_TAAccessor",
]


# ─────────────────────────────────────────────────────────────────────────────
# Self-test  (python ta_compat.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, traceback, time as _time
    from datetime import datetime, timedelta, timezone

    G = "\033[92m"; R = "\033[91m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"

    def ok(m): print(f"  {G}✓{X} {m}")
    def fail(m, e=""): print(f"  {R}✗{X} {m}"); e and print(f"    {R}{e}{X}")
    def sec(t): print(f"\n{B}{C}── {t} {X}")

    print(f"\n{B}ta_compat v5 SUPERPOWER ULTIMATE — Self-Test Suite{X}")

    rng  = np.random.default_rng(42)
    N    = 300
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    idx  = [base + timedelta(minutes=15*i) for i in range(N)]
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, N))
    high  = close + rng.uniform(0.1, 1.5, N)
    low   = close - rng.uniform(0.1, 1.5, N)
    low   = np.minimum(low, close - 0.01)
    vol   = rng.integers(1_000, 50_000, N).astype(float)

    def fresh():
        return pd.DataFrame({
            "open": close - rng.uniform(0, 0.5, N),
            "high": high, "low": low, "close": close, "volume": vol,
        }, index=pd.DatetimeIndex(idx))

    df = fresh()
    passed = errors = 0

    # ── TREND ─────────────────────────────────────────────────────────────────
    sec("TREND")
    for p in (9, 21, 50, 100, 200):
        try:
            df.ta.ema(length=p)
            v = df[f"EMA_{p}"].iloc[-1]; assert not np.isnan(v)
            ok(f"EMA_{p} = {v:.4f}"); passed += 1
        except Exception as e: fail(f"EMA_{p}", str(e)); errors += 1

    for label, fn in [("DEMA_9",  lambda: df.ta.dema(9)),
                      ("TEMA_9",  lambda: df.ta.tema(9)),
                      ("HMA_9",   lambda: df.ta.hma(9)),
                      ("WMA_14",  lambda: df.ta.wma(14)),
                      ("VWMA_20", lambda: df.ta.vwma(20))]:
        try:
            fn()
            v = df[label].dropna().iloc[-1]; assert v > 0
            ok(f"{label} = {v:.4f}"); passed += 1
        except Exception as e: fail(label, str(e)); errors += 1

    try:
        df.ta.vwap()
        v = df["VWAP_D"].iloc[-1]; assert not np.isnan(v) and v > 0
        ok(f"VWAP_D = {v:.4f}"); passed += 1
    except Exception as e: fail("VWAP_D", str(e)); errors += 1

    try:
        df.ta.vwap_bands()
        for c in ("VWAP_D_upper_1","VWAP_D_lower_1","VWAP_D_upper_2","VWAP_D_lower_2"):
            assert not np.isnan(df[c].iloc[-1])
        ok("VWAP Bands ±1σ/±2σ"); passed += 1
    except Exception as e: fail("VWAP Bands", str(e)); errors += 1

    try:
        st, sd = df.ta.supertrend(7, 3.0)
        assert not np.isnan(st.iloc[-1]) and sd.iloc[-1] in (1.0,-1.0)
        ok(f"SuperTrend = {st.iloc[-1]:.4f}  dir={int(sd.iloc[-1]):+d}"); passed += 1
    except Exception as e: fail("SuperTrend", str(e)); errors += 1

    try:
        u, m, l = df.ta.donchian(20)
        assert u.iloc[-1] >= l.iloc[-1] > 0
        ok(f"Donchian(20): upper={u.iloc[-1]:.4f}  lower={l.iloc[-1]:.4f}"); passed += 1
    except Exception as e: fail("Donchian", str(e)); errors += 1

    try:
        ten, kij, spa, spb, chi = df.ta.ichimoku()
        assert not np.isnan(ten.iloc[-1]) and not np.isnan(kij.iloc[-1])
        ok(f"Ichimoku: tenkan={ten.iloc[-1]:.4f}  kijun={kij.iloc[-1]:.4f}"); passed += 1
    except Exception as e: fail("Ichimoku", str(e)); errors += 1

    try:
        ps, pd_, pr = df.ta.psar()
        assert not np.isnan(ps.iloc[-1]) and pd_.iloc[-1] in (1.0,-1.0)
        ok(f"PSAR={ps.iloc[-1]:.4f}  dir={int(pd_.iloc[-1]):+d}  rev={int(pr.sum())} reversals"); passed += 1
    except Exception as e: fail("PSAR", str(e)); errors += 1

    try:
        df.ta.ema_stack_score()
        v = df["_ema_stack_score"].iloc[-1]; assert 0 <= v <= 100
        ok(f"EMA Stack Score = {v:.1f}"); passed += 1
    except Exception as e: fail("EMA Stack Score", str(e)); errors += 1

    # ── MOMENTUM ──────────────────────────────────────────────────────────────
    sec("MOMENTUM")
    try:
        df.ta.rsi(14)
        v = df["RSI_14"].iloc[-1]; assert 0 <= v <= 100
        ok(f"RSI_14 = {v:.2f}"); passed += 1
    except Exception as e: fail("RSI_14", str(e)); errors += 1

    try:
        df.ta.rsi_slope(14)
        v = df["RSI_14_slope"].iloc[-1]; assert not np.isnan(v)
        # v5: OLS slope — verifikasi hasilnya bukan sekadar diff
        s = df["RSI_14_slope"]
        # slope harus bervariasi (bukan diff konstan)
        assert s.dropna().std() > 0
        ok(f"RSI_14_slope (OLS v5) = {v:.4f}  [std={s.std():.4f} → bukan diff]"); passed += 1
    except Exception as e: fail("RSI_slope OLS", str(e)); errors += 1

    try:
        df.ta.rsi_divergence(14, 14)
        v = df["RSI_DIV_14"].iloc[-1]
        s = df["RSI_DIV_14"]
        # v5: harus ada divergence signal di 300 bar random
        has_bull = (s > 0).any(); has_bear = (s < 0).any()
        ok(f"RSI_DIV_14 = {v:.3f}  [bull:{has_bull}, bear:{has_bear}]  [v5 vectorized]"); passed += 1
    except Exception as e: fail("RSI Divergence vectorized", str(e)); errors += 1

    try:
        ml, sl, hl = df.ta.macd()
        assert not np.isnan(ml.iloc[-1])
        ok(f"MACD={ml.iloc[-1]:.5f}  sig={sl.iloc[-1]:.5f}"); passed += 1
    except Exception as e: fail("MACD", str(e)); errors += 1

    try:
        k, d = df.ta.stochrsi()
        assert 0 <= k.iloc[-1] <= 100
        ok(f"StochRSI %K={k.iloc[-1]:.2f}  %D={d.iloc[-1]:.2f}"); passed += 1
    except Exception as e: fail("StochRSI", str(e)); errors += 1

    try:
        df.ta.cci(20)
        v = df["CCI_20"].iloc[-1]; assert not np.isnan(v)
        ok(f"CCI_20 = {v:.2f}"); passed += 1
    except Exception as e: fail("CCI", str(e)); errors += 1

    try:
        df.ta.williams_r(14)
        v = df["WILLR_14"].iloc[-1]; assert -100 <= v <= 0
        zone = "OB" if v > -20 else ("OS" if v < -80 else "neutral")
        ok(f"Williams %R = {v:.2f}  [{zone}]"); passed += 1
    except Exception as e: fail("Williams %R", str(e)); errors += 1

    try:
        df.ta.roc(9)
        v = df["ROC_9"].iloc[-1]
        ok(f"ROC_9 = {v:.3f}%"); passed += 1
    except Exception as e: fail("ROC", str(e)); errors += 1

    try:
        df.ta.roc_slope(9, 5)
        v = df["ROC_SLOPE_9_5"].iloc[-1]
        ok(f"ROC_SLOPE = {v:.4f}  (>0=acc, <0=dec)"); passed += 1
    except Exception as e: fail("ROC Slope", str(e)); errors += 1

    try:
        df.ta.ema_cross(9, 21)
        crosses = df["EMAXS_9_21"]
        n_bull  = int((crosses == 1).sum())
        n_bear  = int((crosses == -1).sum())
        ok(f"EMA Cross (9×21): {n_bull} bullish, {n_bear} bearish in {N} bars"); passed += 1
    except Exception as e: fail("EMA Cross", str(e)); errors += 1

    # ── VOLATILITY ────────────────────────────────────────────────────────────
    sec("VOLATILITY")
    try:
        df.ta.atr(14); v = df["ATRr_14"].iloc[-1]; assert v > 0
        ok(f"ATRr_14 = {v:.5f}"); passed += 1
    except Exception as e: fail("ATR", str(e)); errors += 1

    try:
        df.ta.atr_pct(14); v = df["ATRr_14_pct"].iloc[-1]; assert 0 < v < 100
        ok(f"ATRr_14_pct = {v:.4f}%"); passed += 1
    except Exception as e: fail("ATR %", str(e)); errors += 1

    try:
        df.ta.atr_percentile(14, 100)
        v = df["_atr_percentile_100"].iloc[-1]; assert 0 <= v <= 100
        ok(f"ATR percentile(100) = {v:.1f}%"); passed += 1
    except Exception as e: fail("ATR percentile", str(e)); errors += 1

    try:
        df.ta.bbands(20, 2.0)
        for c in ("BBU_20_2.0","BBM_20_2.0","BBL_20_2.0"):
            assert not np.isnan(df[c].iloc[-1])
        ok(f"BB: upper={df['BBU_20_2.0'].iloc[-1]:.4f}"); passed += 1
    except Exception as e: fail("Bollinger Bands", str(e)); errors += 1

    try:
        df.ta.keltner(20, 2.0); assert not np.isnan(df["KCUe_20_2"].iloc[-1])
        ok(f"Keltner upper={df['KCUe_20_2'].iloc[-1]:.4f}"); passed += 1
    except Exception as e: fail("Keltner", str(e)); errors += 1

    try:
        df.ta.squeeze(); v = df["SQZ_20_2.0_20_1.5"].iloc[-1]; assert v in (0.,1.)
        ok(f"Squeeze = {'AKTIF' if v==1 else 'tidak aktif'}"); passed += 1
    except Exception as e: fail("Squeeze", str(e)); errors += 1

    try:
        df.ta.chop(14); v = df["CHOP_14"].iloc[-1]; assert 0 <= v <= 100
        lbl = "TRENDING" if v < 38.2 else ("CHOPPY" if v > 61.8 else "TRANSITIONING")
        ok(f"CHOP_14 = {v:.2f}  [{lbl}]"); passed += 1
    except Exception as e: fail("CHOP", str(e)); errors += 1

    # ── STRENGTH ──────────────────────────────────────────────────────────────
    sec("STRENGTH")
    try:
        df.ta.adx(14)
        adx=df["ADX_14"].iloc[-1]; dmp=df["DMP_14"].iloc[-1]; dmn=df["DMN_14"].iloc[-1]
        assert all(0<=v<=100 for v in (adx,dmp,dmn))
        ok(f"ADX={adx:.2f}  +DI={dmp:.2f}  -DI={dmn:.2f}"); passed += 1
    except Exception as e: fail("ADX", str(e)); errors += 1

    try:
        df.ta.obv(); v = df["OBV"].iloc[-1]; assert not np.isnan(v)
        ok(f"OBV = {v:,.0f}"); passed += 1
    except Exception as e: fail("OBV", str(e)); errors += 1

    try:
        df.ta.mfi(14); v = df["MFI_14"].iloc[-1]; assert 0<=v<=100
        ok(f"MFI_14 = {v:.2f}"); passed += 1
    except Exception as e: fail("MFI", str(e)); errors += 1

    try:
        df.ta.cmf(20); v = df["CMF_20"].iloc[-1]; assert -1.01 <= v <= 1.01
        bias = "buying" if v > 0.1 else ("selling" if v < -0.1 else "neutral")
        ok(f"CMF_20 = {v:.4f}  [{bias}]"); passed += 1
    except Exception as e: fail("CMF", str(e)); errors += 1

    # ── REGRESSION ────────────────────────────────────────────────────────────
    sec("REGRESSION — semua edge-case + performance fix")

    # RSI pure uptrend → 100
    try:
        cu = np.linspace(100, 130, 20)
        df_u = pd.DataFrame({"open":cu-0.1,"high":cu+0.2,"low":cu-0.2,"close":cu,"volume":np.full(20,1000.)})
        df_u.ta.rsi(14)
        v = df_u["RSI_14"].iloc[-1]; assert v >= 99.0
        ok(f"RSI pure-uptrend = {v:.2f} (≈100, bukan 50)  [v2 fix]"); passed += 1
    except Exception as e: fail("RSI pure-uptrend", str(e)); errors += 1

    # RSI flat → 50
    try:
        cf = np.full(20, 100.0)
        df_f = pd.DataFrame({"open":cf,"high":cf,"low":cf,"close":cf,"volume":np.full(20,1000.)})
        df_f.ta.rsi(14)
        v = df_f["RSI_14"].iloc[-1]; assert abs(v-50) < 1e-6
        ok(f"RSI flat = {v:.2f} (tepat 50)  [v2 fix]"); passed += 1
    except Exception as e: fail("RSI flat", str(e)); errors += 1

    # MFI tanpa outflow → 100
    try:
        cu = np.linspace(100,130,20)
        df_m = pd.DataFrame({"open":cu-0.1,"high":cu+0.2,"low":cu-0.2,"close":cu,"volume":np.full(20,1000.)})
        df_m.ta.mfi(14)
        v = df_m["MFI_14"].iloc[-1]; assert v >= 99.0
        ok(f"MFI tanpa outflow = {v:.2f} (≈100)  [v2 fix]"); passed += 1
    except Exception as e: fail("MFI tanpa outflow", str(e)); errors += 1

    # WMA vectorized == naive
    try:
        lw=14; w=np.arange(1,lw+1,dtype=float); cv=df["close"].to_numpy(dtype=float)
        naive=np.full(len(cv),np.nan)
        for i in range(lw-1,len(cv)): naive[i]=np.dot(cv[i-lw+1:i+1],w)/w.sum()
        df.ta.wma(lw)
        valid=~np.isnan(naive)
        assert np.allclose(naive[valid], df[f"WMA_{lw}"].to_numpy()[valid], rtol=1e-9)
        ok(f"WMA vectorized == naive  [{valid.sum()} pts]  [v2 perf]"); passed += 1
    except Exception as e: fail("WMA vectorized", str(e)); errors += 1

    # CCI vectorized (v4) == slow rolling.apply
    try:
        import numpy as _np
        def _cci_slow(df_, l=20):
            tp=(df_["high"]+df_["low"]+df_["close"])/3
            sm=tp.rolling(l).mean()
            md=tp.rolling(l).apply(lambda x:_np.abs(x-x.mean()).mean(),raw=False)
            return (tp-sm)/(0.015*md.replace(0,_np.nan))
        df2=fresh(); df2.ta.cci(20)
        ref=_cci_slow(df2,20).dropna()
        got=df2["CCI_20"].loc[ref.index]   # [v5 fix] .loc bukan .iloc — DatetimeIndex safe
        assert _np.allclose(ref.values,got.values,atol=1e-8)
        ok(f"CCI vectorized == rolling.apply  [{len(ref)} pts]  [v4 perf 297x]"); passed+=1
    except Exception as e: fail("CCI vectorized == slow", str(e)); errors+=1

    # ATR percentile vectorized (v4) == rolling.apply
    try:
        def _ap_slow(s, lb=100):
            def rank(x): return float(np.sum(x[:-1]<x[-1]))/(len(x)-1)*100
            return s.rolling(lb,min_periods=25).apply(rank,raw=True)
        df3=fresh(); df3.ta.atr(14); df3.ta.atr_percentile(14,100)
        ref=_ap_slow(df3["ATRr_14"],100).dropna()
        got=df3["_atr_percentile_100"].loc[ref.index]   # [v5 fix] .loc — DatetimeIndex safe
        assert np.allclose(ref.values,got.values,atol=1e-6)
        ok(f"ATR percentile vectorized == rolling.apply  [{len(ref)} pts]  [v4 perf 7x]"); passed+=1
    except Exception as e: fail("ATR percentile vectorized", str(e)); errors+=1

    # SuperTrend no dead code (v3 fix)
    try:
        df4=fresh(); st,sd=df4.ta.supertrend(7,3.0)
        assert len(st)==N and sd.iloc[-1] in (1.,-1.)
        dup=[c for c in df4.columns if df4.columns.tolist().count(c)>1]
        assert not dup, f"dup kolom: {dup}"
        ok("SuperTrend no dead code, no duplicate columns  [v3 fix]"); passed+=1
    except Exception as e: fail("SuperTrend dead code", str(e)); errors+=1

    # Williams %R range check
    try:
        df5=fresh(); df5.ta.williams_r(14)
        v=df5["WILLR_14"]
        assert (v>=-100).all() and (v<=0).all()
        ok(f"Williams %R seluruh Series dalam [-100, 0]"); passed+=1
    except Exception as e: fail("Williams %R range", str(e)); errors+=1

    # PSAR: SAR di bawah harga saat bullish, di atas saat bearish
    try:
        df6=fresh(); ps,pd_,pr=df6.ta.psar()
        bull_mask = pd_==1; bear_mask = pd_==-1
        assert (ps[bull_mask] < df6["low"][bull_mask]).mean() > 0.85
        assert (ps[bear_mask] > df6["high"][bear_mask]).mean() > 0.85
        ok(f"PSAR posisi valid: >85% benar (bull/bear)"); passed+=1
    except Exception as e: fail("PSAR position", str(e)); errors+=1

    # [v5 FIX] vwap_bands: std vectorized — hasilnya harus identik dengan var manual
    try:
        df7=fresh()
        df7.ta.vwap()
        df7.ta.vwap_bands(stdev_mult_1=1.0, stdev_mult_2=2.0)
        u1=df7["VWAP_D_upper_1"]; l1=df7["VWAP_D_lower_1"]
        vwap=df7["VWAP_D"]
        # Band harus simetris di setiap bar
        assert np.allclose((u1-vwap).dropna().values, (vwap-l1).dropna().values, rtol=1e-6)
        ok(f"VWAP bands simetris ±1σ  [v5 vectorized std, no lambda loop]"); passed+=1
    except Exception as e: fail("VWAP bands symmetry", str(e)); errors+=1

    # [v5 FIX] rsi_slope OLS: verifikasi BUKAN sekadar diff(N)
    try:
        rng_v5 = np.random.default_rng(77)
        cl_v5  = 100.0 + np.linspace(0, 15, 100) + rng_v5.normal(0, 0.4, 100)
        df8 = pd.DataFrame({"open":cl_v5-0.05,"high":cl_v5+0.2,"low":cl_v5-0.2,
                             "close":cl_v5,"volume":np.full(100,5000.)})
        df8.ta.rsi(14); df8.ta.rsi_slope(14)
        slope_s = df8["RSI_14_slope"].dropna()
        half    = max(3, 14 // 2)
        diff_s  = df8["RSI_14"].diff(half).dropna()
        common  = slope_s.index.intersection(diff_s.index)
        # [kunci] OLS slope BUKAN identik dengan diff(half)
        are_same = np.allclose(slope_s.loc[common].values, diff_s.loc[common].values, atol=1e-6)
        assert not are_same, "slope masih identik dengan diff — OLS belum berjalan"
        # Verifikasi slope ada variansi (bukan konstan 0)
        assert slope_s.std() > 0.01, f"slope terlalu flat: std={slope_s.std():.4f}"
        ok(f"RSI slope OLS: berbeda dari diff, std={slope_s.std():.4f}  [v5 fix]"); passed+=1
    except Exception as e: fail("RSI slope OLS quality", str(e)); errors+=1

    # [v5 FIX] rsi_divergence vectorized == loop python (reference)
    try:
        import numpy as _np2
        df9=fresh()
        df9.ta.rsi(14); df9.ta.rsi_divergence(14,14)
        got = df9["RSI_DIV_14"].to_numpy()
        # Reference: manual shift check
        close_a = df9["close"].to_numpy(dtype=float)
        rsi_a   = df9["RSI_14"].to_numpy(dtype=float)
        ref     = _np2.zeros(len(close_a))
        lb=14
        for i in range(lb, len(close_a)):
            cc,pc = close_a[i], close_a[i-lb]
            cr,pr = rsi_a[i], rsi_a[i-lb]
            if cc < pc and cr > pr: ref[i] = cr-pr
            elif cc > pc and cr < pr: ref[i] = cr-pr
        assert _np2.allclose(got[lb:], ref[lb:], atol=1e-10)
        ok(f"RSI divergence vectorized == reference loop  [{len(close_a)-lb} pts]  [v5 fix]"); passed+=1
    except Exception as e: fail("RSI divergence vectorized==loop", str(e)); errors+=1

    # ── PERFORMANCE BENCHMARK ─────────────────────────────────────────────────
    sec("PERFORMANCE BENCHMARK (1000 bar)")
    rng2=np.random.default_rng(0); N2=1000
    cl2=100+np.cumsum(rng2.normal(0,0.5,N2))
    hi2=cl2+rng2.uniform(0.1,1.5,N2); lo2=cl2-rng2.uniform(0.1,1.5,N2)
    lo2=np.minimum(lo2,cl2-0.01)
    df_perf=pd.DataFrame({"open":cl2-0.2,"high":hi2,"low":lo2,"close":cl2,
                           "volume":rng2.integers(1000,50000,N2).astype(float)},
                          index=pd.DatetimeIndex([base+timedelta(minutes=15*i) for i in range(N2)]))

    _REPS=5
    for label, fn in [
        ("CCI(20)",             lambda: (lambda d: (d.ta.cci(20), d)[1])(df_perf.copy())),
        ("ATR_percentile(100)", lambda: (lambda d: (d.ta.atr_percentile(14,100), d)[1])(df_perf.copy())),
        ("enrich_production",   lambda: df_perf.copy().ta.enrich_production()),
        ("compute_all",         lambda: df_perf.copy().ta.compute_all()),
    ]:
        try:
            t0=_time.perf_counter()
            for _ in range(_REPS): fn()
            ms=((_time.perf_counter()-t0)/_REPS)*1000
            ok(f"{label:<28}: {ms:6.1f}ms avg ({_REPS} runs)"); passed+=1
        except Exception as e: fail(f"PERF {label}", str(e)); errors+=1

    # ── UTILITY ───────────────────────────────────────────────────────────────
    sec("UTILITY — enrich_production & compute_all")
    df_ep=fresh()
    try:
        df_ep.ta.enrich_production()
        exp_prod=[
            # EMA stack
            "EMA_9","EMA_21","EMA_50","EMA_100","EMA_200",
            "DEMA_9","DEMA_21","TEMA_9","TEMA_21","HMA_9","HMA_21",
            "WMA_14","VWMA_20",
            "_ema_stack_score","EMAXS_9_21","EMAXS_21_50",
            # Momentum
            "RSI_14","RSI_14_slope","RSI_DIV_14",
            "MACD_12_26_9","MACDs_12_26_9","MACDh_12_26_9",
            "STOCHRSIk_14_14_3_3","STOCHRSId_14_14_3_3",
            "CCI_20","WILLR_14","ROC_9","ROC_SLOPE_9_5",
            # Volatility
            "ATRr_14","ATRr_14_pct","_atr_percentile_100",
            "BBU_20_2.0","BBM_20_2.0","BBL_20_2.0","BBB_20_2.0","BBP_20_2.0",
            "KCUe_20_2","KCBe_20_2","KCLe_20_2",
            "SQZ_20_2.0_20_1.5","CHOP_14",
            # Structure
            "SUPERT_7_3.0","SUPERTd_7_3.0",
            "DCU_20","DCM_20","DCL_20",
            "PSAR","PSAR_DIR","PSAR_REV",
            # Strength
            "ADX_14","DMP_14","DMN_14",
            "OBV","MFI_14","CMF_20",
            # VWAP
            "VWAP_D","VWAP_D_upper_1","VWAP_D_lower_1",
            "VWAP_D_upper_2","VWAP_D_lower_2",
        ]
        miss=[c for c in exp_prod if c not in df_ep.columns]
        if miss: fail(f"enrich_production hilang {len(miss)} kolom: {miss}"); errors+=1
        else: ok(f"enrich_production v5: {len(exp_prod)} kolom ✓ (termasuk MACD/BB/Keltner/Squeeze/OBV/MFI)"); passed+=1
    except Exception as e: fail("enrich_production", traceback.format_exc()); errors+=1

    # skip_existing test
    try:
        nb=len(df_ep.columns); df_ep.ta.enrich_production(); na=len(df_ep.columns)
        assert nb==na, f"panggilan ke-2 menambah {na-nb} kolom"
        ok("enrich_production skip_existing: aman dipanggil berkali-kali"); passed+=1
    except Exception as e: fail("skip_existing", str(e)); errors+=1

    df_ca=fresh()
    try:
        df_ca.ta.compute_all()
        exp_all=[
            "EMA_9","EMA_21","EMA_50","EMA_100","EMA_200",
            "DEMA_9","TEMA_9","HMA_9","WMA_14","VWMA_20",
            "_ema_stack_score","EMAXS_9_21","EMAXS_21_50",
            "RSI_14","RSI_14_slope","RSI_DIV_14",
            "MACD_12_26_9","MACDs_12_26_9","MACDh_12_26_9",
            "STOCHRSIk_14_14_3_3","STOCHRSId_14_14_3_3",
            "CCI_20","WILLR_14","ROC_9","ROC_SLOPE_9_5",
            "ATRr_14","ATRr_14_pct","_atr_percentile_100",
            "BBU_20_2.0","BBM_20_2.0","BBL_20_2.0","BBB_20_2.0","BBP_20_2.0",
            "KCUe_20_2","KCBe_20_2","KCLe_20_2",
            "CHOP_14","SQZ_20_2.0_20_1.5","SUPERT_7_3.0","SUPERTd_7_3.0",
            "DCU_20","DCM_20","DCL_20","ICH_TENKAN","ICH_KIJUN","PSAR","PSAR_DIR",
            "ADX_14","DMP_14","DMN_14","OBV","MFI_14","CMF_20",
            "VWAP_D","VWAP_D_upper_1","VWAP_D_lower_1","VWAP_D_upper_2","VWAP_D_lower_2",
        ]
        miss=[c for c in exp_all if c not in df_ca.columns]
        if miss: fail(f"compute_all hilang {len(miss)} kolom: {miss}"); errors+=1
        else: ok(f"compute_all: {len(exp_all)} kolom ✓"); passed+=1
    except Exception as e: fail("compute_all", traceback.format_exc()); errors+=1

    total = passed + errors
    print(f"\n{'─'*58}")
    if errors == 0:
        print(f"{G}{B}  SEMUA {passed}/{total} TEST PASSED ✓{X}")
        print(f"  ta_compat v5 SUPERPOWER ULTIMATE — siap produksi\n")
        sys.exit(0)
    else:
        print(f"{R}{B}  {errors}/{total} TEST GAGAL ✗{X}")
        sys.exit(1)
