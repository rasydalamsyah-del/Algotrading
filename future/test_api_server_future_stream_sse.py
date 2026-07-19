"""
future/test_api_server_future_stream_sse.py -- Test untuk GET /api/stream
di futures, genuinely event-driven (audit item #8, langkah 4/4 -- endpoint
BARU, futures sebelumnya TIDAK PUNYA /api/stream sama sekali).

Pola sama persis dgn spot/test_api_server_spot_stream_sse.py -- lihat
docstring di sana utk latar belakang lengkap kenapa desainnya
subscribe-ke-EventBus, bukan polling interval tetap.

    python3 -m unittest future.test_api_server_future_stream_sse -v
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DASHBOARD_API_KEY_FUTURES", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from engine.event_bus import EventBus
from future.api_server_future import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _make_position(symbol="BTC/USDT", **overrides):
    defaults = dict(
        id=1, symbol=symbol, side="long", entry_time=None, entry_price=100.0,
        current_price=105.0, amount=1.0, unrealized_pnl=5.0, unrealized_pnl_pct=5.0,
        realized_pnl=None, realized_pnl_pct=None, stop_loss_price=95.0,
        take_profit_price=110.0, atr_at_entry=1.0, strategy_name="test",
        strategy_profile="scalp_volatile", entry_order_id="O1", is_open=True,
        is_closing=False, entry_score=None, entry_regime=None, highest_price=None,
        exit_time=None, market_type="futures", leverage=10, margin_mode="isolated",
        liquidation_price=80.0, mark_price_at_entry=100.0, funding_paid_total=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trade(**overrides):
    defaults = dict(
        id=1, timestamp=None, symbol="BTC/USDT", side="buy", order_type="market",
        order_id="O1", status="filled", requested_price=100.0, executed_price=100.0,
        amount=1.0, filled=1.0, cost=100.0, fee_cost=0.1, fee_currency="USDT",
        fee_rate=0.001, slippage_pct=0.0, stop_loss_price=95.0, take_profit_price=110.0,
        realized_pnl=None, realized_pnl_pct=None, strategy_name="test",
        strategy_profile="scalp_volatile", signal_origin="scan", notes="",
        market_type="futures", leverage=10, margin_mode="isolated", realized_funding=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_bot(event_bus):
    return SimpleNamespace(
        event_bus=event_bus,
        db=SimpleNamespace(get_open_positions=AsyncMock(return_value=[])),
        ws_feed=SimpleNamespace(live_tickers={}),
        risk_manager=SimpleNamespace(is_halted=False),
    )


def _get_stream_endpoint(app):
    for r in app.routes:
        if getattr(r, "path", None) == "/api/stream":
            return r.endpoint
    raise AssertionError("/api/stream route not found")


class TestStreamEndpointAuth(unittest.TestCase):

    def test_requires_auth(self):
        bus = EventBus()
        app = create_app(lambda: _build_bot(bus))
        client = TestClient(app)
        r = client.get("/api/stream")
        self.assertEqual(r.status_code, 401)


class TestStreamEndpointEventDriven(unittest.TestCase):

    def test_initial_snapshot_emitted_first_tagged_futures(self):
        async def scenario():
            bus = EventBus()
            bot = _build_bot(bus)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator

            chunk = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
            text = chunk if isinstance(chunk, str) else chunk.decode()
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["type"], "initial_snapshot")
            self.assertEqual(payload["market_type"], "futures")

            await agen.aclose()

        asyncio.run(scenario())

    def test_published_position_closed_includes_futures_specific_fields(self):
        """[Regresi kunci] Payload posisi HARUS memuat field futures-only
        (leverage, margin_mode, liquidation_price) -- dari _pos_dict()
        LOKAL futures, bukan versi spot yang tidak punya field ini."""
        async def scenario():
            bus = EventBus()
            bot = _build_bot(bus)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(
                is_disconnected=AsyncMock(side_effect=[False, True]),
            )

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator
            await asyncio.wait_for(agen.__anext__(), timeout=1.0)

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0.02)
            pos = _make_position(leverage=20, margin_mode="cross")
            bus.publish("position_closed", pos, market_type="futures")

            chunk = await asyncio.wait_for(task, timeout=1.0)
            text = chunk if isinstance(chunk, str) else chunk.decode()
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["type"], "position_closed")
            self.assertEqual(payload["data"]["leverage"], 20)
            self.assertEqual(payload["data"]["margin_mode"], "cross")
            self.assertIn("liquidation_price", payload["data"])

            await agen.aclose()

        asyncio.run(scenario())

    def test_published_trade_includes_market_type_field(self):
        async def scenario():
            bus = EventBus()
            bot = _build_bot(bus)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(
                is_disconnected=AsyncMock(side_effect=[False, True]),
            )

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator
            await asyncio.wait_for(agen.__anext__(), timeout=1.0)

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0.02)
            trade = _make_trade(leverage=5)
            bus.publish("trade", trade, market_type="futures")

            chunk = await asyncio.wait_for(task, timeout=1.0)
            text = chunk if isinstance(chunk, str) else chunk.decode()
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["data"]["market_type"], "futures")
            self.assertEqual(payload["data"]["leverage"], 5)

            await agen.aclose()

        asyncio.run(scenario())

    def test_subscription_unsubscribed_after_generator_closes(self):
        async def scenario():
            bus = EventBus()
            bot = _build_bot(bus)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator
            await asyncio.wait_for(agen.__anext__(), timeout=1.0)
            self.assertEqual(bus.subscriber_count, 1)

            try:
                await asyncio.wait_for(agen.__anext__(), timeout=1.0)
            except StopAsyncIteration:
                pass

            self.assertEqual(bus.subscriber_count, 0)

        asyncio.run(scenario())

    def test_futures_bus_isolated_from_spot_bus(self):
        """[Regresi kunci -- prasyarat desain #8] Publish ke bus SPOT tidak
        boleh bocor ke stream futures."""
        async def scenario():
            bus_futures = EventBus()
            bus_spot    = EventBus()
            bot = _build_bot(bus_futures)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(
                is_disconnected=AsyncMock(side_effect=[False, False, True]),
            )

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator
            await asyncio.wait_for(agen.__anext__(), timeout=1.0)

            bus_spot.publish("trade", _make_trade(), market_type="spot")  # bus SALAH, tidak boleh muncul

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "event dari bus spot tidak boleh muncul di stream futures")

            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            await agen.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
