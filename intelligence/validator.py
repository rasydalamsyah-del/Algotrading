"""
intelligence/validator.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from constants import (
    OBSERVATION_STALE_THRESHOLD_SECONDS,
    SCORE_NEUTRAL,
    SPREAD_LIMIT_DEFAULT,
)
from core.models import (
    IndicatorSet,
    MarketRegime,
    ObservationReport,
    PatternContext,
    ScoredSignal,
    clamp_score,
)

log = logging.getLogger("intelligence.validator")

@dataclass
class ValidationResult:
    passed: bool = True
    confidence_adjustment: float = 0.0
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hard_reject: bool = False
    hard_reject_reason: str = ""

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def add_warning(self, warning: str, confidence_penalty: float = 0.0) -> None:
        self.warnings.append(warning)
        self.confidence_adjustment -= abs(confidence_penalty)

    def reject(self, reason: str) -> None:
        self.hard_reject = True
        self.hard_reject_reason = reason
        self.passed = False
        self.notes.append(f"HARD REJECT: {reason}")

    @property
    def summary(self) -> str:
        parts = []
        if self.hard_reject:
            parts.append(f"❌ REJECTED: {self.hard_reject_reason}")
        elif self.passed:
            parts.append("✅ Passed")
        else:
            parts.append("⚠️ Warnings")
        if self.warnings:
            parts.append(f"Warnings ({len(self.warnings)}): {'; '.join(self.warnings[:3])}")
        if self.confidence_adjustment < 0:
            parts.append(f"Confidence adj: {self.confidence_adjustment:+.2f}")
        return " | ".join(parts)

def _check_rsi_divergence(iset: IndicatorSet, result: ValidationResult) -> None:
    div = iset.momentum.rsi_divergence
    if div is None or div == 0.0:
        return

    if div > 0:
        result.add_note(f"✅ RSI bullish divergence terdeteksi ({div:.1f}) — konfirmasi sinyal")
        result.confidence_adjustment += 0.05
    elif div < 0:
        result.add_warning(
            f"RSI bearish divergence ({div:.1f}) — berlawanan dengan sinyal BUY",
            confidence_penalty=0.08,
        )

def _check_macd_divergence(iset: IndicatorSet, result: ValidationResult) -> None:
    div = iset.momentum.macd_divergence
    if div is None or div == 0.0:
        return

    if div > 0:
        result.add_note(f"✅ MACD bullish divergence ({div:.1f})")
        result.confidence_adjustment += 0.03
    elif div < 0:
        result.add_warning(
            f"MACD bearish divergence ({div:.1f})",
            confidence_penalty=0.05,
        )

def _check_support_resistance_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    context = iset.patterns.pattern_context

    if context == PatternContext.NEAR_SUPPORT:
        result.add_note("✅ Harga dekat support — risk/reward favorable untuk entry")
        result.confidence_adjustment += 0.04

    elif context == PatternContext.NEAR_RESISTANCE:
        dist_to_res = iset.patterns.distance_to_resistance
        dist_str = f"{dist_to_res:.2f}%" if dist_to_res else "unknown"
        result.add_warning(
            f"Harga dekat resistance ({dist_str}) — potensi terhalang, "
            f"risk/reward kurang optimal",
            confidence_penalty=0.06,
        )

    elif context == PatternContext.MID_RANGE:
        result.add_note("📍 Harga di mid-range — context netral")

    else:
        result.add_note("❓ Context support/resistance tidak bisa ditentukan")

def _check_higher_tf_alignment(
    observation: ObservationReport,
    result: ValidationResult,
) -> None:
    if not observation.confirmation_tf_valid:
        result.add_note(
            "⚠️ Confirmation TF tidak tersedia atau tidak valid — "
            "sinyal tidak punya konfirmasi higher TF"
        )
        result.confidence_adjustment -= 0.05
        return

    conf_score = observation.confirmation_tf_score

    if conf_score >= 60.0:
        result.add_note(
            f"✅ Higher TF align (conf_score={conf_score:.1f}) — "
            f"sinyal didukung timeframe lebih besar"
        )
        result.confidence_adjustment += 0.06

    elif conf_score <= 40.0:
        result.add_warning(
            f"Higher TF BEARISH (conf_score={conf_score:.1f}) — "
            f"sinyal berlawanan dengan trend di TF lebih besar",
            confidence_penalty=0.12,
        )
    else:
        result.add_note(
            f"📊 Higher TF neutral (conf_score={conf_score:.1f})"
        )

def _check_volume_climax(iset: IndicatorSet, result: ValidationResult) -> None:
    if iset.strength.volume_climax:
        vol_ratio = iset.strength.volume_ratio or 0
        result.add_warning(
            f"⚠️ Volume climax terdeteksi ({vol_ratio:.1f}x) — "
            f"potensi exhaustion/pembalikan, bukan kelanjutan trend",
            confidence_penalty=0.10,
        )

    if iset.patterns.secondary_pattern is not None:
        from core.models import PatternType
        if iset.patterns.secondary_pattern == PatternType.VOLUME_CLIMAX:
            result.add_warning(
                "Volume climax pattern sebagai secondary signal",
                confidence_penalty=0.05,
            )

def _check_consecutive_losses(
    symbol: str,
    profile_name: str,
    result: ValidationResult,
    db_manager=None,
    max_consecutive: int = 3,
) -> None:
    if db_manager is None:
        return

    try:
        import asyncio
        try:
            recent_trades = asyncio.run(db_manager.get_recent_trades(
                symbol=symbol, profile=profile_name, limit=max_consecutive + 3,
            ))
        except Exception:
            return
        if not recent_trades:
            return

        consecutive_losses = 0
        for trade in sorted(recent_trades, key=lambda t: t.get("closed_at", ""), reverse=True):
            pnl = trade.get("pnl_pct", 0.0)
            if pnl is None:
                break
            if pnl < 0:
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= max_consecutive:
            penalty = min(0.20, consecutive_losses * 0.05)
            result.add_warning(
                f"{consecutive_losses} consecutive losses untuk {symbol}/{profile_name} — "
                f"fatigue penalty diterapkan (confidence -{penalty:.0%})",
                confidence_penalty=penalty,
            )
            if consecutive_losses >= max_consecutive + 2:
                result.add_warning(
                    f"⚠️ Pertimbangkan pause trading {symbol} "
                    f"sementara untuk evaluasi kondisi market"
                )
        elif consecutive_losses > 0:
            result.add_note(f"📊 {consecutive_losses} loss terakhir untuk {symbol}/{profile_name}")

    except Exception as exc:
        log.debug("Gagal check consecutive losses (non-critical): %s", exc)

def _check_data_staleness(
    observation: ObservationReport,
    result: ValidationResult,
    stale_threshold_secs: float = OBSERVATION_STALE_THRESHOLD_SECONDS,
) -> None:
    age_secs = (datetime.utcnow() - observation.observed_at).total_seconds()

    if age_secs > stale_threshold_secs:
        result.add_warning(
            f"Data stale: observasi {age_secs:.0f}s yang lalu "
            f"(threshold {stale_threshold_secs:.0f}s) — "
            f"sinyal mungkin tidak mencerminkan kondisi terkini",
            confidence_penalty=0.08,
        )
    elif age_secs > stale_threshold_secs * 0.7:
        result.add_note(
            f"⏰ Data mendekati stale ({age_secs:.0f}s / {stale_threshold_secs:.0f}s threshold)"
        )

def _check_indicator_errors(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    if not iset.calculation_errors:
        return

    critical_keywords = ["ema9", "ema21", "rsi", "atr"]
    critical_errors = [
        e for e in iset.calculation_errors
        if any(kw in e.lower() for kw in critical_keywords)
    ]
    non_critical = [e for e in iset.calculation_errors if e not in critical_errors]

    if critical_errors:
        result.reject(
            f"Indikator kritis gagal dihitung: {critical_errors[:3]}"
        )
        return

    if non_critical:
        result.add_warning(
            f"{len(non_critical)} indikator non-kritis gagal: "
            f"{non_critical[:2]}",
            confidence_penalty=0.03 * len(non_critical),
        )

def _check_atr_threshold(
    iset: IndicatorSet,
    profile_cfg,
    result: ValidationResult,
) -> None:
    atr_pct = iset.volatility.atr_pct
    if atr_pct is None:
        return

    min_atr = getattr(profile_cfg, "atr_pct_threshold", 0.3)

    if atr_pct < min_atr:
        result.add_warning(
            f"ATR% {atr_pct:.3f}% < minimum {min_atr:.3f}% — "
            f"volatilitas terlalu rendah, spread/fee bisa dominasi P&L",
            confidence_penalty=0.07,
        )
    else:
        result.add_note(f"✅ ATR% {atr_pct:.3f}% ≥ minimum {min_atr:.3f}%")

def _check_squeeze_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    squeeze_active = iset.volatility.squeeze_active
    squeeze_bars   = iset.volatility.squeeze_bars

    if not squeeze_active and squeeze_bars < 0:
        bars_ago = abs(squeeze_bars)
        if bars_ago <= 2:
            result.add_note(
                f"🔥 Baru keluar dari squeeze ({bars_ago} bar lalu) — "
                f"potensi breakout kuat"
            )
            result.confidence_adjustment += 0.05

    elif squeeze_active and squeeze_bars > 15:
        result.add_note(
            f"⏳ Squeeze sudah {squeeze_bars} bar — "
            f"bisa berlanjut lebih lama sebelum breakout"
        )

def _check_oscillator_context(iset: IndicatorSet, result: ValidationResult) -> None:
    """CCI, Williams %R, ROC — early warning momentum & overbought/oversold."""
    osc = iset.oscillators
    if not osc.is_valid():
        return

    # ── CCI ───────────────────────────────────────────────────────────────────
    if osc.cci is not None:
        if osc.cci > 150:
            result.add_warning(
                f"CCI {osc.cci:.1f} — ekstrem overbought, potensi pullback",
                confidence_penalty=0.06,
            )
        elif osc.cci < -100:
            result.add_note(f"✅ CCI {osc.cci:.1f} — oversold, mendukung entry long")
            result.confidence_adjustment += 0.03
        elif 0 < osc.cci < 100:
            result.add_note(f"✅ CCI {osc.cci:.1f} — zona bullish sehat")
            result.confidence_adjustment += 0.02

    # [v2] CCI trend — arah pergerakan indikator lebih penting dari nilai sesaat
    if osc.cci_trend == "rising" and osc.cci is not None and osc.cci < 0:
        result.add_note(
            f"✅ CCI rising ({osc.cci:.1f}) dari zona negatif — potensi recovery"
        )
        result.confidence_adjustment += 0.02
    elif osc.cci_trend == "falling" and osc.cci is not None and osc.cci > 50:
        result.add_warning(
            f"CCI falling ({osc.cci:.1f}) dari zona positif — momentum melemah",
            confidence_penalty=0.03,
        )

    # [v2] CCI divergence — early reversal signal
    if osc.cci_divergence is not None:
        if osc.cci_divergence > 5:
            result.add_note(
                f"✅ CCI bullish divergence ({osc.cci_divergence:.1f}) — potensi reversal up"
            )
            result.confidence_adjustment += 0.04
        elif osc.cci_divergence < -5:
            result.add_warning(
                f"CCI bearish divergence ({osc.cci_divergence:.1f}) — potensi reversal down",
                confidence_penalty=0.04,
            )

    # ── Williams %R ───────────────────────────────────────────────────────────
    if osc.williams_r is not None:
        if osc.williams_r >= -20:
            result.add_warning(
                f"Williams %R {osc.williams_r:.1f} — overbought zone",
                confidence_penalty=0.04,
            )
        elif osc.williams_r <= -80:
            result.add_note(f"✅ Williams %R {osc.williams_r:.1f} — oversold, momentum recovery")
            result.confidence_adjustment += 0.02

    # [v2] Williams %R trend — apakah bergerak keluar dari oversold?
    if osc.willr_trend == "rising" and osc.williams_r is not None and osc.williams_r <= -70:
        result.add_note(
            f"✅ Williams %R rising dari oversold ({osc.williams_r:.1f}) — sinyal recovery"
        )
        result.confidence_adjustment += 0.02
    elif osc.willr_trend == "falling" and osc.williams_r is not None and osc.williams_r >= -30:
        result.add_warning(
            f"Williams %R jatuh dari overbought ({osc.williams_r:.1f}) — tekanan jual",
            confidence_penalty=0.03,
        )

    # ── ROC — early warning momentum ──────────────────────────────────────────
    if osc.roc is not None and osc.roc_slope is not None:
        if osc.roc > 0 and osc.roc_slope < -1.5:
            result.add_warning(
                f"ROC positif ({osc.roc:.2f}%) tapi melambat (slope={osc.roc_slope:.2f}) "
                f"— momentum mulai habis",
                confidence_penalty=0.05,
            )
        elif osc.roc > 0 and osc.roc_slope > 1.0:
            result.add_note(
                f"✅ ROC {osc.roc:.2f}% akselerasi (slope={osc.roc_slope:.2f}) "
                f"— momentum menguat"
            )
            result.confidence_adjustment += 0.03

    # [v2] ROC fast/slow crossover
    if osc.roc_crossover == "bullish":
        result.add_note(
            f"✅ ROC crossover bullish (fast={osc.roc:.2f}% > slow={osc.roc_slow:.2f}%) "
            f"— momentum shift positif"
        )
        result.confidence_adjustment += 0.03
    elif osc.roc_crossover == "bearish":
        result.add_warning(
            f"ROC crossover bearish (fast={osc.roc:.2f}% < slow={osc.roc_slow:.2f}%) "
            f"— momentum shift negatif",
            confidence_penalty=0.04,
        )


def _check_structure_context(iset: IndicatorSet, result: ValidationResult) -> None:
    """Ichimoku, SAR, Pivot, Fibonacci — posisi harga terhadap struktur pasar."""
    st = iset.structure
    if not st.is_valid():
        return

    price = iset.current_price
    if not price or price <= 0:
        return

    # Ichimoku — posisi vs cloud
    if st.price_vs_cloud == "above":
        result.add_note("✅ Ichimoku: harga di atas cloud — trend bullish terkonfirmasi")
        result.confidence_adjustment += 0.04
        if st.cloud_thickness and st.cloud_thickness / price > 0.015:
            result.add_note("✅ Cloud tebal — support kuat di bawah harga")
            result.confidence_adjustment += 0.02
    elif st.price_vs_cloud == "below":
        result.add_warning(
            "Ichimoku: harga di bawah cloud — trend bearish, entry long berisiko",
            confidence_penalty=0.08,
        )
    elif st.price_vs_cloud == "inside":
        result.add_warning(
            "Ichimoku: harga dalam cloud — pasar ragu, sinyal lemah",
            confidence_penalty=0.04,
        )

    if st.tk_cross == "bullish":
        result.add_note("✅ Ichimoku TK Cross bullish — momentum entry terkonfirmasi")
        result.confidence_adjustment += 0.03
    elif st.tk_cross == "bearish":
        result.add_warning(
            "Ichimoku TK Cross bearish — momentum berbalik",
            confidence_penalty=0.06,
        )

    # Parabolic SAR
    if st.sar_direction == "up":
        result.add_note(f"✅ SAR uptrend (${st.sar_value:.6f}) — trailing support aktif")
        result.confidence_adjustment += 0.02
    elif st.sar_direction == "down":
        result.add_warning(
            f"SAR downtrend (${st.sar_value:.6f}) — harga di bawah SAR, hindari entry long",
            confidence_penalty=0.07,
        )

    # Pivot Points — posisi vs nearest resistance
    if st.nearest_resistance and price > 0:
        dist_res_pct = (st.nearest_resistance - price) / price * 100
        if dist_res_pct < 1.0:
            result.add_warning(
                f"Pivot: harga hanya {dist_res_pct:.2f}% dari resistance "
                f"(${st.nearest_resistance:.6f}) — upside sangat terbatas",
                confidence_penalty=0.08,
            )
        elif dist_res_pct < 2.0:
            result.add_warning(
                f"Pivot: resistance dekat ({dist_res_pct:.2f}%) — "
                f"perhatikan R/R",
                confidence_penalty=0.03,
            )
        elif dist_res_pct > 4.0:
            result.add_note(
                f"✅ Pivot: ruang gerak {dist_res_pct:.2f}% sebelum resistance — "
                f"R/R favorable"
            )
            result.confidence_adjustment += 0.02

    if st.nearest_support and price > 0:
        dist_sup_pct = (price - st.nearest_support) / price * 100
        if dist_sup_pct < 1.5:
            result.add_note(
                f"✅ Pivot: harga dekat support (${st.nearest_support:.6f}, "
                f"{dist_sup_pct:.2f}%) — zona entry ideal"
            )
            result.confidence_adjustment += 0.03

    # Fibonacci — level kunci
    if st.fib_618 and price > 0:
        dist_fib_pct = abs(price - st.fib_618) / price * 100
        if dist_fib_pct < 0.8:
            result.add_note(
                f"✅ Fibonacci: harga di golden ratio 61.8% "
                f"(${st.fib_618:.6f}) — level support/resistance terkuat"
            )
            result.confidence_adjustment += 0.04

    if st.nearest_fib_resistance and price > 0:
        dist_fib_res = (st.nearest_fib_resistance - price) / price * 100
        if dist_fib_res < 1.5:
            result.add_warning(
                f"Fibonacci: resistance Fib dekat ({dist_fib_res:.2f}%) — "
                f"upside terbatas",
                confidence_penalty=0.04,
            )


def _check_orderbook_context(iset: IndicatorSet, result: ValidationResult) -> None:
    """Orderbook imbalance & whale wall — tekanan beli/jual real-time."""
    ob = iset.orderbook
    if not ob.is_valid():
        return

    # Bid/Ask imbalance
    imb = ob.bid_ask_imbalance
    if imb is not None:
        if imb >= 0.65:
            result.add_note(
                f"✅ Orderbook: bid dominan ({imb:.2f}) — "
                f"tekanan beli kuat, mendukung entry"
            )
            result.confidence_adjustment += 0.04
        elif imb <= 0.35:
            result.add_warning(
                f"Orderbook: ask dominan ({imb:.2f}) — "
                f"tekanan jual kuat, hati-hati entry long",
                confidence_penalty=0.08,
            )
        elif imb >= 0.55:
            result.add_note(f"📊 Orderbook: sedikit condong bid ({imb:.2f})")

    # Whale ask wall — resistance besar
    if ob.whale_ask_wall and ob.ask_wall_strength:
        result.add_warning(
            f"Whale ask wall di ${ob.whale_ask_wall:.6f} "
            f"({ob.ask_wall_strength:.1f}% volume) — "
            f"resistance kuat dari whale",
            confidence_penalty=0.06,
        )

    # Whale bid wall — support besar
    if ob.whale_bid_wall and ob.bid_wall_strength:
        result.add_note(
            f"✅ Whale bid wall di ${ob.whale_bid_wall:.6f} "
            f"({ob.bid_wall_strength:.1f}% volume) — "
            f"support kuat dari whale"
        )
        result.confidence_adjustment += 0.03

    # Absorbed ask wall = breakout signal
    if ob.absorbed_ask:
        result.add_note(
            "✅ Orderbook: whale ask wall terserap — "
            "breakout signal kuat"
        )
        result.confidence_adjustment += 0.05

    # Spread lebar = likuiditas buruk
    if ob.spread_pct and ob.spread_pct > 0.15:
        result.add_warning(
            f"Spread lebar ({ob.spread_pct:.3f}%) — "
            f"likuiditas rendah, fee/slippage bisa besar",
            confidence_penalty=0.03,
        )


def validate_signal(
    signal: ScoredSignal,
    db_manager=None,
    max_consecutive_losses: int = 3,
) -> ValidationResult:
    result = ValidationResult()
    observation = signal.observation
    iset = observation.primary_tf_indicators

    if iset is None:
        result.reject("Primary indicator set tidak tersedia")
        return result

    _check_indicator_errors(iset, result)
    if result.hard_reject:
        return result

    _check_data_staleness(observation, result)

    try:
        from profiles.thresholds import get_profile_thresholds
        profile_cfg = get_profile_thresholds(signal.strategy_profile)
        _check_atr_threshold(iset, profile_cfg, result)
        consecutive_max = getattr(profile_cfg, "max_consecutive_losses", max_consecutive_losses)
    except Exception:
        profile_cfg = None
        consecutive_max = max_consecutive_losses

    _check_rsi_divergence(iset, result)

    _check_macd_divergence(iset, result)

    _check_support_resistance_context(iset, result)

    _check_higher_tf_alignment(observation, result)

    _check_volume_climax(iset, result)

    _check_squeeze_context(iset, result)

    _check_oscillator_context(iset, result)

    _check_structure_context(iset, result)

    _check_orderbook_context(iset, result)

    _check_consecutive_losses(
        symbol=signal.symbol,
        profile_name=signal.strategy_profile,
        result=result,
        db_manager=db_manager,
        max_consecutive=consecutive_max,
    )

    result.confidence_adjustment = max(-0.40, min(0.20, result.confidence_adjustment))

    log.debug(
        "%s: Validation | passed=%s hard_reject=%s conf_adj=%.2f | "
        "notes=%d warnings=%d",
        signal.symbol,
        result.passed,
        result.hard_reject,
        result.confidence_adjustment,
        len(result.notes),
        len(result.warnings),
    )

    return result

def apply_validation(signal: ScoredSignal, result: ValidationResult) -> ScoredSignal:
    new_confidence = signal.confidence + result.confidence_adjustment
    signal.confidence = round(max(0.0, min(1.0, new_confidence)), 3)

    for note in result.notes:
        signal.add_validation_note(note)
    for warning in result.warnings:
        signal.add_validation_note(f"⚠️ {warning}")

    if result.hard_reject:
        signal.trigger_met = False
        signal.signal_type = "hold"
        signal.add_validation_note(f"HARD REJECT: {result.hard_reject_reason}")
        # Tambahkan ke narrative
        signal.scoring_narrative = (
            f"❌ VALIDATOR REJECT: {result.hard_reject_reason}\n"
            + signal.scoring_narrative
        )

    return signal

def validate_and_apply(
    signal: ScoredSignal,
    db_manager=None,
) -> Tuple[ScoredSignal, ValidationResult]:
    result = validate_signal(signal, db_manager=db_manager)
    updated_signal = apply_validation(signal, result)
    return updated_signal, result


def summarize_validation(result: ValidationResult) -> str:
    lines = [f"Validator: {result.summary}"]
    if result.notes:
        lines.append(f"  Notes: {len(result.notes)} item")
    if result.warnings:
        lines.append(f"  Warnings: {'; '.join(result.warnings[:3])}")
    return "\n".join(lines)


class SignalValidator:
    def __init__(self, db=None):
        self._db = db

    def validate(self, signal: ScoredSignal) -> ValidationResult:
        return validate_signal(signal, db_manager=self._db)
