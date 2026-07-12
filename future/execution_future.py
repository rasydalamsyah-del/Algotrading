"""
future/execution_future.py — OrderExecutionManager khusus Binance USDT-M Futures

Extend engine.execution_base.BaseOrderExecutionManager. Di sini cuma tersisa:
- _map_signal_to_side(): pemetaan SignalType->side, BEDA dari spot karena
  perlu handle OPEN_SHORT (buka short baru).
- _extra_trade_fields(): sisipkan leverage/margin_mode/liquidation_price ke
  trade_data, diambil dari order['info'] (hasil FutureExchangeConnector
  ._simulate_order_fill(), lihat future/exchange_future.py).
"""

from __future__ import annotations

from typing import Dict

from engine.execution_base import BaseOrderExecutionManager
from engine.risk_base import RiskAssessment
from engine.core.models import SignalEvent, SignalType


class OrderExecutionManager(BaseOrderExecutionManager):

    def _map_signal_to_side(self, signal: SignalEvent) -> str:
        """
        [FUTURES-SPECIFIC] Pemetaan lengkap termasuk OPEN_SHORT (tidak ada
        di spot). Mekanika "buy"/"sell" tetap reusable dari base.

        BUY         -> "buy"  (buka long)
        OPEN_SHORT  -> "sell" (buka short baru)
        CLOSE_LONG  -> "sell" (tutup long)
        CLOSE_SHORT -> "buy"  (tutup short / buy-to-cover)
        SELL        -> "sell" (fallback, belum ada makna eksplisit)
        """
        if signal.signal_type in (SignalType.BUY, SignalType.CLOSE_SHORT):
            return "buy"
        else:
            # OPEN_SHORT, CLOSE_LONG, SELL, atau fallback lain -> "sell"
            return "sell"

    def _extra_trade_fields(
        self, order: dict, signal: SignalEvent, assessment: RiskAssessment
    ) -> Dict:
        """
        [FUTURES-SPECIFIC] Sisipkan leverage/margin_mode/liquidation_price ke
        trade_data. Sumber utama: assessment (diisi eksplisit oleh
        risk_future.py::evaluate_order() -- lihat field leverage/margin_mode/
        liquidation_price yang ditambahkan ke RiskAssessment di engine/risk_base.py
        khusus untuk kebutuhan ini). Fallback ke order['info'] (diisi
        FutureExchangeConnector._simulate_order_fill()) kalau assessment
        tidak mengisinya untuk alasan tertentu.
        """
        info = order.get("info", {}) or {}

        leverage = assessment.leverage
        margin_mode = assessment.margin_mode
        liquidation_price = assessment.liquidation_price
        if liquidation_price is None:
            liquidation_price = info.get("liquidation_price")

        return {
            "market_type":      "futures",
            "leverage":         leverage,
            "margin_mode":      margin_mode,
            "realized_funding": None,  # diisi terpisah oleh proses funding settlement,
                                        # bukan pada saat fill order (lihat future/funding.py)
        }
