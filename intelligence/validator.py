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


def _check_bb_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Memanfaatkan bb_middle, bb_position, bb_trending yang sebelumnya idle.

    bb_middle  → support dinamis: close di atas middle = bullish bias
    bb_position → posisi relatif dalam band [0,1]: < 0.35 = buy zone
    bb_trending  → arah lebar band: contracting = setup squeeze pre-breakout
    """
    vol = iset.volatility
    if vol.bb_middle is None or vol.bb_position is None:
        return

    # -- bb_middle sebagai dynamic support --
    price = iset.current_price
    if price and vol.bb_middle > 0:
        pct_above = (price - vol.bb_middle) / vol.bb_middle * 100
        if pct_above < -1.0:
            result.add_warning(
                f"Harga {pct_above:.1f}% di bawah BB middle ({vol.bb_middle:.4f}) — "
                f"dynamic support tertembus, bullish bias melemah",
                confidence_penalty=0.04,
            )
        elif 0 <= pct_above <= 2.0:
            result.add_note(
                f"✅ Harga tepat di atas BB middle (+{pct_above:.1f}%) — "
                f"dynamic support terjaga, entry zone valid"
            )

    # -- bb_position: posisi dalam band --
    pos = vol.bb_position
    if pos <= 0.25:
        result.add_note(
            f"✅ BB position {pos:.2f} (lower zone) — "
            f"harga di area oversold band, risk/reward optimal"
        )
        result.confidence_adjustment += 0.03
    elif pos >= 0.85:
        result.add_warning(
            f"BB position {pos:.2f} (upper extreme) — "
            f"harga mendekati upper band, potensi resistance dan mean-reversion",
            confidence_penalty=0.05,
        )

    # -- bb_trending: arah lebar band --
    trend = vol.bb_trending or "flat"
    if trend == "contracting" and pos <= 0.5:
        result.add_note(
            f"🔥 BB contracting + posisi lower half ({pos:.2f}) — "
            f"energy terakumulasi, setup pre-breakout ideal"
        )
        result.confidence_adjustment += 0.04
    elif trend == "expanding" and pos >= 0.75:
        result.add_warning(
            f"BB expanding saat harga di upper zone ({pos:.2f}) — "
            f"potensi blow-off, waspada exhaustion",
            confidence_penalty=0.04,
        )


def _check_kc_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Memanfaatkan kc_upper, kc_lower, kc_middle, kc_score yang sebelumnya idle.

    Keltner Channel memberi konteks berbeda dari BB: berbasis ATR (bukan std dev),
    lebih stabil saat volatilitas spike. Kombinasi KC+BB memberi sinyal squeeze
    dan posisi relatif yang lebih robust.
    kc_score  → skor posisi KC: < 40 = overbought KC, > 60 = near lower KC (bullish)
    kc_middle → EMA trend: harga vs KC middle = trend bias
    kc_upper/lower → range channel untuk context entry
    """
    vol = iset.volatility
    if vol.kc_upper is None or vol.kc_lower is None or vol.kc_middle is None:
        return

    price = iset.current_price
    if not price:
        return

    kc_range = vol.kc_upper - vol.kc_lower
    if kc_range <= 0:
        return

    kc_pos = (price - vol.kc_lower) / kc_range  # 0=lower, 0.5=middle, 1=upper

    # Posisi KC
    if kc_pos < 0.30:
        result.add_note(
            f"✅ Harga di lower KC zone ({kc_pos:.2f}) — "
            f"dekat EMA-ATR support, entry favorable"
        )
        result.confidence_adjustment += 0.03
    elif kc_pos > 0.80:
        result.add_warning(
            f"Harga di upper KC zone ({kc_pos:.2f}) — "
            f"dekat EMA-ATR resistance, room terbatas",
            confidence_penalty=0.04,
        )

    # Harga vs KC middle (EMA): jika di bawah middle, trend lemah
    if price < vol.kc_middle:
        pct = (vol.kc_middle - price) / vol.kc_middle * 100
        result.add_warning(
            f"Harga {pct:.1f}% di bawah KC middle (EMA={vol.kc_middle:.4f}) — "
            f"trend jangka menengah belum bullish",
            confidence_penalty=0.03,
        )

    # kc_score informatif: rendah = KC overbought
    if vol.kc_score is not None and vol.kc_score < 40:
        result.add_warning(
            f"KC score rendah ({vol.kc_score:.0f}) — "
            f"posisi dalam channel terlalu tinggi untuk entry optimal",
            confidence_penalty=0.03,
        )


