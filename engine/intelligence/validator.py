"""
intelligence/validator.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from engine.constants import (
    OBSERVATION_STALE_THRESHOLD_SECONDS,
    SCORE_NEUTRAL,
    SPREAD_LIMIT_DEFAULT,
)
from engine.core.models import (
    IndicatorSet,
    MarketRegime,
    ObservationReport,
    PatternContext,
    PatternType,
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

def _check_rsi_divergence(iset: IndicatorSet, result: ValidationResult, side: str = "long") -> None:
    # [FUTURES-READY] side="long" default identik persis dgn sebelumnya.
    # Short: mirror -- bearish divergence jadi konfirmasi, bullish jadi warning.
    div = iset.momentum.rsi_divergence
    if div is None or div == 0.0:
        return
    is_long = side != "short"
    confirming = div > 0 if is_long else div < 0
    opposing   = div < 0 if is_long else div > 0

    if confirming:
        arah = "bullish" if is_long else "bearish"
        result.add_note(f"✅ RSI {arah} divergence terdeteksi ({div:.1f}) — konfirmasi sinyal")
        result.confidence_adjustment += 0.05
    elif opposing:
        arah = "bearish" if is_long else "bullish"
        sinyal = "BUY" if is_long else "SHORT"
        result.add_warning(
            f"RSI {arah} divergence ({div:.1f}) — berlawanan dengan sinyal {sinyal}",
            confidence_penalty=0.08,
        )

def _check_macd_divergence(iset: IndicatorSet, result: ValidationResult, side: str = "long") -> None:
    # [FUTURES-READY] side="long" default identik persis dgn sebelumnya.
    div = iset.momentum.macd_divergence
    if div is None or div == 0.0:
        return
    is_long = side != "short"
    confirming = div > 0 if is_long else div < 0
    opposing   = div < 0 if is_long else div > 0

    if confirming:
        arah = "bullish" if is_long else "bearish"
        result.add_note(f"✅ MACD {arah} divergence ({div:.1f})")
        result.confidence_adjustment += 0.03
    elif opposing:
        arah = "bearish" if is_long else "bullish"
        result.add_warning(
            f"MACD {arah} divergence ({div:.1f})",
            confidence_penalty=0.05,
        )

def _check_pattern_type_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """
    [UPGRADE] Aktifkan primary_pattern — field paling informatif di
    PatternIndicators yang sebelumnya 100% idle di luar indicators/patterns.py.
    Sebelumnya hanya pattern_score (angka komposit) yang mengalir ke sini;
    JENIS pattern aktual (bullish_engulfing vs gravestone_doji dst) hilang.

    validate_signal() di codebase ini cuma pernah dipakai untuk validasi
    sinyal BUY/long (intelligence/scorer.py tidak pernah menghasilkan
    signal_type="sell" — itu murni jalur exit terpisah di main.py), jadi
    konvensi yang dipakai konsisten dengan check lain di file ini
    (_check_macd_divergence dkk): bullish = bukti mendukung entry,
    bearish = bukti melawan. Pattern netral (doji/spinning_top/squeeze/
    climax/none) tidak punya arah, sengaja di-skip.
    """
    pattern = iset.patterns.primary_pattern
    if pattern is None or pattern == PatternType.NONE:
        return

    is_bullish = pattern.is_bullish
    if is_bullish is None:
        return

    pattern_label = pattern.value.replace("_", " ")
    # [FUTURES-READY] side="long" default identik persis dgn sebelumnya.
    # Short: pattern bearish jadi konfirmasi, bullish jadi warning.
    is_long = side != "short"
    confirming = is_bullish if is_long else not is_bullish

    if confirming:
        arah = "bullish" if is_bullish else "bearish"
        result.add_note(
            f"✅ Pattern {arah} '{pattern_label}' terdeteksi di candle terakhir"
        )
        result.confidence_adjustment += 0.04
    else:
        arah = "bullish" if is_bullish else "bearish"
        arah_entry = "long" if is_long else "short"
        result.add_warning(
            f"Pattern {arah} '{pattern_label}' terdeteksi di candle terakhir — "
            f"melawan arah entry {arah_entry}",
            confidence_penalty=0.06,
        )

def _check_support_resistance_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    # [FUTURES-READY] side="long" default identik persis dgn sebelumnya.
    # Short: mirror -- dekat resistance jadi favorable (entry short dgn
    # ekspektasi ditolak turun), dekat support jadi kurang optimal (risiko
    # bounce naik melawan posisi short).
    context = iset.patterns.pattern_context
    is_long = side != "short"
    favorable_context = PatternContext.NEAR_SUPPORT if is_long else PatternContext.NEAR_RESISTANCE
    unfavorable_context = PatternContext.NEAR_RESISTANCE if is_long else PatternContext.NEAR_SUPPORT

    if context == favorable_context:
        dist = (iset.patterns.distance_to_support if is_long
                else iset.patterns.distance_to_resistance)
        dist_str = f"{dist:.2f}%" if dist else "unknown"
        label = "support" if is_long else "resistance"
        result.add_note(
            f"✅ Harga dekat {label} ({dist_str}) — risk/reward favorable untuk entry"
        )
        result.confidence_adjustment += 0.04

    elif context == unfavorable_context:
        dist = (iset.patterns.distance_to_resistance if is_long
                else iset.patterns.distance_to_support)
        dist_str = f"{dist:.2f}%" if dist else "unknown"
        label = "resistance" if is_long else "support"
        result.add_warning(
            f"Harga dekat {label} ({dist_str}) — potensi terhalang, "
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
    side: str = "long",
) -> None:
    # [FUTURES-READY] side="long" default identik persis dgn sebelumnya.
    # Short: mirror -- conf_score RENDAH (bearish di TF besar) jadi konfirmasi,
    # conf_score TINGGI (bullish di TF besar) jadi warning berlawanan arah.
    if not observation.confirmation_tf_valid:
        result.add_note(
            "⚠️ Confirmation TF tidak tersedia atau tidak valid — "
            "sinyal tidak punya konfirmasi higher TF"
        )
        result.confidence_adjustment -= 0.05
        return

    conf_score = observation.confirmation_tf_score
    is_long = side != "short"
    confirming = conf_score >= 60.0 if is_long else conf_score <= 40.0
    opposing   = conf_score <= 40.0 if is_long else conf_score >= 60.0

    if confirming:
        result.add_note(
            f"✅ Higher TF align (conf_score={conf_score:.1f}) — "
            f"sinyal didukung timeframe lebih besar"
        )
        result.confidence_adjustment += 0.06

    elif opposing:
        arah = "BEARISH" if is_long else "BULLISH"
        result.add_warning(
            f"Higher TF {arah} (conf_score={conf_score:.1f}) — "
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
        from engine.core.models import PatternType
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
    side: str = "long",
) -> None:
    """[UPGRADE] Memanfaatkan bb_middle, bb_position, bb_trending yang sebelumnya idle.

    bb_middle  → support dinamis: close di atas middle = bullish bias
    bb_position → posisi relatif dalam band [0,1]: < 0.35 = buy zone
    bb_trending  → arah lebar band: contracting = setup squeeze pre-breakout

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh 3 sub-check di-mirror -- lower/upper zone tertukar, dan bb_middle
    dibaca dari sisi berlawanan.
    """
    vol = iset.volatility
    if vol.bb_middle is None or vol.bb_position is None:
        return
    is_long = side != "short"

    # -- bb_middle sebagai dynamic support/resistance --
    price = iset.current_price
    if price and vol.bb_middle > 0:
        pct_above = (price - vol.bb_middle) / vol.bb_middle * 100
        pct_check = pct_above if is_long else -pct_above
        if pct_check < -1.0:
            arah = "bullish" if is_long else "bearish"
            posisi = "bawah" if is_long else "atas"
            result.add_warning(
                f"Harga {pct_above:.1f}% di {posisi} BB middle ({vol.bb_middle:.4f}) — "
                f"dynamic support/resistance tertembus, {arah} bias melemah",
                confidence_penalty=0.04,
            )
        elif 0 <= pct_check <= 2.0:
            posisi = "atas" if is_long else "bawah"
            result.add_note(
                f"✅ Harga tepat di {posisi} BB middle ({pct_above:+.1f}%) — "
                f"dynamic support/resistance terjaga, entry zone valid"
            )

    # -- bb_position: posisi dalam band --
    pos = vol.bb_position
    favorable_pos   = pos <= 0.25 if is_long else pos >= 0.85
    unfavorable_pos = pos >= 0.85 if is_long else pos <= 0.25

    if favorable_pos:
        zone = "lower" if is_long else "upper"
        result.add_note(
            f"✅ BB position {pos:.2f} ({zone} zone) — "
            f"harga di area optimal utk entry {'long' if is_long else 'short'}, risk/reward optimal"
        )
        result.confidence_adjustment += 0.03
    elif unfavorable_pos:
        zone = "upper" if is_long else "lower"
        result.add_warning(
            f"BB position {pos:.2f} ({zone} extreme) — "
            f"harga mendekati band berlawanan, potensi resistance/support dan mean-reversion",
            confidence_penalty=0.05,
        )

    # -- bb_trending: arah lebar band --
    trend = vol.bb_trending or "flat"
    setup_half = pos <= 0.5 if is_long else pos >= 0.5
    blowoff_half = pos >= 0.75 if is_long else pos <= 0.25

    if trend == "contracting" and setup_half:
        half_label = "lower" if is_long else "upper"
        result.add_note(
            f"🔥 BB contracting + posisi {half_label} half ({pos:.2f}) — "
            f"energy terakumulasi, setup pre-breakout ideal"
        )
        result.confidence_adjustment += 0.04
    elif trend == "expanding" and blowoff_half:
        zone_label = "upper" if is_long else "lower"
        result.add_warning(
            f"BB expanding saat harga di {zone_label} zone ({pos:.2f}) — "
            f"potensi blow-off, waspada exhaustion",
            confidence_penalty=0.04,
        )


def _check_kc_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Memanfaatkan kc_upper, kc_lower, kc_middle, kc_score yang sebelumnya idle.

    Keltner Channel memberi konteks berbeda dari BB: berbasis ATR (bukan std dev),
    lebih stabil saat volatilitas spike. Kombinasi KC+BB memberi sinyal squeeze
    dan posisi relatif yang lebih robust.
    kc_score  → skor posisi KC: < 40 = overbought KC, > 60 = near lower KC (bullish)
    kc_middle → EMA trend: harga vs KC middle = trend bias
    kc_upper/lower → range channel untuk context entry

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh sub-check di-mirror -- lower/upper KC zone tertukar, kc_middle
    dibaca dari sisi berlawanan, kc_score threshold di-mirror di sekitar 50.
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

    is_long = side != "short"
    kc_pos = (price - vol.kc_lower) / kc_range  # 0=lower, 0.5=middle, 1=upper

    # Posisi KC
    favorable = kc_pos < 0.30 if is_long else kc_pos > 0.70
    unfavorable = kc_pos > 0.80 if is_long else kc_pos < 0.20
    if favorable:
        zone = "lower" if is_long else "upper"
        result.add_note(
            f"✅ Harga di {zone} KC zone ({kc_pos:.2f}) — "
            f"dekat EMA-ATR support/resistance, entry favorable"
        )
        result.confidence_adjustment += 0.03
    elif unfavorable:
        zone = "upper" if is_long else "lower"
        result.add_warning(
            f"Harga di {zone} KC zone ({kc_pos:.2f}) — "
            f"dekat EMA-ATR resistance/support, room terbatas",
            confidence_penalty=0.04,
        )

    # Harga vs KC middle (EMA): trend lemah kalau di sisi berlawanan arah posisi
    weak_trend = (price < vol.kc_middle) if is_long else (price > vol.kc_middle)
    if weak_trend:
        pct = abs(vol.kc_middle - price) / vol.kc_middle * 100
        posisi = "bawah" if is_long else "atas"
        arah = "bullish" if is_long else "bearish"
        result.add_warning(
            f"Harga {pct:.1f}% di {posisi} KC middle (EMA={vol.kc_middle:.4f}) — "
            f"trend jangka menengah belum {arah}",
            confidence_penalty=0.03,
        )

    # kc_score informatif: mirror threshold di sekitar titik tengah
    if vol.kc_score is not None:
        score_warn = vol.kc_score < 40 if is_long else vol.kc_score > 60
        if score_warn:
            result.add_warning(
                f"KC score {'rendah' if is_long else 'tinggi'} ({vol.kc_score:.0f}) — "
                f"posisi dalam channel kurang optimal untuk entry {'long' if is_long else 'short'}",
                confidence_penalty=0.03,
            )


def _check_macd_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Memanfaatkan macd_line, macd_signal, macd_hist_prev, macd_zero_cross.

    macd_line vs macd_signal → konfirmasi trend jangka menengah
    macd_hist_prev → arah momentum: apakah histogram sedang membaik?
    macd_zero_cross → event kritis: MACD baru saja cross above zero = strong bull signal

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    mayoritas sub-check di-mirror. PENGECUALIAN: macd_zero_cross adalah
    Optional[bool] TANPA info arah di data model (core/models.py) -- tidak
    bisa di-mirror dgn benar tanpa update upstream indicator utk membedakan
    "cross above zero" vs "cross below zero". Untuk short, bonus ini
    di-SKIP (bukan di-mirror jadi penalty) supaya tidak salah arah.
    """
    mom = iset.momentum
    if mom.macd_line is None or mom.macd_signal is None:
        return
    is_long = side != "short"

    # MACD line vs signal: basic trend bias
    macd_confirms = (mom.macd_line > mom.macd_signal) if is_long else (mom.macd_line < mom.macd_signal)
    gap = mom.macd_line - mom.macd_signal

    if not macd_confirms:
        arah = "bullish" if is_long else "bearish"
        posisi = "bawah" if is_long else "atas"
        result.add_warning(
            f"MACD line ({mom.macd_line:.5f}) di {posisi} signal ({mom.macd_signal:.5f}) — "
            f"momentum jangka menengah belum {arah}",
            confidence_penalty=0.04,
        )
    elif abs(gap) > 0:
        arah = "bullish" if is_long else "bearish"
        posisi = "atas" if is_long else "bawah"
        result.add_note(
            f"✅ MACD line di {posisi} signal (gap={gap:+.5f}) — "
            f"momentum jangka menengah {arah}"
        )

    # macd_zero_cross: HANYA berlaku utk long (lihat catatan keterbatasan data di docstring)
    if is_long and mom.macd_zero_cross:
        result.add_note(
            "🚀 MACD zero cross bullish — MACD baru melewati zero dari bawah, "
            "momentum shift signifikan"
        )
        result.confidence_adjustment += 0.06

    # -- vwma_vs_sma: konfirmasi volume mendukung momentum --
    if mom.vwma is not None and mom.vwma_vs_sma is not None:
        diff = mom.vwma_vs_sma
        diff_check = diff if is_long else -diff
        if diff_check > 1.5:
            arah = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ VWMA vs SMA ({diff:+.2f}%) — volume lebih berat di bar {arah}: "
                f"momentum dikonfirmasi oleh volume"
            )
            result.confidence_adjustment += 0.04
        elif diff_check > 0.5:
            result.add_note(
                f"✅ VWMA sedikit condong searah ({diff:+.2f}%) — "
                f"volume support moderat"
            )
            result.confidence_adjustment += 0.02
        elif diff_check < -1.5:
            arah = "bearish" if is_long else "bullish"
            result.add_warning(
                f"VWMA vs SMA ({diff:+.2f}%) — volume lebih berat di bar {arah}: "
                f"momentum kurang dikonfirmasi volume, potensi fake breakout",
                confidence_penalty=0.05,
            )
    if mom.macd_histogram is not None and mom.macd_hist_prev is not None:
        if is_long:
            improving = mom.macd_histogram > mom.macd_hist_prev
            weakening_side = mom.macd_histogram < 0
        else:
            improving = mom.macd_histogram < mom.macd_hist_prev
            weakening_side = mom.macd_histogram > 0
        if weakening_side and not improving:
            label = "selling" if is_long else "buying"
            result.add_warning(
                f"MACD histogram {'negatif' if is_long else 'positif'} dan memburuk "
                f"({mom.macd_hist_prev:.5f} → {mom.macd_histogram:.5f}) — "
                f"{label} pressure masih meningkat",
                confidence_penalty=0.05,
            )
        elif weakening_side and improving:
            result.add_note(
                f"⚡ MACD histogram {'negatif' if is_long else 'positif'} tapi membaik "
                f"({mom.macd_hist_prev:.5f} → {mom.macd_histogram:.5f}) — "
                f"early sign of momentum reversal"
            )
            result.confidence_adjustment += 0.03


