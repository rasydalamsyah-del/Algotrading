"""
risk.py (spot) — RiskManager khusus spot trading

Extend engine.risk_base.BaseRiskManager yang menangani semua bagian generik
(halt/resume state machine, drawdown/daily-loss tracking, symbol halt,
breakeven/trailing SL, statistik performa). Di sini cuma tersisa yang
benar-benar spot-specific: evaluate_order() (semantik side="sell" SELALU
berarti menutup posisi existing), _compute_position_size(), _compute_sl_tp().
"""

from __future__ import annotations

from typing import Optional, Tuple

from engine.risk_base import BaseRiskManager, HaltReason, RiskDecision, RiskAssessment

log = __import__("logging").getLogger("risk")


class RiskManager(BaseRiskManager):

    async def evaluate_order(
        self,
        symbol:      str,
        side:        str,
        price:       float,
        quantity:    float,
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
        atr:         Optional[float] = None,
        free_coin_balance: Optional[float] = None,
        exchange_min_cost: Optional[float] = None,
        reserve_slot: bool = True,
    ) -> RiskAssessment:
        async with self._evaluate_lock:
            return await self._evaluate_order_locked(
                symbol=symbol, side=side, price=price, quantity=quantity,
                stop_loss=stop_loss, take_profit=take_profit, atr=atr,
                free_coin_balance=free_coin_balance,
                exchange_min_cost=exchange_min_cost,
                reserve_slot=reserve_slot,
            )

    async def _evaluate_order_locked(
        self,
        symbol:      str,
        side:        str,
        price:       float,
        quantity:    float,
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
        atr:         Optional[float] = None,
        free_coin_balance: Optional[float] = None,
        exchange_min_cost: Optional[float] = None,
        reserve_slot: bool = True,
    ) -> RiskAssessment:

        if self._halted:
            return RiskAssessment(
                RiskDecision.REJECTED, f"Trading halted: {self.halt_reason}"
            )

        if self._current_equity <= 0:
            return RiskAssessment(
                RiskDecision.REJECTED, "Portfolio equity not yet initialised."
            )

        if self._current_drawdown_pct >= self._max_drawdown_pct:
            self.halt_trading(
                HaltReason.MAX_DRAWDOWN,
                f"drawdown {self._current_drawdown_pct:.3f}%",
            )
            return RiskAssessment(RiskDecision.REJECTED, "Max drawdown breached.")

        if self._daily_loss_pct >= self._dynamic_daily_limit:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Daily loss {self._daily_loss_pct:.3f}% >= "
                f"limit {self._dynamic_daily_limit:.2f}%",
            )

        if side == "buy" and self.is_symbol_halted(symbol):
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"{symbol} sedang di-halt karena loss >= {self._max_loss_per_symbol}%. "
                "Reset otomatis besok UTC midnight. Koin lain masih bisa trading.",
            )

        if side == "buy" and self._open_positions_count >= self._max_open_positions:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Max open positions reached: "
                f"{self._open_positions_count}/{self._max_open_positions}",
            )

        if price <= 0:
            return RiskAssessment(RiskDecision.REJECTED, f"Invalid price: {price}")

        if side == "sell":
            # [BUG-FIX] Sebelumnya: kode memanggil self._exchange.fetch_balance(),
            # tapi RiskManager TIDAK PERNAH menerima/menyimpan referensi
            # exchange (constructor hanya terima config & db) — ini akan
            # AttributeError kalau cabang ini tereksekusi. Tidak ada caller
            # yang memanggil evaluate_order(side="sell", ...) saat ini
            # (main.py membuat RiskAssessment manual untuk close, lihat
            # _do_close_position), jadi ini dead code yang belum pernah
            # ter-trigger — tapi tetap bug laten kalau ada caller baru.
            # Sekarang: terima saldo riil lewat parameter free_coin_balance
            # (di-fetch oleh caller, bukan RiskManager pegang object
            # exchange). Kalau caller tidak mengisi parameter ini, guard
            # di-skip — tidak ada perubahan behavior untuk caller lama.
            if free_coin_balance is not None and quantity > free_coin_balance:
                safe_amount = round(free_coin_balance * 0.999, 8)
                log.warning(
                    "%s SELL amount adjusted: %.8f → %.8f (free balance: %.8f)",
                    symbol, quantity, safe_amount, free_coin_balance
                )
                quantity = safe_amount
            if free_coin_balance is not None and quantity <= 0:
                return RiskAssessment(
                    RiskDecision.REJECTED,
                    f"Insufficient coin balance to sell {symbol}: free={free_coin_balance:.8f}"
                )

        if side == "buy":
            requested_value = quantity * price
            if requested_value > self._free_balance * 0.99:
                return RiskAssessment(
                    RiskDecision.REJECTED,
                    f"Insufficient free balance: need ${requested_value:.2f}, "
                    f"available ${self._free_balance:.2f}",
                )

        approved_size, size_reason = self._compute_position_size(
            side, price, quantity, atr
        )
        if approved_size is None or approved_size <= 0:
            return RiskAssessment(
                RiskDecision.REJECTED, size_reason or "Position sizing failed."
            )

        order_value = approved_size * price
        # [BUG-FIX -- ditemukan lewat sapuan dead-code] Sebelumnya cuma cek
        # thd self._min_order_value_usdt -- nilai KONFIG GENERIK yang SAMA
        # utk semua symbol (default $10), BUKAN minimum order REAL per-symbol
        # di exchange (exchange.get_min_order_cost(symbol), yang bisa beda-
        # beda tiap pair -- exchange.py sudah punya fungsi ini sejak awal,
        # TAPI tidak pernah dipanggil dari manapun). Kalau minimum riil
        # exchange utk suatu symbol LEBIH TINGGI dari config generik (mis.
        # exchange minta $15 tapi config bilang $10 cukup), order bisa LOLOS
        # validasi risk.py tapi DITOLAK exchange saat eksekusi nyata --
        # kegagalan order diam-diam yang mestinya bisa dicegah lebih awal.
        # Fix: pakai nilai MAKSIMUM dari keduanya (caller boleh oper
        # exchange_min_cost, opsional & backward-compatible -- kalau tidak
        # dioper, perilaku identik dgn sebelumnya).
        effective_min = self._min_order_value_usdt
        if exchange_min_cost is not None and exchange_min_cost > effective_min:
            effective_min = exchange_min_cost
        if order_value < effective_min:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Order value ${order_value:.4f} < minimum ${effective_min:.4f}"
                + (
                    f" (exchange min=${exchange_min_cost:.4f})"
                    if exchange_min_cost is not None and exchange_min_cost > self._min_order_value_usdt
                    else ""
                ),
            )

        if side == "buy" and order_value > self._free_balance * 0.99:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Insufficient free balance: need ${order_value:.2f}, "
                f"available ${self._free_balance:.2f}",
            )

        sl, tp = self._compute_sl_tp(side, price, stop_loss, take_profit, atr)

        if sl is not None and side == "buy" and price > 0:
            sl_pct     = (price - sl) / price * 100
            max_sl_pct = self._max_position_size_pct * 2.5
            # [BUG-FIX — kritis] Sebelumnya HANYA sl_pct > max_sl_pct (SL
            # terlalu LEBAR) yang di-override. Kalau sl hasil hitung ATR
            # jatuh persis di 0 atau NEGATIF (mis. ATR besar relatif harga,
            # dikombinasikan max_position_size_pct yang longgar sehingga
            # max_sl_pct ikut longgar), sl<=0 TIDAK tertangkap kondisi di
            # atas (sl_pct=100% bisa saja <= max_sl_pct kalau
            # max_position_size_pct >= 40%). sl=0/negatif itu HARGA STOP
            # YANG TIDAK MASUK AKAL (order sell di harga 0 tidak mungkin
            # tereksekusi wajar) -- dan diperparah bug falsy-check di bawah
            # (baris assessment) yang mengubah sl=0.0 jadi None, membuat
            # trade approved TANPA stop-loss sama sekali. Dibuktikan lewat
            # eksperimen: price=1.0, atr=0.5, atr_multiplier_sl=2.0,
            # max_position_size_pct=45% -> sl=0.0 lolos tanpa override ->
            # assessment.stop_loss=None. Sekarang: sl<=0 UNTUK BUY juga
            # di-floor ke SL persentase aman, sama seperti kasus "terlalu lebar".
            if sl_pct > max_sl_pct or sl <= 0:
                sl_override = price * (1 - self._stop_loss_pct / 100)
                log.warning(
                    "%s: SL tidak valid (sl=%.8f, sl_pct=%.2f%% vs max=%.2f%%) "
                    "— overriding ke %.6f",
                    symbol, sl, sl_pct, max_sl_pct, sl_override,
                )
                sl = sl_override

        size_modified = abs(approved_size - quantity) > 1e-10
        sl_modified   = (
            stop_loss  is not None and sl is not None
            and abs(sl - stop_loss) > 1e-10
        )
        tp_modified   = (
            take_profit is not None and tp is not None
            and abs(tp - take_profit) > 1e-10
        )
        decision = (
            RiskDecision.MODIFIED
            if (size_modified or sl_modified or tp_modified)
            else RiskDecision.APPROVED
        )

        # [BUG-FIX — kritis] Sebelumnya "if sl else None"/"if tp else None"
        # -- sl/tp=0.0 (nilai valid secara tipe data, walau harga 0 sendiri
        # tidak masuk akal untuk stop/target) dianggap falsy dan DIHILANGKAN
        # jadi None, bukan malah di-floor/ditolak secara eksplisit. Dengan
        # floor guard di atas, sl untuk BUY sekarang seharusnya tidak akan
        # pernah <=0 lagi -- tapi pengecekan `is not None` tetap dipasang
        # di sini sebagai lapisan pertahanan kedua (defense in depth) untuk
        # SELL/edge-case lain yang belum ter-floor eksplisit.
        assessment = RiskAssessment(
            decision=decision,
            reason=size_reason,
            approved_size=round(approved_size, 8),
            recommended_quantity=round(approved_size, 8),
            stop_loss=round(sl, 8) if sl is not None else None,
            take_profit=round(tp, 8) if tp is not None else None,
        )
        log.info("Risk: %s | %s", symbol, assessment)
        # [Opsi 1 -- audit item #2] Reserve slot ATOMIK di sini, masih di
        # dalam _evaluate_lock yang sama dgn pengecekan max_open_positions
        # di atas -- menutup race antar-worker GATE3_WORKERS. side="buy" di
        # spot SELALU berarti posisi baru (side="sell" SELALU menutup
        # existing, tidak lewat jalur ini). Caller (_handle_buy) WAJIB
        # release_position_slot() kalau entry gagal setelah titik ini.
        # [SLOT-LEAK FIX] reserve_slot=False = mode PROBE (read-only,
        # dipakai commander G4) -- lihat komentar identik di
        # future/risk_future.py utk penjelasan lengkap kebocoran slot.
        if reserve_slot and side == "buy" and assessment.is_approved:
            self.reserve_position_slot()
        return assessment

    def _compute_position_size(
        self,
        side:      str,
        price:     float,
        requested: float,
        atr:       Optional[float],
    ) -> Tuple[Optional[float], str]:
        # [BUG-FIX] Sebelumnya: fungsi ini langsung membagi dengan price
        # tanpa validasi sendiri — ZeroDivisionError kalau price<=0.
        # Saat ini AMAN karena satu-satunya caller (_evaluate_order_locked)
        # sudah cek price<=0 sebelum memanggil fungsi ini. Tapi fungsi ini
        # rapuh terhadap perubahan di masa depan (caller baru, refactor,
        # test langsung) yang tidak melalui guard itu. Tambah validasi
        # sendiri agar fungsi ini aman dipanggil independen.
        if price is None or price <= 0:
            return None, f"Invalid price: {price}"

        # [BUG-FIX — kritis] Sebelumnya parameter `side` diterima TAPI TIDAK
        # PERNAH dipakai di fungsi ini -- baik cabang ATR maupun non-ATR
        # menerapkan cap max_position_size_pct (dan sizing berbasis risk-
        # per-trade) yang SAMA untuk buy MAUPUN sell. Di bot spot ini (tidak
        # ada short), side="sell" SELALU berarti MENUTUP posisi existing,
        # bukan membuka posisi baru -- aturan "jangan buka posisi lebih dari
        # X% equity" tidak relevan sama sekali untuk menutup posisi yang
        # SUDAH ada. Dibuktikan lewat eksperimen: posisi $500 yang mau
        # ditutup penuh, dengan max_position_size_pct=10% dari equity $2000
        # (=$200), ke-CAP jadi cuma $200 -- SISA $300 TIDAK IKUT TERJUAL,
        # padahal caller (_evaluate_order_locked baris ~464) SUDAH menjamin
        # `requested` di sini tidak melebihi free_coin_balance riil. Sekarang:
        # sell/close selalu memakai `requested` apa adanya, tidak dibatasi
        # equity-based cap ataupun ATR-based sizing (keduanya aturan untuk
        # MEMBUKA posisi baru).
        if side == "sell":
            return requested, (
                "Sell/close: ukuran mengikuti saldo riil yang sudah "
                "diverifikasi caller, tidak dibatasi max_position_size_pct "
                "(aturan itu untuk membuka posisi baru, bukan menutup)"
            )

        equity = self._current_equity

        if atr and atr > 0 and price > 0:
            risk_amount   = equity * (self._risk_per_trade_pct / 100)
            stop_distance = atr * self._atr_sl_mult
            if stop_distance <= 0:
                return None, "ATR stop distance is zero."

            vol_sized = risk_amount / stop_distance
            max_qty   = (equity * self._max_position_size_pct / 100) / price
            final     = min(vol_sized, max_qty)

            reason = (
                f"ATR-sized: risk=${risk_amount:.2f} "
                f"/ stop_dist={stop_distance:.6f} "
                f"= {final:.8f} units"
            )
            return final, reason

        max_qty   = (equity * self._max_position_size_pct / 100) / price
        req_value = requested * price
        max_value = equity * self._max_position_size_pct / 100

        if req_value <= max_value:
            return requested, "Within max_position_size_pct (no ATR)"

        capped = max_qty
        log.warning(
            "Position size capped (no ATR): $%.2f → $%.2f (%.1f%% equity limit)",
            req_value, max_value, self._max_position_size_pct,
        )
        return capped, f"Capped to {self._max_position_size_pct}% equity (no ATR)"

    def _compute_sl_tp(
        self,
        side:        str,
        price:       float,
        stop_loss:   Optional[float],
        take_profit: Optional[float],
        atr:         Optional[float],
    ) -> Tuple[Optional[float], Optional[float]]:
        sl = stop_loss
        tp = take_profit

        if atr and atr > 0:
            if side == "buy":
                if sl is None:
                    sl = price - atr * self._atr_sl_mult
                if tp is None:
                    tp = price + atr * self._atr_tp_mult
            else:
                if sl is None:
                    sl = price + atr * self._atr_sl_mult
                if tp is None:
                    tp = price - atr * self._atr_tp_mult
        else:
            if side == "buy":
                if sl is None:
                    sl = price * (1 - self._stop_loss_pct / 100)
                if tp is None:
                    tp = price * (1 + self._take_profit_pct / 100)
            else:
                if sl is None:
                    sl = price * (1 + self._stop_loss_pct / 100)
                if tp is None:
                    tp = price * (1 - self._take_profit_pct / 100)

        if side == "buy" and sl is not None and sl >= price:
            log.warning(
                "SL %.6f >= entry %.6f for long — resetting to %.1f%%",
                sl, price, self._stop_loss_pct,
            )
            sl = price * (1 - self._stop_loss_pct / 100)
        if side == "sell" and sl is not None and sl <= price:
            sl = price * (1 + self._stop_loss_pct / 100)

        return sl, tp


