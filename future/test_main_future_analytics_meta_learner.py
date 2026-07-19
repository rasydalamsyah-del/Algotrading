"""
future/test_main_future_analytics_meta_learner.py -- Test untuk wiring
PerformanceAnalytics/MetaLearner di future/main_future.py (audit item #18).

[LATAR BELAKANG] self._analytics/self._meta_learner SEBELUMNYA SELALU None
di futures -- tidak pernah diinstansiasi di mana pun (dikonfirmasi grep).
Investigasi menemukan 2 hal krusial di luar sekadar "instansiasi objeknya":

1. run_analytics_loop() SUDAH ADA tapi silently broken -- memanggil
   self._analytics.refresh() yang TIDAK PERNAH ADA di PerformanceAnalytics
   (nama asli: refresh_snapshots()). Guard hasattr(...,"refresh") selalu
   False -- loop jalan selamanya tanpa efek apa pun, tanpa error.

2. [TEMUAN PALING PENTING] MetaLearner._apply_suggestion() punya 2 jalur
   apply: (a) parameter threshold (entry_threshold, dst) -> per-bot, aman
   (in-memory _ACTIVE_OVERRIDES + persist ke DB bot itu sendiri), (b)
   weight kategori indikator (weight_trend, dst) -> _apply_weight_change()
   MENULIS LANGSUNG ke engine/profiles/weights.py, file YANG SAMA dipakai
   BERSAMA oleh proses spot maupun futures (LEVEL1_WEIGHTS tanpa namespace
   market-type). Kalau MetaLearner futures meng-apply suggestion weight_*
   (baik manual approve mode advisory MAUPUN otomatis mode autonomous),
   file itu tertimpa berdasar hasil trading futures SAJA, lalu diam-diam
   ikut terbawa ke scoring live SPOT saat spot restart berikutnya.
   Diputuskan (bukan didokumentasikan saja): MetaLearner sekarang punya
   parameter market_type ("spot" default, tidak mengubah perilaku spot
   existing) -- kalau != "spot", _apply_suggestion() MEMBLOKIR suggestion
   weight_* di titik dispatch-nya (dipakai baik approve_suggestion()
   manual maupun _auto_apply_eligible() otonom), return (False, alasan),
   TANPA pernah memanggil _apply_weight_change() sama sekali.

    python3 -m unittest future.test_main_future_analytics_meta_learner -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from engine.learning.meta_learner import MetaLearner
from engine.core.models import ParameterSuggestion, SuggestionStatus
from future.main_future import TradingBot


def _make_pending_suggestion(parameter_name: str, **overrides) -> ParameterSuggestion:
    defaults = dict(
        status=SuggestionStatus.PENDING,
        symbol="BTC/USDT", profile="scalp_volatile",
        parameter_name=parameter_name,
        current_value=1.0, suggested_value=1.5,
    )
    defaults.update(overrides)
    return ParameterSuggestion(**defaults)


def _build_meta_learner(market_type="spot", db=None):
    fake_db = db or AsyncMock()
    fake_db.get_parameter_history = AsyncMock(return_value=[])
    analytics = AsyncMock()
    return MetaLearner(
        db_manager=fake_db,
        analytics_engine=analytics,
        mode="advisory",
        market_type=market_type,
    )


class TestMetaLearnerWeightGuard(unittest.TestCase):
    """[Regresi kunci -- audit item #18] Suggestion tipe weight_* HARUS
    diblokir teknis untuk market_type != "spot", TIDAK boleh pernah
    memanggil _apply_weight_change() (yang menulis engine/profiles/
    weights.py)."""

    def test_futures_weight_suggestion_rejected_and_file_never_touched(self):
        ml  = _build_meta_learner(market_type="futures")
        sug = _make_pending_suggestion("weight_trend")
        ml._pending[sug.suggestion_id] = sug
        ml._load_suggestion  = AsyncMock(return_value=sug)
        ml._update_suggestion_status = AsyncMock()

        with patch("engine.learning.meta_learner._apply_weight_change") as mock_apply:
            ok, msg = asyncio_run(ml.approve_suggestion(sug.suggestion_id, approved_by="manual_api"))

        self.assertFalse(ok, "suggestion weight_* HARUS ditolak untuk futures")
        mock_apply.assert_not_called()
        self.assertIn("weight_", msg.lower() if "weight_" in msg else msg)

    def test_spot_weight_suggestion_still_works_unchanged(self):
        """[Non-regresi] market_type="spot" (default, perilaku existing)
        TIDAK boleh berubah -- _apply_weight_change() tetap dipanggil."""
        ml  = _build_meta_learner(market_type="spot")
        sug = _make_pending_suggestion("weight_trend")
        ml._pending[sug.suggestion_id] = sug
        ml._load_suggestion  = AsyncMock(return_value=sug)
        ml._update_suggestion_status = AsyncMock()

        with patch("engine.learning.meta_learner._apply_weight_change", return_value=(True, "ok")) as mock_apply:
            ok, msg = asyncio_run(ml.approve_suggestion(sug.suggestion_id, approved_by="manual_api"))

        self.assertTrue(ok)
        mock_apply.assert_called_once()

    def test_default_market_type_is_spot_backward_compatible(self):
        """Caller lama (spot/main_spot.py, simulate_test.py) yang tidak
        pernah kirim market_type= HARUS tetap dapat perilaku lama persis."""
        ml = MetaLearner(db_manager=AsyncMock(), analytics_engine=AsyncMock())
        self.assertEqual(ml._market_type, "spot")

    def test_futures_threshold_suggestion_still_applies_normally(self):
        """[Regresi kunci] Suggestion NON-weight (threshold, dst) HARUS
        tetap jalan normal untuk futures -- guard hanya menyasar weight_*,
        bukan seluruh MetaLearner."""
        ml  = _build_meta_learner(market_type="futures")
        sug = _make_pending_suggestion(
            "entry_threshold", current_value=60.0, suggested_value=58.0,
        )
        ml._pending[sug.suggestion_id] = sug
        ml._load_suggestion  = AsyncMock(return_value=sug)
        ml._update_suggestion_status = AsyncMock()
        ml._snapshot_current_performance = AsyncMock(return_value={})
        ml._is_delta_safe = lambda s: True
        ml._db.save_parameter_change = AsyncMock(return_value=1)

        with patch(
            "engine.profiles.registry.apply_parameter_override",
            return_value=(True, "applied"),
        ) as mock_apply:
            ok, msg = asyncio_run(ml.approve_suggestion(sug.suggestion_id, approved_by="manual_api"))

        self.assertTrue(ok, msg)
        mock_apply.assert_called_once()

    def test_disable_regime_suggestion_unaffected_by_guard(self):
        """disable_regime_* itu advisory-only (tidak pernah menulis file
        apa pun) -- pastikan guard weight_* tidak sengaja menangkap ini."""
        ml  = _build_meta_learner(market_type="futures")
        sug = _make_pending_suggestion("disable_regime_choppy")
        ml._pending[sug.suggestion_id] = sug
        ml._load_suggestion  = AsyncMock(return_value=sug)
        ml._update_suggestion_status = AsyncMock()

        ok, msg = asyncio_run(ml.approve_suggestion(sug.suggestion_id, approved_by="manual_api"))
        self.assertTrue(ok)

    def test_autonomous_mode_auto_apply_also_blocked_for_weight(self):
        """[Regresi kunci] Guard HARUS menutup jalur mode autonomous juga
        (_auto_apply_eligible -> approve_suggestion -> _apply_suggestion),
        bukan cuma approval manual."""
        from datetime import timedelta
        ml  = _build_meta_learner(market_type="futures")
        ml._mode = "autonomous"
        sug = _make_pending_suggestion("weight_momentum")
        sug.created_at = sug.created_at - timedelta(hours=100)  # lewat approval_window
        ml._pending[sug.suggestion_id] = sug
        ml._load_suggestion  = AsyncMock(return_value=sug)
        ml._update_suggestion_status = AsyncMock()

        with patch("engine.learning.meta_learner._apply_weight_change") as mock_apply:
            applied_count = asyncio_run(ml._auto_apply_eligible())

        self.assertEqual(applied_count, 0)
        mock_apply.assert_not_called()


class TestFuturesConfigDefaults(unittest.TestCase):
    """[Wiring -- audit item #18] Config key baru harus pakai env var
    TERPISAH dari spot (konsisten pola DASHBOARD_API_KEY_FUTURES dkk),
    default AMAN (sama persis dgn default spot)."""

    def test_meta_learner_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("META_LEARNER_ENABLED_FUTURES", "META_LEARNER_MODE_FUTURES"):
                os.environ.pop(k, None)
            bot = TradingBot()
        self.assertFalse(bot.config["meta_learner_enabled"])
        self.assertEqual(bot.config["meta_learner_mode"], "advisory")

    def test_meta_learner_env_var_is_futures_specific_not_shared_with_spot(self):
        with patch.dict(os.environ, {"META_LEARNER_ENABLED_FUTURES": "true",
                                      "META_LEARNER_ENABLED": "false"}):
            bot = TradingBot()
        self.assertTrue(
            bot.config["meta_learner_enabled"],
            "harus baca META_LEARNER_ENABLED_FUTURES, bukan META_LEARNER_ENABLED milik spot",
        )

    def test_analytics_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANALYTICS_ENABLED", None)
            bot = TradingBot()
        self.assertTrue(bot.config["analytics_enabled"])


class TestInitializeIntelligencePipelineWiring(unittest.TestCase):
    """self._analytics/self._meta_learner SEBELUMNYA SELALU None -- test
    ini membuktikan _initialize_intelligence_pipeline() sekarang genuinely
    menginstansiasi keduanya (bukan cuma menambah kode yang tidak
    tereksekusi)."""

    def _fake_db(self):
        db = AsyncMock()
        db.get_parameter_history = AsyncMock(return_value=[])
        db.save_log = AsyncMock()
        return db

    def test_analytics_instantiated_when_enabled_default(self):
        bot = TradingBot()
        bot.db = self._fake_db()
        asyncio_run(bot._initialize_intelligence_pipeline())
        self.assertIsNotNone(bot._analytics)

    def test_meta_learner_stays_none_when_disabled_default(self):
        bot = TradingBot()
        bot.db = self._fake_db()
        bot.config["meta_learner_enabled"] = False
        asyncio_run(bot._initialize_intelligence_pipeline())
        self.assertIsNone(bot._meta_learner)

    def test_meta_learner_instantiated_with_futures_market_type_when_enabled(self):
        bot = TradingBot()
        bot.db = self._fake_db()
        bot.config["meta_learner_enabled"] = True
        asyncio_run(bot._initialize_intelligence_pipeline())
        self.assertIsNotNone(bot._meta_learner)
        self.assertEqual(
            bot._meta_learner._market_type, "futures",
            "MetaLearner futures HARUS diinstansiasi dgn market_type='futures' -- "
            "kalau tidak, guard weight_* di _apply_suggestion() tidak akan aktif.",
        )


class TestRunAnalyticsLoopCallsRealMethod(unittest.TestCase):
    """[Regresi kunci -- bug ditemukan saat investigasi] Sebelumnya loop
    memanggil self._analytics.refresh() yang tidak pernah ada -- silent
    no-op selamanya. Sekarang harus genuinely memanggil refresh_snapshots()
    (method yang benar-benar ada) dan meta_learner.run_full_cycle()."""

    def test_calls_refresh_snapshots_not_nonexistent_refresh(self):
        bot = TradingBot()
        bot.is_running = True
        bot.config["analytics_refresh_interval"] = 0  # jangan nunggu di test
        bot.db = AsyncMock()
        bot._analytics = AsyncMock()
        bot._meta_learner = None

        call_count = {"n": 0}

        async def _stop_after_one_iteration(*a, **kw):
            call_count["n"] += 1
            bot.is_running = False

        bot._analytics.refresh_snapshots = AsyncMock(side_effect=_stop_after_one_iteration)

        asyncio_run(bot.run_analytics_loop())

        bot._analytics.refresh_snapshots.assert_awaited()
        self.assertFalse(hasattr(bot._analytics, "refresh") and call_count["n"] == 0)

    def test_calls_meta_learner_run_full_cycle_when_active(self):
        bot = TradingBot()
        bot.is_running = True
        bot.config["analytics_refresh_interval"] = 0
        bot.db = AsyncMock()
        bot._analytics = AsyncMock()

        async def _stop(*a, **kw):
            bot.is_running = False

        bot._analytics.refresh_snapshots = AsyncMock(side_effect=_stop)
        bot._meta_learner = AsyncMock()
        bot._meta_learner.run_full_cycle = AsyncMock(return_value=[])

        asyncio_run(bot.run_analytics_loop())

        bot._meta_learner.run_full_cycle.assert_awaited()

    def test_noop_when_analytics_not_active(self):
        bot = TradingBot()
        bot._analytics = None
        asyncio_run(bot.run_analytics_loop())  # tidak boleh hang/error


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
