"""
intelligence/scorer.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from engine.constants import (
    SCORE_NEUTRAL,
    SCORE_MIN,
    SCORE_MAX,
    SIGNAL_CONFIRMATION_MATRIX,
)
from engine.core.models import (
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
from engine.profiles.weights import (
    get_level1_weights,
    get_level2_weights,
    get_regime_modifier,
    compute_category_score,
)
from engine.profiles.thresholds import get_profile_thresholds, get_dynamic_threshold

log = logging.getLogger("intelligence.scorer")

# Buffer konfirmasi sinyal BUY per (symbol, side).
# Format: {(symbol, side): {"count": int, "regime": str}}
#
# [#25 -- audit fungsional, pola bug sama persis dgn _OBSERVATION_CACHE
# sebelum Sub-Batch D (observer.py)] Sebelumnya key HANYA `symbol` --
# main_future.py bisa mengevaluasi kedua sisi (long & short) utk simbol
# yang sama dalam satu siklus (score_signal(..., side=)), jadi begitu skor
# genuinely beda per side (efek proyek MTF Composite Side-Aware yang
# sudah selesai), permintaan side="short" bisa diam-diam dapat/menimpa
# entry buffer confirm-count milik side="long" utk simbol yang sama --
# silent cross-side contamination, belum termanifestasi krn belum ada
# short yang lolos threshold utk memicu buffer ini saat gap ini ditemukan.
#
# [BEDA dari _OBSERVATION_CACHE, TIDAK straight-copy] _OBSERVATION_CACHE
# pakai string key dgn `side` sbg SUFFIX krn ADA consumer lain yang match
# key via `key.startswith(f"{symbol}|{timeframe}|")` (get_cached_observation()/
# clear_cache()) -- suffix dipilih supaya prefix-matching itu tetap jalan
# tanpa diubah. _SIGNAL_CONFIRM_BUFFER TIDAK punya consumer serupa (dicek
# lewat grep repo-wide -- symbol/scorer.py adalah satu-satunya pembaca/
# penulis, tidak ada clear_cache()-like function, tidak ada prefix-matching
# manapun) -- jadi tuple key (symbol, side) dipakai di sini, LEBIH
# SEDERHANA & lebih aman dari isu delimiter (tidak perlu asumsi symbol
# tidak pernah mengandung karakter pemisah).
_SIGNAL_CONFIRM_BUFFER: dict = {}

def _check_primary_trigger(
    profile_name: str,
    iset: IndicatorSet,
    profile_cfg,
    side: str = "long",
) -> Tuple[bool, str]:
    from engine.profiles.base_profile import PrimaryTriggerType

    trigger_type = profile_cfg.primary_trigger_type
    is_long = side != "short"

    if trigger_type == PrimaryTriggerType.BREAKOUT_VOLUME:
        # [FUTURES-READY] Netral arah -- cuma cek band RSI + volume, tidak
        # mensyaratkan arah tertentu. Tidak ada perubahan diperlukan.
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
        # [FUTURES-READY] side="long" (default) IDENTIK PERSIS dgn sebelumnya.
        # Short: mirror ema_score (butuh <=45, bearish stack kuat, simetris
        # dari 55 di sekitar titik netral 100). Untuk RSI: profile_cfg BELUM
        # punya field "rsi_gc_max" simetris (cuma ada rsi_gc_min) -- dipakai
        # pendekatan (100 - rsi_gc_min) sbg PLACEHOLDER, bukan nilai final.
        # TODO saat future/ dikerjakan serius: tambah field rsi_gc_max resmi
        # ke profile schema, jangan andalkan aproksimasi ini selamanya.
        ema_score = iset.trend.ema_stack_score
        if is_long:
            if ema_score < 55.0:
                return False, f"EMA stack score {ema_score:.1f} < 55 (trend belum confirm)"
        else:
            if ema_score > 45.0:
                return False, f"EMA stack score {ema_score:.1f} > 45 (bearish trend belum confirm)"
        rsi = iset.momentum.rsi
        if rsi is None:
            return False, "RSI tidak tersedia"
        if is_long:
            if rsi < profile_cfg.rsi_gc_min:
                return False, f"RSI {rsi:.1f} < rsi_gc_min {profile_cfg.rsi_gc_min}"
        else:
            rsi_gc_max_approx = 100.0 - profile_cfg.rsi_gc_min
            if rsi > rsi_gc_max_approx:
                return False, f"RSI {rsi:.1f} > rsi_gc_max~{rsi_gc_max_approx:.1f} (aproksimasi)"
        return True, f"Trend Confirmation trigger terpenuhi ({'long' if is_long else 'short'})"

    elif trigger_type == PrimaryTriggerType.MOMENTUM_REVERSAL:
        # [FUTURES-READY] side="long" (default) IDENTIK PERSIS dgn sebelumnya.
        # Short: mirror penuh -- overbought (rsi > rsi_max) jadi trigger
        # instan (bukan block), macd_hist harus <=0 (bukan >0) utk konfirmasi
        # reversal turun.
        rsi = iset.momentum.rsi
        if rsi is None:
            return False, "RSI tidak tersedia"
        if is_long:
            if rsi > profile_cfg.rsi_max:
                return False, f"RSI {rsi:.1f} terlalu tinggi untuk mean revert entry"
            if rsi < profile_cfg.rsi_min:
                return True, f"RSI {rsi:.1f} oversold — mean revert trigger OK"
            macd_hist = iset.momentum.macd_histogram
            if macd_hist is not None and macd_hist > 0:
                return False, f"MACD histogram masih positif ({macd_hist:.5f}) — belum reversal"
            return True, "Momentum Reversal trigger terpenuhi"
        else:
            if rsi < profile_cfg.rsi_min:
                return False, f"RSI {rsi:.1f} terlalu rendah untuk mean revert short entry"
            if rsi > profile_cfg.rsi_max:
                return True, f"RSI {rsi:.1f} overbought — mean revert short trigger OK"
            macd_hist = iset.momentum.macd_histogram
            if macd_hist is not None and macd_hist < 0:
                return False, f"MACD histogram masih negatif ({macd_hist:.5f}) — belum reversal"
            return True, "Momentum Reversal short trigger terpenuhi"

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

def _pick_side_score(obj: Any, base_field: str, side: str) -> float:
    """
    [BIAS-FIX -- root cause, category score side-awareness] Selektor generik
    dipakai SEMUA 24 sub-score kategori (trend/momentum/strength/volatility/
    pattern/oscillator/structure/orderbook).

    side="short": coba baca f"{base_field}_short" dulu. Kalau field itu
    None/belum ada (kategori belum dapat batch mirror-nya, atau memang
    genuinely direction-agnostic seperti adx/squeeze/atr yang TIDAK PERNAH
    akan punya field _short), fallback ke field long biasa -- inilah yang
    membuat rollout per-batch aman: kategori yang belum diperbaiki tetap
    berperilaku identik dengan sebelum fix ini ada, tidak pernah crash
    karena AttributeError, tidak pernah diam-diam pakai nilai kosong.

    side="long" (default): SELALU baca field dasar langsung, tidak pernah
    menyentuh field _short sama sekali -- long tidak mungkin berubah oleh
    fix ini, di batch manapun.
    """
    if side == "short":
        short_val = getattr(obj, base_field + "_short", None)
        if short_val is not None:
            return short_val
    return getattr(obj, base_field)


def _extract_indicator_scores(iset: IndicatorSet, side: str = "long") -> Dict[str, Dict[str, float]]:
    return {
        "trend": {
            "ema_stack":  _pick_side_score(iset.trend, "ema_stack_score", side),
            "cross":      _pick_side_score(iset.trend, "cross_score", side),
            "supertrend": _pick_side_score(iset.trend, "supertrend_score", side),
            "vwap":       _pick_side_score(iset.trend, "vwap_score", side),
        },
        "momentum": {
            "rsi":      _pick_side_score(iset.momentum, "rsi_score", side),
            "macd":     _pick_side_score(iset.momentum, "macd_score", side),
            "stochrsi": _pick_side_score(iset.momentum, "stoch_score", side),
        },
        "strength": {
            "adx":    _pick_side_score(iset.strength, "adx_score", side),
            "di":     _pick_side_score(iset.strength, "di_score", side),
            "volume": _pick_side_score(iset.strength, "volume_score", side),
            "mfi":    _pick_side_score(iset.strength, "mfi_score", side),
        },
        "volatility": {
            "bb":      _pick_side_score(iset.volatility, "bb_score", side),
            "squeeze": _pick_side_score(iset.volatility, "squeeze_score", side),
            "atr":     _pick_side_score(iset.volatility, "atr_score", side),
        },
        "pattern": {
            "pattern_score": _pick_side_score(iset.patterns, "pattern_score", side),
            "context_score": _pick_side_score(iset.patterns, "context_score", side),
        },
        "oscillator": {
            "cci":            _pick_side_score(iset.oscillators, "cci_score", side),
            "williams":       _pick_side_score(iset.oscillators, "williams_r_score", side),
            "roc":            _pick_side_score(iset.oscillators, "roc_score", side),
            # [v2] field baru -- sinyal mentah (string/float) utk validator.py,
            # BUKAN skor 0-100, tidak relevan utk di-mirror side-aware di sini.
            "cci_trend":      iset.oscillators.cci_trend,
            "willr_trend":    iset.oscillators.willr_trend,
            "roc_crossover":  iset.oscillators.roc_crossover,
            "cci_divergence": iset.oscillators.cci_divergence,
        },
        "structure": {
            "ichimoku":  _pick_side_score(iset.structure, "ichimoku_score", side),
            "sar":       _pick_side_score(iset.structure, "sar_score", side),
            "pivot":     _pick_side_score(iset.structure, "pivot_score", side),
            "fibonacci": _pick_side_score(iset.structure, "fib_score", side),
        },
        "orderbook": {
            "ob_score": _pick_side_score(iset.orderbook, "orderbook_score", side),
        },
    }

def _calc_weighted_breakdown(
    profile_name: str,
    indicator_scores: Dict[str, Dict[str, float]],
    regime: MarketRegime,
    side: str = "long",
) -> ScoreBreakdown:
    l1_weights = get_level1_weights(profile_name)
    # [SHORT-FIX F2] side diteruskan -- modifier trending_bear=0.00 (tabel
    # long) tidak lagi membunuh kandidat short di sweet spot-nya.
    regime_mod = get_regime_modifier(profile_name, regime.value, side=side)

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
    side: str = "long",
    # [BUG-FIX -- audit item #19] Sebelumnya fungsi ini TIDAK PUNYA parameter
    # side sama sekali -- SL selalu di bawah harga, TP selalu di atas,
    # regardless caller-nya sinyal long atau short. side="long" default
    # (SEMUA caller lama yang tidak eksplisit mengirim side, termasuk spot,
    # tetap dapat hasil IDENTIK PERSIS dengan sebelum parameter ini ada).
) -> Tuple[Optional[float], Optional[float]]:
    if current_price <= 0:
        return None, None

    if side == "short":
        if atr is not None and atr > 0:
            sl = current_price + atr * profile_cfg.atr_sl_mult
            tp = current_price - atr * profile_cfg.atr_tp_mult
        else:
            sl = current_price * (1 + profile_cfg.quick_sl_pct / 100)
            tp = current_price * (1 - profile_cfg.quick_tp_pct / 100)

        if sl <= current_price or tp >= current_price:
            return None, None

        return round(sl, 8), round(tp, 8)

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
    main_loop=None,
    side: str = "long",
    # [FUTURES-READY] side="long" default -- SEMUA pemanggil existing yang
    # tidak eksplisit mengirim side akan tetap dapat "long", behavior
    # IDENTIK PERSIS dengan sebelum parameter ini ditambahkan.
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

    # Dynamic threshold berdasarkan kombinasi profile × regime × side
    dynamic_threshold = get_dynamic_threshold(profile_name, regime.value, side=side)
    signal.threshold_used = dynamic_threshold

    if iset is None or not observation.primary_tf_valid:
        signal.signal_type = "hold"
        signal.trigger_met = False
        reason = "Primary TF data tidak valid atau tidak tersedia"
        signal.scoring_narrative = f"❌ {reason}"
        signal.add_validation_note(reason)
        _save_score_to_db(signal, action="SKIP_INVALID_DATA", db_manager=db_manager, main_loop=main_loop, side=side)
        return signal

    # [FUTURES-READY] Hard block regime -- side-aware. Untuk long (default,
    # SATU-SATUNYA kondisi yang pernah berjalan di produksi): TRENDING_BEAR
    # memaksa score=0/hold, IDENTIK PERSIS dengan sebelum perubahan ini.
    # Untuk short: MIRROR -- TRENDING_BULL yang memaksa score=0/hold (karena
    # tren naik kuat berlawanan arah dengan short), TRENDING_BEAR justru
    # boleh lanjut (searah dengan short).
    is_long = side != "short"
    blocking_regime = MarketRegime.TRENDING_BEAR if is_long else MarketRegime.TRENDING_BULL

    if regime == blocking_regime:
        signal.total_score   = 0.0
        signal.signal_type   = "hold"
        signal.trigger_met   = False
        if is_long:
            signal.scoring_narrative = (
                f"❌ TRENDING_BEAR regime — tidak ada BUY signal. "
                f"Semua long position harus dipertimbangkan untuk exit."
            )
        else:
            signal.scoring_narrative = (
                f"❌ TRENDING_BULL regime — tidak ada SHORT signal. "
                f"Semua short position harus dipertimbangkan untuk exit."
            )
        signal.add_validation_note(f"Blocked by {blocking_regime.value} regime")
        _save_score_to_db(signal, action="REJECT_OPPOSING_REGIME", db_manager=db_manager, main_loop=main_loop, side=side)
        return signal

    # [ENTRY-QUALITY 3b -- veto tren makro] Kasus WIF/BERA: long pantulan
    # 15m pada koin -97% dari ATH / di tepi ATL. Data TF KONFIRMASI (4h/1d)
    # SUDAH dihitung observer -- nol fetch tambahan. Aturan: close berjarak
    # >3% di sisi salah EMA200 TF konfirmasi -> veto (long di bawah, short
    # di atas). Ambang 3% menjaga pullback sehat dekat EMA200 tidak ikut
    # terveto. Fail-open: data konfirmasi/EMA200 tidak tersedia -> lewati.
    import os as _os2
    if _os2.getenv("MACRO_TREND_VETO", "true").lower() == "true":
        _conf_iset = getattr(observation, "confirmation_tf_indicators", None)
        _conf_valid = bool(getattr(observation, "confirmation_tf_valid", False))
        _veto_gap = float(_os2.getenv("MACRO_VETO_GAP_PCT", "3.0"))
        if _conf_iset is not None and _conf_valid:
            _t = getattr(_conf_iset, "trend", None)
            _ema200 = getattr(_t, "ema200", None) if _t is not None else None
            _close = getattr(_conf_iset, "current_price", None)
            if not _close and _t is not None:
                _close = getattr(_t, "close", None)
            if _ema200 and _close and _ema200 > 0 and _close > 0:
                _gap_pct = (_close - _ema200) / _ema200 * 100
                _against_long  = side != "short" and _gap_pct < -_veto_gap
                _against_short = side == "short" and _gap_pct > _veto_gap
                if _against_long or _against_short:
                    signal.total_score = 0.0
                    signal.signal_type = "hold"
                    signal.trigger_met = False
                    signal.scoring_narrative = (
                        f"❌ MACRO VETO: TF konfirmasi close={_close:.6f} "
                        f"{'<' if side != 'short' else '>'} EMA200={_ema200:.6f} "
                        f"(gap {_gap_pct:+.1f}%) -- melawan tren besar ({side})."
                    )
                    signal.add_validation_note("Blocked by macro trend veto (EMA200 conf TF)")
                    _save_score_to_db(signal, action="REJECT_MACRO_TREND", db_manager=db_manager, main_loop=main_loop, side=side)
                    return signal

    trigger_met, trigger_reason = _check_primary_trigger(profile_name, iset, profile_cfg, side=side)
    signal.trigger_met = trigger_met

    if not trigger_met:
        signal.total_score   = 0.0
        signal.signal_type   = "hold"
        signal.scoring_narrative = f"❌ No-Trigger: {trigger_reason}"
        signal.add_validation_note(f"Primary trigger gagal: {trigger_reason}")
        _save_score_to_db(signal, action="NO_TRIGGER", db_manager=db_manager, main_loop=main_loop, side=side)
        return signal

    indicator_scores = _extract_indicator_scores(iset, side=side)
    breakdown = _calc_weighted_breakdown(profile_name, indicator_scores, regime, side=side)
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

    # [ENTRY-QUALITY 3a -- score margin] Entry ambang-noise terbukti rugi
    # (PARTI 52.1/52.0 -1.52, ADA 52.5/52): skor +0.1 di atas threshold
    # secara statistik tak terbedakan dari noise tapi diberi ukuran posisi
    # penuh. Trigger kini butuh threshold + margin (env SCORE_ENTRY_MARGIN,
    # default 2.5). threshold_used TETAP nilai asli utk logging konsisten.
    import os as _os
    _entry_margin = float(_os.getenv("SCORE_ENTRY_MARGIN", "2.5"))
    if dynamic_threshold <= total_score < dynamic_threshold + _entry_margin:
        log.info(
            "%s | skor %.1f di zona margin (thr=%.1f +%.1f) -- entry ditahan (ambang-noise).",
            symbol, total_score, dynamic_threshold, _entry_margin,
        )

    if total_score >= dynamic_threshold + _entry_margin:
        # Konfirmasi BUY berdasarkan regime
        regime_key = regime.value
        required   = SIGNAL_CONFIRMATION_MATRIX.get(regime_key, 6)
        buffer_key = (symbol, side)
        buf        = _SIGNAL_CONFIRM_BUFFER.get(buffer_key, {"count": 0, "regime": regime_key})

        # Reset jika regime berubah
        if buf["regime"] != regime_key:
            buf = {"count": 0, "regime": regime_key}

        buf["count"] += 1
        _SIGNAL_CONFIRM_BUFFER[buffer_key] = buf

        if buf["count"] >= required:
            signal.signal_type = "buy"
            log.info(
                "%s | ✅ Konfirmasi BUY terpenuhi: %d/%d (regime=%s, side=%s)",
                symbol, buf["count"], required, regime_key, side,
            )
            # Reset setelah execute
            _SIGNAL_CONFIRM_BUFFER[buffer_key] = {"count": 0, "regime": regime_key}

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
                "%s | ⏳ Menunggu konfirmasi BUY: %d/%d (regime=%s, side=%s)",
                symbol, buf["count"], required, regime_key, side,
            )
    else:
        signal.signal_type = "hold"
        signal.trigger_met = False  # FIX: score < threshold, trigger harus False
        # Reset buffer jika score turun
        buffer_key = (symbol, side)
        if buffer_key in _SIGNAL_CONFIRM_BUFFER:
            _SIGNAL_CONFIRM_BUFFER[buffer_key] = {"count": 0, "regime": regime.value}

    atr = iset.volatility.atr
    price = iset.current_price
    # [BUG-FIX -- ditemukan saat investigasi audit item #19, forecast/diagnosa
    # bidirectional futures] side= sudah tersedia di scope (parameter fungsi
    # ini, baris 341) tapi TIDAK PERNAH diteruskan ke _suggest_sl_tp() --
    # akibatnya suggested_sl/suggested_tp SELALU dihitung pakai formula long
    # (sl di bawah harga, tp di atas) bahkan untuk signal side="short".
    # Field ini advisory-only (dipakai forecast/dashboard, BUKAN SL/TP order
    # sungguhan -- itu dihitung terpisah & sudah side-aware oleh
    # RiskManager.evaluate_order()), jadi TIDAK mempengaruhi eksekusi trade
    # nyata, tapi tetap salah utk ditampilkan. Fix: teruskan side.
    suggested_sl, suggested_tp = _suggest_sl_tp(price, atr, profile_cfg, side=side)
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
    _save_score_to_db(signal, action=action, db_manager=db_manager, main_loop=main_loop, side=side)

    return signal

def _save_score_to_db(signal: ScoredSignal, action: str, db_manager, main_loop=None, side: str = "long") -> None:
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
                side=side,
            )

        # [BUG-FIX] score_signal() dipanggil lewat run_in_executor() dari
        # strategy.py (jalur produksi utama Gate3->Gate4), yaitu dari WORKER
        # THREAD -- di situ asyncio.get_event_loop() SELALU melempar RuntimeError
        # karena worker thread tidak punya loop sendiri, sehingga except
        # RuntimeError di bawah selalu return diam-diam dan _persist() TIDAK
        # PERNAH benar-benar terjadwal untuk jalur ini (dibuktikan lewat
        # eksperimen: RuntimeError nyata muncul saat pola run_in_executor
        # disimulasikan). Fix: kalau caller yang tahu pasti sedang di main
        # thread (strategy.py) mengoper referensi main_loop eksplisit, pakai
        # langsung via run_coroutine_threadsafe -- aman dipanggil dari thread
        # manapun. Kalau main_loop tidak dioper (caller lama spt
        # position_sync.py yang memang jalan di main thread), fallback ke
        # deteksi lama supaya perilaku existing tidak berubah.
        if main_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(_persist(), main_loop)
            except Exception as exc:
                log.debug("Gagal jadwalkan simpan signal score (main_loop): %s", exc)
            return

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_persist(), loop)
            else:
                loop.run_until_complete(_persist())
        except RuntimeError:
            # No running loop di thread ini DAN main_loop tidak dioper caller.
            # Non-fatal, DB save cuma dilewati (bukan lagi jalur produksi utama
            # setelah fix ini, karena strategy.py sekarang selalu mengoper
            # main_loop).
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
        main_loop=None,
        side: str = "long",
    ) -> Optional[ScoredSignal]:
        return score_signal(
            observation=observation,
            regime=regime,
            regime_confidence=regime_confidence,
            profile_override=profile,
            db_manager=self._db,
            main_loop=main_loop,
            side=side,
        )
