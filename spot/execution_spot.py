"""
execution.py (spot) — OrderExecutionManager khusus spot trading

Extend engine.execution_base.BaseOrderExecutionManager yang menangani semua
mekanika eksekusi generik (slippage, market/limit/iceberg, poll fill,
verifikasi fill). Di sini cuma tersisa pemetaan SignalType->side spot-specific.
"""

from __future__ import annotations


from engine.execution_base import BaseOrderExecutionManager
from engine.core.models import SignalEvent, SignalType


class OrderExecutionManager(BaseOrderExecutionManager):

    def _map_signal_to_side(self, signal: SignalEvent) -> str:
        """
        [FUTURES-READY] Pemetaan SignalType -> aksi order exchange. Mekanika
        "buy"/"sell" di level exchange itu sendiri sudah reusable utk long &
        short -- yang beda cuma pemetaan ini.

        BUY         -> "buy"  (buka long)
        CLOSE_LONG  -> "sell" (tutup long)
        CLOSE_SHORT -> "buy"  (tutup short / buy-to-cover) -- spot tidak
                       pernah menghasilkan sinyal ini secara nyata sampai
                       saat ini, tapi pemetaan tetap benar kalau suatu saat
                       dipakai (mis. lewat cross-learn dari future/).
        SELL        -> "sell" (fallback)
        """
        if signal.signal_type == SignalType.BUY:
            return "buy"
        elif signal.signal_type == SignalType.CLOSE_SHORT:
            return "buy"
        else:
            return "sell"

    # _extra_trade_fields() tidak di-override -- default dari base (dict
    # kosong) sudah benar untuk spot, tidak ada field tambahan yang perlu
    # disisipkan ke trade_data.
