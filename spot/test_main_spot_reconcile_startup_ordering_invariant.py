"""
spot/test_main_spot_reconcile_startup_ordering_invariant.py -- Test untuk
item #27: _reconcile_positions_on_startup() (spot/main_spot.py) auto-close
posisi phantom TANPA debounce -- SENGAJA berbeda dari run_position_sync_loop()
(item #4, debounce 2 siklus, tidak pernah auto-close). Investigasi
menyimpulkan ini BUKAN bug: fungsi ini dipanggil dari start() SEBELUM satu
pun task periodik dibuat (self._tasks masih kosong pada titik itu) --
race condition yang jadi alasan debounce di jalur periodik (posisi
genuinely sedang proses close normal, tampak "phantom" sesaat) SECARA
STRUKTURAL tidak bisa terjadi di startup (nol aktivitas trading konkuren).

Keputusan (dipilih user): BUKAN diseragamkan, BUKAN ditambah debounce
(tidak menambah keamanan riil krn race yg dilindungi tidak ada di sini) --
didokumentasikan eksplisit SEBAGAI keputusan desain, PLUS invarian
"dipanggil sebelum task periodik dibuat" dijaga via `assert not self._tasks`
runtime di start() (bukan cuma komentar) supaya kalau urutan startup
berubah di sesi mendatang tanpa sadar, gagal LOUD alih-alih auto-close
diam-diam mulai beroperasi di tengah race condition yang seharusnya
dilindungi debounce.

[Batasan pengujian] start() penuh terlalu berat utk ditest langsung (DB
connect, exchange connect, I/O nyata) -- test ini memverifikasi INVARIAN
struktural (self._tasks genuinely kosong sampai run() selesai memanggil
start(), dikonfirmasi lewat construction TradingBot() ringan) DAN
verifikasi source-level bahwa assertion + komentar berpasangan genuinely
ada di kedua titik (start() & run()), dengan urutan yang benar (assert
SEBELUM pemanggilan _reconcile_positions_on_startup()).

    python3 -m unittest spot.test_main_spot_reconcile_startup_ordering_invariant -v
"""

from __future__ import annotations

import inspect
import unittest

from spot.main_spot import TradingBot


class TestTasksEmptyInvariantAtConstruction(unittest.TestCase):

    def test_tasks_list_is_empty_right_after_construction(self):
        """[Prekondisi invarian] self._tasks HARUS kosong segera setelah
        TradingBot() dikonstruksi -- baru terisi di run(), SETELAH
        start() (termasuk reconciliation di dalamnya) selesai penuh."""
        bot = TradingBot()
        self.assertEqual(bot._tasks, [])


class TestReconcileOrderingAssertionInSource(unittest.TestCase):
    """Verifikasi assertion runtime `assert not self._tasks` genuinely ada
    di start() TEPAT SEBELUM memanggil _reconcile_positions_on_startup(),
    bukan cuma komentar dokumentasi tanpa penegakan -- kalau assertion ini
    dihapus/dipindah, test ini gagal, sinyal bahwa invarian item #27 perlu
    ditinjau ulang."""

    def test_assert_not_tasks_present_before_reconcile_call(self):
        src = inspect.getsource(TradingBot.start)
        self.assertIn("assert not self._tasks", src)
        self.assertIn("await self._reconcile_positions_on_startup()", src)

        assert_idx  = src.index("assert not self._tasks")
        call_idx    = src.index("await self._reconcile_positions_on_startup()")
        self.assertLess(
            assert_idx, call_idx,
            "assert not self._tasks HARUS muncul SEBELUM pemanggilan "
            "_reconcile_positions_on_startup() di source start() -- kalau "
            "urutannya terbalik, assertion tidak lagi genuinely menjaga "
            "invarian di titik yang benar.",
        )

    def test_reconcile_docstring_documents_design_decision(self):
        """Docstring _reconcile_positions_on_startup() HARUS menyebut
        eksplisit ini keputusan desain (bukan bug) & alasan strukturalnya --
        supaya sesi mendatang tidak salah "memperbaiki" jadi debounce."""
        src = inspect.getsource(TradingBot._reconcile_positions_on_startup)
        normalized = " ".join(src.split())
        self.assertIn("BUKAN bug", normalized)
        self.assertIn("nol aktivitas trading konkuren", normalized)


class TestRunTasksCreationHasPairedComment(unittest.TestCase):

    def test_run_source_documents_tasks_creation_ordering_constraint(self):
        """Komentar berpasangan di run() (titik self._tasks pertama kali
        terisi) HARUS menjelaskan constraint ordering yang sama, supaya
        siapa pun yang menambah task baru di sini melihat peringatannya
        tanpa perlu membaca ulang start()."""
        src = inspect.getsource(TradingBot.run)
        tasks_idx = src.index("self._tasks = [")
        comment_before = src[:tasks_idx]
        self.assertIn("#27", comment_before[-1200:])
        self.assertIn("invarian", comment_before[-1200:].lower())


if __name__ == "__main__":
    unittest.main()
