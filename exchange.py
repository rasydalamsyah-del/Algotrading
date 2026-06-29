"""
exchange.py
AlgoTrader Pro v7.0

"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any, List, Tuple

import ccxt.pro as ccxt
from asyncio_throttle import Throttler

log = logging.getLogger("exchange")

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class ExchangeConnector:
    def __init__(
        self,
        exchange_id:         str,
        api_key:             str,
        api_secret:          str,
        api_passphrase:      str   = "",
        testnet:             bool  = True,
        requests_per_second: float = 5.0,
        db=None,
    ):
        self.exchange_id = exchange_id
        self.testnet     = testnet
        self.db          = db
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
                "defaultType": "spot",
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

    async def connect(self) -> bool:
        try:
            # Sync local drift against exchange server clock to avoid
            # InvalidNonce (-1021) on signed endpoints.
            await self._ex.load_time_difference()
            self._markets = await self._ex.load_markets()
            # Balance requires API credentials; allow "public-only" connect
            # (useful for smoke tests / indicator pipelines).
            if getattr(self._ex, "apiKey", None) and getattr(self._ex, "secret", None):
                await self._ex.fetch_balance()
            self.is_connected = True
            log.info(
                "Connected to %s (%s) | %d markets loaded",
                self.exchange_id.upper(),
                "TESTNET" if self.testnet else "LIVE",
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

    async def fetch_balance(self) -> Dict:
        if not (getattr(self._ex, "apiKey", None) and getattr(self._ex, "secret", None)):
            return {}
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_balance, _ep="balance"
            )
        await self._log_lat("fetch_balance", t0)
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
        params = params or {}
        amount = self.amount_to_precision(symbol, amount)
        if price is not None:
            price = self.price_to_precision(symbol, price)

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

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.cancel_order, order_id, symbol, _ep="cancel_order"
            )
        await self._log_lat("cancel_order", t0)
        return result or {}

    async def fetch_order(self, order_id: str, symbol: str) -> Dict:
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
        # [BUG-FIX] raise None kalau retries<=0.
        # Sebelumnya: last_exc diinisialisasi None, dan kalau `retries` <= 0,
        # loop `for attempt in range(1, retries+1)` tidak pernah jalan sama
        # sekali, jadi `raise last_exc` di akhir fungsi me-raise None →
        # "TypeError: exceptions must derive from BaseException", menutupi
        # error aslinya. Tidak ada caller saat ini yang pakai retries=0, tapi
        # signature mengizinkannya jadi tetap dijaga.
        # Sekarang: fallback ke RuntimeError yang jelas kalau itu terjadi.
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

class WebSocketFeed:

    MAX_STALE_SECS = 30

    def __init__(
        self,
        exchange_id:     str,
        api_key:         str,
        api_secret:      str,
        api_passphrase:  str              = "",
        symbols:         List[str]        = None,
        testnet:         bool             = True,
        reconnect_delay: int              = 5,
        max_retries:     int              = 10,
        on_ticker:       Optional[Callable] = None,
        on_orderbook:    Optional[Callable] = None,
    ):
        self.symbols         = symbols or []
        self.reconnect_delay = reconnect_delay
        self.max_retries     = max_retries
        self.on_ticker       = on_ticker
        self.on_orderbook    = on_orderbook

        cls = getattr(ccxt, exchange_id)
        ws_config = {
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
                "recvWindow": 10000,
            },
        }
        if api_passphrase:
            ws_config["password"] = api_passphrase
        self._ex: ccxt.Exchange = cls(ws_config)
        if testnet and hasattr(self._ex, "set_sandbox_mode"):
            self._ex.set_sandbox_mode(True)

        self.live_tickers:    Dict[str, Dict] = {}
        self.live_orderbooks: Dict[str, Dict] = {}

        self._last_ticker_upd: Dict[str, float] = {}
        self._last_ob_upd:     Dict[str, float] = {}
        # [BUG-FIX] Crash kalau symbols=None (default parameter).
        # Sebelumnya: dict comprehension di sini pakai parameter `symbols` mentah,
        # bukan `self.symbols` (yang sudah di-guard `symbols or []` di atas) —
        # kalau caller tidak mengisi `symbols`, baris ini raise
        # "TypeError: 'NoneType' object is not iterable" karena None bukan iterable.
        # Sekarang: konsisten pakai `self.symbols`.
        self._ticker_dead:     Dict[str, bool]  = {s: False for s in self.symbols}
        self._ob_dead:         Dict[str, bool]  = {s: False for s in self.symbols}
        self._poll_error_count: Dict[str, int] = {s: 0 for s in self.symbols}
        self._feed_mode: Dict[str, str] = {s: "REST_FALLBACK" for s in self.symbols}

        self._running = False
        self._tasks:  List[asyncio.Task] = []

        rest_cls = getattr(ccxt, exchange_id)
        self._rest_exchange: ccxt.Exchange = rest_cls({
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
                "recvWindow": 10000,
            },
        })
        if testnet and hasattr(self._rest_exchange, "set_sandbox_mode"):
            self._rest_exchange.set_sandbox_mode(True)

    @property
    def _stale_threshold(self) -> int:
        return max(self.MAX_STALE_SECS, min(len(self.symbols) * 3, 120))

    async def start(self) -> None:
        self._running = True
        # Skip WebSocket for exchanges that don't support it (e.g. Binance Spot)
        ws_supported = hasattr(self._ex, "watch_ticker")
        if ws_supported:
            log.info("Starting market feed (WS primary + REST fallback).")
            # Gunakan watch_tickers (multiplexed) kalau didukung, fallback ke per-symbol
            if hasattr(self._ex, "watch_tickers"):
                self._tasks.append(asyncio.create_task(self._watch_tickers_all(), name="ws_tickers_all"))
            else:
                for symbol in self.symbols:
                    self._tasks.append(asyncio.create_task(self._watch_ticker(symbol), name=f"ws_ticker_{symbol}"))
            # Orderbook: REST polling saja untuk semua koin
            # WS orderbook dibuka on-demand saat koin masuk pipeline
            log.info("Orderbook mode: REST polling (on-demand WS per koin aktif)")
            self._tasks.append(asyncio.create_task(self._poll_orderbooks_rest(), name="poll_ob_rest"))
        else:
            log.info("Starting market feed (REST polling only — WS not supported for this exchange).")
            for symbol in self.symbols:
                self._feed_mode[symbol] = "REST_POLLING"
        self._tasks.append(asyncio.create_task(self._poll_tickers(), name="ws_poll_tickers"))

    async def _poll_tickers(self) -> None:
        while self._running:
            tasks = [
                self._poll_one_ticker(symbol)
                for symbol in self.symbols
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, result in zip(self.symbols, results):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    cnt = self._poll_error_count.get(symbol, 0) + 1
                    self._poll_error_count[symbol] = cnt
                    if cnt == 1 or cnt % 10 == 0:
                        log.warning(
                            "REST ticker poll error [%s] #%d: %s",
                            symbol, cnt, result,
                        )
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
    
    async def _poll_one_ticker(self, symbol: str) -> None:
        # Prefer WS. Poll only when stale/dead to reduce REST pressure.
        if self.is_feed_healthy(symbol) and not self._ticker_dead.get(symbol, False):
            self._feed_mode[symbol] = "WS_LIVE"
            return
        tk  = await self._rest_exchange.fetch_ticker(symbol)
        now = time.time()
        self.live_tickers[symbol] = {
            "symbol":       symbol,
            "last":         tk.get("last"),
            "bid":          tk.get("bid"),
            "ask":          tk.get("ask"),
            "change_pct":   tk.get("percentage"),
            "volume":       tk.get("baseVolume"),
            "quote_volume": tk.get("quoteVolume"),
            "high_24h":     tk.get("high"),
            "low_24h":      tk.get("low"),
            "vwap_24h":     tk.get("vwap"),
            "_ts":          now,
        }
        self._last_ticker_upd[symbol] = now
        self._ticker_dead[symbol]     = False
        self._poll_error_count[symbol] = 0
        self._feed_mode[symbol] = "REST_FALLBACK"
    
        if self.on_ticker:
            try:
                await self.on_ticker(symbol, self.live_tickers[symbol])
            except Exception as cb_err:
                log.debug("on_ticker callback error [%s]: %s", symbol, cb_err)
            except asyncio.CancelledError:
                return

    async def add_symbols(self, new_symbols: List[str]) -> None:
        """
        Tambah simbol baru ke feed secara runtime tanpa restart.
        Spawn task baru untuk ticker dan orderbook watch.
        Aman dipanggil saat feed sedang berjalan.
        """
        added = []
        for symbol in new_symbols:
            if symbol in self.symbols:
                continue  # sudah ada, skip

            # Inisialisasi tracking dict untuk simbol baru
            self.symbols.append(symbol)
            self._ticker_dead[symbol]      = False
            self._ob_dead[symbol]          = False
            self._poll_error_count[symbol] = 0
            self._feed_mode[symbol]        = "REST_FALLBACK"
            self._last_ticker_upd[symbol]  = 0.0
            self._last_ob_upd[symbol]      = 0.0
            added.append(symbol)

        if not added:
            return

        if not self._running:
            log.warning("add_symbols dipanggil saat feed tidak running — symbols ditambah tapi task tidak di-spawn.")
            return

        # Spawn task baru untuk simbol yang ditambah
        ws_supported = hasattr(self._ex, "watch_ticker")
        for symbol in added:
            if ws_supported:
                self._tasks.append(
                    asyncio.create_task(
                        self._watch_ticker(symbol),
                        name=f"ws_ticker_{symbol}",
                    )
                )
                self._tasks.append(
                    asyncio.create_task(
                        self._watch_orderbook(symbol),
                        name=f"ws_ob_{symbol}",
                    )
                )
            else:
                self._feed_mode[symbol] = "REST_POLLING"

        log.info(
            "WebSocketFeed: +%d simbol baru ditambah runtime: %s",
            len(added), added,
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        try:
            await self._ex.close()
        except Exception:
            pass
        try:
            await self._rest_exchange.close()
        except Exception:
            pass

        log.info("WebSocket feed stopped.")

    async def _watch_tickers_all(self) -> None:
        """
        Watch semua ticker sekaligus via satu koneksi multiplexed.
        Jauh lebih efisien dari per-symbol WS untuk 500+ koin.
        """
        retries = 0
        while self._running and retries < self.max_retries:
            try:
                while self._running:
                    tickers = await self._ex.watch_tickers(self.symbols)
                    now = time.time()
                    for symbol, tk in tickers.items():
                        self.live_tickers[symbol] = {
                            "symbol":       symbol,
                            "last":         tk.get("last"),
                            "bid":          tk.get("bid"),
                            "ask":          tk.get("ask"),
                            "change_pct":   tk.get("percentage"),
                            "volume":       tk.get("baseVolume"),
                            "quote_volume": tk.get("quoteVolume"),
                            "high_24h":     tk.get("high"),
                            "low_24h":      tk.get("low"),
                            "vwap_24h":     tk.get("vwap"),
                            "_ts":          now,
                        }
                        self._last_ticker_upd[symbol] = now
                        self._ticker_dead[symbol]     = False
                        self._feed_mode[symbol]       = "WS_LIVE"
                        if self.on_ticker:
                            await self.on_ticker(symbol, self.live_tickers[symbol])
                    retries = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                retries += 1
                wait = self.reconnect_delay * retries
                log.warning("watch_tickers_all retry %d/%d: %s — wait %ds",
                            retries, self.max_retries, e, wait)
                await asyncio.sleep(wait)
        log.critical("watch_tickers_all DEAD after %d retries.", self.max_retries)

    async def _watch_ticker(self, symbol: str) -> None:
        retries = 0
        while self._running and retries < self.max_retries:
            try:
                while self._running:
                    tk  = await self._ex.watch_ticker(symbol)
                    now = time.time()
                    self.live_tickers[symbol] = {
                        "symbol":       symbol,
                        "last":         tk.get("last"),
                        "bid":          tk.get("bid"),
                        "ask":          tk.get("ask"),
                        "change_pct":   tk.get("percentage"),
                        "volume":       tk.get("baseVolume"),
                        "quote_volume": tk.get("quoteVolume"),
                        "high_24h":     tk.get("high"),
                        "low_24h":      tk.get("low"),
                        "vwap_24h":     tk.get("vwap"),
                        "_ts":          now,
                    }
                    self._last_ticker_upd[symbol] = now
                    self._ticker_dead[symbol]     = False
                    self._poll_error_count[symbol] = 0
                    self._feed_mode[symbol] = "WS_LIVE"
                    retries = 0
                    if self.on_ticker:
                        await self.on_ticker(symbol, self.live_tickers[symbol])
            except asyncio.CancelledError:
                break
            except Exception as e:
                retries += 1
                wait = self.reconnect_delay * retries
                log.warning(
                    "Ticker WS [%s] retry %d/%d: %s — wait %ds",
                    symbol, retries, self.max_retries, e, wait,
                )
                if retries >= self.max_retries:
                    self._ticker_dead[symbol] = True
                    self._feed_mode[symbol] = "WS_DEGRADED"
                    log.critical(
                        "WS ticker DEAD for %s after %d retries.",
                        symbol, self.max_retries,
                    )
                    break
                await asyncio.sleep(wait)

    async def _poll_orderbooks_rest(self) -> None:
        """
        Poll orderbook via REST untuk semua koin secara bergiliran.
        Lebih efisien dari 500 koneksi WS orderbook sekaligus.
        """
        while self._running:
            try:
                for symbol in list(self.symbols):
                    if not self._running:
                        break
                    try:
                        ob = await self._ex.fetch_order_book(symbol, limit=20)
                        self.live_orderbooks[symbol] = {
                            "bids": ob.get("bids", []),
                            "asks": ob.get("asks", []),
                            "_ts":  time.time(),
                        }
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.debug("poll_ob_rest %s error: %s", symbol, e)
                    await asyncio.sleep(0.05)  # 50ms antar koin
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("poll_orderbooks_rest error: %s", e)
                await asyncio.sleep(5)

    async def _watch_orderbook(self, symbol: str) -> None:
        retries = 0
        while self._running and retries < self.max_retries:
            try:
                while self._running:
                    ob  = await self._ex.watch_order_book(symbol, limit=20)
                    now = time.time()
                    self.live_orderbooks[symbol] = {
                        "symbol": symbol,
                        "bids":   ob.get("bids", [])[:20],
                        "asks":   ob.get("asks", [])[:20],
                        "_ts":    now,
                    }
                    self._last_ob_upd[symbol] = now
                    self._ob_dead[symbol]     = False
                    retries = 0
                    if self.on_orderbook:
                        await self.on_orderbook(
                            symbol, self.live_orderbooks[symbol]
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                retries += 1
                wait = self.reconnect_delay * retries
                log.warning(
                    "OB WS [%s] retry %d/%d: %s — wait %ds",
                    symbol, retries, self.max_retries, e, wait,
                )
                if retries >= self.max_retries:
                    self._ob_dead[symbol] = True
                    log.critical("WS orderbook DEAD for %s.", symbol)
                    break
                await asyncio.sleep(wait)

    def get_price(self, symbol: str) -> Optional[float]:
        return self.live_tickers.get(symbol, {}).get("last")

    # [TAMBAHAN] get_orderbook() belum ada — api_server.py endpoint
    # GET /api/orderbook/{symbol} memanggil `b.ws_feed.get_orderbook(sym)` tapi
    # method ini tidak pernah didefinisikan di WebSocketFeed, jadi endpoint itu
    # selalu raise AttributeError → ditangkap try/except generik → selalu balas
    # HTTP 502 ke client, tidak peduli data orderbook-nya sebenarnya ada atau
    # tidak di self.live_orderbooks. Ditambahkan mengikuti pola get_price/
    # get_mid_price yang sudah ada (lookup langsung ke dict live, default {}).
    def get_orderbook(self, symbol: str) -> Dict:
        return self.live_orderbooks.get(symbol, {})

    def get_mid_price(self, symbol: str) -> Optional[float]:
        t   = self.live_tickers.get(symbol, {})
        bid = t.get("bid")
        ask = t.get("ask")
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        last = t.get("last")
        return float(last) if last else None

    def get_spread(self, symbol: str) -> Optional[float]:
        t   = self.live_tickers.get(symbol, {})
        bid = t.get("bid")
        ask = t.get("ask")
        if bid and ask and float(ask) > 0:
            return (float(ask) - float(bid)) / float(ask) * 100
        return None

    def get_spread_absolute(self, symbol: str) -> Optional[float]:
        t   = self.live_tickers.get(symbol, {})
        bid = t.get("bid")
        ask = t.get("ask")
        return (float(ask) - float(bid)) if bid and ask else None
        
    def get_current_spread_pct(self, symbol: str) -> Optional[float]:
        return self.get_spread(symbol)
        
    def get_quote_volume_24h(self, symbol: str) -> float:
        t  = self.live_tickers.get(symbol, {})
        qv = t.get("quote_volume")
        if qv and float(qv) > 0:
            return float(qv)
        bv   = t.get("volume", 0)
        last = t.get("last", 0)
        return float(bv) * float(last) if bv and last else 0.0

    def get_market_depth_slippage(
        self,
        symbol:           str,
        side:             str,
        order_value_usdt: float,
    ) -> Tuple[float, float]:
        ob     = self.live_orderbooks.get(symbol, {})
        levels = ob.get("asks" if side == "buy" else "bids", [])
        mid    = self.get_mid_price(symbol) or 0.0

        if not levels:
            return (mid, 0.0)

        remaining    = order_value_usdt
        weighted_sum = 0.0
        total_filled = 0.0

        for price_lvl, qty_lvl in levels:
            if price_lvl <= 0 or qty_lvl <= 0:
                continue
            fill_usdt     = min(remaining, price_lvl * qty_lvl)
            fill_qty      = fill_usdt / price_lvl
            weighted_sum += price_lvl * fill_qty
            total_filled += fill_qty
            remaining    -= fill_usdt
            if remaining <= 0:
                break

        if total_filled <= 0:
            return (mid, 0.0)

        avg_fill = weighted_sum / total_filled
        slippage = abs(avg_fill - mid) / mid * 100 if mid > 0 else 0.0
        return (avg_fill, slippage)

    def is_feed_healthy(
        self, symbol: str, max_stale: Optional[int] = None
    ) -> bool:
        threshold = max_stale if max_stale is not None else self._stale_threshold
        if self._ticker_dead.get(symbol, False):
            return False
        return (
            time.time() - self._last_ticker_upd.get(symbol, 0)
        ) < threshold

    def is_orderbook_healthy(
        self, symbol: str, max_stale: Optional[int] = None
    ) -> bool:
        threshold = max_stale if max_stale is not None else self._stale_threshold
        if self._ob_dead.get(symbol, False):
            return False
        return (
            time.time() - self._last_ob_upd.get(symbol, 0)
        ) < threshold

    def get_feed_status(self) -> Dict[str, Dict]:
        now = time.time()
        return {
            sym: {
                "feed_mode":       self._feed_mode.get(sym, "REST_FALLBACK"),
                "ticker_healthy":  self.is_feed_healthy(sym),
                "ob_healthy":      self.is_orderbook_healthy(sym),
                "ticker_age_secs": round(
                    now - self._last_ticker_upd.get(sym, 0), 1
                ),
                "ob_age_secs": round(
                    now - self._last_ob_upd.get(sym, 0), 1
                ),
                "ticker_dead":  self._ticker_dead.get(sym, False),
                "ob_dead":      self._ob_dead.get(sym, False),
                "last_price":   self.get_price(sym),
                "mid_price":    self.get_mid_price(sym),
                "spread_pct":   self.get_spread(sym),
            }
            for sym in self.symbols
        }


# ═══════════════════════════════════════════════════════════════
#  Auto-scan universe dari Binance — tanpa API key (public)
#  Hasil disimpan ke universe.json + universe_overrides DB
# ═══════════════════════════════════════════════════════════════
import urllib.request as _urllib_request
import json as _json
import ssl as _ssl
from datetime import datetime as _datetime

_STABLES  = {
    "USDC","BUSD","DAI","TUSD","FDUSD","USDD","USDP",
    "USDT","UST","USTC","USD1","EUR","GBP","AUD","BVND",
}
_LEVERAGE = ["UP","DOWN","BULL","BEAR"]
_UNIVERSE_FILE = "universe.json"


def _fetch_binance_tickers() -> list:
    """Hit Binance public API, return raw list ticker 24hr."""
    urls = [
        "https://api.binance.com/api/v3/ticker/24hr",
        "https://api1.binance.com/api/v3/ticker/24hr",
        "https://api2.binance.com/api/v3/ticker/24hr",
    ]
    import certifi as _certifi
    ctx = _ssl.create_default_context(cafile=_certifi.where())
    for url in urls:
        try:
            req  = _urllib_request.urlopen(url, timeout=15, context=ctx)
            data = _json.loads(req.read())
            log.info("scan_universe: fetch sukses dari %s (%d tickers)", url, len(data))
            return data
        except Exception as e:
            log.warning("scan_universe: gagal %s — %s", url, e)
    return []



def _fetch_binance_trading_symbols() -> set:
    """Fetch exchangeInfo, return set symbol yang statusnya TRADING saja."""
    urls = [
        "https://api.binance.com/api/v3/exchangeInfo",
        "https://api1.binance.com/api/v3/exchangeInfo",
        "https://api2.binance.com/api/v3/exchangeInfo",
    ]
    import certifi as _certifi
    ctx = _ssl.create_default_context(cafile=_certifi.where())
    for url in urls:
        try:
            req  = _urllib_request.urlopen(url, timeout=15, context=ctx)
            data = _json.loads(req.read())
            trading = {
                s["symbol"]
                for s in data.get("symbols", [])
                if s.get("status") == "TRADING"
            }
            log.info("scan_universe: %d symbol TRADING dari exchangeInfo", len(trading))
            return trading
        except Exception as e:
            log.warning("scan_universe: exchangeInfo gagal %s — %s", url, e)
    return set()

def scan_binance_universe(
    min_volume_usdt: float = 100_000,
    max_coins:       int   = 500,
    quote:           str   = "USDT",
) -> list:
    """
    Scan koin paling likuid di Binance.
    Return list of dict: [{"symbol": "BTC/USDT", "volume_24h": 1688600000}, ...]
    """
    raw = _fetch_binance_tickers()
    if not raw:
        log.error("scan_universe: tidak ada data dari Binance.")
        return []

    # Ambil hanya symbol yang benar-benar TRADING di Binance
    _trading_symbols = _fetch_binance_trading_symbols()

    results = []
    for t in raw:
        sym = t.get("symbol", "")
        if not sym.endswith(quote):
            continue
        # Skip symbol yang tidak TRADING (BREAK, delisted, dll)
        if _trading_symbols and sym not in _trading_symbols:
            continue
        base = sym[:-len(quote)]
        # Filter stablecoin
        if base in _STABLES:
            continue
        # Filter leverage token
        if any(base.endswith(lv) or base.startswith(lv) for lv in _LEVERAGE):
            continue
        # Filter karakter non-ASCII (nama koin aneh/Chinese)
        if not base.isascii() or not base.isalnum():
            continue
        vol = float(t.get("quoteVolume", 0))
        if vol < min_volume_usdt:
            continue
        results.append({
            "symbol":     f"{base}/{quote}",
            "volume_24h": round(vol, 2),
        })

    results.sort(key=lambda x: x["volume_24h"], reverse=True)
    results = results[:max_coins]
    log.info(
        "scan_universe: %d koin lolos filter (min_vol=$%.0fM, max=%d)",
        len(results), min_volume_usdt / 1_000_000, max_coins,
    )
    return results


def save_universe_json(coins: list) -> None:
    """Simpan hasil scan ke universe.json."""
    data = {
        "scanned_at":     _datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_coins":    len(coins),
        "min_volume_usd": 10_000_000,
        "symbols":        coins,
    }
    with open(_UNIVERSE_FILE, "w") as f:
        _json.dump(data, f, indent=2)
    log.info("scan_universe: hasil disimpan ke %s (%d koin)", _UNIVERSE_FILE, len(coins))


def load_universe_json() -> list:
    """Baca universe.json, return list symbol string."""
    try:
        with open(_UNIVERSE_FILE) as f:
            data = _json.load(f)
        symbols = [c["symbol"] for c in data.get("symbols", [])]
        log.info("load_universe: %d koin dari %s (scan: %s)",
                 len(symbols), _UNIVERSE_FILE, data.get("scanned_at","?"))
        return symbols
    except FileNotFoundError:
        log.warning("load_universe: %s tidak ditemukan.", _UNIVERSE_FILE)
        return []
    except Exception as e:
        log.error("load_universe: gagal baca — %s", e)
        return []


async def auto_scan_and_populate(db) -> list:
    """
    Fungsi utama dipanggil saat bot startup.
    Cek DB apakah perlu scan ulang, lakukan scan, populate universe_overrides.
    Return: list symbol aktif (dari DB universe_overrides atau universe.json)
    """
    # Cek flag auto_scan di DB
    flag = await db.get_bot_state("auto_scan_universe")
    should_scan = (flag == "true")

    if should_scan:
        log.info("auto_scan_universe=true — mulai scan Binance...")
        # [BUG-FIX] scan_binance_universe() pakai urllib.request sinkron (blocking)
        # tapi dipanggil langsung dari fungsi async — selama panggilan HTTP
        # berjalan (bisa sampai ~15s x 3 fallback URL), seluruh event loop
        # asyncio nge-freeze, termasuk request lain yang sedang dilayani
        # api_server.py kalau startup & web server jalan di proses yang sama.
        # Sekarang: dijalankan di thread pool lewat run_in_executor agar event
        # loop tidak terblokir.
        loop = asyncio.get_running_loop()
        coins = await loop.run_in_executor(
            None, scan_binance_universe, 100_000, 500,
        )

        if coins:
            # Simpan ke universe.json
            save_universe_json(coins)

            # Nonaktifkan semua koin lama di universe_overrides
            old_symbols = await db.get_active_universe_overrides()
            for sym in old_symbols:
                await db.deactivate_universe_override(sym)
            log.info("auto_scan: %d koin lama dinonaktifkan", len(old_symbols))

            # Upsert koin baru hasil scan
            for coin in coins:
                vol_m = coin["volume_24h"] / 1_000_000
                await db.upsert_universe_override(
                    symbol=coin["symbol"],
                    source="auto_scan",
                    notes=f"vol_24h=${vol_m:.1f}M scanned_at={_datetime.utcnow().strftime('%Y-%m-%d')}",
                )
            log.info("auto_scan: %d koin baru dimasukkan ke universe_overrides", len(coins))

            # Reset flag ke false
            await db.set_bot_state("auto_scan_universe", "false")
            log.info("auto_scan: flag auto_scan_universe direset ke false")

            return [c["symbol"] for c in coins]
        else:
            log.error("auto_scan: scan gagal, fallback ke universe.json / .env")

    # Tidak scan — baca dari universe.json kalau ada
    from_json = load_universe_json()
    if from_json:
        return from_json

    # Fallback terakhir — baca dari universe_overrides DB
    from_db = await db.get_active_universe_overrides()
    if from_db:
        log.info("auto_scan: %d koin dari universe_overrides DB", len(from_db))
        return from_db

    log.warning("auto_scan: tidak ada sumber universe, pakai .env")
    return []
