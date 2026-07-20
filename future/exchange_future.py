"""
future/exchange_future.py — ExchangeConnector khusus Binance USDT-M Futures

Extend engine.exchange_base.BaseExchangeConnector, menambahkan:
- default_type="future" (ccxt otomatis arahkan ke fapi.binance.com)
- fetch_funding_rate(), fetch_mark_price(): data publik futures, read-only
- set_leverage(): kontrol leverage (paper trading -- cuma disimulasikan)
- fetch_balance(): margin balance (BEDA dari spot -- bukan saldo per-currency,
  tapi margin balance + unrealized PnL semua posisi terbuka)
- _simulate_order_fill(): leverage-aware, hitung & simpan liquidation_price
  (via future.liquidation, formula APPROXIMATE -- lihat peringatan di sana)

⚠️ CATATAN PENTING: paper trading di sini men-track posisi secara internal
(self._paper_positions) supaya bisa menghitung margin yang terpakai vs
tersedia dengan benar -- ini BEDA dari spot yang cuma track saldo per-currency
tanpa konsep "posisi". Kalau ada BUY lagi utk symbol yang SUDAH ada posisi
SAMA arah, ini dianggap MENAMBAH posisi (average entry price baru dihitung).
Kalau order berlawanan arah dari posisi existing, dianggap MENUTUP (sebagian
atau seluruhnya) posisi itu -- BUKAN membuka posisi baru arah berlawanan
(belum ada dukungan hedge-mode/dual-side di implementasi ini).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable

from engine.exchange_base import BaseExchangeConnector, ReduceOnlyRejected
from future.liquidation import calculate_liquidation_price

log = logging.getLogger("exchange_future")


class FutureExchangeConnector(BaseExchangeConnector):
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
        default_leverage:    int   = 10,
        default_margin_mode: str   = "isolated",
        default_mmr:         float = 0.005,
        # [PERINGATAN] default_mmr APPROXIMATE -- lihat future/liquidation.py
    ):
        super().__init__(
            exchange_id=exchange_id,
            api_key=api_key,
            api_secret=api_secret,
            default_type="future",
            api_passphrase=api_passphrase,
            testnet=testnet,
            requests_per_second=requests_per_second,
            db=db,
        )
        self.paper_trading    = paper_trading
        self._paper_orders: Dict[str, Dict] = {}
        self._paper_quote_ccy = quote_currency
        # Margin balance -- BEDA dari spot: ini "modal futures" yang tersedia
        # utk buka posisi baru (dikurangi margin yang sudah terpakai di
        # posisi terbuka). Tidak ada konsep "punya saldo BTC" seperti spot.
        self._paper_margin_balance: float = float(initial_capital)
        self._default_leverage    = default_leverage
        # [BUG-FIX] Leverage per-symbol, diisi oleh set_leverage(), dipakai
        # _simulate_order_fill() saat membuka posisi -- sebelumnya tidak ada,
        # jadi leverage adaptif dari risk_future.py tidak pernah benar-benar
        # dipakai exchange saat hitung margin/liquidation.
        self._symbol_leverage: Dict[str, int] = {}
        self._default_margin_mode = default_margin_mode
        self._default_mmr         = default_mmr

        # Track posisi terbuka secara internal (exchange-level simulation).
        # Struktur per symbol: {side, amount, entry_price, leverage,
        # margin_mode, margin_locked, liquidation_price}
        self._paper_positions: Dict[str, Dict] = {}

        if self.paper_trading:
            log.warning(
                "📝 PAPER TRADING MODE AKTIF (FUTURES) — data pasar 100%% ASLI "
                "dari Binance Futures (fapi), TAPI order TIDAK PERNAH benar-benar "
                "dikirim ke exchange. Leverage default=%dx margin_mode=%s. "
                "⚠️ Liquidation price yang dihitung APPROXIMATE -- lihat "
                "peringatan lengkap di future/liquidation.py.",
                default_leverage, default_margin_mode,
            )
            log.warning(
                "📝 [PAPER MARGIN] Modal virtual: %.2f %s — TERISOLASI dari "
                "saldo real akun exchange.",
                self._paper_margin_balance, quote_currency,
            )

    # ── Futures-specific read-only data (publik, tidak butuh API key trading) ──

    async def fetch_funding_rate(self, symbol: str) -> Dict:
        """Funding rate saat ini + waktu funding berikutnya. Data publik read-only."""
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_funding_rate, symbol, _ep="funding_rate"
            )
        await self._log_lat("fetch_funding_rate", t0)
        return result or {}

    async def fetch_mark_price(self, symbol: str) -> float:
        """
        Mark price -- BEDA dari last traded price. Liquidation di exchange
        asli dipicu berdasar MARK PRICE (rata-rata beberapa exchange),
        bukan harga transaksi terakhir. Kalau simulasi kita pakai last price
        biasa, hasil bisa meleset dari kondisi liquidation asli.
        """
        t0 = time.monotonic()
        async with self._throttler:
            ticker = await self._retry(
                self._ex.fetch_ticker, symbol, _ep="mark_price"
            )
        await self._log_lat("fetch_mark_price", t0)
        # ccxt biasanya expose mark price via ticker['info']['markPrice'] utk
        # Binance futures, atau via fetch_mark_price/fetch_mark_prices native
        # kalau exchange (versi ccxt) mendukungnya langsung.
        if hasattr(self._ex, "fetch_mark_price"):
            try:
                mp_result = await self._retry(
                    self._ex.fetch_mark_price, symbol, _ep="mark_price_native"
                )
                if mp_result and mp_result.get("markPrice"):
                    return float(mp_result["markPrice"])
            except Exception:
                pass
        info = (ticker or {}).get("info", {})
        mark = info.get("markPrice")
        if mark:
            return float(mark)
        # Fallback: last price kalau mark price tidak tersedia dari response
        # (JANGAN diam-diam anggap ini sama akuratnya -- log warning eksplisit)
        log.warning(
            "fetch_mark_price(%s): markPrice tidak ditemukan di response, "
            "fallback ke last price -- TIDAK seakurat mark price asli utk "
            "estimasi liquidation risk.", symbol,
        )
        return float((ticker or {}).get("last") or 0.0)

    async def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        Set leverage untuk symbol. Paper trading: disimpan di
        self._symbol_leverage (per-symbol), dipakai oleh
        _simulate_order_fill() saat membuka posisi baru untuk symbol ini.

        [BUG-FIX] Sebelumnya method ini CUMA log, tidak menyimpan apapun --
        _simulate_order_fill() selalu fallback ke self._default_leverage
        (nilai dari konstruktor), TIDAK PERNAH memakai leverage yang baru
        di-set lewat pemanggilan ini. Akibatnya leverage ADAPTIF (dari
        risk_future.py::compute_adaptive_leverage) tercatat benar di
        RiskAssessment & DB, TAPI margin/liquidation_price yang dihitung
        exchange saat fill tetap pakai leverage DEFAULT yang salah --
        ditemukan lewat log yang menunjukkan dua nilai leverage berbeda
        utk transaksi yang sama.
        """
        self._symbol_leverage[symbol] = leverage
        if self.paper_trading:
            log.info(
                "📝 [PAPER] set_leverage disimulasikan: %s -> %dx (TIDAK dikirim ke exchange)",
                symbol, leverage,
            )
            return {"symbol": symbol, "leverage": leverage, "info": {"paper_trading": True}}

        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.set_leverage, leverage, symbol, _ep="set_leverage"
            )
        await self._log_lat("set_leverage", t0)
        return result or {}

    async def fetch_positions(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        """
        Posisi terbuka. Paper trading: return dari _paper_positions internal
        (BUKAN dari fetch_balance seperti spot's position_sync -- futures
        emang punya endpoint khusus utk ini, position_sync_future.py nanti
        akan pakai method ini, bukan fetch_balance()).
        """
        if self.paper_trading:
            if symbols:
                return [
                    {**pos, "symbol": sym}
                    for sym, pos in self._paper_positions.items()
                    if sym in symbols
                ]
            return [{**pos, "symbol": sym} for sym, pos in self._paper_positions.items()]

        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_positions, symbols, _ep="positions"
            )
        await self._log_lat("fetch_positions", t0)
        return result or []

    # ── Balance & order simulation (futures-specific, override base) ──────────

    def apply_funding_payment(self, symbol: str, payment: float) -> float:
        """
        [FUTURES-SPECIFIC -- BARU] Terapkan funding payment ke margin balance
        virtual. payment NEGATIF = posisi membayar (margin_balance berkurang),
        POSITIF = posisi menerima (margin_balance bertambah). Ini menyesuaikan
        _paper_margin_balance langsung (BUKAN margin_locked posisi -- funding
        adalah cash flow ke/dari wallet balance, bukan collateral posisi).

        Dipanggil oleh main_future.py::run_funding_settlement_loop() secara
        periodik. Tidak melakukan apapun ke exchange asli (paper trading only,
        exchange asli menerapkan funding otomatis sendiri di sisi mereka).

        Return: margin_balance TERBARU setelah payment diterapkan (utk logging).
        """
        self._paper_margin_balance += payment
        return self._paper_margin_balance

    async def fetch_balance(self) -> Dict:
        """
        [PAPER TRADING FUTURES] BEDA dari spot: ini margin balance, bukan
        saldo per-currency. "free" = margin tersedia utk posisi baru,
        "used" = margin terkunci di posisi terbuka, "total" = free+used
        (BELUM termasuk unrealized PnL -- itu field terpisah, lihat
        get_unrealized_pnl_total()).
        """
        if self.paper_trading:
            used_margin = sum(
                pos.get("margin_locked", 0.0) for pos in self._paper_positions.values()
            )
            free = self._paper_margin_balance
            total = free + used_margin
            return {
                "free":  {self._paper_quote_ccy: free},
                "used":  {self._paper_quote_ccy: used_margin},
                "total": {self._paper_quote_ccy: total},
            }

        if not (getattr(self._ex, "apiKey", None) and getattr(self._ex, "secret", None)):
            return {}
        t0 = time.monotonic()
        async with self._throttler:
            result = await self._retry(
                self._ex.fetch_balance, _ep="balance"
            )
        await self._log_lat("fetch_balance", t0)
        return result or {}

    def get_unrealized_pnl_total(self, current_prices: Dict[str, float]) -> float:
        """
        Hitung total unrealized PnL semua posisi terbuka berdasar harga
        current_prices yang diberikan (dict symbol->price, biasanya dari
        fetch_ticker/fetch_mark_price masing-masing symbol -- caller
        bertanggung jawab menyediakan harga terkini, fungsi ini murni
        kalkulasi dari data yang sudah ada).
        """
        total_pnl = 0.0
        for sym, pos in self._paper_positions.items():
            price = current_prices.get(sym)
            if price is None:
                continue
            entry = pos["entry_price"]
            amount = pos["amount"]
            if pos["side"] == "long":
                total_pnl += (price - entry) * amount
            else:
                total_pnl += (entry - price) * amount
        return total_pnl

    async def _simulate_order_fill(
        self,
        symbol:     str,
        order_type: str,
        side:       str,
        amount:     float,
        price:      Optional[float],
        reduce_only: bool = False,
    ) -> Dict:
        """
        [PAPER TRADING FUTURES] Simulasikan fill order dengan leverage-aware
        margin calculation. BEDA MENDASAR dari spot: order "buy" bisa berarti
        BUKA LONG (kalau belum ada posisi/posisi sudah long) ATAU TUTUP SHORT
        (kalau ada posisi short existing) -- begitu juga "sell" bisa BUKA
        SHORT atau TUTUP LONG. Determinasi ini dilakukan di sini berdasar
        posisi existing utk symbol tsb.

        ⚠️ BELUM MENDUKUNG: hedge-mode (long & short bersamaan utk symbol
        sama), partial close dengan margin_mode campuran, cross margin
        (raise dari future.liquidation kalau margin_mode='cross' diminta).

        [ITEM #15 -- Temuan C, Opsi C1] `reduce_only=True` (diset caller,
        lihat _do_close_position() di main_future.py) -- backstop TOCTOU
        thd Opsi C2 (verify-before-send): kalau order ini TERNYATA akan
        MEMBUKA posisi baru atau MENAMBAH posisi existing (bukan genuinely
        MENGURANGI/MENUTUP), tolak dgn ReduceOnlyRejected, JANGAN pernah
        eksekusi. Ini menutup celah race sempit antara verify-before-send
        (C2) dicek dan order ini benar-benar diproses.
        """
        ticker = await self.fetch_ticker(symbol)
        bid = float(ticker.get("bid") or ticker.get("last") or price or 0.0)
        ask = float(ticker.get("ask") or ticker.get("last") or price or 0.0)

        if order_type == "market" or price is None:
            fill_price = ask if side == "buy" else bid
        else:
            fill_price = min(ask, price) if side == "buy" else max(bid, price)
        fill_price = self.price_to_precision(symbol, fill_price) or fill_price

        existing = self._paper_positions.get(symbol)

        if reduce_only:
            # Order ini genuinely MENGURANGI/MENUTUP hanya kalau ADA posisi
            # existing DAN arahnya BERLAWANAN dari `side` (persis kondisi
            # cabang "TUTUP" di bawah) -- selain itu (tidak ada posisi SAMA
            # SEKALI, atau ada tapi searah/nambah) HARUS ditolak.
            would_reduce = (
                existing is not None
                and existing["side"] == ("short" if side == "buy" else "long")
            )
            if not would_reduce:
                raise ReduceOnlyRejected(
                    f"[PAPER FUTURES] reduce-only order DITOLAK utk {symbol} "
                    f"({side}, amount={amount:.8f}): tidak ada posisi cocok "
                    f"utk direduce (existing={existing}) -- kemungkinan sudah "
                    f"closed duluan (item #15, race TOCTOU sisa Opsi C2)."
                )

        order_id = f"PAPER-FUT-{uuid.uuid4().hex[:16]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        fee_rate = self.get_taker_fee(symbol)
        notional = amount * fill_price
        fee_amt  = notional * fee_rate

        realized_pnl = 0.0
        liquidation_price = None
        action = None  # "open" | "close" | "add" | "reduce"

        if existing is None:
            # Buka posisi baru: buy=long, sell=short
            new_side = "long" if side == "buy" else "short"
            action = "open"
            leverage = self._symbol_leverage.get(symbol, self._default_leverage)
            margin_mode = self._default_margin_mode
            required_margin = notional / leverage

            if required_margin + fee_amt > self._paper_margin_balance:
                raise ValueError(
                    f"[PAPER FUTURES] Margin tidak cukup: butuh {required_margin + fee_amt:.4f} "
                    f"{self._paper_quote_ccy}, tersedia {self._paper_margin_balance:.4f}"
                )

            liq_result = calculate_liquidation_price(
                entry_price=fill_price, leverage=leverage, side=new_side,
                mmr=self._default_mmr, margin_mode=margin_mode,
            )
            liquidation_price = liq_result.liquidation_price

            self._paper_margin_balance -= (required_margin + fee_amt)
            self._paper_positions[symbol] = {
                "side": new_side,
                "amount": amount,
                "entry_price": fill_price,
                "leverage": leverage,
                "margin_mode": margin_mode,
                "margin_locked": required_margin,
                "liquidation_price": liquidation_price,
            }
            log.info(
                "📝 [PAPER FUTURES] BUKA %s %s | amount=%.8f @ %.8f leverage=%dx "
                "margin=%.4f liq_price=%.8f (APPROXIMATE)",
                new_side.upper(), symbol, amount, fill_price, leverage,
                required_margin, liquidation_price,
            )

        elif existing["side"] == ("long" if side == "buy" else "short"):
            # Order searah posisi existing -- MENAMBAH posisi (average entry)
            action = "add"
            old_amount = existing["amount"]
            old_entry  = existing["entry_price"]
            new_amount = old_amount + amount
            new_entry  = (old_amount * old_entry + amount * fill_price) / new_amount
            leverage = existing["leverage"]
            additional_margin = notional / leverage

            if additional_margin + fee_amt > self._paper_margin_balance:
                raise ValueError(
                    f"[PAPER FUTURES] Margin tidak cukup utk menambah posisi: "
                    f"butuh {additional_margin + fee_amt:.4f}, tersedia {self._paper_margin_balance:.4f}"
                )

            liq_result = calculate_liquidation_price(
                entry_price=new_entry, leverage=leverage, side=existing["side"],
                mmr=self._default_mmr, margin_mode=existing["margin_mode"],
            )
            liquidation_price = liq_result.liquidation_price

            self._paper_margin_balance -= (additional_margin + fee_amt)
            existing["amount"] = new_amount
            existing["entry_price"] = new_entry
            existing["margin_locked"] += additional_margin
            existing["liquidation_price"] = liquidation_price
            log.info(
                "📝 [PAPER FUTURES] TAMBAH %s %s | +%.8f @ %.8f -> total=%.8f avg_entry=%.8f",
                existing["side"].upper(), symbol, amount, fill_price, new_amount, new_entry,
            )

        else:
            # Order berlawanan arah posisi existing -- TUTUP (sebagian/seluruh)
            close_amount = min(amount, existing["amount"])
            entry = existing["entry_price"]
            if existing["side"] == "long":
                realized_pnl = (fill_price - entry) * close_amount
            else:
                realized_pnl = (entry - fill_price) * close_amount

            margin_released = existing["margin_locked"] * (close_amount / existing["amount"])
            self._paper_margin_balance += margin_released + realized_pnl - fee_amt

            if close_amount >= existing["amount"]:
                action = "close"
                del self._paper_positions[symbol]
                log.info(
                    "📝 [PAPER FUTURES] TUTUP PENUH %s %s | %.8f @ %.8f | "
                    "realized_pnl=%+.4f %s",
                    existing["side"].upper(), symbol, close_amount, fill_price,
                    realized_pnl, self._paper_quote_ccy,
                )
            else:
                action = "reduce"
                existing["amount"] -= close_amount
                existing["margin_locked"] -= margin_released
                log.info(
                    "📝 [PAPER FUTURES] TUTUP SEBAGIAN %s %s | %.8f @ %.8f | "
                    "sisa=%.8f realized_pnl=%+.4f %s",
                    existing["side"].upper(), symbol, close_amount, fill_price,
                    existing["amount"], realized_pnl, self._paper_quote_ccy,
                )

        order = {
            "id":        order_id,
            "symbol":    symbol,
            "type":      order_type,
            "side":      side,
            "status":    "closed",
            "amount":    amount,
            "filled":    amount,
            "remaining": 0.0,
            "average":   fill_price,
            "price":     fill_price,
            "cost":      notional,
            "fee":       {"cost": fee_amt, "currency": self._paper_quote_ccy},
            "timestamp": int(time.time() * 1000),
            "datetime":  now_iso,
            "info": {
                "paper_trading":     True,
                "note":              "Simulated futures fill, no real order sent.",
                "action":            action,
                "realized_pnl":      realized_pnl,
                "liquidation_price": liquidation_price,
            },
        }
        self._paper_orders[order_id] = order

        log.warning(
            "📝 [PAPER ORDER FUTURES] %s %s %s | amount=%.8f @ %.8f | "
            "action=%s | id=%s — TIDAK dikirim ke exchange asli.",
            symbol, side.upper(), order_type, amount, fill_price, action, order_id,
        )
        return dict(order)


