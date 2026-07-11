"""
future/strategy_future.py — VolumetricBreakoutStrategy khusus futures

Extend engine.strategy_base.VolumetricBreakoutStrategyBase (scoring/tracking
pipeline market-agnostic, side-aware). Dibangun untuk menutup gap
independensi: sebelumnya future/main_future.py mengimpor langsung dari
spot/strategy_spot.py (get_strategy, PositionTracker) -- pelanggaran
arsitektur yang ditemukan lewat pertanyaan user, sekarang diperbaiki dengan
pola yang sama seperti Exchange/Risk/Execution.

generate_signals() WAJIB diimplementasikan (abstract method di BaseStrategy),
TAPI future/main_future.py TIDAK PERNAH memanggilnya -- pipeline futures
sepenuhnya lewat run_gate3_worker() yang memanggil get_scored_signal()
langsung (bidirectional, side-aware). Method ini sengaja raise
NotImplementedError dgn pesan jelas, BUKAN diam-diam return [] (supaya
kalau ada kode lain di masa depan yang keliru memanggilnya, errornya
eksplisit, bukan silent no-op yang menyembunyikan bug).
"""

from __future__ import annotations

import logging
from typing import Dict, List

from engine.strategy_base import (
    VolumetricBreakoutStrategyBase, BaseStrategy, PositionTracker,
    SignalType, SignalEvent, ExitMode,
)

log = logging.getLogger("strategy_future")


class VolumetricBreakoutStrategy(VolumetricBreakoutStrategyBase):

    async def generate_signals(
        self, symbol: str, df
    ) -> List[SignalEvent]:
        raise NotImplementedError(
            "generate_signals() tidak dipakai di pipeline futures -- "
            "future/main_future.py::run_gate3_worker() memanggil "
            "get_scored_signal() langsung (bidirectional, side-aware), "
            "tidak pernah lewat generate_signals(). Kalau kode ini "
            "terpanggil, kemungkinan ada caller baru yang keliru "
            "mengasumsikan pipeline 'legacy' spot tersedia di futures."
        )


_REGISTRY: Dict[str, type] = {
    "volumetric_breakout": VolumetricBreakoutStrategy,
}


def get_strategy(
    name:      str,
    symbols:   List[str],
    timeframe: str,
    params:    Dict = None,
) -> BaseStrategy:
    if params is None:
        params = {}
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Strategy '{name}' tidak dikenal. Tersedia: {list(_REGISTRY)}")
    return cls(symbols=symbols, timeframe=timeframe, params=params)


def list_strategies() -> List[str]:
    return list(_REGISTRY.keys())
