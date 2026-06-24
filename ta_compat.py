"""
ta_compat.py — Drop-in replacement pandas_ta untuk Termux / environment tanpa numba.

Menyediakan df.ta.<indikator>() identik dengan pandas_ta API, 100% pure NumPy/pandas.
Semua nama kolom output IDENTIK dengan yang didefinisikan di constants.py.

Indikator tersedia:
  Trend    : ema, wma, vwap, vwap_bands, supertrend
  Momentum : rsi, macd, stochrsi
  Volatility: atr, atr_pct, bbands, keltner, squeeze
  Strength : adx, obv, mfi
  Utility  : compute_all  ← satu panggilan isi semua kolom standar

CHANGELOG (audit & upgrade)
────────────────────────────────────────────────────────────────────────────
v2 — Bug fix & optimisasi (hasil audit menyeluruh + cross-reference repo):

  [BUG-FIX KRITIS] rsi(): kasus avg_loss == 0 (tidak ada candle merah sama
    sekali di seluruh data yang dihitung — pure uptrend) sebelumnya
    di-fillna(50) (netral), padahal secara matematis RSI harus → 100
    (overbought ekstrem). Kolom RSI_14 dari fungsi ini dipakai langsung
    sebagai entry gate (main.py "Gate3": skip entry jika RSI di luar
    rsi_min/rsi_max) dan exit gate (strategy.py QUICK_PROFIT: exit jika
    RSI > rsi_max). Bug lama membuat overbought ekstrem terbaca netral →
    risiko entry telat di puncak rally DAN gagal exit tepat waktu,
    persis di kondisi paling berisiko. Lihat juga indicators/momentum.py
    yang sudah benar menangani edge-case ini (rujukan fix).
  [BUG-FIX] mfi(): pola identik dengan bug RSI di atas (neg_sum == 0 → MFI
    seharusnya 100, bukan 50 jika ada inflow). Diperbaiki preventif
    walau saat ini compute_all()/mfi() belum terhubung ke decision path
    produksi manapun.
  [DRY/KONSISTENSI] adx() & supertrend(): sebelumnya masing-masing punya
    re-implementasi Wilder's-smoothing & True-Range sendiri secara lokal
    (mis. fungsi _ws() privat di dalam adx()) — duplikat dari
    _wilder_smooth()/_true_range() yang sudah ada di level modul. Ini
    persis kelas risiko yang menyebabkan bug RSI di atas (dua rumus
    matematika yang sama, bisa diam-diam berbeda kalau salah satu diubah
    tanpa mengubah yang lain). Sekarang keduanya memusat ke helper yang
    sama — sekaligus menghapus beberapa for-loop Python yang redundan.
  [PERFORMA] wma(): rolling().apply(lambda) (lambat, overhead callback
    Python per-window) diganti sliding_window_view + matmul (vectorized,
    pakai BLAS).
  [PERFORMA] adx(): directional-movement (+DM/-DM) & true range divektor-
    isasi (sebelumnya for-loop Python per-bar).
  [KONSISTENSI] Helper baru _fmt_param() & _ensure_datetime_index()
    menggantikan logic format-angka-untuk-nama-kolom dan konversi
    DatetimeIndex yang sebelumnya diduplikasi di keltner()/squeeze() dan
    vwap()/vwap_bands().
  [OBSERVASI ARSITEKTUR — tidak diubah, sekadar dicatat] compute_all() dan
    sebagian besar indikator di file ini (macd, stochrsi, bbands, keltner,
    squeeze, adx, obv, mfi, supertrend, wma, vwap_bands) TIDAK dipanggil
    di production code manapun (main.py/strategy.py/telegram_bot.py/
    api_server.py hanya memakai ema/rsi/atr/vwap dasar). Decision pipeline
    sesungguhnya (intelligence/observer.py → indicators/*.py) punya
    implementasi paralel sendiri untuk MACD/Stoch/ADX/dll. File ini hanya
    "satu-satunya sumber kebenaran" untuk RSI/EMA/ATR/VWAP dasar yang
    dipakai Gate3 (entry) dan QUICK_PROFIT/RTW (exit) — itulah sebabnya
    bug RSI di atas berdampak nyata ke keputusan trading, bukan sekadar
    tampilan dashboard.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("ta_compat")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers internal
# ─────────────────────────────────────────────────────────────────────────────

def _require(df: pd.DataFrame, *cols: str, ctx: str = "") -> bool:
    """Return False dan log warning jika ada kolom yang hilang."""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        log.warning("[ta_compat%s] kolom hilang: %s", f":{ctx}" if ctx else "", missing)
        return False
    return True


def _to_numeric(df: pd.DataFrame) -> None:
    """Konversi OHLCV ke float inplace (safe, coerce error ke NaN)."""
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def _ema_series(series: pd.Series, span: int) -> pd.Series:
    """EMA standar (exponential weighted, adjust=False)."""
    return series.ewm(span=span, adjust=False).mean()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing — identik dengan com=period-1 di ewm."""
    return series.ewm(com=period - 1, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    """True Range vector (3-komponen)."""
    high  = df["high"]
    low   = df["low"]
    prev  = df["close"].shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev).abs(),
        (low  - prev).abs(),
    ], axis=1).max(axis=1)
    # Bar pertama: tidak ada prev_close → pakai high-low
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def _fmt_param(x: float) -> str:
    """
    Format angka untuk nama kolom ala pandas_ta: '2' bukan '2.0' jika bulat,
    selain itu apa adanya. Dipusatkan di sini supaya keltner() dan squeeze()
    selalu menghasilkan string identik untuk parameter yang sama (sebelumnya
    logic ini diduplikasi terpisah di kedua method).
    """
    return f"{int(x)}" if x == int(x) else f"{x}"