# ═══════════════════════════════════════════════════════════════
#  Auto-scan universe KHUSUS FUTURES dari Binance — tanpa API key (public)
#  Hasil disimpan ke universe_futures.json + universe_overrides DB
#
#  [PENTING] Ini BUKAN sekadar duplikasi dari spot/exchange_spot.py.
#  Binance Spot dan Futures (USDT-M) adalah DUA MARKET BERBEDA dengan
#  daftar simbol yang TIDAK SAMA -- banyak koin yang ada di spot TIDAK
#  punya kontrak futures. Fungsi ini hit endpoint PUBLIK FUTURES
#  (fapi.binance.com), BUKAN endpoint spot (api.binance.com) yang dipakai
#  spot/exchange_spot.py::scan_binance_universe().
#
#  Ditemukan sebagai gap SAAT user tanya "di mana watchlist spot disimpan"
#  -- ternyata main_spot.py punya mekanisme auto-discovery dinamis
#  (scan Binance -> universe.json) yang SAMA SEKALI TIDAK ADA
#  padanannya di future/ sebelum ini.
# ═══════════════════════════════════════════════════════════════
import urllib.request as _urllib_request
import json as _json
import ssl as _ssl
from datetime import datetime as _datetime

_STABLES_FUT  = {
    "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP", "USDT",
}
_LEVERAGE_FUT = ["UP", "DOWN", "BULL", "BEAR"]
_UNIVERSE_FILE_FUTURES = "universe_futures.json"


