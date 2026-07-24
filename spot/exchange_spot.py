"""
exchange.py
AlgoTrader Pro v7.0

"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any, List, Tuple

import ccxt.pro as ccxt

from engine.exchange_base import BaseExchangeConnector

log = logging.getLogger("exchange")

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _extract_bad_symbol_from_error(e: Exception) -> Optional[str]:
    """Ekstrak simbol dari pesan error format ccxt 'X does not have market
    symbol Y'. Dipakai baik untuk exception ccxt.BadSymbol yang tipenya
    benar, MAUPUN sebagai circuit-breaker pola-pesan di except Exception
    generik _watch_tickers_all() untuk exception lain yang kebetulan
    membawa pesan identik -- ditemukan lewat insiden EVAA/USDT 13 Juli 2026
    (~3 jam siklus mati-restart), di mana exception yang tertangkap TERNYATA
    BUKAN ccxt.BadSymbol (dibuktikan lewat investigasi source ccxt + repro
    langsung), tapi pesannya persis sama -- root cause tipe exception
    aslinya belum terkonfirmasi. Regex ini struktural unik: dikonfirmasi
    lewat grep seluruh source ccxt, frasa "does not have market symbol"
    HANYA pernah dihasilkan oleh raise BadSymbol() di ccxt/base/exchange.py
    market() -- tidak ada exception lain (rate-limit/network/auth/timeout)
    yang pernah menghasilkan pola pesan ini."""
    m = re.search(r"does not have market symbol (\S+)", str(e))
    return m.group(1) if m else None

class ExchangeConnector(BaseExchangeConnector):
    """
    Spot-specific ExchangeConnector -- extend BaseExchangeConnector (engine/
    exchange_base.py) yang menangani semua bagian generik (connect, precision
    helpers, fetch_ohlcv/ticker/orderbook, create_order dispatcher, cancel_order,
    fetch_order, retry logic). Di sini cuma tersisa yang benar-benar spot-specific:
    fetch_balance() (saldo virtual 1 currency) dan _simulate_order_fill()
    (update _paper_balance langsung, tanpa leverage/margin).
    """
    def __init__(
        self,
        exchange_id:         str,
        api_key:             str,
        api_secret:          str,
        api_passphrase:      str   = "",
        testnet:             bool  = True,
        requests_per_second: float = 5.0,
        db=None,
        paper_trading:       bool  = False,
        initial_capital:     float = 1000.0,
        quote_currency:      str   = "USDT",
    ):
        super().__init__(
            exchange_id=exchange_id,
            api_key=api_key,
            api_secret=api_secret,
            default_type="spot",
            api_passphrase=api_passphrase,
            testnet=testnet,
            requests_per_second=requests_per_second,
            db=db,
        )
        # [FITUR -- PAPER TRADING MODE] Kalau True: data pasar (ticker/
        # orderbook/candle/fee) TETAP 100% ASLI dari exchange (dibaca normal,
        # tidak disentuh sama sekali) -- TAPI create_order()/cancel_order()
        # TIDAK PERNAH benar-benar dikirim ke exchange. Order disimulasikan
        # pakai harga pasar RIIL saat itu (dari fetch_ticker asli), diberi ID
        # unik berawalan "PAPER-". Tidak ada jalur di sini yang bisa membuat
        # create_order/cancel_order asli terpanggil ketika paper_trading True
        # -- lihat _simulate_order_fill(), yang dipanggil via create_order()
        # dispatcher di base class SEBELUM baris manapun yang menyentuh
        # self._ex.create_order.
        self.paper_trading    = paper_trading
        self._paper_orders: Dict[str, Dict] = {}
        # [PAPER TRADING] Saldo virtual TERISOLASI dari saldo real exchange.
        # Diinisialisasi HANYA dari INITIAL_CAPITAL, bukan pernah dibaca dari
        # akun asli. fetch_balance() akan mengembalikan dict ini (bukan query
        # ke exchange) selama paper_trading=True.
        self._paper_quote_ccy = quote_currency
        self._paper_balance: Dict[str, float] = {quote_currency: float(initial_capital)}

        if self.paper_trading:
            log.warning(
                "📝 PAPER TRADING MODE AKTIF (SPOT) — data pasar 100%% ASLI (harga/"
                "orderbook/fee dari exchange sungguhan), TAPI order TIDAK "
                "PERNAH benar-benar dikirim ke exchange. Semua order "
                "disimulasikan pakai harga pasar riil saat itu. "
                "Order ID disimulasi selalu berawalan 'PAPER-'."
            )
            log.warning(
                "📝 [PAPER BALANCE] Modal virtual: %.2f %s — TERISOLASI dari "
                "saldo real akun exchange. fetch_balance() tidak akan pernah "
                "membaca saldo asli selama mode ini aktif.",
                self._paper_balance[quote_currency], quote_currency,
            )

    def hydrate_from_positions(self, positions) -> int:
        """[HYDRATION FIX -- insiden restart 2026-07-20 21:48: reconcile
        menutup paksa SEMUA posisi paper (BERA/JTO spot, WIF/AIGENSYN
        futures) krn _paper_balance amnesia pasca-restart] Rekonstruksi
        saldo virtual dari DB open positions. HARUS dipanggil SEBELUM
        reconciliation startup. No-op kalau bukan paper mode. Fee entry
        sesi lama TIDAK didebit ulang (sudah terbayar di sesi lalu; bias
        kecil yang diterima secara sadar). Return: jumlah posisi terhidrasi.
        """
        if not self.paper_trading:
            return 0
        count = 0
        for pos in positions:
            try:
                symbol = getattr(pos, "symbol", None) or pos.get("symbol")
                amount = float(getattr(pos, "amount", None) or pos.get("amount") or 0)
                entry  = float(getattr(pos, "entry_price", None) or pos.get("entry_price") or 0)
            except Exception:
                continue
            if not symbol or amount <= 0 or entry <= 0:
                continue
            base, _, quote = symbol.partition("/")
            cost = amount * entry
            self._paper_balance[base]  = self._paper_balance.get(base, 0.0) + amount
            self._paper_balance[quote] = self._paper_balance.get(quote, 0.0) - cost
            count += 1
            log.warning(
                "📝 [PAPER HYDRATE] %s: +%.8f %s / -%.4f %s (cost basis dari DB)",
                symbol, amount, base, cost, quote,
            )
        return count

    async def fetch_balance(self) -> Dict:
        # [PAPER TRADING] JANGAN PERNAH query exchange asli untuk saldo --
        # kembalikan saldo virtual (dari INITIAL_CAPITAL + hasil simulasi
        # fill) dalam format yang identik dengan respons ccxt asli, supaya
        # parse_balance() dan seluruh kode caller (main.py) berjalan tanpa
        # perubahan apapun di sisi mereka.
        if self.paper_trading:
            free  = dict(self._paper_balance)
            used  = {k: 0.0 for k in self._paper_balance}
            total = dict(self._paper_balance)
            return {"free": free, "used": used, "total": total}

        if not (getattr(self._ex, "apiKey", None) and getattr(self._ex, "secret", None)):
            return {}
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_balance, _ep="balance"
            )
        await self._log_lat("fetch_balance", t0)
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
        """[PAPER TRADING] Simulasikan fill order pakai harga pasar RIIL
        (fetch_ticker asli, read-only, tidak pernah menyentuh endpoint order).

        [ITEM #15 -- Temuan C] `reduce_only` diterima HANYA utk kompatibilitas
        signature dgn dispatcher generik (engine/exchange_base.py::create_order())
        -- TIDAK DIPAKAI di sini. Spot paper trading mensimulasikan SALDO
        per-currency (bukan "posisi" spt futures), item #15 (root cause
        _paper_positions futures) tidak berlaku di spot sama sekali.

        Untuk order "market": fill di best ask (buy) / best bid (sell) harga
        SAAT INI dari exchange sungguhan.
        Untuk order "limit" (dipakai execution.py sbg marketable-limit
        fallback, harga sengaja sedikit di atas/bawah pasar spy pasti fill):
        fill di harga yang LEBIH BAIK utk trader antara harga pasar riil dan
        limit price -- persis meniru perilaku marketable-limit order asli.
        """
        ticker = await self.fetch_ticker(symbol)  # data ASLI, read-only
        bid = float(ticker.get("bid") or ticker.get("last") or price or 0.0)
        ask = float(ticker.get("ask") or ticker.get("last") or price or 0.0)

        if order_type == "market" or price is None:
            fill_price = ask if side == "buy" else bid
        else:
            # marketable-limit: fill di harga terbaik yg masih memenuhi limit
            fill_price = min(ask, price) if side == "buy" else max(bid, price)

        fill_price = self.price_to_precision(symbol, fill_price) or fill_price
        order_id = f"PAPER-{uuid.uuid4().hex[:16]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        # [PAPER TRADING] Update saldo virtual -- fee dihitung pakai
        # taker_fee ASLI dari market info (data publik, bukan simulasi),
        # dipotong dalam quote currency, meniru perilaku fill real.
        base, _, quote = symbol.partition("/")
        fee_rate = self.get_taker_fee(symbol)
        fee_amt  = amount * fill_price * fee_rate
        if side == "buy":
            cost = amount * fill_price
            self._paper_balance[quote] = self._paper_balance.get(quote, 0.0) - cost - fee_amt
            self._paper_balance[base]  = self._paper_balance.get(base, 0.0) + amount
        else:
            proceeds = amount * fill_price
            self._paper_balance[base]  = self._paper_balance.get(base, 0.0) - amount
            self._paper_balance[quote] = self._paper_balance.get(quote, 0.0) + proceeds - fee_amt

        # Clamp -- jaga-jaga floating point drift bikin saldo jadi negatif
        # tipis (mis. -0.0000001) akibat pembulatan; risk.py seharusnya
        # sudah mencegah oversell, ini hanya pengaman kosmetik terakhir.
        for ccy in (base, quote):
            if -1e-6 < self._paper_balance.get(ccy, 0.0) < 0:
                self._paper_balance[ccy] = 0.0

        log.info(
            "📝 [PAPER BALANCE] setelah fill %s %s: %s=%.4f %s=%.8f",
            side.upper(), symbol, quote, self._paper_balance.get(quote, 0.0),
            base, self._paper_balance.get(base, 0.0),
        )

        order = {
            "id":        order_id,
            "symbol":    symbol,
            "type":      order_type,
            "side":      side,
            "status":    "closed",       # paper order selalu langsung "terisi"
            "amount":    amount,
            "filled":    amount,
            "remaining": 0.0,
            "average":   fill_price,
            "price":     fill_price,
            "cost":      amount * fill_price,
            "fee":       {"cost": fee_amt, "currency": quote},
            "timestamp": int(time.time() * 1000),
            "datetime":  now_iso,
            "info":      {"paper_trading": True, "note": "Simulated fill, no real order sent."},
        }
        self._paper_orders[order_id] = order

        log.warning(
            "📝 [PAPER ORDER] %s %s %s | amount=%.8f @ %.8f (harga pasar RIIL "
            "saat simulasi) | id=%s — TIDAK dikirim ke exchange asli.",
            symbol, side.upper(), order_type, amount, fill_price, order_id,
        )
        return dict(order)


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
        # [FUTURES-READY] Default "spot" mempertahankan perilaku lama
        # PERSIS untuk semua caller yang tidak eksplisit mengirim
        # parameter ini (yaitu spot/main_spot.py) -- future/main_future.py
        # yang reuse class ini akan kirim "future" secara eksplisit.
        default_type:    str              = "spot",
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
                "defaultType": default_type,
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
        # [BUG-FIX] Task WS ticker (_watch_tickers_all atau per-symbol
        # _watch_ticker) SEBELUMNYA mati PERMANEN setelah max_retries koneksi
        # gagal berturut-turut, TANPA mekanisme restart apa pun. REST fallback
        # (_poll_tickers, jalan tiap 10s) memang tetap menjaga data ticker
        # tidak benar2 kosong, tapi efeknya: SATU burst gangguan WS sementara
        # (mis. exchange maintenance singkat) men-downgrade SEMUA simbol
        # (untuk _watch_tickers_all — jalur utama di Binance) ke REST-only
        # polling SELAMANYA sampai bot di-restart manual, walau WS exchange
        # sudah pulih normal beberapa menit kemudian. Ini juga menaikkan beban
        # REST API secara signifikan (semua simbol lewat REST tiap 10s,
        # bukan cuma yang benar2 degraded). Sekarang: _poll_tickers (loop
        # yang sudah jalan terus tiap 10s) ikut memantau kesehatan task WS
        # dan me-restart otomatis kalau task itu sudah 'done' (mati).
        self._ws_ticker_task:    Optional[asyncio.Task] = None
        self._ws_ticker_is_multiplexed: bool = False
        self._ws_restart_count:  int = 0
        self._ws_last_restart_ts: float = 0.0
        # [FIX] Simbol yang terbukti ccxt.BadSymbol (exchange tidak mengenalnya
        # sama sekali) -- dikeluarkan dari batch watch_tickers() berikutnya
        # supaya satu simbol jelek tidak menghentikan/loop-kan seluruh batch.
        # Tetap ada di self.symbols (REST fallback & get_feed_status() masih
        # perlu tahu simbol ini ada, statusnya cuma "dead" untuk WS).
        self._ws_excluded_symbols: set = set()

        # [BUG-FIX -- self-healing _watch_orderbook(), pola sama dgn
        # _ws_ticker_task/_ws_restart_count di atas] _watch_orderbook(symbol)
        # SEBELUMNYA mati PERMANEN per-symbol setelah max_retries, TANPA
        # mekanisme restart -- beda dari _watch_tickers_all yang sudah
        # dibereskan (lihat _poll_tickers()). Severity lebih rendah drpd bug
        # ticker asli krn _poll_orderbooks_rest() SUDAH jalan unconditional
        # utk SEMUA symbol (bukan cuma failover), jadi live_orderbooks tetap
        # ter-update via REST walau WS per-symbol mati -- tapi tetap
        # inkonsisten desain dibanding ticker, dan WS orderbook (kalau
        # sehat) lebih rendah latency drpd REST polling murni. Beda dari
        # ticker (SATU task multiplexed, jadi cukup 1 counter/timestamp),
        # orderbook watch PER-SYMBOL -- perlu cooldown independen per symbol
        # (counter/timestamp GLOBAL tunggal akan salah: satu symbol yang
        # baru saja direstart bisa memblokir symbol LAIN yang juga mati
        # ikut direstart, krn cooldown dicek bersama).
        # `_ws_orderbook_tasks` HANYA berisi entry utk symbol yang PERNAH
        # dapat WS orderbook (dipicu add_symbols(), bukan semua self.symbols
        # -- WS orderbook memang "on-demand saat koin masuk pipeline",
        # lihat catatan di start()/add_symbols()).
        self._ws_orderbook_tasks:  Dict[str, asyncio.Task] = {}
        self._ob_restart_count:    Dict[str, int]   = {}
        self._ob_last_restart_ts:  Dict[str, float] = {}

        rest_cls = getattr(ccxt, exchange_id)
        self._rest_exchange: ccxt.Exchange = rest_cls({
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": default_type,
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
                self._ws_ticker_task = asyncio.create_task(self._watch_tickers_all(), name="ws_tickers_all")
                self._ws_ticker_is_multiplexed = True
                self._tasks.append(self._ws_ticker_task)
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
            # [BUG-FIX] Self-healing: cek apakah task WS ticker utama sudah
            # mati (done) — kalau ya, restart dengan cooldown supaya tidak
            # restart-loop rapat kalau exchange memang lagi down total.
            if (
                self._ws_ticker_is_multiplexed
                and self._ws_ticker_task is not None
                and self._ws_ticker_task.done()
                and self._running
            ):
                now = time.time()
                cooldown = min(30 * (2 ** self._ws_restart_count), 600)  # max 10 menit
                if now - self._ws_last_restart_ts >= cooldown:
                    self._ws_restart_count += 1
                    self._ws_last_restart_ts = now
                    log.warning(
                        "WS ticker task mati (done) — restart otomatis "
                        "#%d (cooldown %ds berikutnya kalau gagal lagi).",
                        self._ws_restart_count, cooldown,
                    )
                    try:
                        new_task = asyncio.create_task(
                            self._watch_tickers_all(), name="ws_tickers_all"
                        )
                        self._ws_ticker_task = new_task
                        self._tasks.append(new_task)
                    except Exception as re_err:
                        log.error("Gagal restart WS ticker task: %s", re_err)
                else:
                    log.debug(
                        "WS ticker task mati, masih cooldown (%ds lagi)",
                        cooldown - (now - self._ws_last_restart_ts),
                    )

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
            # [BUG-FIX — resource leak] Sebelumnya: _watch_ticker(symbol)
            # individual SELALU di-spawn di sini asal ws_supported=True,
            # TANPA mengecek apakah feed sedang jalan mode MULTIPLEXED
            # (_watch_tickers_all, dipakai default untuk Binance dkk yang
            # mendukung watch_tickers banyak simbol sekaligus). Simbol baru
            # yang di-append ke self.symbols (baris di atas) OTOMATIS
            # ke-cover oleh _watch_tickers_all pada iterasi berikutnya
            # (fungsi itu selalu membaca ulang self.symbols yang sama,
            # bukan copy) -- jadi spawn _watch_ticker individual di sini
            # menghasilkan KONEKSI WS DUPLIKAT untuk simbol yang sama.
            # Dibuktikan lewat eksperimen. Ini menumpuk setiap kali
            # coin_swap/update universe_watchlist terjadi saat runtime
            # (add_symbols dipanggil aktif dari main.py, bukan dead code),
            # karena task lama tidak pernah dibersihkan sampai bot restart.
            # Sekarang: individual _watch_ticker HANYA di-spawn kalau feed
            # TIDAK sedang multiplexed (exchange yang tidak dukung
            # watch_tickers massal). Orderbook TETAP selalu on-demand per
            # simbol (memang didesain begitu, tidak ada mode multiplexed
            # untuk orderbook di codebase ini).
            if ws_supported and not self._ws_ticker_is_multiplexed:
                self._tasks.append(
                    asyncio.create_task(
                        self._watch_ticker(symbol),
                        name=f"ws_ticker_{symbol}",
                    )
                )
            if ws_supported:
                # [BUG-FIX -- self-healing] Simpan referensi task per-symbol
                # di _ws_orderbook_tasks -- dipakai _poll_orderbooks_rest()
                # utk deteksi task yang sudah mati (.done()) dan restart
                # otomatis dgn cooldown, sama seperti _ws_ticker_task.
                ob_task = asyncio.create_task(
                    self._watch_orderbook(symbol),
                    name=f"ws_ob_{symbol}",
                )
                self._tasks.append(ob_task)
                self._ws_orderbook_tasks[symbol] = ob_task
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
                    active = [s for s in self.symbols if s not in self._ws_excluded_symbols]
                    if not active:
                        log.error(
                            "watch_tickers_all: semua %d simbol ter-exclude "
                            "(BadSymbol) — task berhenti.", len(self.symbols),
                        )
                        return
                    tickers = await self._ex.watch_tickers(active)
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
                    self._ws_restart_count = 0
            except asyncio.CancelledError:
                break
            except ccxt.BadSymbol as e:
                # [FIX] Satu simbol tidak dikenal exchange TIDAK BOLEH menghentikan/
                # loop-kan seluruh batch watch_tickers(). Keluarkan simbol itu saja,
                # lanjut SEGERA dengan sisa simbol valid -- bukan reconnect/backoff.
                bad = _extract_bad_symbol_from_error(e)
                if bad and bad in self.symbols:
                    self._ws_excluded_symbols.add(bad)
                    self._ticker_dead[bad] = True
                    self._feed_mode[bad]   = "WS_UNSUPPORTED"
                    log.warning(
                        "watch_tickers_all: %s tidak dikenal exchange — dikeluarkan "
                        "dari batch WS (skip, bukan reconnect). Sisa %d/%d simbol aktif.",
                        bad, len(self.symbols) - len(self._ws_excluded_symbols), len(self.symbols),
                    )
                else:
                    # Tidak bisa identifikasi simbolnya dari pesan error --
                    # treat seperti error biasa supaya tidak tight-loop tanpa backoff.
                    retries += 1
                    wait = self.reconnect_delay * retries
                    log.warning(
                        "watch_tickers_all: BadSymbol tak teridentifikasi: %s — wait %ds",
                        e, wait,
                    )
                    await asyncio.sleep(wait)
                continue
            except Exception as e:
                # [CIRCUIT-BREAKER -- WS-exclude gagal via ccxt.BadSymbol, insiden
                # EVAA/USDT 13 Juli 2026] Cek pola pesan TERLEPAS dari tipe exception
                # aktualnya -- regex sama persis dgn cabang ccxt.BadSymbol di atas.
                # Exclude HANYA simbol yang match pola ini; exception lain (network
                # timeout, rate-limit, auth, connection reset, dll -- format pesannya
                # beda total dari pola ini, dikonfirmasi lewat grep source ccxt) tetap
                # jatuh ke retry/backoff normal di bawah, TIDAK ikut ter-exclude.
                bad = _extract_bad_symbol_from_error(e)
                if bad and bad in self.symbols:
                    self._ws_excluded_symbols.add(bad)
                    self._ticker_dead[bad] = True
                    self._feed_mode[bad]   = "WS_UNSUPPORTED"
                    log.warning(
                        "watch_tickers_all: %s dikeluarkan dari batch WS via "
                        "circuit-breaker pola pesan (exception_type=%s, BUKAN "
                        "ccxt.BadSymbol) — sisa %d/%d simbol aktif.",
                        bad, type(e).__name__,
                        len(self.symbols) - len(self._ws_excluded_symbols), len(self.symbols),
                        exc_info=True,
                    )
                    continue

                # Bukan pola bad-symbol -- retry/backoff normal, tidak berubah dari
                # sebelumnya, cuma ditambah type(e).__name__ utk instrumentasi
                # diagnostik ringan (TANPA exc_info -- lihat catatan di atas, jalur
                # ini sering terjadi utk network blip biasa yang sudah terbukti
                # benign, exc_info penuh di sini akan membengkakkan log signifikan).
                retries += 1
                wait = self.reconnect_delay * retries
                log.warning(
                    "watch_tickers_all retry %d/%d: [%s] %s — wait %ds",
                    retries, self.max_retries, type(e).__name__, e, wait,
                )
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

    def _maybe_restart_orderbook_task(self, symbol: str) -> None:
        """
        [BUG-FIX -- self-healing _watch_orderbook(), mirror _poll_tickers()]
        Dipanggil dari _poll_orderbooks_rest() utk SATU symbol per iterasi
        (bukan pass terpisah) -- restart tersebar alami mengikuti jeda 50ms
        antar-symbol yang sudah ada di loop REST, bukan burst semua sekaligus
        di satu titik.

        Cooldown eksponensial PERSIS formula _ws_ticker_task (min(30*2^n,
        600)) -- konsistensi disengaja, bukan hasil optimasi terpisah.
        Beda dari ticker: counter/timestamp di sini PER-SYMBOL (dict), krn
        orderbook watch memang banyak task independen, bukan satu task
        multiplexed.

        [KNOWN LIMITATION -- belum divalidasi, TIDAK diperbaiki di sini,
        di luar cakupan fix ini] watch_order_book() (dipanggil di dalam
        _watch_orderbook()) TIDAK dibungkus self._throttler, beda dari
        REST calls di engine/exchange_base.py. Kalau BANYAK symbol mati
        bersamaan (mis. gangguan WS luas yang mempengaruhi semua koin
        aktif-pipeline sekaligus), restart per-symbol yang independen ini
        BISA menghasilkan burst percobaan re-subscribe WS ke exchange
        hampir bersamaan, tanpa rate-limit lokal yang menahannya. Belum
        ada bukti ini bermasalah nyata (WS subscribe bukan endpoint REST
        yang kena rate limit ketat), tapi perlu diperhatikan ulang kalau
        jumlah symbol aktif-pipeline membesar jauh dari kondisi saat ini.
        """
        task = self._ws_orderbook_tasks.get(symbol)
        if task is None or not task.done():
            return

        now      = time.time()
        count    = self._ob_restart_count.get(symbol, 0)
        cooldown = min(30 * (2 ** count), 600)  # max 10 menit, sama dgn ticker
        last_restart = self._ob_last_restart_ts.get(symbol, 0.0)
        if now - last_restart < cooldown:
            return

        self._ob_restart_count[symbol]   = count + 1
        self._ob_last_restart_ts[symbol] = now
        log.warning(
            "WS orderbook task [%s] mati (done) — restart otomatis "
            "#%d (cooldown %ds berikutnya kalau gagal lagi).",
            symbol, count + 1, cooldown,
        )
        try:
            new_task = asyncio.create_task(
                self._watch_orderbook(symbol), name=f"ws_ob_{symbol}",
            )
            # [PENTING] Task LAMA sudah .done() (dicek di atas) sebelum
            # referensinya diganti -- tidak ada dua koneksi WS aktif
            # bersamaan utk symbol yang sama (beda dari bug resource-leak
            # lama di add_symbols(), yang terjadi krn spawn task BARU tanpa
            # mengecek task LAMA masih hidup atau tidak).
            self._ws_orderbook_tasks[symbol] = new_task
            self._tasks.append(new_task)
        except Exception as re_err:
            log.error("Gagal restart WS orderbook task [%s]: %s", symbol, re_err)

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
                    self._maybe_restart_orderbook_task(symbol)
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
        # [G3-SPREAD FIX -- log produksi: 100% keputusan Commander berbunyi
        # G3_SPREAD_UNKNOWN, gate likuiditas buta total] Ticker Binance utk
        # sebagian simbol/jalur datang tanpa bid/ask (None) -- fallback ke
        # live_orderbooks (sumber yang SAMA dgn Gate2, terbukti hidup di
        # spot & futures, diisi WS + REST-poll). Guard kesegaran 30s
        # mencegah spread basi dipakai menilai likuiditas saat ini.
        import time as _t
        ob = self.live_orderbooks.get(symbol, {})
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        ts = ob.get("_ts", 0)
        if bids and asks and (_t.time() - ts) <= 30:
            try:
                bb, ba = float(bids[0][0]), float(asks[0][0])
                if bb > 0 and ba > 0 and ba >= bb:
                    return (ba - bb) / ba * 100
            except (TypeError, ValueError, IndexError):
                pass
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


def save_universe_json(coins: list, min_volume_usdt: float = 100_000) -> None:
    """Simpan hasil scan ke universe.json."""
    # [BUG-FIX] Sebelumnya "min_volume_usd" di-hardcode 10_000_000, padahal
    # caller sebenarnya (auto_scan_and_populate) memanggil
    # scan_binance_universe(100_000, ...) -- SELISIH 100x dari nilai yang
    # BENAR-BENAR dipakai untuk filter. Metadata ini murni informasional
    # (tidak dibaca ulang oleh load_universe_json ataupun kode lain), tapi
    # tetap MENYESATKAN operator yang membuka universe.json langsung untuk
    # audit/debug. Sekarang: parameter aktual diteruskan dari caller.
    data = {
        "scanned_at":     _datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_coins":    len(coins),
        "min_volume_usd": min_volume_usdt,
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


async def auto_scan_and_populate(
    db,
    is_valid_symbol: Optional[Callable[[str], bool]] = None,
) -> list:
    """
    Fungsi utama dipanggil saat bot startup.
    Cek DB apakah perlu scan ulang, lakukan scan, populate universe_overrides.
    Return: list symbol aktif (dari DB universe_overrides atau universe.json)

    [BUG-FIX #35 -- ditemukan saat membangun endpoint futures, diperbaiki
    di jalur yang sama (auto_scan_and_populate_futures(), dipicu insiden
    EVAA/USDT) tapi jalur spot ini SEBELUMNYA tidak pernah dapat perbaikan
    yang sama, dikonfirmasi baca kode langsung -- bukan diasumsikan
    otomatis sama.] is_valid_symbol: callback opsional (main_spot.py kirim
    self.exchange.is_symbol_supported) utk validasi tiap simbol hasil scan
    terhadap ccxt SEBELUM ditulis ke universe.json/universe_overrides.
    scan_binance_universe() sendiri hit REST Binance mentah, independen
    dari ccxt -- bisa menghasilkan simbol yang secara teknis
    TRADING/listing di Binance tapi entah kenapa tidak dikenali objek ccxt
    yang benar-benar dipakai bot (kelas masalah sama dgn insiden EVAA/USDT
    di futures, walau spot genuinely belum pernah punya insiden serupa
    tercatat -- risiko strukturalnya identik, tetap layak ditutup).
    Kalau None (default), tidak ada validasi -- perilaku lama, tidak
    breaking utk caller lain.

    [#22 -- audit fungsional, diverifikasi lewat kode] Flag 'auto_scan_
    universe' ini SENGAJA manual-only by design, BUKAN bug. Satu-satunya
    write ke flag ini ada di baris ~1178 di bawah, dan SELALU menulis
    "false" (reset setelah scan) -- tidak ada mekanisme manapun di repo
    (cron/scheduler/reconciliation loop/stale-universe detector) yang
    pernah menulis "true" secara otomatis, dan tidak ada endpoint API
    untuk men-set flag ini. Operator yang ingin memicu re-scan universe
    harus set flag ini ke "true" langsung lewat SQL:
    UPDATE bot_state SET value='true' WHERE key='auto_scan_universe';
    (padanan futures: 'auto_scan_universe_futures', lifecycle identik).
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
        _scan_min_volume = 100_000
        coins = await loop.run_in_executor(
            None, scan_binance_universe, _scan_min_volume, 500,
        )

        if coins and is_valid_symbol is not None:
            before  = len(coins)
            invalid = [c["symbol"] for c in coins if not is_valid_symbol(c["symbol"])]
            coins   = [c for c in coins if is_valid_symbol(c["symbol"])]
            if invalid:
                log.warning(
                    "auto_scan: %d/%d simbol hasil scan tidak dikenali ccxt, "
                    "dibuang sebelum ditulis ke universe.json: %s",
                    len(invalid), before, invalid,
                )

        if coins:
            # Simpan ke universe.json
            save_universe_json(coins, min_volume_usdt=_scan_min_volume)

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
