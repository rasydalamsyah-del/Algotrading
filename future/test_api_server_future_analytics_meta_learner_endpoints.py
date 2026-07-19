"""
future/test_api_server_future_analytics_meta_learner_endpoints.py -- Test
untuk endpoint analytics/*/meta_learner/* di future/api_server_future.py
(audit item #18, langkah terakhir -- endpoint di atas wiring PerformanceAnalytics/
MetaLearner yang sudah diverifikasi terpisah di
future/test_main_future_analytics_meta_learner.py).

9 endpoint, straight port dari spot/api_server_spot.py (data source
engine/database.py sudah diverifikasi generik/market-agnostic, engine/
learning/analytics.py & meta_learner.py nihil bias long-only):

  GET  /api/analytics/attribution
  GET  /api/analytics/indicator_effectiveness
  GET  /api/analytics/regime_performance
  GET  /api/analytics/attribution_by_profile
  POST /api/analytics/refresh
  GET  /api/meta_learner/suggestions
  POST /api/meta_learner/approve/{suggestion_id}
  POST /api/meta_learner/reject/{suggestion_id}
  GET  /api/meta_learner/history

Guard shared-file weights.py (MetaLearner market_type="futures") DIUJI
terpisah di test_main_future_analytics_meta_learner.py pada level
MetaLearner langsung -- file ini HANYA menguji endpoint HTTP-nya (routing,
auth, 503 saat belum diinisialisasi, passthrough hasil apa adanya).

    python3 -m unittest future.test_api_server_future_analytics_meta_learner_endpoints -v
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DASHBOARD_API_KEY_FUTURES", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from future.api_server_future import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _build_fake_bot(with_analytics=True, with_meta_learner=True):
    analytics = None
    if with_analytics:
        analytics = SimpleNamespace(
            compute_attribution=AsyncMock(return_value={"total_trades": 10}),
            compute_indicator_effectiveness=AsyncMock(return_value={"rsi": 0.5}),
            compute_all_profiles=AsyncMock(return_value={"scalp_volatile": {}}),
            run_full_analysis=AsyncMock(return_value=None),
        )

    meta_learner = None
    if with_meta_learner:
        meta_learner = SimpleNamespace(
            approve_suggestion=AsyncMock(return_value=(True, "applied")),
            reject_suggestion=AsyncMock(return_value=(True, "rejected")),
        )

    db = SimpleNamespace(
        get_pending_suggestions=AsyncMock(return_value=[{
            "id": 1, "timestamp": None, "symbol": "BTC/USDT", "profile": "scalp_volatile",
            "parameter_name": "entry_threshold", "old_value": 60.0, "new_value": 58.0,
            "reason": "win rate rendah", "confidence": 0.7, "projected_improvement": 2.5,
            "status": "pending",
        }]),
        get_parameter_history=AsyncMock(return_value=[{
            "id": 1, "timestamp": None, "symbol": "BTC/USDT", "profile": "scalp_volatile",
            "parameter_name": "entry_threshold", "old_value": 60.0, "new_value": 58.0,
            "reason": "win rate rendah", "approved_by": "manual_api",
            "outcome": "applied", "trades_after_apply": 5,
        }]),
    )

    return SimpleNamespace(analytics=analytics, meta_learner=meta_learner, db=db)


def _client(bot):
    return TestClient(create_app(lambda: bot))


class TestAnalyticsAttributionEndpoint(unittest.TestCase):

    def test_returns_200_and_calls_compute_attribution(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/analytics/attribution?lookback_days=14&profile=scalp_volatile", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["lookback_days"], 14)
        self.assertEqual(body["filters"]["profile"], "scalp_volatile")
        bot.analytics.compute_attribution.assert_awaited_once_with(
            lookback_days=14, filters={"profile": "scalp_volatile"},
        )

    def test_503_when_analytics_not_initialized(self):
        bot = _build_fake_bot(with_analytics=False)
        c = _client(bot)
        r = c.get("/api/analytics/attribution", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/analytics/attribution")
        self.assertEqual(r.status_code, 401)


class TestIndicatorEffectivenessEndpoint(unittest.TestCase):

    def test_returns_200(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/analytics/indicator_effectiveness", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.analytics.compute_indicator_effectiveness.assert_awaited_once()

    def test_503_when_analytics_not_initialized(self):
        c = _client(_build_fake_bot(with_analytics=False))
        r = c.get("/api/analytics/indicator_effectiveness", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/analytics/indicator_effectiveness")
        self.assertEqual(r.status_code, 401)


class TestRegimePerformanceEndpoint(unittest.TestCase):

    def test_calls_compute_attribution_with_group_by_regime(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/analytics/regime_performance", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.analytics.compute_attribution.assert_awaited_once_with(
            lookback_days=30, filters={}, group_by="regime",
        )

    def test_503_when_analytics_not_initialized(self):
        c = _client(_build_fake_bot(with_analytics=False))
        r = c.get("/api/analytics/regime_performance", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/analytics/regime_performance")
        self.assertEqual(r.status_code, 401)


class TestAttributionByProfileEndpoint(unittest.TestCase):

    def test_returns_200(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/analytics/attribution_by_profile", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("scalp_volatile", r.json()["profiles"])

    def test_503_when_analytics_not_initialized(self):
        c = _client(_build_fake_bot(with_analytics=False))
        r = c.get("/api/analytics/attribution_by_profile", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/analytics/attribution_by_profile")
        self.assertEqual(r.status_code, 401)


class TestAnalyticsRefreshEndpoint(unittest.TestCase):

    def test_returns_200_and_calls_run_full_analysis(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.post("/api/analytics/refresh", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "refreshed")
        bot.analytics.run_full_analysis.assert_awaited_once()

    def test_503_when_analytics_not_initialized(self):
        c = _client(_build_fake_bot(with_analytics=False))
        r = c.post("/api/analytics/refresh", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.post("/api/analytics/refresh")
        self.assertEqual(r.status_code, 401)


class TestMetaLearnerSuggestionsEndpoint(unittest.TestCase):

    def test_returns_200_with_mapped_fields(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/meta_learner/suggestions", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["suggestions"][0]["parameter_name"], "entry_threshold")

    def test_db_error_returns_200_empty_list_not_500(self):
        bot = _build_fake_bot()
        bot.db.get_pending_suggestions = AsyncMock(side_effect=RuntimeError("db locked"))
        c = _client(bot)
        r = c.get("/api/meta_learner/suggestions", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/meta_learner/suggestions")
        self.assertEqual(r.status_code, 401)


class TestMetaLearnerApproveEndpoint(unittest.TestCase):

    def test_returns_200_passthrough_result(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.post("/api/meta_learner/approve/abc123", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["applied"])
        bot.meta_learner.approve_suggestion.assert_awaited_once_with(
            suggestion_id="abc123", approved_by="manual_api",
        )

    def test_passthrough_rejection_from_weight_guard(self):
        """[Integrasi dgn guard shared-file] Endpoint HARUS meneruskan
        apa adanya applied=False + pesan blokir weight_* -- tidak
        menyembunyikan atau mengubahnya jadi error lain."""
        bot = _build_fake_bot()
        bot.meta_learner.approve_suggestion = AsyncMock(
            return_value=(False, "Suggestion tipe weight_* DIBLOKIR utk market_type='futures'"),
        )
        c = _client(bot)
        r = c.post("/api/meta_learner/approve/weight-sug-1", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["applied"])
        self.assertIn("DIBLOKIR", body["message"])

    def test_503_when_meta_learner_not_initialized(self):
        c = _client(_build_fake_bot(with_meta_learner=False))
        r = c.post("/api/meta_learner/approve/abc123", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.post("/api/meta_learner/approve/abc123")
        self.assertEqual(r.status_code, 401)


class TestMetaLearnerRejectEndpoint(unittest.TestCase):

    def test_returns_200_passthrough_result(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.post("/api/meta_learner/reject/abc123", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["rejected"])

    def test_503_when_meta_learner_not_initialized(self):
        c = _client(_build_fake_bot(with_meta_learner=False))
        r = c.post("/api/meta_learner/reject/abc123", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.post("/api/meta_learner/reject/abc123")
        self.assertEqual(r.status_code, 401)


class TestMetaLearnerHistoryEndpoint(unittest.TestCase):

    def test_returns_200_with_mapped_fields(self):
        bot = _build_fake_bot()
        c = _client(bot)
        r = c.get("/api/meta_learner/history?symbol=BTC/USDT&limit=10", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["history"][0]["outcome"], "applied")

    def test_limit_capped_at_200(self):
        bot = _build_fake_bot()
        c = _client(bot)
        c.get("/api/meta_learner/history?limit=9999", headers=_HEADERS)
        _, kwargs = bot.db.get_parameter_history.await_args
        self.assertEqual(kwargs["limit"], 200)

    def test_db_error_returns_200_empty_list_not_500(self):
        bot = _build_fake_bot()
        bot.db.get_parameter_history = AsyncMock(side_effect=RuntimeError("db locked"))
        c = _client(bot)
        r = c.get("/api/meta_learner/history", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/meta_learner/history")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