def _fetch_binance_futures_tickers() -> list:
    """Hit Binance Futures public API (fapi), return raw list ticker 24hr."""
    urls = [
        "https://fapi.binance.com/fapi/v1/ticker/24hr",
    ]
    import certifi as _certifi
    ctx = _ssl.create_default_context(cafile=_certifi.where())
    for url in urls:
        try:
            req  = _urllib_request.urlopen(url, timeout=15, context=ctx)
            data = _json.loads(req.read())
            log.info("scan_universe_futures: fetch sukses dari %s (%d tickers)", url, len(data))
            return data
        except Exception as e:
            log.warning("scan_universe_futures: gagal %s — %s", url, e)
    return []


def _fetch_binance_futures_trading_symbols() -> set:
    """Fetch exchangeInfo FUTURES, return set symbol dgn status TRADING &
    contractType PERPETUAL saja (skip kontrak quarterly/delivery)."""
    urls = [
        "https://fapi.binance.com/fapi/v1/exchangeInfo",
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
                if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            }
            log.info("scan_universe_futures: %d symbol TRADING+PERPETUAL dari exchangeInfo", len(trading))
            return trading
        except Exception as e:
            log.warning("scan_universe_futures: exchangeInfo gagal %s — %s", url, e)
    return set()


def scan_binance_futures_universe(
    min_volume_usdt: float = 100_000,
    max_coins:       int   = 200,
    quote:           str   = "USDT",
) -> list:
    """
    Scan koin paling likuid di Binance USDT-M FUTURES (BUKAN spot).
    Return list of dict: [{"symbol": "BTC/USDT", "volume_24h": 1688600000}, ...]

    ⚠️ CATATAN: field response Binance Futures API BISA berbeda nama dari
    spot (mis. beberapa field pakai penamaan berbeda) -- struktur di sini
    ditulis mengikuti pola umum publik Binance Futures API, TAPI BELUM
    diverifikasi terhadap response API asli (sandbox ini tidak punya akses
    network ke Binance). WAJIB divalidasi/dites dulu di lingkungan yang
    punya akses internet sebelum dipakai produksi.
    """
    raw = _fetch_binance_futures_tickers()
    if not raw:
        log.error("scan_universe_futures: tidak ada data dari Binance Futures.")
        return []

    _trading_symbols = _fetch_binance_futures_trading_symbols()

    results = []
    for t in raw:
        sym = t.get("symbol", "")
        if not sym.endswith(quote):
            continue
        if _trading_symbols and sym not in _trading_symbols:
            continue
        base = sym[:-len(quote)]
        if base in _STABLES_FUT:
            continue
        if any(base.endswith(lv) or base.startswith(lv) for lv in _LEVERAGE_FUT):
            continue
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
        "scan_universe_futures: %d koin lolos filter (min_vol=$%.0fM, max=%d)",
        len(results), min_volume_usdt / 1_000_000, max_coins,
    )
    return results


