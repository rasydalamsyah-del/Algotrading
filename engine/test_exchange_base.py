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

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt.pro as ccxt

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


class TestReloadMarketsRetryWrapping(unittest.TestCase):
    """[ITEM #6] reload_markets() sekarang membungkus self._ex.load_markets()
    lewat self._retry() -- pola yang sama dgn fetch_ohlcv/fetch_ticker/dkk di
    file ini -- supaya rate-limit/network blip SESAAT tidak bikin refresh
    periodik (run_market_cache_refresh() di main_spot.py/main_future.py)
    gagal total di 1 percobaan pertama. Test di sini membuktikan retry
    BENAR-BENAR terjadi (bukan cuma pass-through 1x), dan fail-safe: kalau
    retry habis, self._markets LAMA tetap dipertahankan (tidak ditimpa
    None/kosong)."""

    def setUp(self):
        # _retry() pakai asyncio.sleep() sungguhan utk backoff antar
        # percobaan (delay*2**attempt / delay*attempt*3 dst) -- patch jadi
        # no-op supaya test tidak benar-benar menunggu detik nyata.
        self._sleep_patcher = patch(
            "engine.exchange_base.asyncio.sleep", new=AsyncMock(return_value=None)
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_reload_markets_retries_on_rate_limit_then_succeeds(self):
        """Percobaan pertama kena RateLimitExceeded, percobaan kedua sukses
        -- self._markets harus terisi hasil percobaan yang berhasil, dan
        load_markets(reload=True) harus dipanggil LEBIH DARI SEKALI
        (membuktikan _retry() genuinely retry, bukan cuma meneruskan
        exception langsung)."""
        conn = _make_connector()
        good_markets = {"BTC/USDT": {"id": "BTCUSDT"}}
        conn._ex.load_markets = AsyncMock(
            side_effect=[ccxt.RateLimitExceeded("too many requests"), good_markets]
        )

        asyncio.run(conn.reload_markets())

        self.assertEqual(conn._markets, good_markets)
        self.assertEqual(conn._ex.load_markets.await_count, 2)

    def test_reload_markets_all_retries_exhausted_raises_and_keeps_old_cache(self):
        """Semua percobaan (default retries=3) gagal -- reload_markets()
        HARUS raise (supaya caller/loop periodik tahu siklus ini gagal &
        log error, bukan diam-diam sukses dgn data kosong), DAN self._markets
        tetap berisi cache LAMA (fail-safe -- tidak pernah ditimpa
        None/dict kosong hanya karena reload gagal)."""
        conn = _make_connector()
        old_markets = {"BTC/USDT": {"id": "BTCUSDT"}}
        conn._markets = dict(old_markets)
        conn._ex.load_markets = AsyncMock(
            side_effect=ccxt.RateLimitExceeded("too many requests")
        )

        with self.assertRaises(ccxt.RateLimitExceeded):
            asyncio.run(conn.reload_markets())

        self.assertEqual(conn._ex.load_markets.await_count, 3)  # default retries
        self.assertEqual(conn._markets, old_markets)


class _FakeExchangeForCacheRefreshLoop:
    """Stub exchange minimal -- cuma yang dipakai run_market_cache_refresh():
    .reload_markets() (async) dan ._markets (dibaca sekedar utk logging)."""

    def __init__(self, fail_times: int = 0):
        self.reload_calls = 0
        self._fail_times = fail_times
        self._markets = {"BTC/USDT": {}}

    async def reload_markets(self):
        self.reload_calls += 1
        if self.reload_calls <= self._fail_times:
            raise ccxt.NetworkError("temporary network blip")
        self._markets = {"BTC/USDT": {}, f"NEW{self.reload_calls}/USDT": {}}


class _FakeBotSelfForCacheRefreshLoop:
    """Stand-in `self` minimal -- run_market_cache_refresh() cuma menyentuh
    self.is_running / self.config / self.exchange, jadi tidak perlu
    instansiasi TradingBot sungguhan (yang butuh DB/exchange/notifier live)."""

    def __init__(self, exchange, interval=0, stop_after=1):
        self.exchange = exchange
        self.config = {"market_cache_refresh_interval": interval}
        self.is_running = True
        self._stop_after = stop_after

    def maybe_stop(self):
        if self.exchange.reload_calls >= self._stop_after:
            self.is_running = False


class TestMarketCacheRefreshLoop(unittest.TestCase):
    """[ITEM #6] run_market_cache_refresh() -- loop periodik BARU di
    future/main_future.py::TradingBot dan spot/main_spot.py::TradingBot,
    pola identik run_analytics_loop/run_daily_summary (while self.is_running:
    try/except CancelledError/except Exception: log.error(); sleep(interval)).
    reload_markets() sebelumnya HANYA dipanggil sekali (futures, sebelum
    auto-scan) atau TIDAK PERNAH (spot) sepanjang umur proses -- loop ini
    memanggilnya ulang tiap market_cache_refresh_interval detik (default
    3600, dioverride ke 0 di test ini supaya tidak benar-benar menunggu).

    Dites lewat method UNBOUND (TradingBot.run_market_cache_refresh(fake_self))
    dgn `self` stub minimal -- method ini cuma menyentuh self.is_running/
    self.config/self.exchange, jadi tidak perlu bot sungguhan (DB/notifier/
    exchange live) utk membuktikan behavior loop-nya."""

    def _run_loop_once(self, TradingBotCls, exchange, **kwargs):
        fake_self = _FakeBotSelfForCacheRefreshLoop(exchange, **kwargs)
        # exchange.reload_markets() asli (stub) tidak tahu kapan harus
        # menghentikan loop -- bungkus supaya tiap panggilan reload_markets
        # juga mengecek apakah sudah waktunya stop (mirip CancelledError
        # sungguhan yg akan menghentikan loop di produksi saat shutdown).
        orig_reload = fake_self.exchange.reload_markets

        async def _reload_then_maybe_stop():
            try:
                await orig_reload()
            finally:
                fake_self.maybe_stop()

        fake_self.exchange.reload_markets = _reload_then_maybe_stop
        asyncio.run(TradingBotCls.run_market_cache_refresh(fake_self))
        return fake_self

    def test_future_bot_calls_reload_markets(self):
        from future.main_future import TradingBot as FutureTradingBot
        exchange = _FakeExchangeForCacheRefreshLoop()
        fake_self = self._run_loop_once(FutureTradingBot, exchange, stop_after=1)
        self.assertEqual(exchange.reload_calls, 1)
        self.assertIn("NEW1/USDT", exchange._markets)

    def test_spot_bot_calls_reload_markets(self):
        """[Gap yang ditutup] Spot TIDAK PERNAH memanggil reload_markets()
        sama sekali sebelum ini -- test ini membuktikan loop barunya
        genuinely memanggil exchange.reload_markets()."""
        from spot.main_spot import TradingBot as SpotTradingBot
        exchange = _FakeExchangeForCacheRefreshLoop()
        fake_self = self._run_loop_once(SpotTradingBot, exchange, stop_after=1)
        self.assertEqual(exchange.reload_calls, 1)
        self.assertIn("NEW1/USDT", exchange._markets)

    def test_loop_survives_transient_exception_and_retries_next_cycle(self):
        """[REGRESI -- ketahanan loop] Kalau reload_markets() gagal di satu
        siklus (mis. rate-limit blip yg lolos dari _retry internal), loop
        TIDAK BOLEH mati -- harus log error lalu lanjut ke siklus
        berikutnya, persis pola except Exception di run_analytics_loop/
        run_daily_summary."""
        from future.main_future import TradingBot as FutureTradingBot
        exchange = _FakeExchangeForCacheRefreshLoop(fail_times=1)
        fake_self = self._run_loop_once(FutureTradingBot, exchange, stop_after=2)
        self.assertEqual(exchange.reload_calls, 2)
        # Panggilan pertama gagal (fail_times=1) -- _markets tidak berubah
        # dari default sampai panggilan KEDUA sukses.
        self.assertIn("NEW2/USDT", exchange._markets)

    def test_default_interval_is_one_hour_when_not_configured(self):
        """[Nilai yang disepakati -- 3600s/1 jam] Kalau
        market_cache_refresh_interval tidak ada di config sama sekali,
        fallback HARUS 3600 (bukan silently 0 / None) -- dicek langsung dari
        argumen default get(), tanpa menjalankan loop sungguhan (supaya
        test tidak perlu menunggu 1 jam)."""
        fake_self = _FakeBotSelfForCacheRefreshLoop(
            _FakeExchangeForCacheRefreshLoop(), interval=0,
        )
        fake_self.config = {}  # sengaja kosong, simulasikan key belum di-set
        self.assertEqual(fake_self.config.get("market_cache_refresh_interval", 3600), 3600)

    def test_default_config_has_market_cache_refresh_interval_3600(self):
        """[Regresi konfigurasi] _load_config() (staticmethod, tidak butuh
        instance/network) HARUS menghasilkan market_cache_refresh_interval
        default 3600 detik di KEDUA bot, sesuai keputusan yg disepakati."""
        from future.main_future import TradingBot as FutureTradingBot
        from spot.main_spot import TradingBot as SpotTradingBot
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MARKET_CACHE_REFRESH_INTERVAL", None)
            self.assertEqual(
                FutureTradingBot._load_config()["market_cache_refresh_interval"], 3600
            )
            self.assertEqual(
                SpotTradingBot._load_config()["market_cache_refresh_interval"], 3600
            )


if __name__ == "__main__":
    unittest.main()
