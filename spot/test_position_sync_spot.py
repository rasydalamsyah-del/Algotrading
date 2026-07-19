"""
spot/test_position_sync_spot.py -- Test untuk deteksi rekonsiliasi posisi
bidirectional (spot/position_sync_spot.py).

[ITEM #4 -- audit fungsional] Mirror persis future/test_position_sync_futures.py
-- lihat docstring di sana untuk latar belakang lengkap (blocker fetch-gagal-
vs-genuinely-kosong, deteksi phantom bidirectional, debounce 2 siklus, tanpa
auto-close). Root cause phantom di spot: _paper_balance berkurang SEBELUM
db.close_position() di _do_close_position() (main_spot.py) -- analog dgn
_paper_positions di futures.

    python3 -m unittest spot.test_position_sync_spot -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spot.position_sync_spot import (
    fetch_binance_spot_positions,
    find_untracked_positions,
    run_position_sync,
)


def _make_db_position(symbol, is_closing=False, amount=10.0):
    return SimpleNamespace(symbol=symbol, is_closing=is_closing, amount=amount)


class _FakeExchange:
    def __init__(self, balance_total=None, fetch_error=None):
        balance = {"total": balance_total or {}}
        self.fetch_balance = AsyncMock(return_value=balance, side_effect=fetch_error)
        self.fetch_ticker = AsyncMock(return_value={"last": 10.0})


class _FakeDBManager:
    def __init__(self, open_positions=None):
        self.get_open_positions = AsyncMock(return_value=open_positions or [])
        self.save_log = AsyncMock()


class TestFetchBinanceSpotPositionsRaisesOnFailure(unittest.TestCase):

    def test_fetch_error_raises_not_swallowed(self):
        exchange = _FakeExchange(fetch_error=RuntimeError("rate limit exceeded"))
        with self.assertRaises(RuntimeError):
            asyncio.run(fetch_binance_spot_positions(exchange))

    def test_genuinely_empty_still_returns_empty_list(self):
        exchange = _FakeExchange(balance_total={})
        result = asyncio.run(fetch_binance_spot_positions(exchange))
        self.assertEqual(result, [])

    def test_successful_fetch_returns_positions_unchanged(self):
        exchange = _FakeExchange(balance_total={"BTC": 1.0})
        result = asyncio.run(fetch_binance_spot_positions(exchange))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "BTC/USDT")


class TestFindUntrackedPositionsBidirectional(unittest.TestCase):

    def test_fetch_failure_untracked_fail_safe_phantom_skipped(self):
        exchange = _FakeExchange(fetch_error=RuntimeError("network error"))
        db = _FakeDBManager(open_positions=[_make_db_position("ETH/USDT")])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["phantom_candidates"], [])
        self.assertEqual(result["amount_mismatches"], [])
        self.assertTrue(result["fetch_failed"])

    def test_untracked_direction_unchanged_regression(self):
        exchange = _FakeExchange(balance_total={"SOL": 5.0})
        db = _FakeDBManager(open_positions=[])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(len(result["untracked"]), 1)
        self.assertEqual(result["untracked"][0]["symbol"], "SOL/USDT")
        self.assertFalse(result["fetch_failed"])

    def test_phantom_direction_detects_db_open_absent_from_exchange(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertIn("GHOST/USDT", result["phantom_candidates"])

    def test_is_closing_filters_out_false_positive(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("CLOSING/USDT", is_closing=True)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["phantom_candidates"], [])

    def test_symbol_present_in_both_not_phantom_not_untracked(self):
        exchange = _FakeExchange(balance_total={"AVAX": 2.0})
        db = _FakeDBManager(open_positions=[_make_db_position("AVAX/USDT", amount=2.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["phantom_candidates"], [])
        self.assertEqual(result["amount_mismatches"], [])


class TestPhantomDebounceViaRunPositionSync(unittest.TestCase):

    def test_first_cycle_not_yet_confirmed_no_notification(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {}

        result = asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertEqual(result["phantom_candidates"], 1)
        self.assertEqual(result["phantom_confirmed"], 0)
        self.assertEqual(suspects["GHOST/USDT"], 1)
        notifier.notify_error.assert_not_awaited()
        db.save_log.assert_not_awaited()

    def test_second_consecutive_cycle_confirms_and_notifies(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"GHOST/USDT": 1}

        with self.assertLogs("intelligence.position_sync", level="CRITICAL") as cm:
            result = asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertEqual(result["phantom_confirmed"], 1)
        self.assertEqual(suspects["GHOST/USDT"], 2)
        notifier.notify_error.assert_awaited_once()
        db.save_log.assert_awaited_once()
        self.assertEqual(db.save_log.call_args[0][0], "CRITICAL")
        self.assertIn("PHANTOM POSITION", "\n".join(cm.output))

    def test_symbol_resolved_clears_debounce_counter(self):
        exchange = _FakeExchange(balance_total={"RESOLVED": 1.0})
        db = _FakeDBManager(open_positions=[_make_db_position("RESOLVED/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"RESOLVED/USDT": 1}

        asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertNotIn("RESOLVED/USDT", suspects)

    def test_fetch_failure_freezes_debounce_counter_not_reset_not_incremented(self):
        exchange = _FakeExchange(fetch_error=RuntimeError("network error"))
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"GHOST/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertEqual(suspects["GHOST/USDT"], 1)
        self.assertEqual(result["phantom_confirmed"], 0)
        notifier.notify_error.assert_not_awaited()

    def test_no_notifier_provided_does_not_crash(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        suspects = {"GHOST/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=None, phantom_suspects=suspects))
        self.assertEqual(result["phantom_confirmed"], 1)


class TestAmountMismatchDetection(unittest.TestCase):
    """[#36 -- audit fungsional] Mirror future/test_position_sync_futures.py
    ::TestAmountMismatchDetection -- lihat docstring di sana utk latar
    belakang lengkap. Penyebab plausible di spot: fee/dust deduction atau
    aktivitas manual eksternal (BUKAN kegagalan retry partial-close --
    spot tidak punya partial-close sama sekali)."""

    def test_amount_mismatch_beyond_tolerance_detected(self):
        exchange = _FakeExchange(balance_total={"BTC": 6.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(len(result["amount_mismatches"]), 1)
        m = result["amount_mismatches"][0]
        self.assertEqual(m["symbol"], "BTC/USDT")
        self.assertEqual(m["db_amount"], 10.0)
        self.assertEqual(m["exchange_amount"], 6.0)
        self.assertAlmostEqual(m["diff_pct"], 40.0, places=4)
        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["phantom_candidates"], [])

    def test_amount_within_tolerance_not_flagged(self):
        exchange = _FakeExchange(balance_total={"BTC": 9.97})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])

    def test_is_closing_excluded_from_mismatch_check(self):
        exchange = _FakeExchange(balance_total={"BTC": 0.5})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0, is_closing=True)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])

    def test_symbol_only_in_db_not_double_counted_as_mismatch(self):
        exchange = _FakeExchange(balance_total={})
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT", amount=10.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])
        self.assertIn("GHOST/USDT", result["phantom_candidates"])


class TestAmountMismatchDebounceViaRunPositionSync(unittest.TestCase):
    """Pola debounce IDENTIK dgn phantom -- 2 siklus, TIDAK auto-correct,
    counter TERPISAH dari phantom_suspects."""

    def test_first_cycle_not_yet_confirmed_no_notification(self):
        exchange = _FakeExchange(balance_total={"BTC": 6.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {}

        result = asyncio.run(run_position_sync(
            exchange, db, notifier=notifier, amount_mismatch_suspects=suspects,
        ))

        self.assertEqual(result["amount_mismatch_candidates"], 1)
        self.assertEqual(result["amount_mismatch_confirmed"], 0)
        self.assertEqual(suspects["BTC/USDT"], 1)
        notifier.notify_error.assert_not_awaited()

    def test_second_consecutive_cycle_confirms_and_notifies(self):
        exchange = _FakeExchange(balance_total={"BTC": 6.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"BTC/USDT": 1}

        with self.assertLogs("intelligence.position_sync", level="CRITICAL") as cm:
            result = asyncio.run(run_position_sync(
                exchange, db, notifier=notifier, amount_mismatch_suspects=suspects,
            ))

        self.assertEqual(result["amount_mismatch_confirmed"], 1)
        self.assertEqual(suspects["BTC/USDT"], 2)
        notifier.notify_error.assert_awaited_once()
        self.assertEqual(notifier.notify_error.call_args[0][0], "amount_mismatch_spot")
        db.save_log.assert_awaited_once()
        self.assertEqual(db.save_log.call_args[0][0], "CRITICAL")
        self.assertIn("AMOUNT MISMATCH", "\n".join(cm.output))

    def test_mismatch_and_phantom_suspects_are_independent_counters(self):
        exchange = _FakeExchange(balance_total={"BTC": 6.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        phantom_suspects  = {"BTC/USDT": 1}
        mismatch_suspects = {}

        asyncio.run(run_position_sync(
            exchange, db, phantom_suspects=phantom_suspects,
            amount_mismatch_suspects=mismatch_suspects,
        ))

        self.assertNotIn("BTC/USDT", phantom_suspects)
        self.assertEqual(mismatch_suspects["BTC/USDT"], 1)

    def test_resolved_mismatch_clears_debounce_counter(self):
        exchange = _FakeExchange(balance_total={"BTC": 10.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        suspects = {"BTC/USDT": 1}

        asyncio.run(run_position_sync(exchange, db, amount_mismatch_suspects=suspects))

        self.assertNotIn("BTC/USDT", suspects)

    def test_no_notifier_provided_does_not_crash(self):
        exchange = _FakeExchange(balance_total={"BTC": 6.0})
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        suspects = {"BTC/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=None, amount_mismatch_suspects=suspects))
        self.assertEqual(result["amount_mismatch_confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