def _check_macd_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Memanfaatkan macd_line, macd_signal, macd_hist_prev, macd_zero_cross.

    macd_line vs macd_signal → konfirmasi trend jangka menengah
    macd_hist_prev → arah momentum: apakah histogram sedang membaik?
    macd_zero_cross → event kritis: MACD baru saja cross above zero = strong bull signal
    """
    mom = iset.momentum
    if mom.macd_line is None or mom.macd_signal is None:
        return

    # MACD line vs signal: basic trend bias
    macd_above_signal = mom.macd_line > mom.macd_signal
    gap = mom.macd_line - mom.macd_signal

    if not macd_above_signal:
        result.add_warning(
            f"MACD line ({mom.macd_line:.5f}) di bawah signal ({mom.macd_signal:.5f}) — "
            f"momentum jangka menengah belum bullish",
            confidence_penalty=0.04,
        )
    elif abs(gap) > 0:
        result.add_note(
            f"✅ MACD line di atas signal (gap={gap:+.5f}) — "
            f"momentum jangka menengah bullish"
        )

    # macd_zero_cross: MACD baru saja naik melewati zero = sinyal kuat
    if mom.macd_zero_cross:
        result.add_note(
            "🚀 MACD zero cross bullish — MACD baru melewati zero dari bawah, "
            "momentum shift signifikan"
        )
        result.confidence_adjustment += 0.06

    # -- vwma_vs_sma: konfirmasi volume mendukung momentum --
    # [UPGRADE] field vwma dan vwma_vs_sma dari ta_compat.vwma()
    if mom.vwma is not None and mom.vwma_vs_sma is not None:
        diff = mom.vwma_vs_sma
        if diff > 1.5:
            result.add_note(
                f"✅ VWMA > SMA (+{diff:.2f}%) — volume lebih berat di bar bullish: "
                f"momentum dikonfirmasi oleh volume"
            )
            result.confidence_adjustment += 0.04
        elif diff > 0.5:
            result.add_note(
                f"✅ VWMA sedikit di atas SMA (+{diff:.2f}%) — "
                f"volume support moderat"
            )
            result.confidence_adjustment += 0.02
        elif diff < -1.5:
            result.add_warning(
                f"VWMA < SMA ({diff:.2f}%) — volume lebih berat di bar bearish: "
                f"momentum kurang dikonfirmasi volume, potensi fake breakout",
                confidence_penalty=0.05,
            )
    if mom.macd_histogram is not None and mom.macd_hist_prev is not None:
        improving = mom.macd_histogram > mom.macd_hist_prev
        if mom.macd_histogram < 0 and not improving:
            result.add_warning(
                f"MACD histogram negatif dan memburuk "
                f"({mom.macd_hist_prev:.5f} → {mom.macd_histogram:.5f}) — "
                f"selling pressure masih meningkat",
                confidence_penalty=0.05,
            )
        elif mom.macd_histogram < 0 and improving:
            result.add_note(
                f"⚡ MACD histogram negatif tapi membaik "
                f"({mom.macd_hist_prev:.5f} → {mom.macd_histogram:.5f}) — "
                f"early sign of momentum reversal"
            )
            result.confidence_adjustment += 0.03


def _check_stoch_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Memanfaatkan stoch_k, stoch_d, stoch_kd_cross, stoch_zone yang idle.

    StochRSI memberi konfirmasi overbought/oversold lebih sensitif dari RSI biasa.
    stoch_k, stoch_d → level saat ini
    stoch_kd_cross   → crossover K-D: sinyal entry/exit yang presisi
    stoch_zone       → oversold/overbought/neutral context
    """
    mom = iset.momentum
    if mom.stoch_k is None or mom.stoch_d is None:
        return

    k, d = mom.stoch_k, mom.stoch_d

    # Overbought zone = risk tinggi untuk entry baru
    if mom.stoch_zone == "overbought":
        result.add_warning(
            f"StochRSI overbought (K={k:.1f} D={d:.1f}) — "
            f"momentum sudah stretched, entry baru berisiko tinggi",
            confidence_penalty=0.06,
        )

    # Oversold + bullish KD cross = sinyal kuat
    elif mom.stoch_zone == "oversold":
        if mom.stoch_kd_cross == "bullish":
            result.add_note(
                f"🔥 StochRSI oversold + KD cross bullish (K={k:.1f} D={d:.1f}) — "
                f"konfirmasi kuat: momentum berbalik dari bottom"
            )
            result.confidence_adjustment += 0.07
        else:
            result.add_note(
                f"✅ StochRSI oversold (K={k:.1f} D={d:.1f}) — "
                f"potensi reversal, tunggu KD cross untuk konfirmasi"
            )
            result.confidence_adjustment += 0.03

    # Neutral zone: cek KD cross untuk directional bias
    elif mom.stoch_kd_cross == "bullish" and k > d:
        result.add_note(
            f"✅ StochRSI KD cross bullish di neutral zone (K={k:.1f} D={d:.1f}) — "
            f"momentum mulai membaik"
        )
        result.confidence_adjustment += 0.03
    elif mom.stoch_kd_cross == "bearish":
        result.add_warning(
            f"StochRSI KD cross bearish (K={k:.1f} D={d:.1f}) — "
            f"momentum momentum melemah",
            confidence_penalty=0.04,
        )

    # K masih di bawah D tanpa cross = momentum belum confirmed
    if k < d and mom.stoch_zone != "oversold":
        result.add_note(
            f"⚠️ StochRSI K ({k:.1f}) < D ({d:.1f}) — "
            f"momentum belum terkonfirmasi bullish"
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
    """[UPGRADE] Semua 22 field OrderbookIndicators kini aktif.

    Sebelumnya hanya 6 field terpakai. Sekarang:
    - imbalance_score      → skor kalkulasi imbalance bid/ask
    - cluster_bid/ask_wall → wall yang terbentuk dari cluster level (lebih reliable dari single)
    - bid/ask_wall_dist    → relevance factor: wall yang jauh = pengaruh kecil
    - absorbed_bid         → bid wall terserap = breakdown signal (simetri absorbed_ask)
    - whale_score          → sub-skor komposit whale activity
    - spread_score         → spread kontekstual vs baseline historis coin ini
    - absorption_score     → sub-skor absorption event
    - liquidity_score      → total depth USDT → apakah cukup likuid untuk entry?
    - spoofing_confidence  → berapa % wall yang kemungkinan genuine (bukan spoof)
    """
    ob = iset.orderbook
    if not ob.is_valid():
        return

    price = iset.current_price

    # ── Imbalance ─────────────────────────────────────────────────────────────
    imb = ob.bid_ask_imbalance
    if imb is not None:
        if imb >= 0.65:
            result.add_note(
                f"✅ Orderbook: bid dominan ({imb:.2f}, score={ob.imbalance_score:.0f}) — "
                f"tekanan beli kuat, mendukung entry"
            )
            result.confidence_adjustment += 0.04
        elif imb <= 0.35:
            result.add_warning(
                f"Orderbook: ask dominan ({imb:.2f}, score={ob.imbalance_score:.0f}) — "
                f"tekanan jual kuat, hati-hati entry long",
                confidence_penalty=0.08,
            )
        elif imb >= 0.55:
            result.add_note(f"📊 Orderbook: sedikit condong bid ({imb:.2f})")

    # ── Whale walls (single level) ────────────────────────────────────────────
    if ob.whale_ask_wall and ob.ask_wall_strength:
        dist_adj = ob.ask_wall_dist if ob.ask_wall_dist is not None else 1.0
        eff_str  = ob.ask_wall_strength * dist_adj
        result.add_warning(
            f"Whale ask wall di ${ob.whale_ask_wall:.6f} "
            f"({ob.ask_wall_strength:.1f}% vol, relevance={dist_adj:.2f}, eff={eff_str:.1f}) — "
            f"resistance dari whale{' (dekat harga, sangat relevan)' if dist_adj > 0.7 else ' (jauh, pengaruh kecil)'}",
            confidence_penalty=0.06 * dist_adj,
        )

    if ob.whale_bid_wall and ob.bid_wall_strength:
        dist_adj = ob.bid_wall_dist if ob.bid_wall_dist is not None else 1.0
        result.add_note(
            f"✅ Whale bid wall di ${ob.whale_bid_wall:.6f} "
            f"({ob.bid_wall_strength:.1f}% vol, relevance={dist_adj:.2f}) — "
            f"support kuat dari whale"
        )
        result.confidence_adjustment += 0.03 * dist_adj

    # ── Cluster walls (MSL-3): lebih reliable dari single wall ────────────────
    if ob.cluster_bid_wall and ob.cluster_bid_str:
        # Cluster berbeda dari whale wall = double support layer
        is_different = (ob.cluster_bid_wall != ob.whale_bid_wall)
        dist_adj = ob.bid_wall_dist if ob.bid_wall_dist is not None else 1.0
        if is_different:
            result.add_note(
                f"✅ Cluster bid wall di ${ob.cluster_bid_wall:.6f} "
                f"({ob.cluster_bid_str:.1f}% vol) — "
                f"support berlapis: whale + cluster di level berbeda"
            )
            result.confidence_adjustment += 0.03 * dist_adj
        else:
            result.add_note(
                f"✅ Bid wall dikonfirmasi cluster ({ob.cluster_bid_str:.1f}% vol) — "
                f"wall lebih genuine, bukan single order"
            )
            result.confidence_adjustment += 0.02

    if ob.cluster_ask_wall and ob.cluster_ask_str:
        is_different = (ob.cluster_ask_wall != ob.whale_ask_wall)
        dist_adj = ob.ask_wall_dist if ob.ask_wall_dist is not None else 1.0
        if is_different:
            result.add_warning(
                f"Cluster ask wall di ${ob.cluster_ask_wall:.6f} "
                f"({ob.cluster_ask_str:.1f}% vol) — "
                f"resistance berlapis: whale + cluster",
                confidence_penalty=0.04 * dist_adj,
            )

    # ── Absorption ────────────────────────────────────────────────────────────
    if ob.absorbed_ask:
        result.add_note(
            "🚀 Orderbook: whale ASK wall terserap — "
            f"breakout signal kuat (absorption_score={ob.absorption_score:.0f})"
        )
        result.confidence_adjustment += 0.06

    if ob.absorbed_bid:
        # [UPGRADE] absorbed_bid sebelumnya tidak dipakai — ini BEARISH signal
        result.add_warning(
            "⚠️ Orderbook: whale BID wall terserap — "
            f"breakdown signal: support dari whale gagal menahan tekanan jual "
            f"(absorption_score={ob.absorption_score:.0f})",
            confidence_penalty=0.07,
        )

    # ── Spoofing confidence ───────────────────────────────────────────────────
    sc = ob.spoofing_confidence
    if sc is not None and sc < 0.7:
        result.add_warning(
            f"⚠️ Spoofing confidence rendah ({sc:.2f}) — "
            f"banyak wall kemungkinan tidak genuine, data orderbook kurang reliable",
            confidence_penalty=0.05,
        )
    elif sc is not None and sc >= 0.90:
        result.add_note(
            f"✅ Spoofing confidence tinggi ({sc:.2f}) — "
            f"wall-wall orderbook kemungkinan besar genuine"
        )
        result.confidence_adjustment += 0.02

    # ── Liquidity score ───────────────────────────────────────────────────────
    liq = ob.liquidity_score
    if liq is not None:
        if liq < 35:
            result.add_warning(
                f"Likuiditas orderbook rendah (score={liq:.0f}) — "
                f"depth USDT tipis, slippage bisa besar untuk order ini",
                confidence_penalty=0.05,
            )
        elif liq >= 70:
            result.add_note(
                f"✅ Likuiditas orderbook baik (score={liq:.0f}) — "
                f"depth cukup untuk eksekusi bersih"
            )
            result.confidence_adjustment += 0.02

    # ── Spread score (kontekstual vs baseline historis coin) ─────────────────
    ssp = ob.spread_score
    if ssp is not None and ssp <= 40:
        result.add_warning(
            f"Spread tidak normal (score={ssp:.0f}) — "
            f"spread saat ini jauh di atas baseline historis coin ini, "
            f"kondisi likuiditas memburuk",
            confidence_penalty=0.04,
        )
    elif ssp is not None and ssp >= 80:
        result.add_note(
            f"✅ Spread normal/bagus (score={ssp:.0f}) — "
            f"spread dalam range historis coin ini"
        )

    # ── Whale score composite ─────────────────────────────────────────────────
    ws = ob.whale_score
    if ws is not None:
        if ws >= 65:
            result.add_note(
                f"✅ Whale score {ws:.0f} — "
                f"aktivitas whale net bullish, mendukung entry"
            )
            result.confidence_adjustment += 0.02
        elif ws <= 35:
            result.add_warning(
                f"Whale score {ws:.0f} — "
                f"aktivitas whale net bearish",
                confidence_penalty=0.04,
            )


def _check_trend_cross_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan golden_cross_bars_ago, dead_cross_bars_ago, supertrend_value.

    golden_cross_bars_ago → freshness: cross baru (≤10) = momentum kuat,
                             stale (>50) = sudah terlambat, konfirmasi lemah
    dead_cross_bars_ago   → bearish event baru-baru ini = waspada entry long
    supertrend_value      → level harga ST line = dynamic support/resistance
                             yang bisa dipakai untuk contextual SL reference
    """
    tr = iset.trend
    if tr is None:
        return

    price = iset.current_price

    # -- Golden cross freshness --
    gc = tr.golden_cross_bars_ago
    if gc is not None:
        if gc <= 5:
            result.add_note(
                f"🚀 Golden cross SEGAR ({gc} bar lalu) — "
                f"momentum bullish dalam puncak kekuatan"
            )
            result.confidence_adjustment += 0.07
        elif gc <= 15:
            result.add_note(
                f"✅ Golden cross masih fresh ({gc} bar lalu) — "
                f"momentum bullish terkonfirmasi"
            )
            result.confidence_adjustment += 0.04
        elif gc <= 50:
            result.add_note(
                f"📊 Golden cross {gc} bar lalu — "
                f"trend bullish established, momentum mulai melambat"
            )

    # -- Dead cross: sinyal bearish baru-baru ini = penalti entry long --
    dc = tr.dead_cross_bars_ago
    if dc is not None:
        if dc <= 5:
            result.add_warning(
                f"⚠️ Dead cross SEGAR ({dc} bar lalu) — "
                f"momentum bearish baru dimulai, hindari long",
                confidence_penalty=0.09,
            )
        elif dc <= 20:
            result.add_warning(
                f"Dead cross {dc} bar lalu — "
                f"trend bearish masih aktif, entry long berisiko",
                confidence_penalty=0.05,
            )

    # -- Supertrend value sebagai dynamic S/R reference --
    if tr.supertrend_value and price:
        dist_pct = (price - tr.supertrend_value) / tr.supertrend_value * 100
        if tr.supertrend_direction == 1:  # bullish ST
            if 0 < dist_pct < 1.5:
                result.add_note(
                    f"✅ Harga sangat dekat SuperTrend support "
                    f"(${tr.supertrend_value:.6f}, +{dist_pct:.2f}%) — "
                    f"entry di atas ST support, SL reference jelas"
                )
                result.confidence_adjustment += 0.03
            elif dist_pct <= 0:
                result.add_warning(
                    f"Harga di bawah SuperTrend ({dist_pct:.2f}%) — "
                    f"ST belum flip bearish tapi harga sudah tembus, waspada",
                    confidence_penalty=0.04,
                )
        elif tr.supertrend_direction == -1:  # bearish ST
            result.add_warning(
                f"SuperTrend bearish (line=${tr.supertrend_value:.6f}) — "
                f"dynamic resistance di atas harga, trend down",
                confidence_penalty=0.05,
            )


def _check_vwap_band_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan vwap_upper_1/2, vwap_lower_1/2.

    Bands VWAP ±1σ ±2σ memberi konteks presisi posisi harga dalam distribusi
    volume harian. Lebih informatif dari sekadar 'above/below VWAP'.
    """
    tr = iset.trend
    if tr is None:
        return

    price = iset.current_price
    vwap  = tr.vwap
    if not price or not vwap:
        return

    u1 = tr.vwap_upper_1
    u2 = tr.vwap_upper_2
    l1 = tr.vwap_lower_1
    l2 = tr.vwap_lower_2

    if None in (u1, u2, l1, l2):
        return

    # Posisi presisi dalam band VWAP
    if price >= u2:
        result.add_warning(
            f"Harga di atas VWAP +2σ (${u2:.6f}) — "
            f"extreme overbought vs distribusi volume, mean-reversion risk tinggi",
            confidence_penalty=0.07,
        )
    elif price >= u1:
        result.add_warning(
            f"Harga di zona VWAP +1σ–+2σ (${u1:.6f}–${u2:.6f}) — "
            f"stretched di atas VWAP, R/R kurang ideal untuk entry baru",
            confidence_penalty=0.03,
        )
    elif price >= vwap:
        dist_to_u1 = (u1 - price) / price * 100
        result.add_note(
            f"✅ Harga di atas VWAP (${vwap:.6f}), ruang ke +1σ={dist_to_u1:.2f}% — "
            f"bullish VWAP zone dengan room yang cukup"
        )
        result.confidence_adjustment += 0.03
    elif price >= l1:
        dist_to_vwap = (vwap - price) / price * 100
        result.add_note(
            f"Harga di bawah VWAP ({dist_to_vwap:.2f}%) tapi di atas -1σ — "
            f"sedikit bearish tapi masih dalam distribusi normal"
        )
    elif price >= l2:
        result.add_note(
            f"✅ Harga di zona VWAP -1σ–-2σ (${l1:.6f}–${l2:.6f}) — "
            f"value zone: banyak volume transacted di atas level ini, oversold VWAP"
        )
        result.confidence_adjustment += 0.04
    else:
        result.add_note(
            f"✅ Harga di bawah VWAP -2σ (${l2:.6f}) — "
            f"extreme oversold vs distribusi volume, strong mean-reversion kandidat"
        )
        result.confidence_adjustment += 0.05


def _check_ichimoku_detail_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan tenkan, kijun, senkou_a/b, chikou, cloud_top/bottom.

    Level-level Ichimoku memberi 5 lapisan konfirmasi yang saat ini hanya
    dipakai satu (price_vs_cloud). Tiap level = S/R dinamis tersendiri.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return

    # -- Tenkan/Kijun sebagai dynamic S/R --
    if st.tenkan and st.kijun:
        if price > st.tenkan > st.kijun:
            result.add_note(
                f"✅ Ichimoku: price > tenkan (${st.tenkan:.6f}) > kijun (${st.kijun:.6f}) — "
                f"triple bullish alignment"
            )
            result.confidence_adjustment += 0.04
        elif price < st.tenkan and price < st.kijun:
            result.add_warning(
                f"Ichimoku: price di bawah tenkan & kijun — "
                f"short-term dan medium-term momentum keduanya bearish",
                confidence_penalty=0.05,
            )
        elif price > st.kijun and price < st.tenkan:
            result.add_note(
                f"📊 Harga di antara kijun (${st.kijun:.6f}) dan tenkan (${st.tenkan:.6f}) — "
                f"momentum campuran, kijun masih jadi support"
            )

        # Tenkan-Kijun gap: gap besar = trend kuat
        tk_gap_pct = abs(st.tenkan - st.kijun) / st.kijun * 100 if st.kijun else 0
        if tk_gap_pct > 2.0 and st.tenkan > st.kijun:
            result.add_note(
                f"✅ TK gap lebar ({tk_gap_pct:.1f}%) — trend bullish kuat"
            )
            result.confidence_adjustment += 0.02

    # -- Chikou: konfirmasi lagging --
    if st.chikou and price:
        if st.chikou > price:
            result.add_note(
                f"✅ Chikou (${st.chikou:.6f}) > harga sekarang — "
                f"lagging confirmation bullish"
            )
            result.confidence_adjustment += 0.02
        else:
            result.add_warning(
                f"Chikou (${st.chikou:.6f}) < harga sekarang — "
                f"lagging span belum konfirmasi bullish",
                confidence_penalty=0.03,
            )

    # -- Cloud top/bottom sebagai level S/R eksplisit --
    if st.cloud_top and st.cloud_bottom and price:
        if st.price_vs_cloud == "above":
            dist_to_cloud = (price - st.cloud_top) / price * 100
            if dist_to_cloud < 1.5:
                result.add_note(
                    f"⚠️ Harga hanya {dist_to_cloud:.2f}% di atas cloud top "
                    f"(${st.cloud_top:.6f}) — dekat edge cloud, risiko pullback ke cloud"
                )
            elif dist_to_cloud > 5.0:
                result.add_note(
                    f"✅ Harga {dist_to_cloud:.2f}% di atas cloud — "
                    f"jarak aman dari cloud support"
                )
                result.confidence_adjustment += 0.02
        elif st.price_vs_cloud == "below":
            dist_to_cloud = (st.cloud_bottom - price) / price * 100
            result.add_warning(
                f"Harga {dist_to_cloud:.2f}% di bawah cloud bottom "
                f"(${st.cloud_bottom:.6f}) — cloud resistance kuat di atas",
                confidence_penalty=0.04,
            )

    # -- Senkou A vs B: kumo twist / cloud quality --
    if st.senkou_a and st.senkou_b:
        if st.senkou_a > st.senkou_b:
            result.add_note("✅ Kumo bullish (Senkou A > B) — cloud mendukung uptrend")
            result.confidence_adjustment += 0.02
        else:
            result.add_warning(
                "Kumo bearish (Senkou A < B) — cloud resistance lebih kuat",
                confidence_penalty=0.03,
            )


def _check_pivot_ladder_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan r1/r2/r3, s1/s2/s3, price_vs_pivot.

    Pivot ladder lengkap memungkinkan kalkulasi R/R ke target R1/R2 dan
    SL reference ke S1/S2 — jauh lebih presisi dari sekadar nearest_resistance.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return

    # -- price_vs_pivot: intraday directional bias --
    if st.price_vs_pivot:
        if st.price_vs_pivot == "above":
            result.add_note(
                f"✅ Harga di atas daily pivot (${st.pivot:.6f}) — "
                f"intraday bias bullish"
            )
            result.confidence_adjustment += 0.02
        elif st.price_vs_pivot == "below":
            result.add_warning(
                f"Harga di bawah daily pivot (${st.pivot:.6f}) — "
                f"intraday bias bearish",
                confidence_penalty=0.04,
            )

    # -- R/R ke R1 dan target R2 --
    if st.r1 and st.s1:
        dist_r1_pct = (st.r1 - price) / price * 100
        dist_s1_pct = (price - st.s1) / price * 100

        if dist_r1_pct > 0 and dist_s1_pct > 0:
            rr = dist_r1_pct / dist_s1_pct if dist_s1_pct > 0 else 0
            if rr >= 2.0:
                result.add_note(
                    f"✅ Pivot R/R: target R1={dist_r1_pct:.2f}% / SL ke S1={dist_s1_pct:.2f}% "
                    f"→ R/R={rr:.1f}x"
                )
                result.confidence_adjustment += 0.03
            elif rr < 1.0:
                result.add_warning(
                    f"Pivot R/R buruk: target R1={dist_r1_pct:.2f}% / SL ke S1={dist_s1_pct:.2f}% "
                    f"→ R/R={rr:.1f}x (< 1.0)",
                    confidence_penalty=0.05,
                )

        # Jarak ke R1 sangat kecil = upside terbatas
        if dist_r1_pct < 0.8:
            result.add_warning(
                f"R1 sangat dekat ({dist_r1_pct:.2f}%) — "
                f"upside ke target pivot pertama sangat terbatas",
                confidence_penalty=0.05,
            )

    # -- R2 sebagai extended target --
    if st.r2 and price:
        dist_r2 = (st.r2 - price) / price * 100
        if dist_r2 > 3.0:
            result.add_note(
                f"✅ R2 target (${st.r2:.6f}) = +{dist_r2:.2f}% — "
                f"extended target tersedia"
            )


def _check_market_structure_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan trend_structure, structure_event, last_swing_high/low,
    market_structure_score, sr_zones, nearest_structure_support/resistance.

    Market structure (HH/HL = uptrend, LH/LL = downtrend) dan BOS/CHoCH
    adalah konfirmasi paling fundamental apakah harga bergerak searah signal.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return

    # -- trend_structure: HH/HL vs LH/LL --
    ts = st.trend_structure
    if ts in ("HH_HL", "strong_uptrend"):
        result.add_note(
            f"✅ Market structure BULLISH ({ts}) — "
            f"Higher Highs + Higher Lows terkonfirmasi"
        )
        result.confidence_adjustment += 0.06
    elif ts in ("HL_only", "weak_uptrend"):
        result.add_note(
            f"✅ Market structure lemah bullish ({ts}) — "
            f"HL terbentuk, tapi HH belum terkonfirmasi"
        )
        result.confidence_adjustment += 0.02
    elif ts in ("LH_LL", "strong_downtrend"):
        result.add_warning(
            f"Market structure BEARISH ({ts}) — "
            f"Lower Highs + Lower Lows: entry long melawan struktur",
            confidence_penalty=0.08,
        )
    elif ts in ("LH_only", "weak_downtrend"):
        result.add_warning(
            f"Market structure condong bearish ({ts}) — "
            f"LH terbentuk, struktur belum bullish",
            confidence_penalty=0.04,
        )

    # -- structure_event: BOS / CHoCH --
    ev = st.structure_event
    if ev == "BOS_bullish":
        result.add_note(
            "🚀 Break of Structure BULLISH (BOS) — "
            "harga tembus swing high sebelumnya: konfirmasi trend continuation"
        )
        result.confidence_adjustment += 0.07
    elif ev == "CHoCH_bullish":
        result.add_note(
            "🔥 Change of Character BULLISH (CHoCH) — "
            "struktur berbalik bullish setelah downtrend: high-probability reversal"
        )
        result.confidence_adjustment += 0.08
    elif ev == "BOS_bearish":
        result.add_warning(
            "Break of Structure BEARISH — harga tembus swing low: "
            "trend bearish terkonfirmasi, hindari long",
            confidence_penalty=0.09,
        )
    elif ev == "CHoCH_bearish":
        result.add_warning(
            "Change of Character BEARISH (CHoCH) — "
            "struktur berbalik bearish: high-probability reversal bearish",
            confidence_penalty=0.10,
        )

    # -- market_structure_score --
    mss = st.market_structure_score
    if mss is not None:
        if mss >= 70:
            result.add_note(
                f"✅ Market structure score tinggi ({mss:.0f}) — "
                f"kualitas struktur bullish sangat baik"
            )
            result.confidence_adjustment += 0.03
        elif mss <= 30:
            result.add_warning(
                f"Market structure score rendah ({mss:.0f}) — "
                f"struktur market lemah/bearish",
                confidence_penalty=0.04,
            )

    # -- nearest_structure_support/resistance dari S/R zone clustering --
    nss = st.nearest_structure_support
    nsr = st.nearest_structure_resistance

    if nss and price:
        dist_sup = (price - nss) / price * 100
        if dist_sup < 1.0:
            result.add_note(
                f"✅ Clustered S/R support sangat dekat "
                f"(${nss:.6f}, {dist_sup:.2f}%) — "
                f"zona support multi-confluence di bawah entry"
            )
            result.confidence_adjustment += 0.04

    if nsr and price:
        dist_res = (nsr - price) / price * 100
        if 0 < dist_res < 1.5:
            result.add_warning(
                f"Clustered S/R resistance dekat "
                f"(${nsr:.6f}, +{dist_res:.2f}%) — "
                f"zona resistance multi-confluence membatasi upside",
                confidence_penalty=0.05,
            )

    # -- last_swing_high/low untuk R/R reference --
    if st.last_swing_high and st.last_swing_low and price:
        swing_range = st.last_swing_high - st.last_swing_low
        pos_in_swing = (price - st.last_swing_low) / swing_range if swing_range > 0 else 0.5
        if pos_in_swing <= 0.35:
            result.add_note(
                f"✅ Harga di bagian bawah swing range ({pos_in_swing:.0%}) — "
                f"swing low (${st.last_swing_low:.6f}) dekat, entry low-risk"
            )
            result.confidence_adjustment += 0.03
        elif pos_in_swing >= 0.75:
            result.add_warning(
                f"Harga di bagian atas swing range ({pos_in_swing:.0%}) — "
                f"entry di upper swing, R/R tidak ideal",
                confidence_penalty=0.04,
            )


def _check_fib_detail_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan fib_236/382/500/786, fib_trend, fib_ext_1272/1618.

    Full Fibonacci ladder memberi target multi-level dan konfirmasi arah trend.
    fib_trend: apakah fib dihitung dari upswing atau downswing
    fib_ext: target profit extension 1.272 dan 1.618
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return

    # -- fib_trend: konfirmasi arah --
    if st.fib_trend == "down":
        result.add_warning(
            "Fibonacci trend: downswing — fib level dihitung dari swing tertinggi "
            "ke terendah, harga dalam koreksi",
            confidence_penalty=0.04,
        )
    elif st.fib_trend == "up":
        result.add_note(
            "✅ Fibonacci trend: upswing — fib dihitung dari swing rendah ke tinggi, "
            "mengonfirmasi struktur bullish"
        )
        result.confidence_adjustment += 0.02

    # -- Cek apakah harga di level Fibonacci kunci (toleransi 0.5%) --
    fib_levels = {
        "23.6%": st.fib_236,
        "38.2%": st.fib_382,
        "50.0%": st.fib_500,
        "61.8%": st.fib_618,
        "78.6%": st.fib_786,
    }
    hit_levels = []
    for label, level in fib_levels.items():
        if level and abs(price - level) / price * 100 < 0.5:
            hit_levels.append(f"{label} (${level:.6f})")

    if hit_levels:
        result.add_note(
            f"✅ Harga di Fibonacci level: {', '.join(hit_levels)} — "
            f"confluence Fibonacci kuat"
        )
        result.confidence_adjustment += 0.04 * len(hit_levels)

    # -- fib_ext sebagai profit target reference --
    if st.fib_ext_1272 and st.fib_ext_1618 and price:
        to_1272 = (st.fib_ext_1272 - price) / price * 100
        to_1618 = (st.fib_ext_1618 - price) / price * 100
        if to_1272 > 2.0:
            result.add_note(
                f"✅ Fib extension 1.272 target: ${st.fib_ext_1272:.6f} (+{to_1272:.2f}%) — "
                f"profit target konservatif tersedia"
            )
        if to_1618 > 3.0:
            result.add_note(
                f"✅ Fib extension 1.618 target: ${st.fib_ext_1618:.6f} (+{to_1618:.2f}%) — "
                f"profit target agresif tersedia"
            )


def _check_donchian_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan donchian_upper/lower/middle, donchian_pct_b,
    donchian_width_pct, donchian_score.

    Donchian Channel = breakout system: upper = recent high, lower = recent low.
    donchian_pct_b [0-1]: posisi harga dalam channel
    donchian_width_pct: lebar channel relatif = volatility context
    donchian_score: composite Donchian score
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return

    pct_b = st.donchian_pct_b
    width = st.donchian_width_pct
    upper = st.donchian_upper
    lower = st.donchian_lower
    mid   = st.donchian_middle

    if pct_b is None or upper is None or lower is None:
        return

    # -- Posisi dalam channel --
    if pct_b >= 0.85:
        result.add_warning(
            f"Donchian pct_b={pct_b:.2f} — harga di upper channel "
            f"(${upper:.6f}): potensi resistance di recent high",
            confidence_penalty=0.05,
        )
    elif pct_b <= 0.20:
        result.add_note(
            f"✅ Donchian pct_b={pct_b:.2f} — harga di lower channel "
            f"(${lower:.6f}): dekat recent low, potential reversal zone"
        )
        result.confidence_adjustment += 0.04
    elif 0.40 <= pct_b <= 0.65 and mid:
        result.add_note(
            f"Donchian: harga di mid-channel (pct_b={pct_b:.2f}, "
            f"mid=${mid:.6f}) — zona netral"
        )

    # -- Width: narrow = breakout setup, wide = trending/extended --
    if width is not None:
        if width < 3.0:
            result.add_note(
                f"✅ Donchian channel sempit ({width:.1f}%) — "
                f"range konsolidasi: potensi breakout imminent"
            )
            result.confidence_adjustment += 0.03
        elif width > 15.0:
            result.add_warning(
                f"Donchian channel sangat lebar ({width:.1f}%) — "
                f"volatilitas tinggi, harga sudah bergerak jauh dari range",
                confidence_penalty=0.03,
            )

    # -- donchian_score --
    ds = st.donchian_score
    if ds is not None and ds >= 65:
        result.add_note(
            f"✅ Donchian score {ds:.0f} — "
            f"posisi dalam channel mendukung entry bullish"
        )
        result.confidence_adjustment += 0.02


def _check_strength_context(
    iset: IndicatorSet,
    result: ValidationResult,
) -> None:
    """[UPGRADE] Aktifkan obv, obv_trend, mfi_divergence yang sebelumnya idle.

    Strength indicators mengukur KEKUATAN trend, bukan arahnya:
    - ADX/DI: sudah dipakai di _check_oscillator_context
    - obv + obv_trend: konfirmasi volume flow searah price action
    - mfi_divergence:  divergence antara MFI dan RSI = early warning reversal
    """
    st = iset.strength
    if st is None or st.composite_score is None:
        return

    # -- OBV trend: apakah volume flow searah price? --
    obv_trend = st.obv_trend
    if obv_trend == "rising":
        result.add_note(
            f"✅ OBV rising — volume flow mengonfirmasi uptrend: "
            f"tekanan akumulasi lebih besar dari distribusi"
        )
        result.confidence_adjustment += 0.04
    elif obv_trend == "falling":
        result.add_warning(
            "OBV falling — volume flow berlawanan dengan price: "
            "distribusi lebih dominan, potensi weakness tersembunyi",
            confidence_penalty=0.06,
        )

    # -- OBV nilai absolut: konteks apakah OBV di puncak historis atau tidak --
    if st.obv is not None:
        # Informasi OBV absolut penting untuk divergence manual
        # Tapi tanpa historical series kita tidak bisa hitung puncak
        # Cukup log sebagai informatif — nilai disediakan untuk konsumen
        pass

    # -- MFI divergence: MFI dan RSI bergerak berbeda = peringatan --
    mfi_div = st.mfi_divergence
    if mfi_div is not None and mfi_div != 0.0:
        from constants import RSI_DIVERGENCE_THRESHOLD
        if mfi_div > RSI_DIVERGENCE_THRESHOLD:
            result.add_note(
                f"✅ MFI-RSI divergence bullish ({mfi_div:+.1f}) — "
                f"money flow (MFI) naik lebih cepat dari RSI: "
                f"tekanan beli berbasis volume lebih kuat dari momentum harga"
            )
            result.confidence_adjustment += 0.05
        elif mfi_div < -RSI_DIVERGENCE_THRESHOLD:
            result.add_warning(
                f"MFI-RSI divergence bearish ({mfi_div:+.1f}) — "
                f"money flow turun lebih cepat dari RSI: "
                f"volume selling pressure lebih besar dari yang terlihat di harga",
                confidence_penalty=0.06,
            )

    # -- ADX kekuatan trend: ADX < 20 = sideways = sinyal trend lebih berisiko --
    if st.adx is not None:
        from constants import ADX_WEAK_TREND, ADX_STRONG_TREND
        if st.adx < ADX_WEAK_TREND:
            result.add_warning(
                f"ADX rendah ({st.adx:.1f} < {ADX_WEAK_TREND}) — "
                f"trend sangat lemah/sideways: sinyal trend-following lebih berisiko",
                confidence_penalty=0.05,
            )
        elif st.adx >= ADX_STRONG_TREND:
            result.add_note(
                f"✅ ADX kuat ({st.adx:.1f}) — "
                f"trend established: sinyal trend-following lebih reliable"
            )
            result.confidence_adjustment += 0.03

    # -- DI alignment: +DI > -DI = directional bias bullish --
    if st.plus_di is not None and st.minus_di is not None:
        if st.plus_di > st.minus_di * 1.5:
            result.add_note(
                f"✅ DI+ ({st.plus_di:.1f}) jauh di atas DI- ({st.minus_di:.1f}) — "
                f"directional pressure bullish dominan"
            )
            result.confidence_adjustment += 0.03
        elif st.minus_di > st.plus_di * 1.5:
            result.add_warning(
                f"DI- ({st.minus_di:.1f}) jauh di atas DI+ ({st.plus_di:.1f}) — "
                f"directional pressure bearish dominan",
                confidence_penalty=0.05,
            )

    # -- Volume ratio + spike --
    if st.volume_ratio is not None:
        from constants import VOLUME_RATIO_ELEVATED, VOLUME_RATIO_SPIKE
        if st.volume_climax:
            result.add_warning(
                f"Volume climax ({st.volume_ratio:.1f}x) — "
                f"volume ekstrem sering menandai exhaustion/reversal, bukan continuation",
                confidence_penalty=0.05,
            )
        elif st.volume_spike:
            result.add_note(
                f"✅ Volume spike ({st.volume_ratio:.1f}x) — "
                f"volume tinggi mengonfirmasi momentum breakout"
            )
            result.confidence_adjustment += 0.04
        elif st.volume_ratio >= VOLUME_RATIO_ELEVATED:
            result.add_note(
                f"✅ Volume elevated ({st.volume_ratio:.1f}x rata-rata) — "
                f"partisipasi pasar meningkat"
            )
            result.confidence_adjustment += 0.02
        elif st.volume_ratio < 0.7:
            result.add_warning(
                f"Volume rendah ({st.volume_ratio:.1f}x rata-rata) — "
                f"breakout tanpa volume berisiko false breakout",
                confidence_penalty=0.04,
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

    # [UPGRADE] Checks baru yang mengaktifkan field sebelumnya idle
    _check_bb_context(iset, result)
    _check_kc_context(iset, result)
    _check_macd_context(iset, result)
    _check_stoch_context(iset, result)
    _check_strength_context(iset, result)

    _check_oscillator_context(iset, result)

    _check_structure_context(iset, result)

    # [UPGRADE] Checks trend & structure yang mengaktifkan field sebelumnya idle
    _check_trend_cross_context(iset, result)
    _check_vwap_band_context(iset, result)
    _check_ichimoku_detail_context(iset, result)
    _check_pivot_ladder_context(iset, result)
    _check_market_structure_context(iset, result)
    _check_fib_detail_context(iset, result)
    _check_donchian_context(iset, result)

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
    # [UPGRADE] summarize_validation diintegrasikan ke debug logging agar
    # output validator terlihat di log tanpa perlu caller memanggil manual.
    log.debug("[%s] %s", signal.symbol, summarize_validation(result))
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
