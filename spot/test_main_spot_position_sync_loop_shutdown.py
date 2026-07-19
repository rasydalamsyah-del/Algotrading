"""
spot/test_main_spot_position_sync_loop_shutdown.py -- Test untuk bug-fix
#11: TradingBot.run_position_sync_loop() (spot/main_spot.py) sebelumnya
pakai `while True:` -- BEDA dari SEMUA loop lain di file yang sama
(run_analytics_loop, run_sl_tp_monitor, run_scanner_loop, dkk) dan dari
padanan futures-nya sendiri (future/main_future.py::run_position_sync_loop),
yang semuanya pakai `while self.is_running:`.

[ROOT CAUSE] Cancellation lewat task.cancel() (dipanggil TradingBot.stop())
kemungkinan besar tetap propagate lewat `except Exception` (CancelledError
adalah BaseException sejak Python 3.8, tidak tertangkap di situ) --
DIKONFIRMASI tidak ada bare except/except BaseException di jalur
position_sync_spot.py::run_position_sync() yang bisa menelannya. TAPI
`while True:` tetap gap nyata: stop() men-set `self.is_running=False`
sebagai jalur shutdown independen KEDUA (defense-in-depth) yang dipakai
konsisten oleh SEMUA loop lain -- loop ini sebelumnya SATU-SATUNYA yang
mengabaikan flag itu sepenuhnya, sehingga hanya punya SATU jalur shutdown
(cancellation) alih-alih dua. Kalau cancellation gagal sampai/tertunda
karena alasan apa pun, loop ini akan berjalan selamanya walau
`self.is_running` sudah False -- dan stop() (`await
asyncio.gather(*self._tasks, return_exceptions=True)`) akan menggantung
menunggu task ini selesai.

Fix: `while True:` -> `while self.is_running:`, plus `except
asyncio.CancelledError: break` eksplisit (menyamakan pola persis dengan
run_sl_tp_monitor/run_analytics_loop di file yang sama).

Pola test: method UNBOUND (TradingBot.run_position_sync_loop(fake_self))
dengan `self` stub minimal, `is_running` diflipkan ke False DI DALAM mock
run_position_sync() (bukan lewat cancellation) -- membuktikan loop
berhenti karena mengecek flag, bukan karena kebetulan/timeout. Dibungkus
asyncio.wait_for() dengan timeout pendek supaya kalau regresi ke
`while True:` lagi, test GAGAL cepat (bukan hang selamanya).

    python3 -m unittest spot.test_main_spot_position_sync_loop_shutdown -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spot.main_spot import TradingBot


def _build_fake_self(scan_result=None):
    fake_self = SimpleNamespace()
    fake_self.is_running = True
    fake_self.exchange = SimpleNamespace()
    fake_self.db = SimpleNamespace()
    fake_self.notifier = None
    fake_self._phantom_suspects = {}
    fake_self._amount_mismatch_suspects = {}
    return fake_self


class TestPositionSyncLoopRespectsIsRunning(unittest.TestCase):

    def test_loop_stops_after_flag_flips_false_exactly_one_iteration(self):
        """[Regresi kunci -- inti bug #11] Begitu is_running diset False di
        dalam iterasi PERTAMA, loop TIDAK BOLEH memanggil run_position_sync
        lagi di iterasi kedua -- membuktikan `while self.is_running:`
        genuinely dicek tiap iterasi, bukan `while True:` yang mengabaikannya."""
        fake_self = _build_fake_self()
        call_count = 0

        async def _fake_run_position_sync(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            fake_self.is_running = False
            return {"adopted": 0, "rejected": 0, "errors": 0, "phantom_confirmed": 0}

        with patch("spot.main_spot.run_position_sync", side_effect=_fake_run_position_sync), \
             patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(
                asyncio.wait_for(
                    TradingBot.run_position_sync_loop(fake_self), timeout=5.0,
                )
            )

        self.assertEqual(
            call_count, 1,
            "loop harus berhenti TEPAT setelah 1 iterasi begitu is_running=False "
            "-- kalau ini gagal (call_count>1 atau test timeout), loop masih "
            "mengabaikan flag is_running seperti sebelum fix.",
        )

    def test_loop_never_calls_sync_when_is_running_false_from_start(self):
        """Kalau is_running SUDAH False sebelum loop mulai (mis. shutdown
        terjadi persis sebelum task ini sempat jalan), run_position_sync
        TIDAK BOLEH dipanggil sama sekali."""
        fake_self = _build_fake_self()
        fake_self.is_running = False
        mock_sync = AsyncMock(return_value={
            "adopted": 0, "rejected": 0, "errors": 0, "phantom_confirmed": 0,
        })

        with patch("spot.main_spot.run_position_sync", mock_sync), \
             patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(
                asyncio.wait_for(
                    TradingBot.run_position_sync_loop(fake_self), timeout=5.0,
                )
            )

        mock_sync.assert_not_awaited()

    def test_cancelled_error_during_sync_still_breaks_loop(self):
        """[Non-regresi] Cancellation eksplisit (asyncio.CancelledError
        dilempar dari dalam run_position_sync, spt yg terjadi kalau
        task.cancel() menembak persis di titik itu) tetap harus membuat
        loop berhenti bersih -- lewat `except asyncio.CancelledError: break`
        yang baru ditambahkan, bukan cuma lewat pengecekan is_running."""
        fake_self = _build_fake_self()

        async def _cancel_immediately(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch("spot.main_spot.run_position_sync", side_effect=_cancel_immediately), \
             patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(
                asyncio.wait_for(
                    TradingBot.run_position_sync_loop(fake_self), timeout=5.0,
                )
            )
        # Tidak raise/timeout -> loop genuinely berhenti bersih di iterasi pertama.


if __name__ == "__main__":
    unittest.main()
