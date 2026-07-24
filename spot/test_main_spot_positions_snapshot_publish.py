"""
spot/test_main_spot_positions_snapshot_publish.py -- Test untuk publish
event "positions_snapshot" teragregasi di run_sl_tp_monitor()
(spot/main_spot.py, audit item #8, Tier 2).

[LATAR BELAKANG] update_position_price() dipanggil PER-POSISI di dalam
loop run_sl_tp_monitor() (tiap SL_TP_CHECK_INTERVAL=5 detik) -- publish 1
event per posisi per write akan flood (persis pola yang dihindari sesuai
desain #8, dikonfirmasi user). Fix: SATU query segar (get_open_positions())
+ SATU publish "positions_snapshot" di akhir tiap cycle, HANYA kalau ada
posisi terbuka.

    python3 -m unittest spot.test_main_spot_positions_snapshot_publish -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.event_bus import EventBus
from engine.risk_base import BaseRiskManager
from spot.main_spot import TradingBot


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=50.0,
        stop_loss_price=45.0, take_profit_price=100.0,
        highest_price=50.0, atr_at_entry=1.0,
        strategy_profile="mean_revert", entry_regime="undefined",
        entry_score=None, amount=1.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeDB:
    def __init__(self, positions, fresh_positions=None):
        self._positions = positions
        self._fresh_positions = fresh_positions if fresh_positions is not None else positions
        self.get_open_positions_call_count = 0
        self.get_latest_signal_score = AsyncMock(return_value=None)

    async def get_open_positions(self):
        self.get_open_positions_call_count += 1
        if self.get_open_positions_call_count == 1:
            return self._positions
        return self._fresh_positions

    async def update_position_sl(self, symbol, new_sl):
        pass

    async def update_position_highest_price(self, symbol, price):
        pass

    async def update_position_price(self, symbol, price, upnl, upnl_pct):
        pass


def _build_fake_self(positions, price_map, event_bus=None, fresh_positions=None):
    fake_self = SimpleNamespace()
    # [DOUBLE-COUNT FIX] _do_close_position() kini memegang _equity_lock
    # (mirror _handle_entry) -- stub wajib menyediakannya.
    fake_self._equity_lock = asyncio.Lock()
    fake_self.is_running = True
    fake_self.SL_TP_CHECK_INTERVAL = 0
    fake_self.db = _FakeDB(positions, fresh_positions=fresh_positions)
    fake_self._closing_lock = asyncio.Lock()
    fake_self._closing_symbols = set()
    fake_self.risk_manager = BaseRiskManager({})
    fake_self.exchange = SimpleNamespace(fetch_ohlcv=AsyncMock(return_value=[]))
    fake_self.strategy = None
    fake_self.notifier = SimpleNamespace(notify_sl_tp_hit=AsyncMock())
    fake_self.event_bus = event_bus if event_bus is not None else EventBus()

    async def _get_price(symbol):
        return price_map.get(symbol)
    fake_self._get_current_price = _get_price

    async def _close(pos, price, reason):
        pass
    fake_self._close_position_market = _close

    orig_get_positions = fake_self.db.get_open_positions

    async def _get_positions_and_stop():
        fake_self.is_running = False
        return await orig_get_positions()
    fake_self.db.get_open_positions = _get_positions_and_stop

    return fake_self


class TestPositionsSnapshotPublish(unittest.TestCase):

    def test_publishes_positions_snapshot_when_positions_open(self):
        bus = EventBus()
        sub = bus.subscribe()
        pos = _make_position()
        fake_self = _build_fake_self([pos], {"TEST/USDT": 55.0}, event_bus=bus)

        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        found = False
        while not sub.queue.empty():
            event = sub.queue.get_nowait()
            if event.type == "positions_snapshot":
                found = True
                self.assertEqual(event.market_type, "spot")
                self.assertEqual(len(event.data), 1)
        self.assertTrue(found, "harus ada event positions_snapshot setelah 1 cycle dgn posisi terbuka")

    def test_uses_fresh_query_not_stale_in_memory_objects(self):
        """[Regresi kunci] update_position_price() adalah UPDATE massal
        (tidak RETURNING) -- objek `positions` awal TIDAK ter-refresh.
        Snapshot yang dipublish HARUS dari query segar (harga ter-update),
        bukan objek lama di memori."""
        bus = EventBus()
        sub = bus.subscribe()
        stale_pos = _make_position(current_price=50.0)
        fresh_pos = _make_position(current_price=55.0)  # simulasi row setelah update_position_price()
        fake_self = _build_fake_self(
            [stale_pos], {"TEST/USDT": 55.0}, event_bus=bus, fresh_positions=[fresh_pos],
        )

        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        snapshot_event = None
        while not sub.queue.empty():
            event = sub.queue.get_nowait()
            if event.type == "positions_snapshot":
                snapshot_event = event
        self.assertIsNotNone(snapshot_event)
        self.assertEqual(snapshot_event.data[0].current_price, 55.0)

    def test_no_publish_when_no_open_positions(self):
        bus = EventBus()
        sub = bus.subscribe()
        fake_self = _build_fake_self([], {}, event_bus=bus)

        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        self.assertTrue(sub.queue.empty(), "tidak boleh publish snapshot kosong kalau tidak ada posisi")

    def test_does_not_publish_per_position_individually(self):
        """[Regresi kunci -- prasyarat desain #8] update_position_price()
        di dalam loop TIDAK boleh memicu publish sendiri-sendiri -- cuma
        SATU event positions_snapshot per cycle, terlepas jumlah posisi."""
        bus = EventBus()
        sub = bus.subscribe()
        positions = [_make_position(symbol=f"COIN{i}/USDT") for i in range(5)]
        price_map = {f"COIN{i}/USDT": 55.0 for i in range(5)}
        fake_self = _build_fake_self(positions, price_map, event_bus=bus)

        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        snapshot_events = []
        while not sub.queue.empty():
            event = sub.queue.get_nowait()
            if event.type == "positions_snapshot":
                snapshot_events.append(event)
        self.assertEqual(len(snapshot_events), 1, "5 posisi 1 cycle harus tetap cuma 1 event, bukan 5")


if __name__ == "__main__":
    unittest.main()
