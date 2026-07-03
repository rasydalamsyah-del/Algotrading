"""
learning/analytics.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from constants import (
    MIN_SAMPLE_FOR_ATTRIBUTION,
    MIN_SAMPLE_FOR_INDICATOR_STATS,
    ANALYTICS_REFRESH_INTERVAL_S,
    ANALYTICS_TRIGGER_ON_N_TRADES,
    WIN_RATE_ROLLING_LONG,
    WIN_RATE_ROLLING_SHORT,
    INSIGHT_MIN_WIN_RATE_DIFF,
    INSIGHT_MIN_SAMPLE_SIZE,
    INDICATOR_PREDICTIVE_THRESHOLD,
)
from core.models import (
    AttributionReport,
    IndicatorEffectiveness,
    MarketRegime,
    RegimePerformance,
)

log = logging.getLogger("learning.analytics")

# Cross-learning: baca data dari algotrader_test jika diaktifkan
try:
    from learning.cross_learn import get_cross_learn_reader
    _CROSS_LEARN_AVAILABLE = True
except ImportError:
    _CROSS_LEARN_AVAILABLE = False
    log.debug("cross_learn tidak tersedia, skip cross-learning.")

def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    # Avoid MagicMock auto-creating attributes: only trust explicitly-set attrs.
    d = getattr(row, "__dict__", None)
    if isinstance(d, dict) and key in d:
        return d.get(key, default)
    return default

def _normalize_trade_row(row: Any) -> Dict[str, Any]:
    # Accept either dict rows (DB layer) or objects/mocks (tests).
    if isinstance(row, dict):
        base = dict(row)
    else:
        base = {
            "symbol": _row_get(row, "symbol"),
            "timestamp": _row_get(row, "timestamp"),
            "strategy_profile": _row_get(row, "strategy_profile") or _row_get(row, "strategy_name"),
            "regime": _row_get(row, "regime", "undefined"),
            "realized_pnl": _row_get(row, "realized_pnl", 0.0),
            "realized_pnl_pct": _row_get(row, "realized_pnl_pct", None),
            "entry_score": _row_get(row, "entry_score", None) or _row_get(row, "total_score", None),
            "total_score": _row_get(row, "total_score", None),
            "trend_score": _row_get(row, "trend_score", None),
            "momentum_score": _row_get(row, "momentum_score", None),
            "strength_score": _row_get(row, "strength_score", None),
            "volatility_score": _row_get(row, "volatility_score", None),
            "pattern_score": _row_get(row, "pattern_score", None),
        }

    # If realized_pnl_pct isn't provided (common in tests), treat realized_pnl as pct-like.
    if base.get("realized_pnl_pct") is None:
        try:
            base["realized_pnl_pct"] = float(base.get("realized_pnl") or 0.0)
        except Exception:
            base["realized_pnl_pct"] = 0.0

    try:
        base["realized_pnl_pct"] = float(base.get("realized_pnl_pct") or 0.0)
    except Exception:
        base["realized_pnl_pct"] = 0.0

    base["is_win"] = base["realized_pnl_pct"] > 0
    return base

def _mean(values: List[float]) -> float:
    return float(np.mean(values)) if values else 0.0

def _std(values: List[float]) -> float:
    return float(np.std(values, ddof=1)) if len(values) >= 2 else 0.0

def _pearson_r(x: List[float], y: List[float]) -> float:
    if len(x) < 3 or len(y) < 3 or len(x) != len(y):
        return 0.0
    ax = np.array(x, dtype=float)
    ay = np.array(y, dtype=float)
    if np.std(ax) < 1e-9 or np.std(ay) < 1e-9:
        return 0.0
    return float(np.corrcoef(ax, ay)[0, 1])

def _bootstrap_confidence_interval(
    values:     List[float],
    n_boot:     int   = 500,
    confidence: float = 0.95,
) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    arr = np.array(values, dtype=float)
    n   = len(arr)
    rng = np.random.default_rng(42)
    boot_means = [
        float(np.mean(rng.choice(arr, size=n, replace=True)))
        for _ in range(n_boot)
    ]
    alpha = (1.0 - confidence) / 2
    lo    = float(np.percentile(boot_means, alpha * 100))
    hi    = float(np.percentile(boot_means, (1 - alpha) * 100))
    return lo, hi

def _rolling_win_rate(
    outcomes: List[bool],
    window:   int,
) -> Optional[float]:
    recent = outcomes[-window:] if len(outcomes) >= window else outcomes
    if not recent:
        return None
    return sum(1 for x in recent if x) / len(recent) * 100

def _profit_factor(wins: List[float], losses: List[float]) -> float:
    gp = sum(p for p in wins   if p > 0)
    gl = sum(abs(p) for p in losses if p < 0)
    if gl < 1e-9:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)

class AnalyticsEngine:

    def __init__(
        self,
        db_manager,
        min_sample:                  int   = MIN_SAMPLE_FOR_ATTRIBUTION,
        min_sample_indicator:        int   = MIN_SAMPLE_FOR_INDICATOR_STATS,
        insight_min_win_rate_diff:   float = INSIGHT_MIN_WIN_RATE_DIFF,
        insight_min_sample:          int   = INSIGHT_MIN_SAMPLE_SIZE,
        indicator_predictive_thresh: float = INDICATOR_PREDICTIVE_THRESHOLD,
    ):
        self._db          = db_manager
        self._min_sample  = min_sample
        self._min_ind     = min_sample_indicator
        self._wr_diff_min = insight_min_win_rate_diff
        self._ins_sample  = insight_min_sample
        self._ind_thresh  = indicator_predictive_thresh
        self._last_run:   Optional[datetime] = None
        self._trade_count_at_last_run: int   = 0

    async def should_refresh(
        self,
        current_trade_count: int,
        force:               bool = False,
    ) -> bool:
        if force or self._last_run is None:
            return True

        age_secs = (datetime.now(timezone.utc) - self._last_run).total_seconds()
        if age_secs >= ANALYTICS_REFRESH_INTERVAL_S:
            return True

        new_trades = current_trade_count - self._trade_count_at_last_run
        if new_trades >= ANALYTICS_TRIGGER_ON_N_TRADES:
            return True

        return False

    async def compute_attribution(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
        force_refresh: bool          = False,
    ) -> AttributionReport:
        scope = self._build_scope(symbol, profile)

        if not force_refresh:
            cached = await self._db.get_latest_snapshot(scope, lookback_days)
            if cached and cached.get("sufficient_data") and cached.get("summary"):
                log.debug("Attribution cache hit: scope=%s lookback=%d", scope, lookback_days)
                return self._deserialize_report(cached["summary"])

        log.info(
            "Computing attribution: scope=%s lookback=%dd ...",
            scope, lookback_days,
        )

        trades = await self._db.get_trades_with_regime(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        trades = [_normalize_trade_row(t) for t in (trades or [])]
        if symbol:
            trades = [t for t in trades if t.get("symbol") == symbol]
        if profile:
            trades = [t for t in trades if t.get("strategy_profile") == profile]

        report = AttributionReport(
            lookback_days=lookback_days,
            scope=scope,
            total_trades=len(trades),
        )

        if len(trades) < self._min_sample:
            report.sufficient_data = False
            report.warnings.append(
                f"Sample tidak cukup: {len(trades)} trades < minimum {self._min_sample}. "
                f"Tambah lebih banyak trade sebelum attribution bisa dipercaya."
            )
            log.info(
                "Attribution: insufficient data (%d trades) for scope=%s",
                len(trades), scope,
            )
            await self._cache_report(report, scope, lookback_days)
            self._last_run = datetime.now(timezone.utc)
            return report

        report.sufficient_data = True

        report = self._compute_overall_metrics(report, trades)

        report = self._compute_regime_attribution(report, trades)

        score_outcomes = await self._db.get_score_vs_outcome(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        score_outcomes = [_normalize_trade_row(t) for t in (score_outcomes or [])]
        if symbol:
            score_outcomes = [t for t in score_outcomes if t.get("symbol") == symbol]
        if profile:
            score_outcomes = [t for t in score_outcomes if t.get("strategy_profile") == profile]
        if len(score_outcomes) >= self._min_ind:
            report = self._compute_indicator_effectiveness(report, score_outcomes)

        report = self._compute_win_rate_trend(report, trades)

        report = self._generate_insights(report)

        await self._cache_report(report, scope, lookback_days)

        self._last_run                 = datetime.now(timezone.utc)
        self._trade_count_at_last_run  = len(trades)

        log.info(
            "Attribution complete: scope=%s | %d trades | "
            "win_rate=%.1f%% | PF=%.2f | insights=%d",
            scope, len(trades),
            report.overall_win_rate,
            report.overall_profit_factor,
            len(report.insights),
        )
        return report

    async def compute_all_profiles(
        self,
        lookback_days: int   = 30,
        force_refresh: bool  = False,
    ) -> Dict[str, AttributionReport]:
        try:
            all_trades = await self._db.get_trades_with_regime(lookback_days=lookback_days)
        except Exception as e:
            log.error("compute_all_profiles: error loading trades: %s", e)
            return {}

        all_trades = [_normalize_trade_row(t) for t in (all_trades or [])]
        profiles: set = set()
        for t in all_trades:
            p = t.get("strategy_profile")
            if p:
                profiles.add(p)

        results: Dict[str, AttributionReport] = {}

        results["global"] = await self.compute_attribution(
            lookback_days=lookback_days,
            force_refresh=force_refresh,
        )

        for prof in sorted(profiles):
            results[prof] = await self.compute_attribution(
                lookback_days=lookback_days,
                profile=prof,
                force_refresh=force_refresh,
            )

        return results

    def _compute_overall_metrics(
        self,
        report: AttributionReport,
        trades: List[Dict[str, Any]],
    ) -> AttributionReport:
        wins   = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]

        report.overall_win_rate = (
            len(wins) / len(trades) * 100 if trades else 0.0
        )

        win_pnls  = [t["realized_pnl_pct"] for t in wins]
        loss_pnls = [t["realized_pnl_pct"] for t in losses]

        report.overall_profit_factor = _profit_factor(win_pnls, loss_pnls)

        scores = [t.get("entry_score", 50.0) for t in trades if t.get("entry_score") is not None]
        report.overall_avg_score = _mean(scores) if scores else 50.0

        log.debug(
            "Overall: trades=%d wins=%d losses=%d wr=%.1f%% pf=%.2f avg_score=%.1f",
            len(trades), len(wins), len(losses),
            report.overall_win_rate, report.overall_profit_factor, report.overall_avg_score,
        )
        return report

    def _compute_regime_attribution(
        self,
        report: AttributionReport,
        trades: List[Dict[str, Any]],
    ) -> AttributionReport:

        regime_groups: Dict[str, List[Dict]] = {}
        for t in trades:
            regime_val = t.get("regime", "undefined")
            regime_groups.setdefault(regime_val, []).append(t)

        performances: List[RegimePerformance] = []

        for regime_str, group in regime_groups.items():
            try:
                regime_enum = MarketRegime(regime_str)
            except ValueError:
                regime_enum = MarketRegime.UNDEFINED

            wins   = [t for t in group if t["is_win"]]
            losses = [t for t in group if not t["is_win"]]

            perf = RegimePerformance(
                regime=regime_enum,
                total_trades=len(group),
                win_count=len(wins),
                loss_count=len(losses),
            )

            win_pnls  = [t["realized_pnl_pct"] for t in wins]
            loss_pnls = [t["realized_pnl_pct"] for t in losses]

            perf.gross_profit = sum(win_pnls)
            perf.gross_loss   = sum(loss_pnls)

            score_wins   = [t.get("entry_score", 50.0) for t in wins]
            score_losses = [t.get("entry_score", 50.0) for t in losses]

            perf.avg_score_wins   = _mean(score_wins)
            perf.avg_score_losses = _mean(score_losses)

            perf.is_significant = len(group) >= self._min_sample // 3

            performances.append(perf)

            log.debug(
                "Regime %s: %d trades, wr=%.1f%%, pf=%.2f, significant=%s",
                regime_str, len(group),
                perf.win_rate, perf.profit_factor, perf.is_significant,
            )

        report.regime_performance = performances

        sig_perfs = [p for p in performances if p.is_significant]
        if sig_perfs:
            best  = max(sig_perfs, key=lambda p: p.win_rate)
            worst = min(sig_perfs, key=lambda p: p.win_rate)
            report.best_regime  = best.regime
            report.worst_regime = worst.regime

        return report

    def _compute_indicator_effectiveness(
        self,
        report:         AttributionReport,
        score_outcomes: List[Dict[str, Any]],
    ) -> AttributionReport:
        indicator_cols = [
            "trend_score",
            "momentum_score",
            "strength_score",
            "volatility_score",
            "pattern_score",
            "oscillator_score",
            "structure_score",
            "orderbook_score",
            "total_score",
        ]

        effectiveness_list: List[IndicatorEffectiveness] = []

        outcomes_binary = [1.0 if r["is_win"] else 0.0 for r in score_outcomes]

        for col in indicator_cols:
            scores = [r.get(col) for r in score_outcomes]
            valid_pairs = [
                (s, o) for s, o in zip(scores, outcomes_binary)
                if s is not None and not math.isnan(s)
            ]

            if len(valid_pairs) < self._min_ind:
                continue

            score_vals = [p[0] for p in valid_pairs]
            outcome_vals = [p[1] for p in valid_pairs]

            wins_scores  = [s for s, o in valid_pairs if o == 1.0]
            loss_scores  = [s for s, o in valid_pairs if o == 0.0]

            avg_win  = _mean(wins_scores)
            avg_loss = _mean(loss_scores)
            diff     = avg_win - avg_loss
            corr     = _pearson_r(score_vals, outcome_vals)

            eff = IndicatorEffectiveness(
                indicator_name=col.replace("_score", ""),
                avg_score_in_wins=round(avg_win, 2),
                avg_score_in_losses=round(avg_loss, 2),
                score_differential=round(diff, 2),
                correlation=round(corr, 4),
                sample_size=len(valid_pairs),
                is_significant=len(valid_pairs) >= self._min_ind,
            )
            effectiveness_list.append(eff)

            log.debug(
                "Indicator %s: avg_win=%.1f avg_loss=%.1f diff=%.1f corr=%.3f n=%d predictive=%s",
                col, avg_win, avg_loss, diff, corr,
                len(valid_pairs), eff.is_predictive,
            )

        report.indicator_effectiveness = effectiveness_list

        predictive = [e for e in effectiveness_list if e.is_predictive and e.indicator_name != "total"]
        if predictive:
            report.most_predictive_indicator = max(
                predictive, key=lambda e: abs(e.score_differential)
            ).indicator_name
            report.least_predictive_indicator = min(
                predictive, key=lambda e: abs(e.score_differential)
            ).indicator_name

        return report

    def _compute_win_rate_trend(
        self,
        report: AttributionReport,
        trades: List[Dict[str, Any]],
    ) -> AttributionReport:
        sorted_trades = sorted(trades, key=lambda t: t["timestamp"])
        outcomes      = [t["is_win"] for t in sorted_trades]

        wr_long  = _rolling_win_rate(outcomes, WIN_RATE_ROLLING_LONG)
        wr_short = _rolling_win_rate(outcomes, WIN_RATE_ROLLING_SHORT)

        report.last_30_win_rate = round(wr_long  or 0.0, 2)
        report.last_10_win_rate = round(wr_short or 0.0, 2)

        if wr_short is not None and wr_long is not None:
            diff = wr_short - wr_long
            if diff >= 10.0:
                report.win_rate_trend = "improving"
            elif diff <= -10.0:
                report.win_rate_trend = "degrading"
            else:
                report.win_rate_trend = "stable"
        else:
            report.win_rate_trend = "stable"

        log.debug(
            "Win rate trend: last_30=%.1f%% last_10=%.1f%% trend=%s",
            report.last_30_win_rate, report.last_10_win_rate, report.win_rate_trend,
        )
        return report

    def _generate_insights(
        self,
        report: AttributionReport,
    ) -> AttributionReport:
        insights: List[str] = []
        warnings: List[str] = []

        baseline_wr = report.overall_win_rate

        for perf in report.regime_performance:
            if not perf.is_significant:
                continue
            if perf.total_trades < self._ins_sample:
                continue

            wr_diff = baseline_wr - perf.win_rate
            if wr_diff >= self._wr_diff_min:
                insights.append(
                    f"⚠️ Win rate di regime {perf.regime.display_name} hanya "
                    f"{perf.win_rate:.1f}% ({wr_diff:+.1f}% vs baseline). "
                    f"Sample: {perf.total_trades} trades. "
                    f"Pertimbangkan untuk tidak trading di regime ini."
                )
            elif perf.win_rate - baseline_wr >= self._wr_diff_min:
                insights.append(
                    f"✅ Win rate terbaik di regime {perf.regime.display_name}: "
                    f"{perf.win_rate:.1f}% ({perf.win_rate - baseline_wr:+.1f}% vs baseline). "
                    f"Fokus entry saat regime ini aktif."
                )

        for eff in report.indicator_effectiveness:
            if not eff.is_significant:
                continue
            if abs(eff.score_differential) < self._wr_diff_min * 0.5:
                insights.append(
                    f"🔍 Indikator '{eff.indicator_name}' hampir tidak predictive "
                    f"(differential=+{eff.score_differential:.1f}, corr={eff.correlation:.3f}). "
                    f"Pertimbangkan turunkan bobotnya di profile ini."
                )
            elif eff.score_differential >= self._wr_diff_min:
                insights.append(
                    f"💪 Indikator '{eff.indicator_name}' sangat predictive "
                    f"(avg win={eff.avg_score_in_wins:.1f} vs loss={eff.avg_score_in_losses:.1f}, "
                    f"diff={eff.score_differential:+.1f}). "
                    f"Pertimbangkan naikkan bobotnya."
                )

        if report.win_rate_trend == "degrading":
            drop = report.last_30_win_rate - report.last_10_win_rate
            warnings.append(
                f"📉 Win rate memburuk: {report.last_30_win_rate:.1f}% (30 trade) → "
                f"{report.last_10_win_rate:.1f}% (10 trade terakhir). "
                f"Penurunan {drop:.1f}pp. Evaluasi kondisi market atau parameter."
            )
        elif report.win_rate_trend == "improving":
            gain = report.last_10_win_rate - report.last_30_win_rate
            insights.append(
                f"📈 Win rate membaik: {report.last_30_win_rate:.1f}% (30 trade) → "
                f"{report.last_10_win_rate:.1f}% (10 trade terakhir). "
                f"Kenaikan {gain:.1f}pp."
            )

        if baseline_wr < 45.0 and report.total_trades >= self._ins_sample:
            warnings.append(
                f"🚨 Overall win rate sangat rendah: {baseline_wr:.1f}% "
                f"dari {report.total_trades} trades. "
                f"Review strategi secara menyeluruh."
            )
        elif baseline_wr > 65.0 and report.total_trades >= self._ins_sample:
            insights.append(
                f"🎯 Win rate tinggi: {baseline_wr:.1f}% dari {report.total_trades} trades. "
                f"Pertimbangkan apakah threshold bisa diturunkan sedikit "
                f"untuk meningkatkan jumlah trade."
            )

        total_eff = next(
            (e for e in report.indicator_effectiveness if e.indicator_name == "total"),
            None,
        )
        if total_eff and total_eff.is_significant:
            if total_eff.score_differential < 5.0:
                warnings.append(
                    f"⚠️ Perbedaan total score antara winning ({total_eff.avg_score_in_wins:.1f}) "
                    f"dan losing trades ({total_eff.avg_score_in_losses:.1f}) hanya "
                    f"{total_eff.score_differential:.1f} poin. "
                    f"Scoring mungkin perlu di-review."
                )

        report.insights  = insights
        report.warnings  = warnings

        return report

    async def compute_attribution_with_peer(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
        force_refresh: bool          = False,
    ) -> AttributionReport:
        """
        Seperti compute_attribution, tapi menggabungkan data dari
        algotrader_test (peer) setelah dinormalisasi.
        Jika cross-learning tidak aktif, fallback ke compute_attribution biasa.
        """
        if not _CROSS_LEARN_AVAILABLE:
            return await self.compute_attribution(
                lookback_days=lookback_days,
                symbol=symbol,
                profile=profile,
                force_refresh=force_refresh,
            )

        import os
        if os.getenv("CROSS_LEARN_ENABLED", "false").lower() != "true":
            return await self.compute_attribution(
                lookback_days=lookback_days,
                symbol=symbol,
                profile=profile,
                force_refresh=force_refresh,
            )

        # Ambil data sendiri dulu
        own_trades = await self._db.get_trades_with_regime(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        own_trades = [_normalize_trade_row(t) for t in (own_trades or [])]

        # Ambil data peer dan gabungkan
        reader = get_cross_learn_reader()
        peer_trades = reader.get_peer_trades(
            lookback_days=lookback_days,
            profile=profile,
        )

        combined_trades = own_trades + peer_trades

        if symbol:
            combined_trades = [t for t in combined_trades if t.get("symbol") == symbol]
        if profile:
            combined_trades = [t for t in combined_trades if t.get("strategy_profile") == profile]

        log.info(
            "CrossLearn attribution: own=%d peer=%d combined=%d (scope=%s)",
            len(own_trades), len(peer_trades), len(combined_trades),
            self._build_scope(symbol, profile),
        )

        scope  = self._build_scope(symbol, profile)
        report = AttributionReport(
            lookback_days=lookback_days,
            scope=f"{scope}+peer",
            total_trades=len(combined_trades),
        )

        if len(combined_trades) < self._min_sample:
            report.sufficient_data = False
            report.warnings.append(
                f"Sample gabungan tidak cukup: {len(combined_trades)} < {self._min_sample}."
            )
            return report

        report.sufficient_data = True
        report = self._compute_overall_metrics(report, combined_trades)
        report = self._compute_regime_attribution(report, combined_trades)

        # Score outcomes gabungan dari signal_scores
        own_scores = await self._db.get_score_vs_outcome(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        own_scores = [_normalize_trade_row(t) for t in (own_scores or [])]

        peer_scores = reader.get_peer_signal_scores(
            lookback_days=lookback_days,
            profile=profile,
        )
        combined_scores = own_scores + peer_scores

        if symbol:
            combined_scores = [t for t in combined_scores if t.get("symbol") == symbol]
        if profile:
            combined_scores = [t for t in combined_scores if t.get("strategy_profile") == profile]

        if len(combined_scores) >= self._min_ind:
            report = self._compute_indicator_effectiveness(report, combined_scores)

        report = self._compute_win_rate_trend(report, combined_trades)
        report = self._generate_insights(report)

        # Tambah info peer ke insights
        peer_regime = reader.get_peer_regime_stats(lookback_days=lookback_days)
        if peer_regime:
            dominant = max(peer_regime.values(), key=lambda r: r["total_signals"])
            report.insights.append(
                f"🔗 CrossLearn: regime dominan di algotrader_test adalah "
                f"'{dominant['regime']}' ({dominant['total_signals']} sinyal, "
                f"avg_score={dominant['avg_score']:.1f})."
            )

        self._last_run = __import__("datetime").datetime.now(timezone.utc)
        return report

    def _build_scope(
        self,
        symbol:  Optional[str],
        profile: Optional[str],
    ) -> str:
        if symbol and profile:
            return f"symbol:{symbol}:profile:{profile}"
        if symbol:
            return f"symbol:{symbol}"
        if profile:
            return f"profile:{profile}"
        return "global"

    async def _cache_report(
        self,
        report:       AttributionReport,
        scope:        str,
        lookback_days: int,
    ) -> None:
        try:
            summary = self._serialize_report(report)
            await self._db.save_performance_snapshot(
                scope=scope,
                lookback_days=lookback_days,
                total_trades=report.total_trades,
                win_rate=report.overall_win_rate if report.sufficient_data else None,
                profit_factor=report.overall_profit_factor if report.sufficient_data else None,
                avg_score_wins=None,
                avg_score_losses=None,
                best_regime=report.best_regime.value if report.best_regime else None,
                worst_regime=report.worst_regime.value if report.worst_regime else None,
                sufficient_data=report.sufficient_data,
                summary_json=summary,
            )
        except Exception as e:
            log.error("_cache_report error: %s", e)

    def _serialize_report(self, report: AttributionReport) -> Dict[str, Any]:
        return {
            "computed_at":        report.computed_at.isoformat(),
            "lookback_days":      report.lookback_days,
            "scope":              report.scope,
            "total_trades":       report.total_trades,
            "sufficient_data":    report.sufficient_data,
            "overall_win_rate":   report.overall_win_rate,
            "overall_profit_factor": report.overall_profit_factor,
            "overall_avg_score":  report.overall_avg_score,
            "best_regime":        report.best_regime.value if report.best_regime else None,
            "worst_regime":       report.worst_regime.value if report.worst_regime else None,
            "win_rate_trend":     report.win_rate_trend,
            "last_30_win_rate":   report.last_30_win_rate,
            "last_10_win_rate":   report.last_10_win_rate,
            "most_predictive_indicator":  report.most_predictive_indicator,
            "least_predictive_indicator": report.least_predictive_indicator,
            "insights":           report.insights,
            "warnings":           report.warnings,
            "regime_performance": [
                {
                    "regime":           p.regime.value,
                    "total_trades":     p.total_trades,
                    "win_count":        p.win_count,
                    "loss_count":       p.loss_count,
                    "win_rate":         round(p.win_rate, 2),
                    "profit_factor":    round(p.profit_factor, 4) if p.profit_factor != float("inf") else 9999.0,
                    "net_pnl":          round(p.net_pnl, 4),
                    # [BUG-FIX] gross_profit & gross_loss sebelumnya TIDAK
                    # disimpan sama sekali -- hanya net_pnl (gross_profit +
                    # gross_loss) dan profit_factor (turunan) yang di-cache.
                    # Akibatnya _deserialize_report tidak bisa merekonstruksi
                    # gross_profit/gross_loss asli (lihat fix di sana).
                    "gross_profit":     round(p.gross_profit, 4),
                    "gross_loss":       round(p.gross_loss, 4),
                    "avg_score_wins":   round(p.avg_score_wins, 2),
                    "avg_score_losses": round(p.avg_score_losses, 2),
                    "is_significant":   p.is_significant,
                }
                for p in report.regime_performance
            ],
            "indicator_effectiveness": [
                {
                    "indicator_name":      e.indicator_name,
                    "avg_score_in_wins":   e.avg_score_in_wins,
                    "avg_score_in_losses": e.avg_score_in_losses,
                    "score_differential":  e.score_differential,
                    "correlation":         e.correlation,
                    "sample_size":         e.sample_size,
                    "is_significant":      e.is_significant,
                    "is_predictive":       e.is_predictive,
                }
                for e in report.indicator_effectiveness
            ],
        }

    def _deserialize_report(self, data: Dict[str, Any]) -> AttributionReport:
        report = AttributionReport(
            lookback_days=data.get("lookback_days", 30),
            scope=data.get("scope", "global"),
            total_trades=data.get("total_trades", 0),
            sufficient_data=data.get("sufficient_data", False),
            overall_win_rate=data.get("overall_win_rate", 0.0),
            overall_profit_factor=data.get("overall_profit_factor", 0.0),
            overall_avg_score=data.get("overall_avg_score", 50.0),
            win_rate_trend=data.get("win_rate_trend", "stable"),
            last_30_win_rate=data.get("last_30_win_rate", 0.0),
            last_10_win_rate=data.get("last_10_win_rate", 0.0),
            most_predictive_indicator=data.get("most_predictive_indicator", ""),
            least_predictive_indicator=data.get("least_predictive_indicator", ""),
            insights=data.get("insights", []),
            warnings=data.get("warnings", []),
        )

        br = data.get("best_regime")
        wr = data.get("worst_regime")
        try:
            report.best_regime  = MarketRegime(br) if br else None
            report.worst_regime = MarketRegime(wr) if wr else None
        except ValueError:
            pass

        for rp in data.get("regime_performance", []):
            try:
                regime_enum = MarketRegime(rp["regime"])
            except (ValueError, KeyError):
                regime_enum = MarketRegime.UNDEFINED

            perf = RegimePerformance(
                regime=regime_enum,
                total_trades=rp.get("total_trades", 0),
                win_count=rp.get("win_count", 0),
                loss_count=rp.get("loss_count", 0),
                # [BUG-FIX] Sebelumnya: gross_profit=rp.get("net_pnl", 0.0) --
                # salah field (net_pnl = gross_profit + gross_loss, bukan
                # gross_profit itu sendiri), dan gross_loss tidak di-pass
                # sama sekali -> selalu default 0.0. Akibatnya
                # perf.profit_factor (property = gross_profit/gross_loss)
                # SELALU jadi inf atau 0 untuk report yang diambil dari
                # cache (skip komputasi ulang). Sekarang baca gross_profit
                # & gross_loss langsung dari field yang benar (lihat fix di
                # _serialize_report yang sekarang menyimpan keduanya).
                gross_profit=rp.get("gross_profit", 0.0),
                gross_loss=rp.get("gross_loss", 0.0),
                avg_score_wins=rp.get("avg_score_wins", 50.0),
                avg_score_losses=rp.get("avg_score_losses", 50.0),
                is_significant=rp.get("is_significant", False),
            )
            report.regime_performance.append(perf)

        for ie in data.get("indicator_effectiveness", []):
            eff = IndicatorEffectiveness(
                indicator_name=ie.get("indicator_name", ""),
                avg_score_in_wins=ie.get("avg_score_in_wins", 50.0),
                avg_score_in_losses=ie.get("avg_score_in_losses", 50.0),
                score_differential=ie.get("score_differential", 0.0),
                correlation=ie.get("correlation", 0.0),
                sample_size=ie.get("sample_size", 0),
                is_significant=ie.get("is_significant", False),
            )
            report.indicator_effectiveness.append(eff)

        return report

    def format_report_telegram(self, report: AttributionReport) -> str:
        if not report.sufficient_data:
            return (
                f"📊 Attribution ({report.scope}) — Data belum cukup\n"
                f"  Trades: {report.total_trades} / {self._min_sample} minimum\n"
                + "\n".join(f"  ⚠️ {w}" for w in report.warnings[:3])
            )

        lines = [
            f"📊 Attribution: {report.scope} (last {report.lookback_days}d)",
            f"  Trades: {report.total_trades} | WR: {report.overall_win_rate:.1f}% | "
            f"PF: {report.overall_profit_factor:.2f}",
            f"  Trend: {report.win_rate_trend} "
            f"({report.last_30_win_rate:.1f}% → {report.last_10_win_rate:.1f}%)",
        ]

        if report.regime_performance:
            lines.append("\n📈 Regime Performance:")
            sig = [p for p in report.regime_performance if p.is_significant]
            for p in sorted(sig, key=lambda x: x.win_rate, reverse=True)[:4]:
                lines.append(
                    f"  {p.regime.emoji} {p.regime.display_name[:16]:<16} "
                    f"WR={p.win_rate:.1f}% ({p.total_trades}T)"
                )

        if report.insights:
            lines.append("\n💡 Insights:")
            for ins in report.insights[:3]:
                short = ins[:120] + ("..." if len(ins) > 120 else "")
                lines.append(f"  • {short}")

        if report.warnings:
            lines.append("\n⚠️ Warnings:")
            for w in report.warnings[:2]:
                short = w[:120] + ("..." if len(w) > 120 else "")
                lines.append(f"  • {short}")

        return "\n".join(lines)

    def format_report_api(self, report: AttributionReport) -> Dict[str, Any]:
        return self._serialize_report(report)
        
class PerformanceAnalytics:

    def __init__(self, db, config: dict):
        cfg = config or {}

        min_sample = int(
            cfg.get("min_sample_for_attribution", MIN_SAMPLE_FOR_ATTRIBUTION)
        )
        refresh_interval = int(
            cfg.get("analytics_refresh_interval", ANALYTICS_REFRESH_INTERVAL_S)
        )
        after_n_trades = int(
            cfg.get("analytics_after_n_trades", ANALYTICS_TRIGGER_ON_N_TRADES)
        )

        self._engine = AnalyticsEngine(
            db_manager=db,
            min_sample=min_sample,
            min_sample_indicator=MIN_SAMPLE_FOR_INDICATOR_STATS,
            insight_min_win_rate_diff=INSIGHT_MIN_WIN_RATE_DIFF,
            insight_min_sample=INSIGHT_MIN_SAMPLE_SIZE,
            indicator_predictive_thresh=INDICATOR_PREDICTIVE_THRESHOLD,
        )

        self._engine._refresh_interval_s  = refresh_interval
        self._engine._trigger_on_n_trades  = after_n_trades

        self._db     = db
        self._config = cfg

        self._last_run_time:          Optional[datetime] = None
        self._trade_count_at_last_run: int               = 0

        log.info(
            "PerformanceAnalytics init: min_sample=%d refresh=%ds after_n=%d",
            min_sample, refresh_interval, after_n_trades,
        )

    async def compute_attribution(
        self,
        lookback_days: int            = 30,
        symbol:        Optional[str]  = None,
        profile:       Optional[str]  = None,
        force_refresh: bool           = False,
        filters:       Optional[Dict] = None,
        group_by:      Optional[str]  = None,
    ) -> AttributionReport:
        """
        # [BUG-FIX] Sebelumnya tidak ada parameter group_by di signature ini,
        # padahal api_server.py endpoint /api/analytics/regime_performance
        # (baris ~1460) memanggil compute_attribution(..., group_by="regime")
        # -> TypeError: unexpected keyword argument 'group_by' -> endpoint
        # SELALU crash (caught -> HTTP 500) setiap kali diakses.
        # Fix: terima parameter group_by. AttributionReport SUDAH SELALU
        # menyertakan report.regime_performance (breakdown per regime)
        # apapun nilai group_by-nya -- jadi cukup diterima & diabaikan utk
        # saat ini (tidak ada mode grouping lain yang diimplementasikan di
        # AnalyticsEngine). Parameter dipertahankan di signature supaya
        # caller yang eksplisit minta group_by="regime" tidak crash, dan
        # supaya future group_by mode lain bisa ditambah di sini nanti
        # tanpa breaking existing caller.
        """
        if filters:
            symbol  = filters.get("symbol",  symbol)
            profile = filters.get("profile", profile)

        if group_by is not None and group_by != "regime":
            log.debug(
                "compute_attribution: group_by='%s' diterima tapi belum ada "
                "mode grouping selain 'regime' (default report sudah "
                "menyertakan regime_performance).", group_by,
            )

        return await self._engine.compute_attribution(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
            force_refresh=force_refresh,
        )

    async def compute_all_profiles(
        self,
        lookback_days: int  = 30,
        force_refresh: bool = False,
    ) -> Dict[str, AttributionReport]:
        return await self._engine.compute_all_profiles(
            lookback_days=lookback_days,
            force_refresh=force_refresh,
        )

    async def compute_indicator_effectiveness(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
    ) -> Dict[str, Any]:
        score_outcomes = await self._db.get_score_vs_outcome(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        if not score_outcomes:
            return {}

        report = AttributionReport(
            lookback_days=lookback_days,
            scope=self._engine._build_scope(symbol, profile),
            total_trades=len(score_outcomes),
            sufficient_data=len(score_outcomes) >= self._engine._min_ind,
        )
        if report.sufficient_data:
            report = self._engine._compute_indicator_effectiveness(
                report, score_outcomes
            )

        return {
            eff.indicator_name: eff
            for eff in report.indicator_effectiveness
        }

    def should_run_now(self, new_trade_count: int = 0) -> bool:
        if self._last_run_time is None:
            return True

        refresh_s = getattr(
            self._engine, "_refresh_interval_s",
            int(self._config.get("analytics_refresh_interval", ANALYTICS_REFRESH_INTERVAL_S)),
        )
        trigger_n = getattr(
            self._engine, "_trigger_on_n_trades",
            int(self._config.get("analytics_after_n_trades", ANALYTICS_TRIGGER_ON_N_TRADES)),
        )

        age_secs = (datetime.now(timezone.utc) - self._last_run_time).total_seconds()
        if age_secs >= refresh_s:
            return True
        if new_trade_count >= trigger_n:
            return True
        return False

    async def run_full_analysis(
        self,
        lookback_days: int  = 30,
        force_refresh: bool = False,
    ) -> Dict[str, AttributionReport]:
        results = await self.compute_all_profiles(
            lookback_days=lookback_days,
            force_refresh=force_refresh,
        )
        self._last_run_time = datetime.now(timezone.utc)
        return results

    async def refresh_snapshots(
        self,
        lookback_days: int  = 30,
        force_refresh: bool = False,
    ) -> Dict[str, AttributionReport]:
        return await self.run_full_analysis(
            lookback_days=lookback_days,
            force_refresh=force_refresh,
        )

    async def load_persistent_parameters(self) -> None:
        try:
            history = await self._db.get_parameter_history(limit=200)
            if not history:
                log.info(
                    "load_persistent_parameters: tidak ada parameter history di DB."
                )
                return

            applied = sum(
                1 for r in history
                # [BUG-FIX] Sebelumnya: getattr(r, "outcome", None) -- tapi
                # db_manager.get_parameter_history() return List[Dict], bukan
                # objek. getattr() pada dict TIDAK PERNAH menemukan key
                # sebagai attribute -> selalu default None -> applied selalu
                # 0 di log, walaupun datanya ada. Sekarang pakai _row_get()
                # (helper yang sudah ada di file ini) yang benar utk dict
                # MAUPUN objek/mock.
                if _row_get(r, "outcome") == "applied"
            )
            log.info(
                "load_persistent_parameters: %d records di DB, "
                "%d berstatus 'applied'.",
                len(history), applied,
            )
        except AttributeError:
            log.debug(
                "load_persistent_parameters: get_parameter_history "
                "tidak tersedia (mungkin mock)."
            )
        except Exception as e:
            log.warning("load_persistent_parameters error (non-fatal): %s", e)

    def format_report_telegram(self, report: AttributionReport) -> str:
        return self._engine.format_report_telegram(report)

    def format_report_api(self, report: AttributionReport) -> Dict[str, Any]:
        return self._engine.format_report_api(report)

    async def run_cross_analysis(
        self,
        lookback_days: int  = 30,
        force_refresh: bool = False,
    ) -> Dict[str, AttributionReport]:
        """
        Jalankan analisis gabungan: data algotrader + algotrader_test.
        Hasilnya lebih kaya karena punya lebih banyak sample.
        """
        import os
        if os.getenv("CROSS_LEARN_ENABLED", "false").lower() != "true":
            log.info("CrossLearn: tidak aktif, skip run_cross_analysis.")
            return {}

        log.info("CrossLearn: menjalankan analisis gabungan (lookback=%dd)...", lookback_days)

        results: Dict[str, AttributionReport] = {}

        # Global (semua profil, semua coin)
        results["global+peer"] = await self._engine.compute_attribution_with_peer(
            lookback_days=lookback_days,
            force_refresh=force_refresh,
        )

        # Per profil
        try:
            all_trades = await self._db.get_trades_with_regime(lookback_days=lookback_days)
            all_trades = all_trades or []
            profiles = set(
                t.get("strategy_profile") or t.get("strategy_name", "")
                for t in all_trades
                if t.get("strategy_profile") or t.get("strategy_name")
            )
            for prof in sorted(profiles):
                results[f"{prof}+peer"] = await self._engine.compute_attribution_with_peer(
                    lookback_days=lookback_days,
                    profile=prof,
                    force_refresh=force_refresh,
                )
        except Exception as e:
            log.error("run_cross_analysis per-profile error: %s", e)

        log.info("CrossLearn: analisis gabungan selesai: %d reports.", len(results))
        self._last_run_time = __import__("datetime").datetime.now(timezone.utc)
        return results

    async def get_cross_learn_summary(self) -> Dict:
        """Ringkasan status cross-learning untuk Telegram/API."""
        if not _CROSS_LEARN_AVAILABLE:
            return {"enabled": False, "reason": "module tidak tersedia"}
        reader = get_cross_learn_reader()
        return reader.get_summary()

    async def compute_rolling_win_rate(
        self,
        window:        int           = 30,
        lookback_days: int           = 90,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
    ) -> List[Optional[float]]:
        trades = await self._db.get_trades_with_regime(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )
        trades = [_normalize_trade_row(t) for t in (trades or [])]
        if symbol:
            trades = [t for t in trades if t.get("symbol") == symbol]
        if profile:
            trades = [t for t in trades if t.get("strategy_profile") == profile]
        if not trades:
            return []

        results = []
        for i in range(len(trades)):
            start = max(0, i - window + 1)
            chunk = trades[start: i + 1]
            wins  = sum(
                1 for t in chunk
                if t.get("is_win", (t.get("realized_pnl_pct") or 0) > 0)
            )
            results.append(
                wins / len(chunk) * 100.0 if chunk else None
            )
        return results