def save_universe_json_futures(coins: list, min_volume_usdt: float = 100_000) -> None:
    """Simpan hasil scan ke universe_futures.json -- FILE TERPISAH dari
    universe.json milik spot, supaya tidak saling menimpa."""
    data = {
        "market":         "futures",
        "scanned_at":     _datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_coins":    len(coins),
        "min_volume_usd": min_volume_usdt,
        "symbols":        coins,
    }
    with open(_UNIVERSE_FILE_FUTURES, "w") as f:
        _json.dump(data, f, indent=2)
    log.info("scan_universe_futures: hasil disimpan ke %s (%d koin)", _UNIVERSE_FILE_FUTURES, len(coins))


def load_universe_json_futures() -> list:
    """Baca universe_futures.json, return list symbol string."""
    try:
        with open(_UNIVERSE_FILE_FUTURES) as f:
            data = _json.load(f)
        symbols = [c["symbol"] for c in data.get("symbols", [])]
        log.info("load_universe_futures: %d koin dari %s (scan: %s)",
                 len(symbols), _UNIVERSE_FILE_FUTURES, data.get("scanned_at", "?"))
        return symbols
    except FileNotFoundError:
        log.warning("load_universe_futures: %s tidak ditemukan.", _UNIVERSE_FILE_FUTURES)
        return []
    except Exception as e:
        log.error("load_universe_futures: gagal baca — %s", e)
        return []


