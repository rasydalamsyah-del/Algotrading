"""
learning/meta_learner.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    META_LEARNER_MIN_SAMPLE,
    META_LEARNER_MAX_THRESHOLD_CHANGE,
    META_LEARNER_COOLING_OFF_DAYS,
    META_LEARNER_APPROVAL_WINDOW_H,
    META_WIN_RATE_LOW_THRESHOLD,
    META_WIN_RATE_HIGH_THRESHOLD,
    META_MIN_PROJECTED_IMPROVEMENT,
    META_TRACK_TRADES_AFTER_APPLY,
    META_REVERT_WIN_RATE_DROP,
    META_PARAM_BOUNDS,
    INSIGHT_MIN_SAMPLE_SIZE,
)
from core.models import (
    AttributionReport,
    IndicatorEffectiveness,
    MarketRegime,
    ParameterSuggestion,
    RegimePerformance,
    SuggestionStatus,
)

log = logging.getLogger("learning.meta_learner")

# Cross-learning support
try:
    from learning.cross_learn import get_cross_learn_reader
    _CROSS_LEARN_AVAILABLE = True
except ImportError:
    _CROSS_LEARN_AVAILABLE = False

_THRESHOLD_STEP_UP   = 3.0
_THRESHOLD_STEP_DOWN = 2.0
_VOLUME_STEP         = 0.5
_RSI_STEP            = 2.0
_WEIGHT_STEP         = 0.05

# ── Adaptive Weight Auto-Apply ────────────────────────────────────────────────
_CATEGORY_MAP = {
    "trend":      ["ema_stack", "cross", "supertrend", "vwap"],
    "momentum":   ["rsi", "macd", "stochrsi"],
    "strength":   ["adx", "di", "volume", "mfi"],
    "volatility": ["bb", "squeeze", "atr"],
    "pattern":    ["pattern_score", "context_score"],
    "oscillator": ["cci", "williams", "roc"],
    "structure":  ["ichimoku", "sar", "pivot", "fibonacci"],
    "orderbook":  ["ob_score"],
}

def _apply_weight_change(profile: str, indicator: str, delta: float) -> tuple:
    import sys, logging
    log = logging.getLogger(__name__)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

    try:
        from profiles.weights import LEVEL1_WEIGHTS
    except ImportError:
        return False, "Gagal import LEVEL1_WEIGHTS"

    if profile not in LEVEL1_WEIGHTS:
        return False, f"Profile '{profile}' tidak ditemukan"

    category = None
    for cat, members in _CATEGORY_MAP.items():
        if indicator == cat or indicator in members:
            category = cat
            break
    if category is None:
        return False, f"Indikator '{indicator}' tidak dikenali"

    weights = dict(LEVEL1_WEIGHTS[profile])
    if category not in weights:
        return False, f"Kategori '{category}' tidak ada di profile '{profile}'"

    old_w = weights[category]
    new_w = round(max(0.02, old_w + delta), 4)
    actual_delta = new_w - old_w
    if abs(actual_delta) < 0.001:
        return False, "Delta terlalu kecil."

    others = {k: v for k, v in weights.items() if k != category}
    total_others = sum(others.values())
    if total_others < 0.001:
        return False, "Tidak ada kategori lain untuk redistribusi."

    weights[category] = new_w
    for k in others:
        weights[k] = round(others[k] - (others[k] / total_others) * actual_delta, 4)

    total = sum(weights.values())
    weights = {k: round(v / total, 4) for k, v in weights.items()}

    weights_path = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "profiles" / "weights.py")
    try:
        with open(weights_path, "r") as f:
            src = f.read()

        l1_start = src.find("LEVEL1_WEIGHTS")
        if l1_start == -1:
            return False, "LEVEL1_WEIGHTS tidak ditemukan"

        prof_marker = f'"{profile}":'
        prof_pos = src.find(prof_marker, l1_start)
        if prof_pos == -1:
            return False, f"Profile '{profile}' tidak ditemukan di weights.py"

        brace_open = src.find("{", prof_pos)
        depth = 0
        i = brace_open
        while i < len(src):
            if src[i] == "{": depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    brace_close = i
                    break
            i += 1

        new_block_inner = ""
        for k, v in weights.items():
            new_block_inner += f'        "{k}": {v},\n'

        new_block = "{\n" + new_block_inner + "    }"
        new_src = src[:brace_open] + new_block + src[brace_close+1:]

        with open(weights_path, "w") as f:
            f.write(new_src)

        # [BUG-FIX -- ditemukan lewat verifikasi arsitektur lebih dalam,
        # 2026-07-09] Sebelumnya fungsi ini HANYA menulis ke file
        # profiles/weights.py di disk -- dict LEVEL1_WEIGHTS yang SUDAH
        # di-import ke memori (dipakai get_level1_weights() di seluruh
        # pipeline scoring, mis. scorer.py._calc_weighted_breakdown) TIDAK
        # PERNAH di-refresh. Tidak ada mekanisme reload apapun di codebase
        # (grep importlib.reload nihil). Akibatnya seluruh fitur "autonomous
        # weight adjustment" MetaLearner TIDAK benar-benar mengubah perilaku
        # bot yang sedang berjalan -- suggestion "berhasil di-apply" (file
        # ditulis, log sukses, notifikasi terkirim) tapi scoring LIVE tetap
        # pakai bobot LAMA sampai proses di-restart. Beda dgn parameter lain
        # (entry_threshold, volume_multiplier, dst) yang lewat
        # apply_parameter_override() -- itu mutasi dict in-memory
        # _ACTIVE_OVERRIDES langsung, jadi otomatis live-effect, TIDAK
        # kena bug ini. Fix root-cause: setelah berhasil tulis file (utk
        # persist ke disk, bertahan lintas restart), JUGA mutasi
        # LEVEL1_WEIGHTS[profile] in-memory langsung -- karena LEVEL1_WEIGHTS
        # adalah dict module-level yang di-share by-reference ke semua
        # importer (termasuk profiles.weights.get_level1_weights() yang
        # dipakai scorer.py), mutasi in-place ini otomatis terlihat di
        # seluruh proses tanpa perlu reload modul.
        LEVEL1_WEIGHTS[profile] = weights

        log.info("AdaptiveWeights: profile=%s category=%s %.4f→%.4f",
                 profile, category, old_w, new_w)
        return True, (
            f"Bobot '{category}' di profile '{profile}' "
            f"diubah {old_w:.4f} → {new_w:.4f} (delta {actual_delta:+.4f})"
        )
    except Exception as e:
        return False, f"Error tulis weights.py: {e}"


class MetaLearner:

    def __init__(
        self,
        db_manager,
        analytics_engine,
        mode:                    str   = "advisory",
        min_sample:              int   = META_LEARNER_MIN_SAMPLE,
        max_threshold_change:    float = META_LEARNER_MAX_THRESHOLD_CHANGE,
        cooling_off_days:        int   = META_LEARNER_COOLING_OFF_DAYS,
        approval_window_hours:   int   = META_LEARNER_APPROVAL_WINDOW_H,
        win_rate_low_threshold:  float = META_WIN_RATE_LOW_THRESHOLD,
        win_rate_high_threshold: float = META_WIN_RATE_HIGH_THRESHOLD,
        min_projected_improvement: float = META_MIN_PROJECTED_IMPROVEMENT,
        track_trades_after:      int   = META_TRACK_TRADES_AFTER_APPLY,
        revert_win_rate_drop:    float = META_REVERT_WIN_RATE_DROP,
    ):
        if mode not in ("advisory", "autonomous"):
            raise ValueError(
                f"META_LEARNER_MODE harus 'advisory' atau 'autonomous', got: '{mode}'. "
                f"Default adalah 'advisory' — autonomous harus di-enable eksplisit di .env."
            )

        self._db                   = db_manager
        self._analytics            = analytics_engine
        self._mode                 = mode
        self._min_sample           = min_sample
        self._max_change           = max_threshold_change
        self._cooling_days         = cooling_off_days
        self._approval_window_h    = approval_window_hours
        self._wr_low               = win_rate_low_threshold
        self._wr_high              = win_rate_high_threshold
        self._min_improvement      = min_projected_improvement
        self._track_trades         = track_trades_after
        self._revert_drop          = revert_win_rate_drop
        
        self._cooling_cache: Dict[str, datetime] = {}
        self._pending: Dict[str, ParameterSuggestion] = {}
        self._last_apply_at: Dict[str, datetime] = {}

        log.info(
            "MetaLearner init: mode=%s min_sample=%d cooling=%dd approval=%dh",
            mode, min_sample, cooling_off_days, approval_window_hours,
        )

    async def run_full_cycle(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
    ) -> List[ParameterSuggestion]:
        log.info(
            "MetaLearner full cycle: mode=%s scope=%s lookback=%dd",
            self._mode,
            f"{symbol or 'all'}/{profile or 'all'}",
            lookback_days,
        )

        report = await self._analytics.compute_attribution(
            lookback_days=lookback_days,
            symbol=symbol,
            profile=profile,
        )

        new_suggestions = await self.generate_suggestions(report, symbol=symbol, profile=profile)

        if self._mode == "autonomous":
            await self._auto_apply_eligible(symbol=symbol, profile=profile)

        await self.check_pending_outcomes(symbol=symbol, profile=profile)

        return new_suggestions

    async def run_cross_cycle(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
    ) -> List[ParameterSuggestion]:
        """
        Seperti run_full_cycle tapi menggunakan data gabungan
        algotrader + algotrader_test untuk generate suggestions
        yang lebih akurat karena sample lebih banyak.
        """
        import os
        if not _CROSS_LEARN_AVAILABLE:
            return await self.run_full_cycle(lookback_days, symbol, profile)
        if os.getenv("CROSS_LEARN_ENABLED", "false").lower() != "true":
            return await self.run_full_cycle(lookback_days, symbol, profile)

        log.info(
            "MetaLearner cross cycle: mode=%s lookback=%dd",
            self._mode, lookback_days,
        )

        # Gunakan compute_attribution_with_peer jika tersedia
        compute_fn = getattr(self._analytics, "run_cross_analysis", None)
        if callable(compute_fn):
            cross_reports = await compute_fn(lookback_days=lookback_days)
        else:
            cross_reports = {}

        # Ambil report global+peer untuk generate suggestions
        report = cross_reports.get("global+peer")
        if report is None:
            # Fallback ke compute biasa
            report = await self._analytics.compute_attribution(
                lookback_days=lookback_days,
                symbol=symbol,
                profile=profile,
            )

        suggestions = await self.generate_suggestions(
            report, symbol=symbol, profile=profile
        )

        # Tambah insights dari peer regime stats
        if _CROSS_LEARN_AVAILABLE:
            try:
                reader = get_cross_learn_reader()
                peer_regimes = reader.get_peer_regime_stats(lookback_days=lookback_days)
                if peer_regimes:
                    log.info(
                        "CrossLearn meta: peer regimes=%s",
                        list(peer_regimes.keys()),
                    )
                    # Jika peer banyak sinyal di regime tertentu tapi score rendah,
                    # jadikan sinyal peringatan
                    for regime, stats in peer_regimes.items():
                        if (stats["total_signals"] >= 10
                                and stats["avg_score"] < 50.0):
                            log.warning(
                                "CrossLearn: regime '%s' di peer punya avg_score rendah "
                                "(%.1f) dari %d sinyal — waspada di regime ini.",
                                regime, stats["avg_score"], stats["total_signals"],
                            )
            except Exception as e:
                log.debug("CrossLearn regime insight error: %s", e)

        if self._mode == "autonomous":
            await self._auto_apply_eligible(symbol=symbol, profile=profile)

        await self.check_pending_outcomes(symbol=symbol, profile=profile)

        return suggestions

    async def generate_suggestions(
        self,
        report:  AttributionReport,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
    ) -> List[ParameterSuggestion]:
        if not report.sufficient_data:
            log.info(
                "generate_suggestions: skipped — insufficient data (scope=%s)",
                report.scope,
            )
            return []

        if report.total_trades < self._min_sample:
            log.info(
                "generate_suggestions: skipped — %d trades < %d minimum",
                report.total_trades, self._min_sample,
            )
            return []

        suggestions: List[ParameterSuggestion] = []

        target_symbol  = symbol  or ""
        target_profile = profile or ""

        sug = await self._rule_threshold_too_low(
            report, target_symbol, target_profile
        )
        if sug:
            suggestions.append(sug)

        sug = await self._rule_indicator_weight_adjustment(
            report, target_symbol, target_profile
        )
        if sug:
            suggestions.append(sug)

        sug = await self._rule_threshold_too_high(
            report, target_symbol, target_profile
        )
        if sug:
            suggestions.append(sug)

        for perf in report.regime_performance:
            sug = await self._rule_bad_regime(
                perf, report, target_symbol, target_profile
            )
            if sug:
                suggestions.append(sug)
                break

        sug = await self._rule_volume_adjustment(
            report, target_symbol, target_profile
        )
        if sug:
            suggestions.append(sug)

        saved: List[ParameterSuggestion] = []
        for sug in suggestions:
            if not self._is_delta_safe(sug):
                log.info(
                    "Suggestion rejected (unsafe delta): %s/%s %s (%s -> %s)",
                    sug.symbol, sug.profile, sug.parameter_name,
                    sug.current_value, sug.suggested_value,
                )
                continue
            if sug.projected_improvement < self._min_improvement:
                log.debug(
                    "Suggestion rejected (projected_improvement %.1f < %.1f): %s/%s %s",
                    sug.projected_improvement, self._min_improvement,
                    sug.symbol, sug.profile, sug.parameter_name,
                )
                continue

            await self._save_suggestion(sug)
            self._pending[sug.suggestion_id] = sug
            saved.append(sug)

            log.info(
                "Suggestion generated: %s/%s | %s: %s → %s | "
                "conf=%.1f%% | proj_improvement=+%.1f%%",
                sug.symbol or "ALL", sug.profile or "ALL",
                sug.parameter_name,
                sug.current_value, sug.suggested_value,
                sug.confidence * 100,
                sug.projected_improvement,
            )

        if not saved:
            log.info("generate_suggestions: no actionable suggestions (scope=%s)", report.scope)
        else:
            log.info(
                "generate_suggestions: %d suggestion(s) generated (scope=%s)",
                len(saved), report.scope,
            )

        return saved

    async def approve_suggestion(
        self,
        suggestion_id: str,
        approved_by:   str = "manual",
    ) -> Tuple[bool, str]:
        sug = await self._load_suggestion(suggestion_id)
        if sug is None:
            return False, f"Suggestion '{suggestion_id[:8]}' tidak ditemukan."

        if sug.status != SuggestionStatus.PENDING:
            return False, (
                f"Suggestion '{suggestion_id[:8]}' sudah dalam status "
                f"{sug.status.value} — tidak bisa di-approve lagi."
            )

        ok, msg = await self._apply_suggestion(sug, approved_by=approved_by)
        if not ok:
            return False, f"Gagal apply: {msg}"

        sug.status      = SuggestionStatus.APPLIED
        sug.approved_at = _utcnow()
        sug.approved_by = approved_by
        await self._update_suggestion_status(sug)

        self._set_cooling(sug.symbol, sug.profile, sug.parameter_name)

        log.info(
            "Suggestion APPROVED & APPLIED: %s by %s | %s/%s %s: %s → %s",
            suggestion_id[:8], approved_by,
            sug.symbol or "ALL", sug.profile or "ALL",
            sug.parameter_name, sug.current_value, sug.suggested_value,
        )
        return True, (
            f"✅ Applied: {sug.symbol or 'ALL'}/{sug.profile or 'ALL'} | "
            f"{sug.parameter_name}: {sug.current_value} → {sug.suggested_value}"
        )

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reason:        str = "",
        rejected_by:   str = "manual",
    ) -> Tuple[bool, str]:
        sug = await self._load_suggestion(suggestion_id)
        if sug is None:
            return False, f"Suggestion '{suggestion_id[:8]}' tidak ditemukan."

        if sug.status != SuggestionStatus.PENDING:
            return False, (
                f"Suggestion '{suggestion_id[:8]}' sudah {sug.status.value}."
            )

        sug.status        = SuggestionStatus.REJECTED
        sug.rejected_at   = _utcnow()
        sug.rejection_note = reason
        await self._update_suggestion_status(sug)

        self._set_cooling(sug.symbol, sug.profile, sug.parameter_name)

        log.info(
            "Suggestion REJECTED: %s by %s | reason: %s",
            suggestion_id[:8], rejected_by, reason or "(no reason)",
        )
        return True, (
            f"❌ Rejected: {sug.parameter_name} untuk "
            f"{sug.symbol or 'ALL'}/{sug.profile or 'ALL'}"
        )

    async def check_pending_outcomes(
        self,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # [CATATAN — cross-check dgn database.py.get_pending_outcomes()]
        # Parameter min_trades_threshold=self._track_trades di bawah TIDAK
        # dipakai untuk filter di level query (lihat komentar di
        # database.py.get_pending_outcomes: trades_after_apply masih 0 selagi
        # PENDING sehingga filter di situ akan selalu kosong). Gating jumlah
        # trade yang sebenarnya terjadi di _evaluate_outcome() di bawah, via
        # get_trade_stats(limit=self._track_trades, since=applied_at) + cek
        # "total_trades < self._track_trades // 2". Keduanya SENGAJA tidak
        # disatukan: separuh dari track_trades dipakai sebagai ambang minimum
        # supaya outcome bisa dievaluasi lebih awal (feedback lebih cepat)
        # daripada menunggu penuh track_trades trade baru. Parameter
        # min_trades_threshold di sini tetap dikirim untuk dokumentasi intent
        # pemanggil, meski saat ini tidak berefek di sisi database.py.
        try:
            pending_records = await self._db.get_pending_outcomes(
                min_trades_threshold=self._track_trades,
            )
        except Exception as e:
            log.error("check_pending_outcomes: DB error: %s", e)
            return []

        results = []
        for record in pending_records:
            if symbol and record.get("symbol") != symbol:
                continue
            if profile and record.get("profile") != profile:
                continue

            # [BUG-FIX] Sebelumnya _evaluate_outcome() dipanggil tanpa
            # try/except di sini — kalau satu record punya data korup/tak
            # terduga (mis. field "id" hilang), exception akan menjalar ke
            # run_full_cycle()/run_cross_cycle() dan menggagalkan SELURUH
            # batch, termasuk record lain yang valid. Sekarang: isolasi per
            # record supaya satu record bermasalah tidak menghentikan yang
            # lain.
            try:
                result = await self._evaluate_outcome(record)
            except Exception as e:
                log.error(
                    "check_pending_outcomes: gagal evaluasi record id=%s: %s",
                    record.get("id", "?"), e,
                )
                continue
            if result:
                results.append(result)

        return results

    async def get_pending_suggestions(
        self,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
    ) -> List[ParameterSuggestion]:
        history = await self._db.get_parameter_history(
            symbol=symbol,
            profile=profile,
            limit=200,
        )
        result = []
        for rec in history:
            sid = rec.get("id")
            if sid and str(sid) in self._pending:
                sug = self._pending[str(sid)]
                if sug.status == SuggestionStatus.PENDING:
                    result.append(sug)
        return result

    async def expire_old_suggestions(self, max_age_hours: int = 48) -> int:
        cutoff = _utcnow() - timedelta(hours=max_age_hours)
        count  = 0
        for sid, sug in list(self._pending.items()):
            if sug.status == SuggestionStatus.PENDING and sug.created_at < cutoff:
                sug.status = SuggestionStatus.EXPIRED
                await self._update_suggestion_status(sug)
                self._pending.pop(sid, None)
                count += 1
                log.debug("Suggestion expired: %s", sid[:8])
        return count

    async def _rule_threshold_too_low(
        self,
        report:         AttributionReport,
        target_symbol:  str,
        target_profile: str,
    ) -> Optional[ParameterSuggestion]:
        if report.overall_win_rate >= self._wr_low:
            return None

        current_threshold = await self._get_current_threshold(target_profile)
        if current_threshold is None:
            return None

        total_eff = self._find_indicator_eff(report, "total")
        if total_eff is None:
            return None

        avg_loss_score = total_eff.avg_score_in_losses
        if avg_loss_score < current_threshold - 10:
            return None

        if self._is_cooling(target_symbol, target_profile, "entry_threshold"):
            log.debug("Rule1: cooling-off aktif untuk entry_threshold %s/%s",
                      target_symbol, target_profile)
            return None

        step = min(_THRESHOLD_STEP_UP, self._max_change)
        new_threshold = current_threshold + step

        bounds = META_PARAM_BOUNDS.get("entry_threshold")
        if bounds and not (bounds[0] <= new_threshold <= bounds[1]):
            log.debug("Rule1: new_threshold %.1f di luar bounds %s", new_threshold, bounds)
            return None

        filtered_losses = self._estimate_filtered_trades(
            report, current_threshold, new_threshold, is_win=False
        )
        total = report.total_trades
        if total == 0:
            return None

        current_wins = round(report.overall_win_rate / 100 * total)
        new_total    = total - filtered_losses
        if new_total <= 0:
            return None
        new_wr      = current_wins / new_total * 100
        proj_improvement = new_wr - report.overall_win_rate

        if proj_improvement < self._min_improvement:
            return None

        confidence = self._calc_confidence(
            sample_size=report.total_trades,
            win_rate_diff=proj_improvement,
            correlation=total_eff.correlation if total_eff else 0.0,
        )

        return ParameterSuggestion(
            symbol=target_symbol,
            profile=target_profile,
            parameter_name="entry_threshold",
            current_value=current_threshold,
            suggested_value=round(new_threshold, 1),
            value_delta=round(new_threshold - current_threshold, 1),
            confidence=confidence,
            reasoning=(
                f"Win rate {report.overall_win_rate:.1f}% < threshold {self._wr_low:.0f}%. "
                f"Avg score pada losing trades ({avg_loss_score:.1f}) dekat threshold "
                f"({current_threshold:.1f}) — banyak sinyal 'hampir bagus' yang masuk tapi kalah. "
                f"Menaikkan threshold {current_threshold:.1f} → {new_threshold:.1f} "
                f"diproyeksikan memfilter {filtered_losses} loss, "
                f"meningkatkan win rate ke ~{new_wr:.1f}%."
            ),
            supporting_data={
                "overall_win_rate":    report.overall_win_rate,
                "avg_score_losses":    avg_loss_score,
                "current_threshold":   current_threshold,
                "estimated_filtered":  filtered_losses,
                "projected_new_wr":    round(new_wr, 1),
            },
            projected_improvement=round(proj_improvement, 2),
            within_bounds=True,
            bounds_min=bounds[0] if bounds else None,
            bounds_max=bounds[1] if bounds else None,
        )

    async def _rule_threshold_too_high(
        self,
        report:         AttributionReport,
        target_symbol:  str,
        target_profile: str,
    ) -> Optional[ParameterSuggestion]:
        if report.overall_win_rate < self._wr_high:
            return None

        expected_min = self._min_sample * 2
        if report.total_trades >= expected_min:
            return None

        current_threshold = await self._get_current_threshold(target_profile)
        if current_threshold is None:
            return None

        if self._is_cooling(target_symbol, target_profile, "entry_threshold"):
            return None

        step = min(_THRESHOLD_STEP_DOWN, self._max_change)
        new_threshold = current_threshold - step

        bounds = META_PARAM_BOUNDS.get("entry_threshold")
        if bounds and not (bounds[0] <= new_threshold <= bounds[1]):
            return None

        new_wr     = report.overall_win_rate - 5.0
        proj_improvement = max(0.0, new_wr - self._wr_low)

        if proj_improvement < self._min_improvement:
            return None

        confidence = self._calc_confidence(
            sample_size=report.total_trades,
            win_rate_diff=10.0,
            correlation=0.5,
        ) * 0.7

        return ParameterSuggestion(
            symbol=target_symbol,
            profile=target_profile,
            parameter_name="entry_threshold",
            current_value=current_threshold,
            suggested_value=round(new_threshold, 1),
            value_delta=round(new_threshold - current_threshold, 1),
            confidence=confidence,
            reasoning=(
                f"Win rate sangat tinggi ({report.overall_win_rate:.1f}%) tapi "
                f"hanya {report.total_trades} trades dalam {report.lookback_days} hari. "
                f"Threshold mungkin terlalu ketat sehingga melewatkan peluang. "
                f"Menurunkan {current_threshold:.1f} → {new_threshold:.1f} "
                f"diperkirakan meningkatkan frekuensi trade."
            ),
            supporting_data={
                "overall_win_rate":   report.overall_win_rate,
                "total_trades":       report.total_trades,
                "expected_min":       expected_min,
                "current_threshold":  current_threshold,
            },
            projected_improvement=round(proj_improvement, 2),
            within_bounds=True,
            bounds_min=bounds[0] if bounds else None,
            bounds_max=bounds[1] if bounds else None,
        )

    async def _rule_indicator_weight_adjustment(
        self,
        report:         AttributionReport,
        target_symbol:  str,
        target_profile: str,
    ) -> Optional[ParameterSuggestion]:
        if report.overall_win_rate >= self._wr_high:
            return None

        if not report.indicator_effectiveness:
            return None

        candidates = [
            e for e in report.indicator_effectiveness
            if e.is_significant
            and e.indicator_name != "total"
            and abs(e.score_differential) < 5.0
            and abs(e.correlation) < 0.10
        ]

        if not candidates:
            return None

        worst = min(candidates, key=lambda e: abs(e.score_differential))

        param_name = f"weight_{worst.indicator_name}"
        if self._is_cooling(target_symbol, target_profile, param_name):
            return None

        confidence = self._calc_confidence(
            sample_size=worst.sample_size,
            win_rate_diff=5.0,
            correlation=abs(worst.correlation),
        ) * 0.6

        proj = max(self._min_improvement, 3.0)

        return ParameterSuggestion(
            symbol=target_symbol,
            profile=target_profile,
            parameter_name=param_name,
            current_value="(lihat profiles/weights.py)",
            suggested_value=f"turunkan bobot '{worst.indicator_name}' ~{_WEIGHT_STEP:.0%}",
            value_delta=f"-{_WEIGHT_STEP:.0%}",
            confidence=confidence,
            reasoning=(
                f"Indikator '{worst.indicator_name}' hampir tidak predictive: "
                f"avg score di winning trades ({worst.avg_score_in_wins:.1f}) vs "
                f"losing trades ({worst.avg_score_in_losses:.1f}), "
                f"differential = {worst.score_differential:.1f} poin, "
                f"correlation = {worst.correlation:.3f}. "
                f"Menurunkan bobotnya di profiles/weights.py untuk profile "
                f"'{target_profile or 'semua'}' berpotensi meningkatkan sinyal quality."
            ),
            supporting_data={
                "indicator":           worst.indicator_name,
                "avg_score_wins":      worst.avg_score_in_wins,
                "avg_score_losses":    worst.avg_score_in_losses,
                "score_differential":  worst.score_differential,
                "correlation":         worst.correlation,
                "sample_size":         worst.sample_size,
            },
            projected_improvement=proj,
            within_bounds=True,
        )

    async def _rule_bad_regime(
        self,
        perf:           RegimePerformance,
        report:         AttributionReport,
        target_symbol:  str,
        target_profile: str,
    ) -> Optional[ParameterSuggestion]:
        if not perf.is_significant:
            return None

        if perf.regime == MarketRegime.TRENDING_BEAR:
            return None

        baseline      = report.overall_win_rate
        wr_diff       = baseline - perf.win_rate
        min_wr_diff   = 20.0

        if wr_diff < min_wr_diff:
            return None

        if perf.total_trades < INSIGHT_MIN_SAMPLE_SIZE:
            return None

        param_name = f"disable_regime_{perf.regime.value}"
        if self._is_cooling(target_symbol, target_profile, param_name):
            return None

        confidence = self._calc_confidence(
            sample_size=perf.total_trades,
            win_rate_diff=wr_diff,
            correlation=0.7,
        )

        proj_improvement = wr_diff * (perf.total_trades / report.total_trades)

        if proj_improvement < self._min_improvement:
            return None

        return ParameterSuggestion(
            symbol=target_symbol,
            profile=target_profile,
            parameter_name=param_name,
            current_value="enabled",
            suggested_value="disabled",
            value_delta="enabled → disabled",
            confidence=confidence,
            reasoning=(
                f"Win rate di regime {perf.regime.display_name} hanya "
                f"{perf.win_rate:.1f}% ({wr_diff:.1f}pp di bawah baseline {baseline:.1f}%). "
                f"Dari {perf.total_trades} trades di regime ini, "
                f"profit factor = {perf.profit_factor:.2f}. "
                f"Pertimbangkan menghapus '{perf.regime.value}' dari allowed_regimes "
                f"di profile '{target_profile or 'semua'}'."
            ),
            supporting_data={
                "regime":         perf.regime.value,
                "regime_wr":      perf.win_rate,
                "baseline_wr":    baseline,
                "wr_diff":        wr_diff,
                "total_trades":   perf.total_trades,
                "profit_factor":  perf.profit_factor if perf.profit_factor != float("inf") else 9999.0,
            },
            projected_improvement=round(proj_improvement, 2),
            within_bounds=True,
        )

    async def _rule_volume_adjustment(
        self,
        report:         AttributionReport,
        target_symbol:  str,
        target_profile: str,
    ) -> Optional[ParameterSuggestion]:
        if report.overall_win_rate >= self._wr_high:
            return None

        strength_eff = self._find_indicator_eff(report, "strength")
        if strength_eff is None or not strength_eff.is_significant:
            return None

        current_volume_mult = await self._get_current_param(
            target_profile, "volume_mult"
        )
        if current_volume_mult is None:
            return None

        param_name = "volume_multiplier"
        if self._is_cooling(target_symbol, target_profile, param_name):
            return None

        bounds = META_PARAM_BOUNDS.get("volume_multiplier")

        if abs(strength_eff.score_differential) < 8.0:
            new_vol = current_volume_mult + _VOLUME_STEP
            if bounds and new_vol > bounds[1]:
                return None

            confidence = self._calc_confidence(
                sample_size=strength_eff.sample_size,
                win_rate_diff=8.0,
                correlation=abs(strength_eff.correlation),
            ) * 0.65

            proj = max(self._min_improvement, 3.5)

            return ParameterSuggestion(
                symbol=target_symbol,
                profile=target_profile,
                parameter_name=param_name,
                current_value=round(current_volume_mult, 1),
                suggested_value=round(new_vol, 1),
                value_delta=round(_VOLUME_STEP, 1),
                confidence=confidence,
                reasoning=(
                    f"Indikator strength/volume hampir tidak membedakan "
                    f"winning ({strength_eff.avg_score_in_wins:.1f}) vs "
                    f"losing trades ({strength_eff.avg_score_in_losses:.1f}), "
                    f"differential = {strength_eff.score_differential:.1f}. "
                    f"Menaikkan volume_mult {current_volume_mult:.1f}x → {new_vol:.1f}x "
                    f"untuk memfilter trade dengan volume rendah."
                ),
                supporting_data={
                    "strength_diff":       strength_eff.score_differential,
                    "strength_corr":       strength_eff.correlation,
                    "current_volume_mult": current_volume_mult,
                },
                projected_improvement=proj,
                within_bounds=bounds is not None and (bounds[0] <= new_vol <= bounds[1]),
                bounds_min=bounds[0] if bounds else None,
                bounds_max=bounds[1] if bounds else None,
            )

        return None

    async def _apply_suggestion(
        self,
        sug:         ParameterSuggestion,
        approved_by: str = "auto",
    ) -> Tuple[bool, str]:
        from profiles.registry import apply_parameter_override

        # [BUG-FIX] Cooldown 1 jam sebelumnya HANYA dicek untuk parameter
        # numerik (entry_threshold, volume_multiplier, dst) karena cabang
        # weight_/disable_regime_ di bawah langsung return sebelum sampai ke
        # pengecekan cooldown. Akibatnya suggestion weight_* baru untuk
        # indikator yang sama bisa diterapkan berkali-kali dalam waktu
        # singkat (mis. beberapa cycle berturut-turut) tanpa jeda — berisiko
        # weight drift tak terkendali di profiles/weights.py karena jenis
        # parameter ini juga tidak dilacak di parameter_history untuk
        # auto-revert (lihat _revert_suggestion: weight_/disable_regime_
        # hanya bisa direvert manual).
        # Sekarang: cooldown dicek di awal, berlaku untuk SEMUA jenis
        # parameter termasuk weight_/disable_regime_.
        cooling_key = self._cooling_key(sug.symbol, sug.profile, sug.parameter_name)
        last_apply = self._last_apply_at.get(cooling_key)
        if last_apply and (_utcnow() - last_apply).total_seconds() < 3600:
            return False, "Cooldown apply 1 jam aktif untuk parameter ini."

        if sug.parameter_name.startswith("weight_"):
            indicator = sug.parameter_name.replace("weight_", "")
            ok, msg = _apply_weight_change(
                profile=sug.profile or "",
                indicator=indicator,
                delta=-_WEIGHT_STEP,
            )
            if ok:
                self._last_apply_at[cooling_key] = _utcnow()
            return ok, msg
        if sug.parameter_name.startswith("disable_regime_"):
            return True, (
                f"Advisory-only: edit allowed_regimes di profiles/thresholds.py "
                f"untuk menghapus '{sug.parameter_name.replace('disable_regime_', '')}'."
            )

        perf_before = await self._snapshot_current_performance(sug.symbol, sug.profile)

        if not self._is_delta_safe(sug):
            return False, "Perubahan parameter terlalu agresif untuk guardrail."

        res = apply_parameter_override(
            symbol=sug.symbol or "",
            profile_name=sug.profile or "",
            parameter_name=sug.parameter_name,
            new_value=sug.suggested_value,
            source=f"meta_learner_{approved_by}",
            db_manager=self._db,
        )
        if hasattr(res, "__await__"):
            res = await res
        ok, msg = res if isinstance(res, tuple) and len(res) == 2 else (False, "apply_parameter_override invalid result")

        if ok:
            self._last_apply_at[cooling_key] = _utcnow()
            history_id = await self._db.save_parameter_change(
                symbol=sug.symbol or "",
                profile=sug.profile or "",
                parameter_name=sug.parameter_name,
                old_value=sug.current_value,
                new_value=sug.suggested_value,
                reason=sug.reasoning[:500],
                approved_by=approved_by,
                performance_before=perf_before,
            )
            sug.supporting_data["history_record_id"] = history_id

        return ok, msg

    def _is_delta_safe(self, sug: ParameterSuggestion) -> bool:
        if isinstance(sug.current_value, (int, float)) and isinstance(sug.suggested_value, (int, float)):
            cur = float(sug.current_value)
            nxt = float(sug.suggested_value)
            if abs(nxt - cur) > float(self._max_change):
                return False
            if cur != 0 and abs((nxt - cur) / cur) > 0.25:
                return False
        if sug.confidence is not None and float(sug.confidence) > 0 and float(sug.confidence) < 0.35:
            return False
        return True

    async def _auto_apply_eligible(
        self,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
    ) -> int:
        applied = 0
        cutoff  = _utcnow() - timedelta(hours=self._approval_window_h)

        for sid, sug in list(self._pending.items()):
            if sug.status != SuggestionStatus.PENDING:
                continue
            if symbol  and sug.symbol  != symbol:
                continue
            if profile and sug.profile != profile:
                continue
            if sug.created_at > cutoff:
                continue

            ok, msg = await self.approve_suggestion(sid, approved_by="autonomous")
            if ok:
                applied += 1
                log.info(
                    "Auto-applied suggestion: %s | %s",
                    sid[:8], msg,
                )
            else:
                log.warning("Auto-apply failed: %s | %s", sid[:8], msg)

        if applied:
            log.info(
                "Autonomous mode: %d suggestion(s) auto-applied "
                "(window=%dh).",
                applied, self._approval_window_h,
            )
        return applied

    async def _revert_suggestion(
        self,
        sug:    ParameterSuggestion,
        reason: str,
    ) -> Tuple[bool, str]:
        from profiles.registry import revert_parameter_override

        if sug.parameter_name.startswith(("weight_", "disable_regime_")):
            log.warning(
                "Revert tidak bisa dilakukan otomatis untuk %s — "
                "lakukan manual di profiles/*.py",
                sug.parameter_name,
            )
            sug.status = SuggestionStatus.REVERTED
            return True, "Manual revert diperlukan"

        ok, msg = await revert_parameter_override(
            symbol=sug.symbol or "",
            parameter_name=sug.parameter_name,
            db_manager=self._db,
        )

        if ok:
            sug.status       = SuggestionStatus.REVERTED
            sug.revert_reason = reason
            await self._update_suggestion_status(sug)

            key = self._cooling_key(sug.symbol, sug.profile, sug.parameter_name)
            self._cooling_cache.pop(key, None)

            log.info(
                "Suggestion REVERTED: %s | reason: %s",
                sug.suggestion_id[:8], reason,
            )

        return ok, msg

    async def _evaluate_outcome(
        self,
        record: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        record_id  = record["id"]
        sym        = record.get("symbol", "")
        prof       = record.get("profile", "")
        param      = record.get("parameter_name", "")
        perf_before = record.get("performance_before")
        applied_at  = record.get("timestamp")

        if perf_before is None:
            return None

        # [BUG-FIX] Sebelumnya get_trade_stats() dipanggil tanpa filter waktu
        # sama sekali — hanya "N trade terakhir" apapun kapan terjadinya.
        # Kalau belum ada cukup trade BARU sejak parameter diubah, stats bisa
        # didominasi trade dari SEBELUM perubahan (termasuk trade yang justru
        # jadi alasan suggestion ini dibuat) — sehingga wr_before/wr_after
        # dibandingkan dengan data yang sebagian tumpang tindih, membuat
        # keputusan auto-revert bisa salah. Ini adalah isu yang sudah dicatat
        # di database.py get_pending_outcomes() untuk dicek ulang di sini.
        # Sekarang: pakai param `since=applied_at` (baru ditambahkan di
        # database.get_trade_stats) supaya hanya trade SETELAH parameter
        # diubah yang dihitung.
        if isinstance(applied_at, str):
            try:
                applied_at = datetime.fromisoformat(applied_at)
            except ValueError:
                applied_at = None

        stats = await self._db.get_trade_stats(
            symbol=sym or None,
            profile=prof or None,
            limit=self._track_trades,
            since=applied_at,
        )

        if not stats or stats["total_trades"] < self._track_trades // 2:
            return None

        wr_before = perf_before.get("win_rate", 0.0) * 100
        wr_after  = stats["win_rate"] * 100
        wr_change = wr_after - wr_before

        if wr_change >= 2.0:
            outcome = "improved"
        elif wr_change <= -self._revert_drop:
            outcome = "degraded"
        else:
            outcome = "neutral"

        perf_after = {
            "win_rate":      stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "total_trades":  stats["total_trades"],
        }

        await self._db.update_parameter_outcome(
            record_id=record_id,
            performance_after=perf_after,
            trades_after_apply=stats["total_trades"],
            outcome=outcome,
        )

        log.info(
            "Outcome evaluated: %s/%s %s | wr_before=%.1f%% wr_after=%.1f%% "
            "wr_change=%+.1f%% | outcome=%s",
            sym, prof, param,
            wr_before, wr_after, wr_change, outcome,
        )

        if outcome == "degraded":
            matching_sug = None
            for sug in self._pending.values():
                if (sug.symbol == sym
                        and sug.profile == prof
                        and sug.parameter_name == param
                        and sug.status == SuggestionStatus.APPLIED):
                    matching_sug = sug
                    break

            if matching_sug is None:
                # [BUG-FIX] Sebelumnya kalau matching_sug tidak ketemu di
                # self._pending (in-memory), auto-revert langsung SILENT
                # SKIP — tidak ada fallback. self._pending kosong setiap kali
                # MetaLearner di-restart (restart proses, deploy, crash),
                # sehingga suggestion yang di-APPLY sebelum restart tidak
                # pernah bisa di-auto-revert lagi walau outcome-nya degraded,
                # padahal semua data yang dibutuhkan (symbol, profile,
                # parameter_name, old_value/new_value, db_record_id) sudah
                # lengkap di record parameter_history ini.
                # Sekarang: rekonstruksi ParameterSuggestion langsung dari
                # record DB sebagai fallback supaya revert tetap jalan.
                matching_sug = ParameterSuggestion(
                    symbol=sym,
                    profile=prof,
                    parameter_name=param,
                    current_value=record.get("old_value"),
                    suggested_value=record.get("new_value"),
                    status=SuggestionStatus.APPLIED,
                )
                matching_sug.supporting_data["db_record_id"] = record_id
                log.info(
                    "AUTO-REVERT: suggestion %s/%s %s tidak ada di memori "
                    "(kemungkinan proses baru saja restart) — direkonstruksi "
                    "dari parameter_history id=%s.",
                    sym, prof, param, record_id,
                )

            if matching_sug:
                revert_reason = (
                    f"Win rate turun {abs(wr_change):.1f}pp setelah {stats['total_trades']} "
                    f"trade ({wr_before:.1f}% → {wr_after:.1f}%). "
                    f"Auto-revert triggered (threshold drop={self._revert_drop}pp)."
                )
                ok, msg = await self._revert_suggestion(matching_sug, revert_reason)
                log.warning(
                    "AUTO-REVERT %s/%s %s: %s",
                    sym, prof, param, msg,
                )

        return {
            "record_id":    record_id,
            "symbol":       sym,
            "profile":      prof,
            "parameter":    param,
            "wr_before":    round(wr_before, 2),
            "wr_after":     round(wr_after, 2),
            "wr_change":    round(wr_change, 2),
            "outcome":      outcome,
        }

    def _find_indicator_eff(
        self,
        report: AttributionReport,
        name:   str,
    ) -> Optional[IndicatorEffectiveness]:
        return next(
            (e for e in report.indicator_effectiveness if e.indicator_name == name),
            None,
        )

    def _calc_confidence(
        self,
        sample_size:   int,
        win_rate_diff: float,
        correlation:   float,
    ) -> float:

        sample_factor = min(1.0, sample_size / (self._min_sample * 3))

        wr_factor = min(1.0, win_rate_diff / 30.0)

        corr_factor = min(1.0, abs(correlation) * 2.0)

        confidence = (
            sample_factor * 0.4
            + wr_factor   * 0.35
            + corr_factor * 0.25
        )
        return round(min(0.95, max(0.10, confidence)), 3)

    def _estimate_filtered_trades(
        self,
        report:           AttributionReport,
        current_threshold: float,
        new_threshold:     float,
        is_win:            bool,
    ) -> int:
        total     = report.total_trades
        wr        = report.overall_win_rate / 100
        wins      = round(total * wr)
        losses    = total - wins

        total_eff = self._find_indicator_eff(report, "total")
        if total_eff is None:
            return 0

        step = abs(new_threshold - current_threshold)
        if is_win:
            return max(0, round(wins * step * 0.01))
        else:
            return max(0, round(losses * step * 0.025))

    async def _get_current_threshold(
        self,
        profile: str,
    ) -> Optional[float]:
        try:
            from profiles.thresholds import get_dynamic_threshold
            # Gunakan regime undefined sebagai base threshold untuk meta learner
            return get_dynamic_threshold(profile, "undefined")
        except Exception as e:
            # [BUG-FIX] Sebelumnya "except (KeyError, Exception)" — redundan
            # karena KeyError sudah subclass dari Exception, tidak berefek
            # apa pun selain membingungkan pembaca kode.
            log.debug("_get_current_threshold: %s", e)
            return None

    async def _get_current_param(
        self,
        profile:    str,
        param_name: str,
    ) -> Optional[float]:
        try:
            from profiles.thresholds import get_profile_thresholds
            thresh = get_profile_thresholds(profile)
            val = getattr(thresh, param_name, None)
            return float(val) if val is not None else None
        except Exception as e:
            log.debug("_get_current_param %s/%s: %s", profile, param_name, e)
            return None

    async def _snapshot_current_performance(
        self,
        symbol:  str,
        profile: str,
    ) -> Dict[str, Any]:
        get_stats = getattr(self._db, "get_trade_stats", None)
        if not callable(get_stats):
            return {"win_rate": 0.0, "profit_factor": 0.0, "total_trades": 0}

        try:
            stats = get_stats(
                symbol=symbol or None,
                profile=profile or None,
                limit=self._track_trades,
            )
            # Support both async and sync mocks.
            if hasattr(stats, "__await__"):
                stats = await stats
        except Exception:
            return {"win_rate": 0.0, "profit_factor": 0.0, "total_trades": 0}

        if stats:
            return {
                "win_rate":      stats.get("win_rate", 0.0),
                "profit_factor": stats.get("profit_factor", 0.0),
                "total_trades":  stats.get("total_trades", 0),
                "snapped_at":    _utcnow().isoformat(),
            }
        return {"win_rate": 0.0, "profit_factor": 0.0, "total_trades": 0}

    def _cooling_key(
        self,
        symbol:  str,
        profile: str,
        param:   str,
    ) -> str:
        return f"{symbol or '_'}:{profile or '_'}:{param}"

    def _set_cooling(
        self,
        symbol:  str,
        profile: str,
        param:   str,
    ) -> None:
        key    = self._cooling_key(symbol, profile, param)
        until  = _utcnow() + timedelta(days=self._cooling_days)
        self._cooling_cache[key] = until
        log.debug("Cooling-off set: %s until %s", key, until.strftime("%Y-%m-%d"))

    def _is_cooling(
        self,
        symbol:  str,
        profile: str,
        param:   str,
    ) -> bool:
        key   = self._cooling_key(symbol, profile, param)
        until = self._cooling_cache.get(key)
        if until is None:
            return False
        if _utcnow() < until:
            remaining = (until - _utcnow()).days
            log.debug(
                "Cooling-off aktif: %s | %d hari tersisa",
                key, remaining,
            )
            return True
        del self._cooling_cache[key]
        return False

    async def _load_cooling_from_db(self) -> None:
        try:
            history = await self._db.get_parameter_history(limit=500)
            now     = _utcnow()
            for rec in history:
                ts      = rec.get("timestamp")
                sym     = rec.get("symbol", "")
                prof    = rec.get("profile", "")
                param   = rec.get("parameter_name", "")
                if ts is None or param.startswith("_"):
                    continue
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                cooling_until = ts + timedelta(days=self._cooling_days)
                if cooling_until > now:
                    key = self._cooling_key(sym, prof, param)
                    if key not in self._cooling_cache:
                        self._cooling_cache[key] = cooling_until
                        log.debug("Cooling restored from DB: %s", key)
        except Exception as e:
            log.warning("_load_cooling_from_db error (non-critical): %s", e)

    async def _save_suggestion(self, sug: ParameterSuggestion) -> None:
        try:
            row_id = await self._db.save_parameter_change(
                symbol=sug.symbol or "",
                profile=sug.profile or "",
                parameter_name=sug.parameter_name,
                old_value=sug.current_value,
                new_value=sug.suggested_value,
                reason=f"[SUGGESTION:{sug.suggestion_id}] {sug.reasoning[:400]}",
                approved_by="pending",
                performance_before=sug.supporting_data,
            )
            if row_id:
                sug.supporting_data["db_record_id"] = row_id
        except Exception as e:
            log.error("_save_suggestion error: %s", e)

    async def _update_suggestion_status(self, sug: ParameterSuggestion) -> None:
        record_id = sug.supporting_data.get("db_record_id")
        if record_id is None:
            return
        try:
            status_map = {
                SuggestionStatus.APPLIED:  "applied",
                SuggestionStatus.REJECTED: "rejected",
                SuggestionStatus.REVERTED: "reverted",
                SuggestionStatus.EXPIRED:  "expired",
            }
            outcome = status_map.get(sug.status, "pending")
            await self._db.update_parameter_outcome(
                record_id=record_id,
                performance_after={},
                trades_after_apply=0,
                outcome=outcome,
            )
        except Exception as e:
            log.debug("_update_suggestion_status non-critical: %s", e)

        # Best-effort compatibility with DB APIs that store suggestions separately.
        try:
            upd = getattr(self._db, "update_suggestion_status", None)
            if callable(upd):
                r = upd(
                    suggestion_id=sug.suggestion_id,
                    status=(sug.status.value if hasattr(sug.status, "value") else str(sug.status)),
                )
                if hasattr(r, "__await__"):
                    await r
        except Exception:
            pass

    async def _load_suggestion(
        self,
        suggestion_id: str,
    ) -> Optional[ParameterSuggestion]:
        if suggestion_id in self._pending:
            return self._pending[suggestion_id]

        # Preferred direct lookup (DB API used by the dashboard/telegram flows).
        try:
            get_by_id = getattr(self._db, "get_suggestion_by_id", None)
            if callable(get_by_id):
                rec = await get_by_id(suggestion_id)
                if rec:
                    r = rec if isinstance(rec, dict) else getattr(rec, "__dict__", {}) or {}
                    symbol  = r.get("symbol") or getattr(rec, "symbol", "") or ""
                    profile = r.get("profile") or getattr(rec, "profile", "") or ""
                    pname   = r.get("parameter_name") or getattr(rec, "parameter_name", "") or ""
                    old_v   = r.get("old_value")
                    if old_v is None:
                        old_v = r.get("current_value")
                    if old_v is None:
                        old_v = getattr(rec, "old_value", None)
                    if old_v is None:
                        old_v = getattr(rec, "current_value", None)

                    new_v = r.get("new_value")
                    if new_v is None:
                        new_v = r.get("suggested_value")
                    if new_v is None:
                        new_v = getattr(rec, "new_value", None)
                    if new_v is None:
                        new_v = getattr(rec, "suggested_value", None)

                    raw_status = r.get("status") or getattr(rec, "status", None)
                    status = SuggestionStatus.PENDING
                    try:
                        if isinstance(raw_status, SuggestionStatus):
                            status = raw_status
                        elif isinstance(raw_status, str):
                            status = SuggestionStatus(raw_status)
                    except Exception:
                        status = SuggestionStatus.PENDING

                    sug = ParameterSuggestion(
                        suggestion_id=suggestion_id,
                        symbol=symbol,
                        profile=profile,
                        parameter_name=pname,
                        current_value=old_v,
                        suggested_value=new_v,
                        reasoning=str(r.get("reason") or getattr(rec, "reason", "") or ""),
                        status=status,
                    )

                    rid = r.get("id") or getattr(rec, "id", None)
                    if rid is not None:
                        sug.supporting_data["db_record_id"] = rid

                    self._pending[suggestion_id] = sug
                    return sug
        except Exception as e:
            log.debug("_load_suggestion direct lookup error: %s", e)

        try:
            history = await self._db.get_parameter_history(limit=500)
            for rec in history:
                reason = rec.get("reason", "")
                if f"[SUGGESTION:{suggestion_id}]" in (reason or ""):
                    sug = ParameterSuggestion(
                        suggestion_id=suggestion_id,
                        symbol=rec.get("symbol", ""),
                        profile=rec.get("profile", ""),
                        parameter_name=rec.get("parameter_name", ""),
                        current_value=rec.get("old_value"),
                        suggested_value=rec.get("new_value"),
                        reasoning=reason,
                        status=SuggestionStatus.PENDING,
                    )
                    sug.supporting_data["db_record_id"] = rec.get("id")
                    self._pending[suggestion_id] = sug
                    return sug
        except Exception as e:
            log.debug("_load_suggestion DB lookup error: %s", e)

        return None

    async def initialize(self) -> None:
        await self._load_cooling_from_db()
        log.info(
            "MetaLearner initialized: mode=%s | %d cooling-off entries loaded",
            self._mode, len(self._cooling_cache),
        )

    def format_suggestions_telegram(
        self,
        suggestions: List[ParameterSuggestion],
    ) -> str:
        if not suggestions:
            return "📭 Tidak ada parameter suggestion yang pending."

        lines = [f"🧠 Meta Learner Suggestions ({len(suggestions)}):"]
        for sug in suggestions[:5]:
            lines.append(sug.to_telegram_summary())
            lines.append("")

        if len(suggestions) > 5:
            lines.append(f"... dan {len(suggestions) - 5} suggestion lainnya.")
        lines.append("Gunakan /approve <id> atau /reject <id> untuk merespons.")
        return "\n".join(lines)

    def format_suggestion_detail_telegram(
        self,
        sug: ParameterSuggestion,
    ) -> str:
        status_map = {
            SuggestionStatus.PENDING:  "⏳ Pending",
            SuggestionStatus.APPROVED: "✅ Approved",
            SuggestionStatus.REJECTED: "❌ Rejected",
            SuggestionStatus.APPLIED:  "🔧 Applied",
            SuggestionStatus.REVERTED: "↩️ Reverted",
            SuggestionStatus.EXPIRED:  "⌛ Expired",
        }

        lines = [
            f"📋 Suggestion Detail",
            f"ID: `{sug.suggestion_id[:8]}`",
            f"Status: {status_map.get(sug.status, '❓')}",
            f"Target: {sug.symbol or 'ALL'} / {sug.profile or 'ALL'}",
            f"Parameter: `{sug.parameter_name}`",
            f"Perubahan: {sug.current_value} → {sug.suggested_value}",
            f"Confidence: {sug.confidence:.1%}",
            f"Projected improvement: +{sug.projected_improvement:.1f}%",
            f"",
            f"Alasan:",
            sug.reasoning[:400],
        ]

        if sug.status == SuggestionStatus.PENDING:
            lines += [
                "",
                f"Ketik `/approve {sug.suggestion_id[:8]}` untuk apply",
                f"Ketik `/reject {sug.suggestion_id[:8]}` untuk tolak",
            ]

        return "\n".join(lines)

    async def get_peer_coin_performance(
        self,
        lookback_days: int = 30,
    ) -> List[Dict]:
        """
        Ambil performa coin di algotrader_test yang berpotensi
        layak dipindahkan ke algotrader (untuk coin_swap engine).
        """
        import os
        if not _CROSS_LEARN_AVAILABLE:
            return []
        if os.getenv("CROSS_LEARN_ENABLED", "false").lower() != "true":
            return []

        try:
            reader = get_cross_learn_reader()
            peer_scores = reader.get_peer_signal_scores(
                lookback_days=lookback_days,
                only_triggered=True,
            )
            peer_trades = reader.get_peer_trades(lookback_days=lookback_days)

            # Rangkum per coin
            coin_stats: Dict[str, Dict] = {}
            for t in peer_trades:
                sym = t.get("symbol", "")
                if not sym:
                    continue
                if sym not in coin_stats:
                    coin_stats[sym] = {
                        "symbol":   sym,
                        "profile":  t.get("strategy_profile", ""),
                        "trades":   0,
                        "wins":     0,
                        "pnl_sum":  0.0,
                    }
                coin_stats[sym]["trades"] += 1
                pnl = float(t.get("realized_pnl_pct") or 0.0)
                if pnl > 0:
                    coin_stats[sym]["wins"] += 1
                coin_stats[sym]["pnl_sum"] += pnl

            result = []
            for sym, s in coin_stats.items():
                total = s["trades"]
                wr    = s["wins"] / total * 100 if total > 0 else 0.0
                result.append({
                    "symbol":       sym,
                    "profile":      s["profile"],
                    "total_trades": total,
                    "win_rate":     round(wr, 1),
                    "avg_pnl_pct":  round(s["pnl_sum"] / total, 2) if total > 0 else 0.0,
                    "source":       "peer_algotrader_test",
                })

            result.sort(key=lambda x: x["win_rate"], reverse=True)
            log.info(
                "get_peer_coin_performance: %d coin dari peer (lookback=%dd)",
                len(result), lookback_days,
            )
            return result

        except Exception as e:
            log.error("get_peer_coin_performance error: %s", e)
            return []

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)