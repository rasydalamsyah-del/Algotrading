"""
engine/reentry_cooldown.py -- Re-entry cooldown per simbol (opsi B).

Kejadian nyata yang dicegah (2026-07-20): PARTI/USDT ditutup ATG_EXIT
(sinyal bearish) lalu dibuka LAGI 2 detik kemudian di harga sama, ditutup
lagi 7 detik kemudian dengan alasan bearish yang sama -- exit dan entry
saling membatalkan, membakar fee+slippage. MANTRA/USDT: trailing-exit
profit lalu re-entry 1 menit kemudian di harga puncak, kena SL.

Kebijakan (opsi B): HANYA exit NEGATIF yang memicu cooldown --
  - realized_pnl < 0 (loss apa pun), ATAU
  - reason mengandung pola stop-loss / ATG_EXIT / liquidation.
Exit positif (TP, trailing profit) TIDAK memicu cooldown: momentum
re-entry yang sah tetap diizinkan.

Durasi = 1x timeframe profile koin (PROFILE_TIMEFRAME -> TIMEFRAME_SECONDS),
fallback DEFAULT_COOLDOWN_SECS kalau profile tidak dikenal/kosong.

State in-memory per proses (spot & futures proses terpisah = registry
terpisah). Restart menghapus cooldown -- keputusan sadar: konsisten dgn
peak_equity yang juga in-memory, tanpa perubahan skema DB.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Dict, Optional

log = logging.getLogger("engine.reentry_cooldown")

try:
    from engine.profiles.registry import PROFILE_TIMEFRAME  # type: ignore
except Exception:  # pragma: no cover
    PROFILE_TIMEFRAME: Dict[str, str] = {}

try:
    from engine.constants import TIMEFRAME_SECONDS  # type: ignore
except Exception:  # pragma: no cover
    TIMEFRAME_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
    }

DEFAULT_COOLDOWN_SECS = 900  # 15m -- timeframe scalp, profil paling umum

_NEGATIVE_PATTERNS = re.compile(
    r"stop[\s_-]?loss|\bSL\b|ATG_EXIT|liquidat", re.IGNORECASE,
)


# [COOLDOWN-FIX -- kasus AIGENSYN 2026-07-21 00:07] Exit "Stop-loss hit"
# dengan PnL +0.42 (trailing/breakeven SL yang sudah NAIK di atas entry lalu
# tersentuh) sebelumnya diklasifikasi negatif oleh pattern reason -> kena
# cooldown 900s padahal secara fungsional itu trailing-exit profit. Ambang
# profit "berarti" utk membedakan dari breakeven-tipis (yang tetap layak
# cooldown krn sinyal memang memburuk sebelum sempat profit).
PROFIT_EXIT_EXEMPT_PNL = 0.10  # > $0.10 profit = exit menang, bukan negatif


def is_negative_exit(reason: Optional[str], realized_pnl: Optional[float]) -> bool:
    """True kalau exit ini harus memicu cooldown (loss ATAU exit-karena-
    sinyal-buruk). Prioritas PnL dua arah: loss jelas = cooldown; profit
    berarti (> PROFIT_EXIT_EXEMPT_PNL) = BUKAN negatif walau reason
    berbunyi "Stop-loss hit" (trailing-SL-in-profit). Pattern reason hanya
    menentukan utk zona abu-abu (PnL None atau ~0)."""
    if realized_pnl is not None and realized_pnl < 0:
        return True
    if realized_pnl is not None and realized_pnl > PROFIT_EXIT_EXEMPT_PNL:
        return False
    if reason and _NEGATIVE_PATTERNS.search(reason):
        return True
    return False


def duration_for_profile(profile_name: Optional[str]) -> int:
    """1x timeframe profile dalam detik; fallback DEFAULT_COOLDOWN_SECS."""
    tf = PROFILE_TIMEFRAME.get(str(profile_name or "").strip())
    if not tf:
        return DEFAULT_COOLDOWN_SECS
    return int(TIMEFRAME_SECONDS.get(tf, DEFAULT_COOLDOWN_SECS))


class CooldownRegistry:
    def __init__(self) -> None:
        self._until: Dict[str, float] = {}
        self._reason: Dict[str, str] = {}

    def register(self, symbol: str, seconds: float, reason: str = "") -> None:
        if seconds <= 0:
            return
        self._until[symbol] = time.monotonic() + float(seconds)
        self._reason[symbol] = reason or ""
        log.info(
            "[ReentryCooldown] %s diblokir %.0fs (exit negatif: %s)",
            symbol, seconds, (reason or "-")[:120],
        )

    def blocked(self, symbol: str) -> float:
        """Sisa detik cooldown; 0.0 kalau bebas. Entri kadaluarsa dibersihkan."""
        deadline = self._until.get(symbol)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._until.pop(symbol, None)
            self._reason.pop(symbol, None)
            return 0.0
        return remaining


# Singleton per proses.
registry = CooldownRegistry()
