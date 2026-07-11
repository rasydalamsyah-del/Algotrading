"""
engine/risk_base.py — Base class RiskManager, market-agnostic

Diekstrak dari spot/risk_spot.py saat restrukturisasi engine/spot/future
(2026-07-11). Berisi SEMUA logic risk management yang bekerja identik untuk
spot maupun futures: halt/resume state machine, drawdown & daily-loss
tracking, symbol-level halt, breakeven/trailing SL (sudah side-aware sejak
perbaikan bias long-only sebelumnya), dan seluruh statistik performa
(sharpe/sortino/max_drawdown/calmar/profit_factor/dst -- murni matematika).

YANG SENGAJA TIDAK ADA DI SINI (harus diimplementasikan di subclass masing-
masing, karena semantik "side" & sizing berbeda secara mendasar antara spot
dan futures -- spot: side="sell" SELALU berarti menutup posisi existing;
futures: "sell" bisa berarti BUKA SHORT atau TUTUP LONG, dan sizing perlu
leverage + liquidation-safety check):
- evaluate_order() / _evaluate_order_locked()
- _compute_position_size()
- _compute_sl_tp()

Subclass WAJIB implementasikan ketiganya sendiri.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, date
from enum import Enum
from typing import Optional, Dict, List

import numpy as np

log = logging.getLogger("risk_base")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class HaltReason(str, Enum):
    NONE             = ""
    DAILY_LOSS       = "daily_loss_limit"
    MAX_DRAWDOWN     = "max_drawdown_breached"
    PANIC_BUTTON     = "panic_button"
    MANUAL           = "manual_halt"
    LOW_BALANCE      = "insufficient_balance"
    # [FUTURES-READY] Alasan halt baru, khusus futures -- belum pernah
    # dipicu di manapun sampai risk_future.py benar-benar memakainya.
    LIQUIDATION_RISK = "liquidation_risk_breached"


class RiskDecision(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


@dataclass
class RiskAssessment:
    decision:      RiskDecision
    reason:        str
    approved_size: Optional[float] = None
    recommended_quantity: Optional[float] = None
    stop_loss:     Optional[float] = None
    take_profit:   Optional[float] = None
    # [FUTURES-READY] Optional, default None -- tidak dipakai/tidak diisi
    # sama sekali oleh spot RiskManager (behavior tidak berubah). Diisi oleh
    # future/risk_future.py supaya execution_future.py bisa menyisipkan
    # leverage/margin_mode ke trade_data tanpa harus menebak/getattr fallback.
    leverage:      Optional[int] = None
    margin_mode:   Optional[str] = None
    liquidation_price: Optional[float] = None

    @property
    def is_approved(self) -> bool:
        return self.decision in (RiskDecision.APPROVED, RiskDecision.MODIFIED)

    def __str__(self) -> str:
        return (
            f"RiskAssessment({self.decision.value}) "
            f"size={self.approved_size} "
            f"sl={self.stop_loss} tp={self.take_profit} "
            f"— {self.reason}"
        )


class BaseRiskManager:
    """
    Base class market-agnostic. Subclass (RiskManager di spot/, RiskManager
    di future/) WAJIB implementasikan evaluate_order(), _compute_position_size(),
    dan _compute_sl_tp() sendiri.
    """

    def __init__(self, config: Dict, db=None):
        self._evaluate_lock = asyncio.Lock()
        self._max_drawdown_pct      = float(config.get("max_drawdown_pct",      15.0))
        self._max_position_size_pct = float(config.get("max_position_size_pct", 10.0))
        self._max_open_positions    = int(config.get("max_open_positions",       3))
        self._stop_loss_pct         = float(config.get("stop_loss_pct",          2.5))
        self._take_profit_pct       = float(config.get("take_profit_pct",        5.0))
        self._atr_sl_mult           = float(config.get("atr_multiplier_sl",      2.0))
        self._atr_tp_mult           = float(config.get("atr_multiplier_tp",      3.5))
        self._min_order_value_usdt  = float(config.get("min_order_value_usdt",  10.0))
        self._daily_loss_limit_pct  = float(config.get("daily_loss_limit_pct",  10.0))
        self._risk_per_trade_pct    = float(config.get("risk_per_trade_pct",     1.0))
        self._trailing_atr_mult     = float(config.get("trailing_atr_mult",      1.5))
        self._use_trailing_stop     = bool(config.get("use_trailing_stop",       True))
        self._max_loss_per_symbol   = float(config.get("max_loss_per_symbol",    2.0))
        self._db = db

        self._current_equity:       float = 0.0
        self._initial_equity:       float = 0.0
        self._free_balance:         float = 0.0
        self._open_positions_count: int   = 0
        self._peak_equity:          float = 0.0
        self._current_drawdown_pct: float = 0.0
        self._daily_loss_pct:       float = 0.0
        self._daily_reset_date:     date  = _utcnow().date()
        self._equity_at_day_start:  float = 0.0
        self._dynamic_daily_limit: float = self._daily_loss_limit_pct
        self._halted:      bool       = False
        self._halt_reason: HaltReason = HaltReason.NONE
        self._halt_detail: str        = ""
        self._symbol_halt: Dict[str, bool]  = {}
        self._symbol_loss: Dict[str, float] = {}

    def _update_config(self, config: dict) -> None:
        """Hot-reload parameter risk dari config terbaru."""
        old_risk = self._risk_per_trade_pct
        old_dd   = self._max_drawdown_pct
        old_pos  = self._max_open_positions
        self._max_drawdown_pct      = float(config.get("max_drawdown_pct",      15.0))
        self._max_position_size_pct = float(config.get("max_position_size_pct", 10.0))
        self._max_open_positions    = int(config.get("max_open_positions",       3))
        self._stop_loss_pct         = float(config.get("stop_loss_pct",          2.5))
        self._take_profit_pct       = float(config.get("take_profit_pct",        5.0))
        self._atr_sl_mult           = float(config.get("atr_multiplier_sl",      2.0))
        self._atr_tp_mult           = float(config.get("atr_multiplier_tp",      3.5))
        self._min_order_value_usdt  = float(config.get("min_order_value_usdt",  10.0))
        self._daily_loss_limit_pct  = float(config.get("daily_loss_limit_pct",  10.0))
        self._risk_per_trade_pct    = float(config.get("risk_per_trade_pct",     1.0))
        self._trailing_atr_mult     = float(config.get("trailing_atr_mult",      1.5))
        self._use_trailing_stop     = bool(config.get("use_trailing_stop",       True))
        self._max_loss_per_symbol   = float(config.get("max_loss_per_symbol",    2.0))
        log.info(
            "RiskManager config updated | MaxDD: %.1f→%.1f%% Risk/trade: %.2f→%.2f%% "
            "MaxOpen: %d→%d | SL:%.2f%% TP:%.2f%% ATR_SL:%.2fx ATR_TP:%.2fx "
            "Trailing:%.2fx(%s)",
            old_dd, self._max_drawdown_pct,
            old_risk, self._risk_per_trade_pct,
            old_pos, self._max_open_positions,
            self._stop_loss_pct, self._take_profit_pct,
            self._atr_sl_mult, self._atr_tp_mult,
            self._trailing_atr_mult, self._use_trailing_stop,
        )

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        if not self._halted:
            return ""
        return self._halt_reason.value

    @property
    def halt_detail(self) -> str:
        return self._halt_detail if self._halted else ""

    @property
    def current_drawdown_pct(self) -> float:
        return round(self._current_drawdown_pct, 4)

    @property
    def daily_loss_pct(self) -> float:
        return round(self._daily_loss_pct, 4)

    @property
    def daily_loss_limit_pct(self) -> float:
        return self._daily_loss_limit_pct

    @property
    def equity_at_day_start(self) -> float:
        return self._equity_at_day_start

    def _compute_dynamic_daily_limit(self, atr_pct: float = 0.0) -> float:
        base = self._daily_loss_limit_pct
        if atr_pct <= 0:
            return base
        if atr_pct > 2.0:
            adjusted = min(base * 1.5, base + 1.5)
        elif atr_pct < 0.5:
            adjusted = max(base * 0.7, base - 1.0)
        else:
            adjusted = base
        log.debug("Dynamic daily limit: %.2f%% (atr_pct=%.2f%%)", adjusted, atr_pct)
        return round(adjusted, 2)

    def _compute_low_balance_threshold(self) -> float:
        slot_based = self._min_order_value_usdt * self._max_open_positions
        risk_based = self._initial_equity * (self._risk_per_trade_pct / 100) * 3
        raw = max(slot_based, risk_based)

        if self._initial_equity > 0:
            cap = self._initial_equity * 0.5
            if raw > cap:
                log.debug(
                    "LOW_BALANCE threshold di-cap: %.4f → %.4f (50%% dari initial_equity %.4f)",
                    raw, cap, self._initial_equity,
                )
                return cap
        return raw

    def update_portfolio_state(
        self,
        equity:               float,
        initial_equity:       float,
        free_balance:         float,
        open_positions_count: int,
        atr_pct:              float = 0.0,
    ) -> None:
        today = _utcnow().date()

        if today != self._daily_reset_date:
            log.info(
                "New trading day. Previous-day loss: %.4f%%", self._daily_loss_pct
            )
            self._daily_reset_date    = today
            self._equity_at_day_start = equity
            self._daily_loss_pct      = 0.0
            self.reset_symbol_halts()

            if self._halted and self._halt_reason == HaltReason.DAILY_LOSS:
                self._resume()
                log.info("Auto-resumed after daily loss reset.")

        if self._equity_at_day_start == 0.0 and equity > 0:
            self._equity_at_day_start = equity
            self._daily_reset_date    = today
            log.info("equity_at_day_start inisialisasi: %.4f", equity)

        self._current_equity       = equity
        self._initial_equity       = initial_equity
        self._free_balance         = free_balance
        self._open_positions_count = open_positions_count

        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity > 0:
            self._current_drawdown_pct = (
                (self._peak_equity - equity) / self._peak_equity * 100
            )

        if self._equity_at_day_start > 0:
            raw = (
                (self._equity_at_day_start - equity)
                / self._equity_at_day_start * 100
            )
            self._daily_loss_pct = max(0.0, raw)

        if self._current_drawdown_pct >= self._max_drawdown_pct and not self._halted:
            self.halt_trading(
                HaltReason.MAX_DRAWDOWN,
                f"drawdown {self._current_drawdown_pct:.3f}% >= limit {self._max_drawdown_pct}%",
            )

        self._dynamic_daily_limit = self._compute_dynamic_daily_limit(atr_pct)
        if self._daily_loss_pct >= self._dynamic_daily_limit and not self._halted:
            self.halt_trading(
                HaltReason.DAILY_LOSS,
                f"daily loss {self._daily_loss_pct:.3f}% >= limit "
                f"{self._daily_loss_limit_pct}%. Auto-resumes at UTC midnight.",
            )

        low_balance_threshold = self._compute_low_balance_threshold()
        if (
            low_balance_threshold > 0
            and self._free_balance < low_balance_threshold
            and not self._halted
        ):
            self.halt_trading(
                HaltReason.LOW_BALANCE,
                f"free balance ${self._free_balance:.2f} < ambang otomatis "
                f"${low_balance_threshold:.2f}. Auto-resume jika saldo naik "
                f"di atas ${low_balance_threshold * 1.1:.2f}.",
            )
        elif (
            self._halted
            and self._halt_reason == HaltReason.LOW_BALANCE
            and self._free_balance >= low_balance_threshold * 1.1
        ):
            self._resume()
            log.info(
                "Auto-resumed after LOW_BALANCE: free_balance $%.2f >= "
                "threshold*1.1 $%.2f",
                self._free_balance, low_balance_threshold * 1.1,
            )

    def halt_trading(
        self, reason: HaltReason = HaltReason.MANUAL, detail: str = ""
    ) -> None:
        self._halted      = True
        self._halt_reason = reason
        self._halt_detail = detail
        log.critical("TRADING HALTED [%s]: %s", reason.value, detail)
        self._persist_halt()

    def resume_trading(self) -> None:
        if self._halt_reason in (HaltReason.MAX_DRAWDOWN, HaltReason.PANIC_BUTTON):
            log.warning(
                "Cannot resume from %s via API. Manual review required.",
                self._halt_reason.value,
            )
            return
        self._resume()

    def _resume(self) -> None:
        self._halted      = False
        self._halt_reason = HaltReason.NONE
        self._halt_detail = ""
        log.info("Trading resumed.")
        self._clear_halt_persist()

    def _persist_halt(self) -> None:
        if self._db is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._db.set_bot_state(
                "halt_state",
                f"{self._halt_reason.value}|||{self._halt_detail}"
            ))
        except RuntimeError:
            pass

    def _clear_halt_persist(self) -> None:
        if self._db is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._db.clear_bot_state("halt_state"))
        except RuntimeError:
            pass

    def record_symbol_loss(self, symbol: str, pnl: float) -> None:
        if pnl >= 0:
            return
        loss_pct = abs(pnl) / max(self._current_equity, 1) * 100
        self._symbol_loss[symbol] = self._symbol_loss.get(symbol, 0) + loss_pct
        if self._symbol_loss[symbol] >= self._max_loss_per_symbol:
            self._symbol_halt[symbol] = True
            log.warning(
                "SYMBOL HALT: %s — cumulative loss %.2f%% >= limit %.2f%%",
                symbol, self._symbol_loss[symbol], self._max_loss_per_symbol,
            )

    def is_symbol_halted(self, symbol: str) -> bool:
        return self._symbol_halt.get(symbol, False)

    def reset_symbol_halts(self) -> None:
        self._symbol_loss.clear()
        self._symbol_halt.clear()
        log.info("Symbol-level halts reset untuk hari baru.")

    async def evaluate_order(self, *args, **kwargs) -> RiskAssessment:
        raise NotImplementedError(
            "evaluate_order() WAJIB diimplementasikan di subclass -- "
            "semantik 'side' berbeda total antara spot (sell=selalu tutup) "
            "dan futures (sell=bisa buka short ATAU tutup long)."
        )

    def _compute_position_size(self, *args, **kwargs):
        raise NotImplementedError(
            "_compute_position_size() WAJIB diimplementasikan di subclass -- "
            "futures perlu leverage + liquidation-safety check yang tidak "
            "berlaku di spot."
        )

    def _compute_sl_tp(self, *args, **kwargs):
        raise NotImplementedError(
            "_compute_sl_tp() WAJIB diimplementasikan di subclass."
        )

    def check_breakeven_sl(
        self,
        entry_price:   float,
        current_price: float,
        current_sl:    Optional[float],
        take_profit:   Optional[float],
        side:          str = "long",
    ) -> Optional[float]:
        if not all([entry_price, current_sl, take_profit]):
            return None
        if entry_price <= 0:
            return None

        if side == "long":
            risk   = entry_price - current_sl
            reward = take_profit - entry_price
            if risk <= 0 or reward <= 0:
                return None
            trigger = entry_price + risk
            if current_price >= trigger and current_sl < entry_price:
                log.info(
                    "Breakeven SL: %s price=%.6f >= trigger=%.6f | SL %.6f → %.6f",
                    side, current_price, trigger, current_sl, entry_price,
                )
                return entry_price

        elif side == "short":
            risk   = current_sl - entry_price
            reward = entry_price - take_profit
            if risk <= 0 or reward <= 0:
                return None
            trigger = entry_price - risk
            if current_price <= trigger and current_sl > entry_price:
                return entry_price

        return None

    def check_trailing_sl(
        self,
        entry_price:   float,
        current_price: float,
        current_sl:    float,
        atr:           float,
        side:          str = "long",
        strategy_profile: str = "",
    ) -> Optional[float]:
        if not self._use_trailing_stop:
            return None
        if atr <= 0 or current_sl is None:
            return None

        _FIXED_PROFILES = {"trend_follow", "hodl_accumulate"}
        use_progressive = strategy_profile.lower() not in _FIXED_PROFILES

        if use_progressive and entry_price > 0:
            profit_pct = ((current_price - entry_price) / entry_price * 100) if side == "long" \
                else ((entry_price - current_price) / entry_price * 100)
            if profit_pct >= 30:
                mult = self._trailing_atr_mult * 0.7
            elif profit_pct >= 10:
                mult = self._trailing_atr_mult * 0.85
            else:
                mult = self._trailing_atr_mult
            log.debug(
                "Progressive trailing | profile=%s profit=%.1f%% mult=%.2f",
                strategy_profile, profit_pct, mult,
            )
        else:
            mult = self._trailing_atr_mult

        trail_dist = atr * mult

        if side == "long":
            if current_sl < entry_price:
                return None
            new_sl = current_price - trail_dist
            if new_sl > current_sl:
                log.debug(
                    "Trailing SL (long): %.6f → %.6f (price=%.6f trail=%.6f)",
                    current_sl, new_sl, current_price, trail_dist,
                )
                return round(new_sl, 8)

        elif side == "short":
            if current_sl > entry_price:
                return None
            new_sl = current_price + trail_dist
            if new_sl < current_sl:
                return round(new_sl, 8)

        return None

    @staticmethod
    def compute_sharpe_ratio(
        pnl_list:         List[float],
        risk_free_rate:   float = 0.0,
        periods_per_year: int   = 365,
    ) -> float:
        if len(pnl_list) < 2:
            return 0.0
        arr    = np.array(pnl_list, dtype=float)
        excess = arr - risk_free_rate / periods_per_year
        std    = excess.std(ddof=1)
        return float(np.sqrt(periods_per_year) * excess.mean() / std) if std > 0 else 0.0

    @staticmethod
    def compute_sortino_ratio(
        pnl_list:         List[float],
        risk_free_rate:   float = 0.0,
        periods_per_year: int   = 365,
    ) -> float:
        if len(pnl_list) < 2:
            return 0.0
        arr      = np.array(pnl_list, dtype=float)
        excess   = arr - risk_free_rate / periods_per_year
        downside = excess[excess < 0]
        if len(downside) < 2:
            return 0.0
        dstd = downside.std(ddof=1)
        return float(np.sqrt(periods_per_year) * excess.mean() / dstd) if dstd > 0 else 0.0

    @staticmethod
    def compute_max_drawdown(equity_curve: List[float]) -> float:
        eq_clean = [
            v for v in equity_curve
            if v is not None
            and isinstance(v, (int, float))
            and not math.isnan(v)
            and not math.isinf(v)
            and v > 0
        ]
        if len(eq_clean) < 2:
            return 0.0
        eq    = np.array(eq_clean, dtype=float)
        peaks = np.maximum.accumulate(eq)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = np.where(peaks > 0, (peaks - eq) / peaks * 100, 0.0)
        result = float(dd.max())
        return result if not math.isnan(result) else 0.0

    @staticmethod
    def compute_calmar_ratio(
        annualized_return_pct: float, max_drawdown_pct: float
    ) -> float:
        return (
            annualized_return_pct / max_drawdown_pct
            if max_drawdown_pct > 0 else 0.0
        )

    @staticmethod
    def compute_profit_factor(pnl_list: List[float]) -> float:
        if not pnl_list:
            return 0.0
        gross_profit = sum(p for p in pnl_list if p > 0)
        gross_loss   = abs(sum(p for p in pnl_list if p < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 4)

    @staticmethod
    def compute_win_rate(pnl_list: List[float]) -> float:
        if not pnl_list:
            return 0.0
        return sum(1 for p in pnl_list if p > 0) / len(pnl_list) * 100

    @staticmethod
    def compute_expectancy(pnl_list: List[float]) -> float:
        if not pnl_list:
            return 0.0
        return sum(pnl_list) / len(pnl_list)

    @staticmethod
    def compute_avg_win_loss_ratio(pnl_list: List[float]) -> float:
        wins   = [p for p in pnl_list if p > 0]
        losses = [abs(p) for p in pnl_list if p < 0]
        if not wins or not losses:
            return 0.0
        return (sum(wins) / len(wins)) / (sum(losses) / len(losses))

    def get_system_health(self) -> Dict:
        return {
            "is_halted":             self._halted,
            "halt_reason":           self.halt_reason,
            "halt_reason_code":      self._halt_reason.value,
            "current_drawdown_pct":  round(self._current_drawdown_pct, 4),
            "max_drawdown_pct":      self._max_drawdown_pct,
            "daily_loss_pct":        round(self._daily_loss_pct, 4),
            "daily_loss_limit_pct":  self._daily_loss_limit_pct,
            "open_positions":        self._open_positions_count,
            "max_open_positions":    self._max_open_positions,
            "current_equity":        round(self._current_equity, 4),
            "initial_equity":        round(self._initial_equity, 4),
            "free_balance":          round(self._free_balance, 4),
            "peak_equity":           round(self._peak_equity, 4),
            "equity_at_day_start":   round(self._equity_at_day_start, 4),
            "risk_per_trade_pct":    self._risk_per_trade_pct,
            "trailing_stop_enabled": self._use_trailing_stop,
            "trailing_atr_mult":     self._trailing_atr_mult,
            "low_balance_threshold": round(self._compute_low_balance_threshold(), 4),
        }
