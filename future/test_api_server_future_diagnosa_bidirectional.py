"""
future/test_api_server_future_diagnosa_bidirectional.py -- Test untuk
GET /api/diagnosa di future/api_server_future.py (audit item #19).

[KEPUTUSAN DESAIN, dikonfirmasi user] Genuinely bidirectional. BEDA
STRUKTURAL dari spot: spot pakai get_cached_observation(symbol, tf) (jalur
primer) -- fungsi itu TIDAK PUNYA parameter side, ambil entry cache
ter-update APA PUN sisinya (ambiguitas sama persis dgn root cause bug
/api/forecast). Di sini SENGAJA dihindari -- pakai
get_latest_signal_score(symbol, side=X) per sisi sbg sumber utama
(_diagnosa_entry_from_row), fallback manual mirror side="short" HANYA
dipakai kalau symbol genuinely belum pernah discoring sisi itu
(_diagnosa_fallback_entry -- lihat docstring-nya utk batasan jujur: mirror
manual, BUKAN hasil verifikasi fuzz-test spt sub-indikator pipeline utama).

    python3 -m unittest future.test_api_server_future_diagnosa_bidirectional -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd

os.environ.setdefault("DASHBOARD_API_KEY_FUTURES", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from future.api_server_future import (
    create_app, _diagnosa_entry_from_row, _diagnosa_fallback_entry,
)

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _make_score_row(side="long", **overrides):
    defaults = dict(
        current_price=100.0, strategy_profile="scalp_volatile",
        regime="trending_bull", regime_confidence=0.8,
        total_score=72.0, trigger_met=True, threshold_used=65.0,
        trend_score=70.0, momentum_score=68.0, strength_score=75.0,
        volatility_score=60.0, pattern_score=55.0, oscillator_score=50.0,
        structure_score=65.0, orderbook_score=58.0,
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_profile(profile_name="scalp_volatile"):
    return SimpleNamespace(
        profile=SimpleNamespace(value=profile_name),
        volume_mult=1.3, volume_spike=3.0,
        rsi_min=45.0, rsi_max=77.0, rsi_gc_min=45.0,
        min_breakout_pct=0.3,
        atr_sl_mult=1.5, atr_tp_mult=2.5,
        quick_sl_pct=1.0, quick_tp_pct=2.0,
        atr_pct_threshold=0.8,
        entry_threshold=70.0, min_score=None,
    )


def _make_fallback_df(
    close=100.0, ema9=105.0, ema21=103.0, ema50=100.0, rsi=60.0,
    atr=2.0, resistance=95.0, support=104.0, vol_ratio_mult=2.0,
    prev_ema9=101.0, prev_ema21=103.0,
):
    """DataFrame minimal langsung (BUKAN via enrich_production sungguhan)
    -- kolom persis yang dibaca _diagnosa_fallback_entry()."""
    vol_ma = 1000.0
    quote_volume_last = vol_ma * vol_ratio_mult
    rows = []
    n = 6
    for i in range(n):
        rows.append({
            "close": close, "volume": 10.0, "quote_volume": vol_ma,
            "EMA_9": prev_ema9 if i == n - 3 else ema9,
            "EMA_21": prev_ema21 if i == n - 3 else ema21,
            "EMA_50": ema50, "RSI_14": rsi, "ATRr_14": atr,
            "_resistance": resistance, "_support": support, "_vol_ma": vol_ma,
        })
    rows[-2]["quote_volume"] = quote_volume_last
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(rows, index=idx)


class TestDiagnosaEntryFromRow(unittest.TestCase):

    def test_long_entry_maps_fields_correctly(self):
        row = _make_score_row(side="long")
        entry = _diagnosa_entry_from_row(row, "long")
        self.assertEqual(entry["side"], "long")
        self.assertEqual(entry["total_score"], 72.0)
        self.assertEqual(entry["source"], "database")
        self.assertEqual(len(entry["breakdown"]), 8)
        self.assertEqual(entry["breakdown"]["trend"], 70.0)

    def test_open_position_included_when_matching_side(self):
        row = _make_score_row(side="short", total_score=41.0)
        pos = SimpleNamespace(entry_score=50.0, entry_price=105.0, unrealized_pnl_pct=-3.0)
        entry = _diagnosa_entry_from_row(row, "short", open_position=pos)
        self.assertIn("open_position", entry)
        self.assertEqual(entry["open_position"]["score_delta"], round(41.0 - 50.0, 2))

    def test_no_narrative_field_documented_limitation(self):
        """[Batasan jujur, disengaja] narrative/calculation_errors TIDAK
        disertakan -- sumbernya (observation cache) ambigu sisi."""
        row = _make_score_row()
        entry = _diagnosa_entry_from_row(row, "long")
        self.assertNotIn("narrative", entry)
        self.assertNotIn("calculation_errors", entry)


class TestDiagnosaFallbackEntryDirectional(unittest.TestCase):

    def test_long_sl_below_tp_above_price(self):
        df = _make_fallback_df(close=100.0, ema9=105.0, ema21=103.0, ema50=100.0, rsi=60.0)
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "long", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertIsNotNone(entry)
        self.assertLess(entry["sl"], entry["price"])
        self.assertGreater(entry["tp"], entry["price"])

    def test_short_sl_above_tp_below_price(self):
        """[Regresi kunci] SL short di ATAS harga, TP di BAWAH -- bukan
        formula long yang direplikasi."""
        df = _make_fallback_df(close=100.0, ema9=95.0, ema21=98.0, ema50=100.0, rsi=40.0)
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "short", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertIsNotNone(entry)
        self.assertGreater(entry["sl"], entry["price"])
        self.assertLess(entry["tp"], entry["price"])

    def test_long_ema_stack_condition_bullish(self):
        df = _make_fallback_df(ema9=105.0, ema21=103.0, ema50=100.0)
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "long", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertNotIn("EMAStack", entry["failed_conditions"])

    def test_short_ema_stack_condition_needs_bearish_not_bullish(self):
        """[Regresi kunci] Stack bullish (ema9>ema21>ema50) HARUS gagal
        cond_trend utk short -- bukan kondisi long yang direplikasi."""
        df = _make_fallback_df(ema9=105.0, ema21=103.0, ema50=100.0)  # bullish stack
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "short", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertIn("EMAStack", entry["failed_conditions"])

    def test_short_ema_stack_passes_when_genuinely_bearish(self):
        df = _make_fallback_df(ema9=95.0, ema21=98.0, ema50=100.0)  # bearish stack
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "short", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertNotIn("EMAStack", entry["failed_conditions"])

    def test_source_is_fallback_v6(self):
        df = _make_fallback_df()
        prof = _make_profile()
        entry = _diagnosa_fallback_entry(df, "long", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertEqual(entry["source"], "fallback_v6")

    def test_insufficient_bars_returns_none(self):
        df = _make_fallback_df().iloc[:2]
        prof = _make_profile()
        self.assertIsNone(_diagnosa_fallback_entry(df, "long", prof, is_testnet=False, tf_used="15m", tf_note=""))

    def test_long_and_short_not_mirror_by_accident_same_inputs(self):
        """'Bukan cuma beda angka' -- pastikan hasil genuinely beda arah,
        bukan kebetulan sama."""
        df = _make_fallback_df(ema9=105.0, ema21=103.0, ema50=100.0, rsi=60.0)
        prof = _make_profile()
        long_e  = _diagnosa_fallback_entry(df, "long", prof, is_testnet=False, tf_used="15m", tf_note="")
        short_e = _diagnosa_fallback_entry(df, "short", prof, is_testnet=False, tf_used="15m", tf_note="")
        self.assertNotEqual(long_e["sl"], short_e["sl"])
        self.assertNotEqual(long_e["tp"], short_e["tp"])
        self.assertNotEqual(long_e["trigger_met"], short_e["trigger_met"])


class TestDiagnosaEndpointBidirectional(unittest.TestCase):

    def _build_fake_bot(self, rows_by_side, positions=None, exchange_ohlcv=None):
        async def _get_score(symbol, side=None):
            return rows_by_side.get((symbol, side))

        db = SimpleNamespace(
            get_latest_signal_score=AsyncMock(side_effect=_get_score),
            get_open_positions=AsyncMock(return_value=positions or []),
        )
        exchange = SimpleNamespace(
            fetch_ohlcv=AsyncMock(return_value=exchange_ohlcv or []),
        )
        return SimpleNamespace(
            config={"universe_watchlist": ["BTC/USDT"], "timeframe": "15m", "testnet": True},
            db=db,
            exchange=exchange,
        )

    def _client(self, bot):
        return TestClient(create_app(lambda: bot))

    def test_both_sides_from_database_when_both_rows_exist(self):
        rows = {
            ("BTC/USDT", "long"):  _make_score_row(side="long"),
            ("BTC/USDT", "short"): _make_score_row(side="short", total_score=40.0),
        }
        bot = self._build_fake_bot(rows)
        c = self._client(bot)
        r = c.get("/api/diagnosa", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["results"]), 2)
        sides = {e["side"] for e in body["results"]}
        self.assertEqual(sides, {"long", "short"})
        for e in body["results"]:
            self.assertEqual(e["source"], "database")
        bot.exchange.fetch_ohlcv.assert_not_awaited()

    def test_error_entry_when_no_data_at_all(self):
        bot = self._build_fake_bot({}, exchange_ohlcv=[])
        c = self._client(bot)
        r = c.get("/api/diagnosa", headers=_HEADERS)
        body = r.json()
        self.assertEqual(len(body["results"]), 2)
        for e in body["results"]:
            self.assertIn("error", e)

    def test_requires_auth(self):
        bot = self._build_fake_bot({})
        c = self._client(bot)
        r = c.get("/api/diagnosa")
        self.assertEqual(r.status_code, 401)

    def test_open_position_matched_to_correct_side_only(self):
        rows = {("BTC/USDT", "long"): _make_score_row(side="long")}
        pos_short = SimpleNamespace(
            symbol="BTC/USDT", side="short", entry_score=50.0,
            entry_price=105.0, unrealized_pnl_pct=-2.0,
        )
        bot = self._build_fake_bot(rows, positions=[pos_short])
        c = self._client(bot)
        r = c.get("/api/diagnosa", headers=_HEADERS)
        body = r.json()
        long_entry = next(e for e in body["results"] if e["side"] == "long")
        self.assertNotIn("open_position", long_entry, "posisi short tidak boleh nempel ke entry long")


if __name__ == "__main__":
    unittest.main()
