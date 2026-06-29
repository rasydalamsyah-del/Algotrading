"""
intelligence/observer.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

CHANGELOG v2:
  [PERF] _build_indicator_set(): RSI(14) dihitung sekali, dibagi ke
         score_momentum & score_strength (lihat indicators/strength.py v3).
  [BUG-FIX KRITIS] _compute_tf_score(): patterns/oscillators/structure
         sebelumnya scores.append() TANPA weights.append() pasangan —
         zip(scores, weights) di akhir diam-diam: (a) BUANG TOTAL
         composite_score oscillators & structure dari hasil akhir, (b)
         salah-pasangkan composite_score patterns dgn weight milik
         orderbook (0.10). Dibuktikan dgn skenario sintetis: structure
         100->0 tadinya TIDAK mengubah hasil sama sekali (terbuang),
         sekarang composite ikut bergerak proporsional. Dampak nyata:
         primary_tf_score/confirmation_tf_score dipakai utk hard MTF gate
         di strategy.py (bisa blokir sinyal) dan confidence di scorer.py
         — bukan cuma estimasi kosmetik. Fix: tambah weights.append()
         utk ketiganya (0.10/0.07/0.07, representatif dari rata-rata
         LEVEL1_WEIGHTS lintas profile di profiles/weights.py).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from constants import (
    OBSERVATION_CACHE_TTL_SECONDS,
    OBSERVATION_STALE_THRESHOLD_SECONDS,
    SCORE_NEUTRAL,
    MIN_CANDLES_FOR_INDICATORS,
)
from core.models import (
    IndicatorSet,
    ObservationReport,
    TrendIndicators,
    MomentumIndicators,
    StrengthIndicators,
    VolatilityIndicators,
    PatternIndicators,
    PatternContext,
    OscillatorIndicators,
    StructureIndicators,
    OrderbookIndicators,
)
from indicators.trend import score_trend
from indicators.momentum import score_momentum, _calc_rsi
from indicators.strength import score_strength
from indicators.volatility import score_volatility
from indicators.patterns import score_pattern
from indicators.oscillators import score_oscillators
from indicators.structure import score_structure
from indicators.orderbook import score_orderbook_data

log = logging.getLogger("intelligence.observer")

_OBSERVATION_CACHE: Dict[str, Tuple[ObservationReport, float]] = {}

_CACHE_TTL = OBSERVATION_CACHE_TTL_SECONDS
_STALE_THRESHOLD = OBSERVATION_STALE_THRESHOLD_SECONDS

def _cache_key(symbol: str, timeframe: str, bar_timestamp: Optional[datetime]) -> str:
    ts = bar_timestamp.isoformat() if bar_timestamp else "latest"
    return f"{symbol}|{timeframe}|{ts}"

def _get_cached(key: str) -> Optional[ObservationReport]:
    entry = _OBSERVATION_CACHE.get(key)
    if entry is None:
        return None
    report, cached_at = entry
    age = time.monotonic() - cached_at
    if age > _CACHE_TTL:
        del _OBSERVATION_CACHE[key]
        return None
    return report

def _put_cache(key: str, report: ObservationReport) -> None:
    _OBSERVATION_CACHE[key] = (report, time.monotonic())
    if len(_OBSERVATION_CACHE) > 200:
        oldest_key = min(_OBSERVATION_CACHE, key=lambda k: _OBSERVATION_CACHE[k][1])
        del _OBSERVATION_CACHE[oldest_key]

def clear_cache(symbol: Optional[str] = None) -> int:
    if symbol is None:
        count = len(_OBSERVATION_CACHE)
        _OBSERVATION_CACHE.clear()
        return count

    keys_to_delete = [k for k in _OBSERVATION_CACHE if k.startswith(f"{symbol}|")]
    for k in keys_to_delete:
        del _OBSERVATION_CACHE[k]
    return len(keys_to_delete)

def _build_indicator_set(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    is_primary: bool = True,
    bb_context_kwargs: Optional[Dict] = None,
    higher_tf_aligned: Optional[bool] = None,
    ob_data: Optional[dict] = None,
) -> IndicatorSet:
    n_bars = len(df)

    iset = IndicatorSet(
        symbol=symbol,
        timeframe=timeframe,
        is_primary_tf=is_primary,
        bars_available=n_bars,
    )

    if n_bars > 0:
        last = df.iloc[-1]
        iset.current_price = float(last.get("close", 0.0))
        iset.open_price    = float(last.get("open",  0.0))
        iset.high_price    = float(last.get("high",  0.0))
        iset.low_price     = float(last.get("low",   0.0))
        iset.close_price   = float(last.get("close", 0.0))
        iset.volume        = float(last.get("volume", 0.0))
        iset.quote_volume  = float(last.get("quote_volume", 0.0))

    if n_bars < MIN_CANDLES_FOR_INDICATORS:
        iset.add_error("observer", f"Hanya {n_bars} bar, butuh minimal {MIN_CANDLES_FOR_INDICATORS}")
        log.warning(
            "%s/%s: Data tidak cukup (%d bar) untuk kalkulasi indikator.",
            symbol, timeframe, n_bars,
        )
        return iset

    errors: List[str] = []

    try:
        iset.trend = score_trend(df, errors=errors, timeframe=timeframe)
    except Exception as exc:
        iset.add_error("trend", str(exc))
        log.exception("%s/%s: Error kalkulasi trend: %s", symbol, timeframe, exc)

    # [PERF v2.2] RSI(14) dihitung SEKALI di sini lalu dibagikan ke score_momentum
    # dan score_strength — sebelumnya keduanya menghitung _calc_rsi() independen
    # dengan period sama (14) dari close yang sama, ~19% waktu score_strength()
    # terbuang untuk kerja dobel. Period 14 di sini cocok dengan default
    # calculate_rsi_enhanced() (momentum.py) dan _MFI_PERIOD (strength.py).
    # Kalau gagal dihitung (data kurang/exception), fallback None — masing-masing
    # fungsi tetap menghitung sendiri seperti semula (backward-compatible).
    shared_rsi_series: Optional[pd.Series] = None
    try:
        if "close" in df.columns and len(df) >= 16:
            shared_rsi_series = _calc_rsi(df["close"], 14)
    except Exception as exc:
        log.debug("%s/%s: Gagal precompute shared RSI: %s", symbol, timeframe, exc)
        shared_rsi_series = None

    try:
        iset.momentum = score_momentum(df, errors=errors, rsi_series=shared_rsi_series)
    except Exception as exc:
        iset.add_error("momentum", str(exc))
        log.exception("%s/%s: Error kalkulasi momentum: %s", symbol, timeframe, exc)

    try:
        iset.strength = score_strength(df, errors=errors, rsi_series=shared_rsi_series)
    except Exception as exc:
        iset.add_error("strength", str(exc))
        log.exception("%s/%s: Error kalkulasi strength: %s", symbol, timeframe, exc)

    try:
        iset.volatility = score_volatility(df, errors=errors)
    except Exception as exc:
        iset.add_error("volatility", str(exc))
        log.exception("%s/%s: Error kalkulasi volatility: %s", symbol, timeframe, exc)

    try:
        bb_lower = iset.volatility.bb_lower if iset.volatility else None
        bb_upper = iset.volatility.bb_upper if iset.volatility else None
        ema_vals = {
            "ema9":  iset.trend.ema9  if iset.trend else None,
            "ema21": iset.trend.ema21 if iset.trend else None,
            "ema50": iset.trend.ema50 if iset.trend else None,
        }

        if bb_context_kwargs:
            bb_lower = bb_context_kwargs.get("bb_lower", bb_lower)
            bb_upper = bb_context_kwargs.get("bb_upper", bb_upper)
            ema_vals.update(bb_context_kwargs.get("ema_values", {}))

        iset.patterns = score_pattern(
            df,
            bb_lower=bb_lower,
            bb_upper=bb_upper,
            ema_values=ema_vals,
            higher_tf_aligned=higher_tf_aligned,
            errors=errors,
        )
    except Exception as exc:
        iset.add_error("patterns", str(exc))
        log.exception("%s/%s: Error kalkulasi patterns: %s", symbol, timeframe, exc)

    try:
        iset.oscillators = score_oscillators(df, errors=errors)
    except Exception as exc:
        iset.add_error("oscillators", str(exc))
        log.exception("%s/%s: Error kalkulasi oscillators: %s", symbol, timeframe, exc)

    try:
        iset.structure = score_structure(df, errors=errors)
    except Exception as exc:
        iset.add_error("structure", str(exc))
        log.exception("%s/%s: Error kalkulasi structure: %s", symbol, timeframe, exc)

    try:
        if ob_data:
            iset.orderbook = score_orderbook_data(ob_data, errors=errors, symbol=symbol)
    except Exception as exc:
        iset.add_error("orderbook", str(exc))
        log.exception("%s/%s: Error kalkulasi orderbook: %s", symbol, timeframe, exc)

    for err in errors:
        iset.add_error("calc", err)

    log.debug(
        "%s/%s: IndicatorSet built | bars=%d | "
        "trend=%.1f mom=%.1f str=%.1f vol=%.1f pat=%.1f | errors=%d",
        symbol, timeframe, n_bars,
        iset.trend.composite_score,
        iset.momentum.composite_score,
        iset.strength.composite_score,
        iset.volatility.composite_score,
        iset.patterns.composite_score,
        len(iset.calculation_errors),
    )

    return iset

def _compute_tf_score(iset: IndicatorSet) -> float:
    if iset.bars_available < MIN_CANDLES_FOR_INDICATORS:
        return SCORE_NEUTRAL

    scores = []
    weights = []

    if iset.trend.is_valid():
        scores.append(iset.trend.composite_score)
        weights.append(0.30)

    if iset.momentum.is_valid():
        scores.append(iset.momentum.composite_score)
        weights.append(0.25)

    if iset.strength.is_valid():
        scores.append(iset.strength.composite_score)
        weights.append(0.25)

    if iset.volatility.is_valid():
        scores.append(iset.volatility.composite_score)
        weights.append(0.10)

    # [BUG-FIX v2] Sebelumnya 3 baris di bawah ini TIDAK punya weights.append()
    # pasangan, sementara di akhir fungsi scores & weights di-zip(). zip()
    # berhenti diam-diam di larik TERPENDEK tanpa error — akibatnya:
    #  - composite_score oscillators & structure 100% DIBUANG dari hasil akhir
    #    (terbukti dgn skenario nyata: composite jadi 55.0 padahal seharusnya
    #    jauh berbeda kalau structure=100/oscillators=0 betul2 ikut terhitung)
    #  - composite_score patterns malah ke-pasang dgn weight milik orderbook
    #    (0.10), bukan weight pattern sendiri.
    # Dampak nyata: report.primary_tf_score & confirmation_tf_score dipakai
    # utk hard MTF gate di strategy.py (bisa blokir sinyal kalau conf_score
    # < threshold) dan komponen confidence di scorer.py — jadi bukan cuma
    # estimasi kosmetik. Bobot di bawah ini representatif dari rata-rata
    # LEVEL1_WEIGHTS lintas profile di profiles/weights.py (pattern~0.07-0.10,
    # oscillator~0.07, structure~0.06-0.08); fungsi ini tetap normalisasi via
    # total_weight di akhir jadi tidak perlu pas sama dgn LEVEL1_WEIGHTS persis.
    if iset.patterns.is_valid():
        scores.append(iset.patterns.composite_score)
        weights.append(0.10)

    if iset.oscillators.is_valid():
        scores.append(iset.oscillators.composite_score)
        weights.append(0.07)

    if iset.structure.is_valid():
        scores.append(iset.structure.composite_score)
        weights.append(0.07)

    if iset.orderbook.is_valid():
        scores.append(iset.orderbook.composite_score)
        weights.append(0.10)

    if not scores:
        return SCORE_NEUTRAL

    total_weight = sum(weights)
    if total_weight < 1e-6:
        return SCORE_NEUTRAL

    composite = sum(s * w for s, w in zip(scores, weights)) / total_weight
    return round(max(0.0, min(100.0, composite)), 2)

def observe(
    symbol: str,
    strategy_profile: str,
    primary_df: pd.DataFrame,
    primary_timeframe: str,
    confirmation_df: Optional[pd.DataFrame] = None,
    confirmation_timeframe: Optional[str] = None,
    confirmation_weight: float = 0.25,
    use_cache: bool = True,
    ob_data: Optional[dict] = None,
) -> ObservationReport:
    bar_ts: Optional[datetime] = None
    if isinstance(primary_df.index, pd.DatetimeIndex) and len(primary_df) > 0:
        bar_ts = primary_df.index[-1].to_pydatetime()

    cache_key = _cache_key(symbol, primary_timeframe, bar_ts)

    if use_cache:
        cached = _get_cached(cache_key)
        if cached is not None:
            log.debug("%s/%s: ObservationReport dari cache.", symbol, primary_timeframe)
            return cached

    report = ObservationReport(
        symbol=symbol,
        strategy_profile=strategy_profile,
    )

    primary_iset = _build_indicator_set(
        df=primary_df,
        symbol=symbol,
        timeframe=primary_timeframe,
        is_primary=True,
        ob_data=ob_data,
    )
    report.primary_tf_indicators = primary_iset
    report.primary_tf_score = _compute_tf_score(primary_iset)
    report.primary_tf_valid = (
        primary_iset.is_fully_valid()
        and not primary_iset.has_critical_errors()
    )

    conf_score = SCORE_NEUTRAL
    report.confirmation_tf_valid = False

    if confirmation_df is not None and confirmation_timeframe is not None:
        try:
            primary_is_bullish = report.primary_tf_score > SCORE_NEUTRAL

            conf_iset = _build_indicator_set(
                df=confirmation_df,
                symbol=symbol,
                timeframe=confirmation_timeframe,
                is_primary=False,
                higher_tf_aligned=primary_is_bullish,
            )
            report.confirmation_tf_indicators = conf_iset
            conf_score = _compute_tf_score(conf_iset)
            report.confirmation_tf_score = conf_score
            report.confirmation_tf_valid = (
                conf_iset.is_fully_valid()
                and not conf_iset.has_critical_errors()
            )

        except Exception as exc:
            log.exception(
                "%s: Error saat build confirmation TF (%s): %s",
                symbol, confirmation_timeframe, exc,
            )
            report.confirmation_tf_score = SCORE_NEUTRAL

    else:
        report.confirmation_tf_score = SCORE_NEUTRAL

    if report.confirmation_tf_indicators is not None and confirmation_weight > 0:
        primary_weight = 1.0 - confirmation_weight
        report.composite_raw_score = round(
            report.primary_tf_score * primary_weight
            + report.confirmation_tf_score * confirmation_weight,
            2,
        )
    else:
        report.composite_raw_score = report.primary_tf_score

    report.used_cached = False

    log.info(
        "%s | primary_score=%.1f (valid=%s) conf_score=%.1f (valid=%s) "
        "composite=%.1f | profile=%s",
        symbol,
        report.primary_tf_score,
        report.primary_tf_valid,
        report.confirmation_tf_score,
        report.confirmation_tf_valid,
        report.composite_raw_score,
        strategy_profile,
    )

    if use_cache:
        _put_cache(cache_key, report)

    return report

def is_report_stale(report: ObservationReport) -> bool:
    age = (datetime.utcnow() - report.observed_at).total_seconds()
    return age > _STALE_THRESHOLD

def get_higher_tf_alignment(
    confirmation_report: ObservationReport,
    threshold: float = 55.0,
) -> Optional[bool]:
    if not confirmation_report.confirmation_tf_valid:
        return None

    conf_score = confirmation_report.confirmation_tf_score
    if conf_score >= threshold:
        return True
    if conf_score <= (100.0 - threshold):
        return False
    return None

def summarize_observation(report: ObservationReport) -> str:
    lines = [
        f"📊 Observation: {report.symbol} | Profile: {report.strategy_profile}",
        f"  Primary TF  : score={report.primary_tf_score:.1f} valid={report.primary_tf_valid}",
        f"  Confirm TF  : score={report.confirmation_tf_score:.1f} valid={report.confirmation_tf_valid}",
        f"  Composite   : {report.composite_raw_score:.1f}",
    ]

    pi = report.primary_tf_indicators
    if pi:
        lines.append(
            f"  Indikator   : trend={pi.trend.composite_score:.1f} "
            f"mom={pi.momentum.composite_score:.1f} "
            f"str={pi.strength.composite_score:.1f} "
            f"vol={pi.volatility.composite_score:.1f} "
            f"pat={pi.patterns.composite_score:.1f}"
        )
        if pi.calculation_errors:
            lines.append(f"  ⚠️ Errors ({len(pi.calculation_errors)}): {pi.calculation_errors[:3]}")

    return "\n".join(lines)


class MarketObserver:
    async def get_cached_observation(
        self,
        symbol: str,
        timeframe: str,
    ) -> Optional[ObservationReport]:
        prefix = f"{symbol}|{timeframe}|"
        best_report = None
        best_time   = -1.0
        for key, (report, cached_at) in list(_OBSERVATION_CACHE.items()):
            if key.startswith(prefix) and cached_at > best_time:
                best_report = report
                best_time   = cached_at
        return best_report

    def observe(
        self,
        symbol: str,
        primary_df: pd.DataFrame,
        profile,
        confirmation_df: Optional[pd.DataFrame] = None,
        confirmation_timeframe: Optional[str] = None,
        ob_data: Optional[dict] = None,
    ) -> ObservationReport:
        return observe(
            symbol=symbol,
            strategy_profile=profile.profile.value,
            primary_df=primary_df,
            primary_timeframe=profile.timeframe,
            confirmation_df=confirmation_df,
            confirmation_timeframe=confirmation_timeframe,
            confirmation_weight=float(getattr(profile, "confirmation_weight", 0.25)),
            ob_data=ob_data,
        )