def _check_stoch_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Memanfaatkan stoch_k, stoch_d, stoch_kd_cross, stoch_zone yang idle.

    StochRSI memberi konfirmasi overbought/oversold lebih sensitif dari RSI biasa.
    stoch_k, stoch_d → level saat ini
    stoch_kd_cross   → crossover K-D: sinyal entry/exit yang presisi
    stoch_zone       → oversold/overbought/neutral context

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    overbought/oversold zone tertukar makna, kd_cross bullish/bearish tertukar.
    """
    mom = iset.momentum
    if mom.stoch_k is None or mom.stoch_d is None:
        return

    k, d = mom.stoch_k, mom.stoch_d
    is_long = side != "short"
    favorable_zone   = "oversold" if is_long else "overbought"
    unfavorable_zone = "overbought" if is_long else "oversold"
    confirming_cross = "bullish" if is_long else "bearish"
    opposing_cross   = "bearish" if is_long else "bullish"

    # Zona berlawanan arah = risk tinggi untuk entry baru
    if mom.stoch_zone == unfavorable_zone:
        result.add_warning(
            f"StochRSI {unfavorable_zone} (K={k:.1f} D={d:.1f}) — "
            f"momentum sudah stretched, entry baru berisiko tinggi",
            confidence_penalty=0.06,
        )

    # Zona favorable + KD cross searah = sinyal kuat
    elif mom.stoch_zone == favorable_zone:
        if mom.stoch_kd_cross == confirming_cross:
            result.add_note(
                f"🔥 StochRSI {favorable_zone} + KD cross {confirming_cross} (K={k:.1f} D={d:.1f}) — "
                f"konfirmasi kuat: momentum berbalik dari extreme"
            )
            result.confidence_adjustment += 0.07
        else:
            result.add_note(
                f"✅ StochRSI {favorable_zone} (K={k:.1f} D={d:.1f}) — "
                f"potensi reversal, tunggu KD cross untuk konfirmasi"
            )
            result.confidence_adjustment += 0.03

    # Neutral zone: cek KD cross untuk directional bias
    elif mom.stoch_kd_cross == confirming_cross and ((k > d) if is_long else (k < d)):
        result.add_note(
            f"✅ StochRSI KD cross {confirming_cross} di neutral zone (K={k:.1f} D={d:.1f}) — "
            f"momentum mulai membaik"
        )
        result.confidence_adjustment += 0.03
    elif mom.stoch_kd_cross == opposing_cross:
        result.add_warning(
            f"StochRSI KD cross {opposing_cross} (K={k:.1f} D={d:.1f}) — "
            f"momentum melemah",
            confidence_penalty=0.04,
        )

    # K belum searah D tanpa cross = momentum belum confirmed
    k_not_confirmed = (k < d) if is_long else (k > d)
    if k_not_confirmed and mom.stoch_zone != favorable_zone:
        result.add_note(
            f"⚠️ StochRSI K ({k:.1f}) {'<' if is_long else '>'} D ({d:.1f}) — "
            f"momentum belum terkonfirmasi {'bullish' if is_long else 'bearish'}"
        )

def _check_oscillator_context(iset: IndicatorSet, result: ValidationResult, side: str = "long") -> None:
    """CCI, Williams %R, ROC — early warning momentum & overbought/oversold.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh 7 sub-check (CCI level/trend/divergence, Williams%R level/trend,
    ROC momentum/crossover) di-mirror penuh.
    """
    osc = iset.oscillators
    if not osc.is_valid():
        return
    is_long = side != "short"

    # ── CCI ───────────────────────────────────────────────────────────────────
    if osc.cci is not None:
        overbought = osc.cci > 150 if is_long else osc.cci < -150
        oversold   = osc.cci < -100 if is_long else osc.cci > 100
        healthy    = (0 < osc.cci < 100) if is_long else (-100 < osc.cci < 0)
        if overbought:
            zone = "overbought" if is_long else "oversold (mirror utk short)"
            result.add_warning(
                f"CCI {osc.cci:.1f} — ekstrem {zone}, potensi pullback",
                confidence_penalty=0.06,
            )
        elif oversold:
            zone = "oversold" if is_long else "overbought (mirror utk short)"
            arah = "long" if is_long else "short"
            result.add_note(f"✅ CCI {osc.cci:.1f} — {zone}, mendukung entry {arah}")
            result.confidence_adjustment += 0.03
        elif healthy:
            arah = "bullish" if is_long else "bearish"
            result.add_note(f"✅ CCI {osc.cci:.1f} — zona {arah} sehat")
            result.confidence_adjustment += 0.02

    # [v2] CCI trend — arah pergerakan indikator lebih penting dari nilai sesaat
    recovering = (osc.cci_trend == "rising" and osc.cci is not None and osc.cci < 0) if is_long else                  (osc.cci_trend == "falling" and osc.cci is not None and osc.cci > 0)
    weakening  = (osc.cci_trend == "falling" and osc.cci is not None and osc.cci > 50) if is_long else                  (osc.cci_trend == "rising" and osc.cci is not None and osc.cci < -50)
    if recovering:
        arah = "negatif" if is_long else "positif"
        result.add_note(
            f"✅ CCI {osc.cci_trend} ({osc.cci:.1f}) dari zona {arah} — potensi recovery"
        )
        result.confidence_adjustment += 0.02
    elif weakening:
        arah = "positif" if is_long else "negatif"
        result.add_warning(
            f"CCI {osc.cci_trend} ({osc.cci:.1f}) dari zona {arah} — momentum melemah",
            confidence_penalty=0.03,
        )

    # [v2] CCI divergence — early reversal signal
    if osc.cci_divergence is not None:
        confirming = osc.cci_divergence > 5 if is_long else osc.cci_divergence < -5
        opposing   = osc.cci_divergence < -5 if is_long else osc.cci_divergence > 5
        if confirming:
            arah = "bullish" if is_long else "bearish"
            arah_reversal = "up" if is_long else "down"
            result.add_note(
                f"✅ CCI {arah} divergence ({osc.cci_divergence:.1f}) — potensi reversal {arah_reversal}"
            )
            result.confidence_adjustment += 0.04
        elif opposing:
            arah = "bearish" if is_long else "bullish"
            arah_reversal = "down" if is_long else "up"
            result.add_warning(
                f"CCI {arah} divergence ({osc.cci_divergence:.1f}) — potensi reversal {arah_reversal}",
                confidence_penalty=0.04,
            )

    # ── Williams %R ───────────────────────────────────────────────────────────
    if osc.williams_r is not None:
        overbought_wr = osc.williams_r >= -20 if is_long else osc.williams_r <= -80
        oversold_wr   = osc.williams_r <= -80 if is_long else osc.williams_r >= -20
        if overbought_wr:
            zone = "overbought" if is_long else "oversold (mirror utk short)"
            result.add_warning(
                f"Williams %R {osc.williams_r:.1f} — {zone} zone",
                confidence_penalty=0.04,
            )
        elif oversold_wr:
            zone = "oversold" if is_long else "overbought (mirror utk short)"
            result.add_note(f"✅ Williams %R {osc.williams_r:.1f} — {zone}, momentum recovery")
            result.confidence_adjustment += 0.02

    # [v2] Williams %R trend — apakah bergerak keluar dari extreme searah posisi?
    wr_recovering = (osc.willr_trend == "rising" and osc.williams_r is not None and osc.williams_r <= -70) if is_long else                     (osc.willr_trend == "falling" and osc.williams_r is not None and osc.williams_r >= -30)
    wr_weakening  = (osc.willr_trend == "falling" and osc.williams_r is not None and osc.williams_r >= -30) if is_long else                     (osc.willr_trend == "rising" and osc.williams_r is not None and osc.williams_r <= -70)
    if wr_recovering:
        zone = "oversold" if is_long else "overbought"
        result.add_note(
            f"✅ Williams %R {osc.willr_trend} dari {zone} ({osc.williams_r:.1f}) — sinyal recovery"
        )
        result.confidence_adjustment += 0.02
    elif wr_weakening:
        zone = "overbought" if is_long else "oversold"
        tekanan = "jual" if is_long else "beli"
        result.add_warning(
            f"Williams %R bergerak dari {zone} ({osc.williams_r:.1f}) — tekanan {tekanan}",
            confidence_penalty=0.03,
        )

    # ── ROC — early warning momentum ──────────────────────────────────────────
    if osc.roc is not None and osc.roc_slope is not None:
        exhausting = (osc.roc > 0 and osc.roc_slope < -1.5) if is_long else (osc.roc < 0 and osc.roc_slope > 1.5)
        strengthening = (osc.roc > 0 and osc.roc_slope > 1.0) if is_long else (osc.roc < 0 and osc.roc_slope < -1.0)
        if exhausting:
            result.add_warning(
                f"ROC {osc.roc:+.2f}% tapi melambat (slope={osc.roc_slope:.2f}) "
                f"— momentum mulai habis",
                confidence_penalty=0.05,
            )
        elif strengthening:
            result.add_note(
                f"✅ ROC {osc.roc:+.2f}% akselerasi (slope={osc.roc_slope:.2f}) "
                f"— momentum menguat"
            )
            result.confidence_adjustment += 0.03

    # [v2] ROC fast/slow crossover
    confirming_cross = "bullish" if is_long else "bearish"
    opposing_cross   = "bearish" if is_long else "bullish"
    cross_op         = ">" if is_long else "<"
    cross_op_opp     = "<" if is_long else ">"
    if osc.roc_crossover == confirming_cross:
        result.add_note(
            f"✅ ROC crossover {confirming_cross} (fast={osc.roc:.2f}% {cross_op} slow={osc.roc_slow:.2f}%) "
            f"— momentum shift {'positif' if is_long else 'negatif'}"
        )
        result.confidence_adjustment += 0.03
    elif osc.roc_crossover == opposing_cross:
        result.add_warning(
            f"ROC crossover {opposing_cross} (fast={osc.roc:.2f}% {cross_op_opp} slow={osc.roc_slow:.2f}%) "
            f"— momentum shift {'negatif' if is_long else 'positif'}",
            confidence_penalty=0.04,
        )


def _check_structure_context(iset: IndicatorSet, result: ValidationResult, side: str = "long") -> None:
    """Ichimoku, SAR, Pivot, Fibonacci — posisi harga terhadap struktur pasar.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    Ichimoku cloud/TK-cross/SAR di-mirror; Pivot & Fibonacci pakai sisi
    support/resistance berlawanan (mis. long peduli jarak ke resistance
    utk cek upside, short peduli jarak ke support utk cek downside).
    Cabang "inside cloud" TIDAK diubah -- pasar ragu/sinyal lemah berlaku
    sama untuk kedua arah, genuinely netral.
    """
    st = iset.structure
    if not st.is_valid():
        return

    price = iset.current_price
    if not price or price <= 0:
        return
    is_long = side != "short"

    # Ichimoku — posisi vs cloud
    favorable_cloud   = "above" if is_long else "below"
    unfavorable_cloud = "below" if is_long else "above"
    if st.price_vs_cloud == favorable_cloud:
        arah = "bullish" if is_long else "bearish"
        posisi = "atas" if is_long else "bawah"
        result.add_note(f"✅ Ichimoku: harga di {posisi} cloud — trend {arah} terkonfirmasi")
        result.confidence_adjustment += 0.04
        if st.cloud_thickness and st.cloud_thickness / price > 0.015:
            label = "support" if is_long else "resistance"
            result.add_note(f"✅ Cloud tebal — {label} kuat di {'bawah' if is_long else 'atas'} harga")
            result.confidence_adjustment += 0.02
    elif st.price_vs_cloud == unfavorable_cloud:
        arah = "bearish" if is_long else "bullish"
        posisi = "bawah" if is_long else "atas"
        arah_entry = "long" if is_long else "short"
        result.add_warning(
            f"Ichimoku: harga di {posisi} cloud — trend {arah}, entry {arah_entry} berisiko",
            confidence_penalty=0.08,
        )
    elif st.price_vs_cloud == "inside":
        result.add_warning(
            "Ichimoku: harga dalam cloud — pasar ragu, sinyal lemah",
            confidence_penalty=0.04,
        )

    confirming_tk = "bullish" if is_long else "bearish"
    opposing_tk   = "bearish" if is_long else "bullish"
    if st.tk_cross == confirming_tk:
        result.add_note(f"✅ Ichimoku TK Cross {confirming_tk} — momentum entry terkonfirmasi")
        result.confidence_adjustment += 0.03
    elif st.tk_cross == opposing_tk:
        result.add_warning(
            f"Ichimoku TK Cross {opposing_tk} — momentum berbalik",
            confidence_penalty=0.06,
        )

    # Parabolic SAR
    favorable_sar   = "up" if is_long else "down"
    unfavorable_sar = "down" if is_long else "up"
    if st.sar_direction == favorable_sar:
        label = "uptrend" if is_long else "downtrend"
        result.add_note(f"✅ SAR {label} (${st.sar_value:.6f}) — trailing support/resistance aktif")
        result.confidence_adjustment += 0.02
    elif st.sar_direction == unfavorable_sar:
        label = "downtrend" if is_long else "uptrend"
        posisi = "bawah" if is_long else "atas"
        arah_entry = "long" if is_long else "short"
        result.add_warning(
            f"SAR {label} (${st.sar_value:.6f}) — harga di {posisi} SAR, hindari entry {arah_entry}",
            confidence_penalty=0.07,
        )

    # Pivot Points — long peduli jarak ke resistance (upside), short peduli jarak ke support (downside)
    barrier_level = st.nearest_resistance if is_long else st.nearest_support
    barrier_label = "resistance" if is_long else "support"
    ruang_label   = "upside" if is_long else "downside"
    if barrier_level and price > 0:
        dist_pct = abs(barrier_level - price) / price * 100
        if dist_pct < 1.0:
            result.add_warning(
                f"Pivot: harga hanya {dist_pct:.2f}% dari {barrier_label} "
                f"(${barrier_level:.6f}) — {ruang_label} sangat terbatas",
                confidence_penalty=0.08,
            )
        elif dist_pct < 2.0:
            result.add_warning(
                f"Pivot: {barrier_label} dekat ({dist_pct:.2f}%) — "
                f"perhatikan R/R",
                confidence_penalty=0.03,
            )
        elif dist_pct > 4.0:
            result.add_note(
                f"✅ Pivot: ruang gerak {dist_pct:.2f}% sebelum {barrier_label} — "
                f"R/R favorable"
            )
            result.confidence_adjustment += 0.02

    entry_zone_level = st.nearest_support if is_long else st.nearest_resistance
    entry_zone_label = "support" if is_long else "resistance"
    if entry_zone_level and price > 0:
        dist_zone_pct = abs(price - entry_zone_level) / price * 100
        if dist_zone_pct < 1.5:
            result.add_note(
                f"✅ Pivot: harga dekat {entry_zone_label} (${entry_zone_level:.6f}, "
                f"{dist_zone_pct:.2f}%) — zona entry ideal"
            )
            result.confidence_adjustment += 0.03

    # Fibonacci 61.8% — level kunci, netral (berfungsi sbg support/resistance di kedua arah)
    if st.fib_618 and price > 0:
        dist_fib_pct = abs(price - st.fib_618) / price * 100
        if dist_fib_pct < 0.8:
            result.add_note(
                f"✅ Fibonacci: harga di golden ratio 61.8% "
                f"(${st.fib_618:.6f}) — level support/resistance terkuat"
            )
            result.confidence_adjustment += 0.04

    # Fibonacci barrier -- long pakai fib resistance (batasi upside),
    # short pakai fib support (batasi downside)
    fib_barrier = st.nearest_fib_resistance if is_long else st.nearest_fib_support
    fib_barrier_label = "resistance" if is_long else "support"
    fib_ruang_label = "upside" if is_long else "downside"
    if fib_barrier and price > 0:
        dist_fib_barrier = abs(fib_barrier - price) / price * 100
        if dist_fib_barrier < 1.5:
            result.add_warning(
                f"Fibonacci: {fib_barrier_label} Fib dekat ({dist_fib_barrier:.2f}%) — "
                f"{fib_ruang_label} terbatas",
                confidence_penalty=0.04,
            )


def _check_orderbook_context(iset: IndicatorSet, result: ValidationResult, side: str = "long") -> None:
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

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    imbalance/whale wall/cluster wall/absorption di-mirror (ask<->bid tertukar
    makna favorable/unfavorable), whale_score threshold di-mirror. TIDAK
    diubah (genuinely netral, soal kualitas eksekusi bukan arah): spoofing_confidence,
    liquidity_score, spread_score.
    """
    ob = iset.orderbook
    if not ob.is_valid():
        return

    price = iset.current_price
    is_long = side != "short"

    # ── Imbalance ─────────────────────────────────────────────────────────────
    imb = ob.bid_ask_imbalance
    if imb is not None:
        favorable_imb = imb >= 0.65 if is_long else imb <= 0.35
        unfavorable_imb = imb <= 0.35 if is_long else imb >= 0.65
        if favorable_imb:
            side_label = "bid" if is_long else "ask"
            tekanan = "beli" if is_long else "jual"
            result.add_note(
                f"✅ Orderbook: {side_label} dominan ({imb:.2f}, score={ob.imbalance_score:.0f}) — "
                f"tekanan {tekanan} kuat, mendukung entry"
            )
            result.confidence_adjustment += 0.04
        elif unfavorable_imb:
            side_label = "ask" if is_long else "bid"
            tekanan = "jual" if is_long else "beli"
            arah_entry = "long" if is_long else "short"
            result.add_warning(
                f"Orderbook: {side_label} dominan ({imb:.2f}, score={ob.imbalance_score:.0f}) — "
                f"tekanan {tekanan} kuat, hati-hati entry {arah_entry}",
                confidence_penalty=0.08,
            )
        elif (imb >= 0.55) if is_long else (imb <= 0.45):
            side_label = "bid" if is_long else "ask"
            result.add_note(f"📊 Orderbook: sedikit condong {side_label} ({imb:.2f})")

    # ── Whale walls (single level): utk long, ask wall=resistance(bad),
    # bid wall=support(good). Utk short, MIRROR: ask wall=favorable (potensi
    # rejection turun), bid wall=unfavorable (potensi rejection naik) ──────────
    favorable_wall_price   = ob.whale_bid_wall if is_long else ob.whale_ask_wall
    favorable_wall_strength = ob.bid_wall_strength if is_long else ob.ask_wall_strength
    favorable_wall_dist     = ob.bid_wall_dist if is_long else ob.ask_wall_dist
    unfavorable_wall_price    = ob.whale_ask_wall if is_long else ob.whale_bid_wall
    unfavorable_wall_strength = ob.ask_wall_strength if is_long else ob.bid_wall_strength
    unfavorable_wall_dist     = ob.ask_wall_dist if is_long else ob.bid_wall_dist

    if unfavorable_wall_price and unfavorable_wall_strength:
        dist_adj = unfavorable_wall_dist if unfavorable_wall_dist is not None else 1.0
        eff_str  = unfavorable_wall_strength * dist_adj
        label = "ask" if is_long else "bid"
        result.add_warning(
            f"Whale {label} wall di ${unfavorable_wall_price:.6f} "
            f"({unfavorable_wall_strength:.1f}% vol, relevance={dist_adj:.2f}, eff={eff_str:.1f}) — "
            f"resistance/support dari whale{' (dekat harga, sangat relevan)' if dist_adj > 0.7 else ' (jauh, pengaruh kecil)'}",
            confidence_penalty=0.06 * dist_adj,
        )

    if favorable_wall_price and favorable_wall_strength:
        dist_adj = favorable_wall_dist if favorable_wall_dist is not None else 1.0
        label = "bid" if is_long else "ask"
        result.add_note(
            f"✅ Whale {label} wall di ${favorable_wall_price:.6f} "
            f"({favorable_wall_strength:.1f}% vol, relevance={dist_adj:.2f}) — "
            f"support/resistance kuat dari whale searah posisi"
        )
        result.confidence_adjustment += 0.03 * dist_adj

    # ── Cluster walls (MSL-3): lebih reliable dari single wall ────────────────
    favorable_cluster_wall = ob.cluster_bid_wall if is_long else ob.cluster_ask_wall
    favorable_cluster_str  = ob.cluster_bid_str if is_long else ob.cluster_ask_str
    favorable_whale_ref    = ob.whale_bid_wall if is_long else ob.whale_ask_wall
    if favorable_cluster_wall and favorable_cluster_str:
        is_different = (favorable_cluster_wall != favorable_whale_ref)
        dist_adj = favorable_wall_dist if favorable_wall_dist is not None else 1.0
        label = "bid" if is_long else "ask"
        if is_different:
            result.add_note(
                f"✅ Cluster {label} wall di ${favorable_cluster_wall:.6f} "
                f"({favorable_cluster_str:.1f}% vol) — "
                f"support/resistance berlapis: whale + cluster di level berbeda"
            )
            result.confidence_adjustment += 0.03 * dist_adj
        else:
            result.add_note(
                f"✅ {label.capitalize()} wall dikonfirmasi cluster ({favorable_cluster_str:.1f}% vol) — "
                f"wall lebih genuine, bukan single order"
            )
            result.confidence_adjustment += 0.02

    unfavorable_cluster_wall = ob.cluster_ask_wall if is_long else ob.cluster_bid_wall
    unfavorable_cluster_str  = ob.cluster_ask_str if is_long else ob.cluster_bid_str
    unfavorable_whale_ref    = ob.whale_ask_wall if is_long else ob.whale_bid_wall
    if unfavorable_cluster_wall and unfavorable_cluster_str:
        is_different = (unfavorable_cluster_wall != unfavorable_whale_ref)
        dist_adj = unfavorable_wall_dist if unfavorable_wall_dist is not None else 1.0
        label = "ask" if is_long else "bid"
        if is_different:
            result.add_warning(
                f"Cluster {label} wall di ${unfavorable_cluster_wall:.6f} "
                f"({unfavorable_cluster_str:.1f}% vol) — "
                f"resistance/support berlapis: whale + cluster",
                confidence_penalty=0.04 * dist_adj,
            )

    # ── Absorption: long favorable=absorbed_ask(breakout up), short
    # favorable=absorbed_bid(breakdown down) ──────────────────────────────────
    favorable_absorbed   = ob.absorbed_ask if is_long else ob.absorbed_bid
    unfavorable_absorbed = ob.absorbed_bid if is_long else ob.absorbed_ask
    if favorable_absorbed:
        label = "ASK" if is_long else "BID"
        arah = "breakout" if is_long else "breakdown"
        result.add_note(
            f"🚀 Orderbook: whale {label} wall terserap — "
            f"{arah} signal kuat (absorption_score={ob.absorption_score:.0f})"
        )
        result.confidence_adjustment += 0.06

    if unfavorable_absorbed:
        label = "BID" if is_long else "ASK"
        tekanan = "jual" if is_long else "beli"
        result.add_warning(
            f"⚠️ Orderbook: whale {label} wall terserap — "
            f"signal berlawanan arah: level whale gagal menahan tekanan {tekanan} "
            f"(absorption_score={ob.absorption_score:.0f})",
            confidence_penalty=0.07,
        )

    # ── Spoofing confidence (netral, soal kualitas data bukan arah) ──────────
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

    # ── Liquidity score (netral, soal kualitas eksekusi bukan arah) ──────────
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

    # ── Spread score (netral, kontekstual vs baseline historis coin) ─────────
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

    # ── Whale score composite: mirror threshold di sekitar titik tengah ──────
    ws = ob.whale_score
    if ws is not None:
        favorable_ws = ws >= 65 if is_long else ws <= 35
        unfavorable_ws = ws <= 35 if is_long else ws >= 65
        if favorable_ws:
            arah = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ Whale score {ws:.0f} — "
                f"aktivitas whale net {arah}, mendukung entry"
            )
            result.confidence_adjustment += 0.02
        elif unfavorable_ws:
            arah = "bearish" if is_long else "bullish"
            result.add_warning(
                f"Whale score {ws:.0f} — "
                f"aktivitas whale net {arah}",
                confidence_penalty=0.04,
            )


