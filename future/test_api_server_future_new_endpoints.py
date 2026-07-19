"""
future/test_api_server_future_new_endpoints.py -- Test untuk endpoint yang
di-port dari spot/api_server_spot.py ke future/api_server_future.py
(audit item #7):

  Langkah 1/4 (3 endpoint kecil, straight port + 1 bug ditemukan&dihindari):
    GET /api/candles/{symbol}/indicators
    GET /api/orderbook/{symbol}
    GET /api/market_info/{symbol}

  Langkah 2/4 (universe write, penyesuaian konteks futures):
    POST /api/universe/add
    POST /api/universe/remove

[TEMUAN KRUSIAL langkah 1 -- bukan straight copy di /api/orderbook]
Investigasi menemukan versi spot get_orderbook() memanggil
`_get_ob_danger_level(ob)` dengan HANYA 1 argumen -- padahal fungsi
aslinya (spot & futures, identik) butuh 5 argumen wajib
(symbol, bids, asks, ratio, confidence). Ini SELALU TypeError -> HTTP 502
di spot, dikonfirmasi lewat baca kode (bukan asumsi/dites langsung ke bot
live). Versi futures di sini TIDAK mereplikasi bug itu -- hitung
ratio/confidence sungguhan lewat WhaleDetector.analyze() (WhaleDetector()
instance BARU per request, supaya tidak mengotori state
_prev_bids/_prev_asks milik b._whale_detectors[symbol] yang dipegang live
scanner loop).

[TEMUAN KRUSIAL langkah 2 -- universe/add PUNYA validasi is_symbol_supported()
di futures, TIDAK ADA di spot] Ditelusuri seluruh pipeline spot
(endpoint -> DB -> main_spot.py run_scanner hot-reload -> WebSocketFeed.
add_symbols) -- NOL validasi exchange di titik mana pun. Futures pakai
is_symbol_supported() (preseden nyata: sudah dipakai
auto_scan_and_populate_futures(), didorong insiden EVAA/USDT) SEBELUM
menulis ke DB. universe/remove SENGAJA TIDAK divalidasi (symbol delisted
harus tetap bisa dihapus).

    python3 -m unittest future.test_api_server_future_new_endpoints -v
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DASHBOARD_API_KEY_FUTURES", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from future.api_server_future import create_app


def _make_ohlcv_bars(n=250, start_price=100.0):
    bars = []
    price = start_price
    ts0 = 1_700_000_000_000
    for i in range(n):
        price += 0.1
        o, h, l, c = price, price + 0.5, price - 0.5, price + 0.2
        bars.append([ts0 + i * 60_000, o, h, l, c, 1000.0 + i])
    return bars


def _make_fake_book(mid=100.0, levels=25):
    bids = [[mid - i * 0.1, 10.0 + i] for i in range(1, levels + 1)]
    asks = [[mid + i * 0.1, 10.0 + i] for i in range(1, levels + 1)]
    return {"bids": bids, "asks": asks}


def _real_get_ob_danger_level(symbol, bids, asks, ratio, confidence) -> int:
    """Reimplementasi minimal setia pada engine/indicators/orderbook.py
    kontrak return int 1-10 -- cukup utk test integrasi endpoint (bukan
    test unit utk _get_ob_danger_level itu sendiri)."""
    if not bids or not asks:
        return 10
    if 0.9 <= ratio <= 1.1 and confidence >= 0.8:
        return 8
    return 5


class _FakeWsFeed:
    def __init__(self, book=None, tickers=None):
        self._book = book if book is not None else _make_fake_book()
        self.live_tickers = tickers or {
            "BTC/USDT": {"last": 100.2, "bid": 100.1, "ask": 100.3,
                         "volume": 12345.0, "quote_volume": 999999.0,
                         "high_24h": 105.0, "low_24h": 95.0, "change_pct": 1.5},
        }

    def get_orderbook(self, symbol):
        return self._book

    def get_spread(self, symbol):
        return 0.02

    def get_mid_price(self, symbol):
        return 100.2

    def get_spread_absolute(self, symbol):
        return 0.2

    def is_feed_healthy(self, symbol):
        return True


def _build_fake_bot(book=None, symbol_supported=True):
    exchange = SimpleNamespace(
        is_connected=True,
        fetch_ohlcv=AsyncMock(return_value=_make_ohlcv_bars()),
        get_market_info=MagicMock(return_value={
            "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT",
            "active": True, "precision_price": 2, "precision_amount": 3,
            "min_amount": 0.001, "max_amount": None, "min_cost": 5.0,
            "taker_fee": 0.0004, "maker_fee": 0.0002,
        }),
        is_symbol_supported=MagicMock(return_value=symbol_supported),
    )
    db = SimpleNamespace(
        get_recent_trades=AsyncMock(return_value=[]),
        upsert_universe_override=AsyncMock(),
        deactivate_universe_override=AsyncMock(),
    )
    bot = SimpleNamespace(
        exchange=exchange,
        db=db,
        ws_feed=_FakeWsFeed(book=book),
        _whale_detectors={},
        _get_ob_danger_level=_real_get_ob_danger_level,
    )
    return bot


def _client(bot):
    app = create_app(lambda: bot)
    return TestClient(app)


_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


class TestCandlesWithIndicatorsEndpoint(unittest.TestCase):

    def test_returns_200_with_expected_structure(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT/indicators?timeframe=15m&limit=50", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertEqual(body["timeframe"], "15m")
        self.assertIn("columns", body)
        self.assertIn("candles", body)
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

    def test_not_shadowed_by_plain_candles_route(self):
        """[Regresi kunci -- bug routing ditemukan di spot, dicek & dihindari
        di sini] Route /indicators HARUS genuinely reachable -- bukan
        ketangkap handler /api/candles/{symbol} yang polos (return
        {candles, markers} tanpa 'columns'/'count'). Dibuktikan sebelumnya
        via TestClient thd app spot SUNGGUHAN: response persis field
        get_candles() polos, bukan endpoint ini -- root cause: converter
        `:path` Starlette greedy + urutan registrasi. Test ini memastikan
        futures TIDAK mereplikasi bug itu."""
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT/indicators", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("columns", body, "harus kena handler /indicators, bukan get_candles() polos")
        self.assertIn("count", body)
        self.assertNotIn("markers", body, "field 'markers' cuma ada di get_candles() polos -- indikasi salah handler")

    def test_plain_candles_route_still_reachable_separately(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/candles/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("candles", body)
        self.assertIn("markers", body)
        self.assertNotIn("columns", body)


class TestOrderbookEndpoint(unittest.TestCase):

    def test_returns_200_not_502_unlike_spot_bug(self):
        """[Regresi kunci] Ini SKENARIO PERSIS yang bikin versi spot selalu
        502 -- membuktikan versi futures ini genuinely tidak mereplikasi
        bug argumen _get_ob_danger_level."""
        c = _client(_build_fake_bot())
        r = c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertIn("danger_level", body)
        self.assertIsInstance(body["danger_level"], (int, float))
        self.assertEqual(len(body["bids"]), 20)
        self.assertEqual(len(body["asks"]), 20)

    def test_empty_orderbook_gives_danger_level_10_not_crash(self):
        c = _client(_build_fake_bot(book={"bids": [], "asks": []}))
        r = c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["danger_level"], 10)

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.get("/api/orderbook/BTC%2FUSDT")
        self.assertEqual(r.status_code, 401)

    def test_does_not_mutate_live_whale_detector_state(self):
        """[Non-regresi state live] Endpoint HARUS pakai WhaleDetector()
        baru per request -- b._whale_detectors (dipegang scanner loop
        live) tidak boleh ikut ter-mutasi oleh panggilan API read-only."""
        bot = _build_fake_bot()
        c = _client(bot)
        c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(bot._whale_detectors, {}, "state whale detector live tidak boleh disentuh endpoint ini")

    def test_ws_feed_not_active_returns_503(self):
        bot = _build_fake_bot()
        bot.ws_feed = None
        c = _client(bot)
        r = c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)


class TestMarketInfoEndpoint(unittest.TestCase):

    def test_returns_200_without_auth_header_matches_spot_parity(self):
        """[Parity dgn spot, dicek langsung] Endpoint ini TIDAK di-guard
        verify_api_key di versi spot -- dipertahankan identik, jadi HARUS
        tetap 200 tanpa header X-API-Key."""
        c = _client(_build_fake_bot())
        r = c.get("/api/market_info/BTC%2FUSDT")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertEqual(body["base"], "BTC")
        self.assertIn("last_price", body)
        self.assertIn("feed_healthy", body)

    def test_no_ws_feed_still_returns_market_fields_only(self):
        bot = _build_fake_bot()
        bot.ws_feed = None
        c = _client(bot)
        r = c.get("/api/market_info/BTC%2FUSDT")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertNotIn("last_price", body)


class TestUniverseAddEndpoint(unittest.TestCase):

    def test_valid_symbol_added_calls_db_upsert(self):
        bot = _build_fake_bot(symbol_supported=True)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "sol/usdt", "notes": "manual add"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "added")
        self.assertEqual(body["symbol"], "SOL/USDT", "harus di-uppercase+strip")
        bot.exchange.is_symbol_supported.assert_called_once_with("SOL/USDT")
        bot.db.upsert_universe_override.assert_awaited_once_with(
            symbol="SOL/USDT", source="api", notes="manual add",
        )

    def test_invalid_symbol_rejected_400_and_never_written_to_db(self):
        """[Regresi kunci -- penyesuaian futures] Beda dari spot yang NOL
        validasi -- symbol yang tidak dikenali ccxt HARUS ditolak SEBELUM
        upsert_universe_override() dipanggil sama sekali."""
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
        bot = _build_fake_bot()
        bot.exchange.is_connected = False
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 503)
        bot.db.upsert_universe_override.assert_not_awaited()

    def test_notes_optional_defaults_empty_string(self):
        bot = _build_fake_bot(symbol_supported=True)
        c = _client(bot)
        r = c.post("/api/universe/add", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.db.upsert_universe_override.assert_awaited_once_with(
            symbol="SOL/USDT", source="api", notes="",
        )


class TestUniverseRemoveEndpoint(unittest.TestCase):

    def test_removes_without_validating_symbol_supported(self):
        """[Regresi kunci -- desain sengaja] Symbol delisted/tidak lagi
        dikenali ccxt HARUS TETAP bisa dihapus dari watchlist --
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
        """[Regresi kunci] Beda dari universe/add -- remove TIDAK butuh
        exchange terhubung sama sekali (tidak ada validasi exchange di
        jalur ini)."""
        bot = _build_fake_bot()
        bot.exchange.is_connected = False
        c = _client(bot)
        r = c.post("/api/universe/remove", json={"symbol": "SOL/USDT"}, headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        bot.db.deactivate_universe_override.assert_awaited_once_with(symbol="SOL/USDT")

    def test_requires_auth(self):
        c = _client(_build_fake_bot())
        r = c.post("/api/universe/remove", json={"symbol": "SOL/USDT"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
