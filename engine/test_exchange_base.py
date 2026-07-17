"""
engine/test_exchange_base.py — Test untuk BaseExchangeConnector.reload_markets()

[FIX -- insiden EVAA/USDT 13 Juli 2026, rencana perbaikan #1 dari 3]
reload_markets() ditambahkan supaya cache market ccxt bisa di-refresh paksa
sebelum auto-scan universe futures memvalidasi simbol hasil scan lewat
is_symbol_supported() -- cache lama (dari load_markets() di connect()) bisa
ketinggalan kalau ada simbol baru listing setelahnya.

Tidak ada test untuk BaseExchangeConnector sebelumnya (dikonfirmasi lewat
audit repo) -- file ini baru, cakupannya sengaja dibatasi ke reload_markets()
+ interaksinya dengan is_symbol_supported(), bukan audit menyeluruh class ini.

    python3 -m unittest engine.test_exchange_base -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from engine.exchange_base import BaseExchangeConnector


def _make_connector() -> BaseExchangeConnector:
    """Instansiasi BaseExchangeConnector TANPA menyentuh network -- ccxt.pro
    exchange object cuma dikonfigurasi saat __init__, tidak connect apapun
    sampai method seperti load_markets()/connect() dipanggil eksplisit."""
    return BaseExchangeConnector(
        exchange_id="binance",
        api_key="test_key",
        api_secret="test_secret",
        default_type="future",
        testnet=True,
    )


class TestReloadMarkets(unittest.TestCase):

    def test_reload_markets_calls_ccxt_with_reload_true(self):
        conn = _make_connector()
        conn._ex.load_markets = AsyncMock(return_value={"BTC/USDT": {}})

        import asyncio
        asyncio.run(conn.reload_markets())

        conn._ex.load_markets.assert_awaited_once_with(reload=True)

    def test_reload_markets_updates_internal_cache(self):
        conn = _make_connector()
        new_markets = {"BTC/USDT": {"id": "BTCUSDT"}, "NEW/USDT": {"id": "NEWUSDT"}}
        conn._ex.load_markets = AsyncMock(return_value=new_markets)

        import asyncio
        asyncio.run(conn.reload_markets())

        self.assertEqual(conn._markets, new_markets)

    def test_is_symbol_supported_unchanged_for_existing_symbol_across_reload(self):
        """[SAFETY] Simbol yang SUDAH dikenal ccxt harus tetap dikenal, baik
        sebelum maupun sesudah reload_markets() -- reload tidak boleh
        mengubah perilaku is_symbol_supported() untuk simbol yang tidak
        berubah statusnya di exchange."""
        conn = _make_connector()
        conn._ex.market = MagicMock(side_effect=lambda s: {} if s == "BTC/USDT" else (_ for _ in ()).throw(Exception("bad symbol")))

        before = conn.is_symbol_supported("BTC/USDT")

        conn._ex.load_markets = AsyncMock(return_value={"BTC/USDT": {}})
        import asyncio
        asyncio.run(conn.reload_markets())

        after = conn.is_symbol_supported("BTC/USDT")

        self.assertTrue(before)
        self.assertTrue(after)
        self.assertEqual(before, after)

    def test_is_symbol_supported_picks_up_new_symbol_only_after_reload(self):
        """Mendemonstrasikan NILAI fix ini: simbol yang baru listing (belum
        dikenal ccxt sebelum reload) tetap dianggap tidak didukung SAMPAI
        reload_markets() dipanggil -- setelah itu, dikenali."""
        conn = _make_connector()
        known = {"BTC/USDT"}

        def _market_lookup(symbol):
            if symbol in known:
                return {}
            raise Exception("bad symbol")

        conn._ex.market = MagicMock(side_effect=_market_lookup)

        self.assertFalse(conn.is_symbol_supported("NEW/USDT"))

        async def _reload_and_discover(reload=True):
            known.add("NEW/USDT")
            return {"BTC/USDT": {}, "NEW/USDT": {}}

        conn._ex.load_markets = AsyncMock(side_effect=_reload_and_discover)
        import asyncio
        asyncio.run(conn.reload_markets())

        self.assertTrue(conn.is_symbol_supported("NEW/USDT"))


if __name__ == "__main__":
    unittest.main()