def _check_trend_cross_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan golden_cross_bars_ago, dead_cross_bars_ago, supertrend_value.

    golden_cross_bars_ago → freshness: cross baru (≤10) = momentum kuat,
                             stale (>50) = sudah terlambat, konfirmasi lemah
    dead_cross_bars_ago   → bearish event baru-baru ini = waspada entry long
    supertrend_value      → level harga ST line = dynamic support/resistance
                             yang bisa dipakai untuk contextual SL reference

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    golden_cross jadi opposing signal, dead_cross jadi confirming signal
    (mirror total), SuperTrend direction check di-swap.
    """
    tr = iset.trend
    if tr is None:
        return

    price = iset.current_price
    is_long = side != "short"

    # -- Cross freshness: confirming cross utk long=golden, utk short=dead --
    confirming_bars = tr.golden_cross_bars_ago if is_long else tr.dead_cross_bars_ago
    opposing_bars   = tr.dead_cross_bars_ago if is_long else tr.golden_cross_bars_ago
    confirming_label = "Golden cross" if is_long else "Dead cross"
    opposing_label    = "Dead cross" if is_long else "Golden cross"
    arah_momentum     = "bullish" if is_long else "bearish"
    arah_momentum_opp = "bearish" if is_long else "bullish"

    if confirming_bars is not None:
        if confirming_bars <= 5:
            result.add_note(
                f"🚀 {confirming_label} SEGAR ({confirming_bars} bar lalu) — "
                f"momentum {arah_momentum} dalam puncak kekuatan"
            )
            result.confidence_adjustment += 0.07
        elif confirming_bars <= 15:
            result.add_note(
                f"✅ {confirming_label} masih fresh ({confirming_bars} bar lalu) — "
                f"momentum {arah_momentum} terkonfirmasi"
            )
            result.confidence_adjustment += 0.04
        elif confirming_bars <= 50:
            result.add_note(
                f"📊 {confirming_label} {confirming_bars} bar lalu — "
                f"trend {arah_momentum} established, momentum mulai melambat"
            )

    # -- Opposing cross: sinyal berlawanan baru-baru ini = penalti entry --
    if opposing_bars is not None:
        entry_label = "long" if is_long else "short"
        if opposing_bars <= 5:
            result.add_warning(
                f"⚠️ {opposing_label} SEGAR ({opposing_bars} bar lalu) — "
                f"momentum {arah_momentum_opp} baru dimulai, hindari {entry_label}",
                confidence_penalty=0.09,
            )
        elif opposing_bars <= 20:
            result.add_warning(
                f"{opposing_label} {opposing_bars} bar lalu — "
                f"trend {arah_momentum_opp} masih aktif, entry {entry_label} berisiko",
                confidence_penalty=0.05,
            )

    # -- Supertrend value sebagai dynamic S/R reference --
    if tr.supertrend_value and price:
        dist_pct = (price - tr.supertrend_value) / tr.supertrend_value * 100
        aligned_st_dir = 1 if is_long else -1
        opposing_st_dir = -1 if is_long else 1
        if tr.supertrend_direction == aligned_st_dir:
            dist_check = dist_pct if is_long else -dist_pct
            label = "support" if is_long else "resistance"
            if 0 < dist_check < 1.5:
                result.add_note(
                    f"✅ Harga sangat dekat SuperTrend {label} "
                    f"(${tr.supertrend_value:.6f}, {dist_pct:+.2f}%) — "
                    f"entry searah ST {label}, SL reference jelas"
                )
                result.confidence_adjustment += 0.03
            elif dist_check <= 0:
                posisi = "bawah" if is_long else "atas"
                arah = "bearish" if is_long else "bullish"
                result.add_warning(
                    f"Harga di {posisi} SuperTrend ({dist_pct:+.2f}%) — "
                    f"ST belum flip {arah} tapi harga sudah tembus, waspada",
                    confidence_penalty=0.04,
                )
        elif tr.supertrend_direction == opposing_st_dir:
            arah = "bearish" if is_long else "bullish"
            label = "resistance" if is_long else "support"
            posisi = "atas" if is_long else "bawah"
            trend_label = "down" if is_long else "up"
            result.add_warning(
                f"SuperTrend {arah} (line=${tr.supertrend_value:.6f}) — "
                f"dynamic {label} di {posisi} harga, trend {trend_label}",
                confidence_penalty=0.05,
            )


def _check_vwap_band_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan vwap_upper_1/2, vwap_lower_1/2.

    Bands VWAP ±1σ ±2σ memberi konteks presisi posisi harga dalam distribusi
    volume harian. Lebih informatif dari sekadar 'above/below VWAP'.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh 6 zona di-mirror -- upper jadi unfavorable utk long/favorable
    utk short, dan sebaliknya.
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

    is_long = side != "short"
    # Untuk short, band atas/bawah scan-nya dibalik (cek dari extreme yg
    # favorable dulu: short favorable = upper band, long favorable = lower band)

    if is_long:
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
    else:
        # Mirror penuh: short favorable di UPPER band, unfavorable di LOWER band
        if price <= l2:
            result.add_warning(
                f"Harga di bawah VWAP -2σ (${l2:.6f}) — "
                f"extreme oversold vs distribusi volume, mean-reversion risk tinggi",
                confidence_penalty=0.07,
            )
        elif price <= l1:
            result.add_warning(
                f"Harga di zona VWAP -1σ–-2σ (${l2:.6f}–${l1:.6f}) — "
                f"stretched di bawah VWAP, R/R kurang ideal untuk entry baru",
                confidence_penalty=0.03,
            )
        elif price <= vwap:
            dist_to_l1 = (price - l1) / price * 100
            result.add_note(
                f"✅ Harga di bawah VWAP (${vwap:.6f}), ruang ke -1σ={dist_to_l1:.2f}% — "
                f"bearish VWAP zone dengan room yang cukup"
            )
            result.confidence_adjustment += 0.03
        elif price <= u1:
            dist_to_vwap = (price - vwap) / price * 100
            result.add_note(
                f"Harga di atas VWAP ({dist_to_vwap:.2f}%) tapi di bawah +1σ — "
                f"sedikit bullish tapi masih dalam distribusi normal"
            )
        elif price <= u2:
            result.add_note(
                f"✅ Harga di zona VWAP +1σ–+2σ (${u1:.6f}–${u2:.6f}) — "
                f"value zone: banyak volume transacted di bawah level ini, overbought VWAP"
            )
            result.confidence_adjustment += 0.04
        else:
            result.add_note(
                f"✅ Harga di atas VWAP +2σ (${u2:.6f}) — "
                f"extreme overbought vs distribusi volume, strong mean-reversion kandidat"
            )
            result.confidence_adjustment += 0.05


def _check_ichimoku_detail_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan tenkan, kijun, senkou_a/b, chikou, cloud_top/bottom.

    Level-level Ichimoku memberi 5 lapisan konfirmasi yang saat ini hanya
    dipakai satu (price_vs_cloud). Tiap level = S/R dinamis tersendiri.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh 5 sub-check (tenkan/kijun, TK gap, chikou, cloud dist, kumo) di-mirror.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return
    is_long = side != "short"

    # -- Tenkan/Kijun sebagai dynamic S/R --
    if st.tenkan and st.kijun:
        triple_aligned  = (price > st.tenkan > st.kijun) if is_long else (price < st.tenkan < st.kijun)
        both_opposing    = (price < st.tenkan and price < st.kijun) if is_long else (price > st.tenkan and price > st.kijun)
        if triple_aligned:
            arah = "bullish" if is_long else "bearish"
            cmp_op = ">" if is_long else "<"
            result.add_note(
                f"✅ Ichimoku: price {cmp_op} tenkan (${st.tenkan:.6f}) {cmp_op} kijun (${st.kijun:.6f}) — "
                f"triple {arah} alignment"
            )
            result.confidence_adjustment += 0.04
        elif both_opposing:
            posisi = "bawah" if is_long else "atas"
            arah = "bearish" if is_long else "bullish"
            result.add_warning(
                f"Ichimoku: price di {posisi} tenkan & kijun — "
                f"short-term dan medium-term momentum keduanya {arah}",
                confidence_penalty=0.05,
            )
        elif (price > st.kijun and price < st.tenkan) if is_long else (price < st.kijun and price > st.tenkan):
            result.add_note(
                f"📊 Harga di antara kijun (${st.kijun:.6f}) dan tenkan (${st.tenkan:.6f}) — "
                f"momentum campuran, kijun masih jadi support/resistance"
            )

        # Tenkan-Kijun gap: gap besar searah posisi = trend kuat
        tk_gap_pct = abs(st.tenkan - st.kijun) / st.kijun * 100 if st.kijun else 0
        gap_aligned = (st.tenkan > st.kijun) if is_long else (st.tenkan < st.kijun)
        if tk_gap_pct > 2.0 and gap_aligned:
            arah = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ TK gap lebar ({tk_gap_pct:.1f}%) — trend {arah} kuat"
            )
            result.confidence_adjustment += 0.02

    # -- Chikou: konfirmasi lagging --
    if st.chikou and price:
        confirming_chikou = (st.chikou > price) if is_long else (st.chikou < price)
        if confirming_chikou:
            arah = "bullish" if is_long else "bearish"
            cmp_op = ">" if is_long else "<"
            result.add_note(
                f"✅ Chikou (${st.chikou:.6f}) {cmp_op} harga sekarang — "
                f"lagging confirmation {arah}"
            )
            result.confidence_adjustment += 0.02
        else:
            arah = "bullish" if is_long else "bearish"
            cmp_op = "<" if is_long else ">"
            result.add_warning(
                f"Chikou (${st.chikou:.6f}) {cmp_op} harga sekarang — "
                f"lagging span belum konfirmasi {arah}",
                confidence_penalty=0.03,
            )

    # -- Cloud top/bottom sebagai level S/R eksplisit --
    favorable_cloud_side   = "above" if is_long else "below"
    unfavorable_cloud_side = "below" if is_long else "above"
    if st.cloud_top and st.cloud_bottom and price:
        if st.price_vs_cloud == favorable_cloud_side:
            cloud_edge = st.cloud_top if is_long else st.cloud_bottom
            dist_to_cloud = abs(price - cloud_edge) / price * 100
            if dist_to_cloud < 1.5:
                result.add_note(
                    f"⚠️ Harga hanya {dist_to_cloud:.2f}% di sisi cloud "
                    f"(${cloud_edge:.6f}) — dekat edge cloud, risiko pullback ke cloud"
                )
            elif dist_to_cloud > 5.0:
                label = "support" if is_long else "resistance"
                result.add_note(
                    f"✅ Harga {dist_to_cloud:.2f}% dari cloud — "
                    f"jarak aman dari cloud {label}"
                )
                result.confidence_adjustment += 0.02
        elif st.price_vs_cloud == unfavorable_cloud_side:
            cloud_edge = st.cloud_bottom if is_long else st.cloud_top
            dist_to_cloud = abs(cloud_edge - price) / price * 100
            label = "resistance" if is_long else "support"
            result.add_warning(
                f"Harga {dist_to_cloud:.2f}% di sisi berlawanan cloud "
                f"(${cloud_edge:.6f}) — cloud {label} kuat",
                confidence_penalty=0.04,
            )

    # -- Senkou A vs B: kumo twist / cloud quality --
    if st.senkou_a and st.senkou_b:
        kumo_favorable = (st.senkou_a > st.senkou_b) if is_long else (st.senkou_a < st.senkou_b)
        if kumo_favorable:
            arah = "bullish" if is_long else "bearish"
            cmp_op = ">" if is_long else "<"
            trend_label = "uptrend" if is_long else "downtrend"
            result.add_note(f"✅ Kumo {arah} (Senkou A {cmp_op} B) — cloud mendukung {trend_label}")
            result.confidence_adjustment += 0.02
        else:
            arah = "bearish" if is_long else "bullish"
            cmp_op = "<" if is_long else ">"
            result.add_warning(
                f"Kumo {arah} (Senkou A {cmp_op} B) — cloud resistance/support lebih kuat",
                confidence_penalty=0.03,
            )


def _check_pivot_ladder_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan r1/r2/r3, s1/s2/s3, price_vs_pivot.

    Pivot ladder lengkap memungkinkan kalkulasi R/R ke target R1/R2 dan
    SL reference ke S1/S2 — jauh lebih presisi dari sekadar nearest_resistance.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    target profit pakai S1/S2 (bukan R1/R2), SL reference pakai R1 (bukan S1).
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return
    is_long = side != "short"

    # -- price_vs_pivot: intraday directional bias --
    favorable_pivot_side = "above" if is_long else "below"
    unfavorable_pivot_side = "below" if is_long else "above"
    if st.price_vs_pivot:
        if st.price_vs_pivot == favorable_pivot_side:
            posisi = "atas" if is_long else "bawah"
            arah = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ Harga di {posisi} daily pivot (${st.pivot:.6f}) — "
                f"intraday bias {arah}"
            )
            result.confidence_adjustment += 0.02
        elif st.price_vs_pivot == unfavorable_pivot_side:
            posisi = "bawah" if is_long else "atas"
            arah = "bearish" if is_long else "bullish"
            result.add_warning(
                f"Harga di {posisi} daily pivot (${st.pivot:.6f}) — "
                f"intraday bias {arah}",
                confidence_penalty=0.04,
            )

    # -- R/R ke target profit dan SL reference (long: R1=target,S1=SL; short: S1=target,R1=SL) --
    target_level = st.r1 if is_long else st.s1
    sl_ref_level = st.s1 if is_long else st.r1
    target_label = "R1" if is_long else "S1"
    sl_label      = "S1" if is_long else "R1"
    if target_level and sl_ref_level:
        dist_target_pct = abs(target_level - price) / price * 100
        dist_sl_pct     = abs(price - sl_ref_level) / price * 100

        if dist_target_pct > 0 and dist_sl_pct > 0:
            rr = dist_target_pct / dist_sl_pct if dist_sl_pct > 0 else 0
            if rr >= 2.0:
                result.add_note(
                    f"✅ Pivot R/R: target {target_label}={dist_target_pct:.2f}% / SL ke {sl_label}={dist_sl_pct:.2f}% "
                    f"→ R/R={rr:.1f}x"
                )
                result.confidence_adjustment += 0.03
            elif rr < 1.0:
                result.add_warning(
                    f"Pivot R/R buruk: target {target_label}={dist_target_pct:.2f}% / SL ke {sl_label}={dist_sl_pct:.2f}% "
                    f"→ R/R={rr:.1f}x (< 1.0)",
                    confidence_penalty=0.05,
                )

        # Jarak ke target sangat kecil = ruang gerak terbatas
        if dist_target_pct < 0.8:
            arah_label = "upside" if is_long else "downside"
            result.add_warning(
                f"{target_label} sangat dekat ({dist_target_pct:.2f}%) — "
                f"{arah_label} ke target pivot pertama sangat terbatas",
                confidence_penalty=0.05,
            )

    # -- Target ke-2 sebagai extended target --
    target2_level = st.r2 if is_long else st.s2
    target2_label = "R2" if is_long else "S2"
    if target2_level and price:
        dist_target2 = abs(target2_level - price) / price * 100
        if dist_target2 > 3.0:
            result.add_note(
                f"✅ {target2_label} target (${target2_level:.6f}) = {dist_target2:.2f}% — "
                f"extended target tersedia"
            )


def _check_market_structure_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan trend_structure, structure_event, last_swing_high/low,
    market_structure_score, sr_zones, nearest_structure_support/resistance.

    Market structure (HH/HL = uptrend, LH/LL = downtrend) dan BOS/CHoCH
    adalah konfirmasi paling fundamental apakah harga bergerak searah signal.

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    seluruh 5 sub-check di-mirror -- HH/HL<->LH/LL, BOS/CHoCH bullish<->bearish,
    market_structure_score threshold tinggi/rendah tertukar, S/R clustered
    support<->resistance, posisi swing range atas/bawah tertukar.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return
    is_long = side != "short"

    # -- trend_structure: HH/HL vs LH/LL --
    ts = st.trend_structure
    strong_favorable = ("HH_HL", "strong_uptrend") if is_long else ("LH_LL", "strong_downtrend")
    weak_favorable   = ("HL_only", "weak_uptrend") if is_long else ("LH_only", "weak_downtrend")
    strong_opposing  = ("LH_LL", "strong_downtrend") if is_long else ("HH_HL", "strong_uptrend")
    weak_opposing    = ("LH_only", "weak_downtrend") if is_long else ("HL_only", "weak_uptrend")
    arah = "BULLISH" if is_long else "BEARISH"
    arah_opp = "BEARISH" if is_long else "BULLISH"

    if ts in strong_favorable:
        label = "Higher Highs + Higher Lows" if is_long else "Lower Highs + Lower Lows"
        result.add_note(
            f"✅ Market structure {arah} ({ts}) — "
            f"{label} terkonfirmasi"
        )
        result.confidence_adjustment += 0.06
    elif ts in weak_favorable:
        result.add_note(
            f"✅ Market structure lemah {arah.lower()} ({ts}) — "
            f"struktur mulai terbentuk, belum full konfirmasi"
        )
        result.confidence_adjustment += 0.02
    elif ts in strong_opposing:
        label = "Lower Highs + Lower Lows" if is_long else "Higher Highs + Higher Lows"
        arah_entry = "long" if is_long else "short"
        result.add_warning(
            f"Market structure {arah_opp} ({ts}) — "
            f"{label}: entry {arah_entry} melawan struktur",
            confidence_penalty=0.08,
        )
    elif ts in weak_opposing:
        result.add_warning(
            f"Market structure condong {arah_opp.lower()} ({ts}) — "
            f"struktur belum {arah.lower()}",
            confidence_penalty=0.04,
        )

    # -- structure_event: BOS / CHoCH --
    ev = st.structure_event
    bos_favorable   = "BOS_bullish" if is_long else "BOS_bearish"
    choch_favorable = "CHoCH_bullish" if is_long else "CHoCH_bearish"
    bos_opposing    = "BOS_bearish" if is_long else "BOS_bullish"
    choch_opposing  = "CHoCH_bearish" if is_long else "CHoCH_bullish"

    if ev == bos_favorable:
        arah_label = "BULLISH" if is_long else "BEARISH"
        swing_label = "swing high" if is_long else "swing low"
        result.add_note(
            f"🚀 Break of Structure {arah_label} (BOS) — "
            f"harga tembus {swing_label} sebelumnya: konfirmasi trend continuation"
        )
        result.confidence_adjustment += 0.07
    elif ev == choch_favorable:
        arah_label = "BULLISH" if is_long else "BEARISH"
        prior_trend = "downtrend" if is_long else "uptrend"
        result.add_note(
            f"🔥 Change of Character {arah_label} (CHoCH) — "
            f"struktur berbalik {arah_label.lower()} setelah {prior_trend}: high-probability reversal"
        )
        result.confidence_adjustment += 0.08
    elif ev == bos_opposing:
        swing_label = "swing low" if is_long else "swing high"
        arah_entry = "long" if is_long else "short"
        arah_label = "BEARISH" if is_long else "BULLISH"
        result.add_warning(
            f"Break of Structure {arah_label} — harga tembus {swing_label}: "
            f"trend {arah_label.lower()} terkonfirmasi, hindari {arah_entry}",
            confidence_penalty=0.09,
        )
    elif ev == choch_opposing:
        arah_label = "BEARISH" if is_long else "BULLISH"
        result.add_warning(
            f"Change of Character {arah_label} (CHoCH) — "
            f"struktur berbalik {arah_label.lower()}: high-probability reversal {arah_label.lower()}",
            confidence_penalty=0.10,
        )

    # -- market_structure_score: mirror threshold di sekitar titik tengah --
    mss = st.market_structure_score
    if mss is not None:
        favorable_score   = mss >= 70 if is_long else mss <= 30
        unfavorable_score = mss <= 30 if is_long else mss >= 70
        if favorable_score:
            arah_label = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ Market structure score {'tinggi' if is_long else 'rendah'} ({mss:.0f}) — "
                f"kualitas struktur {arah_label} sangat baik"
            )
            result.confidence_adjustment += 0.03
        elif unfavorable_score:
            arah_label = "lemah/bearish" if is_long else "lemah/bullish"
            result.add_warning(
                f"Market structure score {'rendah' if is_long else 'tinggi'} ({mss:.0f}) — "
                f"struktur market {arah_label}",
                confidence_penalty=0.04,
            )

    # -- nearest_structure_support/resistance: long pakai support(favorable)/
    # resistance(unfavorable), short mirror pakai resistance/support --
    favorable_level   = st.nearest_structure_support if is_long else st.nearest_structure_resistance
    unfavorable_level = st.nearest_structure_resistance if is_long else st.nearest_structure_support
    favorable_label   = "support" if is_long else "resistance"
    unfavorable_label = "resistance" if is_long else "support"

    if favorable_level and price:
        dist_fav = abs(price - favorable_level) / price * 100
        if dist_fav < 1.0:
            result.add_note(
                f"✅ Clustered S/R {favorable_label} sangat dekat "
                f"(${favorable_level:.6f}, {dist_fav:.2f}%) — "
                f"zona {favorable_label} multi-confluence di sisi entry"
            )
            result.confidence_adjustment += 0.04

    if unfavorable_level and price:
        dist_unfav = abs(unfavorable_level - price) / price * 100
        if 0 < dist_unfav < 1.5:
            ruang_label = "upside" if is_long else "downside"
            result.add_warning(
                f"Clustered S/R {unfavorable_label} dekat "
                f"(${unfavorable_level:.6f}, {dist_unfav:.2f}%) — "
                f"zona {unfavorable_label} multi-confluence membatasi {ruang_label}",
                confidence_penalty=0.05,
            )

    # -- last_swing_high/low untuk R/R reference --
    if st.last_swing_high and st.last_swing_low and price:
        swing_range = st.last_swing_high - st.last_swing_low
        pos_in_swing = (price - st.last_swing_low) / swing_range if swing_range > 0 else 0.5
        favorable_zone_swing   = pos_in_swing <= 0.35 if is_long else pos_in_swing >= 0.65
        unfavorable_zone_swing = pos_in_swing >= 0.75 if is_long else pos_in_swing <= 0.25
        if favorable_zone_swing:
            ref_label = "swing low" if is_long else "swing high"
            ref_val = st.last_swing_low if is_long else st.last_swing_high
            result.add_note(
                f"✅ Harga di bagian {'bawah' if is_long else 'atas'} swing range ({pos_in_swing:.0%}) — "
                f"{ref_label} (${ref_val:.6f}) dekat, entry low-risk"
            )
            result.confidence_adjustment += 0.03
        elif unfavorable_zone_swing:
            result.add_warning(
                f"Harga di bagian {'atas' if is_long else 'bawah'} swing range ({pos_in_swing:.0%}) — "
                f"entry di sisi kurang ideal, R/R tidak optimal",
                confidence_penalty=0.04,
            )


def _check_fib_detail_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
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

    is_long = side != "short"
    # -- fib_trend: konfirmasi arah (long favorable="up", short favorable="down") --
    favorable_trend = "up" if is_long else "down"
    opposing_trend  = "down" if is_long else "up"
    if st.fib_trend == opposing_trend:
        swing_desc = "tertinggi ke terendah" if is_long else "terendah ke tertinggi"
        result.add_warning(
            f"Fibonacci trend: {'downswing' if is_long else 'upswing'} — fib level dihitung dari swing {swing_desc}, "
            f"harga dalam koreksi",
            confidence_penalty=0.04,
        )
    elif st.fib_trend == favorable_trend:
        swing_desc = "rendah ke tinggi" if is_long else "tinggi ke rendah"
        arah = "bullish" if is_long else "bearish"
        result.add_note(
            f"✅ Fibonacci trend: {'upswing' if is_long else 'downswing'} — fib dihitung dari swing {swing_desc}, "
            f"mengonfirmasi struktur {arah}"
        )
        result.confidence_adjustment += 0.02

    # -- Cek apakah harga di level Fibonacci kunci (toleransi 0.5%) --
    # Netral thd arah -- fib level berfungsi sbg S/R di kedua arah posisi.
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

    # -- fib_ext sebagai profit target reference. Field ini otomatis proyeksi
    # naik (swing_high+diff) kalau fib_trend="up", atau turun (swing_low-diff)
    # kalau fib_trend="down" (lihat indicators/structure.py calculate_fibonacci).
    # Long peduli target DI ATAS harga, short peduli target DI BAWAH harga.
    if st.fib_ext_1272 and st.fib_ext_1618 and price:
        to_1272 = (st.fib_ext_1272 - price) / price * 100
        to_1618 = (st.fib_ext_1618 - price) / price * 100
        to_1272_check = to_1272 if is_long else -to_1272
        to_1618_check = to_1618 if is_long else -to_1618
        if to_1272_check > 2.0:
            result.add_note(
                f"✅ Fib extension 1.272 target: ${st.fib_ext_1272:.6f} ({to_1272:+.2f}%) — "
                f"profit target konservatif tersedia"
            )
        if to_1618_check > 3.0:
            result.add_note(
                f"✅ Fib extension 1.618 target: ${st.fib_ext_1618:.6f} ({to_1618:+.2f}%) — "
                f"profit target agresif tersedia"
            )


def _check_donchian_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan donchian_upper/lower/middle, donchian_pct_b,
    donchian_width_pct, donchian_score.

    Donchian Channel = breakout system: upper = recent high, lower = recent low.
    donchian_pct_b [0-1]: posisi harga dalam channel
    donchian_width_pct: lebar channel relatif = volatility context
    donchian_score: composite Donchian score

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    posisi channel & donchian_score di-mirror. Width (narrow/wide) TIDAK
    diubah -- volatilitas/breakout-setup itu sendiri direction-agnostic,
    berlaku sama utk kedua arah.
    """
    st = iset.structure
    if not st or not st.is_valid():
        return
    price = iset.current_price
    if not price:
        return
    is_long = side != "short"

    pct_b = st.donchian_pct_b
    width = st.donchian_width_pct
    upper = st.donchian_upper
    lower = st.donchian_lower
    mid   = st.donchian_middle

    if pct_b is None or upper is None or lower is None:
        return

    # -- Posisi dalam channel (mirror utk short) --
    unfavorable_zone = pct_b >= 0.85 if is_long else pct_b <= 0.15
    favorable_zone   = pct_b <= 0.20 if is_long else pct_b >= 0.80
    if unfavorable_zone:
        level = upper if is_long else lower
        label = "upper" if is_long else "lower"
        result.add_warning(
            f"Donchian pct_b={pct_b:.2f} — harga di {label} channel "
            f"(${level:.6f}): potensi resistance/support di recent extreme",
            confidence_penalty=0.05,
        )
    elif favorable_zone:
        level = lower if is_long else upper
        label = "lower" if is_long else "upper"
        result.add_note(
            f"✅ Donchian pct_b={pct_b:.2f} — harga di {label} channel "
            f"(${level:.6f}): dekat recent extreme, potential reversal zone"
        )
        result.confidence_adjustment += 0.04
    elif 0.40 <= pct_b <= 0.65 and mid:
        result.add_note(
            f"Donchian: harga di mid-channel (pct_b={pct_b:.2f}, "
            f"mid=${mid:.6f}) — zona netral"
        )

    # -- Width: narrow = breakout setup, wide = trending/extended (netral thd arah) --
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

    # -- donchian_score: mirror threshold di sekitar titik tengah (default 50) --
    ds = st.donchian_score
    score_favorable = ds >= 65 if is_long else ds <= 35
    if ds is not None and score_favorable:
        arah = "bullish" if is_long else "bearish"
        result.add_note(
            f"✅ Donchian score {ds:.0f} — "
            f"posisi dalam channel mendukung entry {arah}"
        )
        result.confidence_adjustment += 0.02


