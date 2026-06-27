"""
indicators/patterns.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2 (audit kualitas internal + field utilization):
  [BUG-FIX] _is_volume_confirmed(): sebelumnya cek df[vol_col].iloc[-1] —
           candle LIVE/belum closed. Semua fungsi detect_* di file ini
           (engulfing, hammer, doji, marubozu) pakai iloc[-2] sebagai candle
           pattern (konsisten dgn main.py: confirmed_ts = bars[-2][0]).
           Dibuktikan dgn skenario: candle -2 volume 2.8x MA (harusnya
           confirmed) tapi candle -1 baru 0.14x MA (live, wajar kecil) —
           hasil lama False, seharusnya True. Diganti ke iloc[-2].
  [CLEANUP] Hapus parameter require_volume_confirmation dari
           _detect_engulfing_raw()/detect_engulfing() — diterima tapi tidak
           pernah dipakai di body manapun, tidak ada caller yang pass False.
  [UPGRADE] intelligence/validator.py: primary_pattern (field paling
           informatif, sebelumnya 100% idle di luar file ini) dan
           distance_to_support (asimetris dgn distance_to_resistance yg
           sudah aktif) sekarang diaktifkan lewat _check_pattern_type_context()
           dan _check_support_resistance_context().
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from constants import (
    ENGULFING_MIN_BODY_RATIO,
    ENGULFING_COVERAGE_RATIO,
    HAMMER_LOWER_SHADOW_MULT,
    HAMMER_UPPER_SHADOW_MAX_MULT,
    HAMMER_MIN_BODY_PCT,
    DOJI_MAX_BODY_PCT,
    DOJI_BALANCED_SHADOW_RATIO,
    MARUBOZU_MIN_BODY_PCT,
    PATTERN_NEAR_SUPPORT_PCT,
    PATTERN_NEAR_RESISTANCE_PCT,
    PATTERN_VOLUME_CONFIRM_RATIO,
    PATTERN_BASE_SCORE_ENGULFING,
    PATTERN_BASE_SCORE_HAMMER,
    PATTERN_BASE_SCORE_DOJI,
    PATTERN_BASE_SCORE_MARUBOZU,
    PATTERN_VOLUME_CONFIRM_BONUS,
    PATTERN_CONTEXT_SUPPORT_BONUS,
    PATTERN_HIGHER_TF_ALIGN_BONUS,
    PATTERN_NO_CONFIRM_PENALTY,
    SCORE_NEUTRAL,
)
from core.models import PatternIndicators, PatternType, PatternContext, clamp_score

log = logging.getLogger("indicators.patterns")
_VOLUME_MA_WINDOW = 20
_SWING_LOOKBACK = 20
_DOJI_BODY_THRESHOLD = DOJI_MAX_BODY_PCT

def _candle_components(
    o: float, h: float, l: float, c: float
) -> Tuple[float, float, float, float, bool]:
    total_range  = h - l
    if total_range < 1e-10:
        return 0.0, 0.0, 0.0, 0.0, True

    body_size    = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    is_bullish   = c >= o

    return total_range, body_size, upper_shadow, lower_shadow, is_bullish

def _body_pct(total_range: float, body_size: float) -> float:
    if total_range < 1e-10:
        return 0.0
    return body_size / total_range

def _get_volume_ma(df: pd.DataFrame) -> Optional[float]:
    vol_col = "quote_volume" if "quote_volume" in df.columns else "volume"
    if vol_col not in df.columns:
        return None

    volume = df[vol_col]
    if len(volume) < _VOLUME_MA_WINDOW + 1:
        lookback = volume.iloc[:-1]
    else:
        lookback = volume.iloc[-_VOLUME_MA_WINDOW-1:-1]

    if lookback.empty or lookback.sum() < 1e-9:
        return None

    return float(lookback.mean())

def _is_volume_confirmed(df: pd.DataFrame) -> bool:
    """
    [BUG-FIX v2] Sebelumnya cek df[vol_col].iloc[-1] — itu candle LIVE/belum
    closed (main.py pakai konvensi confirmed_ts = bars[-2][0], dan SEMUA
    fungsi detect_* di file ini — engulfing, hammer, doji, marubozu — pakai
    iloc[-2] sebagai candle pattern). Cek volume harus di candle YANG SAMA
    dengan candle tempat pattern terdeteksi (-2), bukan candle selanjutnya
    yang masih berjalan. Sebelumnya pattern dgn volume tinggi di candle -2
    bisa salah dapat PATTERN_NO_CONFIRM_PENALTY karena candle -1 (baru
    sebagian waktu berlalu) volumenya wajar kecil.
    """
    vol_col = "quote_volume" if "quote_volume" in df.columns else "volume"
    if vol_col not in df.columns:
        return False

    current_vol = float(df[vol_col].iloc[-2])
    vol_ma      = _get_volume_ma(df)

    if vol_ma is None or vol_ma < 1e-9:
        return False

    return current_vol >= vol_ma * PATTERN_VOLUME_CONFIRM_RATIO

def get_pattern_context(
    df: pd.DataFrame,
    bb_position: Optional[float] = None,
    bb_lower: Optional[float] = None,
    bb_upper: Optional[float] = None,
    ema_values: Optional[dict] = None,
) -> PatternContext:
    if len(df) < 2:
        return PatternContext.UNKNOWN

    if bb_position is not None:
        try:
            pos = float(bb_position)
            if pos <= 0.10:
                return PatternContext.NEAR_SUPPORT
            if pos >= 0.90:
                return PatternContext.NEAR_RESISTANCE
            return PatternContext.MID_RANGE
        except Exception:
            pass

    close = float(df["close"].iloc[-1])
    if close <= 0:
        return PatternContext.UNKNOWN

    low_arr  = df["low"].values
    high_arr = df["high"].values
    support_levels: List[float] = []

    if bb_lower is not None and bb_lower > 0:
        support_levels.append(bb_lower)

    if ema_values and ema_values.get("ema50"):
        ema50 = float(ema_values["ema50"])
        if ema50 > 0 and ema50 < close:
            support_levels.append(ema50)

    lookback_lows  = low_arr[-_SWING_LOOKBACK-1:-1]
    if len(lookback_lows) > 0:
        swing_low = float(np.min(lookback_lows))
        if swing_low > 0:
            support_levels.append(swing_low)

    resistance_levels: List[float] = []

    if bb_upper is not None and bb_upper > 0:
        resistance_levels.append(bb_upper)

    lookback_highs = high_arr[-_SWING_LOOKBACK-1:-1]
    if len(lookback_highs) > 0:
        swing_high = float(np.max(lookback_highs))
        if swing_high > 0 and swing_high > close:
            resistance_levels.append(swing_high)

    support_threshold    = close * PATTERN_NEAR_SUPPORT_PCT
    resistance_threshold = close * PATTERN_NEAR_RESISTANCE_PCT

    for level in support_levels:
        if abs(close - level) <= support_threshold:
            log.debug("pattern context: NEAR_SUPPORT (close=%.5f level=%.5f)", close, level)
            return PatternContext.NEAR_SUPPORT

    for level in resistance_levels:
        if abs(close - level) <= resistance_threshold:
            log.debug("pattern context: NEAR_RESISTANCE (close=%.5f level=%.5f)", close, level)
            return PatternContext.NEAR_RESISTANCE

    return PatternContext.MID_RANGE

def _detect_engulfing_raw(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> Tuple[PatternType, float]:
    # [CLEANUP v2] Parameter require_volume_confirmation dihapus — sebelumnya
    # diterima tapi tidak pernah dipakai di body fungsi ini (volume confirmation
    # yang sesungguhnya sudah ditangani terpisah lewat _is_volume_confirmed()
    # yang dipanggil score_pattern(), bukan di sini), dan tidak ada caller yang
    # pernah pass False — parameter ini 100% kosmetik/dead.
    if errors is None:
        errors = []

    if len(df) < 3:
        errors.append("engulfing: butuh minimal 3 bar (2 konfirmasi + 1 buffer)")
        return PatternType.NONE, 0.0

    bar_curr = df.iloc[-2]
    bar_prev = df.iloc[-3]

    o_curr = float(bar_curr["open"])
    h_curr = float(bar_curr["high"])
    l_curr = float(bar_curr["low"])
    c_curr = float(bar_curr["close"])

    o_prev = float(bar_prev["open"])
    h_prev = float(bar_prev["high"])
    l_prev = float(bar_prev["low"])
    c_prev = float(bar_prev["close"])

    range_curr, body_curr, upper_curr, lower_curr, is_bull_curr = _candle_components(
        o_curr, h_curr, l_curr, c_curr
    )
    range_prev, body_prev, _, _, is_bull_prev = _candle_components(
        o_prev, h_prev, l_prev, c_prev
    )

    if range_curr < 1e-10 or range_prev < 1e-10:
        return PatternType.NONE, 0.0

    body_pct_curr = _body_pct(range_curr, body_curr)

    if body_pct_curr < ENGULFING_MIN_BODY_RATIO:
        return PatternType.NONE, 0.0

    if not is_bull_prev and is_bull_curr:
        if (o_curr <= c_prev and
                c_curr >= o_prev and
                body_curr >= body_prev * ENGULFING_COVERAGE_RATIO):

            quality = min(1.0, body_curr / (range_prev + 1e-10))

            log.debug(
                "BULLISH_ENGULFING detected: quality=%.3f vol_ok=%s",
                quality, _is_volume_confirmed(df),
            )
            return PatternType.BULLISH_ENGULFING, quality

    if is_bull_prev and not is_bull_curr:
        if (o_curr >= c_prev and
                c_curr <= o_prev and
                body_curr >= body_prev * ENGULFING_COVERAGE_RATIO):

            quality = min(1.0, body_curr / (range_prev + 1e-10))

            log.debug(
                "BEARISH_ENGULFING detected: quality=%.3f",
                quality,
            )
            return PatternType.BEARISH_ENGULFING, quality

    return PatternType.NONE, 0.0

def detect_engulfing(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> PatternIndicators:
    p, q = _detect_engulfing_raw(
        df,
        errors=errors,
    )
    out = PatternIndicators()
    out.primary_pattern = p
    out.pattern_score = _score_single_pattern(
        pattern_type=p,
        quality=q,
        context=PatternContext.UNKNOWN,
        volume_confirmed=_is_volume_confirmed(df),
        higher_tf_aligned=None,
    )
    out.composite_score = out.pattern_score
    return out

def _detect_hammer_shooting_star_raw(
    df: pd.DataFrame,
    context: PatternContext = PatternContext.UNKNOWN,
    errors: Optional[List[str]] = None,
) -> Tuple[PatternType, float]:
    if errors is None:
        errors = []

    if len(df) < 2:
        errors.append("hammer: butuh minimal 2 bar")
        return PatternType.NONE, 0.0

    bar = df.iloc[-2]
    o   = float(bar["open"])
    h   = float(bar["high"])
    l   = float(bar["low"])
    c   = float(bar["close"])

    total_range, body_size, upper_shadow, lower_shadow, is_bullish = \
        _candle_components(o, h, l, c)

    if total_range < 1e-10:
        return PatternType.NONE, 0.0

    body_pct = _body_pct(total_range, body_size)

    if body_pct < HAMMER_MIN_BODY_PCT:
        return PatternType.NONE, 0.0

    if body_pct > 0.50:
        return PatternType.NONE, 0.0

    if body_size < 1e-10:
        return PatternType.NONE, 0.0

    is_hammer = (
        lower_shadow >= HAMMER_LOWER_SHADOW_MULT * body_size and
        upper_shadow <= HAMMER_UPPER_SHADOW_MAX_MULT * body_size
    )

    is_shooting_star = (
        upper_shadow >= HAMMER_LOWER_SHADOW_MULT * body_size and
        lower_shadow <= HAMMER_UPPER_SHADOW_MAX_MULT * body_size
    )

    if not is_hammer and not is_shooting_star:
        return PatternType.NONE, 0.0

    if is_hammer:
        if context not in (PatternContext.NEAR_SUPPORT,):
            log.debug("hammer detected tapi context bukan support (%s) — return NONE", context)
            return PatternType.NONE, 0.0

        quality = min(1.0, lower_shadow / (body_size * 4 + 1e-10))

        log.debug("HAMMER detected: quality=%.3f context=%s", quality, context)
        return PatternType.HAMMER, quality

    if is_shooting_star:
        if context not in (PatternContext.NEAR_RESISTANCE,):
            log.debug(
                "shooting_star detected tapi context bukan resistance (%s) — return NONE",
                context,
            )
            return PatternType.NONE, 0.0

        quality = min(1.0, upper_shadow / (body_size * 4 + 1e-10))
        log.debug("SHOOTING_STAR detected: quality=%.3f context=%s", quality, context)
        return PatternType.SHOOTING_STAR, quality

    return PatternType.NONE, 0.0

def detect_hammer_shooting_star(
    df: pd.DataFrame,
    context: PatternContext = PatternContext.UNKNOWN,
    errors: Optional[List[str]] = None,
) -> PatternIndicators:
    p, q = _detect_hammer_shooting_star_raw(df, context=context, errors=errors)
    out = PatternIndicators()
    out.primary_pattern = p
    out.pattern_context = context
    out.pattern_score = _score_single_pattern(
        pattern_type=p,
        quality=q,
        context=context,
        volume_confirmed=_is_volume_confirmed(df),
        higher_tf_aligned=None,
    )
    out.composite_score = out.pattern_score
    return out

def _detect_doji_raw(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> Tuple[PatternType, float]:
    if errors is None:
        errors = []

    if len(df) < 2:
        errors.append("doji: butuh minimal 2 bar")
        return PatternType.NONE, 0.0

    bar = df.iloc[-2]
    o   = float(bar["open"])
    h   = float(bar["high"])
    l   = float(bar["low"])
    c   = float(bar["close"])

    total_range, body_size, upper_shadow, lower_shadow, _ = _candle_components(
        o, h, l, c
    )

    if total_range < 1e-10:
        return PatternType.NONE, 0.0

    body_pct = _body_pct(total_range, body_size)

    if body_pct > DOJI_MAX_BODY_PCT:
        return PatternType.NONE, 0.0

    body_quality = max(0.0, 1.0 - (body_pct / DOJI_MAX_BODY_PCT))

    if (
        (lower_shadow > 0 and upper_shadow < 1e-10)
        or (
            (upper_shadow > 0 and lower_shadow / upper_shadow >= 3.0)
            and lower_shadow / total_range >= 0.65
        )
    ):
        quality = body_quality * 0.6 + min(1.0, lower_shadow / total_range) * 0.4
        log.debug("DRAGONFLY_DOJI detected: quality=%.3f", quality)
        return PatternType.DRAGONFLY_DOJI, quality

    if (
        (upper_shadow > 0 and lower_shadow < 1e-10)
        or (
            (lower_shadow > 0 and upper_shadow / lower_shadow >= 3.0)
            and upper_shadow / total_range >= 0.65
        )
    ):
        quality = body_quality * 0.6 + min(1.0, upper_shadow / total_range) * 0.4
        log.debug("GRAVESTONE_DOJI detected: quality=%.3f", quality)
        return PatternType.GRAVESTONE_DOJI, quality

    if upper_shadow > 0 and lower_shadow > 0:
        shadow_ratio = max(upper_shadow, lower_shadow) / min(upper_shadow, lower_shadow)
        if shadow_ratio <= DOJI_BALANCED_SHADOW_RATIO:
            log.debug("STANDARD_DOJI detected: quality=%.3f", body_quality)
            return PatternType.STANDARD_DOJI, body_quality

    log.debug("SPINNING_TOP detected (kuasi-doji)")
    return PatternType.SPINNING_TOP, body_quality * 0.6

def detect_doji(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> PatternIndicators:
    p, q = _detect_doji_raw(df, errors=errors)
    out = PatternIndicators()
    out.primary_pattern = p
    out.pattern_score = _score_single_pattern(
        pattern_type=p,
        quality=q,
        context=PatternContext.UNKNOWN,
        volume_confirmed=_is_volume_confirmed(df),
        higher_tf_aligned=None,
    )
    out.composite_score = out.pattern_score
    return out

def detect_all(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
    context: PatternContext = PatternContext.UNKNOWN,
    higher_tf_aligned: Optional[bool] = None,
    bb_lower: Optional[float] = None,
    bb_upper: Optional[float] = None,
    ema_values: Optional[dict] = None,
) -> PatternIndicators:
    return score_pattern(
        df=df,
        context=context,
        higher_tf_aligned=higher_tf_aligned,
        bb_lower=bb_lower,
        bb_upper=bb_upper,
        ema_values=ema_values,
        errors=errors,
    )

def detect_marubozu(
    df: pd.DataFrame,
    errors: Optional[List[str]] = None,
) -> Tuple[PatternType, float]:
    if errors is None:
        errors = []

    if len(df) < 2:
        errors.append("marubozu: butuh minimal 2 bar")
        return PatternType.NONE, 0.0

    bar = df.iloc[-2]
    o   = float(bar["open"])
    h   = float(bar["high"])
    l   = float(bar["low"])
    c   = float(bar["close"])

    total_range, body_size, _, _, is_bullish = _candle_components(o, h, l, c)

    if total_range < 1e-10:
        return PatternType.NONE, 0.0

    body_pct = _body_pct(total_range, body_size)

    if body_pct < MARUBOZU_MIN_BODY_PCT:
        return PatternType.NONE, 0.0

    quality = min(1.0, (body_pct - MARUBOZU_MIN_BODY_PCT) / (1.0 - MARUBOZU_MIN_BODY_PCT) + 0.5)

    if is_bullish:
        log.debug("BULLISH_MARUBOZU detected: quality=%.3f", quality)
        return PatternType.BULLISH_MARUBOZU, quality
    else:
        log.debug("BEARISH_MARUBOZU detected: quality=%.3f", quality)
        return PatternType.BEARISH_MARUBOZU, quality

def detect_volume_climax(
    df: pd.DataFrame,
    climax_ratio: float = 5.0,
    errors: Optional[List[str]] = None,
) -> Tuple[PatternType, float]:
    if errors is None:
        errors = []

    vol_col = "quote_volume" if "quote_volume" in df.columns else "volume"
    if vol_col not in df.columns:
        return PatternType.NONE, 0.0

    if len(df) < _VOLUME_MA_WINDOW + 2:
        return PatternType.NONE, 0.0

    volume  = df[vol_col]
    vol_ma  = _get_volume_ma(df)

    if vol_ma is None or vol_ma < 1e-9:
        return PatternType.NONE, 0.0

    current_vol = float(volume.iloc[-2])
    ratio       = current_vol / vol_ma

    if ratio < climax_ratio:
        return PatternType.NONE, 0.0

    quality = min(1.0, (ratio - climax_ratio) / climax_ratio)
    quality = max(0.3, quality)

    log.debug("VOLUME_CLIMAX detected: ratio=%.1fx quality=%.3f", ratio, quality)
    return PatternType.VOLUME_CLIMAX, quality

def _score_single_pattern(
    pattern_type: PatternType,
    quality: float,
    context: PatternContext,
    volume_confirmed: bool,
    higher_tf_aligned: Optional[bool],
) -> float:
    if pattern_type == PatternType.NONE:
        return SCORE_NEUTRAL

    if pattern_type == PatternType.VOLUME_CLIMAX:
        climax_penalty = 15.0 + quality * 10.0
        return clamp_score(SCORE_NEUTRAL - climax_penalty)

    _BASE_SCORES = {
        PatternType.BULLISH_ENGULFING: PATTERN_BASE_SCORE_ENGULFING,
        PatternType.HAMMER:            PATTERN_BASE_SCORE_HAMMER,
        PatternType.DRAGONFLY_DOJI:    PATTERN_BASE_SCORE_DOJI + 5.0,
        PatternType.MORNING_STAR:      PATTERN_BASE_SCORE_ENGULFING + 5.0,
        PatternType.BULLISH_MARUBOZU:  PATTERN_BASE_SCORE_MARUBOZU,
        PatternType.BEARISH_ENGULFING: -(PATTERN_BASE_SCORE_ENGULFING - 50.0),
        PatternType.SHOOTING_STAR:     -(PATTERN_BASE_SCORE_HAMMER - 50.0),
        PatternType.GRAVESTONE_DOJI:   -(PATTERN_BASE_SCORE_DOJI + 5.0 - 50.0),
        PatternType.EVENING_STAR:      -(PATTERN_BASE_SCORE_ENGULFING + 5.0 - 50.0),
        PatternType.BEARISH_MARUBOZU:  -(PATTERN_BASE_SCORE_MARUBOZU - 50.0),
        PatternType.STANDARD_DOJI:     PATTERN_BASE_SCORE_DOJI,
        PatternType.SPINNING_TOP:      SCORE_NEUTRAL - 3.0,
        PatternType.BB_KC_SQUEEZE:     65.0,
    }

    base = _BASE_SCORES.get(pattern_type, SCORE_NEUTRAL)

    is_bearish_penalty = pattern_type in (
        PatternType.BEARISH_ENGULFING,
        PatternType.SHOOTING_STAR,
        PatternType.GRAVESTONE_DOJI,
        PatternType.EVENING_STAR,
        PatternType.BEARISH_MARUBOZU,
    )

    if is_bearish_penalty:
        raw_penalty = abs(base)
        adjusted    = SCORE_NEUTRAL - raw_penalty * quality
    else:
        excess    = base - SCORE_NEUTRAL
        adjusted  = SCORE_NEUTRAL + excess * quality

    score = adjusted

    if not is_bearish_penalty:
        if volume_confirmed:
            score += PATTERN_VOLUME_CONFIRM_BONUS
        else:
            score += PATTERN_NO_CONFIRM_PENALTY
    if not is_bearish_penalty:
        if context == PatternContext.NEAR_SUPPORT:
            score += PATTERN_CONTEXT_SUPPORT_BONUS
        elif context == PatternContext.NEAR_RESISTANCE:
            score -= 10.0

    if higher_tf_aligned is True and not is_bearish_penalty:
        score += PATTERN_HIGHER_TF_ALIGN_BONUS
    elif higher_tf_aligned is False:
        score -= 8.0
        
    return clamp_score(score)

def score_pattern(
    df: pd.DataFrame,
    context: PatternContext = PatternContext.UNKNOWN,
    higher_tf_aligned: Optional[bool] = None,
    bb_lower: Optional[float] = None,
    bb_upper: Optional[float] = None,
    ema_values: Optional[dict] = None,
    errors: Optional[List[str]] = None,
) -> PatternIndicators:
    if errors is None:
        errors = []

    result = PatternIndicators()

    if context == PatternContext.UNKNOWN and len(df) >= 3:
        context = get_pattern_context(
            df,
            bb_lower=bb_lower,
            bb_upper=bb_upper,
            ema_values=ema_values,
        )

    result.pattern_context    = context
    result.higher_tf_aligned  = higher_tf_aligned

    if len(df) < 3:
        errors.append("score_pattern: butuh minimal 3 bar untuk deteksi pattern")
        result.pattern_score   = SCORE_NEUTRAL
        result.context_score   = SCORE_NEUTRAL
        result.composite_score = SCORE_NEUTRAL
        return result

    vol_confirmed = _is_volume_confirmed(df)
    result.pattern_volume_confirmed = vol_confirmed

    bar = df.iloc[-2]
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    total_range, body_size, _, _, _ = _candle_components(o, h, l, c)
    result.pattern_body_pct = _body_pct(total_range, body_size)

    close = float(df["close"].iloc[-1])
    if bb_lower is not None and bb_lower > 0 and close > 0:
        result.distance_to_support = abs(close - bb_lower) / close * 100.0
    if bb_upper is not None and bb_upper > 0 and close > 0:
        result.distance_to_resistance = abs(close - bb_upper) / close * 100.0

    primary_pattern   = PatternType.NONE
    primary_quality   = 0.0
    secondary_pattern = PatternType.NONE

    climax_pattern, climax_quality = detect_volume_climax(df, errors=errors)
    if climax_pattern == PatternType.VOLUME_CLIMAX:
        secondary_pattern = climax_pattern
        log.debug("Volume climax terdeteksi, lanjut cari candlestick pattern")

    if primary_pattern == PatternType.NONE:
        eng_type, eng_quality = _detect_engulfing_raw(df, errors=errors)
        if eng_type != PatternType.NONE:
            primary_pattern = eng_type
            primary_quality = eng_quality

    if primary_pattern == PatternType.NONE:
        hs_type, hs_quality = _detect_hammer_shooting_star_raw(
            df, context=context, errors=errors
        )
        if hs_type != PatternType.NONE:
            primary_pattern = hs_type
            primary_quality = hs_quality

    if primary_pattern == PatternType.NONE:
        mrz_type, mrz_quality = detect_marubozu(df, errors=errors)
        if mrz_type != PatternType.NONE:
            primary_pattern = mrz_type
            primary_quality = mrz_quality

    if primary_pattern == PatternType.NONE:
        doji_type, doji_quality = _detect_doji_raw(df, errors=errors)
        if doji_type != PatternType.NONE:
            primary_pattern = doji_type
            primary_quality = doji_quality

    if secondary_pattern == PatternType.VOLUME_CLIMAX and primary_pattern == PatternType.NONE:
        primary_pattern = climax_pattern
        primary_quality = climax_quality
        secondary_pattern = PatternType.NONE

    result.primary_pattern   = primary_pattern
    result.secondary_pattern = secondary_pattern

    pattern_score = _score_single_pattern(
        pattern_type      = primary_pattern,
        quality           = primary_quality,
        context           = context,
        volume_confirmed  = vol_confirmed,
        higher_tf_aligned = higher_tf_aligned,
    )

    if secondary_pattern == PatternType.VOLUME_CLIMAX:
        climax_penalty = 12.0 * climax_quality
        pattern_score  = clamp_score(pattern_score - climax_penalty)
        log.debug(
            "Climax penalty diterapkan pada bullish pattern: -%.1f",
            climax_penalty,
        )

    result.pattern_score = pattern_score

    context_score_map = {
        PatternContext.NEAR_SUPPORT:    70.0,
        PatternContext.NEAR_RESISTANCE: 32.0,
        PatternContext.MID_RANGE:       52.0,
        PatternContext.UNKNOWN:         SCORE_NEUTRAL,
    }
    context_score = context_score_map.get(context, SCORE_NEUTRAL)
    result.context_score = context_score

    if primary_pattern == PatternType.NONE:
        result.composite_score = clamp_score(
            SCORE_NEUTRAL * 0.7 + context_score * 0.3
        )
    else:
        result.composite_score = clamp_score(
            pattern_score * 0.70 + context_score * 0.30
        )

    log.debug(
        "pattern composite: primary=%s quality=%.3f vol_ok=%s context=%s "
        "→ pattern_score=%.1f context_score=%.1f composite=%.1f",
        primary_pattern.value, primary_quality, vol_confirmed, context.value,
        pattern_score, context_score, result.composite_score,
    )

    return result
