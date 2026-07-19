"""
future/test_position_sync_futures.py -- Test untuk deteksi rekonsiliasi
posisi bidirectional (future/position_sync_futures.py).

[ITEM #4 -- audit fungsional] find_untracked_positions() sebelumnya HANYA
mendeteksi 1 dari 3 kemungkinan mismatch (posisi ADA di exchange, TIDAK ADA
di DB -- "untracked"). File ini menguji perluasannya jadi bidirectional:
1. fetch_binance_futures_positions() sekarang RAISE saat exchange.fetch_
   positions() gagal (bukan menelan jadi [] -- ambigu dgn "genuinely
   kosong", dikonfirmasi via investigasi kode sebelum implementasi).
2. find_untracked_positions() menangkap exception itu di level ini --
   arah untracked TETAP fail-safe (== [] spt sebelumnya), arah phantom
   SKIP SELURUH perbandingan siklus itu (fetch_failed=True), BUKAN
   false-positive "semua posisi DB phantom".
3. Deteksi phantom (db_symbols - exchange_symbols), difilter is_closing=True.
4. Debounce 2 siklus run_position_sync() berturut-turut sebelum genuinely
   dianggap phantom terkonfirmasi -- TIDAK auto-close, cuma log.critical +
   db.save_log("CRITICAL",...) + notifier.notify_error() (reuse pola yang
   sudah konvensi di _do_close_position()).

    python3 -m unittest future.test_position_sync_futures -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from future.position_sync_futures import (
    fetch_binance_futures_positions,
    find_untracked_positions,
    run_position_sync,
)


def _make_db_position(symbol, is_closing=False, amount=10.0):
    return SimpleNamespace(symbol=symbol, is_closing=is_closing, amount=amount)


def _make_exchange_position(symbol, side="long", amount=10.0, entry_price=10.0):
    return {
        "symbol": symbol, "side": side, "amount": amount,
        "entry_price": entry_price, "leverage": 5,
    }


class _FakeExchange:
    def __init__(self, positions=None, fetch_error=None):
        self.fetch_positions = AsyncMock(return_value=positions or [], side_effect=fetch_error)


class _FakeDBManager:
    def __init__(self, open_positions=None):
        self.get_open_positions = AsyncMock(return_value=open_positions or [])
        self.save_log = AsyncMock()


class TestFetchBinanceFuturesPositionsRaisesOnFailure(unittest.TestCase):
    """Section 1 -- blocker yang diverifikasi sebelum implementasi: fetch
    gagal HARUS raise, bukan diam-diam jadi []."""

    def test_fetch_error_raises_not_swallowed(self):
        exchange = _FakeExchange(fetch_error=RuntimeError("rate limit exceeded"))
        with self.assertRaises(RuntimeError):
            asyncio.run(fetch_binance_futures_positions(exchange))

    def test_genuinely_empty_still_returns_empty_list(self):
        """Regresi: kosong genuinely HARUS tetap [] (bukan exception)."""
        exchange = _FakeExchange(positions=[])
        result = asyncio.run(fetch_binance_futures_positions(exchange))
        self.assertEqual(result, [])

    def test_successful_fetch_returns_positions_unchanged(self):
        """Regresi: perilaku sukses TIDAK berubah dari sebelumnya."""
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT")])
        result = asyncio.run(fetch_binance_futures_positions(exchange))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "BTC/USDT")


class TestFindUntrackedPositionsBidirectional(unittest.TestCase):

    def test_fetch_failure_untracked_fail_safe_phantom_skipped(self):
        """[REGRESI KRITIS] Fetch gagal -- untracked TETAP [] (fail-safe
        spt sebelumnya), phantom_candidates JUGA [] TAPI fetch_failed=True
        -- caller HARUS tau ini beda dari 'genuinely tidak ada phantom'."""
        exchange = _FakeExchange(fetch_error=RuntimeError("network error"))
        db = _FakeDBManager(open_positions=[_make_db_position("ETH/USDT")])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["phantom_candidates"], [])
        self.assertEqual(result["amount_mismatches"], [])
        self.assertTrue(result["fetch_failed"])

    def test_untracked_direction_unchanged_regression(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("NEW/USDT")])
        db = _FakeDBManager(open_positions=[])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(len(result["untracked"]), 1)
        self.assertEqual(result["untracked"][0]["symbol"], "NEW/USDT")
        self.assertFalse(result["fetch_failed"])

    def test_phantom_direction_detects_db_open_absent_from_exchange(self):
        """[REGRESI UTAMA] Simbol is_open=True di DB, TIDAK ADA di exchange
        -- HARUS masuk phantom_candidates. Sebelum fix ini: arah ini TIDAK
        PERNAH dicek sama sekali."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertIn("GHOST/USDT", result["phantom_candidates"])

    def test_is_closing_filters_out_false_positive(self):
        """[False-positive protection] Posisi genuinely sedang proses close
        normal (is_closing=True) TIDAK BOLEH dianggap phantom candidate."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("CLOSING/USDT", is_closing=True)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["phantom_candidates"], [])

    def test_symbol_present_in_both_not_phantom_not_untracked(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("BOTH/USDT")])
        db = _FakeDBManager(open_positions=[_make_db_position("BOTH/USDT")])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["phantom_candidates"], [])


class TestPhantomDebounceViaRunPositionSync(unittest.TestCase):
    """Section 2 -- debounce 2 siklus, TIDAK auto-close, cuma notify."""

    def test_first_cycle_not_yet_confirmed_no_notification(self):
        exchange = _FakeExchange(positions=[])
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
        """[REGRESI UTAMA] Siklus KEDUA berturut-turut simbol yang sama --
        HARUS terkonfirmasi: log.critical + db.save_log(CRITICAL,...) +
        notifier.notify_error() -- TANPA auto-close (db.close_position
        TIDAK PERNAH dipanggil dari jalur ini)."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"GHOST/USDT": 1}  # sudah 1 siklus sebelumnya

        with self.assertLogs("future.position_sync", level="CRITICAL") as cm:
            result = asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertEqual(result["phantom_confirmed"], 1)
        self.assertEqual(suspects["GHOST/USDT"], 2)
        notifier.notify_error.assert_awaited_once()
        db.save_log.assert_awaited_once()
        save_log_args = db.save_log.call_args[0]
        self.assertEqual(save_log_args[0], "CRITICAL")
        self.assertIn("PHANTOM POSITION", "\n".join(cm.output))
        self.assertFalse(hasattr(db, "close_position"), "db fake sengaja TIDAK punya close_position -- kalau kepanggil, AttributeError akan gagalkan test ini")

    def test_symbol_resolved_clears_debounce_counter(self):
        """Simbol tidak lagi jadi kandidat siklus ini (mismatch resolve,
        mis. close normal akhirnya sukses) -- counter dibersihkan, bukan
        nyangkut selamanya."""
        exchange = _FakeExchange(positions=[_make_exchange_position("RESOLVED/USDT")])
        db = _FakeDBManager(open_positions=[_make_db_position("RESOLVED/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"RESOLVED/USDT": 1}

        asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertNotIn("RESOLVED/USDT", suspects)

    def test_fetch_failure_freezes_debounce_counter_not_reset_not_incremented(self):
        """[Sinergi dgn Section 1] Fetch gagal di siklus ke-2 -- counter
        HARUS tetap 1 (tidak direset ke 0 seolah resolve, TIDAK JUGA naik
        ke 2 seolah genuinely terkonfirmasi lagi) -- murni membeku, coba
        lagi siklus berikutnya."""
        exchange = _FakeExchange(fetch_error=RuntimeError("network error"))
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"GHOST/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=notifier, phantom_suspects=suspects))

        self.assertEqual(suspects["GHOST/USDT"], 1, "Counter HARUS membeku, tidak direset/dinaikkan")
        self.assertEqual(result["phantom_confirmed"], 0)
        notifier.notify_error.assert_not_awaited()

    def test_no_notifier_provided_does_not_crash(self):
        """notifier=None (default) -- notify_error() di-skip aman, tidak crash."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])
        suspects = {"GHOST/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=None, phantom_suspects=suspects))
        self.assertEqual(result["phantom_confirmed"], 1)

    def test_default_phantom_suspects_no_persistence_across_separate_calls(self):
        """Tanpa mengoper phantom_suspects sendiri (default None -> dict
        baru tiap panggilan) -- TIDAK ada debounce lintas-panggilan,
        backward-compatible dgn caller lama."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT")])

        result1 = asyncio.run(run_position_sync(exchange, db))
        result2 = asyncio.run(run_position_sync(exchange, db))

        self.assertEqual(result1["phantom_confirmed"], 0)
        self.assertEqual(result2["phantom_confirmed"], 0, "Tiap panggilan mulai dari dict baru -- tidak pernah nyampe 2 siklus")


