"""
spot/test_api_server_spot_orderbook_fix.py -- Test untuk bug-fix #33:
GET /api/orderbook/{symbol} di spot/api_server_spot.py SELALU HTTP 502.

[ROOT CAUSE, ditemukan saat membangun endpoint futures sesi sebelumnya]
Endpoint memanggil `_get_ob_danger_level(ob)` dengan HANYA 1 argumen (dict
orderbook mentah) -- padahal fungsi aslinya (identik di spot & futures,
"[REUSE VERBATIM]") butuh 5 argumen wajib: (symbol, bids, asks, ratio,
confidence). ratio/confidence datang dari WhaleDetector.analyze(), BUKAN
dari orderbook mentah -- setiap panggilan endpoint ini SELALU TypeError,
tertangkap `except Exception -> HTTP 502`, dikonfirmasi lewat baca kode
langsung.

Fix: hitung ratio/confidence sungguhan via WhaleDetector.analyze(),
WhaleDetector() instance BARU per request (bukan reuse
b._whale_detectors[symbol] yang dipegang scanner loop live), wall_first_
seen pakai `{}` baru (BUKAN b._ob_wall_first_seen milik bot -- beda dari
scanner loop spot yang genuinely pakai dict persisten itu, tapi endpoint
read-only ini sengaja tidak boleh menyentuh dict yang sama, sama alasan
kenapa WhaleDetector()-nya juga baru).

    python3 -m unittest spot.test_api_server_spot_orderbook_fix -v
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DASHBOARD_API_KEY", "test-api-key-1234567890")

from fastapi.testclient import TestClient

from spot.api_server_spot import create_app

_HEADERS = {"X-API-Key": "test-api-key-1234567890"}


def _real_get_ob_danger_level(symbol, bids, asks, ratio, confidence) -> int:
    """Reimplementasi setia thd main_spot.py::_get_ob_danger_level() --
    murni matematika (wall_dist_pct dari mid), tidak butuh state bot lain.
    Test integrasi (endpoint HTTP) di file ini, BUKAN unit test fungsi ini
    sendiri -- reimplementasi minimal cukup utk membuktikan endpoint tidak
    lagi 502."""
    if not bids or not asks:
        return 10
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return 10
        max_ask_qty = max((float(a[1]) for a in asks[:20]), default=0)
        max_ask_price = next((float(a[0]) for a in asks[:20] if float(a[1]) == max_ask_qty), best_ask)
        wall_dist_pct = abs(max_ask_price - mid) / mid * 100
        if wall_dist_pct < 0.1: return 1
        if wall_dist_pct < 0.3: return 2
        if wall_dist_pct < 0.5: return 3
        if wall_dist_pct < 1.0: return 4
        if wall_dist_pct < 1.5: return 5
        if wall_dist_pct < 2.0: return 6
        if wall_dist_pct < 3.0: return 7
        return 8
    except Exception:
        return 10


def _make_fake_book(mid=100.0, levels=25):
    bids = [[mid - i * 0.1, 10.0 + i] for i in range(1, levels + 1)]
    asks = [[mid + i * 0.1, 10.0 + i] for i in range(1, levels + 1)]
    return {"bids": bids, "asks": asks}


class _FakeWsFeed:
    def __init__(self, book=None):
        self._book = book if book is not None else _make_fake_book()

    def get_orderbook(self, symbol):
        return self._book

    def get_spread(self, symbol):
        return 0.02

    def get_mid_price(self, symbol):
        return 100.2


def _build_fake_bot(book=None):
    return SimpleNamespace(
        ws_feed=_FakeWsFeed(book=book),
        _whale_detectors={},
        _get_ob_danger_level=_real_get_ob_danger_level,
    )


def _client(bot):
    return TestClient(create_app(lambda: bot))


class TestOrderbookEndpointFix(unittest.TestCase):

    def test_returns_200_not_502_with_normal_orderbook(self):
        """[Regresi kunci -- inti bug #33] Skenario normal HARUS 200,
        BUKAN 502 spt sebelumnya."""
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

    def test_ws_feed_not_active_returns_503(self):
        bot = _build_fake_bot()
        bot.ws_feed = None
        c = _client(bot)
        r = c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_does_not_mutate_live_whale_detector_state(self):
        """[Regresi kunci -- prasyarat instruksi user] Endpoint HARUS pakai
        WhaleDetector() baru per request -- b._whale_detectors (dipegang
        scanner loop live) tidak boleh ikut ter-mutasi oleh panggilan API
        read-only ini."""
        bot = _build_fake_bot()
        c = _client(bot)
        c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        self.assertEqual(
            bot._whale_detectors, {},
            "state whale detector live tidak boleh disentuh endpoint ini",
        )

    def test_does_not_mutate_live_wall_first_seen_dict(self):
        """[Regresi kunci -- beda struktural spot vs futures] Spot (beda
        dari futures) genuinely punya _ob_wall_first_seen persisten yang
        dipakai scanner loop live -- endpoint ini TIDAK BOLEH menerima/
        memutasi dict itu, harus pakai {} baru setiap request."""
        bot = _build_fake_bot()
        bot._ob_wall_first_seen = {"BTC/USDT_wall": 12345.0}  # simulasi state live existing
        original = dict(bot._ob_wall_first_seen)
        c = _client(bot)

        # Orderbook dgn wall signifikan supaya wall-age tracking genuinely dipakai.
        book = _make_fake_book(mid=100.0)
        book["asks"][0][1] = 500.0  # wall besar di ask pertama
        bot.ws_feed = _FakeWsFeed(book=book)

        c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)

        self.assertEqual(
            bot._ob_wall_first_seen, original,
            "_ob_wall_first_seen milik bot (dipakai Gate 2 live) tidak boleh berubah",
        )

    def test_bids_asks_capped_at_20(self):
        c = _client(_build_fake_bot(book=_make_fake_book(levels=30)))
        r = c.get("/api/orderbook/BTC%2FUSDT", headers=_HEADERS)
        body = r.json()
        self.assertEqual(len(body["bids"]), 20)
        self.assertEqual(len(body["asks"]), 20)


if __name__ == "__main__":
    unittest.main()
