"""
future/risk_future.py — RiskManager khusus Binance USDT-M Futures

Extend engine.risk_base.BaseRiskManager (halt/resume, drawdown, symbol halt,
breakeven/trailing SL, statistik performa -- semua dari sana, tidak diubah).
Di sini: evaluate_order(), _compute_position_size(), _compute_sl_tp() versi
leverage-aware, PLUS pengecekan keamanan liquidation yang tidak ada
konsepnya sama sekali di spot.

⚠️ Liquidation price yang dipakai untuk validasi berasal dari
future.liquidation.calculate_liquidation_price() -- formula APPROXIMATE,
lihat peringatan lengkap di modul itu. RiskManager di sini menolak order
kalau stop_loss tidak cukup aman relatif liquidation price (via
is_stop_loss_safe()), TAPI ini hanya seaman akurasi liquidation_price itu
sendiri.
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Tuple

from engine.risk_base import BaseRiskManager, HaltReason, RiskDecision, RiskAssessment
from future.liquidation import calculate_liquidation_price, is_stop_loss_safe

log = logging.getLogger("risk_future")


class RiskManager(BaseRiskManager):

    def __init__(self, config: Dict, db=None):
        super().__init__(config, db)
        # [FUTURES-SPECIFIC] Parameter tambahan yang tidak ada di spot.
        self._default_leverage       = int(config.get("default_leverage", 10))
        self._max_leverage           = int(config.get("max_leverage", 20))
        self._default_mmr            = float(config.get("maintenance_margin_rate", 0.005))
        self._min_liquidation_safety_pct = float(config.get("min_liquidation_safety_pct", 20.0))
        # [FUTURES-SPECIFIC] Margin isolated: cap berapa % dari FREE MARGIN
        # (bukan equity total) yang boleh dipakai utk satu posisi baru.
        # Beda dari spot's max_position_size_pct yang menghitung dari full
        # equity (di futures, "equity" vs "margin tersedia" adalah dua hal
        # berbeda krn sebagian equity sudah terkunci sbg margin posisi lain).

    # [FUTURES-SPECIFIC -- BARU] Tabel faktor leverage per profile koin.
    # Filosofi: koin yang diklasifikasi "stabil/tren jelas" boleh leverage
    # sedikit lebih tinggi, koin yang diklasifikasi "volatile/ekstrem"
    # WAJIB leverage lebih rendah. Angka ini bisa dikalibrasi ulang --
    # bukan hasil backtest, murni penalaran risiko yang masuk akal.
    PROFILE_LEVERAGE_FACTOR = {
        "hodl_accumulate":  1.3,
        "trend_follow":     1.15,
        "breakout_swift":   0.9,
        "mean_revert":      1.0,
        "scalp_volatile":   0.6,
        "extreme_momentum": 0.4,
    }

    def compute_adaptive_leverage(
        self,
        base_leverage: int,
        atr_pct:       Optional[float] = None,
        regime:        Optional[str]   = None,
        profile_name:  Optional[str]   = None,
        score:         Optional[float] = None,
    ) -> int:
        """
        [FUTURES-SPECIFIC -- BARU] Leverage adaptif, MENGGANTIKAN nilai
        config["default_leverage"] yang sebelumnya FLAT/seragam utk semua
        koin. Empat faktor dikombinasikan secara MULTIPLIKATIF:

        1. Volatilitas (ATR% thd harga) -- makin volatile, leverage makin
           rendah (posisi leveraged di koin liar = risiko liquidation tinggi)
        2. Regime pasar -- trend jelas (bull/bear) sedikit lebih longgar,
           volatile_expansion/undefined lebih ketat
        3. Profile koin -- hodl_accumulate/trend_follow (biasanya koin besar,
           stabil) dapat bonus, scalp_volatile/extreme_momentum dapat penalti
        4. Skor confidence sinyal -- skor tinggi = sedikit bonus, skor pas-
           pasan di ambang threshold = sedikit penalti

        Hasil akhir SELALU di-clamp ke [1, max_leverage] dari config --
        tidak akan pernah melebihi batas atas yang sudah ditentukan.

        Kalau semua parameter opsional None (data tidak tersedia), fungsi
        ini return base_leverage APA ADANYA (fallback aman, tidak menebak).
        """
        if atr_pct is None and regime is None and profile_name is None and score is None:
            return base_leverage

        factor = 1.0

        if atr_pct is not None and atr_pct > 0:
            if atr_pct < 0.5:
                factor *= 1.2
            elif atr_pct < 1.0:
                factor *= 1.0
            elif atr_pct < 2.0:
                factor *= 0.7
            elif atr_pct < 3.5:
                factor *= 0.5
            else:
                factor *= 0.3

        if regime is not None:
            regime_lower = str(regime).lower()
            if "trending" in regime_lower:
                factor *= 1.1
            elif "ranging" in regime_lower:
                factor *= 0.9
            elif "volatile_expansion" in regime_lower:
                factor *= 0.6
            elif "undefined" in regime_lower:
                factor *= 0.7
            # regime lain (kalau ada) -- tidak ada penyesuaian, factor tetap

        if profile_name is not None:
            profile_factor = self.PROFILE_LEVERAGE_FACTOR.get(str(profile_name).lower(), 1.0)
            factor *= profile_factor

        if score is not None:
            if score >= 80:
                factor *= 1.1
            elif score >= 65:
                factor *= 1.0
            else:
                factor *= 0.85

        adaptive_leverage = round(base_leverage * factor)
        clamped = max(1, min(adaptive_leverage, self._max_leverage))

        log.debug(
            "Adaptive leverage: base=%dx factor=%.3f (atr=%s regime=%s profile=%s score=%s) -> %dx",
            base_leverage, factor, atr_pct, regime, profile_name, score, clamped,
        )
        return clamped

    async def evaluate_order(
        self,
        symbol:      str,
        side:        str,             # "buy" atau "sell" (aksi level-exchange)
        price:       float,
        quantity:    float,
        leverage:    Optional[int]  = None,
        existing_position_side: Optional[str] = None,  # None|"long"|"short"
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
        atr:         Optional[float] = None,
        margin_mode: str             = "isolated",
        exchange_min_cost: Optional[float] = None,
    ) -> RiskAssessment:
        async with self._evaluate_lock:
            return await self._evaluate_order_locked(
                symbol=symbol, side=side, price=price, quantity=quantity,
                leverage=leverage, existing_position_side=existing_position_side,
                stop_loss=stop_loss, take_profit=take_profit, atr=atr,
                margin_mode=margin_mode, exchange_min_cost=exchange_min_cost,
            )

    async def _evaluate_order_locked(
        self,
        symbol:      str,
        side:        str,
        price:       float,
        quantity:    float,
        leverage:    Optional[int],
        existing_position_side: Optional[str],
        stop_loss:   Optional[float],
        take_profit: Optional[float],
        atr:         Optional[float],
        margin_mode: str,
        exchange_min_cost: Optional[float],
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
        if price <= 0:
            return RiskAssessment(RiskDecision.REJECTED, f"Invalid price: {price}")

        # Tentukan intent: buka baru, tambah (add), atau tutup/kurangi (reduce/close)
        intended_side = "long" if side == "buy" else "short"
        is_closing_or_reducing = (
            existing_position_side is not None
            and existing_position_side != intended_side
        )
        is_adding = (
            existing_position_side is not None
            and existing_position_side == intended_side
        )
        is_opening_new = existing_position_side is None

        if is_closing_or_reducing:
            # [MIRROR dari spot: side="sell" yg menutup posisi] Order yg
            # MENGURANGI/MENUTUP posisi existing tidak dibatasi
            # max_position_size_pct/max_open_positions -- caller (exchange
            # connector) sudah menjamin quantity tidak melebihi posisi yg
            # benar-benar ada.
            return RiskAssessment(
                RiskDecision.APPROVED,
                "Reduce/close: ukuran mengikuti posisi existing yang sudah "
                "diverifikasi caller, tidak dibatasi position sizing "
                "(aturan itu untuk membuka/menambah posisi baru).",
                approved_size=quantity,
                recommended_quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        # --- Dari sini: is_opening_new ATAU is_adding, keduanya perlu cek
        # margin & max_open_positions (khusus opening_new) ---
        if is_opening_new and self.is_symbol_halted(symbol):
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"{symbol} sedang di-halt karena loss >= {self._max_loss_per_symbol}%.",
            )
        if is_opening_new and self._open_positions_count >= self._max_open_positions:
            # [CAPITAL-ALLOCATOR PRASYARAT] Slot habis = soal kapasitas,
            # bukan sinyal jelek -- kandidat ini layak ditunggu sampai ada
            # posisi lain yang tutup (beda dari REJECTED biasa).
            return RiskAssessment(
                RiskDecision.REJECTED_INSUFFICIENT_CAPITAL,
                f"Max open positions reached: "
                f"{self._open_positions_count}/{self._max_open_positions}",
            )

        eff_leverage = leverage if leverage is not None else self._default_leverage
        if eff_leverage > self._max_leverage:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Leverage {eff_leverage}x melebihi batas maksimum {self._max_leverage}x",
            )
        if eff_leverage <= 0:
            return RiskAssessment(RiskDecision.REJECTED, f"Leverage tidak valid: {eff_leverage}")

        approved_size, size_reason = self._compute_position_size(
            intended_side, price, quantity, atr, eff_leverage,
        )
        if approved_size is None or approved_size <= 0:
            return RiskAssessment(
                RiskDecision.REJECTED, size_reason or "Position sizing failed."
            )

        notional     = approved_size * price
        required_margin = notional / eff_leverage
        effective_min = self._min_order_value_usdt
        if exchange_min_cost is not None and exchange_min_cost > effective_min:
            effective_min = exchange_min_cost
        if notional < effective_min:
            return RiskAssessment(
                RiskDecision.REJECTED,
                f"Order notional ${notional:.4f} < minimum ${effective_min:.4f}",
            )

        # [FUTURES-SPECIFIC] Cek margin tersedia. self._free_balance di
        # futures = margin bebas (lihat FutureExchangeConnector.fetch_balance),
        # BUKAN saldo currency spot biasa.
        if required_margin > self._free_balance * 0.99:
            # [CAPITAL-ALLOCATOR PRASYARAT] Margin kurang = soal kapasitas,
            # bukan sinyal jelek -- kandidat ini layak ditunggu sampai
            # margin bebas bertambah (beda dari REJECTED biasa).
            return RiskAssessment(
                RiskDecision.REJECTED_INSUFFICIENT_CAPITAL,
                f"Margin tidak cukup: butuh ${required_margin:.2f}, "
                f"tersedia ${self._free_balance:.2f}",
            )

        sl, tp = self._compute_sl_tp(intended_side, price, stop_loss, take_profit, atr)

        # [FUTURES-SPECIFIC -- TIDAK ADA DI SPOT] Cek keamanan SL terhadap
        # liquidation price. Kalau SL yang dihitung TIDAK cukup aman (terlalu
        # dekat/melewati liquidation), order DITOLAK -- lebih baik menolak
        # daripada membiarkan posisi terbuka dengan risiko liquidation
        # sebelum SL sempat jalan.
        liq_result = calculate_liquidation_price(
            entry_price=price, leverage=eff_leverage, side=intended_side,
            mmr=self._default_mmr, margin_mode=margin_mode,
        )
        if sl is not None:
            sl_safe = is_stop_loss_safe(
                stop_loss_price=sl,
                liquidation_price=liq_result.liquidation_price,
                entry_price=price,
                side=intended_side,
                min_safety_margin_pct=self._min_liquidation_safety_pct,
            )
            if not sl_safe:
                return RiskAssessment(
                    RiskDecision.REJECTED,
                    f"Stop-loss ${sl:.6f} tidak cukup aman terhadap estimasi "
                    f"liquidation price ${liq_result.liquidation_price:.6f} "
                    f"(leverage={eff_leverage}x) -- butuh margin keamanan "
                    f"minimal {self._min_liquidation_safety_pct}%. "
                    f"⚠️ liquidation_price ini APPROXIMATE, lihat future/liquidation.py.",
                )

        size_modified = abs(approved_size - quantity) > 1e-10
        sl_modified   = stop_loss is not None and sl is not None and abs(sl - stop_loss) > 1e-10
        tp_modified   = take_profit is not None and tp is not None and abs(tp - take_profit) > 1e-10
        decision = (
            RiskDecision.MODIFIED
            if (size_modified or sl_modified or tp_modified)
            else RiskDecision.APPROVED
        )

        assessment = RiskAssessment(
            decision=decision,
            reason=size_reason + f" | liq_price≈{liq_result.liquidation_price:.6f} (APPROXIMATE)",
            approved_size=round(approved_size, 8),
            recommended_quantity=round(approved_size, 8),
            stop_loss=round(sl, 8) if sl is not None else None,
            take_profit=round(tp, 8) if tp is not None else None,
            leverage=eff_leverage,
            margin_mode=margin_mode,
            liquidation_price=liq_result.liquidation_price,
        )
        log.info("Risk (futures): %s | %s | leverage=%dx", symbol, assessment, eff_leverage)
        return assessment

    def _compute_position_size(
        self,
        side:      str,
        price:     float,
        requested: float,
        atr:       Optional[float],
        leverage:  int,
    ) -> Tuple[Optional[float], str]:
        """
        [FUTURES-SPECIFIC] BEDA dari spot: sizing di sini menghitung MARGIN
        yang dipakai (notional/leverage), dibatasi terhadap FREE MARGIN
        (self._free_balance), bukan equity total seperti spot. Posisi
        leverage tinggi otomatis butuh margin lebih kecil utk notional yang
        sama -- risk_per_trade_pct tetap dihitung dari RISIKO (jarak ke SL),
        bukan dari margin, supaya besar leverage tidak "menipu" ukuran risk
        sebenarnya.
        """
        if price is None or price <= 0:
            return None, f"Invalid price: {price}"

        equity = self._current_equity

        if atr and atr > 0 and price > 0:
            # Risk-based sizing: SAMA seperti spot (risiko dihitung dari
            # jarak SL, bukan dari margin) -- leverage tidak mengubah
            # RISIKO per unit, cuma mengubah margin yang dibutuhkan.
            risk_amount   = equity * (self._risk_per_trade_pct / 100)
            stop_distance = atr * self._atr_sl_mult
            if stop_distance <= 0:
                return None, "ATR stop distance is zero."
            vol_sized = risk_amount / stop_distance

            # Cap tambahan: margin yang dibutuhkan tidak boleh melebihi
            # max_position_size_pct dari FREE MARGIN yang tersedia (bukan
            # equity total -- equity total termasuk margin yg sudah
            # terkunci di posisi lain).
            max_notional_by_margin = (
                self._free_balance * self._max_position_size_pct / 100
            ) * leverage
            max_qty_by_margin = max_notional_by_margin / price
            final = min(vol_sized, max_qty_by_margin)

            reason = (
                f"ATR-sized (futures): risk=${risk_amount:.2f} / "
                f"stop_dist={stop_distance:.6f} = {final:.8f} units "
                f"(margin needed=${(final*price/leverage):.2f})"
            )
            return final, reason

        max_notional_by_margin = (
            self._free_balance * self._max_position_size_pct / 100
        ) * leverage
        max_qty = max_notional_by_margin / price
        req_notional = requested * price

        if req_notional <= max_notional_by_margin:
            return requested, "Within max_position_size_pct of free margin (no ATR)"

        log.warning(
            "Position size capped (futures, no ATR): notional $%.2f → $%.2f "
            "(%.1f%% free margin x %dx leverage)",
            req_notional, max_notional_by_margin, self._max_position_size_pct, leverage,
        )
        return max_qty, f"Capped to {self._max_position_size_pct}% free margin (no ATR)"

    def _compute_sl_tp(
        self,
        side:        str,
        price:       float,
        stop_loss:   Optional[float],
        take_profit: Optional[float],
        atr:         Optional[float],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        [IDENTIK dgn logic spot _compute_sl_tp] -- tidak ada perbedaan
        semantik SL/TP jarak% antara spot & futures, cuma sisi yang beda
        (long/short bukan buy/sell). Keamanan thd liquidation dicek
        TERPISAH di _evaluate_order_locked (via is_stop_loss_safe), bukan
        di sini -- supaya fungsi ini tetap murni "hitung SL/TP dari
        jarak/ATR", tidak tercampur concern liquidation.
        """
        sl = stop_loss
        tp = take_profit

        if atr and atr > 0:
            if side == "long":
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
            if side == "long":
                if sl is None:
                    sl = price * (1 - self._stop_loss_pct / 100)
                if tp is None:
                    tp = price * (1 + self._take_profit_pct / 100)
            else:
                if sl is None:
                    sl = price * (1 + self._stop_loss_pct / 100)
                if tp is None:
                    tp = price * (1 - self._take_profit_pct / 100)

        if side == "long" and sl is not None and sl >= price:
            log.warning(
                "SL %.6f >= entry %.6f for long — resetting to %.1f%%",
                sl, price, self._stop_loss_pct,
            )
            sl = price * (1 - self._stop_loss_pct / 100)
        if side == "short" and sl is not None and sl <= price:
            sl = price * (1 + self._stop_loss_pct / 100)

        return sl, tp