class TestAmountMismatchDetection(unittest.TestCase):
    """[#36 -- audit fungsional] Mismatch tipe ke-3, sebelumnya "dicatat sbg
    item backlog terpisah" di docstring find_untracked_positions() --
    sekarang genuinely dideteksi. Root cause konkret: reduce_position_
    amount_with_retry() (#28) exhausted, DB nyangkut amount lama."""

    def test_amount_mismatch_beyond_tolerance_detected(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=6.0)])
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
        """[Non-regresi] Beda kecil (dust/rounding, dalam toleransi 5%)
        TIDAK boleh memicu alert."""
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=9.97)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])

    def test_is_closing_excluded_from_mismatch_check(self):
        """[False-positive protection] Posisi sedang proses close normal
        (is_closing=True) TIDAK boleh dianggap amount mismatch walau
        amount-nya genuinely beda jauh (race legitimate)."""
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=0.5)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0, is_closing=True)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])

    def test_dust_below_usdt_floor_not_flagged_despite_large_pct(self):
        """[Non-regresi -- floor absolut] Symbol receh: % relatif besar tapi
        nilai $ selisih di bawah MIN_USDT_VALUE -- TIDAK boleh memicu alert
        (noise dust, bukan masalah nyata)."""
        exchange = _FakeExchange(positions=[_make_exchange_position(
            "DUST/USDT", amount=0.0001, entry_price=0.01,
        )])
        db = _FakeDBManager(open_positions=[_make_db_position("DUST/USDT", amount=0.001)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])

    def test_symbol_only_in_db_not_double_counted_as_mismatch(self):
        """[Isolasi] Symbol yang genuinely phantom (cuma di DB) TIDAK boleh
        ikut masuk amount_mismatches -- itu domain phantom_candidates."""
        exchange = _FakeExchange(positions=[])
        db = _FakeDBManager(open_positions=[_make_db_position("GHOST/USDT", amount=10.0)])

        result = asyncio.run(find_untracked_positions(exchange, db))

        self.assertEqual(result["amount_mismatches"], [])
        self.assertIn("GHOST/USDT", result["phantom_candidates"])