def _ensure_datetime_index(df: pd.DataFrame, ctx: str = "") -> bool:
    """
    Pastikan df.index berupa DatetimeIndex (dibutuhkan vwap/vwap_bands untuk
    reset per-anchor). Konversi inplace bila perlu dan mungkin.

    Returns
    -------
    True  jika index sudah/berhasil jadi DatetimeIndex.
    False jika gagal dikonversi — caller harus fallback ke mode kumulatif.
    """
    if isinstance(df.index, pd.DatetimeIndex):
        return True
    try:
        df.index = pd.to_datetime(df.index, utc=True)
        return True
    except Exception as exc:
        log.debug(
            "%s: index bukan/tidak bisa dikonversi ke DatetimeIndex — %s",
            ctx, exc,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Accessor utama
# ─────────────────────────────────────────────────────────────────────────────

class _TAAccessor:
    """
    Pandas DataFrame accessor: df.ta.<method>()

    Semua method mengikuti konvensi pandas_ta:
      - append=True  → kolom ditambahkan ke df (default)
      - return value → pd.Series hasil kalkulasi
    """

    def __init__(self, df: pd.DataFrame) -> None:
        _to_numeric(df)
        self._df = df

    # ── TREND ────────────────────────────────────────────────────────────────

    def ema(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        Exponential Moving Average.
        Output kolom: EMA_{length}  (e.g. EMA_9, EMA_21, EMA_50, EMA_100, EMA_200)
        """
        if not _require(self._df, "close", ctx="ema"):
            return pd.Series(dtype=float)
        col    = f"EMA_{length}"
        result = _ema_series(self._df["close"], length)
        if append:
            self._df[col] = result
        return result

    def wma(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        Weighted Moving Average (linearly weighted).
        Output kolom: WMA_{length}

        Diimplementasikan dengan numpy sliding_window_view + matmul (BLAS),
        bukan rolling().apply(lambda) — jauh lebih cepat karena menghindari
        overhead pemanggilan fungsi Python per-window (signifikan pada data
        ribuan bar, mis. saat backtest/training jangka panjang).
        """
        if not _require(self._df, "close", ctx="wma"):
            return pd.Series(dtype=float)
        col    = f"WMA_{length}"
        close  = self._df["close"].to_numpy(dtype=float)
        n      = len(close)
        w      = np.arange(1, length + 1, dtype=float)
        w_sum  = w.sum()

        out = np.full(n, np.nan)
        if n >= length:
            windows  = np.lib.stride_tricks.sliding_window_view(close, length)
            out[length - 1:] = windows @ w / w_sum  # oldest..newest cocok urutan w

        result = pd.Series(out, index=self._df.index)
        if append:
            self._df[col] = result
        return result

    def vwap(self, anchor: str = "D", append: bool = True, **kwargs) -> pd.Series:
        """
        Volume Weighted Average Price dengan anchor period yang fleksibel.
        VWAP di-reset setiap awal periode anchor.

        Parameters
        ----------
        anchor : str
            Period reset VWAP. Mendukung semua alias pandas Period:
              'D'  → harian   (default, output: VWAP_D)
              'W'  → mingguan (output: VWAP_W)
              'M'  → bulanan  (output: VWAP_M)
              'Q'  → kuartalan (output: VWAP_Q)
            Jika index bukan DatetimeIndex, VWAP dihitung kumulatif tanpa reset.

        Output kolom: VWAP_{anchor}  (e.g. VWAP_D, VWAP_W, VWAP_M)
        """
        df  = self._df
        col = f"VWAP_{anchor}"

        if not _require(df, "high", "low", "close", "volume", ctx="vwap"):
            return pd.Series(dtype=float)

        _ensure_datetime_index(df, ctx="vwap")

        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        tpv     = typical * df["volume"]

        if isinstance(df.index, pd.DatetimeIndex):
            first_ts = df.index[0]
            if anchor == "D" and (first_ts.hour != 0 or first_ts.minute != 0):
                log.debug(
                    "VWAP: data mulai %s (bukan 00:00 UTC) — bar pertama mungkin inakurat",
                    first_ts,
                )
            # Strip timezone sebelum to_period agar tidak ada UserWarning
            try:
                periods = df.index.tz_convert(None).to_period(anchor)
            except Exception:
                periods = df.index.to_period(anchor)
            cumtpv = tpv.groupby(periods).cumsum()
            cumvol = df["volume"].groupby(periods).cumsum()
        else:
            log.debug("vwap: index bukan DatetimeIndex — VWAP dihitung kumulatif tanpa reset")
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
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        VWAP ± 1σ dan ± 2σ bands (volume-weighted std per periode anchor).

        Parameters
        ----------
        anchor       : period reset, sama dengan df.ta.vwap() — 'D', 'W', 'M', dll.
        stdev_mult_1 : multiplier std untuk band ke-1 (default 1.0)
        stdev_mult_2 : multiplier std untuk band ke-2 (default 2.0)

        Output kolom:
          VWAP_{anchor}_upper_1, VWAP_{anchor}_lower_1  (±1σ)
          VWAP_{anchor}_upper_2, VWAP_{anchor}_lower_2  (±2σ)
        """
        df      = self._df
        vwap_col = f"VWAP_{anchor}"

        # Pastikan VWAP tersedia untuk anchor yang diminta
        if vwap_col not in df.columns:
            self.vwap(anchor=anchor, append=True)

        if not _require(df, vwap_col, "high", "low", "close", "volume", ctx="vwap_bands"):
            empty = pd.Series(dtype=float)
            return empty, empty, empty, empty

        _ensure_datetime_index(df, ctx="vwap_bands")

        vwap    = df[vwap_col]
        volume  = df["volume"]

        def _rolling_vwap_std(group_df: pd.DataFrame) -> pd.Series:
            """Std volume-weighted kumulatif dalam satu periode."""
            tp     = (group_df["high"] + group_df["low"] + group_df["close"]) / 3.0
            vol    = group_df["volume"]
            vw     = group_df[vwap_col]
            cumvol = vol.cumsum().replace(0, np.nan)
            var_s  = (vol * (tp - vw) ** 2).cumsum() / cumvol
            return var_s.apply(lambda v: np.sqrt(max(v, 0.0)) if pd.notna(v) else np.nan)

        if isinstance(df.index, pd.DatetimeIndex):
            try:
                periods = df.index.tz_convert(None).to_period(anchor)
            except Exception:
                periods = df.index.to_period(anchor)
            std_series = df.groupby(periods, group_keys=False).apply(_rolling_vwap_std)
        else:
            typical   = (df["high"] + df["low"] + df["close"]) / 3.0
            cumvol    = volume.cumsum().replace(0, np.nan)
            var_cum   = (volume * (typical - vwap) ** 2).cumsum() / cumvol
            std_series = var_cum.apply(lambda v: np.sqrt(max(v, 0.0)) if pd.notna(v) else np.nan)

        upper_1 = vwap + stdev_mult_1 * std_series
        lower_1 = vwap - stdev_mult_1 * std_series
        upper_2 = vwap + stdev_mult_2 * std_series
        lower_2 = vwap - stdev_mult_2 * std_series

        if append:
            df[f"{vwap_col}_upper_1"] = upper_1
            df[f"{vwap_col}_lower_1"] = lower_1
            df[f"{vwap_col}_upper_2"] = upper_2
            df[f"{vwap_col}_lower_2"] = lower_2

        return upper_1, lower_1, upper_2, lower_2

    def supertrend(
        self,
        length: int = 7,
        multiplier: float = 3.0,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        SuperTrend indicator.
        Output kolom:
          SUPERT_7_3.0   — nilai garis SuperTrend
          SUPERTd_7_3.0  — arah: 1 = bullish, -1 = bearish

        Catatan implementasi: TR/ATR dihitung lewat `_true_range()` +
        `_wilder_smooth()` (helper modul, sama dipakai atr()/rsi()/adx()) —
        bukan loop lokal terpisah. Hanya band/direction yang tetap berupa
        loop Python: nilainya rekursif-kondisional (final_ub[i] bergantung
        pada keputusan final_ub[i-1] DAN close[i-1] sekaligus), sehingga
        secara matematis tidak bisa divektorisasi murni dengan numpy/pandas
        tanpa pustaka tambahan (mis. numba) — yang justru bertentangan
        dengan tujuan modul ini (drop-in tanpa numba untuk Termux).
        """
        df  = self._df
        col_val = f"SUPERT_{length}_{multiplier}"
        col_dir = f"SUPERTd_{length}_{multiplier}"

        if not _require(df, "high", "low", "close", ctx="supertrend"):
            empty = pd.Series(dtype=float)
            return empty, empty

        min_bars = length + 2
        if len(df) < min_bars:
            log.warning("supertrend: data hanya %d bar, butuh minimal %d", len(df), min_bars)
            empty = pd.Series(dtype=float)
            return empty, empty

        high  = df["high"].to_numpy(dtype=float)
        low   = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        n     = len(close)

        atr_arr = _wilder_smooth(_true_range(df), length).to_numpy(dtype=float)

        # Basic upper/lower bands
        hl2       = (high + low) / 2.0
        raw_ub    = hl2 + multiplier * atr_arr
        raw_lb    = hl2 - multiplier * atr_arr

        final_ub  = np.empty(n)
        final_lb  = np.empty(n)
        direction = np.zeros(n, dtype=int)
        st_line   = np.empty(n)

        final_ub[0] = raw_ub[0]
        final_lb[0] = raw_lb[0]
        direction[0] = 1
        st_line[0]   = final_lb[0]

        for i in range(1, n):
            # Final upper band
            final_ub[i] = (
                raw_ub[i]
                if raw_ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]
                else final_ub[i - 1]
            )
            # Final lower band
            final_lb[i] = (
                raw_lb[i]
                if raw_lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]
                else final_lb[i - 1]
            )
            # Direction
            if direction[i - 1] == -1:
                direction[i] = 1 if close[i] > final_ub[i] else -1
            else:
                direction[i] = -1 if close[i] < final_lb[i] else 1

            st_line[i] = final_lb[i] if direction[i] == 1 else final_ub[i]

        st_series  = pd.Series(st_line,  index=df.index)
        dir_series = pd.Series(direction.astype(float), index=df.index)

        if append:
            df[col_val] = st_series
            df[col_dir] = dir_series

        return st_series, dir_series
        hl2       = (high + low) / 2.0
        raw_ub    = hl2 + multiplier * atr_arr
        raw_lb    = hl2 - multiplier * atr_arr

        final_ub  = np.empty(n)
        final_lb  = np.empty(n)
        direction = np.zeros(n, dtype=int)
        st_line   = np.empty(n)

        final_ub[0] = raw_ub[0]
        final_lb[0] = raw_lb[0]
        direction[0] = 1
        st_line[0]   = final_lb[0]

        for i in range(1, n):
            # Final upper band
            final_ub[i] = (
                raw_ub[i]
                if raw_ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]
                else final_ub[i - 1]
            )
            # Final lower band
            final_lb[i] = (
                raw_lb[i]
                if raw_lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]
                else final_lb[i - 1]
            )
            # Direction
            if direction[i - 1] == -1:
                direction[i] = 1 if close[i] > final_ub[i] else -1
            else:
                direction[i] = -1 if close[i] < final_lb[i] else 1

            st_line[i] = final_lb[i] if direction[i] == 1 else final_ub[i]

        st_series  = pd.Series(st_line,  index=df.index)
        dir_series = pd.Series(direction.astype(float), index=df.index)

        if append:
            df[col_val] = st_series
            df[col_dir] = dir_series

        return st_series, dir_series

    # ── MOMENTUM ─────────────────────────────────────────────────────────────

    def rsi(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        Relative Strength Index (Wilder's smoothing, sesuai standar industri).
        Output kolom: RSI_{length}  (e.g. RSI_14)

        Penanganan edge-case avg_loss == 0 (PENTING — lihat CHANGELOG modul):
          - avg_loss == 0 dan avg_gain >  0  → RSI = 100  (tidak ada satu pun
            candle turun di seluruh data yang dihitung — overbought ekstrem,
            BUKAN netral). Sebelumnya kasus ini salah diisi 50 (lihat fix v2).
          - avg_loss == 0 dan avg_gain == 0  → RSI = 50   (harga benar-benar
            flat / tidak ada gerakan sama sekali — netral sejati).
          - Sisa NaN (mis. warm-up data sangat pendek) → fallback 50.
        """
        if not _require(self._df, "close", ctx="rsi"):
            return pd.Series(dtype=float)
        col = f"RSI_{length}"
        n   = len(self._df)
        if n < length + 1:
            log.warning(
                "rsi: data hanya %d bar, butuh minimal %d — hasil awal kurang andal",
                n, length + 1,
            )

        delta = self._df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)

        avg_gain = _wilder_smooth(gain, length)
        avg_loss = _wilder_smooth(loss, length)

        rs     = avg_gain / avg_loss.replace(0, np.nan)
        result = 100 - (100 / (1 + rs))

        no_loss         = avg_loss == 0
        no_loss_pure_up = no_loss & (avg_gain > 0)
        no_loss_flat    = no_loss & (avg_gain == 0)
        result = result.mask(no_loss_pure_up, 100.0)
        result = result.mask(no_loss_flat, 50.0)
        result = result.fillna(50.0)

        if append:
            self._df[col] = result
        return result

    def macd(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD — Moving Average Convergence Divergence.
        Output kolom:
          MACD_{fast}_{slow}_{signal}   — MACD line
          MACDs_{fast}_{slow}_{signal}  — Signal line
          MACDh_{fast}_{slow}_{signal}  — Histogram
        Default mengikuti pandas_ta: MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
        """
        if not _require(self._df, "close", ctx="macd"):
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        n = len(self._df)
        min_bars = slow + signal
        if n < min_bars:
            log.warning("macd: data hanya %d bar, idealnya %d+ untuk hasil stabil", n, min_bars)

        col_m = f"MACD_{fast}_{slow}_{signal}"
        col_s = f"MACDs_{fast}_{slow}_{signal}"
        col_h = f"MACDh_{fast}_{slow}_{signal}"

        close      = self._df["close"]
        fast_ema   = _ema_series(close, fast)
        slow_ema   = _ema_series(close, slow)
        macd_line  = fast_ema - slow_ema
        sig_line   = _ema_series(macd_line, signal)
        histogram  = macd_line - sig_line

        if append:
            self._df[col_m] = macd_line
            self._df[col_s] = sig_line
            self._df[col_h] = histogram

        return macd_line, sig_line, histogram

    def stochrsi(
        self,
        length: int = 14,
        rsi_length: int = 14,
        k: int = 3,
        d: int = 3,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Stochastic RSI — normalisasi RSI ke skala 0-100 lalu smooth %K dan %D.
        Output kolom:
          STOCHRSIk_{length}_{rsi_length}_{k}_{d}  — %K smooth
          STOCHRSId_{length}_{rsi_length}_{k}_{d}  — %D (MA of %K)
        Default: STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3
        """
        if not _require(self._df, "close", ctx="stochrsi"):
            empty = pd.Series(dtype=float)
            return empty, empty

        n = len(self._df)
        min_bars = rsi_length + length
        if n < min_bars:
            log.warning("stochrsi: data hanya %d bar, idealnya %d+ untuk hasil stabil", n, min_bars)

        col_k = f"STOCHRSIk_{length}_{rsi_length}_{k}_{d}"
        col_d = f"STOCHRSId_{length}_{rsi_length}_{k}_{d}"

        # Hitung RSI dulu (tanpa append ke df — hanya untuk keperluan StochRSI ini)
        delta    = self._df["close"].diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = _wilder_smooth(gain, rsi_length)
        avg_loss = _wilder_smooth(loss, rsi_length)
        rs       = avg_gain / avg_loss.replace(0, np.nan)
        rsi_s    = (100 - (100 / (1 + rs))).fillna(50.0)

        # Stochastic dari RSI
        rsi_min = rsi_s.rolling(length, min_periods=length).min()
        rsi_max = rsi_s.rolling(length, min_periods=length).max()
        rsi_rng = (rsi_max - rsi_min).replace(0, np.nan)
        raw_k   = ((rsi_s - rsi_min) / rsi_rng * 100).fillna(50.0)

        # Smooth %K dan %D
        k_line = raw_k.rolling(k, min_periods=1).mean()
        d_line = k_line.rolling(d, min_periods=1).mean()

        if append:
            self._df[col_k] = k_line
            self._df[col_d] = d_line

        return k_line, d_line

    # ── VOLATILITY ───────────────────────────────────────────────────────────

    def atr(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        Average True Range (Wilder's smoothing).
        Output kolom: ATRr_{length}  (e.g. ATRr_14)
        """
        if not _require(self._df, "high", "low", "close", ctx="atr"):
            return pd.Series(dtype=float)
        col    = f"ATRr_{length}"
        tr     = _true_range(self._df)
        result = _wilder_smooth(tr, length)
        if append:
            self._df[col] = result
        return result

    def atr_pct(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        ATR sebagai % dari harga close — ukuran volatilitas relatif.
        Output kolom: ATRr_{length}_pct  (e.g. ATRr_14_pct)
        """
        if not _require(self._df, "high", "low", "close", ctx="atr_pct"):
            return pd.Series(dtype=float)
        col_atr = f"ATRr_{length}"
        col_pct = f"ATRr_{length}_pct"

        if col_atr not in self._df.columns:
            self.atr(length=length, append=True)

        result = (self._df[col_atr] / self._df["close"].replace(0, np.nan)) * 100.0
        if append:
            self._df[col_pct] = result
        return result

    def bbands(
        self,
        length: int = 20,
        std: float = 2.0,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Bollinger Bands.
        Output kolom (nama identik pandas_ta):
          BBU_{length}_{std}  — Upper band
          BBM_{length}_{std}  — Middle band (SMA)
          BBL_{length}_{std}  — Lower band
          BBB_{length}_{std}  — Bandwidth = (Upper-Lower)/Middle * 100
          BBP_{length}_{std}  — %B position = (close-Lower)/(Upper-Lower)
        Default: BBU_20_2.0, BBM_20_2.0, BBL_20_2.0, BBB_20_2.0, BBP_20_2.0
        """
        if not _require(self._df, "close", ctx="bbands"):
            empty = pd.Series(dtype=float)
            return empty, empty, empty, empty, empty

        n = len(self._df)
        if n < length:
            log.warning("bbands: data hanya %d bar, butuh minimal %d", n, length)

        std_str  = f"{std:.1f}"
        col_u    = f"BBU_{length}_{std_str}"
        col_m    = f"BBM_{length}_{std_str}"
        col_l    = f"BBL_{length}_{std_str}"
        col_b    = f"BBB_{length}_{std_str}"
        col_p    = f"BBP_{length}_{std_str}"

        close  = self._df["close"]
        middle = close.rolling(length, min_periods=length).mean()
        stddev = close.rolling(length, min_periods=length).std(ddof=0)  # population std

        upper = middle + std * stddev
        lower = middle - std * stddev
        bw    = ((upper - lower) / middle.replace(0, np.nan)) * 100.0
        pos   = (close - lower) / (upper - lower).replace(0, np.nan)

        if append:
            self._df[col_u] = upper
            self._df[col_m] = middle
            self._df[col_l] = lower
            self._df[col_b] = bw
            self._df[col_p] = pos

        return upper, middle, lower, bw, pos

    def keltner(
        self,
        length: int = 20,
        scalar: float = 2.0,
        atr_length: int = 14,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Keltner Channel (EMA ± scalar × ATR).
        Output kolom (nama identik pandas_ta):
          KCUe_{length}_{scalar}  — Upper channel
          KCBe_{length}_{scalar}  — Middle (EMA)
          KCLe_{length}_{scalar}  — Lower channel
        Default: KCUe_20_2, KCBe_20_2, KCLe_20_2
        """
        if not _require(self._df, "high", "low", "close", ctx="keltner"):
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        n = len(self._df)
        if n < length:
            log.warning("keltner: data hanya %d bar, butuh minimal %d", n, length)

        scalar_str = _fmt_param(scalar)
        col_u = f"KCUe_{length}_{scalar_str}"
        col_b = f"KCBe_{length}_{scalar_str}"
        col_l = f"KCLe_{length}_{scalar_str}"

        # ATR — pakai yang sudah ada di df, atau hitung baru
        atr_col = f"ATRr_{atr_length}"
        if atr_col not in self._df.columns:
            atr_s = _wilder_smooth(_true_range(self._df), atr_length)
        else:
            atr_s = self._df[atr_col]

        middle = _ema_series(self._df["close"], length)
        upper  = middle + scalar * atr_s
        lower  = middle - scalar * atr_s

        if append:
            self._df[col_u] = upper
            self._df[col_b] = middle
            self._df[col_l] = lower

        return upper, middle, lower

    def squeeze(
        self,
        bb_length: int = 20,
        bb_mult: float = 2.0,
        kc_length: int = 20,
        kc_mult: float = 1.5,
        append: bool = True,
        **kwargs,
    ) -> pd.Series:
        """
        Bollinger Band / Keltner Channel Squeeze.
        Nilai: 1.0 = squeeze aktif (BB di dalam KC), 0.0 = squeeze tidak aktif.
        Output kolom: SQZ_{bb_length}_{bb_mult}_{kc_length}_{kc_mult}
        Default: SQZ_20_2.0_20_1.5
        """
        col = f"SQZ_{bb_length}_{bb_mult}_{kc_length}_{kc_mult}"

        # Pastikan BB dan KC tersedia
        bb_std_str = f"{bb_mult:.1f}"
        kc_scalar  = _fmt_param(kc_mult)
        col_bbu    = f"BBU_{bb_length}_{bb_std_str}"
        col_bbl    = f"BBL_{bb_length}_{bb_std_str}"
        col_kcu    = f"KCUe_{kc_length}_{kc_scalar}"
        col_kcl    = f"KCLe_{kc_length}_{kc_scalar}"

        if col_bbu not in self._df.columns:
            self.bbands(length=bb_length, std=bb_mult, append=True)
        if col_kcu not in self._df.columns:
            self.keltner(length=kc_length, scalar=kc_mult, append=True)

        bb_upper = self._df[col_bbu]
        bb_lower = self._df[col_bbl]
        kc_upper = self._df[col_kcu]
        kc_lower = self._df[col_kcl]

        # Squeeze = BB sepenuhnya berada di dalam KC
        in_squeeze = (bb_upper <= kc_upper) & (bb_lower >= kc_lower)
        result     = in_squeeze.astype(float)

        if append:
            self._df[col] = result
        return result

    # ── STRENGTH ─────────────────────────────────────────────────────────────

    def adx(
        self,
        length: int = 14,
        append: bool = True,
        **kwargs,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Average Directional Index + Directional Movement Indicators.
        Output kolom:
          ADX_{length}  — ADX (0-100, trend strength)
          DMP_{length}  — +DI (bullish directional movement)
          DMN_{length}  — -DI (bearish directional movement)
        Default: ADX_14, DMP_14, DMN_14

        Catatan implementasi: Wilder's smoothing dipusatkan lewat
        `_wilder_smooth()` (helper modul yang sama dipakai rsi()/atr()),
        bukan reimplementasi loop lokal — menghindari risiko dua rumus
        matematika yang sama tapi diam-diam berbeda (lihat CHANGELOG modul).
        """
        if not _require(self._df, "high", "low", "close", ctx="adx"):
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        col_adx = f"ADX_{length}"
        col_dmp = f"DMP_{length}"
        col_dmn = f"DMN_{length}"

        df  = self._df
        idx = df.index
        n   = len(df)

        if n < length + 1:
            log.warning("adx: data hanya %d bar, butuh %d+", n, length)
            empty = pd.Series(dtype=float)
            return empty, empty, empty

        high = df["high"].to_numpy(dtype=float)
        low  = df["low"].to_numpy(dtype=float)

        # Directional movement — divektorisasi (sebelumnya for-loop Python).
        up_move   = np.diff(high, prepend=high[0])
        down_move = -np.diff(low, prepend=low[0])
        plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr_series       = _true_range(df)
        plus_dm_series  = pd.Series(plus_dm, index=idx)
        minus_dm_series = pd.Series(minus_dm, index=idx)

        sm_tr    = _wilder_smooth(tr_series, length)
        sm_plus  = _wilder_smooth(plus_dm_series, length)
        sm_minus = _wilder_smooth(minus_dm_series, length)

        sm_tr_safe = sm_tr.replace(0, np.nan)
        di_plus    = (100.0 * sm_plus  / sm_tr_safe).fillna(0.0)
        di_minus   = (100.0 * sm_minus / sm_tr_safe).fillna(0.0)

        di_sum = (di_plus + di_minus).replace(0, np.nan)
        dx     = (100.0 * (di_plus - di_minus).abs() / di_sum).fillna(0.0)

        s_adx = _wilder_smooth(dx, length)
        s_dmp = di_plus
        s_dmn = di_minus

        if append:
            df[col_adx] = s_adx
            df[col_dmp] = s_dmp
            df[col_dmn] = s_dmn

        return s_adx, s_dmp, s_dmn

    def obv(self, append: bool = True, **kwargs) -> pd.Series:
        """
        On-Balance Volume — kumulatif volume berdasarkan arah harga.
        Output kolom: OBV
        """
        if not _require(self._df, "close", "volume", ctx="obv"):
            return pd.Series(dtype=float)

        close  = self._df["close"]
        volume = self._df["volume"]
        sign   = np.sign(close.diff().fillna(0))
        result = (sign * volume).cumsum()

        if append:
            self._df["OBV"] = result
        return result

    def mfi(self, length: int = 14, append: bool = True, **kwargs) -> pd.Series:
        """
        Money Flow Index — RSI berbasis volume (0-100).
        Output kolom: MFI_{length}  (e.g. MFI_14)

        Penanganan edge-case neg_sum == 0 (sama prinsipnya dengan rsi(), lihat
        CHANGELOG modul): tidak ada outflow sama sekali → MFI = 100 (bukan 50),
        kecuali pos_sum juga 0 (tidak ada flow sama sekali) → MFI = 50.
        """
        if not _require(self._df, "high", "low", "close", "volume", ctx="mfi"):
            return pd.Series(dtype=float)
        col = f"MFI_{length}"
        n   = len(self._df)
        if n < length + 1:
            log.warning(
                "mfi: data hanya %d bar, butuh minimal %d — hasil awal kurang andal",
                n, length + 1,
            )

        df           = self._df
        typical      = (df["high"] + df["low"] + df["close"]) / 3.0
        raw_mf       = typical * df["volume"]
        tp_change    = typical.diff()

        pos_mf = raw_mf.where(tp_change > 0, 0.0)
        neg_mf = raw_mf.where(tp_change < 0, 0.0)

        pos_sum = pos_mf.rolling(length, min_periods=length).sum()
        neg_sum = neg_mf.rolling(length, min_periods=length).sum()

        mfr    = pos_sum / neg_sum.replace(0, np.nan)
        result = 100.0 - (100.0 / (1.0 + mfr))

        no_outflow             = neg_sum == 0
        no_outflow_has_inflow  = no_outflow & (pos_sum > 0)
        no_outflow_no_flow     = no_outflow & (pos_sum == 0)
        result = result.mask(no_outflow_has_inflow, 100.0)
        result = result.mask(no_outflow_no_flow, 50.0)
        result = result.fillna(50.0)

        if append:
            self._df[col] = result
        return result

    # ── UTILITY ──────────────────────────────────────────────────────────────

    def compute_all(
        self,
        ema_periods: Tuple[int, ...] = (9, 21, 50, 100, 200),
        with_vwap_bands: bool = True,
    ) -> pd.DataFrame:
        """
        Hitung SEMUA indikator standar dalam satu panggilan.
        Mengisi DataFrame dengan seluruh kolom yang didefinisikan di constants.py.

        Urutan eksekusi dioptimalkan: indikator dasar dihitung dulu,
        lalu indikator turunan (Squeeze, VWAP bands) yang bergantung padanya.

        Parameters
        ----------
        ema_periods     : periode EMA yang dihitung (default semua standar)
        with_vwap_bands : hitung VWAP bands jika True (butuh DatetimeIndex)

        Returns
        -------
        df  : DataFrame yang sudah diperkaya (referensi inplace, bukan copy)
        """
        df = self._df
        n  = len(df)

        log.debug("compute_all: mulai, %d bar", n)

        # ── Layer 1: EMA (dibutuhkan Keltner) ────────────────────────────────
        for p in ema_periods:
            self.ema(length=p, append=True)

        # ── Layer 2: Indikator dasar independen ──────────────────────────────
        self.rsi(length=14,  append=True)
        self.atr(length=14,  append=True)
        self.atr_pct(length=14, append=True)
        self.obv(append=True)

        # ── Layer 3: MACD, StochRSI, ADX, MFI ───────────────────────────────
        self.macd(fast=12, slow=26, signal=9, append=True)
        self.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)
        self.adx(length=14, append=True)
        self.mfi(length=14, append=True)

        # ── Layer 4: BB, Keltner ─────────────────────────────────────────────
        self.bbands(length=20, std=2.0, append=True)
        self.keltner(length=20, scalar=2.0, atr_length=14, append=True)

        # ── Layer 5: Squeeze (butuh BB + KC dari layer 4) ────────────────────
        self.squeeze(bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5, append=True)

        # ── Layer 6: SuperTrend ───────────────────────────────────────────────
        self.supertrend(length=7, multiplier=3.0, append=True)

        # ── Layer 7: VWAP + Bands (butuh DatetimeIndex) ──────────────────────
        self.vwap(anchor="D", append=True)
        if with_vwap_bands:
            self.vwap_bands(stdev_mult_1=1.0, stdev_mult_2=2.0, append=True)

        cols_added = [c for c in df.columns if c not in ("open","high","low","close","volume")]
        log.debug("compute_all: selesai, %d kolom indikator ditambahkan", len(cols_added))

        return df


# ─────────────────────────────────────────────────────────────────────────────
# Registrasi accessor
# ─────────────────────────────────────────────────────────────────────────────

try:
    pd.api.extensions.register_dataframe_accessor("ta")(_TAAccessor)
    _PATCHED = True
    log.debug("ta_compat: accessor df.ta berhasil diregistrasi")
except Exception as _reg_exc:
    _PATCHED = False
    log.warning("ta_compat: gagal registrasi accessor — %s", _reg_exc)


def patch() -> bool:
    """
    Panggil untuk memverifikasi bahwa df.ta.* aktif.
    Import modul ini sudah cukup untuk aktivasi.
    Returns True jika registrasi berhasil.
    """
    return _PATCHED


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (python ta_compat.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import traceback
    from datetime import datetime, timedelta, timezone

    GREEN = "\033[92m"
    RED   = "\033[91m"
    CYAN  = "\033[96m"
    BOLD  = "\033[1m"
    RESET = "\033[0m"

    def ok(msg: str) -> None:
        print(f"  {GREEN}✓{RESET} {msg}")

    def fail(msg: str, err: str = "") -> None:
        print(f"  {RED}✗{RESET} {msg}")
        if err:
            print(f"    {RED}{err}{RESET}")

    def section(title: str) -> None:
        print(f"\n{BOLD}{CYAN}── {title} {RESET}")

    print(f"\n{BOLD}ta_compat — Self-Test Suite{RESET}")

    # ── Buat dataset realistis ─────────────────────────────────────────────
    rng = np.random.default_rng(42)
    N   = 300
    base_ts = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    idx  = [base_ts + timedelta(minutes=15 * i) for i in range(N)]

    close = 100.0 + np.cumsum(rng.normal(0, 0.5, N))
    high  = close + rng.uniform(0.1, 1.5, N)
    low   = close - rng.uniform(0.1, 1.5, N)
    low   = np.minimum(low, close - 0.01)
    df = pd.DataFrame({
        "open":   close - rng.uniform(0, 0.5, N),
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": rng.integers(1_000, 50_000, N).astype(float),
    }, index=pd.DatetimeIndex(idx))

    errors = 0
    passed = 0

    # ── Trend ─────────────────────────────────────────────────────────────
    section("TREND")
    for p in (9, 21, 50, 100, 200):
        try:
            df.ta.ema(length=p, append=True)
            v = df[f"EMA_{p}"].iloc[-1]
            assert not np.isnan(v), "NaN"
            ok(f"EMA_{p} = {v:.4f}")
            passed += 1
        except Exception as e:
            fail(f"EMA_{p}", str(e)); errors += 1

    try:
        df.ta.wma(length=14, append=True)
        v = df["WMA_14"].iloc[-1]
        assert not np.isnan(v)
        ok(f"WMA_14 = {v:.4f}")
        passed += 1
    except Exception as e:
        fail("WMA_14", str(e)); errors += 1

    try:
        df.ta.vwap(append=True)
        v = df["VWAP_D"].iloc[-1]
        assert not np.isnan(v) and v > 0
        ok(f"VWAP_D = {v:.4f}")
        passed += 1
    except Exception as e:
        fail("VWAP_D", str(e)); errors += 1

    try:
        df.ta.vwap_bands(append=True)
        for col in ("VWAP_D_upper_1", "VWAP_D_lower_1", "VWAP_D_upper_2", "VWAP_D_lower_2"):
            v = df[col].iloc[-1]
            assert not np.isnan(v), f"{col} NaN"
        ok("VWAP Bands (±1σ, ±2σ) OK")
        passed += 1
    except Exception as e:
        fail("VWAP Bands", str(e)); errors += 1

    try:
        df.ta.supertrend(length=7, multiplier=3.0, append=True)
        st  = df["SUPERT_7_3.0"].iloc[-1]
        std = df["SUPERTd_7_3.0"].iloc[-1]
        assert not np.isnan(st)
        assert std in (1.0, -1.0)
        ok(f"SuperTrend = {st:.4f}, dir = {int(std):+d}")
        passed += 1
    except Exception as e:
        fail("SuperTrend", str(e)); errors += 1

    # ── Momentum ──────────────────────────────────────────────────────────
    section("MOMENTUM")
    try:
        df.ta.rsi(length=14, append=True)
        v = df["RSI_14"].iloc[-1]
        assert 0 <= v <= 100
        ok(f"RSI_14 = {v:.2f}")
        passed += 1
    except Exception as e:
        fail("RSI_14", str(e)); errors += 1

    try:
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        for col in ("MACD_12_26_9", "MACDs_12_26_9", "MACDh_12_26_9"):
            v = df[col].iloc[-1]
            assert not np.isnan(v), f"{col} NaN"
        ok(f"MACD = {df['MACD_12_26_9'].iloc[-1]:.5f} | sig = {df['MACDs_12_26_9'].iloc[-1]:.5f}")
        passed += 1
    except Exception as e:
        fail("MACD", str(e)); errors += 1

    try:
        df.ta.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)
        k = df["STOCHRSIk_14_14_3_3"].iloc[-1]
        d = df["STOCHRSId_14_14_3_3"].iloc[-1]
        assert 0 <= k <= 100 and 0 <= d <= 100
        ok(f"StochRSI %K={k:.2f}  %D={d:.2f}")
        passed += 1
    except Exception as e:
        fail("StochRSI", str(e)); errors += 1

    # ── Volatility ────────────────────────────────────────────────────────
    section("VOLATILITY")
    try:
        df.ta.atr(length=14, append=True)
        v = df["ATRr_14"].iloc[-1]
        assert v > 0
        ok(f"ATRr_14 = {v:.5f}")
        passed += 1
    except Exception as e:
        fail("ATRr_14", str(e)); errors += 1

    try:
        df.ta.atr_pct(length=14, append=True)
        v = df["ATRr_14_pct"].iloc[-1]
        assert 0 < v < 100
        ok(f"ATRr_14_pct = {v:.4f}%")
        passed += 1
    except Exception as e:
        fail("ATRr_14_pct", str(e)); errors += 1

    try:
        df.ta.bbands(length=20, std=2.0, append=True)
        cols = ("BBU_20_2.0", "BBM_20_2.0", "BBL_20_2.0", "BBB_20_2.0", "BBP_20_2.0")
        for col in cols:
            assert col in df.columns and not np.isnan(df[col].iloc[-1])
        ok(f"BB: upper={df['BBU_20_2.0'].iloc[-1]:.4f} mid={df['BBM_20_2.0'].iloc[-1]:.4f} lower={df['BBL_20_2.0'].iloc[-1]:.4f}")
        passed += 1
    except Exception as e:
        fail("Bollinger Bands", str(e)); errors += 1

    try:
        df.ta.keltner(length=20, scalar=2.0, append=True)
        for col in ("KCUe_20_2", "KCBe_20_2", "KCLe_20_2"):
            assert col in df.columns and not np.isnan(df[col].iloc[-1])
        ok(f"Keltner: upper={df['KCUe_20_2'].iloc[-1]:.4f}")
        passed += 1
    except Exception as e:
        fail("Keltner Channel", str(e)); errors += 1

    try:
        df.ta.squeeze(append=True)
        col = "SQZ_20_2.0_20_1.5"
        v   = df[col].iloc[-1]
        assert v in (0.0, 1.0)
        ok(f"Squeeze = {'AKTIF 🔴' if v == 1 else 'tidak aktif'}")
        passed += 1
    except Exception as e:
        fail("Squeeze", str(e)); errors += 1

    # ── Strength ──────────────────────────────────────────────────────────
    section("STRENGTH")
    try:
        df.ta.adx(length=14, append=True)
        adx = df["ADX_14"].iloc[-1]
        dmp = df["DMP_14"].iloc[-1]
        dmn = df["DMN_14"].iloc[-1]
        assert all(0 <= v <= 100 for v in (adx, dmp, dmn))
        ok(f"ADX={adx:.2f}  +DI={dmp:.2f}  -DI={dmn:.2f}")
        passed += 1
    except Exception as e:
        fail("ADX/DI", str(e)); errors += 1

    try:
        df.ta.obv(append=True)
        v = df["OBV"].iloc[-1]
        assert not np.isnan(v)
        ok(f"OBV = {v:,.0f}")
        passed += 1
    except Exception as e:
        fail("OBV", str(e)); errors += 1

    try:
        df.ta.mfi(length=14, append=True)
        v = df["MFI_14"].iloc[-1]
        assert 0 <= v <= 100
        ok(f"MFI_14 = {v:.2f}")
        passed += 1
    except Exception as e:
        fail("MFI_14", str(e)); errors += 1

    # ── Regression: edge-case yang sebelumnya BUG (v2 fix) ───────────────────
    section("REGRESSION — edge-case fix v2")

    try:
        n_up = 20
        close_up = np.linspace(100.0, 130.0, n_up)  # naik monoton, 0 candle merah
        df_up = pd.DataFrame({
            "open": close_up - 0.1, "high": close_up + 0.2,
            "low": close_up - 0.2, "close": close_up,
            "volume": np.full(n_up, 1000.0),
        })
        df_up.ta.rsi(length=14, append=True)
        v = df_up["RSI_14"].iloc[-1]
        assert v >= 99.0, f"RSI pure-uptrend harus ~100, didapat {v}"
        ok(f"RSI pure-uptrend (0 candle merah) = {v:.2f} (harus ≈100, BUKAN 50)")
        passed += 1
    except Exception as e:
        fail("RSI pure-uptrend edge-case", str(e)); errors += 1

    try:
        n_flat = 20
        close_flat = np.full(n_flat, 100.0)  # harga benar-benar flat
        df_flat = pd.DataFrame({
            "open": close_flat, "high": close_flat,
            "low": close_flat, "close": close_flat,
            "volume": np.full(n_flat, 1000.0),
        })
        df_flat.ta.rsi(length=14, append=True)
        v = df_flat["RSI_14"].iloc[-1]
        assert abs(v - 50.0) < 1e-6, f"RSI flat harus tepat 50, didapat {v}"
        ok(f"RSI flat (tidak ada gerakan harga) = {v:.2f} (harus tepat 50)")
        passed += 1
    except Exception as e:
        fail("RSI flat edge-case", str(e)); errors += 1

    try:
        n_up = 20
        close_up = np.linspace(100.0, 130.0, n_up)
        df_up_mfi = pd.DataFrame({
            "open": close_up - 0.1, "high": close_up + 0.2,
            "low": close_up - 0.2, "close": close_up,
            "volume": np.full(n_up, 1000.0),
        })
        df_up_mfi.ta.mfi(length=14, append=True)
        v = df_up_mfi["MFI_14"].iloc[-1]
        assert v >= 99.0, f"MFI tanpa outflow harus ~100, didapat {v}"
        ok(f"MFI tanpa outflow sama sekali = {v:.2f} (harus ≈100, BUKAN 50)")
        passed += 1
    except Exception as e:
        fail("MFI tanpa-outflow edge-case", str(e)); errors += 1

    try:
        # WMA vectorized (sliding_window_view) harus identik dengan definisi
        # naif (dot product manual per window) — cross-check hasil optimisasi.
        length_w = 14
        w = np.arange(1, length_w + 1, dtype=float)
        close_vals = df["close"].to_numpy(dtype=float)
        naive = np.full(len(close_vals), np.nan)
        for i in range(length_w - 1, len(close_vals)):
            window = close_vals[i - length_w + 1: i + 1]
            naive[i] = np.dot(window, w) / w.sum()
        df.ta.wma(length=length_w, append=True)
        vectorized = df[f"WMA_{length_w}"].to_numpy(dtype=float)
        valid = ~np.isnan(naive)
        assert np.allclose(naive[valid], vectorized[valid], rtol=1e-9), "WMA vectorized ≠ naive"
        ok(f"WMA_{length_w} vectorized cocok 100% dengan definisi naif ({valid.sum()} titik dicek)")
        passed += 1
    except Exception as e:
        fail("WMA vectorized vs naive", str(e)); errors += 1

    # ── compute_all ───────────────────────────────────────────────────────
    section("COMPUTE_ALL")
    df2 = pd.DataFrame({
        "open":   close - rng.uniform(0, 0.5, N),
        "high":   high, "low": low, "close": close,
        "volume": rng.integers(1_000, 50_000, N).astype(float),
    }, index=pd.DatetimeIndex(idx))
    try:
        df2.ta.compute_all()
        expected = [
            "EMA_9","EMA_21","EMA_50","EMA_100","EMA_200",
            "RSI_14","ATRr_14","ATRr_14_pct",
            "MACD_12_26_9","MACDs_12_26_9","MACDh_12_26_9",
            "STOCHRSIk_14_14_3_3","STOCHRSId_14_14_3_3",
            "ADX_14","DMP_14","DMN_14",
            "OBV","MFI_14",
            "BBU_20_2.0","BBM_20_2.0","BBL_20_2.0","BBB_20_2.0","BBP_20_2.0",
            "KCUe_20_2","KCBe_20_2","KCLe_20_2",
            "SQZ_20_2.0_20_1.5",
            "SUPERT_7_3.0","SUPERTd_7_3.0",
            "VWAP_D","VWAP_D_upper_1","VWAP_D_lower_1","VWAP_D_upper_2","VWAP_D_lower_2",
        ]
        missing = [c for c in expected if c not in df2.columns]
        if missing:
            fail(f"compute_all: {len(missing)} kolom hilang: {missing}"); errors += 1
        else:
            ok(f"compute_all: {len(expected)} kolom berhasil — semua ada ✓")
            passed += 1
    except Exception as e:
        fail("compute_all", traceback.format_exc()); errors += 1

    # ── Summary ───────────────────────────────────────────────────────────
    total = passed + errors
    print(f"\n{'─'*50}")
    if errors == 0:
        print(f"{GREEN}{BOLD}  SEMUA {passed}/{total} TEST PASSED ✓{RESET}")
        print(f"  ta_compat superpower — siap produksi\n")
        sys.exit(0)
    else:
        print(f"{RED}{BOLD}  {errors}/{total} TEST GAGAL ✗{RESET}")
        sys.exit(1)
