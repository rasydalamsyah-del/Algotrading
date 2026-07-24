"""
strategy.py (spot) — VolumetricBreakoutStrategy khusus spot trading

Extend engine.strategy_base.VolumetricBreakoutStrategyBase yang menangani
seluruh scoring/tracking pipeline market-agnostic (side-aware). Di sini cuma
tersisa "legacy pipeline" yang genuinely spot-only: generate_signals()
(dipanggil dari spot/main_spot.py::run_strategy_loop(), TIDAK dipakai
future/) dan helper-nya (_detect_exit_mode, _compute_sl_tp_quick/wave,
_compute_confidence) -- semuanya hardcode SignalType.BUY/formula long-only,
sengaja TIDAK diekstrak ke engine/ karena genuinely spot-specific.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
import time as _time
import aiohttp

from engine.strategy_base import (
    VolumetricBreakoutStrategyBase, BaseStrategy, PositionTracker,
    SignalType, SignalEvent, ExitMode, _utcnow, _UNIVERSAL_DEFAULTS,
    _TA_AVAILABLE, pd,
)
from engine.profiles.base_profile import CoinProfile, AdaptiveParams, StrategyProfile
from engine.profiles.registry import get_coin_profile
from engine.constants import APP_VERSION

log = logging.getLogger("strategy")


class VolumetricBreakoutStrategy(VolumetricBreakoutStrategyBase):

    def _detect_exit_mode(
        self,
        close:     float,
        atr:       float,
        vol_ratio: float,
        p:         Dict,
    ) -> ExitMode:
        atr_pct = (atr / close * 100) if close > 0 else 0

        if vol_ratio >= p["volume_spike_threshold"]:
            log.info(
                "ExitMode: RIDE_THE_WAVE (vol spike %.2fx ≥ %.1fx)",
                vol_ratio, p["volume_spike_threshold"],
            )
            return ExitMode.RIDE_THE_WAVE

        if atr_pct >= p["atr_pct_threshold"]:
            log.info(
                "ExitMode: RIDE_THE_WAVE (ATR=%.3f%% ≥ threshold=%.1f%%)",
                atr_pct, p["atr_pct_threshold"],
            )
            return ExitMode.RIDE_THE_WAVE

        log.info(
            "ExitMode: QUICK_PROFIT (ATR=%.3f%% < threshold=%.1f%%)",
            atr_pct, p["atr_pct_threshold"],
        )
        return ExitMode.QUICK_PROFIT

    def _compute_sl_tp_quick(
        self, close: float, atr: float, p: Dict
    ) -> Tuple[float, float]:
        sl_from_pct = close * (p["quick_sl_pct"] / 100)
        sl_from_atr = atr * p["atr_sl_mult"] if atr > 0 else 0.0
        sl_dist     = max(sl_from_pct, sl_from_atr)
        sl          = round(close - sl_dist, 8)

        tp_from_pct = close * (p["quick_tp_pct"] / 100)
        tp_from_atr = atr * p["atr_tp_mult"] if atr > 0 else 0.0
        tp          = round(close + max(tp_from_pct, tp_from_atr), 8)

        log.debug(
            "Quick SL: pct=%.6f atr=%.6f → dist=%.6f → SL=%.6f | "
            "Quick TP: pct=%.6f atr=%.6f → TP=%.6f",
            sl_from_pct, sl_from_atr, sl_dist, sl,
            tp_from_pct, tp_from_atr, tp,
        )
        return sl, tp

    def _compute_sl_tp_wave(
        self, close: float, atr: float, p: Dict
    ) -> Tuple[float, float]:
        # [BUG-FIX -- HARDENING, ditemukan lewat eksperimen edge case putaran 2]
        # Sebelumnya fungsi ini TIDAK punya guard atr<=0, beda dgn
        # _compute_sl_tp_quick yang sudah aman (`if atr > 0 else 0.0`).
        # Dibuktikan: atr=0 -> sl=tp=close (SL/TP order invalid, sama dgn
        # harga entry); atr negatif (data corrupt/bug hulu) -> SL malah DI
        # ATAS close dan TP malah DI BAWAH close, terbalik total dari makna
        # SL/TP untuk posisi long. Saat ini SATU-SATUNYA caller
        # (_generate_signals_legacy) sudah menjaga lewat `if atr <= 0: return`
        # lebih awal sebelum sampai ke fungsi ini -- jadi TIDAK exploitable
        # di jalur produksi saat ini -- tapi fungsi ini sendiri rapuh (silent
        # wrong output, bukan crash) kalau suatu saat dipanggil dari jalur
        # lain tanpa guard yang identik. Root-cause fix taruh di fungsi ini
        # sendiri (bukan cuma andalkan disiplin caller), konsisten dgn pola
        # defensif _compute_sl_tp_quick.
        if atr is None or atr <= 0:
            log.warning(
                "_compute_sl_tp_wave dipanggil dgn atr tidak valid (%s) — "
                "fallback ke quick_sl_pct/quick_tp_pct dari profile.", atr,
            )
            sl = round(close * (1 - p.get("quick_sl_pct", 1.20) / 100), 8)
            tp = round(close * (1 + p.get("quick_tp_pct", 1.75) / 100), 8)
            return sl, tp

        sl = round(close - atr * p["atr_sl_mult"], 8)
        tp = round(close + atr * p["atr_tp_mult"], 8)
        return sl, tp

    async def generate_signals(
        self,
        symbol: str,
        df:     pd.DataFrame,
    ) -> List[SignalEvent]:
        signals: List[SignalEvent] = []

        try:
            if self._pipeline_ready:
                signals = await self._generate_signals_v7(symbol, df)
            else:
                signals = await self._generate_signals_legacy(symbol, df)
        except Exception as exc:
            log.error(
                "generate_signals error [%s]: %s",
                symbol, exc, exc_info=True,
            )

        return signals

    async def _generate_signals_v7(
        self, symbol: str, df: pd.DataFrame
    ) -> List[SignalEvent]:
        signals: List[SignalEvent] = []

        if len(df) < self.params.get("min_candles", 60):
            return signals

        df = self.enrich(df.copy())
        if df.empty or len(df) < 5:
            return signals

        if not self._validate_cols(df, self._REQUIRED_COLS, ctx=symbol):
            return signals

        with self._lock:
            tracker_ref = self._pos_trackers.get(symbol)
            in_position = self._in_position.get(symbol, False)
            # [BUG-FIX] Sebelumnya: variabel `pending` diisi di sini tapi
            # tidak pernah dipakai — fungsi ini (_generate_signals_v7) HANYA
            # menghasilkan sinyal CLOSE_LONG untuk posisi yang sudah terbuka
            # (entry baru dibuat lewat get_scored_signal, dipanggil terpisah
            # dari main.py). _pending_entry juga tidak relevan di jalur
            # pipeline ini — proteksi anti-duplicate-entry yang setara sudah
            # ada di main.py lewat _pipeline_active/_queued_symbols. Variabel
            # dead code dihapus untuk hindari kebingungan audit berikutnya.

        if tracker_ref:
            tracker_ref.increment_hold()

        if in_position:
            bar  = df.iloc[-2]
            prev = df.iloc[-3] if len(df) >= 3 else bar

            close = float(bar["close"])
            rsi   = float(bar[self._COL_RSI])
            ema9  = float(bar[self._COL_EMA9])
            ema21 = float(bar[self._COL_EMA21])
            atr   = float(bar[self._COL_ATR])

            prev_ema9_v  = (
                float(prev[self._COL_EMA9])
                if self._COL_EMA9 in prev.index else ema9
            )
            prev_ema21_v = (
                float(prev[self._COL_EMA21])
                if self._COL_EMA21 in prev.index else ema21
            )

            exit_mode_cur = (
                tracker_ref.exit_mode if tracker_ref else ExitMode.QUICK_PROFIT
            )
            reason = None

            if tracker_ref and tracker_ref.is_overtime():
                elapsed_h = (
                    (_utcnow() - tracker_ref.entry_time).total_seconds() / 3600
                )
                profit_pct = (
                    (close - tracker_ref.entry_price) / tracker_ref.entry_price * 100
                    if tracker_ref else 0.0
                )
                reason = (
                    f"MaxHoldExit(elapsed={elapsed_h:.1f}h,"
                    f"max={tracker_ref.max_hold_seconds/3600:.1f}h,"
                    f"profit={profit_pct:+.2f}%)"
                )

            elif exit_mode_cur == ExitMode.QUICK_PROFIT:
                p = self._resolve_params(symbol, close, atr, 1.0, rsi)
                cond_rsi_ob    = rsi > p["rsi_max"]
                cond_ema_cross = (prev_ema9_v > prev_ema21_v) and (ema9 < ema21)
                cond_below_ema = close < ema21

                if cond_rsi_ob:
                    reason = f"QP_RSI_Overbought(rsi={rsi:.1f}>{p['rsi_max']:.0f})"
                elif cond_ema_cross:
                    reason = "QP_EMA_BearishCross(ema9 crossed below ema21)"
                elif cond_below_ema:
                    reason = (
                        f"QP_PriceBelowEMA21(close={close:.4f}"
                        f"<ema21={ema21:.4f})"
                    )

            else:
                with self._lock:
                    if tracker_ref and close > tracker_ref.highest_price:
                        tracker_ref.highest_price = close

                trailing_reason = self.check_trailing_exit(symbol, close)

                if trailing_reason:
                    reason = trailing_reason
                elif rsi < 35:
                    reason = f"RTW_RSI_Weak(rsi={rsi:.1f}<35)"
                elif (
                    (prev_ema9_v > prev_ema21_v)
                    and (ema9 < ema21)
                    and rsi < 50
                ):
                    reason = f"RTW_EMA_Reversal+RSI(rsi={rsi:.1f})"

            if reason:
                profit_pct = (
                    (close - tracker_ref.entry_price) / tracker_ref.entry_price * 100
                    if tracker_ref else 0.0
                )
                hold_time = (
                    (_utcnow() - tracker_ref.entry_time).total_seconds() / 3600
                    if tracker_ref else 0.0
                )

                sig = SignalEvent(
                    symbol=symbol,
                    signal_type=SignalType.CLOSE_LONG,
                    price=close,
                    timestamp=_utcnow(),
                    strategy=self.name,
                    confidence=1.0,
                    metadata={
                        "exit_reason":     reason,
                        "exit_mode":       exit_mode_cur.value,
                        "profit_pct":      round(profit_pct, 4),
                        "hold_hours":      round(hold_time, 2),
                        "candles_held":    tracker_ref.candles_held if tracker_ref else 0,
                        "rsi":             round(rsi, 2),
                        "ema9":            round(ema9, 8),
                        "ema21":           round(ema21, 8),
                        "atr":             round(atr, 8),
                        "highest_price":   tracker_ref.highest_price if tracker_ref else close,
                        "trailing_active": tracker_ref.trailing_active if tracker_ref else False,
                        "coin_profile":    tracker_ref.profile_name if tracker_ref else "unknown",
                        "entry_score":     tracker_ref.entry_score if tracker_ref else 0.0,
                        "entry_regime":    tracker_ref.entry_regime if tracker_ref else "unknown",
                        "strategy_version": f"v{APP_VERSION}",
                    },
                )
                signals.append(sig)

                log.info(
                    "CLOSE_LONG [%s] @ %.6f | reason=%s mode=%s "
                    "profit=%+.2f%% hold=%.1fh",
                    symbol, close, reason, exit_mode_cur.value,
                    profit_pct, hold_time,
                )

        return signals

    async def _generate_signals_legacy(
        self, symbol: str, df: pd.DataFrame
    ) -> List[SignalEvent]:
        signals: List[SignalEvent] = []

        try:
            if len(df) < self.params.get("min_candles", 60):
                return signals

            df = self.enrich(df.copy())
            if df.empty or len(df) < 5:
                return signals

            if not self._validate_cols(df, self._REQUIRED_COLS, ctx=symbol):
                return signals

            lb = self.params.get("lookback", _UNIVERSAL_DEFAULTS["lookback"])
            df["_resistance"] = df["close"].shift(1).rolling(lb).max()
            df["_vol_ma"]     = df["volume"].rolling(20).mean()

            if "quote_volume" in df.columns:
                df["_qvol_ma"] = df["quote_volume"].rolling(20).mean()

            if len(df) < 3:
                return signals

            bar  = df.iloc[-2]
            prev = df.iloc[-3]

            close = float(bar["close"])
            atr   = float(bar[self._COL_ATR])
            rsi   = float(bar[self._COL_RSI])
            ema9  = float(bar[self._COL_EMA9])
            ema21 = float(bar[self._COL_EMA21])
            ema50 = float(bar[self._COL_EMA50])

            resist = (
                float(bar["_resistance"])
                if pd.notna(bar.get("_resistance"))
                else close
            )
            vol    = float(bar["volume"])
            vol_ma = float(bar["_vol_ma"]) if pd.notna(bar.get("_vol_ma")) else 1.0

            if self.params.get("use_quote_volume") and "_qvol_ma" in df.columns:
                qv    = bar.get("quote_volume")
                qv_ma = bar.get("_qvol_ma")
                if (
                    qv is not None and qv_ma is not None
                    and pd.notna(qv) and pd.notna(qv_ma)
                    and float(qv_ma) > 0
                ):
                    vol    = float(qv)
                    vol_ma = float(qv_ma)

            vol_ratio  = vol / vol_ma if vol_ma > 0 else 0.0
            prev_ema9  = (
                float(prev[self._COL_EMA9])
                if self._COL_EMA9 in prev.index else ema9
            )
            prev_ema21 = (
                float(prev[self._COL_EMA21])
                if self._COL_EMA21 in prev.index else ema21
            )

            p  = self._resolve_params(symbol, close, atr, vol_ratio, rsi)
            tf = self.get_symbol_timeframe(symbol)
            vwap = None
            if tf not in ("1d", "3d", "1w"):
                vwap = self._get_vwap(bar)

            with self._lock:
                tracker_ref = self._pos_trackers.get(symbol)
                in_position = self._in_position.get(symbol, False)
                pending     = symbol in self._pending_entry

            if tracker_ref:
                tracker_ref.increment_hold()

            if in_position or pending:
                if pending and not in_position:
                    log.debug("[%s] Entry pending — skip cycle ini.", symbol)
                pass

            if not in_position and not pending:
                if atr <= 0:
                    log.debug("[%s] ATR=0, skip entry.", symbol)
                    return signals

                min_dist           = close * (p["min_breakout_pct"] / 100)
                breakout_dist      = close - resist if resist > 0 else 0.0
                trigger_a_breakout = breakout_dist >= min_dist
                trigger_a_volume   = vol_ratio >= p["volume_multiplier"]
                trigger_a          = trigger_a_breakout and trigger_a_volume

                golden_cross  = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
                trigger_b_rsi = rsi > p["rsi_golden_cross_min"]
                trigger_b     = golden_cross and trigger_b_rsi

                cond_trend    = ema9 > ema21 > ema50
                cond_momentum = p["rsi_min"] <= rsi <= p["rsi_max"]
                cond_vwap     = (close > vwap) if vwap is not None else True

                sentiment_score = 0.0
                if p.get("sentiment_enabled", True):
                    try:
                        sentiment_score = await asyncio.wait_for(
                            check_market_sentiment(symbol), timeout=3.0
                        )
                    except Exception:
                        sentiment_score = 0.0

                cond_sentiment = sentiment_score >= -0.2
                if not cond_sentiment:
                    log.info(
                        "BUY diblokir sentimen negatif: %s score=%.3f",
                        symbol, sentiment_score,
                    )
                    return signals

                entry_ok = (
                    (trigger_a or trigger_b)
                    and cond_trend
                    and cond_momentum
                    and cond_vwap
                )

                if entry_ok:
                    exit_mode = self._detect_exit_mode(close, atr, vol_ratio, p)

                    if exit_mode == ExitMode.QUICK_PROFIT:
                        sl, tp = self._compute_sl_tp_quick(close, atr, p)
                        exit_label = (
                            f"QUICK_PROFIT(TP={p['quick_tp_pct']:.1f}%,"
                            f"SL=max({p['quick_sl_pct']:.1f}%,"
                            f"ATR×{p['atr_sl_mult']:.1f}))"
                        )
                    else:
                        sl, tp = self._compute_sl_tp_wave(close, atr, p)
                        exit_label = (
                            f"RIDE_THE_WAVE("
                            f"trailing_act={p['trailing_activation_pct']:.1f}%,"
                            f"gap={p['trailing_gap_pct']:.1f}%)"
                        )

                    if sl <= 0 or sl >= close:
                        log.warning(
                            "[%s] SL invalid=%.6f (close=%.6f) — skip",
                            symbol, sl, close,
                        )
                        return signals
                    if tp <= close:
                        log.warning(
                            "[%s] TP invalid=%.6f (close=%.6f) — skip",
                            symbol, tp, close,
                        )
                        return signals

                    with self._lock:
                        if (
                            self._in_position.get(symbol, False)
                            or symbol in self._pending_entry
                        ):
                            log.debug(
                                "[%s] Entry dibatalkan — state berubah saat "
                                "komputasi (race condition dicegah).", symbol
                            )
                            return signals

                        self._in_position[symbol] = True
                        self._pending_entry.add(symbol)
                        self._last_entry_params[symbol] = {
                            "exit_mode": exit_mode,
                            "p":         p,
                        }

                    if trigger_a and trigger_b:
                        entry_trigger = "BOTH(Breakout+GoldenCross)"
                    elif trigger_a:
                        entry_trigger = "Breakout"
                    elif trigger_b:
                        entry_trigger = "GoldenCross"
                    else:
                        entry_trigger = "None"

                    # Profile otomatis — ditentukan kondisi indikator saat ini
                    from engine.profiles.registry import select_profile_from_indicators
                    _last_regime = getattr(self, '_last_regime', {})
                    _cur_regime  = _last_regime.get(symbol, 'trending_bull')
                    _adx_col = [c for c in df.columns if 'ADX' in c.upper() and 'DI' not in c.upper()]
                    _adx_val = float(df.iloc[-2][_adx_col[0]]) if _adx_col else 20.0
                    _atr_pct = atr / close * 100 if close > 0 else 0.5
                    _auto_profile = select_profile_from_indicators(
                        symbol       = symbol,
                        ind_momentum = rsi,
                        ind_trend    = float(ema9 / ema50 * 50) if ema50 > 0 else 50.0,
                        ema_stack_score = float((ema9 > ema21) * 33 + (ema21 > ema50) * 33 + (ema9 > ema50) * 34),
                        adx          = _adx_val,
                        rsi          = rsi,
                        atr_pct      = _atr_pct,
                        regime       = _cur_regime,
                    )
                    _base_profile = self._profiles.get(symbol)
                    _base_name    = _base_profile.profile.value if _base_profile else 'universal'
                    if _auto_profile != _base_name:
                        log.info(
                            "[%s] Profile otomatis: %s → %s (regime=%s adx=%.1f rsi=%.1f)",
                            symbol, _base_name, _auto_profile, _cur_regime, _adx_val, rsi,
                        )
                    from engine.profiles.registry import get_coin_profile as _gcp
                    profile     = _gcp(symbol, override_profile=_auto_profile)
                    profile_val = profile.profile.value if profile else "universal"
                    atr_pct     = atr / close * 100 if close > 0 else 0.0

                    confidence = self._compute_confidence(
                        close, resist, atr, rsi, vol_ratio,
                        ema9, ema50, trigger_a, trigger_b, p,
                    )

                    sig = SignalEvent(
                        symbol=symbol,
                        signal_type=SignalType.BUY,
                        price=close,
                        timestamp=_utcnow(),
                        strategy=self.name,
                        confidence=confidence,
                        stop_loss=sl,
                        take_profit=tp,
                        metadata={
                            "entry_trigger":       entry_trigger,
                            "golden_cross":        trigger_b,
                            "breakout_ok":         trigger_a_breakout,
                            "breakout_dist":       round(breakout_dist, 8),
                            "breakout_dist_pct":   round(
                                breakout_dist / close * 100, 4
                            ),
                            "min_breakout_pct":    p["min_breakout_pct"],
                            "resistance":          round(resist, 8),
                            "vol_ratio":           round(vol_ratio, 4),
                            "volume_ok":           trigger_a_volume,
                            "trend_ok":            cond_trend,
                            "momentum_ok":         cond_momentum,
                            "above_vwap":          cond_vwap,
                            "vwap":                round(vwap, 8) if vwap else None,
                            "sentiment_score":     round(sentiment_score, 4),
                            "rsi":                 round(rsi, 2),
                            "ema9":                round(ema9, 8),
                            "ema21":               round(ema21, 8),
                            "ema50":               round(ema50, 8),
                            "atr":                 round(atr, 8),
                            "atr_pct":             round(atr_pct, 4),
                            "exit_mode":           exit_mode.value,
                            "exit_label":          exit_label,
                            "sl_from_strategy":    sl,
                            "tp_from_strategy":    tp,
                            "atr_sl_mult":         p["atr_sl_mult"],
                            "atr_tp_mult":         p["atr_tp_mult"],
                            "coin_profile":        profile_val,
                            "adaptive_mode":       p.get("_adaptive_mode", "N/A"),
                            "atr_ratio":           round(p.get("_atr_ratio", 1.0), 2),
                            "rsi_min_used":        p["rsi_min"],
                            "rsi_max_used":        p["rsi_max"],
                            "vol_mult_used":       round(p["volume_multiplier"], 3),
                            "max_hold_seconds":    p.get("max_hold_seconds", 0),
                            "strategy_version":    f"v{APP_VERSION}",
                            "pipeline_mode":       "legacy",
                        },
                    )
                    signals.append(sig)

                    log.info(
                        "BUY [%s] (legacy) profile=%s trigger=%s mode=%s "
                        "@ %.6f conf=%.3f SL=%.6f TP=%.6f "
                        "vol_ratio=%.2fx RSI=%.1f ATR_pct=%.3f%%",
                        symbol, profile_val, entry_trigger, exit_mode.value,
                        close, confidence, sl, tp,
                        vol_ratio, rsi, atr_pct,
                    )

                else:
                    reasons = []
                    if not (trigger_a or trigger_b):
                        reasons.append(
                            f"NoTrigger(brk_ok={trigger_a_breakout},"
                            f"vol_ok={trigger_a_volume},gc={golden_cross})"
                        )
                    if not cond_trend:
                        reasons.append(
                            f"EMAStack(ema9={ema9:.4f},"
                            f"ema21={ema21:.4f},ema50={ema50:.4f})"
                        )
                    if not cond_momentum:
                        reasons.append(
                            f"RSI({rsi:.1f} not in "
                            f"[{p['rsi_min']},{p['rsi_max']}])"
                        )
                    if not cond_vwap:
                        reasons.append(
                            f"BelowVWAP(close={close:.4f},vwap={vwap:.4f})"
                        )
                    log.debug("[%s] No entry: %s", symbol, " | ".join(reasons))

            else:
                exit_mode_cur = (
                    tracker_ref.exit_mode if tracker_ref else ExitMode.QUICK_PROFIT
                )
                reason = None

                prev_ema9_v  = (
                    float(prev[self._COL_EMA9])
                    if self._COL_EMA9 in prev.index else ema9
                )
                prev_ema21_v = (
                    float(prev[self._COL_EMA21])
                    if self._COL_EMA21 in prev.index else ema21
                )

                if tracker_ref and tracker_ref.is_overtime():
                    elapsed_h = (
                        (_utcnow() - tracker_ref.entry_time).total_seconds() / 3600
                    )
                    profit_pct = (
                        (close - tracker_ref.entry_price) / tracker_ref.entry_price * 100
                        if tracker_ref else 0.0
                    )
                    reason = (
                        f"MaxHoldExit(elapsed={elapsed_h:.1f}h,"
                        f"max={tracker_ref.max_hold_seconds/3600:.1f}h,"
                        f"profit={profit_pct:+.2f}%)"
                    )

                elif exit_mode_cur == ExitMode.QUICK_PROFIT:
                    cond_rsi_ob    = rsi > p["rsi_max"]
                    cond_ema_cross = (prev_ema9_v > prev_ema21_v) and (ema9 < ema21)
                    cond_below_ema = close < ema21

                    if cond_rsi_ob:
                        reason = (
                            f"QP_RSI_Overbought(rsi={rsi:.1f}>{p['rsi_max']:.0f})"
                        )
                    elif cond_ema_cross:
                        reason = "QP_EMA_BearishCross(ema9 crossed below ema21)"
                    elif cond_below_ema:
                        reason = (
                            f"QP_PriceBelowEMA21(close={close:.4f}"
                            f"<ema21={ema21:.4f})"
                        )

                else:
                    with self._lock:
                        if tracker_ref and close > tracker_ref.highest_price:
                            tracker_ref.highest_price = close

                    trailing_reason = self.check_trailing_exit(symbol, close)

                    if trailing_reason:
                        reason = trailing_reason
                    elif rsi < 35:
                        reason = f"RTW_RSI_Weak(rsi={rsi:.1f}<35)"
                    elif (
                        (prev_ema9_v > prev_ema21_v)
                        and (ema9 < ema21)
                        and rsi < 50
                    ):
                        reason = f"RTW_EMA_Reversal+RSI(rsi={rsi:.1f})"

                if reason:
                    profit_pct = (
                        (close - tracker_ref.entry_price) / tracker_ref.entry_price * 100
                        if tracker_ref else 0.0
                    )
                    hold_time = (
                        (_utcnow() - tracker_ref.entry_time).total_seconds() / 3600
                        if tracker_ref else 0.0
                    )

                    sig = SignalEvent(
                        symbol=symbol,
                        signal_type=SignalType.CLOSE_LONG,
                        price=close,
                        timestamp=_utcnow(),
                        strategy=self.name,
                        confidence=1.0,
                        metadata={
                            "exit_reason":     reason,
                            "exit_mode":       exit_mode_cur.value,
                            "profit_pct":      round(profit_pct, 4),
                            "hold_hours":      round(hold_time, 2),
                            "candles_held":    tracker_ref.candles_held if tracker_ref else 0,
                            "rsi":             round(rsi, 2),
                            "ema9":            round(ema9, 8),
                            "ema21":           round(ema21, 8),
                            "atr":             round(atr, 8),
                            "highest_price":   tracker_ref.highest_price if tracker_ref else close,
                            "trailing_active": tracker_ref.trailing_active if tracker_ref else False,
                            "coin_profile":    tracker_ref.profile_name if tracker_ref else "unknown",
                            "strategy_version": f"v{APP_VERSION}",
                            "pipeline_mode":   "legacy",
                        },
                    )
                    signals.append(sig)

                    log.info(
                        "CLOSE_LONG [%s] @ %.6f | reason=%s mode=%s "
                        "profit=%+.2f%% hold=%.1fh",
                        symbol, close, reason, exit_mode_cur.value,
                        profit_pct, hold_time,
                    )

        except Exception as exc:
            log.error(
                "generate_signals_legacy error [%s]: %s",
                symbol, exc, exc_info=True,
            )

        return signals

    def _compute_confidence(
        self,
        close:     float,
        resist:    float,
        atr:       float,
        rsi:       float,
        vol_ratio: float,
        ema9:      float,
        ema50:     float,
        trigger_a: bool,
        trigger_b: bool,
        p:         Dict,
    ) -> float:
        if trigger_a and atr > 0:
            breakout_str = min((close - resist) / (atr + 1e-9), 2.0) / 2.0
        else:
            breakout_str = 0.3

        vol_threshold = p.get("volume_multiplier", 1.3) * 3
        vol_str = min(vol_ratio / max(vol_threshold, 1e-9), 1.0)

        rsi_center = (p["rsi_min"] + p["rsi_max"]) / 2.0
        rsi_range  = (p["rsi_max"] - p["rsi_min"]) / 2.0
        rsi_str    = max(
            0.0, 1.0 - abs(rsi - rsi_center) / max(rsi_range, 1.0)
        )

        trend_str = min((ema9 - ema50) / (atr + 1e-9), 2.0) / 2.0
        trend_str = max(0.0, trend_str)

        gc_bonus = 0.05 if trigger_b else 0.0

        confidence = round(
            0.30 * breakout_str
            + 0.25 * vol_str
            + 0.20 * rsi_str
            + 0.20 * trend_str
            + gc_bonus,
            4,
        )
        return max(0.0, min(1.0, confidence))


_REGISTRY: Dict[str, type] = {
    "volumetric_breakout": VolumetricBreakoutStrategy,
}

def get_strategy(
    name:      str,
    symbols:   List[str],
    timeframe: str,
    params:    Dict = None,
) -> BaseStrategy:
    if params is None: params = {}
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Strategy '{name}' tidak dikenal. Tersedia: {list(_REGISTRY)}"
        )
    return cls(symbols=symbols, timeframe=timeframe, params=params)

def list_strategies() -> List[str]:
    return list(_REGISTRY.keys())
