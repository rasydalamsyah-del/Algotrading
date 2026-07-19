"""
spot/test_main_spot_equity_lock_deadlock_fix.py -- Test untuk fix deadlock
_equity_lock di TradingBot._on_trade_executed() (spot/main_spot.py).

[URGENT FIX -- audit performa live, kejadian nyata TOWNS/USDT 2026-07-17
15:00:07] Root cause terkonfirmasi via audit database+log produksi (bukan
teori): `_handle_buy()` memegang `_equity_lock` sepanjang `execute_signal()`
(pelebaran lock yang DISENGAJA -- lihat komentar "AIGENSYN & PYR" di
`_handle_buy()`, mencegah drawdown palsu). `execute_signal()` di ujungnya
memanggil `_process_fill()` (engine/execution_base.py), yang memanggil
`on_trade_executed(trade)` -- yaitu `_on_trade_executed()` -- SEBELUM fix
ini, fungsi itu langsung `await self._refresh_portfolio()`, yang mencoba
`async with self._equity_lock:` LAGI. `asyncio.Lock` TIDAK reentrant --
task yang sama menunggu lock yang sudah dipegangnya sendiri = deadlock
PERMANEN (sembuh hanya lewat restart proses). Kejadian nyata: TOWNS/USDT
memicu ini pertama kali, melumpuhkan ~92%+ eksekusi spot (entry maupun
close/SL-TP) selama 12.5+ jam sampai diaudit.

FIX (diterapkan di `_on_trade_executed()`): kedua caller `execute_signal()`
(`_handle_buy()` & `_do_close_position()`) SUDAH memanggil
`_refresh_portfolio()` sendiri secara eksplisit DI LUAR `_equity_lock`
(item #2 & kode lama) -- jadi refresh di `_on_trade_executed()` sekarang
redundan untuk keduanya, TAPI dipertahankan sbg jaring pengaman fire-and-
forget (`asyncio.create_task()`, BUKAN `await` blocking) untuk caller masa
depan yang mungkin lupa refresh sendiri. Task yang di-fork MENUNGGU lock
dengan aman (task lain yang menunggu tidak memblokir task yang MEMEGANG
lock untuk selesai & melepasnya) -- tidak deadlock diri sendiri.

Cakupan test file ini:
1. `TestOnTradeExecutedNoLongerDeadlocks` -- membuktikan pola BARU (fire-
   and-forget) TIDAK macet lagi ketika dipanggil dari dalam scope
   `_equity_lock` yang sama (reproduksi persis mekanisme TOWNS), DAN
   safety-net refresh tetap benar-benar berjalan (bukan didiamkan begitu
   saja), DAN task lain yang menunggu lock yang sama TIDAK ikut macet
   selamanya (lock tidak "teracuni" permanen).
2. `TestOldBlockingPatternDeadlockDocumentation` -- dokumentasi: reproduksi
   LANGSUNG pola LAMA (`await self._refresh_portfolio()` tanpa
   create_task) utk membuktikan mekanisme deadlock-nya nyata & bukan
   spekulasi teoretis (test ini SENGAJA tidak memanggil kode produksi yang
   sudah diperbaiki -- murni dokumentasi bug).
3. `TestHandleBuyEquityLockStillWideAigensynPyrRegression` -- non-regresi:
   membuktikan fix ini TIDAK menyempitkan scope `_equity_lock` di
   `_handle_buy()` -- `execute_signal()` HARUS tetap terbungkus lock,
   sehingga `_refresh_portfolio()` konkuren (spt dari
   `run_portfolio_monitor()`) TETAP diblokir sampai `_handle_buy()`
   selesai (upsert_position() tuntas) -- proteksi drawdown palsu
   (kasus AIGENSYN & PYR) tidak regresi oleh fix deadlock ini.

    python3 -m unittest spot.test_main_spot_equity_lock_deadlock_fix -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from engine.core.models import SignalEvent, SignalType
from spot.main_spot import TradingBot
from spot.risk_spot import RiskManager


def _make_signal(symbol="TEST/USDT", price=10.0):
    return SignalEvent(
        symbol=symbol, signal_type=SignalType.BUY, price=price,
        timestamp=datetime.now(timezone.utc), strategy="test_strategy",
        stop_loss=None, take_profit=None, metadata={},
    )


def _make_trade(fee_cost=0.0, notes=""):
    return SimpleNamespace(
        filled=10.0, amount=10.0, executed_price=10.0, notes=notes,
        fee_cost=fee_cost, order_id="ORDER1", id=1, timestamp=None,
    )


async def _drain_pending_tasks(timeout=2.0):
    """Beri kesempatan task fire-and-forget (asyncio.create_task) untuk
    selesai sebelum assert -- tanpa ini, safety-net task bisa saja belum
    sempat jalan sama sekali saat test membaca hasilnya."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=timeout)


