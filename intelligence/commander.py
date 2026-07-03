"""
intelligence/commander.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    KELLY_FRACTION,
    KELLY_MIN_SAMPLE,
    KELLY_MAX_SIZE_PCT,
    KELLY_MIN_SIZE_PCT,
    CORRELATED_POSITION_PENALTY,
    CORRELATION_HIGH_THRESHOLD,
    SPREAD_LIMIT_DEFAULT,
    REGIME_MIN_CONFIDENCE_TO_TRADE,
    get_correlation_group,
)
from core.models import (
    DecisionAction,
    MarketRegime,
    ScoredSignal,
    TradeDecision,
    clamp_score,
)
from intelligence.classifier import is_tradeable_regime

log = logging.getLogger("intelligence.commander")

def _calc_kelly_size(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    kelly_fraction: float = KELLY_FRACTION,
    max_size_pct: float = KELLY_MAX_SIZE_PCT,
    min_size_pct: float = KELLY_MIN_SIZE_PCT,
) -> Tuple[float, str]:
    if avg_loss_pct <= 0:
        return min_size_pct, "fallback_zero_loss"

    r = avg_win_pct / avg_loss_pct

    if r <= 0 or win_rate <= 0:
        return min_size_pct, "fallback_negative_edge"

    k_full = win_rate - (1.0 - win_rate) / r

    if k_full <= 0:
        return 0.0, "kelly_negative_edge"

    k_fractional = k_full * kelly_fraction * 100

    k_clamped = max(min_size_pct, min(max_size_pct, k_fractional))

    return round(k_clamped, 2), "kelly_half"

async def _get_kelly_inputs(
    symbol: str,
    profile_name: str,
    lookback: int,
    db_manager,
) -> Optional[Tuple[float, float, float]]:
    if db_manager is None:
        return None

    try:
        stats = await db_manager.get_trade_stats(  
            symbol=symbol,
            profile=profile_name,
            limit=lookback,
        )
        if not stats:
            return None

        total   = stats.get("total_trades", 0)
        wins    = stats.get("win_count", 0)
        avg_w   = stats.get("avg_win_pct", 0.0)
        avg_l   = stats.get("avg_loss_pct", 0.0)

        if total < KELLY_MIN_SAMPLE:
            return None

        win_rate = wins / total if total > 0 else 0.0
        return win_rate, avg_w, abs(avg_l)

    except Exception as exc:
        log.debug("Gagal ambil Kelly inputs dari DB: %s", exc)
        return None

def _get_correlated_open_symbols(
    symbol: str,
    open_positions: List[str],
) -> List[str]:
    base = symbol.split("/")[0].upper()
    my_group = get_correlation_group(base)

    if not my_group:
        return []

    correlated = []
    for pos_sym in open_positions:
        pos_base  = pos_sym.split("/")[0].upper()
        pos_group = get_correlation_group(pos_base)
        if pos_group and pos_group == my_group and pos_base != base:
            correlated.append(pos_sym)

    return correlated

def _calc_correlation_penalty(
    n_correlated: int,
    penalty_per: float = CORRELATED_POSITION_PENALTY,
) -> float:
    if n_correlated == 0:
        return 0.0
    penalty = 1.0 - (1.0 - penalty_per) ** n_correlated
    return round(min(0.90, penalty), 3)

def _refine_sl_tp(
    entry_price: float,
    suggested_sl: Optional[float],
    suggested_tp: Optional[float],
    iset,
    profile_cfg,
    atr: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if entry_price <= 0:
        return suggested_sl, suggested_tp

    min_sl_dist = (atr or 0) * 1.0
    min_tp_dist = (atr or 0) * 1.5

    final_sl = suggested_sl
    if final_sl is not None and atr is not None:
        atr_sl = entry_price - min_sl_dist
        final_sl = min(final_sl, atr_sl)

    final_tp = suggested_tp
    if final_tp is not None and iset is not None:
        bb_upper = getattr(iset.volatility, "bb_upper", None)
        if bb_upper and bb_upper > entry_price:
            final_tp = min(final_tp, bb_upper * 0.998)

    if final_sl is not None and final_sl >= entry_price:
        final_sl = entry_price * (1 - profile_cfg.quick_sl_pct / 100)
    if final_tp is not None and final_tp <= entry_price:
        final_tp = entry_price * (1 + profile_cfg.quick_tp_pct / 100)

    if final_sl is not None and final_tp is not None:
        risk   = entry_price - final_sl
        reward = final_tp - entry_price
        if risk > 0 and reward / risk < 1.3:
            final_tp = entry_price + risk * 1.3

    if final_sl:
        final_sl = round(final_sl, 8)
    if final_tp:
        final_tp = round(final_tp, 8)

    return final_sl, final_tp

def _gate_score_and_trigger(
    signal: ScoredSignal,
    decision: TradeDecision,
) -> bool:
    from profiles.thresholds import get_dynamic_threshold
    try:
        min_score = get_dynamic_threshold(
            signal.strategy_profile,
            signal.regime.value if signal.regime else "undefined"
        )
    except Exception:
        min_score = signal.threshold_used

    if not signal.trigger_met:
        decision.add_gate_failed("G1_TRIGGER", f"Primary trigger tidak terpenuhi")
        return False

    if signal.total_score < min_score:
        decision.add_gate_failed(
            "G1_SCORE",
            f"Score {signal.total_score:.1f} < threshold {min_score:.1f}",
        )
        return False

    decision.add_gate_passed("G1_SCORE_TRIGGER")
    return True

def _gate_regime(
    signal: ScoredSignal,
    decision: TradeDecision,
    allowed_regimes: List[str],
    min_confidence: float = REGIME_MIN_CONFIDENCE_TO_TRADE,
) -> bool:
    tradeable, reason = is_tradeable_regime(
        regime=signal.regime,
        confidence=signal.regime_confidence,
        allowed_regimes=allowed_regimes,
        min_confidence=min_confidence,
    )
    if not tradeable:
        decision.add_gate_failed("G2_REGIME", reason)
        return False

    decision.add_gate_passed("G2_REGIME")
    return True

def _gate_spread(
    symbol: str,
    max_spread_pct: float,
    exchange_connector,
    decision: TradeDecision,
) -> bool:
    if exchange_connector is None:
        decision.add_gate_passed("G3_SPREAD_SKIPPED")
        return True

    try:
        # Feed health check (if available).
        is_healthy = getattr(exchange_connector, "is_feed_healthy", None)
        if callable(is_healthy) and not bool(is_healthy(symbol)):
            decision.add_gate_failed("G3_FEED", "WS feed unhealthy")
            return False

        spread_pct = None
        # [BUG-FIX] Sebelumnya kode mengecek `fn_name not in explicit_attrs`
        # dengan explicit_attrs = exchange_connector.__dict__ (dict INSTANCE).
        # Method seperti get_current_spread_pct/get_spread_pct/get_spread
        # didefinisikan sebagai method biasa di body class ExchangeConnector
        # (exchange.py) — method class TIDAK PERNAH muncul di __dict__
        # instance (hanya atribut yang di-assign via self.x = ... yang
        # muncul di situ). Akibatnya `fn_name not in explicit_attrs` SELALU
        # True untuk ketiga nama method ini -> loop selalu `continue` tanpa
        # pernah memanggil getattr yang sebenarnya -> spread_pct SELALU None
        # -> gate G3_SPREAD tidak pernah benar-benar mengecek spread nyata,
        # selalu lolos sebagai "G3_SPREAD_UNKNOWN" walau data spread ada.
        # Sekarang: getattr() sendiri sudah aman kalau atribut tidak ada
        # (return None default), jadi pre-check __dict__ yang salah itu
        # dihapus — cukup coba getattr+callable langsung.
        for fn_name in ("get_current_spread_pct", "get_spread_pct", "get_spread"):
            fn = getattr(exchange_connector, fn_name, None)
            if callable(fn):
                spread_pct = fn(symbol)
                break

        if spread_pct is not None and not isinstance(spread_pct, (int, float)):
            try:
                spread_pct = float(spread_pct)
            except Exception:
                spread_pct = None

        if spread_pct is None:
            decision.add_gate_passed("G3_SPREAD_UNKNOWN")
            return True

        if spread_pct > max_spread_pct:
            decision.add_gate_failed(
                "G3_SPREAD",
                f"Spread {spread_pct:.3f}% > max {max_spread_pct:.3f}%",
            )
            return False

        decision.add_gate_passed(f"G3_SPREAD({spread_pct:.3f}%)")
        return True

    except Exception as exc:
        log.warning("G3 spread check error (non-blocking): %s", exc)
        decision.add_gate_passed("G3_SPREAD_ERROR_BYPASS")
        return True

async def _gate_risk_manager(
    signal: ScoredSignal,
    decision: TradeDecision,
    risk_manager,
    portfolio_value: float,
    base_position_size_pct: float,
    entry_price: float = 0.0,
) -> Tuple[bool, float]:
    if risk_manager is None:
        decision.add_gate_passed("G4_RISK_SKIPPED")
        return True, base_position_size_pct

    try:
        if bool(getattr(risk_manager, "is_halted", False)):
            reason = getattr(risk_manager, "halt_reason", None) or "Risk manager halted"
            decision.add_gate_failed("G4_RISK_HALTED", str(reason))
            return False, 0.0

        assessment = await risk_manager.evaluate_order(
            symbol=signal.symbol,
            side="buy",
            price=entry_price,
            quantity=(portfolio_value * base_position_size_pct / 100) / entry_price if entry_price > 0 else 0.0,
        )

        if not assessment.is_approved:
            decision.add_gate_failed(
                "G4_RISK",
                assessment.reason or "Risk manager rejected",
            )
            return False, 0.0

        approved_size = base_position_size_pct
        cand = getattr(assessment, "approved_size", None)
        if isinstance(cand, (int, float)):
            approved_size = float(cand)

        decision.add_gate_passed(f"G4_RISK(size={approved_size:.2f}%)")
        return True, approved_size

    except Exception as exc:
        log.warning("G4 risk manager error: %s", exc)
        decision.add_gate_passed("G4_RISK_ERROR_BYPASS")
        return True, base_position_size_pct

async def _gate_kelly_sizing(
    signal: ScoredSignal,
    decision: TradeDecision,
    base_size_pct: float,
    profile_cfg,
    db_manager,
) -> float:
    if not getattr(profile_cfg, "kelly_enabled", True):
        decision.add_gate_passed("G5_KELLY_DISABLED")
        return base_size_pct

    lookback = getattr(profile_cfg, "kelly_lookback_trades", 50)
    kelly_inputs = await _get_kelly_inputs(
        symbol=signal.symbol,
        profile_name=signal.strategy_profile,
        lookback=lookback,
        db_manager=db_manager,
    )

    if kelly_inputs is None:
        decision.kelly_method_used = "fallback_insufficient_data"
        decision.add_gate_passed(f"G5_KELLY_FALLBACK(size={base_size_pct:.2f}%)")
        decision.kelly_fraction = None
        return base_size_pct

    win_rate, avg_win, avg_loss = kelly_inputs
    kelly_size, method = _calc_kelly_size(
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        kelly_fraction=KELLY_FRACTION,
        max_size_pct=KELLY_MAX_SIZE_PCT,
        min_size_pct=KELLY_MIN_SIZE_PCT,
    )

    if kelly_size <= 0:
        decision.add_gate_failed(
            "G5_KELLY",
            f"Kelly negative edge (win_rate={win_rate:.1%}, R={avg_win/avg_loss:.2f})",
        )
        decision.kelly_method_used = "kelly_negative_edge"
        return 0.0

    decision.kelly_fraction    = kelly_size
    decision.kelly_method_used = method

    # ── Adaptive Sizing: aktifkan signal_quality + consecutive_loss_size_mult ──
    from core.models import SignalQuality
    quality_mult = {
        SignalQuality.EXCELLENT: 1.3,
        SignalQuality.GOOD:      1.1,
        SignalQuality.FAIR:      1.0,
        SignalQuality.POOR:      0.8,
    }.get(signal.signal_quality, 1.0)

    consec_mult = getattr(profile_cfg, "consecutive_loss_size_mult", 1.0) or 1.0
    # consecutive_loss_size_mult hanya aktif kalau ada consecutive losses
    # (validator sudah set confidence_adjustment negatif kalau ada)
    conf_adj = getattr(signal, "confidence_adjustment", 0.0) or 0.0
    if conf_adj >= 0:
        consec_mult = 1.0  # tidak ada consecutive losses, tidak perlu kurangi size

    adjusted_size = round(kelly_size * quality_mult * consec_mult, 2)
    adjusted_size = max(KELLY_MIN_SIZE_PCT, min(KELLY_MAX_SIZE_PCT, adjusted_size))

    decision.add_gate_passed(
        f"G5_KELLY({method}: {kelly_size:.2f}%->adj={adjusted_size:.2f}% "
        f"quality={signal.signal_quality.value} qmult={quality_mult:.1f} cmult={consec_mult:.1f} "
        f"wr={win_rate:.1%})"
    )
    return adjusted_size

def _gate_correlation(
    signal: ScoredSignal,
    decision: TradeDecision,
    open_positions: List[str],
    base_size_pct: float,
) -> float:
    correlated = _get_correlated_open_symbols(signal.symbol, open_positions)

    if not correlated:
        decision.add_gate_passed("G6_CORRELATION_CLEAN")
        return base_size_pct

    penalty = _calc_correlation_penalty(len(correlated))
    adjusted_size = base_size_pct * (1.0 - penalty)
    adjusted_size = round(max(KELLY_MIN_SIZE_PCT, adjusted_size), 2)

    decision.correlated_symbols  = correlated
    decision.correlation_penalty = penalty

    decision.add_gate_passed(
        f"G6_CORRELATION(correlated={correlated}, penalty={penalty:.0%}, "
        f"size {base_size_pct:.2f}%→{adjusted_size:.2f}%)"
    )
    return adjusted_size

def _gate_supertrend(
    signal: ScoredSignal,
    decision: TradeDecision,
) -> bool:
    """
    Supertrend Confidence Filter — otomatis menyesuaikan kondisi pasar.
    Bukan hard filter kaku, tapi dinamis berdasarkan regime + ADX.
    """
    iset = signal.observation.primary_tf_indicators
    if iset is None:
        decision.add_gate_passed("G1_ST_NO_DATA")
        return True

    st_dir = iset.trend.supertrend_direction
    adx    = iset.strength.adx
    regime = signal.regime.value if signal.regime else "undefined"

    if st_dir == 1:
        decision.add_gate_passed(f"G1_ST_BULL(dir={st_dir})")
        return True

    if st_dir is None or st_dir == 0:
        decision.add_gate_passed("G1_ST_NEUTRAL")
        return True

    if regime == "volatile_expansion":
        decision.add_gate_passed(f"G1_ST_VOLATILE_BYPASS(regime={regime})")
        return True

    if adx is not None and adx >= 25.0:
        decision.add_gate_failed(
            "G1_ST_BEAR",
            f"Supertrend bearish (dir=-1) + tren kuat (ADX={adx:.1f}) — arah berlawanan"
        )
        return False

    decision.add_gate_passed(f"G1_ST_WEAK_TREND(adx={adx})")
    return True


async def decide(
    signal: ScoredSignal,
    open_positions: Optional[List[str]] = None,
    portfolio_value: float = 0.0,
    base_risk_pct: float = 1.0,
    min_regime_confidence: Optional[float] = None,
    max_spread_pct_override: Optional[float] = None,
    exchange_connector=None,
    risk_manager=None,
    db_manager=None,
) -> TradeDecision:
    decision = TradeDecision(scored_signal=signal)
    open_positions = open_positions or []

    if signal.symbol in open_positions:
        decision.action = DecisionAction.WAIT
        decision.rejection_reason = f"Posisi sudah terbuka untuk {signal.symbol}"
        decision.add_gate_failed("G0_ALREADY_OPEN", decision.rejection_reason)
        decision.decision_narrative = _build_narrative(decision, "WAIT — posisi sudah terbuka")
        return decision

    try:
        from profiles.thresholds import get_profile_thresholds
        profile_cfg = get_profile_thresholds(signal.strategy_profile)
        allowed_regimes     = profile_cfg.allowed_regimes
        max_spread_pct      = float(
            max_spread_pct_override
            if max_spread_pct_override is not None
            else getattr(profile_cfg, "max_spread_pct", SPREAD_LIMIT_DEFAULT)
        )
    except KeyError:
        decision.action           = DecisionAction.REJECT
        decision.rejection_reason = f"Profile '{signal.strategy_profile}' tidak ditemukan"
        decision.add_gate_failed("G0_PROFILE", decision.rejection_reason)
        decision.decision_narrative = decision.rejection_reason
        return decision

    if not _gate_supertrend(signal, decision):
        decision.action           = DecisionAction.REJECT
        decision.rejection_reason = decision.gates_failed[-1] if decision.gates_failed else "Supertrend bearish"
        decision.decision_narrative = _build_narrative(decision, "REJECT — supertrend bearish + tren kuat")
        return decision

    if not _gate_score_and_trigger(signal, decision):
        # Trigger not met is a hard reject (not "wait for better score").
        decision.action           = DecisionAction.REJECT if not signal.trigger_met else DecisionAction.WAIT
        decision.rejection_reason = decision.gates_failed[-1] if decision.gates_failed else "Score/trigger tidak terpenuhi"
        decision.decision_narrative = _build_narrative(
            decision,
            "REJECT — trigger tidak terpenuhi" if decision.action == DecisionAction.REJECT
            else "WAIT — menunggu kondisi lebih baik",
        )
        return decision

    min_conf = (
        float(min_regime_confidence)
        if min_regime_confidence is not None
        else float(getattr(profile_cfg, "min_regime_confidence", REGIME_MIN_CONFIDENCE_TO_TRADE))
    )
    if not _gate_regime(signal, decision, allowed_regimes, min_confidence=min_conf):
        decision.action           = DecisionAction.WAIT
        decision.rejection_reason = decision.gates_failed[-1] if decision.gates_failed else "Regime tidak diizinkan"
        decision.decision_narrative = _build_narrative(decision, "WAIT — regime tidak favorable")
        return decision

    if not _gate_spread(signal.symbol, max_spread_pct, exchange_connector, decision):
        decision.action           = DecisionAction.WAIT
        decision.rejection_reason = decision.gates_failed[-1]
        decision.decision_narrative = _build_narrative(decision, "WAIT — spread terlalu lebar")
        return decision

    iset        = signal.observation.primary_tf_indicators
    entry_price = iset.current_price if iset else 0.0

    risk_ok, approved_size = await _gate_risk_manager(
        signal, decision, risk_manager, portfolio_value, base_risk_pct,
        entry_price=entry_price,
    )
    if not risk_ok:
        decision.action           = DecisionAction.REJECT
        decision.rejection_reason = decision.gates_failed[-1]
        decision.decision_narrative = _build_narrative(decision, "REJECT — risk manager block")
        return decision

    kelly_size = await _gate_kelly_sizing(signal, decision, approved_size, profile_cfg, db_manager)
    if kelly_size <= 0:
        decision.action           = DecisionAction.REJECT
        decision.rejection_reason = "Kelly negative edge — tidak ada keuntungan statiskal"
        decision.decision_narrative = _build_narrative(decision, "REJECT — Kelly negative edge")
        return decision

    final_size_pct = _gate_correlation(signal, decision, open_positions, kelly_size)

    atr        = iset.volatility.atr if iset else None

    final_sl, final_tp = _refine_sl_tp(
        entry_price=entry_price,
        suggested_sl=signal.suggested_sl,
        suggested_tp=signal.suggested_tp,
        iset=iset,
        profile_cfg=profile_cfg,
        atr=atr,
    )

    if final_sl is None or final_tp is None or entry_price <= 0:
        decision.action           = DecisionAction.REJECT
        decision.rejection_reason = "Tidak bisa tentukan SL/TP yang valid"
        decision.add_gate_failed("G7_SLTP", decision.rejection_reason)
        decision.decision_narrative = _build_narrative(decision, "REJECT — SL/TP calculation failed")
        return decision

    decision.add_gate_passed(
        f"G7_SLTP(sl={final_sl:.6f}, tp={final_tp:.6f})"
    )

    decision.action            = DecisionAction.EXECUTE
    decision.final_sl          = final_sl
    decision.final_tp          = final_tp
    decision.position_size_pct = final_size_pct

    if portfolio_value > 0:
        decision.position_size = round(portfolio_value * final_size_pct / 100, 2)
    else:
        # Fallback: store fractional sizing when portfolio value is unknown.
        decision.position_size = round(final_size_pct / 100, 6)

    decision.decision_narrative = _build_narrative(
        decision,
        f"EXECUTE | {signal.symbol} | score={signal.total_score:.1f} | "
        f"size={final_size_pct:.2f}% | SL={final_sl:.6f} TP={final_tp:.6f}"
    )

    log.info(
        "🟢 EXECUTE %s | score=%.1f/%.1f | regime=%s | "
        "size=%.2f%% | SL=%.6f TP=%.6f | gates=%d/%d passed",
        signal.symbol,
        signal.total_score, signal.threshold_used,
        signal.regime.value,
        final_size_pct, final_sl, final_tp,
        len(decision.gates_passed),
        len(decision.gates_passed) + len(decision.gates_failed),
    )

    return decision

def _build_narrative(decision: TradeDecision, summary_line: str) -> str:
    lines = [summary_line]
    if decision.gates_passed:
        lines.append(f"  ✅ Gates passed: {', '.join(decision.gates_passed)}")
    if decision.gates_failed:
        lines.append(f"  ❌ Gates failed: {', '.join(decision.gates_failed)}")
    if decision.correlated_symbols:
        lines.append(
            f"  🔗 Correlated: {decision.correlated_symbols} "
            f"(penalty={decision.correlation_penalty:.0%})"
        )
    if decision.kelly_method_used:
        lines.append(f"  📐 Kelly: {decision.kelly_method_used}")
    return "\n".join(lines)

def should_exit_on_regime_change(
    symbol: str,
    current_regime: MarketRegime,
    entry_regime: Optional[MarketRegime],
    profile_name: str = "universal",
) -> Tuple[bool, str, str]:
    """Evaluasi aksi berdasarkan transisi regime.
    Return: (should_exit: bool, reason: str, action: str)
    action: HOLD | HOLD_TIGHTEN_SL | HOLD_RELAX_SL | EXIT
    """
    from profiles.thresholds import get_transition_action
    current_val = current_regime.value if current_regime else "undefined"
    entry_val   = entry_regime.value   if entry_regime   else "undefined"

    action = get_transition_action(profile_name, entry_val, current_val)

    if action == "EXIT":
        return True, f"Regime {entry_val}→{current_val} [{profile_name}]: EXIT", action
    if action == "HOLD_TIGHTEN_SL":
        return False, f"Regime {entry_val}→{current_val} [{profile_name}]: TIGHTEN SL", action
    if action == "HOLD_RELAX_SL":
        return False, f"Regime {entry_val}→{current_val} [{profile_name}]: RELAX SL", action
    return False, f"Regime {entry_val}→{current_val} [{profile_name}]: HOLD", action

def should_exit_on_score_drop(
    current_score: float,
    entry_score: float,
    drop_threshold: float = 30.0,
) -> Tuple[bool, str]:
    drop = entry_score - current_score
    if drop >= drop_threshold:
        return True, (
            f"Score drop signifikan: {entry_score:.1f} → {current_score:.1f} "
            f"(-{drop:.1f} poin)"
        )
    return False, ""

def evaluate_exit(
    symbol: str,
    current_signal: ScoredSignal,
    entry_score: float,
    entry_regime: Optional[MarketRegime] = None,
) -> Tuple[bool, str]:
    regime_exit, regime_reason, regime_action = should_exit_on_regime_change(
        symbol=symbol,
        current_regime=current_signal.regime,
        entry_regime=entry_regime,
    )
    if regime_exit:
        return True, regime_reason

    score_exit, score_reason = should_exit_on_score_drop(
        current_score=current_signal.total_score,
        entry_score=entry_score,
    )
    if score_exit:
        return True, score_reason

    return False, ""

async def decide_all(
    signals: Dict[str, ScoredSignal],
    open_positions: Optional[List[str]] = None,
    portfolio_value: float = 0.0,
    base_risk_pct: float = 1.0,
    min_regime_confidence: Optional[float] = None,
    max_spread_pct_override: Optional[float] = None,
    exchange_connector=None,
    risk_manager=None,
    db_manager=None,
) -> Dict[str, TradeDecision]:
    decisions: Dict[str, TradeDecision] = {}

    effective_open = list(open_positions or [])

    for symbol, signal in signals.items():
        try:
            decision = await decide( 
                signal=signal,
                open_positions=effective_open,
                portfolio_value=portfolio_value,
                base_risk_pct=base_risk_pct,
                min_regime_confidence=min_regime_confidence,
                max_spread_pct_override=max_spread_pct_override,
                exchange_connector=exchange_connector,
                risk_manager=risk_manager,
                db_manager=db_manager,
            )
            decisions[symbol] = decision

            if decision.is_executable:
                effective_open.append(symbol)

        except Exception as exc:
            log.exception("Error decide() untuk %s: %s", symbol, exc)
            fallback = TradeDecision(scored_signal=signal)
            fallback.action = DecisionAction.REJECT
            fallback.rejection_reason = f"Commander error: {exc}"
            decisions[symbol] = fallback

    execute_count = sum(1 for d in decisions.values() if d.is_executable)
    wait_count    = sum(1 for d in decisions.values() if d.action == DecisionAction.WAIT)
    reject_count  = sum(1 for d in decisions.values() if d.action == DecisionAction.REJECT)

    log.info(
        "decide_all: %d symbols | EXECUTE=%d WAIT=%d REJECT=%d",
        len(decisions), execute_count, wait_count, reject_count,
    )

    return decisions

def get_decision_summary_text(decisions: Dict[str, TradeDecision]) -> str:
    if not decisions:
        return "Tidak ada decision."

    lines = ["🎯 Trade Decisions:"]
    for symbol, dec in sorted(decisions.items()):
        icon = {"execute": "🟢", "wait": "🟡", "reject": "🔴"}.get(
            dec.action.value, "⚪"
        )
        score = dec.scored_signal.total_score
        gates = f"{len(dec.gates_passed)}/{len(dec.gates_passed)+len(dec.gates_failed)}"
        lines.append(
            f"  {icon} {symbol:<12} {dec.action.value.upper():<7} "
            f"score={score:.1f} gates={gates}"
        )
        if dec.action == DecisionAction.EXECUTE:
            lines.append(
                f"       SL={dec.final_sl:.6f} TP={dec.final_tp:.6f} "
                f"size={dec.position_size_pct:.2f}%"
            )
        elif dec.rejection_reason:
            reason = dec.rejection_reason[:70]
            lines.append(f"       Reason: {reason}")

    return "\n".join(lines)

class TradeCommander:

    def __init__(
        self,
        risk_manager=None,
        ws_feed=None,
        db=None,
        config: dict = None,
    ):
        self._risk    = risk_manager
        self._ws_feed = ws_feed
        self._db      = db
        self._config  = config or {}
        log.info(
            "TradeCommander init | risk=%s ws_feed=%s db=%s",
            risk_manager is not None,
            ws_feed is not None,
            db is not None,
        )

    async def decide(self, signal: ScoredSignal) -> TradeDecision:
        open_symbols: List[str] = []
        try:
            open_positions_raw = await self._db.get_open_positions()
            open_symbols = [p.symbol for p in open_positions_raw]
        except Exception as exc:
            log.warning("TradeCommander.decide: gagal ambil open positions: %s", exc)

        base_risk_pct = float(self._config.get("risk_per_trade_pct", 1.0))

        decision = await decide(
            signal=signal,
            open_positions=open_symbols,
            portfolio_value=float(self._config.get("portfolio_value", 0.0)),
            base_risk_pct=base_risk_pct,
            min_regime_confidence=self._config.get("min_regime_confidence"),
            max_spread_pct_override=self._config.get("max_spread_pct"),
            exchange_connector=self._ws_feed,
            risk_manager=self._risk,
            db_manager=self._db,
        )

        try:
            sig = decision.scored_signal
            bd  = getattr(sig, "score_breakdown", None)
            await self._db.save_signal_score(
                symbol           = sig.symbol,
                strategy_profile = getattr(sig, "strategy_profile", ""),
                total_score      = float(getattr(sig, "total_score", 0)),
                trend_score      = float(getattr(bd, "trend",      50) if bd else 50),
                momentum_score   = float(getattr(bd, "momentum",   50) if bd else 50),
                strength_score   = float(getattr(bd, "strength",   50) if bd else 50),
                volatility_score = float(getattr(bd, "volatility", 50) if bd else 50),
                pattern_score    = float(getattr(bd, "pattern",    50) if bd else 50),
                oscillator_score = float(getattr(bd, "oscillator", 50) if bd else 50),
                structure_score  = float(getattr(bd, "structure",  50) if bd else 50),
                orderbook_score  = float(getattr(bd, "orderbook",  50) if bd else 50),
                regime           = getattr(sig, "regime", None),
                regime_confidence= float(getattr(sig, "regime_confidence", 0)),
                trigger_met      = decision.is_executable,
                action_taken     = decision.action.value if decision.action else None,
                rejection_reason = decision.rejection_reason or None,
            )
        except Exception as exc:
            log.debug("TradeCommander.decide: gagal save_signal_score: %s", exc)

        return decision

    async def decide_all(
        self,
        signals: Dict[str, ScoredSignal],
        open_positions: Optional[List[str]] = None,
    ) -> Dict[str, TradeDecision]:
        base_risk_pct = float(self._config.get("risk_per_trade_pct", 1.0))
        return await decide_all(
            signals=signals,
            open_positions=open_positions,
            portfolio_value=float(self._config.get("portfolio_value", 0.0)),
            base_risk_pct=base_risk_pct,
            min_regime_confidence=self._config.get("min_regime_confidence"),
            max_spread_pct_override=self._config.get("max_spread_pct"),
            exchange_connector=self._ws_feed,
            risk_manager=self._risk,
            db_manager=self._db,
        )


class IntelligenceCommander:

    def __init__(self, db, config: dict):
        self._db     = db
        self._config = config or {}
        log.info(
            "IntelligenceCommander init | intelligence=%s regime=%s",
            self._config.get("intelligence_enabled", True),
            self._config.get("regime_detection_enabled", True),
        )

    async def process(
        self,
        symbol:   str,
        df,
        strategy,
        confirmation_df=None,
        confirmation_timeframe: Optional[str] = None,
    ):
        from strategy import SignalEvent, SignalType, ExitMode
        from datetime import datetime, timezone

        def _utcnow():
            return datetime.now(timezone.utc).replace(tzinfo=None)

        signals = []

        try:
            exit_signals = await strategy.generate_signals(symbol, df)
            signals.extend(exit_signals)
        except Exception as exc:
            log.error(
                "Commander.process: generate_signals error [%s]: %s",
                symbol, exc, exc_info=True,
            )

        try:
            scored = await strategy.get_scored_signal(
                symbol,
                df,
                confirmation_df=confirmation_df,
                confirmation_timeframe=confirmation_timeframe,
            )
        except Exception as exc:
            log.warning(
                "Commander.process: get_scored_signal error [%s]: %s — "
                "skip entry evaluation.",
                symbol, exc,
            )
            return signals

        if scored is None:
            log.debug(
                "Commander.process: [%s] ScoredSignal=None — no entry candidate.",
                symbol,
            )
            return signals

        try:
            open_positions_raw = await self._db.get_open_positions()
            open_symbols       = [p.symbol for p in open_positions_raw]
            portfolio_value    = 0.0

        except Exception as exc:
            log.warning(
                "Commander.process: gagal ambil open positions: %s", exc
            )
            open_symbols    = []
            portfolio_value = 0.0

        if symbol in open_symbols:
            log.debug(
                "Commander.process: [%s] already in position — skip entry.",
                symbol,
            )
            return signals

        base_risk_pct = float(
            self._config.get("risk_per_trade_pct", 1.0)
        )

        try:
            decision = await decide(
                signal=scored,
                open_positions=open_symbols,
                portfolio_value=portfolio_value,
                base_risk_pct=base_risk_pct,
                exchange_connector=getattr(self, "_exchange", None),
                risk_manager=getattr(self, "_risk", None),
                db_manager=self._db,
            )
        except Exception as exc:
            log.error(
                "Commander.process: decide() error [%s]: %s",
                symbol, exc, exc_info=True,
            )
            return signals

        log.debug(
            "Commander.process: [%s] decision=%s score=%.1f gates=%d/%d",
            symbol,
            decision.action.value,
            scored.total_score,
            len(decision.gates_passed),
            len(decision.gates_passed) + len(decision.gates_failed),
        )

        bd = scored.score_breakdown

        def _bd_raw(attr: str) -> Optional[float]:
            return getattr(bd, attr, None) if bd is not None else None

        if decision.is_executable:
            try:
                iset  = scored.observation.primary_tf_indicators
                price = (iset.current_price if iset else 0.0) or 0.0

                if price <= 0:
                    log.warning(
                        "Commander.process: [%s] EXECUTE tapi price=0 — "
                        "skip BUY signal.",
                        symbol,
                    )
                    return signals

                profile_obj = strategy.get_profile(symbol)
                atr         = iset.volatility.atr if iset else 0.0
                atr_pct     = (atr / price * 100) if price > 0 else 0.0
                atr_thresh  = (
                    profile_obj.atr_pct_threshold
                    if profile_obj else 0.8
                )
                use_ride = atr_pct >= atr_thresh

                buy_sig = SignalEvent(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    price=price,
                    timestamp=_utcnow(),
                    strategy=strategy.name,
                    confidence=scored.total_score / 100.0,
                    stop_loss=decision.final_sl,
                    take_profit=decision.final_tp,
                    size_pct=decision.position_size_pct,
                    total_score=scored.total_score,
                    regime=scored.regime.value,
                    score_breakdown={
                        "trend":      _bd_raw("trend_raw"),
                        "momentum":   _bd_raw("momentum_raw"),
                        "strength":   _bd_raw("strength_raw"),
                        "volatility": _bd_raw("volatility_raw"),
                        "pattern":    _bd_raw("pattern_raw"),
                    },
                    scoring_narrative=scored.scoring_narrative,
                    metadata={
                        "entry_trigger":    "intelligence_pipeline_v7",
                        "pipeline_mode":    "v7",
                        "gates_passed":     decision.gates_passed,
                        "gates_failed":     decision.gates_failed,
                        "kelly_method":     decision.kelly_method_used,
                        "correlated":       decision.correlated_symbols,
                        "correlation_pen":  decision.correlation_penalty,
                        "exit_mode":        "RIDE_THE_WAVE" if use_ride
                                            else "QUICK_PROFIT",
                    },
                )

                p = strategy._resolve_params(symbol, price, atr, 1.0, 50.0)
                exit_mode_enum = (
                    ExitMode.RIDE_THE_WAVE
                    if use_ride else ExitMode.QUICK_PROFIT
                )
                strategy.register_position(
                    symbol=symbol,
                    entry_price=price,
                    exit_mode=exit_mode_enum,
                    p=p,
                    entry_score=scored.total_score,
                    entry_regime=scored.regime.value,
                )

                signals.append(buy_sig)
                log.info(
                    "🟢 Commander EXECUTE [%s] score=%.1f regime=%s "
                    "SL=%.6f TP=%.6f size=%.2f%%",
                    symbol,
                    scored.total_score,
                    scored.regime.value,
                    decision.final_sl or 0,
                    decision.final_tp or 0,
                    decision.position_size_pct,
                )

                try:
                    await self._db.save_signal_score(
                        symbol=symbol,
                        strategy_profile=scored.strategy_profile,
                        total_score=scored.total_score,
                        trend_score=_bd_raw("trend_raw"),
                        momentum_score=_bd_raw("momentum_raw"),
                        strength_score=_bd_raw("strength_raw"),
                        volatility_score=_bd_raw("volatility_raw"),
                        pattern_score=_bd_raw("pattern_raw"),
                        threshold_used=scored.threshold_used,
                        regime=scored.regime.value,
                        regime_confidence=scored.regime_confidence,
                        trigger_met=scored.trigger_met,
                        signal_type="buy",
                        action_taken="EXECUTE",
                        rejection_reason=None,
                    )
                except Exception as db_exc:
                    log.debug(
                        "Commander.process: gagal save signal_score: %s", db_exc
                    )

            except Exception as exc:
                log.error(
                    "Commander.process: gagal buat BUY SignalEvent [%s]: %s",
                    symbol, exc, exc_info=True,
                )

        else:
            action_str = decision.action.value.upper()
            log.debug(
                "Commander.process: [%s] %s | score=%.1f | reason=%s",
                symbol,
                action_str,
                scored.total_score,
                (decision.rejection_reason or "")[:80],
            )

            try:
                await self._db.save_signal_score(
                    symbol=symbol,
                    strategy_profile=scored.strategy_profile,
                    total_score=scored.total_score,
                    trend_score=_bd_raw("trend_raw"),
                    momentum_score=_bd_raw("momentum_raw"),
                    strength_score=_bd_raw("strength_raw"),
                    volatility_score=_bd_raw("volatility_raw"),
                    pattern_score=_bd_raw("pattern_raw"),
                    threshold_used=scored.threshold_used,
                    regime=scored.regime.value,
                    regime_confidence=scored.regime_confidence,
                    trigger_met=scored.trigger_met,
                    signal_type="hold",
                    action_taken=action_str,
                    rejection_reason=decision.rejection_reason,
                )
            except Exception as db_exc:
                log.debug(
                    "Commander.process: gagal save signal_score (non-exec): %s",
                    db_exc,
                )

        return signals

    def inject_dependencies(
        self,
        exchange_connector=None,
        risk_manager=None,
    ) -> None:
        self._exchange = exchange_connector
        self._risk     = risk_manager
        if exchange_connector:
            log.info("Commander: exchange connector injected.")
        if risk_manager:
            log.info("Commander: risk manager injected.")