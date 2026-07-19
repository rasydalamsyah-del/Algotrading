"""
spot/test_api_server_spot_stream_sse.py -- Test untuk GET /api/stream yang
genuinely event-driven (audit item #8, langkah 4/4).

[LATAR BELAKANG] Versi SEBELUMNYA transport SSE tapi mekanismenya
`while True: ... await asyncio.sleep(2.0)` -- polling DB tiap 2 detik,
BUKAN dipicu event genuinely terjadi. Versi baru subscribe ke
b.event_bus, publish() dari EventBus langsung ter-refleksikan ke stream
tanpa menunggu interval tetap.

Endpoint ini generator tak-terhingga (loop sampai client disconnect) --
di-test dengan memanggil fungsi endpoint LANGSUNG (bukan lewat TestClient
streaming penuh, yang sulit dikontrol presisi utk generator tak-terhingga)
dan mengontrol `request.is_disconnected()` via AsyncMock side_effect
supaya loop berhenti bersih setelah beberapa iterasi terkendali.

    python3 -m unittest spot.test_api_server_spot_stream_sse -v
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DASHBOARD_API_KEY", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from engine.event_bus import EventBus
from spot.api_server_spot import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _make_position(symbol="BTC/USDT", **overrides):
    defaults = dict(
        id=1, symbol=symbol, side="long", entry_time=None, entry_price=100.0,
        current_price=105.0, amount=1.0, unrealized_pnl=5.0, unrealized_pnl_pct=5.0,
        realized_pnl=None, realized_pnl_pct=None, stop_loss_price=95.0,
        take_profit_price=110.0, atr_at_entry=1.0, strategy_name="test",
        strategy_profile="scalp_volatile", entry_order_id="O1", is_open=True,
        is_closing=False, entry_score=None, entry_regime=None, highest_price=None,
        exit_time=None, entry_fee_actual=None,
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
    """Auth check TIDAK butuh interaksi dgn generator SSE sama sekali --
    Depends(verify_api_key) raise SEBELUM badan fungsi dijalankan."""

    def test_requires_auth(self):
        bus = EventBus()
        app = create_app(lambda: _build_bot(bus))
        client = TestClient(app)
        r = client.get("/api/stream")
        self.assertEqual(r.status_code, 401)


class TestStreamEndpointEventDriven(unittest.TestCase):

    def test_initial_snapshot_emitted_first(self):
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
            self.assertTrue(text.startswith("data: "))
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["type"], "initial_snapshot")
            self.assertEqual(payload["market_type"], "spot")

            await agen.aclose()

        asyncio.run(scenario())

    def test_published_trade_event_reaches_stream(self):
        """[Regresi kunci] Event yang di-publish ke event_bus HARUS
        genuinely muncul di stream -- BUKAN menunggu interval polling
        tetap (perbedaan mendasar dari versi lama)."""
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

            await asyncio.wait_for(agen.__anext__(), timeout=1.0)  # initial_snapshot

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0.02)  # beri kesempatan generator sampai queue.get()
            trade = _make_trade()
            bus.publish("trade", trade, market_type="spot")

            chunk = await asyncio.wait_for(task, timeout=1.0)
            text = chunk if isinstance(chunk, str) else chunk.decode()
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["type"], "trade")
            self.assertEqual(payload["data"]["symbol"], "BTC/USDT")
            self.assertEqual(payload["data"]["order_id"], "O1")

            await agen.aclose()

        asyncio.run(scenario())

    def test_positions_snapshot_event_serialized_as_list(self):
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
            await asyncio.wait_for(agen.__anext__(), timeout=1.0)  # initial_snapshot

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0.02)
            positions = [_make_position(symbol="BTC/USDT"), _make_position(symbol="ETH/USDT")]
            bus.publish("positions_snapshot", positions, market_type="spot")

            chunk = await asyncio.wait_for(task, timeout=1.0)
            text = chunk if isinstance(chunk, str) else chunk.decode()
            payload = json.loads(text[len("data: "):].strip())
            self.assertEqual(payload["type"], "positions_snapshot")
            self.assertEqual(len(payload["data"]), 2)

            await agen.aclose()

        asyncio.run(scenario())

    def test_loop_exits_cleanly_on_disconnect(self):
        async def scenario():
            bus = EventBus()
            bot = _build_bot(bus)
            app = create_app(lambda: bot)
            endpoint = _get_stream_endpoint(app)
            fake_request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))

            response = await endpoint(_="key", request=fake_request)
            agen = response.body_iterator

            await asyncio.wait_for(agen.__anext__(), timeout=1.0)  # initial_snapshot
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(agen.__anext__(), timeout=1.0)  # while-loop cek disconnect -> True -> break

        asyncio.run(scenario())

    def test_subscription_unsubscribed_after_generator_closes(self):
        """[Regresi kunci] async with b.event_bus.subscribe() HARUS
        genuinely unsubscribe saat generator selesai/ditutup -- kalau
        tidak, subscriber lama menumpuk selamanya tiap client disconnect."""
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


if __name__ == "__main__":
    unittest.main()