class TestOnTradeExecutedNoLongerDeadlocks(unittest.TestCase):
    """Reproduksi persis mekanisme TOWNS/USDT: _on_trade_executed()
    dipanggil DARI DALAM scope _equity_lock yang sama (persis seperti
    dipanggil dari execute_signal() di dalam _handle_buy()/
    _do_close_position())."""

    def _build_fake_self(self, refresh_calls):
        fake_self = SimpleNamespace()
        fake_self._equity_lock = asyncio.Lock()
        fake_self._last_refresh_time = 0.0
        fake_self._MIN_REFRESH_INTERVAL = 5.0

        async def _fake_refresh_portfolio():
            # Mirrors _refresh_portfolio() asli: re-acquire _equity_lock --
            # inti mekanisme reentrancy yang diuji di sini.
            async with fake_self._equity_lock:
                refresh_calls.append("refreshed")

        fake_self._refresh_portfolio = _fake_refresh_portfolio
        # Pakai implementasi PRODUKSI asli utk wrapper fire-and-forget --
        # supaya test ini genuinely menguji kode produksi (exception
        # handling di dalamnya), bukan reimplementasi sendiri di test.
        fake_self._refresh_portfolio_safety_net = TradingBot._refresh_portfolio_safety_net.__get__(fake_self)
        return fake_self

    def test_call_inside_held_lock_does_not_hang(self):
        """Skenario TOWNS persis: _on_trade_executed() dipanggil sementara
        task yang sama sedang memegang _equity_lock (mis. di dalam
        `async with self._equity_lock: ... execute_signal() -> ...
        on_trade_executed(trade)`). Dengan kode LAMA ini macet SELAMANYA.
        Dengan fix, harus selesai jauh di bawah timeout."""
        refresh_calls = []
        fake_self = self._build_fake_self(refresh_calls)
        trade = SimpleNamespace()

        async def simulated_handle_buy_critical_section():
            async with fake_self._equity_lock:
                await TradingBot._on_trade_executed(fake_self, trade)
                return "handle_buy_completed"

        async def scenario():
            result = await asyncio.wait_for(
                simulated_handle_buy_critical_section(), timeout=2.0,
            )
            await _drain_pending_tasks()
            return result

        result = asyncio.run(scenario())

        self.assertEqual(
            result, "handle_buy_completed",
            "Critical section pemanggil HARUS selesai -- tidak boleh macet "
            "menunggu lock yang dipegangnya sendiri.",
        )

    def test_safety_net_refresh_still_actually_runs(self):
        """Fire-and-forget BUKAN berarti refresh didiamkan/hilang -- harus
        tetap benar-benar terpanggil, cuma tidak blocking caller-nya."""
        refresh_calls = []
        fake_self = self._build_fake_self(refresh_calls)
        trade = SimpleNamespace()

        async def scenario():
            async with fake_self._equity_lock:
                await TradingBot._on_trade_executed(fake_self, trade)
            await _drain_pending_tasks()

        asyncio.run(scenario())

        self.assertEqual(
            refresh_calls, ["refreshed"],
            "Safety-net _refresh_portfolio() harus tetap jalan setelah "
            "lock dilepas caller-nya (bukan silently dropped).",
        )

    def test_throttle_skip_path_unaffected_by_fix(self):
        """Kalau belum >= _MIN_REFRESH_INTERVAL sejak refresh terakhir,
        TIDAK ada task yang di-fork sama sekali (perilaku throttle asli
        tidak berubah oleh fix ini)."""
        import time as _t
        refresh_calls = []
        fake_self = self._build_fake_self(refresh_calls)
        fake_self._last_refresh_time = _t.monotonic()  # baru saja refresh
        trade = SimpleNamespace()

        async def scenario():
            await TradingBot._on_trade_executed(fake_self, trade)
            await _drain_pending_tasks()

        asyncio.run(scenario())

        self.assertEqual(refresh_calls, [])

    def test_second_task_not_starved_by_poisoned_lock(self):
        """[Persis insiden TOWNS] Task KEDUA (mis. kandidat LDO/HEI
        berikutnya lewat _handle_buy()) yang menunggu _equity_lock yang
        SAMA harus tetap bisa mendapatkan lock setelah task pertama
        selesai -- lock TIDAK boleh teracuni permanen."""
        refresh_calls = []
        fake_self = self._build_fake_self(refresh_calls)
        trade = SimpleNamespace()
        order = []

        async def task_a_first_candidate():
            async with fake_self._equity_lock:
                await TradingBot._on_trade_executed(fake_self, trade)
                order.append("A_done_inside_lock")

        async def task_b_second_candidate():
            async with fake_self._equity_lock:
                order.append("B_acquired_lock")

        async def scenario():
            await asyncio.wait_for(
                asyncio.gather(task_a_first_candidate(), task_b_second_candidate()),
                timeout=2.0,
            )
            await _drain_pending_tasks()

        asyncio.run(scenario())

        self.assertEqual(
            order, ["A_done_inside_lock", "B_acquired_lock"],
            "Task B harus tetap dapat lock setelah A selesai -- tidak "
            "starvation permanen spt yg terjadi pada 12 kandidat pasca-TOWNS.",
        )


