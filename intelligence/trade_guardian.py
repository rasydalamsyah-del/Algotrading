"""
intelligence/trade_guardian.py
AlgoTrader Pro v7.0 — Adaptive Trade Guardian

Fase A : Composite Exit Score + Profit Zone Trailing (aktif, dipakai main.py)
Fase B : Pre-Exit Warning — ATGResult.warning_level DIHITUNG (0-4, dari 4 sinyal
         teknikal) tapi TIDAK DIBACA oleh caller manapun (dicek: main.py &
         simulate_test.py hanya baca new_sl/should_exit/exit_reason). Belum ada
         notifikasi pre-exit warning yang jalan. "Regime-Aware Duration" yang
         sebelumnya disebut di sini TIDAK punya implementasi/field sama sekali
         (tidak ada tracking durasi posisi per regime) — docstring lama
         overclaim, dikoreksi supaya jujur (bukan bug logika, cuma dokumentasi
         yang tidak akurat).
         [CATATAN UNTUK AUDIT main.py]: pertimbangkan wire warning_level ke
         notifikasi pre-exit warning saat >=3, dan cek bug terkait: panggilan
         notify_sl_tp_hit(trigger="take_profit", ...) di ATG exit block main.py
         (~baris 1946) HARDCODE trigger="take_profit" walau ATG bisa exit saat
         posisi rugi (profit_pct negatif tapi skor exit tetap >= threshold
         karena sinyal teknikal). Ini akan menampilkan notif "TAKE PROFIT HIT"
         yang salah/menyesatkan saat sebenarnya exit terjadi karena kerugian.

Dipanggil setiap siklus run_sl_tp_monitor.
Self-contained — tidak depend ke intelligence pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

log = logging.getLogger("trade_guardian")


# ─── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class ATGResult:
    should_exit:   bool            = False
    exit_reason:   str             = ""
    new_sl:        Optional[float] = None   # None = tidak ada update SL
    warning_level: int             = 0      # 0=aman 1=hati2 2=waspada 3=bahaya
    signals:       List[str]       = field(default_factory=list)


# ─── Indikator helpers (mandiri, tanpa pandas_ta) ────────────────────────────

def _rsi(close: np.ndarray, period: int = 14) -> float:
    """Wilder RSI — return nilai terakhir, default 50.0 jika data kurang."""
    if len(close) < period + 1:
        return 50.0
    d  = np.diff(close.astype(float))
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = float(np.mean(g[:period]))
    al = float(np.mean(l[:period]))
    for i in range(period, len(g)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return 100.0 if al == 0 else round(100.0 - 100.0 / (1.0 + ag / al), 2)


def _ema(close: np.ndarray, period: int) -> Optional[float]:
    """EMA standard — return nilai terakhir, None jika data kurang."""
    if len(close) < period:
        return None
    alpha = 2.0 / (period + 1)
    val   = float(np.mean(close[:period]))
    for p in close[period:]:
        val = float(p) * alpha + val * (1.0 - alpha)
    return round(val, 8)


def _supertrend_dir(df, period: int = 7) -> int:
    """Adaptive supertrend: 1=bull, -1=bear, 0=tidak diketahui."""
    try:
        hi = df["high"].values.astype(float)
        lo = df["low"].values.astype(float)
        cl = df["close"].values.astype(float)
        n  = len(cl)
        if n < period + 1:
            return 0
        tr    = np.zeros(n)
        tr[0] = hi[0] - lo[0]
        for i in range(1, n):
            tr[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        atr       = np.zeros(n)
        atr[period-1] = float(np.mean(tr[:period]))
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
        ac   = atr[atr > 0]
        mult = 3.0
        if len(ac) >= 20:
            pct  = float(np.sum(ac < atr[-1]) / len(ac) * 100)
            mult = 2.5 if pct < 30 else (3.5 if pct > 70 else 3.0)
        hl2 = (hi + lo) / 2.0
        ub  = hl2 + mult * atr
        lb  = hl2 - mult * atr
        fub = np.zeros(n); flb = np.zeros(n); di = np.zeros(n, dtype=int)
        s   = period - 1
        fub[s] = ub[s]; flb[s] = lb[s]; di[s] = 1
        for i in range(s + 1, n):
            fub[i] = ub[i]  if (ub[i] < fub[i-1] or cl[i-1] > fub[i-1]) else fub[i-1]
            flb[i] = lb[i]  if (lb[i] > flb[i-1] or cl[i-1] < flb[i-1]) else flb[i-1]
            if di[i-1] == -1:
                di[i] = 1  if cl[i] > fub[i] else -1
            else:
                di[i] = -1 if cl[i] < flb[i] else 1
        return int(di[-1])
    except Exception as exc:
        log.debug("ATG supertrend error: %s", exc)
        return 0


# ─── Layer 2 — Profit Zone Trailing ─────────────────────────────────────────

def get_profit_zone_sl(
    entry_price:   float,
    highest_price: float,
    current_sl:    Optional[float] = None,
) -> Optional[float]:
    """
    SL baru berdasarkan profit zone. Return None jika tidak ada improvement.

    Zone 0  peak < +1%  : tidak ada trailing
    Zone 1  peak +1–3%  : lindungi breakeven (+0.1%)
    Zone 2  peak +3–5%  : kunci 50% dari peak profit
    Zone 3  peak +5–8%  : kunci 60% dari peak profit
    Zone 4  peak > +8%  : kunci 70% dari peak profit
    """
    if entry_price <= 0 or highest_price <= entry_price:
        return None
    peak_pct = (highest_price - entry_price) / entry_price * 100.0

    if   peak_pct <  1.0: zone_sl = None
    elif peak_pct <  3.0: zone_sl = round(entry_price * 1.0010, 8)
    elif peak_pct <  5.0: zone_sl = round(entry_price * (1 + peak_pct * 0.50 / 100), 8)
    elif peak_pct <  8.0: zone_sl = round(entry_price * (1 + peak_pct * 0.60 / 100), 8)
    else:                 zone_sl = round(entry_price * (1 + peak_pct * 0.70 / 100), 8)

    if zone_sl is None:
        return None
    if current_sl is not None and zone_sl <= current_sl:
        return None
    return zone_sl


# ─── Layer 1 — Composite Exit Score ─────────────────────────────────────────

def _exit_score(
    profit_pct: float,
    rsi:        float,
    st_dir:     int,
    ema9:       Optional[float],
    ema21:      Optional[float],
    vol_ratio:  float,
    regime:     str,
):
    """Return (score, signals, threshold)."""
    score: float     = 0.0
    signals: List[str] = []

    # Bobot dinamis per regime — otomatis sesuai kondisi pasar
    _W = {
        "trending_bull":      {"st": 40, "rsi": 25, "ema": 20, "vol": 15, "thresh": 40},
        "trending_bear":      {"st": 45, "rsi": 25, "ema": 20, "vol": 10, "thresh": 35},
        "volatile_expansion": {"st": 25, "rsi": 15, "ema": 20, "vol": 40, "thresh": 55},
        "ranging":            {"st": 20, "rsi": 35, "ema": 30, "vol": 15, "thresh": 45},
        "undefined":          {"st": 30, "rsi": 25, "ema": 25, "vol": 20, "thresh": 40},
    }
    w = _W.get(regime, _W["undefined"])

    # Signal 1 — Supertrend bearish
    if st_dir == -1 and profit_pct > -3.0:
        score += float(w["st"]); signals.append("ST_BEAR")

    # Signal 2 — RSI melemah
    rsi_thresh_weak = 35 if regime != "volatile_expansion" else 30
    rsi_thresh_soft = 45 if regime != "volatile_expansion" else 38
    if   rsi < rsi_thresh_weak: score += float(w["rsi"]);       signals.append(f"RSI_WEAK({rsi:.0f})")
    elif rsi < rsi_thresh_soft: score += float(w["rsi"]) * 0.5; signals.append(f"RSI_SOFT({rsi:.0f})")

    # Signal 3 — EMA cross down
    if ema9 is not None and ema21 is not None and ema9 < ema21:
        score += float(w["ema"]); signals.append("EMA_XDOWN")

    # Signal 4 — Volume spike saat rugi
    if profit_pct < 0.5:
        if   vol_ratio > 2.0: score += float(w["vol"]);       signals.append(f"VOL_SPIKE({vol_ratio:.1f}x)")
        elif vol_ratio > 1.5: score += float(w["vol"]) * 0.5; signals.append(f"VOL_ELEV({vol_ratio:.1f}x)")

    threshold = float(w["thresh"])
    return score, signals, threshold


# ─── Main entry point ────────────────────────────────────────────────────────

def check_atg(
    entry_price:   float,
    current_price: float,
    highest_price: float,
    current_sl:    Optional[float],
    df,                               # pd.DataFrame | None — min 15 baris
    symbol:        str = "",
    regime:        str = "trending_bull",
) -> ATGResult:
    """
    Adaptive Trade Guardian — panggil setiap siklus monitoring.
    df: OHLCV DataFrame (open/high/low/close/volume), rekomendasi 50 candle 1h.
    """
    result = ATGResult()
    if entry_price <= 0 or current_price <= 0:
        return result

    profit_pct   = (current_price - entry_price) / entry_price * 100.0
    effective_hi = max(highest_price, current_price)

    # ── Layer 2: Profit Zone Trailing ─────────────────────────────────────
    zone_sl = get_profit_zone_sl(entry_price, effective_hi, current_sl)
    if zone_sl is not None:
        result.new_sl = zone_sl
        peak_pct = (effective_hi - entry_price) / entry_price * 100.0
        log.debug(
            "ATG ProfitZone [%s] profit=%.2f%% peak=%.2f%% zone_sl=%.8f",
            symbol, profit_pct, peak_pct, zone_sl,
        )

    # ── Layer 1 & 4: Composite Exit + Pre-Exit Warning ─────────────────────
    if df is None or len(df) < 15:
        return result

    try:
        cl  = np.array(df["close"].values,  dtype=float)
        vol = np.array(df["volume"].values, dtype=float) \
              if "volume" in df.columns else np.ones(len(cl))

        rsi      = _rsi(cl)
        st_dir   = _supertrend_dir(df)
        ema9     = _ema(cl, 9)
        ema21    = _ema(cl, 21)
        vol_avg  = float(np.mean(vol[-15:])) if len(vol) >= 15 else (float(np.mean(vol)) if len(vol) else 1.0)
        vol_ratio = float(np.mean(vol[-3:])) / vol_avg if vol_avg > 0 and len(vol) >= 3 else 1.0

        score, signals, threshold = _exit_score(
            profit_pct, rsi, st_dir, ema9, ema21, vol_ratio, regime,
        )
        result.signals = signals

        # Warning level (independen dari exit)
        w = 0
        if st_dir == -1:                                              w += 1
        if rsi < 45:                                                  w += 1
        if ema9 is not None and ema21 is not None and ema9 < ema21:  w += 1
        if vol_ratio > 2.0 and profit_pct < 0.5:                     w += 1
        result.warning_level = w

        if score >= threshold:
            result.should_exit = True
            result.exit_reason = (
                f"ATG_EXIT(score={score:.0f}/{threshold:.0f}"
                f"|pnl={profit_pct:+.2f}%|{','.join(signals)})"
            )
            log.info(
                "ATG EXIT [%s] score=%.0f/%.0f profit=%.2f%% signals=%s",
                symbol, score, threshold, profit_pct, signals,
            )

    except Exception as exc:
        log.debug("ATG indicator error [%s]: %s", symbol, exc)

    return result