async def auto_scan_and_populate_futures(
    db,
    is_valid_symbol: Optional[Callable[[str], bool]] = None,
) -> list:
    """
    Padanan futures dari spot/exchange_spot.py::auto_scan_and_populate().
    Dipanggil saat bot futures startup. Pakai bot_state key TERPISAH
    ('auto_scan_universe_futures') supaya tidak bentrok dengan flag spot
    kalau suatu saat DB pernah di-share (saat ini fisik terpisah, tapi
    defensif tetap penting).

    [#22 -- audit fungsional, diverifikasi lewat kode] Flag ini SENGAJA
    manual-only by design, BUKAN bug. Satu-satunya write ke flag ini ada di
    baris ~667 di bawah, dan SELALU menulis "false" (reset setelah scan) --
    tidak ada mekanisme manapun di repo (cron/scheduler/reconciliation loop/
    stale-universe detector) yang pernah menulis "true" secara otomatis, dan
    tidak ada endpoint API untuk men-set flag ini. Operator yang ingin
    memicu re-scan universe futures harus set flag ini ke "true" langsung
    lewat SQL: UPDATE bot_state SET value='true' WHERE key='auto_scan_universe_futures';
    (padanan spot: 'auto_scan_universe', lifecycle identik).

    is_valid_symbol: [FIX] callback opsional (mis. self.exchange.get_market_info
    dibungkus jadi predicate) utk validasi tiap simbol hasil scan terhadap ccxt
    SEBELUM ditulis ke universe_futures.json/universe_overrides.
    scan_binance_futures_universe() sendiri hit REST Binance mentah, independen
    dari ccxt -- bisa menghasilkan simbol yang secara teknis TRADING+PERPETUAL
    di Binance tapi entah kenapa tidak dikenali objek ccxt yang benar-benar
    dipakai bot (insiden nyata: EVAA/USDT). Kalau None (default), tidak ada
    validasi -- perilaku lama, tidak breaking untuk caller lain.
    """
    flag = await db.get_bot_state("auto_scan_universe_futures")
    should_scan = (flag == "true")

    if should_scan:
        log.info("auto_scan_universe_futures=true — mulai scan Binance Futures...")
        loop = asyncio.get_running_loop()
        _scan_min_volume = 100_000
        coins = await loop.run_in_executor(
            None, scan_binance_futures_universe, _scan_min_volume, 200,
        )

        if coins and is_valid_symbol is not None:
            before   = len(coins)
            invalid  = [c["symbol"] for c in coins if not is_valid_symbol(c["symbol"])]
            coins    = [c for c in coins if is_valid_symbol(c["symbol"])]
            if invalid:
                log.warning(
                    "auto_scan_futures: %d/%d simbol hasil scan tidak dikenali "
                    "ccxt, dibuang sebelum ditulis ke universe_futures.json: %s",
                    len(invalid), before, invalid,
                )

        if coins:
            save_universe_json_futures(coins, min_volume_usdt=_scan_min_volume)

            old_symbols = await db.get_active_universe_overrides()
            for sym in old_symbols:
                await db.deactivate_universe_override(sym)
            log.info("auto_scan_futures: %d koin lama dinonaktifkan", len(old_symbols))

            for coin in coins:
                vol_m = coin["volume_24h"] / 1_000_000
                await db.upsert_universe_override(
                    symbol=coin["symbol"], source="auto_scan_futures",
                    notes=f"vol_24h=${vol_m:.1f}M scanned_at={_datetime.utcnow().strftime('%Y-%m-%d')}",
                )
            log.info("auto_scan_futures: %d koin baru dimasukkan ke universe_overrides", len(coins))

            await db.set_bot_state("auto_scan_universe_futures", "false")
            log.info("auto_scan_futures: flag auto_scan_universe_futures direset ke false")

            return [c["symbol"] for c in coins]
        else:
            log.error("auto_scan_futures: scan gagal, fallback ke universe_futures.json / .env")

    from_json = load_universe_json_futures()
    if from_json:
        return from_json

    try:
        db_symbols = await db.get_active_universe_overrides()
        if db_symbols:
            log.info("auto_scan_futures: %d koin dari universe_overrides DB (fallback)", len(db_symbols))
            return db_symbols
    except Exception:
        pass

    log.warning("auto_scan_futures: tidak ada sumber universe tersedia -- pakai UNIVERSE_WATCHLIST_FUTURES/.env")
    return []
