"""
future/test_capital_allocator.py — Unit test untuk future/capital_allocator.py

Dijalankan pakai stdlib unittest (tidak ada pytest terpasang di lingkungan
ini): python3 -m unittest future.test_capital_allocator -v

Strategi test:
- Fungsi murni (is_expired, register_or_refresh, purge_expired,
  pick_best_pair, _select_winner) di-test LANGSUNG tanpa mocking apapun --
  semuanya deterministic dari input plain.
- reconcile_pending() (orkestrasi, I/O berat: fetch OHLCV, indikator,
  scoring, commander.decide) di-test dengan mem-patch _rescore_candidate()
  & _build_entry_signal() (batas I/O modul ini) memakai FakeBot ringan --
  supaya skenario 0/1/2 kandidat tersisa & tie-break bisa diverifikasi
  tanpa perlu mock seluruh pipeline observer/scorer/exchange.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from future import capital_allocator
from future.capital_allocator import PendingCandidate


def _fake_build_entry_signal(bot, symbol, side, payload):
    """Pengganti _build_entry_signal asli utk test -- FakeBot._handle_entry
    butuh signal.symbol, jadi pakai SimpleNamespace ringan, bukan string."""
    return SimpleNamespace(symbol=symbol, side=side)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_candidate(
    symbol="BTC/USDT", side="long", registered_at=None,
    candle_ts=1_000_000, price=100.0, atr=2.0, tf="15m",
    last_score=70.0,
) -> PendingCandidate:
    return PendingCandidate(
        symbol=symbol, side=side,
        registered_at=registered_at or _now(),
        candle_ts_at_registration=candle_ts,
        price_at_registration=price,
        atr_at_registration=atr,
        profile_timeframe=tf,
        profile_name="trend_follow",
        last_score=last_score,
        last_checked_at=registered_at or _now(),
        reason_deferred="Margin tidak cukup",
        defer_count=1,
    )


# ─── is_expired() — 3 aturan OR ─────────────────────────────────────────────


class TestIsExpired(unittest.TestCase):

    def test_not_expired_when_nothing_triggers(self):
        c = _make_candidate(registered_at=_now(), candle_ts=1000, price=100.0, atr=2.0, tf="15m")
        expired, reason = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=1000, current_price=100.5,
        )
        self.assertFalse(expired)
        self.assertEqual(reason, "")

    def test_rule1_candle_closed_triggers(self):
        c = _make_candidate(registered_at=_now(), candle_ts=1000)
        expired, reason = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=2000, current_price=None,
        )
        self.assertTrue(expired)
        self.assertIn("candle_closed", reason)

    def test_rule1_not_triggered_when_same_candle(self):
        c = _make_candidate(registered_at=_now(), candle_ts=1000)
        expired, _ = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=1000, current_price=None,
        )
        self.assertFalse(expired)

    def test_rule1_skipped_when_latest_ts_none(self):
        # Simbol berhenti ter-scan -- _last_candle_ts tidak pernah update.
        c = _make_candidate(registered_at=_now(), candle_ts=1000)
        expired, _ = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=None,
        )
        self.assertFalse(expired)  # aturan 1 tidak bisa dievaluasi, aturan 2/3 juga tidak terpenuhi

    def test_rule2_atr_move_triggers(self):
        c = _make_candidate(price=100.0, atr=2.0)  # threshold = 1.5*2.0 = 3.0
        expired, reason = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=103.01,  # moved=3.01 > 3.0
        )
        self.assertTrue(expired)
        self.assertIn("atr_move", reason)

    def test_rule2_exactly_at_threshold_not_triggered(self):
        c = _make_candidate(price=100.0, atr=2.0)  # threshold = 3.0
        expired, _ = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=103.0,  # moved==3.0, bukan >3.0
        )
        self.assertFalse(expired)

    def test_rule2_skipped_when_atr_zero(self):
        c = _make_candidate(price=100.0, atr=0.0)
        expired, _ = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=99999.0,
        )
        self.assertFalse(expired)  # atr=0 -> aturan 2 di-skip total

    def test_rule3_wall_clock_cap_triggers(self):
        tf_secs = capital_allocator.TIMEFRAME_SECONDS["15m"]  # 900
        old_time = _now() - timedelta(seconds=2 * tf_secs + 1)
        c = _make_candidate(registered_at=old_time, tf="15m")
        expired, reason = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=None,
        )
        self.assertTrue(expired)
        self.assertIn("wall_clock_cap", reason)

    def test_rule3_not_yet_at_cap(self):
        tf_secs = capital_allocator.TIMEFRAME_SECONDS["15m"]
        recent_time = _now() - timedelta(seconds=2 * tf_secs - 5)
        c = _make_candidate(registered_at=recent_time, tf="15m")
        expired, _ = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=None, current_price=None,
        )
        self.assertFalse(expired)

    def test_rule1_priority_over_rule3_when_both_true(self):
        # Kalau candle-close DAN wall-clock-cap sama2 true, reason yg
        # dikembalikan harus dari aturan yg dicek PERTAMA (candle_closed).
        tf_secs = capital_allocator.TIMEFRAME_SECONDS["15m"]
        old_time = _now() - timedelta(seconds=2 * tf_secs + 100)
        c = _make_candidate(registered_at=old_time, candle_ts=1000, tf="15m")
        expired, reason = capital_allocator.is_expired(
            c, now=_now(), latest_candle_ts=2000, current_price=None,
        )
        self.assertTrue(expired)
        self.assertIn("candle_closed", reason)


# ─── register_or_refresh() ──────────────────────────────────────────────────


class TestRegisterOrRefresh(unittest.TestCase):

    def test_new_registration_sets_baseline(self):
        registry = {}
        t0 = _now()
        c = capital_allocator.register_or_refresh(
            registry, "BTC/USDT", "long", "trend_follow", "1h",
            candle_ts=555, price=50000.0, atr=100.0, score=72.0,
            reason="Margin tidak cukup", now=t0,
        )
        self.assertIs(registry["BTC/USDT"], c)
        self.assertEqual(c.candle_ts_at_registration, 555)
        self.assertEqual(c.price_at_registration, 50000.0)
        self.assertEqual(c.atr_at_registration, 100.0)
        self.assertEqual(c.defer_count, 1)
        self.assertEqual(c.registered_at, t0)

    def test_refresh_same_side_preserves_baseline(self):
        registry = {}
        t0 = _now()
        capital_allocator.register_or_refresh(
            registry, "BTC/USDT", "long", "trend_follow", "1h",
            candle_ts=555, price=50000.0, atr=100.0, score=72.0,
            reason="Margin tidak cukup", now=t0,
        )
        t1 = t0 + timedelta(minutes=5)
        c2 = capital_allocator.register_or_refresh(
            registry, "BTC/USDT", "long", "trend_follow", "1h",
            candle_ts=999, price=51000.0, atr=110.0, score=75.0,
            reason="Margin masih tidak cukup", now=t1,
        )
        # Baseline TIDAK berubah walau argumen candle_ts/price/atr baru beda.
        self.assertEqual(c2.candle_ts_at_registration, 555)
        self.assertEqual(c2.price_at_registration, 50000.0)
        self.assertEqual(c2.atr_at_registration, 100.0)
        self.assertEqual(c2.registered_at, t0)
        # Bookkeeping BERUBAH.
        self.assertEqual(c2.last_score, 75.0)
        self.assertEqual(c2.reason_deferred, "Margin masih tidak cukup")
        self.assertEqual(c2.defer_count, 2)
        self.assertEqual(c2.last_checked_at, t1)

    def test_side_flip_replaces_baseline(self):
        registry = {}
        t0 = _now()
        capital_allocator.register_or_refresh(
            registry, "BTC/USDT", "long", "trend_follow", "1h",
            candle_ts=555, price=50000.0, atr=100.0, score=72.0,
            reason="Margin tidak cukup", now=t0,
        )
        t1 = t0 + timedelta(minutes=5)
        c2 = capital_allocator.register_or_refresh(
            registry, "BTC/USDT", "short", "trend_follow", "1h",
            candle_ts=999, price=49000.0, atr=120.0, score=80.0,
            reason="Margin tidak cukup (short)", now=t1,
        )
        self.assertEqual(c2.side, "short")
        self.assertEqual(c2.candle_ts_at_registration, 999)  # baseline BARU
        self.assertEqual(c2.price_at_registration, 49000.0)
        self.assertEqual(c2.registered_at, t1)
        self.assertEqual(c2.defer_count, 1)  # reset, bukan lanjut dari 1->2


# ─── purge_expired() ─────────────────────────────────────────────────────────


class TestPurgeExpired(unittest.TestCase):

    def test_mixed_expired_and_valid(self):
        registry = {}
        now = _now()
        registry["A"] = _make_candidate(symbol="A", registered_at=now, candle_ts=1000)  # akan expired via candle
        registry["B"] = _make_candidate(symbol="B", registered_at=now, candle_ts=1000)  # tetap valid
        last_candle_ts_map = {("A", "15m"): 2000, ("B", "15m"): 1000}
        current_prices = {"A": None, "B": None}

        purged = capital_allocator.purge_expired(registry, now, last_candle_ts_map, current_prices)

        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0][0], "A")
        self.assertNotIn("A", registry)
        self.assertIn("B", registry)


# ─── pick_best_pair() ────────────────────────────────────────────────────────


class TestPickBestPair(unittest.TestCase):

    def test_picks_highest_score_per_side(self):
        registry = {
            "A": _make_candidate(symbol="A", side="long", last_score=60.0),
            "B": _make_candidate(symbol="B", side="long", last_score=80.0),
            "C": _make_candidate(symbol="C", side="short", last_score=90.0),
        }
        best_long, best_short = capital_allocator.pick_best_pair(registry)
        self.assertEqual(best_long.symbol, "B")
        self.assertEqual(best_short.symbol, "C")

    def test_missing_side_returns_none(self):
        registry = {"A": _make_candidate(symbol="A", side="long", last_score=60.0)}
        best_long, best_short = capital_allocator.pick_best_pair(registry)
        self.assertIsNotNone(best_long)
        self.assertIsNone(best_short)

    def test_empty_registry(self):
        best_long, best_short = capital_allocator.pick_best_pair({})
        self.assertIsNone(best_long)
        self.assertIsNone(best_short)


# ─── _select_winner() — 0/1/2 kandidat & tie-break ──────────────────────────


class TestSelectWinner(unittest.TestCase):

    def test_zero_candidates(self):
        self.assertIsNone(capital_allocator._select_winner(None, None))

    def test_one_candidate_long_only(self):
        long_c = _make_candidate(side="long")
        winner = capital_allocator._select_winner((long_c, 70.0), None)
        self.assertEqual(winner, "long")

    def test_one_candidate_short_only(self):
        short_c = _make_candidate(side="short")
        winner = capital_allocator._select_winner(None, (short_c, 70.0))
        self.assertEqual(winner, "short")

    def test_two_candidates_long_wins(self):
        long_c = _make_candidate(side="long")
        short_c = _make_candidate(side="short")
        winner = capital_allocator._select_winner((long_c, 80.0), (short_c, 70.0))
        self.assertEqual(winner, "long")

    def test_two_candidates_short_wins(self):
        long_c = _make_candidate(side="long")
        short_c = _make_candidate(side="short")
        winner = capital_allocator._select_winner((long_c, 60.0), (short_c, 75.0))
        self.assertEqual(winner, "short")

    def test_two_candidates_tie_long_wins(self):
        long_c = _make_candidate(side="long")
        short_c = _make_candidate(side="short")
        winner = capital_allocator._select_winner((long_c, 70.0), (short_c, 70.0))
        self.assertEqual(winner, "long")


# ─── reconcile_pending() — orkestrasi, _rescore_candidate & _build_entry_signal di-mock ──


class FakeWsFeed:
    def __init__(self):
        self.live_tickers = {}


class FakeRiskManager:
    def __init__(self, free_balance=1000.0, min_order=10.0):
        self._free_balance = free_balance
        self._min_order_value_usdt = min_order


class FakeDB:
    def __init__(self):
        self._opened: set = set()

    async def get_open_position_by_symbol(self, symbol):
        return object() if symbol in self._opened else None


class FakeBot:
    def __init__(self, free_balance=1000.0):
        self._pending_candidates: dict = {}
        self._last_candle_ts: dict = {}
        self.ws_feed = FakeWsFeed()
        self.risk_manager = FakeRiskManager(free_balance=free_balance)
        self.db = FakeDB()
        self.handle_entry_calls = []

    async def _handle_entry(self, signal, side):
        self.handle_entry_calls.append((signal, side))
        # Simulasikan sukses: tandai symbol sbg open di DB fake.
        self.db._opened.add(signal.symbol)


class TestReconcilePending(unittest.IsolatedAsyncioTestCase):

    async def test_empty_registry_is_noop(self):
        bot = FakeBot()
        summary = await capital_allocator.reconcile_pending(bot)
        self.assertEqual(summary, {"purged": 0, "attempted": None, "opened": False})
        self.assertEqual(bot.handle_entry_calls, [])

    async def test_skips_when_free_balance_below_min_order(self):
        bot = FakeBot(free_balance=1.0)  # < default min_order 10.0
        bot._pending_candidates["A"] = _make_candidate(symbol="A", side="long")
        with patch("future.capital_allocator._rescore_candidate", new=AsyncMock()) as mock_rescore:
            summary = await capital_allocator.reconcile_pending(bot)
        mock_rescore.assert_not_called()
        self.assertEqual(bot.handle_entry_calls, [])
        self.assertIn("A", bot._pending_candidates)  # tidak disentuh sama sekali

    async def test_one_candidate_executable_gets_opened(self):
        bot = FakeBot()
        candidate = _make_candidate(symbol="A", side="long")
        bot._pending_candidates["A"] = candidate

        async def fake_rescore(_bot, cand):
            return "executable", 80.0, {"fake": "payload"}

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore), \
             patch("future.capital_allocator._build_entry_signal", side_effect=_fake_build_entry_signal):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(len(bot.handle_entry_calls), 1)
        self.assertEqual(bot.handle_entry_calls[0][0].symbol, "A")
        self.assertEqual(bot.handle_entry_calls[0][1], "long")
        self.assertEqual(summary["attempted"], ("A", "long"))
        self.assertTrue(summary["opened"])
        self.assertNotIn("A", bot._pending_candidates)  # dipop krn sukses

    async def test_two_candidates_higher_score_wins_no_tie(self):
        bot = FakeBot()
        bot._pending_candidates["LONG_SYM"] = _make_candidate(symbol="LONG_SYM", side="long", last_score=50.0)
        bot._pending_candidates["SHORT_SYM"] = _make_candidate(symbol="SHORT_SYM", side="short", last_score=50.0)

        async def fake_rescore(_bot, cand):
            if cand.symbol == "LONG_SYM":
                return "executable", 60.0, {"side": "long"}
            return "executable", 90.0, {"side": "short"}  # short menang telak

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore), \
             patch("future.capital_allocator._build_entry_signal", side_effect=_fake_build_entry_signal):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(len(bot.handle_entry_calls), 1)
        self.assertEqual(bot.handle_entry_calls[0][1], "short")
        self.assertEqual(summary["attempted"], ("SHORT_SYM", "short"))
        # Kandidat yg kalah TIDAK ikut dieksekusi & TIDAK dipop (masih pending).
        self.assertIn("LONG_SYM", bot._pending_candidates)
        self.assertNotIn("SHORT_SYM", bot._pending_candidates)

    async def test_two_candidates_tie_score_long_wins(self):
        bot = FakeBot()
        bot._pending_candidates["LONG_SYM"] = _make_candidate(symbol="LONG_SYM", side="long", last_score=50.0)
        bot._pending_candidates["SHORT_SYM"] = _make_candidate(symbol="SHORT_SYM", side="short", last_score=50.0)

        async def fake_rescore(_bot, cand):
            return "executable", 77.0, {"side": cand.side}  # skor SAMA PERSIS

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore), \
             patch("future.capital_allocator._build_entry_signal", side_effect=_fake_build_entry_signal):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(len(bot.handle_entry_calls), 1)
        self.assertEqual(bot.handle_entry_calls[0][1], "long")  # tie-break -> long
        self.assertEqual(summary["attempted"], ("LONG_SYM", "long"))
        self.assertIn("SHORT_SYM", bot._pending_candidates)  # short (kalah) tetap pending

    async def test_both_still_pending_no_attempt(self):
        bot = FakeBot()
        bot._pending_candidates["LONG_SYM"] = _make_candidate(symbol="LONG_SYM", side="long")
        bot._pending_candidates["SHORT_SYM"] = _make_candidate(symbol="SHORT_SYM", side="short")

        async def fake_rescore(_bot, cand):
            return "still_pending", 55.0, None

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(bot.handle_entry_calls, [])
        self.assertIsNone(summary["attempted"])
        self.assertFalse(summary["opened"])
        # Masih pending, bookkeeping ter-refresh (last_score ikut update).
        self.assertIn("LONG_SYM", bot._pending_candidates)
        self.assertIn("SHORT_SYM", bot._pending_candidates)
        self.assertEqual(bot._pending_candidates["LONG_SYM"].last_score, 55.0)

    async def test_stale_candidate_dropped_not_attempted(self):
        bot = FakeBot()
        bot._pending_candidates["LONG_SYM"] = _make_candidate(symbol="LONG_SYM", side="long")

        async def fake_rescore(_bot, cand):
            return "stale", 20.0, None  # skor sudah anjlok di bawah threshold

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(bot.handle_entry_calls, [])
        self.assertIsNone(summary["attempted"])
        self.assertNotIn("LONG_SYM", bot._pending_candidates)  # dibuang, bukan disimpan lagi

    async def test_expired_candidate_purged_before_rescore(self):
        bot = FakeBot()
        # Kandidat sudah lewat wall-clock cap -- harus di-purge SEBELUM
        # sempat masuk _rescore_candidate sama sekali.
        tf_secs = capital_allocator.TIMEFRAME_SECONDS["15m"]
        old_time = _now() - timedelta(seconds=2 * tf_secs + 10)
        bot._pending_candidates["OLD"] = _make_candidate(symbol="OLD", side="long", registered_at=old_time, tf="15m")

        with patch("future.capital_allocator._rescore_candidate", new=AsyncMock()) as mock_rescore:
            summary = await capital_allocator.reconcile_pending(bot)

        mock_rescore.assert_not_called()
        self.assertEqual(summary["purged"], 1)
        self.assertNotIn("OLD", bot._pending_candidates)

    async def test_winner_still_capital_constrained_stays_pending(self):
        # _handle_entry gagal LAGI (mis. saldo baru ternyata tetap kurang) --
        # FakeBot default menandai sukses, jadi override _handle_entry di
        # sini utk simulasikan gagal (symbol TIDAK ditandai open).
        bot = FakeBot()
        bot._pending_candidates["A"] = _make_candidate(symbol="A", side="long")

        async def failing_handle_entry(signal, side):
            bot.handle_entry_calls.append((signal, side))
            # sengaja TIDAK menandai db._opened -- simulasi masih gagal

        bot._handle_entry = failing_handle_entry

        async def fake_rescore(_bot, cand):
            return "executable", 80.0, {}

        with patch("future.capital_allocator._rescore_candidate", side_effect=fake_rescore), \
             patch("future.capital_allocator._build_entry_signal", side_effect=_fake_build_entry_signal):
            summary = await capital_allocator.reconcile_pending(bot)

        self.assertEqual(len(bot.handle_entry_calls), 1)
        self.assertFalse(summary["opened"])
        # TIDAK dipop -- _handle_entry (di produksi) sudah register_or_refresh
        # ulang sendiri kalau gagal lagi krn kapasitas; reconcile_pending tidak
        # perlu (dan tidak boleh) menghapusnya di sini.
        self.assertIn("A", bot._pending_candidates)


if __name__ == "__main__":
    unittest.main()
