"""
execution.py
AlgoTrader Pro v7.0

"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from constants import APP_VERSION
from database import DatabaseManager, Trade
from exchange import ExchangeConnector, WebSocketFeed
from risk import RiskAssessment, RiskDecision
from strategy import SignalEvent, SignalType

log = logging.getLogger("execution")

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class OrderExecutionManager:

    ICEBERG_THRESHOLD_PCT = 3.0
    ICEBERG_CHUNK_COUNT   = 4
    FILL_TIMEOUT_SECS     = 30
    FILL_POLL_INTERVAL    = 2.0
    MAX_SLIPPAGE_PCT      = 0.5  # default fallback

    # Slippage dinamis per profil koin
    SLIPPAGE_PER_PROFILE = {
        "hodl_accumulate":  0.3,   # Blue chip — spread ketat
        "trend_follow":     0.5,   # Mid cap — normal
        "breakout_swift":   1.0,   # Aktif — beri ruang
        "scalp_volatile":   1.5,   # Volatile — ruang lebih lebar
        "mean_revert":      0.5,   # Normal
        "extreme_momentum": 2.0,   # Meme/pump — sangat volatile
    }
    SPREAD_THRESHOLD_PCT  = 0.15

    _SIGNAL_ORIGIN_MAX = 490

    def __init__(
        self,
        exchange:          ExchangeConnector,
        db:                DatabaseManager,
        on_trade_executed: Optional[Callable] = None,
        max_slippage_pct:  float              = MAX_SLIPPAGE_PCT,
        ws_feed:           Optional[WebSocketFeed] = None,
    ):
        self.exchange          = exchange
        self.db                = db
        self.on_trade_executed = on_trade_executed
        self.max_slippage_pct  = max_slippage_pct
        self.ws_feed           = ws_feed

    async def execute_signal(
        self,
        signal:     SignalEvent,
        assessment: RiskAssessment,
    ) -> Optional[Trade]:
        t_start = time.monotonic()

        if not assessment.is_approved:
            log.warning(
                "Eksekusi diblokir — risk rejected: %s", assessment.reason
            )
            return None

        symbol = signal.symbol
        side   = "buy" if signal.signal_type == SignalType.BUY else "sell"
        price  = signal.price
        amount = assessment.approved_size

        if not amount or amount <= 0:
            log.error(
                "Amount tidak valid=%.8f untuk %s — abort.", amount or 0, symbol
            )
            return None

        if price is None or price <= 0:
            log.error(
                "Signal price tidak valid=%.8f untuk %s — abort.",
                price or 0, symbol,
            )
            await self.db.save_log(
                "ERROR", "execution",
                f"Signal price invalid {symbol}: price={price} — order dibatalkan.",
            )
            return None

        if self.ws_feed and not self.ws_feed.is_feed_healthy(symbol):
            log.warning(
                "WS feed stale untuk %s — pakai REST untuk slippage check.", symbol
            )

        # Dynamic slippage berdasarkan profil koin
        _coin_profile = signal.metadata.get("coin_profile", "") if signal.metadata else ""
        _profile_str = _coin_profile.value if hasattr(_coin_profile, "value") else str(_coin_profile)
        _max_slip = self.SLIPPAGE_PER_PROFILE.get(_profile_str, self.max_slippage_pct)

        slip_ok, spread_pct, depth_slip = await self._check_slippage(
            symbol, price, side, amount, max_slippage_override=_max_slip
        )
        if not slip_ok:
            log.warning(
                "SLIPPAGE GUARD blokir %s | signal=%.6f spread=%.4f%% "
                "depth=%.4f%% max=%.2f%%",
                symbol, price, spread_pct, depth_slip, _max_slip,
            )
            await self.db.save_log(
                "WARNING", "execution",
                f"Slippage guard: {symbol} spread={spread_pct:.4f}% "
                f"depth={depth_slip:.4f}% > max={_max_slip}%",
            )
            return None

        log.info(
            "EXECUTE %s %s | amount=%.8f signal_price=%.6f spread=%.4f%%",
            side.upper(), symbol, amount, price, spread_pct,
        )

        # ── Market Filter Validation ──────────────────────────────────────
        if self.exchange:
            mkt = self.exchange.get_market_info(symbol)
            min_amount = mkt.get("min_amount") or 0
            min_cost   = mkt.get("min_cost")   or 0
            if min_amount and amount < min_amount:
                log.error(
                    "Order DITOLAK [%s]: amount=%.8f < min_amount=%.8f",
                    symbol, amount, min_amount,
                )
                await self.db.save_log(
                    "ERROR", "execution",
                    f"Min amount tidak terpenuhi {symbol}: {amount:.8f} < {min_amount:.8f}",
                )
                return None
            order_cost = amount * price
            if min_cost and order_cost < min_cost:
                log.error(
                    "Order DITOLAK [%s]: cost=%.4f USDT < min_cost=%.4f USDT",
                    symbol, order_cost, min_cost,
                )
                await self.db.save_log(
                    "ERROR", "execution",
                    f"Min cost tidak terpenuhi {symbol}: {order_cost:.4f} < {min_cost:.4f} USDT",
                )
                return None
        # ── End Market Filter ──────────────────────────────────────────────
        use_market  = (spread_pct is None) or (spread_pct < self.SPREAD_THRESHOLD_PCT)
        use_iceberg = False

        if self.ws_feed:
            qvol24h = self.ws_feed.get_quote_volume_24h(symbol)
            if qvol24h > 0:
                order_pct = (amount * price) / qvol24h * 100
                if order_pct > self.ICEBERG_THRESHOLD_PCT:
                    use_iceberg = True
                    log.info(
                        "Iceberg triggered: order=$%.2f = %.4f%% dari 24h vol=$%.0f",
                        amount * price, order_pct, qvol24h,
                    )

        primary_trade: Optional[Trade] = None

        if use_iceberg:
            trades = await self._execute_iceberg(signal, assessment, side)
            primary_trade = trades[0] if trades else None
        elif use_market:
            primary_trade = await self._execute_market(
                signal, assessment, side, amount
            )
        else:
            primary_trade = await self._execute_limit(
                signal, assessment, side, amount, price
            )

        if primary_trade is not None:
            lat_ms = (time.monotonic() - t_start) * 1000
            log.info("Signal-to-fill latency: %s %.2f ms", symbol, lat_ms)
            await self.db.append_trade_note(
                primary_trade.id, f"latency_ms={lat_ms:.2f}"
            )

        return primary_trade

    async def _check_slippage(
        self,
        symbol:       str,
        signal_price: float,
        side:         str,
        amount:       float,
        max_slippage_override: Optional[float] = None,
    ) -> Tuple[bool, float, float]:
        effective_max = max_slippage_override if max_slippage_override is not None else self.max_slippage_pct
        if signal_price <= 0:
            log.error(
                "Slippage check: signal_price=%.8f tidak valid untuk %s — "
                "order ditolak.",
                signal_price, symbol,
            )
            return False, 0.0, 0.0

        current_price: Optional[float] = None
        spread_pct    = 0.0
        depth_slip    = 0.0

        if self.ws_feed is not None and self.ws_feed.is_feed_healthy(symbol):
            current_price = self.ws_feed.get_mid_price(symbol)
            spread_pct    = self.ws_feed.get_spread(symbol) or 0.0
            _, depth_slip = self.ws_feed.get_market_depth_slippage(
                symbol, side, amount * signal_price
            )

        if current_price is None:
            try:
                tk  = await self.exchange.fetch_ticker(symbol)
                bid = tk.get("bid")
                ask = tk.get("ask")
                if bid and ask and float(bid) > 0 and float(ask) > 0:
                    current_price = (float(bid) + float(ask)) / 2.0
                    spread_pct    = (float(ask) - float(bid)) / float(ask) * 100
                elif tk.get("last") and float(tk["last"]) > 0:
                    current_price = float(tk["last"])
            except Exception as e:
                log.warning(
                    "REST ticker fallback gagal untuk %s: %s — order diizinkan.",
                    symbol, e,
                )
                return True, 0.0, 0.0

        if current_price is None or current_price <= 0:
            return True, spread_pct, depth_slip

        if side == "buy" and current_price > signal_price:
            drift = (current_price - signal_price) / signal_price * 100
        elif side == "sell" and current_price < signal_price:
            drift = (signal_price - current_price) / signal_price * 100
        else:
            drift = 0.0

        total = drift + depth_slip

        if total > effective_max:
            log.warning(
                "Slippage check: %s signal=%.6f mid=%.6f drift=%.4f%% "
                "depth=%.4f%% total=%.4f%% > max=%.2f%%",
                symbol, signal_price, current_price,
                drift, depth_slip, total, effective_max,
            )
            return False, spread_pct, depth_slip

        return True, spread_pct, depth_slip

    async def _execute_market(
        self,
        signal:     SignalEvent,
        assessment: RiskAssessment,
        side:       str,
        amount:     float,
    ) -> Optional[Trade]:
        try:
            order = await self.exchange.create_order(
                symbol=signal.symbol,
                order_type="market",
                side=side,
                amount=amount,
            )
            return await self._process_fill(
                order, signal, assessment, signal.price
            )
        except Exception as e:
            log.error("Market order GAGAL [%s]: %s", signal.symbol, e)
            await self.db.save_log(
                "ERROR", "execution",
                f"Market order gagal {signal.symbol}: {e}",
            )
            return None

    async def _execute_limit(
        self,
        signal:     SignalEvent,
        assessment: RiskAssessment,
        side:       str,
        amount:     float,
        price:      float,
    ) -> Optional[Trade]:
        limit_price = price * 1.0005 if side == "buy" else price * 0.9995

        try:
            order    = await self.exchange.create_order(
                signal.symbol, "limit", side, amount, limit_price
            )
            order_id = order.get("id", "")
            log.info("Limit order submitted: %s @ %.8f", order_id, limit_price)

            filled_order = await self._poll_fill(signal.symbol, order_id)

            if filled_order:
                return await self._process_fill(
                    filled_order, signal, assessment, limit_price
                )

            log.warning(
                "Limit %s unfilled setelah %ds — cancel, fallback market.",
                order_id, self.FILL_TIMEOUT_SECS,
            )
            try:
                await self.exchange.cancel_order(order_id, signal.symbol)
            except Exception as ce:
                log.warning("Cancel error (mungkin sudah filled): %s", ce)
            
            current_price: Optional[float] = None
            if self.ws_feed and self.ws_feed.is_feed_healthy(signal.symbol):
                current_price = self.ws_feed.get_mid_price(signal.symbol)
            if current_price is None or current_price <= 0:
                try:
                    tk = await self.exchange.fetch_ticker(signal.symbol)
                    bid, ask = tk.get("bid"), tk.get("ask")
                    if bid and ask and float(bid) > 0 and float(ask) > 0:
                        current_price = (float(bid) + float(ask)) / 2.0
                    elif tk.get("last"):
                        current_price = float(tk["last"])
                except Exception as te:
                    log.warning("Tidak bisa ambil harga fresh untuk fallback check: %s", te)
            
            check_price = current_price if (current_price and current_price > 0) else price
            
            slip_ok, _, _ = await self._check_slippage(
                signal.symbol, check_price, side, amount
            )
            if not slip_ok:
                log.warning(
                    "Market fallback diblokir slippage guard untuk %s "
                    "(check_price=%.6f)", signal.symbol, check_price,
                )
                return None
            
            return await self._execute_market(signal, assessment, side, amount)

        except Exception as e:
            log.error("Limit order GAGAL [%s]: %s", signal.symbol, e)
            await self.db.save_log(
                "ERROR", "execution",
                f"Limit order gagal {signal.symbol}: {e}",
            )
            return None

    async def _poll_fill(
        self, symbol: str, order_id: str
    ) -> Optional[dict]:
        deadline = time.monotonic() + self.FILL_TIMEOUT_SECS
        attempt  = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                order  = await self.exchange.fetch_order(order_id, symbol)
                status = order.get("status", "")
                if status in ("closed", "filled"):
                    log.info(
                        "Order %s filled pada poll attempt %d",
                        order_id, attempt,
                    )
                    return order
                if status in ("canceled", "expired", "rejected"):
                    log.warning(
                        "Order %s terminal status: %s", order_id, status
                    )
                    return None
            except Exception as e:
                log.warning("Poll attempt %d error: %s", attempt, e)
            await asyncio.sleep(self.FILL_POLL_INTERVAL)
        return None

    async def _execute_iceberg(
        self,
        signal:     SignalEvent,
        assessment: RiskAssessment,
        side:       str,
    ) -> List[Trade]:
        total = assessment.approved_size

        # [BUG-FIX] Chunk iceberg bisa di bawah min_amount/min_cost exchange.
        # Sebelumnya: chunk = total / ICEBERG_CHUNK_COUNT (selalu 4), tanpa cek
        # minimum order exchange sama sekali. execute_signal() memvalidasi
        # min_amount/min_cost terhadap `total`, tapi begitu order dipecah jadi
        # 4 chunk, tiap chunk bisa jatuh di bawah minimum walau total-nya lolos
        # — exchange akan menolak create_order() tiap chunk, ditangkap except
        # generik di bawah, log error tapi loop tetap lanjut ke chunk berikut
        # (bisa berakhir 0/4 chunk filled tanpa sinyal jelas ke caller).
        # Sekarang: turunkan jumlah chunk otomatis (minimal 1) sampai tiap
        # chunk memenuhi min_amount & min_cost exchange.
        chunk_count = self.ICEBERG_CHUNK_COUNT
        if self.exchange:
            mkt        = self.exchange.get_market_info(signal.symbol)
            min_amount = mkt.get("min_amount") or 0
            min_cost   = mkt.get("min_cost")   or 0
            while chunk_count > 1:
                test_chunk = total / chunk_count
                test_cost  = test_chunk * signal.price
                if (min_amount and test_chunk < min_amount) or (min_cost and test_cost < min_cost):
                    chunk_count -= 1
                    continue
                break

        chunk = total / chunk_count
        done: List[Trade] = []
        actual_filled = 0.0
    
        log.info(
            "Iceberg: total=%.8f × %d chunks = %.8f each",
            total, chunk_count, chunk,
        )
    
        for i in range(chunk_count):
            slip_ok, _, _ = await self._check_slippage(
                signal.symbol, signal.price, side, chunk
            )
            if not slip_ok:
                filled_so_far = len(done)
                if filled_so_far > 0:
                    partial_amount = chunk * filled_so_far
                    log.warning(
                        "Iceberg chunk %d/%d diblokir slippage — "
                        "PARTIAL FILL: %d/%d chunk terisi (%.8f unit).",
                        i + 1, chunk_count,
                        filled_so_far, chunk_count, partial_amount
                    )
                    await self.db.save_log(
                        "WARNING", "execution",
                        f"Iceberg partial fill {signal.symbol}: "
                        f"{filled_so_far} chunks, actual={actual_filled:.8f}"
                    )
                else:
                    log.warning(
                        "Iceberg chunk %d/%d diblokir slippage guard (0 filled).",
                        i + 1, chunk_count
                    )
                break
    
            try:
                order = await self.exchange.create_order(
                    signal.symbol, "market", side, chunk
                )
                chunk_filled = float(
                    order.get("filled") or order.get("amount") or chunk
                )
                actual_filled += chunk_filled
    
                chunk_assessment = replace(
                    assessment,
                    approved_size=chunk_filled
                )
                trade = await self._process_fill(
                    order, signal, chunk_assessment, signal.price
                )
                if trade:
                    done.append(trade)
                if i < chunk_count - 1:
                    await asyncio.sleep(0.8)
            except Exception as e:
                log.error(
                    "Iceberg chunk %d/%d GAGAL [%s]: %s",
                    i + 1, chunk_count, signal.symbol, e
                )

        expected_max = total 
        if actual_filled > expected_max * 1.05:
            log.warning(
                "Iceberg %s: actual_filled=%.8f > expected=%.8f — "
                "kemungkinan double count, gunakan sum dari trade.filled",
                signal.symbol, actual_filled, expected_max,
            )
            
            actual_filled = sum(
                float(t.filled or t.amount or 0) for t in done
            )
            log.info(
                "Iceberg %s: actual_filled di-recalculate dari trades = %.8f",
                signal.symbol, actual_filled,
            )

        # [BUG-FIX] Sebelumnya caller (main.py._handle_buy) memakai
        # trades[0].executed_price (harga CHUNK PERTAMA saja) sebagai
        # entry_price untuk SELURUH posisi, padahal iceberg secara spesifik
        # dipakai untuk order BESAR (>3% volume 24h) di mana harga antar
        # chunk realistis bisa bergeser selama proses (jeda 0.8s per chunk).
        # amount sudah benar diagregasi lewat note iceberg_actual_filled,
        # tapi entry_price tidak punya mekanisme serupa — entry_price yang
        # tercatat bisa meleset dari cost basis sebenarnya, berdampak ke
        # akurasi PnL & pengecekan SL/TP sepanjang umur posisi.
        # Sekarang: hitung rata-rata tertimbang harga eksekusi semua chunk
        # yang berhasil filled, encode ke note yang sama seperti
        # iceberg_actual_filled supaya caller bisa pakai harga yang akurat.
        weighted_avg_price = signal.price
        if done and actual_filled > 0:
            weighted_avg_price = sum(
                float(t.executed_price or 0) * float(t.filled or t.amount or 0)
                for t in done
            ) / actual_filled

        log.info(
            "Iceberg selesai: %d/%d chunks filled | actual=%.8f | avg_price=%.8f | %s",
            len(done), chunk_count, actual_filled, weighted_avg_price, signal.symbol
        )
    
        if done:
            await self.db.append_trade_note(
                done[0].id,
                f"iceberg_actual_filled={actual_filled:.8f}"
                f"|iceberg_avg_price={weighted_avg_price:.8f}"
                f"|chunks={len(done)}/{chunk_count}"
            )
    
        return done

    async def _process_fill(
        self,
        order:           dict,
        signal:          SignalEvent,
        assessment:      RiskAssessment,
        requested_price: float,
    ) -> Optional[Trade]:
        symbol   = signal.symbol
        order_id = order.get("id") or str(uuid.uuid4())
        status   = order.get("status", "unknown")

        executed_price = order.get("average") or order.get("price") or requested_price
        try:
            executed_price = float(executed_price)
        except (TypeError, ValueError):
            executed_price = float(requested_price)

        filled = (
            order.get("filled") or order.get("amount") or assessment.approved_size
        )
        cost = order.get("cost") or (float(filled) * executed_price)

        fee_dict     = order.get("fee") or {}
        fee_cost     = fee_dict.get("cost")
        fee_currency = fee_dict.get("currency", "USDT")
        fee_rate     = fee_dict.get("rate")

        if fee_cost is None:
            fee_rate = self.exchange.get_taker_fee(symbol)
            fee_cost = float(cost) * fee_rate

        fee_cost = round(float(fee_cost), 8)
        if fee_rate is None:
            fee_rate = self.exchange.get_taker_fee(symbol)

        if requested_price and requested_price > 0 and executed_price > 0:
            direction    = 1.0 if signal.signal_type == SignalType.BUY else -1.0
            slippage_pct = (
                direction
                * (executed_price - requested_price)
                / requested_price * 100
            )
        else:
            slippage_pct = 0.0

        signal_origin = self._build_signal_origin(signal)

        trade_data = {
            "order_id":          order_id,
            "timestamp":         _utcnow(),
            "symbol":            symbol,
            "side":              "buy" if signal.signal_type == SignalType.BUY else "sell",
            "order_type":        order.get("type", "market"),
            "status":            status,
            "requested_price":   round(float(requested_price), 8),
            "executed_price":    round(executed_price, 8),
            "amount":            round(
                float(order.get("amount") or assessment.approved_size), 8
            ),
            "filled":            round(float(filled), 8),
            "cost":              round(float(cost), 8),
            "fee_cost":          fee_cost,
            "fee_currency":      fee_currency,
            "fee_rate":          round(float(fee_rate), 8) if fee_rate else None,
            "slippage_pct":      round(slippage_pct, 6),
            "stop_loss_price":   assessment.stop_loss,
            "take_profit_price": assessment.take_profit,
            "strategy_name":     signal.strategy,
            "strategy_profile":  getattr(signal, "strategy_profile", "") or "",
            "signal_origin":     signal_origin,
            "notes":             str(signal.metadata)[:1000] if signal.metadata else None,
        }

        trade = await self.db.save_trade(trade_data)

        log.info(
            "FILL recorded | %s | executed=%.6f slippage=%+.4f%% "
            "fee=%.6f %s | %s",
            symbol, executed_price, slippage_pct,
            fee_cost, fee_currency, signal_origin,
        )
        await self.db.save_log(
            "INFO", "execution",
            f"Fill {symbol} {trade_data['side'].upper()} @ {executed_price:.6f} "
            f"slip={slippage_pct:+.4f}% fee={fee_cost:.6f} {fee_currency} "
            f"| {signal_origin}",
        )

        if self.on_trade_executed:
            await self.on_trade_executed(trade)

        return trade

    def _build_signal_origin(self, signal: SignalEvent) -> str:
        meta   = signal.metadata or {}
        tokens: List[str] = []

        coin_profile  = meta.get("coin_profile", "")
        adaptive_mode = meta.get("adaptive_mode", "")
        exit_mode_val = meta.get("exit_mode", "")
        entry_trigger = meta.get("entry_trigger", "")
        atr_ratio     = meta.get("atr_ratio")

        if coin_profile:
            tokens.append(f"Profile({coin_profile})")
        if adaptive_mode and adaptive_mode not in ("N/A", "NORMAL", ""):
            tokens.append(f"Adaptive({adaptive_mode})")
        if exit_mode_val:
            tokens.append(f"Mode({exit_mode_val})")
        if entry_trigger and entry_trigger != "None":
            tokens.append(f"Trigger({entry_trigger})")
        if atr_ratio is not None and float(atr_ratio) != 1.0:
            tokens.append(f"ATRRatio({float(atr_ratio):.2f})")

        if meta.get("breakout_ok"):
            d = meta.get("breakout_dist_pct")
            tokens.append(f"Breakout({d:.3f}%)" if d else "Breakout")
        if meta.get("golden_cross"):
            tokens.append("GoldenCross")
        if meta.get("vol_ratio") is not None:
            tokens.append(f"Vol({float(meta['vol_ratio']):.2f}x)")
        if meta.get("rsi") is not None:
            tokens.append(f"RSI({float(meta['rsi']):.1f})")
        if meta.get("atr_pct") is not None:
            tokens.append(f"ATR%({float(meta['atr_pct']):.3f})")

        if meta.get("exit_reason"):
            exit_str = str(meta["exit_reason"])[:80]
            tokens.append(f"Exit({exit_str})")

        sent = meta.get("sentiment_score")
        if sent is not None and float(sent) != 0.0:
            tokens.append(f"Sent({float(sent):.3f})")

        sv = meta.get("strategy_version", f"v{APP_VERSION}")
        tokens.append(sv if sv.startswith("v") else f"v{sv}")

        if len(tokens) <= 1:
            skip_keys = {
                "atr", "ema9", "ema21", "ema50", "vwap", "coin_profile",
                "adaptive_mode", "exit_mode", "entry_trigger", "atr_ratio",
                "exit_label", "strategy_version", "breakout_ok", "breakout_dist",
                "breakout_dist_pct", "min_breakout_pct", "resistance",
                "vol_ratio", "volume_ok", "trend_ok", "momentum_ok",
                "above_vwap", "sentiment_score", "rsi", "atr_pct",
                "sl_from_strategy", "tp_from_strategy", "atr_sl_mult",
                "atr_tp_mult", "rsi_min_used", "rsi_max_used", "vol_mult_used",
                "max_hold_candles", "golden_cross",
            }
            extra = [
                f"{k}={v}"
                for k, v in meta.items()
                if k not in skip_keys and v is not None
            ][:4]
            tokens.extend(extra)

        if not tokens:
            return signal.strategy

        result = " | ".join(tokens)
        if len(result) <= self._SIGNAL_ORIGIN_MAX:
            return result

        while (
            len(tokens) > 1
            and len(" | ".join(tokens)) > self._SIGNAL_ORIGIN_MAX
        ):
            tokens.pop()

        return " | ".join(tokens)
