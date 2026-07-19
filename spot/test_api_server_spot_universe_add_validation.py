"""
spot/test_api_server_spot_universe_add_validation.py -- Test untuk
bug-fix #31: POST /api/universe/add di spot/api_server_spot.py nol
validasi sebelum menulis symbol ke DB.

[ROOT CAUSE, ditemukan saat membangun endpoint futures] Ditelusuri seluruh
pipeline spot (endpoint -> DB -> main_spot.py run_scanner hot-reload ->
WebSocketFeed.add_symbols) -- NOL validasi exchange di titik manapun.
Symbol arbitrer bisa masuk DB lalu bikin ws_feed subscription yang
permanen gagal, tanpa pernah ketahuan lewat API response manapun.

Fix: is_symbol_supported() (engine/exchange_base.py::BaseExchangeConnector,
shared, dikonfirmasi spot/exchange_spot.py::ExchangeConnector genuinely
subclass base itu dgn default_type="spot") divalidasi SEBELUM tulis ke DB
-- reject 400 kalau tidak dikenal, 503 kalau exchange belum connect. Pola
sama persis dgn future/api_server_future.py::universe_add() (task #17) --
diverifikasi TIDAK ada perbedaan struktural yang perlu penyesuaian (beda
dari kasus #33 kemarin, _ob_wall_first_seen).

universe/remove SENGAJA TETAP tanpa validasi -- symbol delisted/tidak
lagi dikenal exchange harus tetap bisa dihapus dari watchlist.

    python3 -m unittest spot.test_api_server_spot_universe_add_validation -v
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DASHBOARD_API_KEY", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from spot.api_server_spot import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _build_fake_bot(symbol_supported=True, is_connected=True):
    exchange = SimpleNamespace(
        is_connected=is_connected,
        is_symbol_supported=MagicMock(return_value=symbol_supported),
    )
    db = SimpleNamespace(
        upsert_universe_override=AsyncMock(),
        deactivate_universe_override=AsyncMock(),
    )
    return SimpleNamespace(exchange=exchange, db=db)


def _client(bot):
    return TestClient(create_app(lambda: bot))


class TestUniverseAddValidation(unittest.TestCase):

    def test_valid_symbol_added_calls_db_upsert(self):
        bot = _build_fake_bot(symbol_supported=True)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "sol/usdt", "notes": "manual add"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "added")
        self.assertEqual(body["symbol"], "SOL/USDT")
        bot.exchange.is_symbol_supported.assert_called_once_with("SOL/USDT")
        bot.db.upsert_universe_override.assert_awaited_once_with(
            symbol="SOL/USDT", source="api", notes="manual add",
        )

    def test_invalid_symbol_rejected_400_and_never_written_to_db(self):
        """[Regresi kunci -- inti bug #31] Symbol tidak dikenal HARUS
        ditolak SEBELUM upsert_universe_override() dipanggil sama sekali."""
        bot = _build_fake_bot(symbol_supported=False)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "FAKE/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 400)
        bot.db.upsert_universe_override.assert_not_awaited()

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"})
        self.assertEqual(r.status_code, 401)

    def test_exchange_not_connected_returns_503_and_no_db_write(self):
        bot = _build_fake_bot(is_connected=False)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)
        bot.db.upsert_universe_override.assert_not_awaited()

    def test_no_exchange_at_all_returns_503(self):
        bot = _build_fake_bot()
        bot.exchange = None
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_notes_optional_defaults_empty_string(self):
        bot = _build_fake_bot(symbol_supported=True)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.db.upsert_universe_override.assert_awaited_once_with(
            symbol="SOL/USDT", source="api", notes="",
        )


class TestUniverseRemoveUnaffectedByValidation(unittest.TestCase):

    def test_removes_without_validating_symbol_supported(self):
        """[Regresi kunci -- desain sengaja, sama pola futures] Symbol
        delisted/tidak lagi dikenal exchange HARUS TETAP bisa dihapus --
        is_symbol_supported() TIDAK BOLEH dipanggil sama sekali di jalur
        remove."""
        bot = _build_fake_bot(symbol_supported=False)
        c = _client(bot)
        r = c.post("/api/universe/remove", json={"symbol": "delisted/usdt"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "removed")
        self.assertEqual(body["symbol"], "DELISTED/USDT")
        bot.exchange.is_symbol_supported.assert_not_called()
        bot.db.deactivate_universe_override.assert_awaited_once_with(symbol="DELISTED/USDT")

    def test_works_even_when_exchange_disconnected(self):
        bot = _build_fake_bot(is_connected=False)
        c = _client(bot)
        r = c.post("/api/universe/remove", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.db.deactivate_universe_override.assert_awaited_once_with(symbol="SOL/USDT")


if __name__ == "__main__":
    unittest.main()
