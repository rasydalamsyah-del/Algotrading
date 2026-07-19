"""
spot/test_api_server_spot_candles_indicators_route.py -- Test untuk
bug-fix #34: GET /api/candles/{symbol}/indicators di
spot/api_server_spot.py tidak pernah reachable (route shadowing).

[ROOT CAUSE, ditemukan saat membangun endpoint futures] `/api/candles/
{symbol:path}/indicators` didaftarkan SETELAH `/api/candles/{symbol:path}`
-- converter `:path` Starlette greedy (match termasuk slash lanjutan) +
resolusi rute pakai urutan registrasi pertama-menang, jadi
`/api/candles/X/indicators` SELALU ketangkap handler get_candles() yang
polos (return {candles, markers}), bukan handler /indicators (return
{symbol, timeframe, columns, candles, count, timestamp}). Dibuktikan
lewat TestClient langsung thd app SEBELUM fix (bukan asumsi): response
persis field get_candles().

Fix: route /indicators (lebih spesifik) didaftarkan SEBELUM
/api/candles/{symbol:path} (lebih umum) -- pola sama dgn
future/api_server_future.py (task #16), diverifikasi bukan straight-copy
(urutan registrasi dicek langsung lewat grep sebelum fix, bukan diasumsikan
sama dgn futures).

    python3 -m unittest spot.test_api_server_spot_candles_indicators_route -v
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DASHBOARD_API_KEY", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from spot.api_server_spot import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _make_ohlcv_bars(n=250, start_price=100.0):
    bars = []
    price = start_price
    ts0 = 1_700_000_000_000
    for i in range(n):
        price += 0.1
        o, h, l, c = price, price + 0.5, price - 0.5, price + 0.2
        bars.append([ts0 + i * 60_000, o, h, l, c, 1000.0 + i])
    return bars


def _build_fake_bot():
    exchange = SimpleNamespace(
        is_connected=True,
        fetch_ohlcv=AsyncMock(return_value=_make_ohlcv_bars()),
    )
    db = SimpleNamespace(get_recent_trades=AsyncMock(return_value=[]))
    return SimpleNamespace(exchange=exchange, db=db)


def _client(bot):
    return TestClient(create_app(lambda: bot))


class TestCandlesIndicatorsRouteReachable(unittest.TestCase):

    def test_not_shadowed_by_plain_candles_route(self):
        """[Regresi kunci -- inti bug #34] Route /indicators HARUS
        genuinely reachable -- bukan ketangkap handler
        /api/candles/{symbol} yang polos (return {candles, markers} tanpa
        'columns'/'count')."""
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT/indicators", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("columns", body, "harus kena handler /indicators, bukan get_candles() polos")
        self.assertIn("count", body)
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertNotIn("markers", body, "field 'markers' cuma ada di get_candles() polos -- indikasi salah handler")

    def test_returns_200_with_expected_structure(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT/indicators?timeframe=15m&limit=50", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["timeframe"], "15m")
        self.assertEqual(body["count"], len(body["candles"]))
        self.assertLessEqual(body["count"], 50)
        self.assertGreater(len(body["columns"]), 6, "harus ada kolom indikator tambahan, bukan cuma OHLCV mentah")

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT/indicators")
        self.assertEqual(r.status_code, 401)

    def test_exchange_not_connected_returns_503(self):
        bot = _build_fake_bot()
        bot.exchange.is_connected = False
        c = _client(bot)
        r = c.get("/api/candles/BTC%2FUSDT/indicators", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_plain_candles_route_still_reachable_separately(self):
        """Non-regresi: memindahkan urutan registrasi TIDAK boleh
        merusak /api/candles/{symbol} yang polos."""
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("candles", body)
        self.assertIn("markers", body)
        self.assertNotIn("columns", body)


if __name__ == "__main__":
    unittest.main()