class TestAmountMismatchDebounceViaRunPositionSync(unittest.TestCase):
    """Pola debounce IDENTIK dgn phantom (Section 2 di atas) -- 2 siklus,
    TIDAK auto-correct, counter TERPISAH dari phantom_suspects."""

    def test_first_cycle_not_yet_confirmed_no_notification(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=6.0)])
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
        """[REGRESI UTAMA] HARUS terkonfirmasi di siklus ke-2: log.critical +
        db.save_log + notifier.notify_error, TANPA auto-correct (tidak ada
        panggilan upsert/update amount dari jalur ini)."""
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=6.0)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        notifier = SimpleNamespace(notify_error=AsyncMock())
        suspects = {"BTC/USDT": 1}

        with self.assertLogs("future.position_sync", level="CRITICAL") as cm:
            result = asyncio.run(run_position_sync(
                exchange, db, notifier=notifier, amount_mismatch_suspects=suspects,
            ))

        self.assertEqual(result["amount_mismatch_confirmed"], 1)
        self.assertEqual(suspects["BTC/USDT"], 2)
        notifier.notify_error.assert_awaited_once()
        self.assertEqual(notifier.notify_error.call_args[0][0], "amount_mismatch_futures")
        db.save_log.assert_awaited_once()
        self.assertEqual(db.save_log.call_args[0][0], "CRITICAL")
        self.assertIn("AMOUNT MISMATCH", "\n".join(cm.output))

    def test_mismatch_and_phantom_suspects_are_independent_counters(self):
        """[Regresi kunci -- isolasi counter] Symbol yang SAMA punya
        progres phantom_suspects yang sudah jalan -- TIDAK boleh
        mempengaruhi/ikut ke amount_mismatch_suspects, dan sebaliknya."""
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=6.0)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        phantom_suspects  = {"BTC/USDT": 1}   # progres phantom (symbol ini TIDAK phantom, di kedua sisi)
        mismatch_suspects = {}

        asyncio.run(run_position_sync(
            exchange, db, phantom_suspects=phantom_suspects,
            amount_mismatch_suspects=mismatch_suspects,
        ))

        # BTC/USDT ada di exchange -> bukan phantom candidate -> counter
        # phantom dibersihkan (resolve), TIDAK ikut campur dgn mismatch.
        self.assertNotIn("BTC/USDT", phantom_suspects)
        self.assertEqual(mismatch_suspects["BTC/USDT"], 1)

    def test_resolved_mismatch_clears_debounce_counter(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=10.0)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        suspects = {"BTC/USDT": 1}

        asyncio.run(run_position_sync(exchange, db, amount_mismatch_suspects=suspects))

        self.assertNotIn("BTC/USDT", suspects)

    def test_no_notifier_provided_does_not_crash(self):
        exchange = _FakeExchange(positions=[_make_exchange_position("BTC/USDT", amount=6.0)])
        db = _FakeDBManager(open_positions=[_make_db_position("BTC/USDT", amount=10.0)])
        suspects = {"BTC/USDT": 1}

        result = asyncio.run(run_position_sync(exchange, db, notifier=None, amount_mismatch_suspects=suspects))
        self.assertEqual(result["amount_mismatch_confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