def _check_strength_context(
    iset: IndicatorSet,
    result: ValidationResult,
    side: str = "long",
) -> None:
    """[UPGRADE] Aktifkan obv, obv_trend, mfi_divergence yang sebelumnya idle.

    Strength indicators mengukur KEKUATAN trend, bukan arahnya:
    - ADX/DI: sudah dipakai di _check_oscillator_context
    - obv + obv_trend: konfirmasi volume flow searah price action
    - mfi_divergence:  divergence antara MFI dan RSI = early warning reversal

    [FUTURES-READY] side="long" default IDENTIK PERSIS dgn sebelumnya. Short:
    OBV trend, MFI-RSI divergence, dan DI alignment di-mirror. TIDAK diubah
    (genuinely netral): ADX (ukur KEKUATAN trend, bukan arah -- trend kuat
    bagus utk trend-following di arah manapun), volume ratio/spike (konfirmasi
    momentum berlaku sama utk breakout naik maupun turun).
    """
    st = iset.strength
    if st is None or st.composite_score is None:
        return
    is_long = side != "short"

    # -- OBV trend: apakah volume flow searah price? --
    obv_trend = st.obv_trend
    favorable_obv   = "rising" if is_long else "falling"
    unfavorable_obv = "falling" if is_long else "rising"
    if obv_trend == favorable_obv:
        arah = "uptrend" if is_long else "downtrend"
        tekanan = "akumulasi" if is_long else "distribusi"
        tekanan_opp = "distribusi" if is_long else "akumulasi"
        result.add_note(
            f"✅ OBV {obv_trend} — volume flow mengonfirmasi {arah}: "
            f"tekanan {tekanan} lebih besar dari {tekanan_opp}"
        )
        result.confidence_adjustment += 0.04
    elif obv_trend == unfavorable_obv:
        tekanan = "distribusi" if is_long else "akumulasi"
        result.add_warning(
            f"OBV {unfavorable_obv} — volume flow berlawanan dengan price: "
            f"{tekanan} lebih dominan, potensi weakness tersembunyi",
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
        from engine.constants import RSI_DIVERGENCE_THRESHOLD
        confirming_mfi = mfi_div > RSI_DIVERGENCE_THRESHOLD if is_long else mfi_div < -RSI_DIVERGENCE_THRESHOLD
        opposing_mfi   = mfi_div < -RSI_DIVERGENCE_THRESHOLD if is_long else mfi_div > RSI_DIVERGENCE_THRESHOLD
        if confirming_mfi:
            arah = "bullish" if is_long else "bearish"
            tekanan = "beli" if is_long else "jual"
            result.add_note(
                f"✅ MFI-RSI divergence {arah} ({mfi_div:+.1f}) — "
                f"money flow (MFI) bergerak lebih cepat dari RSI: "
                f"tekanan {tekanan} berbasis volume lebih kuat dari momentum harga"
            )
            result.confidence_adjustment += 0.05
        elif opposing_mfi:
            arah = "bearish" if is_long else "bullish"
            tekanan = "selling" if is_long else "buying"
            result.add_warning(
                f"MFI-RSI divergence {arah} ({mfi_div:+.1f}) — "
                f"money flow bergerak berlawanan lebih cepat dari RSI: "
                f"volume {tekanan} pressure lebih besar dari yang terlihat di harga",
                confidence_penalty=0.06,
            )

    # -- ADX kekuatan trend: ADX < 20 = sideways = sinyal trend lebih berisiko --
    if st.adx is not None:
        from engine.constants import ADX_WEAK_TREND, ADX_STRONG_TREND
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

    # -- DI alignment: mirror utk short (-DI dominan jadi confirming) --
    if st.plus_di is not None and st.minus_di is not None:
        confirming_di = st.plus_di > st.minus_di * 1.5 if is_long else st.minus_di > st.plus_di * 1.5
        opposing_di   = st.minus_di > st.plus_di * 1.5 if is_long else st.plus_di > st.minus_di * 1.5
        if confirming_di:
            dominant, other = (st.plus_di, st.minus_di) if is_long else (st.minus_di, st.plus_di)
            dominant_label, other_label = ("DI+", "DI-") if is_long else ("DI-", "DI+")
            arah = "bullish" if is_long else "bearish"
            result.add_note(
                f"✅ {dominant_label} ({dominant:.1f}) jauh di atas {other_label} ({other:.1f}) — "
                f"directional pressure {arah} dominan"
            )
            result.confidence_adjustment += 0.03
        elif opposing_di:
            dominant, other = (st.minus_di, st.plus_di) if is_long else (st.plus_di, st.minus_di)
            dominant_label, other_label = ("DI-", "DI+") if is_long else ("DI+", "DI-")
            arah = "bearish" if is_long else "bullish"
            result.add_warning(
                f"{dominant_label} ({dominant:.1f}) jauh di atas {other_label} ({other:.1f}) — "
                f"directional pressure {arah} dominan",
                confidence_penalty=0.05,
            )

    # -- Volume ratio + spike --
    # [BUG-FIX v2.1] volume_climax SENGAJA tidak dicek di sini lagi — sebelumnya
    # ada double-penalty: _check_volume_climax() (dipanggil terpisah di
    # validate_signal, baris ~1659) SUDAH menghitung penalty -0.10 untuk
    # st.volume_climax. Cabang ini dulu menambahkan -0.05 lagi untuk field yang
    # SAMA, jadi total penalty diam-diam jadi -0.15 setiap kali volume climax
    # terdeteksi. _check_volume_climax() tetap satu-satunya pemilik logic ini
    # (juga sudah cover secondary_pattern == VOLUME_CLIMAX dari patterns.py).
    if st.volume_ratio is not None:
        from engine.constants import VOLUME_RATIO_ELEVATED, VOLUME_RATIO_SPIKE
        if st.volume_climax:
            pass  # ditangani oleh _check_volume_climax(), jangan duplikasi penalty
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
    side: str = "long",
    # [FUTURES-READY] side="long" default -- semua pemanggil existing yang
    # tidak eksplisit mengirim side akan tetap dapat "long", behavior
    # IDENTIK PERSIS dengan sebelum parameter ini ditambahkan.
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
        from engine.profiles.thresholds import get_profile_thresholds
        profile_cfg = get_profile_thresholds(signal.strategy_profile)
        _check_atr_threshold(iset, profile_cfg, result)
        consecutive_max = getattr(profile_cfg, "max_consecutive_losses", max_consecutive_losses)
    except Exception:
        profile_cfg = None
        consecutive_max = max_consecutive_losses

    _check_rsi_divergence(iset, result, side=side)

    _check_macd_divergence(iset, result, side=side)

    _check_support_resistance_context(iset, result, side=side)

    _check_pattern_type_context(iset, result, side=side)

    _check_higher_tf_alignment(observation, result, side=side)

    _check_volume_climax(iset, result)

    _check_squeeze_context(iset, result)

    # [UPGRADE] Checks baru yang mengaktifkan field sebelumnya idle
    _check_bb_context(iset, result, side=side)
    _check_kc_context(iset, result, side=side)
    _check_macd_context(iset, result, side=side)
    _check_stoch_context(iset, result, side=side)
    _check_strength_context(iset, result, side=side)

    _check_oscillator_context(iset, result, side=side)

    _check_structure_context(iset, result, side=side)

    # [UPGRADE] Checks trend & structure yang mengaktifkan field sebelumnya idle
    _check_trend_cross_context(iset, result, side=side)
    _check_vwap_band_context(iset, result, side=side)
    _check_ichimoku_detail_context(iset, result, side=side)
    _check_pivot_ladder_context(iset, result, side=side)
    _check_market_structure_context(iset, result, side=side)
    _check_fib_detail_context(iset, result, side=side)
    _check_donchian_context(iset, result, side=side)

    _check_orderbook_context(iset, result, side=side)

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
