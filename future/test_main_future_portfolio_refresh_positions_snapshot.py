"""
future/test_main_future_portfolio_refresh_positions_snapshot.py -- Test
untuk publish event "positions_snapshot" teragregasi di
_refresh_portfolio() (future/main_future.py, audit item #8, Tier 2).

[BEDA STRUKTURAL DARI SPOT -- ditemukan saat investigasi] Spot
update_position_price() per-posisi di dalam run_sl_tp_monitor() (tiap
SL_TP_CHECK_INTERVAL=5 detik tetap). Futures TIDAK punya update_position_
price() di run_sl_tp_monitor() sama sekali (dikonfirmasi grep) --
update-nya terjadi DI DALAM _refresh_portfolio() (dipanggil dari
run_portfolio_monitor() tiap SNAPSHOT_INTERVAL=900 detik, PLUS
event-triggered post-entry/post-close/_on_trade_executed throttled >=5s).
Titik publish teragregasi utk futures HARUS di sini, bukan meniru
run_sl_tp_monitor() spot mentah-mentah.

    python3 -m unittest future.test_main_future_portfolio_refresh_positions_snapshot -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.event_bus import EventBus
from engine.risk_base import BaseRiskManager
from future.main_future import TradingBot


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=50.0,
        current_price=50.0, amount=1.0, atr_at_entry=1.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_fake_self(positions, price_map, event_bus=None, fresh_positions=None):
    fake_self = SimpleNamespace()
    fake_self._equity_lock = asyncio.Lock()
    fake_self.config = {"quote_currency": "USDT", "initial_capital": 1000.0}
    fake_self.portfolio_state = {}
    fake_self.risk_manager = BaseRiskManager({})
    fake_self.risk_manager.update_portfolio_state(
        equity=1000.0, initial_equity=1000.0, free_balance=1000.0, open_positions_count=0,
    )
    fake_self.notifier = None
    fake_self.event_bus = event_bus if event_bus is not None else EventBus()

    fake_self.exchange = SimpleNamespace(
        fetch_balance=AsyncMock(return_value={
            "free": {"USDT": 500.0}, "used": {"USDT": 200.0},
        }),
    )

    call_count = {"n": 0}
    resolved_fresh = fresh_positions if fresh_positions is not None else positions

    async def _get_open_positions():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return positions
        return resolved_fresh

    fake_self.db = SimpleNamespace(
        get_open_positions=_get_open_positions,
        update_position_price=AsyncMock(),
        save_snapshot=AsyncMock(),
    )

    async def _get_price(symbol):
        return price_map.get(symbol)
    fake_self._get_current_price = _get_price

    return fake_self


class TestPositionsSnapshotPublishFutures(unittest.TestCase):

    def test_publishes_positions_snapshot_when_positions_open(self):
        bus = EventBus()
        sub = bus.subscribe()
        pos = _make_position()
        fake_self = _build_fake_self([pos], {"TEST/USDT": 55.0}, event_bus=bus)

        asyncio.run(TradingBot._refresh_portfolio(fake_self))

        found = False
        while not sub.queue.empty():
            event = sub.queue.get_nowait()
            if event.type == "positions_snapshot":
                found = True
                self.assertEqual(event.market_type, "futures")
                self.assertEqual(len(event.data), 1)
        self.assertTrue(found)

    def test_uses_fresh_query_not_stale_in_memory_objects(self):
        bus = EventBus()
        sub = bus.subscribe()
        stale_pos = _make_position(current_price=50.0)
        fresh_pos = _make_position(current_price=55.0)
        fake_self = _build_fake_self(
            [stale_pos], {"TEST/USDT": 55.0}, event_bus=bus, fresh_positions=[fresh_pos],
        )

        asyncio.run(TradingBot._refresh_portfolio(fake_self))

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

        asyncio.run(TradingBot._refresh_portfolio(fake_self))

        for _ in range(sub.queue.qsize()):
            event = sub.queue.get_nowait()
            self.assertNotEqual(event.type, "positions_snapshot")

    def test_snapshot_event_also_published_alongside_positions_snapshot(self):
        """[Integrasi] save_snapshot() (Tier 2 lain, wired di database.py)
        dipanggil di dalam _refresh_portfolio() juga -- pastikan keduanya
        (snapshot dari save_snapshot() DAN positions_snapshot) tidak saling
        mengganggu. save_snapshot() di sini di-mock (AsyncMock), jadi event
        "snapshot" TIDAK genuinely terpublish di test ini (itu tanggung
        jawab database.py, sudah diuji terpisah) -- yang diverifikasi:
        _refresh_portfolio() sendiri tidak crash & positions_snapshot tetap
        terkirim."""
        bus = EventBus()
        sub = bus.subscribe()
        pos = _make_position()
        fake_self = _build_fake_self([pos], {"TEST/USDT": 55.0}, event_bus=bus)

        asyncio.run(TradingBot._refresh_portfolio(fake_self))

        fake_self.db.save_snapshot.assert_awaited_once()
        types_seen = []
        while not sub.queue.empty():
            types_seen.append(sub.queue.get_nowait().type)
        self.assertIn("positions_snapshot", types_seen)


if __name__ == "__main__":
    unittest.main()
