"""
engine/test_event_bus.py -- Test untuk EventBus (audit item #8, langkah 1/4).

Cakupan:
1. Publish/subscribe dasar -- subscriber menerima event yang dipublish.
2. Multi-subscriber -- semua subscriber aktif menerima event yang sama.
3. Unsubscribe -- subscriber yang sudah unsubscribe TIDAK menerima event
   berikutnya, dan bus tidak crash walau ada subscriber yang sudah pergi.
4. Publish tanpa subscriber -- no-op aman, tidak error.
5. [Regresi kunci] publish() TIDAK PERNAH block -- verifikasi lewat queue
   penuh (drop-oldest), dan verifikasi publish() genuinely fungsi sinkron
   (bukan coroutine) supaya caller (database.py Tier 1) tidak perlu await.
6. Context manager `async with bus.subscribe()` auto-unsubscribe.
7. Isolasi antar-EventBus instance (spot vs futures, 2 instance terpisah
   tidak saling bocor).

    python3 -m unittest engine.test_event_bus -v
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

from datetime import datetime, timezone
from types import SimpleNamespace

import time

from engine.event_bus import Event, EventBus, KeyedLogThrottle, ThrottledTickerPublisher, serialize_event


class TestEventBusPublishSubscribe(unittest.TestCase):

    def test_subscriber_receives_published_event(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            bus.publish("trade", {"symbol": "BTC/USDT"}, market_type="spot")
            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertIsInstance(event, Event)
            self.assertEqual(event.type, "trade")
            self.assertEqual(event.data, {"symbol": "BTC/USDT"})
            self.assertEqual(event.market_type, "spot")
            self.assertIsInstance(event.ts, float)

        asyncio.run(scenario())

    def test_multiple_subscribers_all_receive_same_event(self):
        async def scenario():
            bus = EventBus()
            sub_a = bus.subscribe()
            sub_b = bus.subscribe()
            bus.publish("position_opened", {"id": 1})

            ea = await asyncio.wait_for(sub_a.queue.get(), timeout=1.0)
            eb = await asyncio.wait_for(sub_b.queue.get(), timeout=1.0)
            self.assertEqual(ea.data, {"id": 1})
            self.assertEqual(eb.data, {"id": 1})

        asyncio.run(scenario())

    def test_publish_with_no_subscribers_is_safe_noop(self):
        bus = EventBus()
        bus.publish("trade", {"x": 1})  # tidak boleh raise

    def test_unsubscribed_subscriber_gets_nothing_further(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            sub.unsubscribe()
            bus.publish("trade", {"x": 1})
            self.assertEqual(bus.subscriber_count, 0)
            self.assertTrue(sub.queue.empty())

        asyncio.run(scenario())

    def test_unsubscribe_one_does_not_affect_others(self):
        async def scenario():
            bus = EventBus()
            sub_a = bus.subscribe()
            sub_b = bus.subscribe()
            sub_a.unsubscribe()
            bus.publish("trade", {"x": 1})

            self.assertTrue(sub_a.queue.empty())
            eb = await asyncio.wait_for(sub_b.queue.get(), timeout=1.0)
            self.assertEqual(eb.data, {"x": 1})

        asyncio.run(scenario())

    def test_double_unsubscribe_does_not_crash(self):
        bus = EventBus()
        sub = bus.subscribe()
        sub.unsubscribe()
        sub.unsubscribe()  # tidak boleh raise KeyError dkk

    def test_async_context_manager_auto_unsubscribes(self):
        async def scenario():
            bus = EventBus()
            async with bus.subscribe() as sub:
                bus.publish("trade", {"x": 1})
                await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                self.assertEqual(bus.subscriber_count, 1)
            self.assertEqual(bus.subscriber_count, 0)

        asyncio.run(scenario())

    def test_subscriber_count_reflects_active_subscriptions(self):
        bus = EventBus()
        self.assertEqual(bus.subscriber_count, 0)
        s1 = bus.subscribe()
        s2 = bus.subscribe()
        self.assertEqual(bus.subscriber_count, 2)
        s1.unsubscribe()
        self.assertEqual(bus.subscriber_count, 1)
        s2.unsubscribe()
        self.assertEqual(bus.subscriber_count, 0)


class TestEventBusNonBlockingBackpressure(unittest.TestCase):
    """[Regresi kunci -- prasyarat desain] publish() TIDAK PERNAH boleh
    block, bahkan kalau subscriber lambat/macet."""

    def test_publish_is_synchronous_function_not_coroutine(self):
        """Caller (database.py Tier 1) memanggil publish() TANPA await --
        kalau publish() ternyata coroutine, caller yang lupa await akan
        silently no-op (bug klasik 'coroutine was never awaited')."""
        bus = EventBus()
        self.assertFalse(
            inspect.iscoroutinefunction(bus.publish),
            "publish() harus fungsi sinkron biasa, bukan async def -- "
            "supaya caller yang manggil tanpa await tidak silently no-op.",
        )

    def test_full_queue_drops_oldest_not_newest(self):
        async def scenario():
            bus = EventBus(queue_maxsize=2)
            sub = bus.subscribe()
            bus.publish("t", "first")
            bus.publish("t", "second")
            bus.publish("t", "third")  # queue penuh (maxsize=2) -- drop "first"

            self.assertEqual(sub.queue.qsize(), 2)
            e1 = sub.queue.get_nowait()
            e2 = sub.queue.get_nowait()
            self.assertEqual([e1.data, e2.data], ["second", "third"])

        asyncio.run(scenario())

    def test_slow_subscriber_does_not_block_publish_to_others(self):
        async def scenario():
            bus = EventBus(queue_maxsize=1)
            slow_sub = bus.subscribe()  # sengaja TIDAK PERNAH di-drain di sini
            fast_sub = bus.subscribe()  # di-drain tiap publish, jadi tidak pernah overflow

            bus.publish("t", "a")
            fast_sub.queue.get_nowait()
            bus.publish("t", "b")  # slow_sub overflow di sini (drop-oldest "a"), publish tetap sukses tanpa raise
            fast_sub.queue.get_nowait()

            # slow_sub cuma nyimpan event TERBARU (drop-oldest), bukan "a" yang pertama.
            self.assertEqual(slow_sub.queue.qsize(), 1)
            self.assertEqual(slow_sub.queue.get_nowait().data, "b")

        asyncio.run(scenario())

    def test_many_publishes_never_raise_even_with_tiny_queue(self):
        async def scenario():
            bus = EventBus(queue_maxsize=1)
            bus.subscribe()
            for i in range(500):
                bus.publish("t", i)  # tidak boleh raise/hang walau queue selalu penuh

        asyncio.run(scenario())


class TestEventBusInstanceIsolation(unittest.TestCase):
    """[Regresi kunci] 2 instance EventBus (spot vs futures) TIDAK boleh
    saling bocor -- masing-masing bot 1 instance in-process sendiri."""

    def test_two_independent_buses_do_not_share_subscribers(self):
        async def scenario():
            bus_spot    = EventBus()
            bus_futures = EventBus()
            sub_spot    = bus_spot.subscribe()
            sub_futures = bus_futures.subscribe()

            bus_spot.publish("trade", {"m": "spot"}, market_type="spot")

            e = await asyncio.wait_for(sub_spot.queue.get(), timeout=1.0)
            self.assertEqual(e.market_type, "spot")
            self.assertTrue(sub_futures.queue.empty(), "bus futures tidak boleh ikut terima event bus spot")

        asyncio.run(scenario())


class TestThrottledTickerPublisher(unittest.TestCase):
    """[Audit item #8, langkah 3/4] Wrapper untuk WebSocketFeed.on_ticker
    -- hook SUDAH ADA di exchange_spot.py tapi TIDAK PERNAH disambungkan
    sebelum ini (dikonfirmasi grep). Throttle per symbol -- Binance WS
    bisa kirim tick beberapa kali/detik, publish mentah akan banjir."""

    def test_first_tick_for_symbol_publishes_immediately(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            pub = ThrottledTickerPublisher(bus, market_type="spot", throttle_interval_secs=1.5)

            await pub("BTC/USDT", {"last": 100.0})

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "ticker")
            self.assertEqual(event.market_type, "spot")
            self.assertEqual(event.data["symbol"], "BTC/USDT")
            self.assertEqual(event.data["last"], 100.0)

        asyncio.run(scenario())

    def test_rapid_ticks_same_symbol_throttled_to_one_publish(self):
        """[Regresi kunci] Beberapa tick beruntun utk symbol yang SAMA
        dalam window throttle HARUS cuma menghasilkan 1 publish."""
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            pub = ThrottledTickerPublisher(bus, market_type="spot", throttle_interval_secs=60.0)

            for i in range(20):
                await pub("BTC/USDT", {"last": 100.0 + i})

            self.assertEqual(sub.queue.qsize(), 1, "20 tick beruntun harus cuma 1 publish dlm window throttle")

        asyncio.run(scenario())

    def test_different_symbols_throttled_independently(self):
        """[Regresi kunci] Throttle PER SYMBOL -- symbol A yang baru saja
        publish tidak boleh menahan symbol B yang belum pernah publish."""
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            pub = ThrottledTickerPublisher(bus, market_type="spot", throttle_interval_secs=60.0)

            await pub("BTC/USDT", {"last": 100.0})
            await pub("ETH/USDT", {"last": 2000.0})  # symbol beda, harus tetap publish

            self.assertEqual(sub.queue.qsize(), 2)

        asyncio.run(scenario())

    def test_tick_after_throttle_window_publishes_again(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            pub = ThrottledTickerPublisher(bus, market_type="spot", throttle_interval_secs=0.05)

            await pub("BTC/USDT", {"last": 100.0})
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)

            await asyncio.sleep(0.08)  # lewati window throttle
            await pub("BTC/USDT", {"last": 101.0})

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.data["last"], 101.0)

        asyncio.run(scenario())

    def test_market_type_tagged_correctly_futures(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            pub = ThrottledTickerPublisher(bus, market_type="futures")

            await pub("BTC/USDT", {"last": 50000.0})

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.market_type, "futures")

        asyncio.run(scenario())

    def test_is_awaitable_callable_matches_on_ticker_signature(self):
        """WebSocketFeed memanggil `await self.on_ticker(symbol, ticker)`
        -- pastikan instance genuinely awaitable dgn signature itu."""
        bus = EventBus()
        pub = ThrottledTickerPublisher(bus, market_type="spot")
        self.assertTrue(asyncio.iscoroutinefunction(pub.__call__))


class TestKeyedLogThrottle(unittest.TestCase):
    """[#23 -- audit fungsional] Rate-limiter generik per-key -- pola SAMA
    persis dgn ThrottledTickerPublisher di atas (item #8), dipakai utk
    throttle log INFO Gate4 ([ScoreThreshold]) di spot & future
    (main_spot.py/main_future.py) supaya terlihat tanpa DEBUG logging tapi
    tidak membanjiri (Gate4 = titik reject volume tertinggi di pipeline).
    BEDA dari ThrottledTickerPublisher: sinkron (bukan `async def`), dan
    key boleh str (spot, symbol saja) ATAU tuple (future, (symbol, side) --
    supaya reject long tidak menahan visibilitas reject short)."""

    def test_first_call_for_key_allowed(self):
        throttle = KeyedLogThrottle(interval_secs=60.0)
        self.assertTrue(throttle.allow("BTC/USDT"))

    def test_repeated_calls_same_key_within_window_throttled_to_once(self):
        """[Regresi kunci] Simbol yang gagal berulang dalam window HARUS
        cuma diizinkan log 1x -- panggilan berikutnya dalam window sama
        harus ditolak (False)."""
        throttle = KeyedLogThrottle(interval_secs=60.0)
        results = [throttle.allow("BTC/USDT") for _ in range(20)]
        self.assertEqual(results[0], True)
        self.assertTrue(all(r is False for r in results[1:]), "20 panggilan beruntun harus cuma 1 yang allow=True")

    def test_different_keys_throttled_independently(self):
        """[Regresi kunci] Throttle PER KEY -- key A yang baru saja lolos
        tidak boleh menahan key B yang belum pernah lolos (symbol beda,
        spot-style str key)."""
        throttle = KeyedLogThrottle(interval_secs=60.0)
        self.assertTrue(throttle.allow("BTC/USDT"))
        self.assertTrue(throttle.allow("ETH/USDT"))

    def test_tuple_keys_same_symbol_different_side_throttled_independently(self):
        """[Regresi kunci -- futures-specific] Key tuple (symbol, side) --
        reject 'long' utk simbol X TIDAK BOLEH menahan visibilitas reject
        'short' utk simbol X yang sama (kelas bug sama persis dgn item #25
        _SIGNAL_CONFIRM_BUFFER: cross-side contamination kalau di-key
        symbol saja)."""
        throttle = KeyedLogThrottle(interval_secs=60.0)
        self.assertTrue(throttle.allow(("BTC/USDT", "long")))
        self.assertTrue(throttle.allow(("BTC/USDT", "short")))
        # Panggilan kedua utk side yang SAMA tetap harus ditolak (masih dlm window).
        self.assertFalse(throttle.allow(("BTC/USDT", "long")))
        self.assertFalse(throttle.allow(("BTC/USDT", "short")))

    def test_allowed_again_after_window_elapses(self):
        """[Regresi kunci] Setelah window throttle lewat, key yang sama
        HARUS diizinkan log lagi (bukan ditahan permanen)."""
        throttle = KeyedLogThrottle(interval_secs=0.05)
        self.assertTrue(throttle.allow("BTC/USDT"))
        self.assertFalse(throttle.allow("BTC/USDT"))

        time.sleep(0.08)  # lewati window throttle

        self.assertTrue(throttle.allow("BTC/USDT"), "harus allow lagi setelah window berlalu")


def _iso(dt):
    return dt.isoformat() if dt else None


def _pos_dict(pos):
    return {"symbol": pos.symbol, "side": pos.side, "SERIALIZED_BY": "pos_dict_fn"}


def _trade_dict(trade):
    return {"symbol": trade.symbol, "SERIALIZED_BY": "trade_dict_fn"}


class TestSerializeEvent(unittest.TestCase):
    """[Audit item #8, langkah 4/4] Dispatch serialisasi event.type ->
    payload siap-JSON. pos_dict_fn/trade_dict_fn DISUNTIK (bukan hardcoded
    di engine/) -- ini yang membuktikan payload tidak dipaksa 1 skema kaku
    (spot vs futures bisa beda field, engine/ tidak perlu tahu)."""

    def _serialize(self, event):
        return serialize_event(event, pos_dict_fn=_pos_dict, trade_dict_fn=_trade_dict, iso_fn=_iso)

    def test_trade_event_uses_injected_trade_dict_fn(self):
        trade = SimpleNamespace(symbol="BTC/USDT")
        event = Event(type="trade", data=trade, market_type="spot")
        result = self._serialize(event)
        self.assertEqual(result["data"]["SERIALIZED_BY"], "trade_dict_fn")
        self.assertEqual(result["type"], "trade")
        self.assertEqual(result["market_type"], "spot")

    def test_position_upserted_uses_injected_pos_dict_fn(self):
        pos = SimpleNamespace(symbol="BTC/USDT", side="long")
        event = Event(type="position_upserted", data=pos)
        result = self._serialize(event)
        self.assertEqual(result["data"]["SERIALIZED_BY"], "pos_dict_fn")

    def test_position_closed_uses_injected_pos_dict_fn(self):
        pos = SimpleNamespace(symbol="BTC/USDT", side="short")
        event = Event(type="position_closed", data=pos)
        result = self._serialize(event)
        self.assertEqual(result["data"]["SERIALIZED_BY"], "pos_dict_fn")

    def test_positions_snapshot_serializes_each_position_in_list(self):
        positions = [SimpleNamespace(symbol="A/USDT", side="long"),
                     SimpleNamespace(symbol="B/USDT", side="short")]
        event = Event(type="positions_snapshot", data=positions)
        result = self._serialize(event)
        self.assertEqual(len(result["data"]), 2)
        self.assertTrue(all(d["SERIALIZED_BY"] == "pos_dict_fn" for d in result["data"]))

    def test_position_closing_passthrough_minimal_dict(self):
        event = Event(type="position_closing", data={"symbol": "BTC/USDT"})
        result = self._serialize(event)
        self.assertEqual(result["data"], {"symbol": "BTC/USDT"})

    def test_universe_override_added_serializes_orm_fields(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        override = SimpleNamespace(symbol="SOL/USDT", source="api", is_active=True, added_at=now, notes="test")
        event = Event(type="universe_override_added", data=override)
        result = self._serialize(event)
        self.assertEqual(result["data"]["symbol"], "SOL/USDT")
        self.assertEqual(result["data"]["is_active"], True)
        self.assertIsNotNone(result["data"]["added_at"])

    def test_universe_override_removed_passthrough_minimal_dict(self):
        event = Event(type="universe_override_removed", data={"symbol": "SOL/USDT"})
        result = self._serialize(event)
        self.assertEqual(result["data"], {"symbol": "SOL/USDT"})

    def test_parameter_changed_serializes_orm_fields(self):
        rec = SimpleNamespace(
            id=1, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", profile="scalp_volatile", parameter_name="entry_threshold",
            old_value="60.0", new_value="58.0", reason="test", approved_by="manual_api", outcome="pending",
        )
        event = Event(type="parameter_changed", data=rec)
        result = self._serialize(event)
        self.assertEqual(result["data"]["parameter_name"], "entry_threshold")
        self.assertEqual(result["data"]["old_value"], "60.0")

    def test_snapshot_serializes_orm_fields_matching_equity_curve_endpoint_shape(self):
        snap = SimpleNamespace(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_equity=1000.0, drawdown_pct=2.5, daily_pnl=10.0, daily_pnl_pct=1.0,
        )
        event = Event(type="snapshot", data=snap)
        result = self._serialize(event)
        self.assertEqual(result["data"]["equity"], 1000.0)
        self.assertEqual(result["data"]["drawdown"], 2.5)

    def test_ticker_passthrough_dict(self):
        event = Event(type="ticker", data={"symbol": "BTC/USDT", "last": 100.0}, market_type="futures")
        result = self._serialize(event)
        self.assertEqual(result["data"]["last"], 100.0)
        self.assertEqual(result["market_type"], "futures")

    def test_halt_changed_passthrough_dict(self):
        event = Event(type="halt_changed", data={"halted": True, "reason": "manual_halt", "detail": ""})
        result = self._serialize(event)
        self.assertTrue(result["data"]["halted"])

    def test_unknown_event_type_falls_back_to_raw_dict_or_str(self):
        event = Event(type="some_future_event_type", data={"x": 1})
        result = self._serialize(event)
        self.assertEqual(result["data"], {"x": 1})

    def test_envelope_always_has_type_market_type_ts_data(self):
        event = Event(type="halt_changed", data={"halted": False, "reason": "", "detail": ""}, market_type="spot")
        result = self._serialize(event)
        self.assertEqual(set(result.keys()), {"type", "market_type", "ts", "data"})


if __name__ == "__main__":
    unittest.main()
