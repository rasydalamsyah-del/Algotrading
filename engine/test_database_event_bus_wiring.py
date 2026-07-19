"""
engine/test_database_event_bus_wiring.py -- Test untuk wiring EventBus di
engine/database.py (audit item #8, langkah 2/4).

Titik Tier 1 yang di-publish (dikonfirmasi lewat investigasi frekuensi
sebelumnya -- save_signal_score/save_market_regime/save_log SENGAJA TIDAK
di-publish, terlalu sering, di luar cakupan test file ini):
  save_trade(), upsert_position(), close_position() (+ close_position_
  with_retry() lewat method yang sama), mark_position_closing(),
  upsert_universe_override(), deactivate_universe_override(),
  save_parameter_change().

Pakai DatabaseManager REAL (sqlite in-memory), BUKAN mock -- supaya
genuinely membuktikan publish() terpicu dari jalur commit DB sungguhan,
bukan cuma dari baca kode.

    python3 -m unittest engine.test_database_event_bus_wiring -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from engine.database import DatabaseManager
from engine.event_bus import EventBus


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_db() -> DatabaseManager:
    db = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db


class TestEventBusWiringOptionalDefault(unittest.TestCase):

    def test_event_bus_defaults_to_none_no_crash_without_it(self):
        """[Non-regresi kunci] DatabaseManager TANPA event_bus di-assign
        HARUS tetap bekerja normal -- caller lama (test lain, script,
        migrasi) yang tidak tahu soal EventBus tidak boleh terpengaruh."""
        async def scenario():
            db = await _make_db()
            self.assertIsNone(db.event_bus)
            trade = await db.save_trade({
                "order_id": "T1", "symbol": "BTC/USDT", "side": "buy",
                "order_type": "market", "status": "filled", "amount": 1.0,
            })
            self.assertIsNotNone(trade)

        asyncio.run(scenario())


class TestSaveTradePublish(unittest.TestCase):

    def test_new_trade_publishes_trade_event(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            trade = await db.save_trade({
                "order_id": "T1", "symbol": "BTC/USDT", "side": "buy",
                "order_type": "market", "status": "filled", "amount": 1.0,
            })

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "trade")
            self.assertIs(event.data, trade)

        asyncio.run(scenario())

    def test_duplicate_order_id_does_not_publish_again(self):
        """[Regresi kunci] Jalur race-recovery (`return existing`) BUKAN
        trade baru -- tidak boleh ikut memicu publish kedua."""
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            trade_data = {
                "order_id": "T-DUP", "symbol": "BTC/USDT", "side": "buy",
                "order_type": "market", "status": "filled", "amount": 1.0,
            }
            await db.save_trade(trade_data)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)

            await db.save_trade(dict(trade_data))  # order_id sama -- duplikat
            self.assertTrue(sub.queue.empty(), "duplikat tidak boleh publish event kedua")

        asyncio.run(scenario())


class TestPositionLifecyclePublish(unittest.TestCase):

    def test_upsert_position_publishes_position_upserted(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            pos = await db.upsert_position("BTC/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0,
                "amount": 1.0, "side": "long", "is_open": True,
            })

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "position_upserted")
            self.assertIs(event.data, pos)

        asyncio.run(scenario())

    def test_mark_position_closing_publishes_minimal_dict(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus

            await db.upsert_position("BTC/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0,
                "amount": 1.0, "side": "long", "is_open": True,
            })
            sub = bus.subscribe()  # subscribe SETELAH upsert, biar queue bersih

            await db.mark_position_closing("BTC/USDT")

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "position_closing")
            self.assertEqual(event.data, {"symbol": "BTC/USDT"})

        asyncio.run(scenario())

    def test_close_position_publishes_position_closed_with_realized_pnl(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus

            await db.upsert_position("BTC/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0,
                "amount": 1.0, "side": "long", "is_open": True,
            })
            sub = bus.subscribe()

            closed = await db.close_position("BTC/USDT", exit_price=110.0, realized_pnl=10.0)

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "position_closed")
            self.assertIs(event.data, closed)
            self.assertEqual(event.data.realized_pnl, 10.0)

        asyncio.run(scenario())

    def test_close_position_no_open_position_does_not_publish(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            result = await db.close_position("NOEXIST/USDT", exit_price=1.0, realized_pnl=0.0)

            self.assertIsNone(result)
            self.assertTrue(sub.queue.empty())

        asyncio.run(scenario())

    def test_close_position_with_retry_publishes_via_underlying_method(self):
        """[Regresi kunci] close_position_with_retry() (item #4) memanggil
        close_position() internal -- publish HARUS tetap terpicu lewat
        jalur ini juga, tanpa duplikasi wiring terpisah."""
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus

            await db.upsert_position("ETH/USDT", {
                "entry_time": _utcnow(), "entry_price": 50.0,
                "amount": 2.0, "side": "short", "is_open": True,
            })
            sub = bus.subscribe()

            closed = await db.close_position_with_retry("ETH/USDT", exit_price=45.0, realized_pnl=10.0)

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "position_closed")
            self.assertIs(event.data, closed)

        asyncio.run(scenario())


class TestUniverseOverridePublish(unittest.TestCase):

    def test_add_publishes_universe_override_added(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            await db.upsert_universe_override("SOL/USDT", source="api", notes="test")

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "universe_override_added")
            self.assertEqual(event.data.symbol, "SOL/USDT")
            self.assertTrue(event.data.is_active)

        asyncio.run(scenario())

    def test_remove_publishes_universe_override_removed_minimal_dict(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            await db.upsert_universe_override("SOL/USDT", source="api")
            sub = bus.subscribe()

            await db.deactivate_universe_override("SOL/USDT")

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "universe_override_removed")
            self.assertEqual(event.data, {"symbol": "SOL/USDT"})

        asyncio.run(scenario())


class TestParameterChangePublish(unittest.TestCase):

    def test_save_parameter_change_publishes_event(self):
        async def scenario():
            db = await _make_db()
            bus = EventBus()
            db.event_bus = bus
            sub = bus.subscribe()

            row_id = await db.save_parameter_change(
                symbol="BTC/USDT", profile="scalp_volatile",
                parameter_name="entry_threshold",
                old_value=60.0, new_value=58.0,
                reason="test", approved_by="manual_api",
            )

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "parameter_changed")
            self.assertEqual(event.data.id, row_id)
            self.assertEqual(event.data.parameter_name, "entry_threshold")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