class TestOldBlockingPatternDeadlockDocumentation(unittest.TestCase):
    """[Dokumentasi bug -- BUKAN test kode produksi] Reproduksi LANGSUNG
    pola LAMA (`await self._refresh_portfolio()` tanpa create_task) utk
    membuktikan mekanisme deadlock TOWNS/USDT nyata, bukan spekulasi
    teoretis. Test ini sengaja TIDAK memanggil
    TradingBot._on_trade_executed() (yang sudah diperbaiki) -- murni
    mereproduksi pola lama secara terisolasi."""

    def test_old_pattern_hangs_forever_until_timeout(self):
        refresh_calls = []
        equity_lock = asyncio.Lock()

        async def old_refresh_portfolio():
            async with equity_lock:
                refresh_calls.append("refreshed")

        async def old_on_trade_executed():
            # Pola SEBELUM fix: await langsung, bukan create_task().
            await old_refresh_portfolio()

        async def old_handle_buy_critical_section():
            async with equity_lock:
                await old_on_trade_executed()

        async def scenario():
            await asyncio.wait_for(old_handle_buy_critical_section(), timeout=0.5)

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(scenario())

        self.assertEqual(
            refresh_calls, [],
            "Dengan pola lama, refresh TIDAK PERNAH sempat jalan -- macet "
            "selamanya menunggu lock yang dipegang task itu sendiri.",
        )


class TestHandleBuyEquityLockStillWideAigensynPyrRegression(unittest.TestCase):
    """[Non-regresi -- kasus AIGENSYN & PYR] Fix deadlock di
    _on_trade_executed() TIDAK menyentuh scope _equity_lock di
    _handle_buy() sama sekali -- execute_signal() HARUS tetap terbungkus
    lock, supaya _refresh_portfolio() konkuren (dari
    run_portfolio_monitor()) TIDAK bisa nyelip membaca saldo yang sudah
    berkurang (fill sudah terjadi) tapi posisi belum tercatat di DB."""

    def _build_fake_self(self, risk_manager, execute_signal_side_effect):
        fake_self = SimpleNamespace()
        fake_self.portfolio_state = {"total_equity": 10000.0}
        fake_self.config = {"max_position_size_pct": 10.0}
        fake_self.risk_manager = risk_manager
        fake_self._equity_lock = asyncio.Lock()
        fake_self.strategy = SimpleNamespace(
            _lock=MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False)),
            _last_entry_params={"TEST/USDT": {"exit_mode": None, "p": {"dummy": True}}},
            _pos_trackers={},
            register_position=MagicMock(),
        )
        fake_self.exchange = SimpleNamespace(get_min_order_cost=MagicMock(return_value=None))

        fake_self.executor = SimpleNamespace(
            execute_signal=AsyncMock(side_effect=execute_signal_side_effect),
        )

        fake_db = SimpleNamespace()
        fake_db.upsert_position = AsyncMock()
        fake_db.update_position_entry_fee = AsyncMock()
        fake_db.link_latest_signal_to_trade = AsyncMock(return_value=True)
        fake_db.get_open_position_by_symbol = AsyncMock(return_value=SimpleNamespace())
        fake_db.save_log = AsyncMock()
        fake_self.db = fake_db

        fake_self.notifier = SimpleNamespace(
            notify_trade_opened=AsyncMock(), notify_projection=AsyncMock(),
        )

        fake_self._refresh_portfolio = AsyncMock()

        return fake_self

    def test_concurrent_refresh_blocked_until_handle_buy_finishes(self):
        rm = RiskManager({"max_open_positions": 2})
        rm.update_portfolio_state(
            equity=10000.0, initial_equity=10000.0,
            free_balance=10000.0, open_positions_count=0,
        )

        order = []

        async def slow_execute_signal(signal, assessment):
            # Simulasikan fill exchange terjadi LALU ada await point
            # (save_trade/notifikasi) sebelum upsert_position() --
            # persis skenario asli AIGENSYN & PYR.
            order.append("fill_happened")
            await asyncio.sleep(0.05)
            return _make_trade()

        fake_self = self._build_fake_self(rm, slow_execute_signal)

        async def competing_refresh_portfolio_monitor():
            # Beri _handle_buy() kesempatan mulai & memegang lock dulu.
            await asyncio.sleep(0.01)
            async with fake_self._equity_lock:
                order.append("refresh_ran")

        async def scenario():
            await asyncio.wait_for(
                asyncio.gather(
                    TradingBot._handle_buy(fake_self, _make_signal()),
                    competing_refresh_portfolio_monitor(),
                ),
                timeout=2.0,
            )

        asyncio.run(scenario())

        self.assertEqual(order[0], "fill_happened")
        self.assertEqual(
            order[-1], "refresh_ran",
            "_refresh_portfolio() konkuren HARUS menunggu sampai "
            "_handle_buy() (termasuk upsert_position()) selesai -- kalau "
            "nyelip di antara fill & upsert, itu regresi kasus AIGENSYN & PYR.",
        )


if __name__ == "__main__":
    unittest.main()
