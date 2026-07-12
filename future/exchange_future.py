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

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List

from engine.exchange_base import BaseExchangeConnector
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
        Set leverage untuk symbol. Paper trading: cuma disimulasikan
        (disimpan di internal state), TIDAK pernah dikirim ke exchange asli.
        """
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
            leverage = self._default_leverage
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
