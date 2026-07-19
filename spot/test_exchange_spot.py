"""
spot/test_exchange_spot.py — Test untuk WebSocketFeed._maybe_restart_orderbook_task()

[BUG-FIX -- self-healing _watch_orderbook(), item #5 audit fungsional]
_watch_orderbook(symbol) sebelumnya mati PERMANEN per-symbol setelah
max_retries, tanpa mekanisme restart -- beda dari _watch_tickers_all yang
sudah dibereskan sebelumnya (self-healing via _poll_tickers()).
_maybe_restart_orderbook_task() menutup celah itu, dipanggil per-symbol dari
_poll_orderbooks_rest(), cooldown eksponensial PERSIS formula ticker
(min(30*2^n, 600)).

Tidak ada test untuk WebSocketFeed sebelumnya (dikonfirmasi lewat audit) --
file ini baru, cakupannya sengaja dibatasi ke _maybe_restart_orderbook_task()
+ interaksinya dengan add_symbols(), bukan audit menyeluruh class ini
(WebSocketFeed butuh koneksi network sungguhan utk sebagian besar fungsinya,
di luar jangkauan sandbox ini).

    python3 -m unittest spot.test_exchange_spot -v
"""

from __future__ import annotations

import asyncio
import time
import unittest

from spot.exchange_spot import WebSocketFeed


def _make_feed(symbols=None) -> WebSocketFeed:
    """Instansiasi WebSocketFeed TANPA menyentuh network -- ccxt.pro exchange
    object cuma dikonfigurasi saat __init__, tidak connect apapun sampai
    start()/method WS eksplisit dipanggil."""
    return WebSocketFeed(
        exchange_id="binance", api_key="test_key", api_secret="test_secret",
        symbols=symbols or ["BTC/USDT"], testnet=True,
    )


async def _instant_coro():
    """Coroutine placeholder yang langsung selesai -- dipakai sbg pengganti
    _watch_orderbook() supaya task jadi .done() SEGERA tanpa perlu network."""
    return None


async def _sleep_forever_coro():
    """Coroutine yang tidak pernah selesai sendiri -- mensimulasikan
    _watch_orderbook() yang MASIH HIDUP (task belum .done())."""
    await asyncio.sleep(3600)


