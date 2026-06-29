"""
intelligence/scorer.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    SCORE_NEUTRAL,
    SCORE_MIN,
    SCORE_MAX,
    SIGNAL_CONFIRMATION_MATRIX,
)
from core.models import (
    DecisionAction,
    IndicatorSet,
    MarketRegime,
    ObservationReport,
    ScoreBreakdown,
    ScoredSignal,
    SignalQuality,
    clamp_score,
    validate_score,
)
from profiles.weights import (
    get_level1_weights,
    get_level2_weights,
    get_regime_modifier,
    compute_category_score,
)
from profiles.thresholds import get_profile_thresholds, get_dynamic_threshold

log = logging.getLogger("intelligence.scorer")

# Buffer konfirmasi sinyal BUY per symbol
# Format: {symbol: {"count": int, "regime": str}}
_SIGNAL_CONFIRM_BUFFER: dict = {}

def _check_primary_trigger(
    profile_name: str,
    iset: IndicatorSet,
    profile_cfg,
) -> Tuple[bool, str]:
    from profiles.base_profile import PrimaryTriggerType

    trigger_type = profile_cfg.primary_trigger_type

    if trigger_type == PrimaryTriggerType.BREAKOUT_VOLUME:
        vol_ratio = iset.strength.volume_ratio
        if vol_ratio is None:
            return False, "Volume ratio tidak tersedia"
        if vol_ratio < profile_cfg.volume_mult:
            return False, (
                f"Volume ratio {vol_ratio:.2f}x < threshold {profile_cfg.volume_mult:.2f}x"
            )
        rsi = iset.momentum.rsi
        if rsi is None:
            return False, "RSI tidak tersedia"
        if rsi < profile_cfg.rsi_min or rsi > profile_cfg.rsi_max:
            return False, (
                f"RSI {rsi:.1f} di luar range [{profile_cfg.rsi_min}, {profile_cfg.rsi_max}]"
            )
        return True, "Breakout+Volume trigger terpenuhi"

    elif trigger_type == PrimaryTriggerType.TREND_CONFIRMATION:
        ema_score = iset.trend.ema_stack_score
        if ema_score < 55.0:
            return False, f"EMA stack score {ema_score:.1f} < 55 (trend belum confirm)"
        rsi = iset.momentum.rsi
        if rsi is None:
            return False, "RSI tidak tersedia"
        if rsi < profile_cfg.rsi_gc_min:
            return False, (
                f"RSI {rsi:.1f} < rsi_gc_min {profile_cfg.rsi_gc_min}"
            )
        return True, "Trend Confirmation trigger terpenuhi"

    elif trigger_type == PrimaryTriggerType.MOMENTUM_REVERSAL:
        rsi = iset.momentum.rsi
        if rsi is None:
            return False, "RSI tidak tersedia"
        if rsi > profile_cfg.rsi_max:
            return False, f"RSI {rsi:.1f} terlalu tinggi untuk mean revert entry"
        if rsi < profile_cfg.rsi_min:
            return True, f"RSI {rsi:.1f} oversold — mean revert trigger OK"
        macd_hist = iset.momentum.macd_histogram
        if macd_hist is not None and macd_hist > 0:
            return False, f"MACD histogram masih positif ({macd_hist:.5f}) — belum reversal"
        return True, "Momentum Reversal trigger terpenuhi"

    else:
        rsi = iset.momentum.rsi
        vol_ratio = iset.strength.volume_ratio
        log.debug(
            f"COMPOSITE trigger check | RSI={rsi} (need {profile_cfg.rsi_min}–{profile_cfg.rsi_max}) "
            f"| vol_ratio={vol_ratio} (need >{profile_cfg.volume_mult * 0.8:.2f}x)"
        )
        if rsi is None:
            return False, "RSI tidak tersedia"
        if rsi < profile_cfg.rsi_min or rsi > profile_cfg.rsi_max:
            return False, (
                f"RSI {rsi:.1f} di luar range [{profile_cfg.rsi_min}, {profile_cfg.rsi_max}]"
            )
        if vol_ratio is not None and vol_ratio < profile_cfg.volume_mult * 0.8:
            return False, (
                f"Volume ratio {vol_ratio:.2f}x terlalu rendah "
                f"(min {profile_cfg.volume_mult * 0.8:.2f}x)"
            )
        return True, "Composite trigger terpenuhi"

def _extract_indicator_scores(iset: IndicatorSet) -> Dict[str, Dict[str, float]]:
    return {
        "trend": {
            "ema_stack":  iset.trend.ema_stack_score,
            "cross":      iset.trend.cross_score,
            "supertrend": iset.trend.supertrend_score,
            "vwap":       iset.trend.vwap_score,
        },
        "momentum": {
            "rsi":      iset.momentum.rsi_score,
            "macd":     iset.momentum.macd_score,
            "stochrsi": iset.momentum.stoch_score,
        },
        "strength": {
            "adx":    iset.strength.adx_score,
            "di":     iset.strength.di_score,
            "volume": iset.strength.volume_score,
            "mfi":    iset.strength.mfi_score,
        },
        "volatility": {
            "bb":      iset.volatility.bb_score,
            "squeeze": iset.volatility.squeeze_score,
            "atr":     iset.volatility.atr_score,
        },
        "pattern": {
            "pattern_score": iset.patterns.pattern_score,
            "context_score": iset.patterns.context_score,
        },
        "oscillator": {
            "cci":            iset.oscillators.cci_score,
            "williams":       iset.oscillators.williams_r_score,
            "roc":            iset.oscillators.roc_score,
            # [v2] field baru
            "cci_trend":      iset.oscillators.cci_trend,
            "willr_trend":    iset.oscillators.willr_trend,
            "roc_crossover":  iset.oscillators.roc_crossover,
            "cci_divergence": iset.oscillators.cci_divergence,
        },
        "structure": {
            "ichimoku":  iset.structure.ichimoku_score,
            "sar":       iset.structure.sar_score,
            "pivot":     iset.structure.pivot_score,
            "fibonacci": iset.structure.fib_score,
        },
        "orderbook": {
            "ob_score": iset.orderbook.orderbook_score,
        },
    }

def _calc_weighted_breakdown(
    profile_name: str,
    indicator_scores: Dict[str, Dict[str, float]],
    regime: MarketRegime,
) -> ScoreBreakdown:
    l1_weights = get_level1_weights(profile_name)
    regime_mod = get_regime_modifier(profile_name, regime.value)

    breakdown = ScoreBreakdown(regime_modifier=regime_mod)

    categories = ["trend", "momentum", "strength", "volatility", "pattern", "oscillator", "structure", "orderbook"]

    for cat in categories:
        l1_weight = l1_weights.get(cat, 0.0)
        cat_indicators = indicator_scores.get(cat, {})

        cat_score = compute_category_score(profile_name, cat, cat_indicators)
        weighted  = round(cat_score * l1_weight, 4)

        setattr(breakdown, f"{cat}_raw",      round(cat_score, 4))
        setattr(breakdown, f"{cat}_weighted", weighted)
        setattr(breakdown, f"{cat}_weight",   l1_weight)

    return breakdown

def _suggest_sl_tp(
    current_price: float,
    atr: Optional[float],
    profile_cfg,
) -> Tuple[Optional[float], Optional[float]]:
    if current_price <= 0:
        return None, None

    if atr is not None and atr > 0:
        sl = current_price - atr * profile_cfg.atr_sl_mult
        tp = current_price + atr * profile_cfg.atr_tp_mult
    else:
        sl = current_price * (1 - profile_cfg.quick_sl_pct / 100)
        tp = current_price * (1 + profile_cfg.quick_tp_pct / 100)

    if sl >= current_price or tp <= current_price:
        return None, None

    return round(sl, 8), round(tp, 8)

def _generate_narrative(
    profile_name: str,
    breakdown: ScoreBreakdown,
    total_score: float,
    threshold: float,
    trigger_met: bool,
    trigger_reason: str,
    regime: MarketRegime,
    regime_confidence: float,
    iset: IndicatorSet,
) -> str:
    gap = total_score - threshold
    gap_str = f"+{gap:.1f}" if gap >= 0 else f"{gap:.1f}"
    status = "✅ TRIGGER" if trigger_met and total_score >= threshold else "❌ NO TRIGGER"

    categories = {
        "Trend":      breakdown.trend_raw,
        "Momentum":   breakdown.momentum_raw,
        "Strength":   breakdown.strength_raw,
        "Volatility": breakdown.volatility_raw,
        "Pattern":    breakdown.pattern_raw,
        "Oscillator": breakdown.oscillator_raw,
        "Structure":  breakdown.structure_raw,
        "Orderbook":  breakdown.orderbook_raw,
    }
    sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    strengths   = [(k, v) for k, v in sorted_cats if v >= 65.0][:2]
    weaknesses  = [(k, v) for k, v in sorted_cats if v < 50.0][:2]

    str_parts = [f"{k} ({v:.0f}/100)" for k, v in strengths]
    weak_parts = [f"{k} ({v:.0f}/100)" for k, v in weaknesses]

    lines = [
        f"{status} | Score: {total_score:.1f}/{threshold:.1f} ({gap_str})",
        f"Profile: {profile_name} | Regime: {regime.emoji} {regime.display_name} (conf={regime_confidence:.0%})",
    ]

    if str_parts:
        lines.append(f"💪 Kekuatan: {', '.join(str_parts)}")
    if weak_parts:
        lines.append(f"⚠️  Kelemahan: {', '.join(weak_parts)}")
    if not trigger_met:
        lines.append(f"🚫 No-Trigger: {trigger_reason}")
    if breakdown.regime_modifier < 1.0:
        lines.append(
            f"🔧 Regime modifier: ×{breakdown.regime_modifier:.2f} "
            f"(raw score sebelum modifier: {f'{breakdown.total() / breakdown.regime_modifier:.1f}' if breakdown.regime_modifier > 0 else 'N/A'})"
        )

    rsi = iset.momentum.rsi
    vol = iset.strength.volume_ratio
    adx = iset.strength.adx

    detail_parts = []
    if rsi is not None:
        detail_parts.append(f"RSI={rsi:.1f}")
    if vol is not None:
        detail_parts.append(f"Vol={vol:.2f}x")
    if adx is not None:
        detail_parts.append(f"ADX={adx:.1f}")
    if detail_parts:
        lines.append(f"📊 Key: {' | '.join(detail_parts)}")

    return "\n".join(lines)

def score_signal(
    observation: ObservationReport,
    regime: MarketRegime,
    regime_confidence: float,
    db_manager=None,
    profile_override=None,
) -> ScoredSignal:
    symbol         = observation.symbol
    profile_name   = observation.strategy_profile
    iset           = observation.primary_tf_indicators

    signal = ScoredSignal(
        observation=observation,
        strategy_profile=profile_name,
        regime=regime,
        regime_confidence=regime_confidence,
    )

    try:
        profile_cfg = profile_override if profile_override is not None else get_profile_thresholds(profile_name)
    except KeyError:
        signal.scoring_narrative = f"Profile '{profile_name}' tidak ditemukan."
        signal.add_validation_note(f"ERROR: Profile tidak dikenal: {profile_name}")
        return signal

    # Dynamic threshold berdasarkan kombinasi profile × regime
    dynamic_threshold = get_dynamic_threshold(profile_name, regime.value)
    signal.threshold_used = dynamic_threshold

    if iset is None or not observation.primary_tf_valid:
        signal.signal_type = "hold"
        signal.trigger_met = False
        reason = "Primary TF data tidak valid atau tidak tersedia"
        signal.scoring_narrative = f"❌ {reason}"
        signal.add_validation_note(reason)
        _save_score_to_db(signal, action="SKIP_INVALID_DATA", db_manager=db_manager)
        return signal

    if regime == MarketRegime.TRENDING_BEAR:
        signal.total_score   = 0.0
        signal.signal_type   = "hold"
        signal.trigger_met   = False
        signal.scoring_narrative = (
            f"❌ TRENDING_BEAR regime — tidak ada BUY signal. "
            f"Semua long position harus dipertimbangkan untuk exit."
        )
        signal.add_validation_note("Blocked by TRENDING_BEAR regime")
        _save_score_to_db(signal, action="REJECT_BEAR_REGIME", db_manager=db_manager)
        return signal

    trigger_met, trigger_reason = _check_primary_trigger(profile_name, iset, profile_cfg)
    signal.trigger_met = trigger_met

    if not trigger_met:
        signal.total_score   = 0.0
        signal.signal_type   = "hold"
        signal.scoring_narrative = f"❌ No-Trigger: {trigger_reason}"
        signal.add_validation_note(f"Primary trigger gagal: {trigger_reason}")
        _save_score_to_db(signal, action="NO_TRIGGER", db_manager=db_manager)
        return signal

    indicator_scores = _extract_indicator_scores(iset)
    breakdown = _calc_weighted_breakdown(profile_name, indicator_scores, regime)
    total_score = breakdown.total()
    total_score = max(SCORE_MIN, min(SCORE_MAX, total_score))

    signal.total_score     = round(total_score, 2)
    signal.score_breakdown = breakdown
    # [BUG-FIX v2] threshold_gap sekarang @property di core/models.py (otomatis
    # dihitung dari total_score & threshold_used terkini) — assignment manual
    # dihapus karena sekarang read-only & sudah selalu konsisten tanpa ini.

    score_confidence = max(0.0, (total_score - 50.0) / 50.0) 
    regime_factor    = min(1.0, regime_confidence * 1.2)
    conf_tf_factor   = (
        min(1.0, observation.confirmation_tf_score / 75.0)
        if observation.confirmation_tf_valid
        else 0.70
    )
    signal.confidence = round(
        score_confidence * 0.55
        + regime_factor   * 0.30
        + conf_tf_factor  * 0.15,
        3,
    )
    signal.confidence = max(0.0, min(1.0, signal.confidence))

    if total_score >= dynamic_threshold:
        # Konfirmasi BUY berdasarkan regime
        regime_key = regime.value
        required   = SIGNAL_CONFIRMATION_MATRIX.get(regime_key, 6)
        buf        = _SIGNAL_CONFIRM_BUFFER.get(symbol, {"count": 0, "regime": regime_key})

        # Reset jika regime berubah
        if buf["regime"] != regime_key:
            buf = {"count": 0, "regime": regime_key}

        buf["count"] += 1
        _SIGNAL_CONFIRM_BUFFER[symbol] = buf

        if buf["count"] >= required:
            signal.signal_type = "buy"
            log.info(
                "%s | ✅ Konfirmasi BUY terpenuhi: %d/%d (regime=%s)",
                symbol, buf["count"], required, regime_key,
            )
            # Reset setelah execute
            _SIGNAL_CONFIRM_BUFFER[symbol] = {"count": 0, "regime": regime_key}

            # ── Validasi conf_score pakai confirmation_min_score dari profil ──
            conf_score  = observation.confirmation_tf_score
            conf_min    = getattr(profile_cfg, "confirmation_min_score", 42.0)
            conf_strong = conf_min + 15.0  # threshold kuat = min + 15

            if conf_score < conf_min:
                signal.signal_type = "hold"
                log.info(
                    "%s | ❌ BUY DITOLAK — conf_score=%.1f < min=%.1f "
                    "(higher TF tidak mendukung, profil=%s)",
                    symbol, conf_score, conf_min, profile_name,
                )
            elif conf_score < conf_strong:
                log.info(
                    "%s | ⚠️  conf_score=%.1f lemah (%.1f-%.1f) — "
                    "BUY lanjut tapi waspada",
                    symbol, conf_score, conf_min, conf_strong,
                )
            else:
                log.info(
                    "%s | ✅ conf_score=%.1f kuat >= %.1f — higher TF mendukung BUY",
                    symbol, conf_score, conf_strong,
                )
        else:
            signal.signal_type = "hold"
            log.info(
                "%s | ⏳ Menunggu konfirmasi BUY: %d/%d (regime=%s)",
                symbol, buf["count"], required, regime_key,
            )
    else:
        signal.signal_type = "hold"
        signal.trigger_met = False  # FIX: score < threshold, trigger harus False
        # Reset buffer jika score turun
        if symbol in _SIGNAL_CONFIRM_BUFFER:
            _SIGNAL_CONFIRM_BUFFER[symbol] = {"count": 0, "regime": regime.value}

    atr = iset.volatility.atr
    price = iset.current_price
    suggested_sl, suggested_tp = _suggest_sl_tp(price, atr, profile_cfg)
    signal.suggested_sl = suggested_sl
    signal.suggested_tp = suggested_tp

    signal.scoring_narrative = _generate_narrative(
        profile_name=profile_name,
        breakdown=breakdown,
        total_score=total_score,
        threshold=dynamic_threshold,
        trigger_met=trigger_met,
        trigger_reason=trigger_reason,
        regime=regime,
        regime_confidence=regime_confidence,
        iset=iset,
    )

    log.info(
        "%s | profile=%s | score=%.1f/%.1f (%+.1f) | trigger=%s | "
        "regime=%s | confidence=%.2f | signal=%s",
        symbol, profile_name,
        total_score, dynamic_threshold, signal.threshold_gap,
        trigger_met,
        regime.value, signal.confidence,
        signal.signal_type,
    )

    action = "EXECUTE_CANDIDATE" if signal.is_actionable else "HOLD"
    _save_score_to_db(signal, action=action, db_manager=db_manager)

    return signal

def _save_score_to_db(signal: ScoredSignal, action: str, db_manager) -> None:
    if db_manager is None:
        return

    try:
        bd = signal.score_breakdown
        rejection_reason = (
            "\n".join(signal.validation_notes)
            if getattr(signal, "validation_notes", None)
            else None
        )

        async def _persist() -> None:
            await db_manager.save_signal_score(
                symbol=signal.symbol,
                strategy_profile=signal.strategy_profile,
                total_score=signal.total_score,
                trend_score=bd.trend_raw if bd else SCORE_NEUTRAL,
                momentum_score=bd.momentum_raw if bd else SCORE_NEUTRAL,
                strength_score=bd.strength_raw if bd else SCORE_NEUTRAL,
                volatility_score=bd.volatility_raw if bd else SCORE_NEUTRAL,
                pattern_score=bd.pattern_raw if bd else SCORE_NEUTRAL,
                oscillator_score=bd.oscillator_raw if bd else SCORE_NEUTRAL,
                structure_score=bd.structure_raw if bd else SCORE_NEUTRAL,
                orderbook_score=bd.orderbook_raw if bd else SCORE_NEUTRAL,
                threshold_used=signal.threshold_used,
                regime=signal.regime.value if signal.regime else "undefined",
                regime_confidence=getattr(signal, "regime_confidence", None),
                trigger_met=signal.trigger_met,
                signal_type=signal.signal_type,
                action_taken=action,
                rejection_reason=rejection_reason,
                current_price=getattr(signal.observation.primary_tf_indicators, "current_price", None) if signal.observation and signal.observation.primary_tf_indicators else None,
                suggested_sl=signal.suggested_sl,
                suggested_tp=signal.suggested_tp,
                nearest_support=getattr(signal.observation.primary_tf_indicators, "nearest_support", None) if signal.observation and signal.observation.primary_tf_indicators else None,
                nearest_resistance=getattr(signal.observation.primary_tf_indicators, "nearest_resistance", None) if signal.observation and signal.observation.primary_tf_indicators else None,
                fib_support=getattr(signal.observation.primary_tf_indicators, "nearest_fib_support", None) if signal.observation and signal.observation.primary_tf_indicators else None,
                fib_resistance=getattr(signal.observation.primary_tf_indicators, "nearest_fib_resistance", None) if signal.observation and signal.observation.primary_tf_indicators else None,
                signal_confidence=getattr(signal, "confidence", None),
            )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_persist(), loop)
            else:
                loop.run_until_complete(_persist())
        except RuntimeError:
            # No running loop (e.g. called from sync context during tests)
            return

    except Exception as exc:
        log.debug("Gagal simpan signal score ke DB (non-critical): %s", exc)

def score_all(
    observations: Dict[str, ObservationReport],
    regimes: Dict[str, Tuple[MarketRegime, float]],
    db_manager=None,
) -> Dict[str, ScoredSignal]:
    results: Dict[str, ScoredSignal] = {}

    for symbol, obs in observations.items():
        regime, confidence = regimes.get(symbol, (MarketRegime.UNDEFINED, 0.0))
        try:
            results[symbol] = score_signal(
                observation=obs,
                regime=regime,
                regime_confidence=confidence,
                db_manager=db_manager,
            )
        except Exception as exc:
            log.exception("Error scoring %s: %s", symbol, exc)
            fallback = ScoredSignal(
                observation=obs,
                strategy_profile=obs.strategy_profile,
                regime=regime,
                regime_confidence=confidence,
            )
            fallback.add_validation_note(f"Scoring error: {exc}")
            results[symbol] = fallback

    sorted_results = sorted(
        results.items(),
        key=lambda kv: kv[1].total_score,
        reverse=True,
    )

    log.info(
        "Scored %d symbols | Top: %s",
        len(results),
        ", ".join(
            f"{sym}={sig.total_score:.1f}"
            for sym, sig in sorted_results[:5]
        ),
    )

    return results

def get_score_board_text(signals: Dict[str, ScoredSignal]) -> str:
    if not signals:
        return "📊 Tidak ada data score tersedia."

    lines = ["📊 Score Board:"]
    sorted_sigs = sorted(signals.values(), key=lambda s: s.total_score, reverse=True)

    for sig in sorted_sigs:
        threshold = sig.threshold_used
        gap = sig.threshold_gap
        gap_str = f"+{gap:.1f}" if gap >= 0 else f"{gap:.1f}"
        trigger_icon = "✅" if sig.is_actionable else ("⚡" if sig.trigger_met else "❌")
        regime_icon  = sig.regime.emoji
        quality_icon = {
            SignalQuality.EXCELLENT: "🔥",
            SignalQuality.GOOD:      "👍",
            SignalQuality.FAIR:      "👌",
            SignalQuality.POOR:      "❄️",
        }.get(sig.signal_quality, "")

        lines.append(
            f"  {trigger_icon} {regime_icon} {sig.symbol:<12} "
            f"{sig.total_score:>5.1f}/{threshold:.0f} ({gap_str:>5}) {quality_icon}"
        )

    return "\n".join(lines)


class SignalScorer:
    def __init__(self, db_manager=None):
        self._db = db_manager

    def score(
        self,
        observation: ObservationReport,
        profile,
        regime: MarketRegime,
        regime_confidence: float,
    ) -> Optional[ScoredSignal]:
        return score_signal(
            observation=observation,
            regime=regime,
            regime_confidence=regime_confidence,
            profile_override=profile,
            db_manager=self._db,
        )
