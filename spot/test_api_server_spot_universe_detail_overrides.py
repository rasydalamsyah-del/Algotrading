"""
spot/test_api_server_spot_universe_detail_overrides.py -- Test untuk
bug-fix #32: GET /api/universe/detail di spot/api_server_spot.py tidak
mengonsultasi db.get_active_universe_overrides().

[ROOT CAUSE] `coins` sebelumnya HANYA dari universe.json (atau fallback
config["universe_watchlist"]) -- db.get_active_universe_overrides() tidak
pernah dibaca di endpoint ini. Symbol yang ditambah manual lewat POST
/api/universe/add genuinely ikut discan/trading (scanner loop main_spot.py
hot-reload dari DB overrides terpisah, jalur itu SUDAH benar), tapi tidak
pernah muncul di tampilan endpoint ini -- gap TAMPILAN, bukan gap
fungsional.

Fix: gabungkan symbol dari db.get_active_universe_overrides() yang belum
ada di `coins` (dari json/config), volume_24h default 0.

[Catatan test] universe.json genuinely ADA di repo ini (bukan skenario
fallback) -- test tidak mock file I/O, cukup pastikan symbol override yang
DIPASTIKAN belum ada di file real (ZZZTESTCOIN/USDT) muncul di response.

    python3 -m unittest spot.test_api_server_spot_universe_detail_overrides -v
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
_TEST_OVERRIDE_SYMBOL = "ZZZTESTCOIN/USDT"  # dikonfirmasi tidak ada di universe.json real


def _build_fake_bot(db_overrides):
    db = SimpleNamespace(
        get_active_universe_overrides=AsyncMock(return_value=db_overrides),
        get_latest_regime=AsyncMock(return_value=None),
        get_latest_signal_score=AsyncMock(return_value=None),
    )
    return SimpleNamespace(db=db, config={"universe_watchlist": []})


def _client(bot):
    return TestClient(create_app(lambda: bot))


class TestUniverseDetailIncludesDbOverrides(unittest.TestCase):

    def test_manually_added_override_symbol_appears_in_response(self):
        """[Regresi kunci -- inti bug #32] Symbol dari DB override (mis.
        hasil POST /api/universe/add) HARUS muncul di /api/universe/detail,
        walau tidak ada di universe.json."""
        bot = _build_fake_bot([_TEST_OVERRIDE_SYMBOL])
        c = _client(bot)
        r = c.get("/api/universe/detail", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        symbols = [row["symbol"] for row in body["universe"]]
        self.assertIn(_TEST_OVERRIDE_SYMBOL, symbols)

    def test_override_symbol_has_default_volume_zero(self):
        bot = _build_fake_bot([_TEST_OVERRIDE_SYMBOL])
        c = _client(bot)
        r = c.get("/api/universe/detail", headers=_HEADERS)
        body = r.json()
        row = next(row for row in body["universe"] if row["symbol"] == _TEST_OVERRIDE_SYMBOL)
        self.assertEqual(row["volume_24h"], 0)

    def test_no_duplicate_when_override_symbol_already_in_json(self):
        """[Regresi kunci] Symbol yang KEBETULAN sudah ada di universe.json
        TIDAK boleh muncul dobel walau juga ada di DB overrides."""
        bot = _build_fake_bot([])
        c = _client(bot)
        r = c.get("/api/universe/detail", headers=_HEADERS)
        existing_symbols = [row["symbol"] for row in r.json()["universe"]]
        self.assertTrue(existing_symbols, "universe.json harus genuinely punya isi utk test ini valid")
        sample_symbol = existing_symbols[0]

        bot2 = _build_fake_bot([sample_symbol])
        c2 = _client(bot2)
        r2 = c2.get("/api/universe/detail", headers=_HEADERS)
        symbols2 = [row["symbol"] for row in r2.json()["universe"]]
        self.assertEqual(
            symbols2.count(sample_symbol), 1,
            "symbol yang sudah ada di json + DB override tidak boleh dobel",
        )

    def test_db_overrides_fetch_failure_does_not_break_endpoint(self):
        bot = _build_fake_bot([])
        bot.db.get_active_universe_overrides = AsyncMock(side_effect=RuntimeError("db locked"))
        c = _client(bot)
        r = c.get("/api/universe/detail", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)

    def test_no_overrides_returns_unaffected_baseline(self):
        bot = _build_fake_bot([])
        c = _client(bot)
        r = c.get("/api/universe/detail", headers=_HEADERS)
        self.assertEqual(r.status_code, 200)
        symbols = [row["symbol"] for row in r.json()["universe"]]
        self.assertNotIn(_TEST_OVERRIDE_SYMBOL, symbols)


if __name__ == "__main__":
    unittest.main()