class TestMaybeRestartOrderbookTask(unittest.TestCase):

    def test_no_op_when_no_task_tracked_for_symbol(self):
        """Symbol yang belum pernah dapat WS orderbook (tidak ada entry di
        _ws_orderbook_tasks) -- tidak boleh crash, tidak boleh restart apa
        pun (tidak ada 'task lama' utk dibandingkan)."""
        feed = _make_feed()
        feed._maybe_restart_orderbook_task("BTC/USDT")
        self.assertNotIn("BTC/USDT", feed._ws_orderbook_tasks)
        self.assertEqual(feed._ob_restart_count.get("BTC/USDT", 0), 0)

    def test_no_restart_when_task_still_alive(self):
        """Task MASIH HIDUP (belum .done()) -- TIDAK BOLEH direstart, TIDAK
        BOLEH ada task kedua dibuat utk symbol yang sama."""
        async def _run():
            feed = _make_feed()
            alive_task = asyncio.create_task(_sleep_forever_coro())
            feed._ws_orderbook_tasks["BTC/USDT"] = alive_task
            try:
                feed._maybe_restart_orderbook_task("BTC/USDT")
                self.assertFalse(alive_task.done())
                # [KUNCI -- no duplicate WS connection] Task yang tersimpan
                # HARUS TETAP task yang SAMA (identity check), bukan diganti
                # task baru selama yang lama belum benar-benar mati.
                self.assertIs(feed._ws_orderbook_tasks["BTC/USDT"], alive_task)
                self.assertEqual(feed._ob_restart_count.get("BTC/USDT", 0), 0)
            finally:
                alive_task.cancel()
                try:
                    asyncio.get_event_loop()
                except Exception:
                    pass
        asyncio.run(_run())

    def test_restart_when_task_done_and_no_prior_restart(self):
        """Task sudah .done() DAN belum pernah direstart sebelumnya
        (_ob_last_restart_ts default 0.0, cooldown pertama otomatis lolos)
        -- HARUS direstart: task baru dibuat, referensi diganti, counter naik."""
        async def _run():
            feed = _make_feed()
            dead_task = asyncio.create_task(_instant_coro())
            await asyncio.sleep(0)  # beri kesempatan task selesai
            await dead_task
            self.assertTrue(dead_task.done())
            feed._ws_orderbook_tasks["BTC/USDT"] = dead_task

            feed._maybe_restart_orderbook_task("BTC/USDT")
            await asyncio.sleep(0)

            new_task = feed._ws_orderbook_tasks["BTC/USDT"]
            # [KUNCI -- no duplicate WS connection] Referensi HARUS berganti
            # ke task BARU (bukan objek yang sama dengan yang lama, yang
            # sudah confirmed .done() sebelum diganti -- tidak pernah ada
            # dua task hidup bersamaan utk symbol yang sama).
            self.assertIsNot(new_task, dead_task)
            self.assertEqual(feed._ob_restart_count["BTC/USDT"], 1)
            self.assertGreater(feed._ob_last_restart_ts["BTC/USDT"], 0.0)
            new_task.cancel()
            try:
                await new_task
            except (asyncio.CancelledError, Exception):
                pass
        asyncio.run(_run())

    def test_restart_blocked_during_cooldown(self):
        """Setelah SATU restart, percobaan restart LAGI segera sesudahnya
        (task baru juga langsung .done(), simulasi gagal lagi) HARUS
        diblokir cooldown -- TIDAK boleh restart kedua terjadi instan."""
        async def _run():
            feed = _make_feed()
            # Simulasikan: restart pertama BARU SAJA terjadi (last_restart_ts
            # = sekarang), restart_count sudah 1 -- cooldown seharusnya
            # min(30*2^1, 600) = 60 detik, jauh dari terlewati.
            feed._ob_restart_count["BTC/USDT"] = 1
            feed._ob_last_restart_ts["BTC/USDT"] = time.time()

            dead_task = asyncio.create_task(_instant_coro())
            await asyncio.sleep(0)
            await dead_task
            feed._ws_orderbook_tasks["BTC/USDT"] = dead_task

            feed._maybe_restart_orderbook_task("BTC/USDT")

            # TIDAK direstart -- referensi tetap task lama yang sudah mati,
            # counter TIDAK bertambah.
            self.assertIs(feed._ws_orderbook_tasks["BTC/USDT"], dead_task)
            self.assertEqual(feed._ob_restart_count["BTC/USDT"], 1)
        asyncio.run(_run())

    def test_cooldown_formula_matches_ticker_exactly(self):
        """[REGRESI] Formula cooldown HARUS identik persis dgn
        _ws_ticker_task punya (min(30*2^n, 600)) -- konsistensi disengaja,
        diverifikasi lewat behavior nyata (bukan cuma baca kode)."""
        async def _run():
            feed = _make_feed()
            for n, expected_cooldown in [(0, 30), (1, 60), (2, 120), (3, 240), (4, 480), (5, 600), (10, 600)]:
                feed._ob_restart_count["BTC/USDT"] = n
                # last_restart_ts pas di ambang: cooldown - 1 detik yang lalu
                # -> HARUS masih diblokir (belum lolos).
                feed._ob_last_restart_ts["BTC/USDT"] = time.time() - (expected_cooldown - 1)
                dead_task = asyncio.create_task(_instant_coro())
                await asyncio.sleep(0)
                await dead_task
                feed._ws_orderbook_tasks["BTC/USDT"] = dead_task

                feed._maybe_restart_orderbook_task("BTC/USDT")
                self.assertIs(
                    feed._ws_orderbook_tasks["BTC/USDT"], dead_task,
                    f"n={n}: seharusnya MASIH cooldown ({expected_cooldown}s), tapi malah direstart",
                )

                # last_restart_ts pas SETELAH cooldown lewat -> HARUS lolos.
                feed._ob_last_restart_ts["BTC/USDT"] = time.time() - (expected_cooldown + 1)
                feed._maybe_restart_orderbook_task("BTC/USDT")
                new_task = feed._ws_orderbook_tasks["BTC/USDT"]
                self.assertIsNot(
                    new_task, dead_task,
                    f"n={n}: seharusnya SUDAH lolos cooldown ({expected_cooldown}s), tapi tidak direstart",
                )
                new_task.cancel()
                try:
                    await new_task
                except (asyncio.CancelledError, Exception):
                    pass
        asyncio.run(_run())

    def test_add_symbols_registers_task_reference(self):
        """add_symbols() HARUS menyimpan referensi task ke
        _ws_orderbook_tasks (prasyarat _maybe_restart_orderbook_task bisa
        bekerja sama sekali) -- feed harus 'running' spy task benar2 di-spawn."""
        async def _run():
            feed = _make_feed(symbols=[])
            feed._running = True
            try:
                await feed.add_symbols(["ETH/USDT"])
                self.assertIn("ETH/USDT", feed._ws_orderbook_tasks)
                task = feed._ws_orderbook_tasks["ETH/USDT"]
                self.assertIsInstance(task, asyncio.Task)
                self.assertFalse(task.done())
            finally:
                feed._running = False
                for t in feed._tasks:
                    t.cancel()
                await asyncio.gather(*feed._tasks, return_exceptions=True)
        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
