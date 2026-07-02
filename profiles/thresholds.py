"""
profiles/thresholds.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from profiles.base_profile import (
    CoinProfile,
    ParameterBounds,
    PrimaryTriggerType,
    StrategyProfile,
)
from constants import (
    SCORE_NEUTRAL,
    KELLY_MIN_SAMPLE,
    KELLY_MAX_SIZE_PCT,
    KELLY_MIN_SIZE_PCT,
    SPREAD_LIMIT_DEFAULT,
    META_PARAM_BOUNDS,
)

log = logging.getLogger("profiles.thresholds")

ENTRY_THRESHOLDS: Dict[str, float] = {
    "hodl_accumulate":  65.0,  # fallback regime undefined
    "trend_follow":     65.0,  # fallback regime undefined
    "breakout_swift":   62.0,  # fallback regime undefined
    "scalp_volatile":   61.0,  # fallback regime undefined
    "mean_revert":      59.0,  # fallback regime undefined
    "extreme_momentum": 68.0,  # fallback regime undefined
}

# Dynamic threshold matrix: profile × regime
# Threshold berubah otomatis sesuai kombinasi profil dan kondisi pasar
DYNAMIC_THRESHOLD_MATRIX: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "trending_bull":      52.0,  # Trending = sweet spot hodl
        "trending_bear":     999.0,  # Tidak trade saat bear
        "ranging":            68.0,  # Hodl di ranging = kurang ideal
        "volatile_expansion": 72.0,  # Hodl di volatile = berisiko
        "undefined":          65.0,
    },
    "trend_follow": {
        "trending_bull":      58.0,  # Trend follower — butuh sinyal di atas netral
        "trending_bear":     999.0,
        "ranging":            75.0,  # Skeptis saat ranging
        "volatile_expansion": 68.0,  # Volatile tidak cocok untuk trend follower
        "undefined":          65.0,
    },
    "breakout_swift": {
        "trending_bull":      55.0,  # Breakout butuh konfirmasi di atas netral
        "trending_bear":     999.0,
        "ranging":            70.0,  # Breakout di ranging sering palsu
        "volatile_expansion": 52.0,  # Sweet spot breakout — tidak boleh di bawah netral
        "undefined":          62.0,
    },
    "scalp_volatile": {
        "trending_bull":      63.0,  # Scalp oke saat trending
        "trending_bear":     999.0,
        "ranging":            65.0,  # Scalp di ranging butuh konfirmasi
        "volatile_expansion": 52.0,  # Sweet spot scalp — tidak boleh di bawah netral
        "undefined":          61.0,
    },
    "mean_revert": {
        "trending_bull":      70.0,  # Mean revert saat trending = melawan arus
        "trending_bear":     999.0,
        "ranging":            50.0,  # Gold Mine — ranging = ideal mean revert
        "volatile_expansion": 68.0,  # Volatile sangat berbahaya untuk mean revert
        "undefined":          59.0,
    },
    "extreme_momentum": {
        "trending_bull":      55.0,  # Momentum kuat saat trending
        "trending_bear":     999.0,
        "ranging":            80.0,  # Extreme momentum di ranging = bahaya
        "volatile_expansion": 48.0,  # Sweet spot agresif — sedikit di bawah netral ok
        "undefined":          68.0,
    },
}

def get_dynamic_threshold(profile_name: str, regime_value: str) -> float:
    """Ambil threshold dinamis berdasarkan kombinasi profil dan regime."""
    matrix = DYNAMIC_THRESHOLD_MATRIX.get(profile_name, {})
    return matrix.get(regime_value, ENTRY_THRESHOLDS.get(profile_name, 65.0))

# Transition Action Matrix: profile × entry_regime × current_regime
# Aksi: HOLD | HOLD_TIGHTEN_SL | HOLD_RELAX_SL | EXIT | NO_POSITION
TRANSITION_ACTION_MATRIX: Dict[str, Dict[str, Dict[str, str]]] = {
    "hodl_accumulate": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
    },
    "trend_follow": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
    },
    "breakout_swift": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
    },
    "scalp_volatile": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
    },
    "mean_revert": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
    },
    "extreme_momentum": {
        "trending_bull":      {"trending_bull": "HOLD", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "ranging":            {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD_TIGHTEN_SL", "undefined": "HOLD"},
        "volatile_expansion": {"trending_bull": "HOLD_RELAX_SL", "ranging": "HOLD_TIGHTEN_SL", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
        "undefined":          {"trending_bull": "HOLD", "ranging": "HOLD", "trending_bear": "EXIT", "volatile_expansion": "HOLD", "undefined": "HOLD"},
    },
}

def get_transition_action(profile_name: str, entry_regime: str, current_regime: str) -> str:
    """Ambil aksi transisi berdasarkan profile x entry_regime x current_regime.
    Return: HOLD | HOLD_TIGHTEN_SL | HOLD_RELAX_SL | EXIT

    # [BUG-FIX] Sebelumnya: docstring menyebut 'NO_POSITION' sebagai salah satu
    # kemungkinan return value. Tapi tidak ada satupun entry di
    # TRANSITION_ACTION_MATRIX yang mengandung 'NO_POSITION' — matrix hanya
    # berisi: HOLD, HOLD_TIGHTEN_SL, HOLD_RELAX_SL, EXIT. Docstring yang
    # tidak akurat ini bisa menyesatkan caller yang menyiapkan handler untuk
    # 'NO_POSITION' yang tidak pernah datang. Dihapus dari docstring.
    """
    if current_regime == "trending_bear":
        return "EXIT"
    profile_matrix = TRANSITION_ACTION_MATRIX.get(profile_name, {})
    entry_map = profile_matrix.get(entry_regime, {})
    return entry_map.get(current_regime, "HOLD")

VOLUME_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "volume_mult":  1.5,
        "volume_spike": 3.0,
    },
    "trend_follow": {
        "volume_mult":  1.8,
        "volume_spike": 2.5,
    },
    "breakout_swift": {
        "volume_mult":  2.0,
        "volume_spike": 3.0,
    },
    "scalp_volatile": {
        "volume_mult":  2.0,
        "volume_spike": 2.5,
    },
    "mean_revert": {
        "volume_mult":  1.2,
        "volume_spike": 2.0,
    },
    "extreme_momentum": {
        "volume_mult":  4.0,
        "volume_spike": 6.0,
    },
}

RSI_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "rsi_min":      40.0,
        "rsi_max":      65.0,
        "rsi_gc_min":   42.0,
    },
    "trend_follow": {
        "rsi_min":      48.0,
        "rsi_max":      72.0,
        "rsi_gc_min":   48.0,
    },
    "breakout_swift": {
        "rsi_min":      50.0,
        "rsi_max":      73.0,
        "rsi_gc_min":   50.0,
    },
    "scalp_volatile": {
        "rsi_min":      52.0,
        "rsi_max":      68.0,
        "rsi_gc_min":   50.0,
    },
    "mean_revert": {
        "rsi_min":      25.0,
        "rsi_max":      55.0,
        "rsi_gc_min":   38.0,
    },
    "extreme_momentum": {
        "rsi_min":      60.0,
        "rsi_max":      82.0,
        "rsi_gc_min":   58.0,
    },
}

BREAKOUT_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "min_breakout_pct": 0.30,
    },
    "trend_follow": {
        "min_breakout_pct": 0.20,
    },
    "breakout_swift": {
        "min_breakout_pct": 0.15,
    },
    "scalp_volatile": {
        "min_breakout_pct": 0.12,
    },
    "mean_revert": {
        "min_breakout_pct": 0.10,
    },
    "extreme_momentum": {
        "min_breakout_pct": 0.50,
    },
}

ATR_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "atr_sl_mult":       3.0,
        "atr_tp_mult":       8.0,
        "atr_pct_threshold": 1.5,
    },
    "trend_follow": {
        "atr_sl_mult":       2.5,
        "atr_tp_mult":       6.0,
        "atr_pct_threshold": 0.7,
    },
    "breakout_swift": {
        "atr_sl_mult":       2.0,
        "atr_tp_mult":       4.0,
        "atr_pct_threshold": 0.6,
    },
    "scalp_volatile": {
        "atr_sl_mult":       2.2,
        "atr_tp_mult":       3.5,
        "atr_pct_threshold": 0.4,
    },
    "mean_revert": {
        "atr_sl_mult":       2.5,
        "atr_tp_mult":       3.5,
        "atr_pct_threshold": 0.8,
    },
    "extreme_momentum": {
        "atr_sl_mult":       1.8,
        "atr_tp_mult":       4.0,
        "atr_pct_threshold": 0.3,
    },
}

QUICK_SLTP_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "quick_sl_pct": 5.0,
        "quick_tp_pct": 15.0,
    },
    "trend_follow": {
        "quick_sl_pct": 3.0,
        "quick_tp_pct": 7.0,
    },
    "breakout_swift": {
        "quick_sl_pct": 2.0,
        "quick_tp_pct": 4.0,
    },
    "scalp_volatile": {
        "quick_sl_pct": 2.0,
        "quick_tp_pct": 3.0,
    },
    "mean_revert": {
        "quick_sl_pct": 2.5,
        "quick_tp_pct": 3.5,
    },
    "extreme_momentum": {
        "quick_sl_pct": 1.5,
        "quick_tp_pct": 4.0,
    },
}

TRAILING_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "trailing_act_pct": 5.0,
        "trailing_gap_pct": 2.5,
    },
    "trend_follow": {
        "trailing_act_pct": 3.0,
        "trailing_gap_pct": 1.5,
    },
    "breakout_swift": {
        "trailing_act_pct": 2.5,
        "trailing_gap_pct": 1.0,
    },
    "scalp_volatile": {
        "trailing_act_pct": 1.8,
        "trailing_gap_pct": 0.7,
    },
    "mean_revert": {
        "trailing_act_pct": 2.0,
        "trailing_gap_pct": 1.0,
    },
    "extreme_momentum": {
        "trailing_act_pct": 3.0,
        "trailing_gap_pct": 1.2,
    },
}

MAX_HOLD_CANDLES: Dict[str, int] = {
    "hodl_accumulate":  0,
    "trend_follow":     100,
    "breakout_swift":   48,
    "scalp_volatile":   24,
    "mean_revert":      60,
    "extreme_momentum": 8,
}

VOLATILITY_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "typical_atr_pct": 2.5,
        "max_spread_pct":  0.20,
    },
    "trend_follow": {
        "typical_atr_pct": 1.5,
        "max_spread_pct":  0.18,
    },
    "breakout_swift": {
        "typical_atr_pct": 0.8,
        "max_spread_pct":  0.15,
    },
    "scalp_volatile": {
        "typical_atr_pct": 0.5,
        "max_spread_pct":  0.12,
    },
    "mean_revert": {
        "typical_atr_pct": 1.0,
        "max_spread_pct":  0.15,
    },
    "extreme_momentum": {
        "typical_atr_pct": 1.5,
        "max_spread_pct":  0.25,
    },
}

MTF_PARAMS: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "confirmation_weight":    0.20,
        "confirmation_min_score": 40.0,
    },
    "trend_follow": {
        "confirmation_weight":    0.25,
        "confirmation_min_score": 45.0,
    },
    "breakout_swift": {
        "confirmation_weight":    0.25,
        "confirmation_min_score": 45.0,
    },
    "scalp_volatile": {
        "confirmation_weight":    0.20,
        "confirmation_min_score": 42.0,
    },
    "mean_revert": {
        "confirmation_weight":    0.15,
        "confirmation_min_score": 35.0,
    },
    "extreme_momentum": {
        "confirmation_weight":    0.15,
        "confirmation_min_score": 40.0,
    },
}

RISK_PARAMS: Dict[str, Dict[str, Any]] = {
    "hodl_accumulate": {
        "kelly_lookback_trades":     50,
        "kelly_enabled":             True,
        "max_consecutive_losses":    3,
        "consecutive_loss_size_mult": 0.5,
    },
    "trend_follow": {
        "kelly_lookback_trades":     50,
        "kelly_enabled":             True,
        "max_consecutive_losses":    3,
        "consecutive_loss_size_mult": 0.5,
    },
    "breakout_swift": {
        "kelly_lookback_trades":     50,
        "kelly_enabled":             True,
        "max_consecutive_losses":    3,
        "consecutive_loss_size_mult": 0.5,
    },
    "scalp_volatile": {
        "kelly_lookback_trades":     75,
        "kelly_enabled":             True,
        "max_consecutive_losses":    4,
        "consecutive_loss_size_mult": 0.6,
    },
    "mean_revert": {
        "kelly_lookback_trades":     50,
        "kelly_enabled":             True,
        "max_consecutive_losses":    3,
        "consecutive_loss_size_mult": 0.5,
    },
    "extreme_momentum": {
        "kelly_lookback_trades":     30,
        "kelly_enabled":             True,
        "max_consecutive_losses":    2,
        "consecutive_loss_size_mult": 0.3,
    },
}

ALLOWED_REGIMES: Dict[str, List[str]] = {
    "hodl_accumulate": [
        "trending_bull",
    ],
    "trend_follow": [
        "trending_bull",
    ],
    "breakout_swift": [
        "trending_bull",
        "ranging",
        "volatile_expansion",
    ],
    "scalp_volatile": [
        "trending_bull",
        "ranging",
        "volatile_expansion",
    ],
    "mean_revert": [
        "trending_bull",
        "ranging",
    ],
    "extreme_momentum": [
        "trending_bull",
        "volatile_expansion",
    ],
}

PARAMETER_BOUNDS_OVERRIDE: Dict[str, Dict[str, Tuple[float, float]]] = {
    "hodl_accumulate": {
        "entry_threshold":  (50.0, 80.0),
        "quick_sl_pct":     (3.0, 8.0),
        "quick_tp_pct":     (8.0, 25.0),
        "rsi_min":          (30.0, 55.0),
        "rsi_max":          (55.0, 80.0),
        "trailing_act_pct": (3.0, 8.0),
    },
    "trend_follow": {
        "entry_threshold":  (52.0, 82.0),
        "quick_sl_pct":     (2.0, 5.0),
        "quick_tp_pct":     (5.0, 15.0),
        "rsi_min":          (40.0, 58.0),
        "rsi_max":          (60.0, 82.0),
    },
    "breakout_swift": {
        "entry_threshold":  (54.0, 82.0),
        "quick_sl_pct":     (1.5, 4.0),
        "quick_tp_pct":     (3.0, 10.0),
        "volume_multiplier": (1.5, 4.0),
        "rsi_min":          (44.0, 60.0),
        "rsi_max":          (62.0, 82.0),
    },
    "scalp_volatile": {
        "entry_threshold":  (53.0, 82.0),
        "quick_sl_pct":     (1.2, 3.5),
        "quick_tp_pct":     (1.8, 6.0),
        "rsi_min":          (45.0, 62.0),
        "rsi_max":          (58.0, 78.0),
        "trailing_act_pct": (0.8, 3.0),
        "trailing_gap_pct": (0.3, 1.5),
    },
    "mean_revert": {
        "entry_threshold":  (48.0, 78.0),
        "quick_sl_pct":     (1.5, 4.0),
        "quick_tp_pct":     (2.5, 7.0),
        "rsi_min":          (20.0, 45.0),
        "rsi_max":          (45.0, 65.0),
        "volume_multiplier": (0.8, 2.5),
    },
    "extreme_momentum": {
        "entry_threshold":  (60.0, 88.0),
        "quick_sl_pct":     (1.0, 2.5),
        "quick_tp_pct":     (3.0, 8.0),
        "volume_multiplier": (3.0, 6.0),
        "rsi_min":          (55.0, 72.0),
        "rsi_max":          (72.0, 90.0),
        "trailing_act_pct": (2.0, 5.0),
        "trailing_gap_pct": (0.8, 2.0),
    },
}

@dataclass
class ProfileThreshold:

    profile_name: str
    entry_threshold: float
    volume_mult:   float
    volume_spike:  float
    rsi_min:     float
    rsi_max:     float
    rsi_gc_min:  float
    min_breakout_pct: float
    atr_sl_mult:       float
    atr_tp_mult:       float
    atr_pct_threshold: float
    quick_sl_pct: float
    quick_tp_pct: float
    trailing_act_pct: float
    trailing_gap_pct: float
    max_hold_candles: int
    typical_atr_pct: float
    max_spread_pct:  float
    confirmation_weight:    float
    confirmation_min_score: float
    kelly_lookback_trades:      int
    kelly_enabled:              bool
    max_consecutive_losses:     int
    consecutive_loss_size_mult: float
    allowed_regimes: List[str]
    param_bounds: ParameterBounds
    primary_trigger_type: PrimaryTriggerType
    meta_learner_allowed: bool
    meta_learner_level1:  bool
    meta_learner_level2:  bool

    def validate(self) -> List[str]:
        errors: List[str] = []

        if self.rsi_min >= self.rsi_max:
            errors.append(
                f"[{self.profile_name}] rsi_min ({self.rsi_min}) >= rsi_max ({self.rsi_max})"
            )

        if self.quick_sl_pct >= self.quick_tp_pct:
            errors.append(
                f"[{self.profile_name}] quick_sl_pct ({self.quick_sl_pct}) "
                f">= quick_tp_pct ({self.quick_tp_pct})"
            )

        rr = self.atr_tp_mult / self.atr_sl_mult if self.atr_sl_mult > 0 else 0
        if rr < 1.3:
            errors.append(
                f"[{self.profile_name}] ATR R/R terlalu rendah: "
                f"{self.atr_tp_mult}/{self.atr_sl_mult} = {rr:.2f} (minimum 1.3)"
            )

        if self.volume_spike <= self.volume_mult:
            errors.append(
                f"[{self.profile_name}] volume_spike ({self.volume_spike}) "
                f"<= volume_mult ({self.volume_mult})"
            )

        if self.trailing_act_pct <= self.trailing_gap_pct:
            errors.append(
                f"[{self.profile_name}] trailing_act_pct ({self.trailing_act_pct}) "
                f"<= trailing_gap_pct ({self.trailing_gap_pct})"
            )

        if not (40.0 <= self.entry_threshold <= 95.0):
            errors.append(
                f"[{self.profile_name}] entry_threshold {self.entry_threshold} "
                f"di luar [40, 95]"
            )

        if not (0.0 <= self.confirmation_weight <= 1.0):
            errors.append(
                f"[{self.profile_name}] confirmation_weight {self.confirmation_weight} "
                f"di luar [0, 1]"
            )

        return errors

    def to_coinprofile_kwargs(self) -> Dict[str, Any]:
        return {
            "entry_threshold":            self.entry_threshold,
            "volume_mult":                self.volume_mult,
            "volume_spike":               self.volume_spike,
            "rsi_min":                    self.rsi_min,
            "rsi_max":                    self.rsi_max,
            "rsi_gc_min":                 self.rsi_gc_min,
            "min_breakout_pct":           self.min_breakout_pct,
            "atr_sl_mult":                self.atr_sl_mult,
            "atr_tp_mult":                self.atr_tp_mult,
            "atr_pct_threshold":          self.atr_pct_threshold,
            "quick_sl_pct":               self.quick_sl_pct,
            "quick_tp_pct":               self.quick_tp_pct,
            "trailing_act_pct":           self.trailing_act_pct,
            "trailing_gap_pct":           self.trailing_gap_pct,
            "max_hold_candles":           self.max_hold_candles,
            "typical_atr_pct":            self.typical_atr_pct,
            "max_spread_pct":             self.max_spread_pct,
            "confirmation_weight":        self.confirmation_weight,
            "confirmation_min_score":     self.confirmation_min_score,
            "kelly_lookback_trades":      self.kelly_lookback_trades,
            "kelly_enabled":              self.kelly_enabled,
            "max_consecutive_losses":     self.max_consecutive_losses,
            "consecutive_loss_size_mult": self.consecutive_loss_size_mult,
            "allowed_regimes":            list(self.allowed_regimes),
            "param_bounds":               self.param_bounds,
            "primary_trigger_type":       self.primary_trigger_type,
            "meta_learner_allowed":       self.meta_learner_allowed,
            "meta_learner_level1":        self.meta_learner_level1,
            "meta_learner_level2":        self.meta_learner_level2,
        }

_PRIMARY_TRIGGER_MAP: Dict[str, PrimaryTriggerType] = {
    "hodl_accumulate":  PrimaryTriggerType.TREND_CONFIRMATION,

    "trend_follow":     PrimaryTriggerType.TREND_CONFIRMATION,

    "breakout_swift":   PrimaryTriggerType.BREAKOUT_VOLUME,

    "scalp_volatile":   PrimaryTriggerType.COMPOSITE,

    "mean_revert":      PrimaryTriggerType.MOMENTUM_REVERSAL,

    "extreme_momentum": PrimaryTriggerType.BREAKOUT_VOLUME,

}

_META_LEARNER_PERMISSIONS: Dict[str, Dict[str, bool]] = {
    "hodl_accumulate": {
        "allowed": True,
        "level1":  False,
        "level2":  True,
    },
    "trend_follow": {
        "allowed": True,
        "level1":  False,
        "level2":  True,
    },
    "breakout_swift": {
        "allowed": True,
        "level1":  False,
        "level2":  True,
    },
    "scalp_volatile": {
        "allowed": True,
        "level1":  False,
        "level2":  True,
    },
    "mean_revert": {
        "allowed": True,
        "level1":  False,
        "level2":  True,
    },
    "extreme_momentum": {
        "allowed": True,
        "level1":  False,
        "level2":  False,
    },
}

def _build_parameter_bounds(profile_name: str) -> ParameterBounds:
    bounds  = ParameterBounds()
    overrides = PARAMETER_BOUNDS_OVERRIDE.get(profile_name, {})

    _BOUNDS_FIELD_MAP = {
        "entry_threshold":  ("entry_threshold_min",  "entry_threshold_max"),
        "volume_multiplier": ("volume_mult_min",      "volume_mult_max"),
        "rsi_min":          ("rsi_min_lower",         "rsi_min_upper"),
        "rsi_max":          ("rsi_max_lower",         "rsi_max_upper"),
        "atr_sl_mult":      ("atr_sl_mult_min",       "atr_sl_mult_max"),
        "atr_tp_mult":      ("atr_tp_mult_min",       "atr_tp_mult_max"),
        "trailing_act_pct": ("trailing_act_pct_min",  "trailing_act_pct_max"),
        "trailing_gap_pct": ("trailing_gap_pct_min",  "trailing_gap_pct_max"),
        "quick_sl_pct":     ("quick_sl_pct_min",      "quick_sl_pct_max"),
        "quick_tp_pct":     ("quick_tp_pct_min",      "quick_tp_pct_max"),
    }

    for param_key, (lo, hi) in overrides.items():
        field_pair = _BOUNDS_FIELD_MAP.get(param_key)
        if field_pair is None:
            log.warning(
                "PARAMETER_BOUNDS_OVERRIDE[%s][%s]: tidak ada mapping — diabaikan.",
                profile_name, param_key,
            )
            continue
        setattr(bounds, field_pair[0], lo)
        setattr(bounds, field_pair[1], hi)

        if lo >= hi:
            raise ValueError(
                f"PARAMETER_BOUNDS_OVERRIDE[{profile_name}][{param_key}]: "
                f"min {lo} >= max {hi}. Ini adalah konfigurasi yang tidak valid."
            )

    return bounds

def _assemble_profile_threshold(profile_name: str) -> ProfileThreshold:

    vol    = VOLUME_PARAMS[profile_name]
    rsi    = RSI_PARAMS[profile_name]
    brk    = BREAKOUT_PARAMS[profile_name]
    atr    = ATR_MULTIPLIERS[profile_name]
    sltp   = QUICK_SLTP_PARAMS[profile_name]
    trail  = TRAILING_PARAMS[profile_name]
    vola   = VOLATILITY_PARAMS[profile_name]
    mtf    = MTF_PARAMS[profile_name]
    risk   = RISK_PARAMS[profile_name]
    perms  = _META_LEARNER_PERMISSIONS[profile_name]
    bounds = _build_parameter_bounds(profile_name)

    return ProfileThreshold(
        profile_name          = profile_name,
        entry_threshold       = ENTRY_THRESHOLDS[profile_name],
        volume_mult           = vol["volume_mult"],
        volume_spike          = vol["volume_spike"],
        rsi_min               = rsi["rsi_min"],
        rsi_max               = rsi["rsi_max"],
        rsi_gc_min            = rsi["rsi_gc_min"],
        min_breakout_pct      = brk["min_breakout_pct"],
        atr_sl_mult           = atr["atr_sl_mult"],
        atr_tp_mult           = atr["atr_tp_mult"],
        atr_pct_threshold     = atr["atr_pct_threshold"],
        quick_sl_pct          = sltp["quick_sl_pct"],
        quick_tp_pct          = sltp["quick_tp_pct"],
        trailing_act_pct      = trail["trailing_act_pct"],
        trailing_gap_pct      = trail["trailing_gap_pct"],
        max_hold_candles      = MAX_HOLD_CANDLES[profile_name],
        typical_atr_pct       = vola["typical_atr_pct"],
        max_spread_pct        = vola["max_spread_pct"],
        confirmation_weight   = mtf["confirmation_weight"],
        confirmation_min_score = mtf["confirmation_min_score"],
        kelly_lookback_trades      = risk["kelly_lookback_trades"],
        kelly_enabled              = risk["kelly_enabled"],
        max_consecutive_losses     = risk["max_consecutive_losses"],
        consecutive_loss_size_mult = risk["consecutive_loss_size_mult"],
        allowed_regimes       = ALLOWED_REGIMES[profile_name],
        param_bounds          = bounds,
        primary_trigger_type  = _PRIMARY_TRIGGER_MAP[profile_name],
        meta_learner_allowed  = perms["allowed"],
        meta_learner_level1   = perms["level1"],
        meta_learner_level2   = perms["level2"],
    )

_KNOWN_PROFILES = [
    "hodl_accumulate",
    "trend_follow",
    "breakout_swift",
    "scalp_volatile",
    "mean_revert",
    "extreme_momentum",
]

_THRESHOLD_REGISTRY: Dict[str, ProfileThreshold] = {}

def get_profile_thresholds(profile_name: str) -> ProfileThreshold:

    if profile_name not in _THRESHOLD_REGISTRY:
        available = list(_THRESHOLD_REGISTRY.keys())
        raise KeyError(
            f"Profile '{profile_name}' tidak ada di threshold registry. "
            f"Profile yang tersedia: {available}. "
            f"Tambahkan ke semua blok di profiles/thresholds.py jika ini profile baru."
        )
    return _THRESHOLD_REGISTRY[profile_name]

def get_entry_threshold(profile_name: str) -> float:
    return get_profile_thresholds(profile_name).entry_threshold


def get_all_profile_names() -> List[str]:
    return list(_THRESHOLD_REGISTRY.keys())


def get_regime_allowed(profile_name: str, regime_value: str) -> bool:
    try:
        thresh = get_profile_thresholds(profile_name)
        return regime_value in thresh.allowed_regimes
    except KeyError:
        return False

def compare_profiles(
    profile_a: str,
    profile_b: str,
    fields: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    thresh_a = get_profile_thresholds(profile_a)
    thresh_b = get_profile_thresholds(profile_b)

    _NUMERIC_FIELDS = [
        "entry_threshold", "volume_mult", "volume_spike",
        "rsi_min", "rsi_max", "rsi_gc_min", "min_breakout_pct",
        "atr_sl_mult", "atr_tp_mult", "atr_pct_threshold",
        "quick_sl_pct", "quick_tp_pct",
        "trailing_act_pct", "trailing_gap_pct",
        "typical_atr_pct", "max_spread_pct",
        "confirmation_weight", "confirmation_min_score",
    ]

    target_fields = fields if fields else _NUMERIC_FIELDS
    result: Dict[str, Dict[str, Any]] = {}

    for f in target_fields:
        val_a = getattr(thresh_a, f, None)
        val_b = getattr(thresh_b, f, None)
        if val_a is None or val_b is None:
            continue
        try:
            diff = float(val_b) - float(val_a)
        except (TypeError, ValueError):
            diff = None
        result[f] = {"a": val_a, "b": val_b, "diff": diff}

    return result

def _validate_all_thresholds() -> None:

    all_bloks = {
        "ENTRY_THRESHOLDS":          ENTRY_THRESHOLDS,
        "VOLUME_PARAMS":             VOLUME_PARAMS,
        "RSI_PARAMS":                RSI_PARAMS,
        "BREAKOUT_PARAMS":           BREAKOUT_PARAMS,
        "ATR_MULTIPLIERS":           ATR_MULTIPLIERS,
        "QUICK_SLTP_PARAMS":         QUICK_SLTP_PARAMS,
        "TRAILING_PARAMS":           TRAILING_PARAMS,
        "MAX_HOLD_CANDLES":          MAX_HOLD_CANDLES,
        "VOLATILITY_PARAMS":         VOLATILITY_PARAMS,
        "MTF_PARAMS":                MTF_PARAMS,
        "RISK_PARAMS":               RISK_PARAMS,
        "ALLOWED_REGIMES":           ALLOWED_REGIMES,
        "_PRIMARY_TRIGGER_MAP":      _PRIMARY_TRIGGER_MAP,
        "_META_LEARNER_PERMISSIONS": _META_LEARNER_PERMISSIONS,
    }

    for blok_name, blok_dict in all_bloks.items():
        for pname in _KNOWN_PROFILES:
            if pname not in blok_dict:
                raise ValueError(
                    f"Profil '{pname}' TIDAK ADA di blok '{blok_name}'. "
                    f"Setiap blok WAJIB mengandung semua profil. "
                    f"Tambahkan entry untuk '{pname}' di '{blok_name}'."
                )

    for pname in _KNOWN_PROFILES:
        try:
            thresh = _assemble_profile_threshold(pname)
        except Exception as e:
            raise ValueError(
                f"Gagal assemble ProfileThreshold untuk '{pname}': {e}"
            ) from e

        errs = thresh.validate()
        if errs:
            raise ValueError(
                f"ProfileThreshold '{pname}' tidak valid:\n"
                + "\n".join(f"  • {e}" for e in errs)
                + "\nPerbaiki nilai di blok yang sesuai di profiles/thresholds.py."
            )

        _THRESHOLD_REGISTRY[pname] = thresh

    extreme_thresh = ENTRY_THRESHOLDS["extreme_momentum"]
    for pname, thresh_val in ENTRY_THRESHOLDS.items():
        if pname != "extreme_momentum" and thresh_val > extreme_thresh:
            raise ValueError(
                f"entry_threshold '{pname}' ({thresh_val}) lebih tinggi dari "
                f"extreme_momentum ({extreme_thresh}). "
                f"extreme_momentum harus memiliki threshold tertinggi karena "
                f"merupakan profil paling berisiko."
            )

    mean_rsi_min = RSI_PARAMS["mean_revert"]["rsi_min"]
    for pname, rsi_p in RSI_PARAMS.items():
        if pname != "mean_revert" and rsi_p["rsi_min"] < mean_rsi_min:
            raise ValueError(
                f"RSI min '{pname}' ({rsi_p['rsi_min']}) lebih rendah dari "
                f"mean_revert ({mean_rsi_min}). "
                f"mean_revert harus punya RSI min terendah (itu nature-nya)."
            )

    extreme_vol = VOLUME_PARAMS["extreme_momentum"]["volume_mult"]
    for pname, vol_p in VOLUME_PARAMS.items():
        if pname != "extreme_momentum" and vol_p["volume_mult"] > extreme_vol:
            raise ValueError(
                f"volume_mult '{pname}' ({vol_p['volume_mult']}) lebih tinggi dari "
                f"extreme_momentum ({extreme_vol}). "
                f"extreme_momentum membutuhkan volume requirement tertinggi."
            )

    for pname in _KNOWN_PROFILES:
        thresh = _THRESHOLD_REGISTRY[pname]
        bounds = thresh.param_bounds

        # entry_threshold harus dalam bounds-nya sendiri
        et = thresh.entry_threshold
        et_bounds = bounds.get_bounds("entry_threshold")
        if et_bounds and not (et_bounds[0] <= et <= et_bounds[1]):
            raise ValueError(
                f"[{pname}] entry_threshold default ({et}) di luar bounds "
                f"{et_bounds}. Nilai default harus selalu dalam bounds."
            )

        sl = thresh.quick_sl_pct
        sl_bounds = bounds.get_bounds("quick_sl_pct")
        if sl_bounds and not (sl_bounds[0] <= sl <= sl_bounds[1]):
            raise ValueError(
                f"[{pname}] quick_sl_pct default ({sl}) di luar bounds "
                f"{sl_bounds}."
            )

        tp = thresh.quick_tp_pct
        tp_bounds = bounds.get_bounds("quick_tp_pct")
        if tp_bounds and not (tp_bounds[0] <= tp <= tp_bounds[1]):
            raise ValueError(
                f"[{pname}] quick_tp_pct default ({tp}) di luar bounds "
                f"{tp_bounds}."
            )

    log.debug(
        "Semua threshold valid: %d profiles, %d blok divalidasi.",
        len(_KNOWN_PROFILES), len(all_bloks),
    )

try:
    _validate_all_thresholds()
except ValueError as _thresh_err:
    raise ImportError(
        f"FATAL: Threshold konfigurasi tidak valid — sistem tidak bisa dimulai.\n"
        f"Detail: {_thresh_err}\n"
        f"Perbaiki profiles/thresholds.py sebelum menjalankan bot."
    ) from _thresh_err