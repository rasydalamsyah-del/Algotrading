"""
future/test_api_server_future_forecast_bidirectional.py -- Test untuk
GET /api/forecast di future/api_server_future.py (audit item #19).

[KEPUTUSAN DESAIN, dikonfirmasi user setelah investigasi] Genuinely
bidirectional -- BUKAN long-only apa adanya spt spot. Root cause masalah
versi spot: `get_latest_signal_score(symbol)` dipanggil TANPA side --
untuk symbol yang di-scoring long DAN short tiap siklus (futures), query
itu ambil row APA PUN yang kebetulan timestamp-nya paling baru (bisa long,
bisa short), lalu 100% diinterpretasi sbg sinyal long (ema_bullish,
probability_up_pct, dst) -- berpotensi aktif menyesatkan.

Fix/desain baru: fetch side="long" DAN side="short" terpisah (row DB
independen, field skornya SUDAH side-aware sejak scorer.py -- dikonfirmasi
lewat _pick_side_score() dipakai di _calc_weighted_breakdown untuk SEMUA
kategori), 1 entri per side per symbol.

[Prasyarat yang juga diperbaiki -- ditemukan saat investigasi ini]
_suggest_sl_tp() (engine/intelligence/scorer.py) sebelumnya hardcode
formula long (SL di bawah, TP di atas) regardless side -- diuji terpisah
di engine/intelligence/test_scorer_suggest_sl_tp_side_aware.py. File ini
HANYA menguji endpoint /api/forecast & helper _build_forecast_entry().

    python3 -m unittest future.test_api_server_future_forecast_bidirectional -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DASHBOARD_API_KEY_FUTURES", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from future.api_server_future import create_app, _build_forecast_entry

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _make_row(side="long", price=100.0, sl=None, tp=None, **overrides):
    if sl is None:
        sl = 95.0 if side == "long" else 105.0
    if tp is None:
        tp = 108.0 if side == "long" else 92.0
    defaults = dict(
        current_price=price, suggested_sl=sl, suggested_tp=tp,
        regime="trending_bull", strategy_profile="scalp_volatile",
        signal_confidence=0.7, total_score=72.0, threshold_used=65.0,
        trend_score=70.0, momentum_score=68.0, strength_score=75.0,
        volatility_score=60.0, pattern_score=55.0, oscillator_score=50.0,
        structure_score=65.0, orderbook_score=58.0,
        nearest_support=98.0, nearest_resistance=110.0,
        fib_support=97.0, fib_resistance=112.0,
        trigger_met=True, regime_confidence=0.8,
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestBuildForecastEntryDirectional(unittest.TestCase):
    """Test langsung ke helper _build_forecast_entry() -- presisi lebih
    tinggi drpd lewat HTTP utk memverifikasi formula arah."""

    def test_long_sl_below_tp_above_price_correct_pct_signs(self):
        row = _make_row(side="long", price=100.0, sl=95.0, tp=108.0)
        entry = _build_forecast_entry(row, "long", "15m", {}, {})
        self.assertEqual(entry["side"], "long")
        self.assertEqual(entry["potential_profit_pct"], 8.0)   # (108-100)/100*100
        self.assertEqual(entry["potential_loss_pct"], 5.0)     # (100-95)/100*100
        self.assertGreater(entry["potential_profit_pct"], 0)
        self.assertGreater(entry["potential_loss_pct"], 0)

    def test_short_sl_above_tp_below_price_correct_pct_signs(self):
        """[Regresi kunci] SL short di ATAS harga, TP di BAWAH -- formula
        profit/loss HARUS dibalik, bukan formula long yang direplikasi."""
        row = _make_row(side="short", price=100.0, sl=105.0, tp=92.0)
        entry = _build_forecast_entry(row, "short", "15m", {}, {})
        self.assertEqual(entry["side"], "short")
        self.assertEqual(entry["potential_profit_pct"], 8.0)   # (100-92)/100*100
        self.assertEqual(entry["potential_loss_pct"], 5.0)     # (105-100)/100*100
        self.assertGreater(entry["potential_profit_pct"], 0)
        self.assertGreater(entry["potential_loss_pct"], 0)

    def test_ema_stack_aligned_mirrored_between_sides(self):
        indicators = {"ema9": 105.0, "ema21": 103.0, "ema50": 100.0}  # bullish stack
        row = _make_row()
        long_entry  = _build_forecast_entry(row, "long", "15m", indicators, {})
        short_entry = _build_forecast_entry(row, "short", "15m", indicators, {})
        self.assertTrue(long_entry["ema_stack_aligned"], "EMA menaik HARUS aligned utk long")
        self.assertFalse(short_entry["ema_stack_aligned"], "EMA menaik HARUS TIDAK aligned utk short")

    def test_ema_stack_aligned_bearish_favors_short(self):
        indicators = {"ema9": 95.0, "ema21": 98.0, "ema50": 100.0}  # bearish stack
        row = _make_row()
        long_entry  = _build_forecast_entry(row, "long", "15m", indicators, {})
        short_entry = _build_forecast_entry(row, "short", "15m", indicators, {})
        self.assertFalse(long_entry["ema_stack_aligned"])
        self.assertTrue(short_entry["ema_stack_aligned"])

    def test_trend_summary_uses_bearish_wording_for_short_strong_signal(self):
        indicators = {"ema9": 95.0, "ema21": 98.0, "ema50": 100.0, "rsi_slope": 1.0}
        row = _make_row()
        entry = _build_forecast_entry(row, "short", "15m", {**indicators, "rsi": 40.0}, {})
        self.assertIn("Bearish", entry["trend_summary"])

    def test_trend_summary_not_identical_wording_between_sides_same_inputs(self):
        """'Bukan cuma beda angka' -- label harus genuinely beda framing,
        bukan cuma nilai numerik yang beda."""
        indicators = {"ema9": 105.0, "ema21": 103.0, "ema50": 100.0, "rsi": 60.0, "rsi_slope": 1.0}
        row = _make_row()
        long_entry  = _build_forecast_entry(row, "long", "15m", indicators, {})
        short_entry = _build_forecast_entry(row, "short", "15m", indicators, {})
        self.assertNotEqual(long_entry["trend_summary"], short_entry["trend_summary"])

    def test_confirm_tf_result_confirms_for_matching_direction(self):
        row = _make_row()
        conf_tf_bullish = {"rsi": 60.0, "ema_bullish": True}
        long_entry  = _build_forecast_entry(row, "long", "15m", {}, conf_tf_bullish)
        short_entry = _build_forecast_entry(row, "short", "15m", {}, conf_tf_bullish)
        self.assertEqual(long_entry["confirm_tf_direction"], "bullish")
        self.assertEqual(long_entry["confirm_tf_result"], "confirms")
        self.assertEqual(short_entry["confirm_tf_result"], "conflicts")

    def test_returns_none_when_no_price(self):
        row = _make_row(price=None)
        self.assertIsNone(_build_forecast_entry(row, "long", "15m", {}, {}))

    def test_returns_none_when_row_is_none(self):
        self.assertIsNone(_build_forecast_entry(None, "long", "15m", {}, {}))

    def test_probability_favorable_pct_field_present_not_probability_up(self):
        """Field lama probability_up_pct diganti probability_favorable_pct
        (generik per-side) -- pastikan nama field baru benar-benar dipakai."""
        row = _make_row()
        entry = _build_forecast_entry(row, "short", "15m", {}, {})
        self.assertIn("probability_favorable_pct", entry)
        self.assertNotIn("probability_up_pct", entry)


class TestForecastEndpointBidirectional(unittest.TestCase):

    def _build_fake_bot(self, rows_by_side, indicators=None, conf_tf_data=None):
        obs = SimpleNamespace(
            primary_tf_indicators=None,
            confirmation_tf_indicators=None,
        )
        observer = SimpleNamespace(get_cached_observation=AsyncMock(return_value=obs if indicators else None))
        strategy = SimpleNamespace(_observer=observer)

        async def _get_score(symbol, side=None):
            return rows_by_side.get((symbol, side))

        db = SimpleNamespace(get_latest_signal_score=AsyncMock(side_effect=_get_score))

        return SimpleNamespace(
            config={"universe_watchlist": ["BTC/USDT"], "timeframe": "15m"},
            strategy=strategy,
            db=db,
        )

    def _client(self, bot):
        return TestClient(create_app(lambda: bot))

    def test_both_sides_returned_when_both_rows_exist(self):
        rows = {
            ("BTC/USDT", "long"):  _make_row(side="long"),
            ("BTC/USDT", "short"): _make_row(side="short"),
        }
        bot = self._build_fake_bot(rows)
        c = self._client(bot)
        r = c.get("/api/forecast", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        sides = {f["side"] for f in body["forecasts"]}
        self.assertEqual(sides, {"long", "short"})
        for f in body["forecasts"]:
            self.assertEqual(f["symbol"], "BTC/USDT")

    def test_only_long_side_when_short_row_missing(self):
        rows = {("BTC/USDT", "long"): _make_row(side="long")}
        bot = self._build_fake_bot(rows)
        c = self._client(bot)
        r = c.get("/api/forecast", headers=_HEADERS)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["forecasts"][0]["side"], "long")

    def test_no_rows_gives_empty_forecast(self):
        bot = self._build_fake_bot({})
        c = self._client(bot)
        r = c.get("/api/forecast", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_db_queried_with_explicit_side_kwarg_both_directions(self):
        """[Regresi kunci] HARUS memanggil get_latest_signal_score dgn
        side="long" DAN side="short" terpisah -- bukan tanpa side spt
        versi spot yang jadi root cause masalah."""
        rows = {
            ("BTC/USDT", "long"):  _make_row(side="long"),
            ("BTC/USDT", "short"): _make_row(side="short"),
        }
        bot = self._build_fake_bot(rows)
        c = self._client(bot)
        c.get("/api/forecast", headers=_HEADERS)
        calls = [c.kwargs.get("side") for c in bot.db.get_latest_signal_score.await_args_list]
        self.assertIn("long", calls)
        self.assertIn("short", calls)

    def test_requires_auth(self):
        bot = self._build_fake_bot({})
        c = self._client(bot)
        r = c.get("/api/forecast")
        self.assertEqual(r.status_code, 401)

    def test_sorted_by_probability_favorable_descending(self):
        rows = {
            ("BTC/USDT", "long"):  _make_row(side="long", total_score=40.0, signal_confidence=0.3),
            ("BTC/USDT", "short"): _make_row(side="short", total_score=90.0, signal_confidence=0.9),
        }
        bot = self._build_fake_bot(rows)
        c = self._client(bot)
        r = c.get("/api/forecast", headers=_HEADERS)
        body = r.json()
        probs = [f["probability_favorable_pct"] for f in body["forecasts"]]
        self.assertEqual(probs, sorted(probs, reverse=True))


if __name__ == "__main__":
    unittest.main()
