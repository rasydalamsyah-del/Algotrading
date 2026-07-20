"""
engine/exchange_base.py — Base class ExchangeConnector, market-agnostic

Diekstrak dari spot/exchange_spot.py saat restrukturisasi engine/spot/future
(2026-07-11). Berisi SEMUA method yang bekerja identik untuk spot maupun
futures (via ccxt.pro, cuma beda `defaultType` di config) -- koneksi,
precision helpers, retry logic, fetch OHLCV/ticker/orderbook, dan mekanisme
paper-order generik (create_order sbg dispatcher, cancel_order, fetch_order).

YANG SENGAJA TIDAK ADA DI SINI (harus di-override di subclass masing-masing,
karena semantiknya beda total antara spot dan futures):
- fetch_balance(): spot = saldo currency biasa; futures = margin balance +
  unrealized PnL + leverage info
- _simulate_order_fill(): spot = update _paper_balance 1 currency biasa;
  futures = perlu leverage-aware margin calc + liquidation price check

Subclass WAJIB implementasikan kedua method itu sendiri.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any, List

import ccxt.pro as ccxt
from asyncio_throttle import Throttler

log = logging.getLogger("exchange_base")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReduceOnlyRejected(Exception):
    """
    [ITEM #15 -- Temuan C, Opsi C1] Dilempar saat order reduce-only
    ditolak karena tidak ada posisi (cocok arah) untuk direduce SAAT
    ORDER BENAR-BENAR DIEKSEKUSI -- backstop TOCTOU utk celah sisa Opsi
    C2 (verify-before-send): antara _verify_position_exists_at_exchange()
    dicek dan order dikirim, posisi bisa berubah (race sempit).

    Paper mode (FutureExchangeConnector._simulate_order_fill()): dilempar
    deterministik, kondisinya genuinely diketahui pasti -- caller
    (_do_close_position()) BOLEH mempercayai ini sbg "sudah closed
    duluan" dan langsung sinkron DB TANPA order baru.

    Live mode: exchange asli (Binance Futures via ccxt) menerima
    reduceOnly=True di params order sungguhan -- exchange SENDIRI yang
    menolak kalau tidak ada posisi utk direduce (proteksi native,
    dikonfirmasi ccxt/Binance API docs). TAPI penolakan itu datang
    sbg exception ccxt generik (kode error spesifik Binance), BUKAN
    exception class ini -- exception ini HANYA dipakai jalur paper.
    Keputusan desain sengaja: untuk live, order yang ditolak exchange
    tetap jatuh ke jalur "CLOSE ORDER GAGAL" existing (retry counter +
    notify manual setelah 3x) -- BUKAN auto-sync DB seperti paper,
    karena mem-parsing kode error exchange spesifik utk memastikan
    penyebabnya PASTI "sudah closed duluan" (bukan sebab lain) berisiko
    salah klasifikasi pada uang sungguhan. Proteksi UTAMA utk live
    adalah order-nya DITOLAK exchange (tidak pernah membuka posisi salah
    arah) -- itu sudah cukup sbg backstop, auto-sync DB cuma kenyamanan
    tambahan yang aman diberikan di paper (kondisinya pasti diketahui).
    """


class BaseExchangeConnector:
    """
    Base class market-agnostic. Subclass (ExchangeConnector di spot/,
    FutureExchangeConnector di future/) WAJIB set `self.paper_trading`,
    `self._paper_orders`, dan implementasikan fetch_balance() +
    _simulate_order_fill() sendiri sebelum method-method di sini bisa
    dipakai dengan aman.
    """

    def __init__(
        self,
        exchange_id:         str,
        api_key:              str,
        api_secret:           str,
        default_type:         str,   # "spot" atau "future" -- WAJIB eksplisit,
                                       # tidak ada default, supaya subclass
                                       # tidak bisa "lupa" set ini dengan benar.
        api_passphrase:       str   = "",
        testnet:              bool  = True,
        requests_per_second:  float = 5.0,
        db=None,
    ):
        self.exchange_id   = exchange_id
        self.testnet       = testnet
        self.db            = db
        self.default_type  = default_type

        self._throttler  = Throttler(
            rate_limit=int(requests_per_second), period=1.0
        )

        cls = getattr(ccxt, exchange_id)
        exchange_config = {
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": default_type,
                "adjustForTimeDifference": True,
                "recvWindow": 10000,
            },
        }
        if api_passphrase:
            exchange_config["password"] = api_passphrase
        self._ex: ccxt.Exchange = cls(exchange_config)

        if testnet:
            if hasattr(self._ex, "set_sandbox_mode"):
                self._ex.set_sandbox_mode(True)
                log.warning("TESTNET MODE — no real funds at risk.")
            else:
                log.warning(
                    "Exchange %s has no sandbox mode.", exchange_id
                )

        self.is_connected: bool       = False
        self._markets: Dict[str, Any] = {}

        # Subclass WAJIB set ini sebelum method create_order/cancel_order/
        # fetch_order di sini dipakai:
        self.paper_trading: bool = False
        self._paper_orders: Dict[str, Dict] = {}

    async def connect(self) -> bool:
        try:
            await self._ex.load_time_difference()
            self._markets = await self._ex.load_markets()
            if getattr(self._ex, "apiKey", None) and getattr(self._ex, "secret", None):
                await self._ex.fetch_balance()
            self.is_connected = True
            log.info(
                "Connected to %s (%s, type=%s) | %d markets loaded",
                self.exchange_id.upper(),
                "TESTNET" if self.testnet else "LIVE",
                self.default_type,
                len(self._markets),
            )
            return True
        except ccxt.AuthenticationError as e:
            log.critical(
                "Authentication FAILED for %s: %s", self.exchange_id, e
            )
            return False
        except Exception as e:
            log.critical("Connection error: %r", e, exc_info=True)
            return False

    async def disconnect(self) -> None:
        await self._ex.close()
        self.is_connected = False
        log.info("Exchange connection closed.")

    def get_market_info(self, symbol: str) -> Dict:
        market = self._markets.get(symbol, {})
        prec   = market.get("precision", {})
        limits = market.get("limits", {})
        return {
            "symbol":           symbol,
            "base":             market.get("base", ""),
            "quote":            market.get("quote", ""),
            "active":           market.get("active", True),
            "precision_price":  prec.get("price"),
            "precision_amount": prec.get("amount"),
            "min_amount":       limits.get("amount", {}).get("min", 0),
            "max_amount":       limits.get("amount", {}).get("max"),
            "min_cost":         limits.get("cost", {}).get("min", 0),
            "taker_fee":        market.get("taker", 0.001),
            "maker_fee":        market.get("maker", 0.001),
        }

    def is_symbol_supported(self, symbol: str) -> bool:
        """
        [FIX] Cek apakah ccxt BENAR-BENAR mengenali simbol ini lewat resolusi
        pintar ex.market() -- BUKAN raw dict lookup seperti get_market_info()
        (self._markets.get(symbol, {})). Perbedaannya nyata dan penting:
        get_market_info("EVAA/USDT") return kosong (dianggap invalid) padahal
        ex.market() berhasil resolve ke "EVAA/USDT:USDT" (swap valid) --
        sebaliknya get_market_info("BONK/USDT") return market SPOT (dianggap
        valid) padahal tidak ada kontrak futures "BONKUSDT" tanpa prefix 1000x.
        Dibuktikan lewat pengujian terhadap 61 simbol nyata yang pernah gagal
        di produksi -- get_market_info salah di kedua arah, method ini benar
        untuk semuanya. Pakai method ini (bukan get_market_info) kalau
        tujuannya validasi "apakah simbol ini bisa dipakai", bukan ambil
        detail precision/limits/fee.
        """
        try:
            self._ex.market(symbol)
            return True
        except Exception:
            return False

    async def reload_markets(self) -> None:
        """
        [FIX -- insiden EVAA/USDT 13 Juli 2026, rencana perbaikan #1 dari 3]
        Paksa ccxt refetch daftar market terbaru dari exchange (bukan pakai
        cache dari load_markets() awal di connect()). Dipakai SEBELUM auto-
        scan universe futures memvalidasi simbol hasil scan lewat
        is_symbol_supported() -- scan_binance_futures_universe() hit REST
        Binance mentah, independen dari cache ccxt, jadi kalau ada simbol
        yang baru listing SETELAH connect() meng-cache market, cache lama
        bisa salah menolaknya sebagai "tidak dikenal". Pure refresh read,
        tidak menyentuh order/saldo/posisi -- kalau data pasar tidak
        berubah, hasilnya identik dengan cache lama.

        [ITEM #6 -- refresh periodik] Sekarang juga dipanggil dari loop
        periodik terjadwal (run_market_cache_refresh() di main_spot.py/
        main_future.py, tiap market_cache_refresh_interval detik, default
        3600) supaya self._markets tidak stale sepanjang umur proses --
        sebelumnya cuma di-set sekali di connect() (dan sekali lagi manual
        sebelum auto-scan futures). Dibungkus self._retry() (pola yang sama
        dgn fetch_ohlcv/fetch_ticker/dkk di file ini) supaya rate-limit/
        network blip sesaat tidak bikin reload gagal total -- kegagalan
        SETELAH retry habis tetap dibiarkan naik (raise) ke pemanggil, yang
        untuk loop periodik berarti cache lama dipertahankan sampai siklus
        berikutnya (fail-safe, self._markets tidak pernah ditimpa None/kosong).
        """
        self._markets = await self._retry(
            self._ex.load_markets, reload=True, _ep="load_markets",
        )

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            return float(self._ex.amount_to_precision(symbol, amount))
        except Exception:
            return round(amount, 8)

    def price_to_precision(self, symbol: str, price: float) -> float:
        try:
            return float(self._ex.price_to_precision(symbol, price))
        except Exception:
            return round(price, 8)

    def get_taker_fee(self, symbol: str) -> float:
        return self._markets.get(symbol, {}).get("taker", 0.001)

    def get_maker_fee(self, symbol: str) -> float:
        return self._markets.get(symbol, {}).get("maker", 0.001)

    def get_min_order_cost(self, symbol: str) -> float:
        return (
            self._markets.get(symbol, {})
            .get("limits", {})
            .get("cost", {})
            .get("min", 1.0)
        )

    def parse_balance(self, balance: dict, currency: str) -> tuple:
        """Return (free, used, total) safely — handles Bybit/OKX None fields."""
        def _f(section):
            v = (balance.get(section) or {}).get(currency)
            return float(v) if v is not None else 0.0
        free  = _f("free")
        used  = _f("used")
        total = _f("total") or (free + used)
        return free, used, total

    async def fetch_ohlcv(
        self,
        symbol:    str,
        timeframe: str = "15m",
        limit:     int = 200,
        since:     Optional[int] = None,
    ) -> List[List]:
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_ohlcv, symbol, timeframe, since, limit,
                _ep="ohlcv",
            )
        await self._log_lat("fetch_ohlcv", t0)
        return result or []

    async def fetch_ticker(self, symbol: str) -> Dict:
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_ticker, symbol, _ep="ticker"
            )
        await self._log_lat("fetch_ticker", t0)
        return result or {}

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> Dict:
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_order_book, symbol, limit, _ep="order_book"
            )
        await self._log_lat("fetch_order_book", t0)
        return result or {}

    async def fetch_open_orders(
        self, symbol: Optional[str] = None
    ) -> List[Dict]:
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_open_orders, symbol, _ep="open_orders"
            )
        await self._log_lat("fetch_open_orders", t0)
        return result or []

    async def create_order(
        self,
        symbol:     str,
        order_type: str,
        side:       str,
        amount:     float,
        price:      Optional[float] = None,
        params:     Dict            = None,
    ) -> Dict:
        """
        Dispatcher generik: kalau paper_trading, panggil _simulate_order_fill()
        (WAJIB diimplementasikan subclass). Kalau tidak, kirim order asli ke
        exchange via ccxt. Logic dispatch ini sendiri market-agnostic --
        yang beda per market cuma ISI _simulate_order_fill().

        [ITEM #15 -- Temuan C, Opsi C1] `params["reduceOnly"]` (kalau ada)
        diteruskan APA ADANYA ke ccxt utk live (sudah didukung signature
        sebelumnya, TIDAK BERUBAH) DAN SEKARANG JUGA diterjemahkan jadi
        kwarg `reduce_only=` ke _simulate_order_fill() utk paper mode --
        sebelumnya `params` sama sekali tidak diteruskan ke paper dispatch,
        jadi reduce-only tidak pernah tersimulasikan paper trading.
        """
        params = params or {}
        amount = self.amount_to_precision(symbol, amount)
        if price is not None:
            price = self.price_to_precision(symbol, price)

        if self.paper_trading:
            return await self._simulate_order_fill(
                symbol, order_type, side, amount, price,
                reduce_only=bool(params.get("reduceOnly", False)),
            )

        t0 = time.monotonic()
        async with self._throttler:
            log.info(
                "SUBMIT ORDER: %s %s %s | amount=%.8f price=%s",
                symbol, side.upper(), order_type, amount, price,
            )
            result = await self._retry(
                self._ex.create_order,
                symbol, order_type, side, amount, price, params,
                _ep="create_order",
            )
        await self._log_lat("create_order", t0)
        return result or {}

    async def _simulate_order_fill(
        self,
        symbol:     str,
        order_type: str,
        side:       str,
        amount:     float,
        price:      Optional[float],
        reduce_only: bool = False,
    ) -> Dict:
        raise NotImplementedError(
            "_simulate_order_fill() WAJIB diimplementasikan di subclass "
            "(ExchangeConnector utk spot, FutureExchangeConnector utk futures) "
            "-- semantik paper-trading balance berbeda total antara spot & futures."
        )

    async def fetch_balance(self) -> Dict:
        raise NotImplementedError(
            "fetch_balance() WAJIB diimplementasikan di subclass -- "
            "spot = saldo currency biasa, futures = margin balance + "
            "unrealized PnL + leverage info."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        if self.paper_trading or str(order_id).startswith("PAPER-"):
            existing = self._paper_orders.get(order_id)
            if existing is not None:
                existing = dict(existing)
                existing["status"] = "canceled"
                self._paper_orders[order_id] = existing
                log.warning("📝 [PAPER ORDER] cancel disimulasikan: %s", order_id)
                return existing
            return {"id": order_id, "status": "canceled", "info": {"paper_trading": True}}

        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.cancel_order, order_id, symbol, _ep="cancel_order"
            )
        await self._log_lat("cancel_order", t0)
        return result or {}

    async def fetch_order(self, order_id: str, symbol: str) -> Dict:
        if str(order_id).startswith("PAPER-") or order_id in self._paper_orders:
            existing = self._paper_orders.get(order_id)
            if existing is not None:
                return dict(existing)
            log.error(
                "📝 [PAPER ORDER] fetch_order utk id=%s tidak ditemukan di "
                "catatan simulasi -- mengembalikan status unknown, BUKAN "
                "query ke exchange asli.", order_id,
            )
            return {"id": order_id, "status": "unknown", "info": {"paper_trading": True}}

        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_order, order_id, symbol, _ep="fetch_order"
            )
        await self._log_lat("fetch_order", t0)
        return result or {}

    async def _log_lat(
        self, endpoint: str, t0: float, success: bool = True
    ) -> None:
        if self.db:
            try:
                await self.db.save_api_metric(
                    endpoint=endpoint,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    success=success,
                )
            except Exception:
                pass

    async def _retry(
        self,
        fn:       Callable,
        *args,
        retries:  int   = 3,
        delay:    float = 1.5,
        _ep:      str   = "?",
        **kwargs,
    ) -> Any:
        clean_kw   = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        last_exc: Exception = RuntimeError(
            f"_retry({_ep}): retries={retries} — tidak ada percobaan dijalankan"
        )
        for attempt in range(1, retries + 1):
            try:
                return await fn(*args, **clean_kw)
            except ccxt.RateLimitExceeded as e:
                wait = delay * (2 ** attempt)
                log.warning(
                    "Rate limit [%s] attempt %d/%d — wait %.1fs",
                    _ep, attempt, retries, wait,
                )
                await asyncio.sleep(wait)
                last_exc = e
            except ccxt.NetworkError as e:
                log.warning(
                    "Network error [%s] attempt %d: %s", _ep, attempt, e
                )
                await asyncio.sleep(delay * attempt * 3)
                last_exc = e
            except ccxt.ExchangeNotAvailable as e:
                log.error("Exchange unavailable [%s]: %s", _ep, e)
                await asyncio.sleep(delay * attempt * 2)
                last_exc = e
            except (ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:
                log.error(
                    "Hard error [%s]: %s — not retrying", _ep, e
                )
                raise
            except Exception as e:
                log.error(
                    "Unexpected error [%s] attempt %d: %s", _ep, attempt, e
                )
                raise
        raise last_exc
