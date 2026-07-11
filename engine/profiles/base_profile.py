"""
profiles/base_profile.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("profiles.base")

class StrategyProfile(str, Enum):
    HODL_ACCUMULATE  = "hodl_accumulate"
    TREND_FOLLOW     = "trend_follow"
    BREAKOUT_SWIFT   = "breakout_swift"
    SCALP_VOLATILE   = "scalp_volatile"
    MEAN_REVERT      = "mean_revert"
    EXTREME_MOMENTUM = "extreme_momentum"

class PrimaryTriggerType(str, Enum):
    BREAKOUT_VOLUME    = "breakout_volume"
    TREND_CONFIRMATION = "trend_confirmation"
    MOMENTUM_REVERSAL  = "momentum_reversal"
    COMPOSITE          = "composite"

@dataclass
class ParameterBounds:
    entry_threshold_min: float = 55.0
    entry_threshold_max: float = 85.0
    volume_mult_min: float = 1.0
    volume_mult_max: float = 5.0
    rsi_min_lower: float = 25.0
    rsi_min_upper: float = 60.0
    rsi_max_lower: float = 55.0
    rsi_max_upper: float = 92.0
    atr_sl_mult_min: float = 1.0
    atr_sl_mult_max: float = 4.5
    atr_tp_mult_min: float = 1.5
    atr_tp_mult_max: float = 12.0
    trailing_act_pct_min: float = 0.5
    trailing_act_pct_max: float = 6.0
    trailing_gap_pct_min: float = 0.2
    trailing_gap_pct_max: float = 3.5
    quick_sl_pct_min: float = 0.5
    quick_sl_pct_max: float = 6.0
    quick_tp_pct_min: float = 0.8
    quick_tp_pct_max: float = 12.0

    def get_bounds(self, param_name: str) -> Optional[Tuple[float, float]]:
        _MAP: Dict[str, Tuple[str, str]] = {
            "entry_threshold":         ("entry_threshold_min",  "entry_threshold_max"),
            "volume_mult":             ("volume_mult_min",       "volume_mult_max"),
            "volume_multiplier":       ("volume_mult_min",       "volume_mult_max"),
            "rsi_min":                 ("rsi_min_lower",         "rsi_min_upper"),
            "rsi_max":                 ("rsi_max_lower",         "rsi_max_upper"),
            "atr_sl_mult":             ("atr_sl_mult_min",       "atr_sl_mult_max"),
            "atr_tp_mult":             ("atr_tp_mult_min",       "atr_tp_mult_max"),
            "trailing_act_pct":        ("trailing_act_pct_min",  "trailing_act_pct_max"),
            "trailing_activation_pct": ("trailing_act_pct_min",  "trailing_act_pct_max"),
            "trailing_gap_pct":        ("trailing_gap_pct_min",  "trailing_gap_pct_max"),
            "quick_sl_pct":            ("quick_sl_pct_min",      "quick_sl_pct_max"),
            "quick_tp_pct":            ("quick_tp_pct_min",      "quick_tp_pct_max"),
        }
        entry = _MAP.get(param_name)
        if entry is None:
            return None
        lo = getattr(self, entry[0])
        hi = getattr(self, entry[1])
        return (lo, hi)

    def is_within_bounds(self, param_name: str, value: Any) -> bool:
        bounds = self.get_bounds(param_name)
        if bounds is None:
            return True
        try:
            v = float(value)
            return bounds[0] <= v <= bounds[1]
        except (TypeError, ValueError):
            return False

@dataclass
class CoinProfile:
    symbol:   str
    profile:  StrategyProfile
    timeframe:         str
    timeframe_conf:    str
    volume_mult:   float
    volume_spike:  float
    rsi_min:       float
    rsi_max:       float
    rsi_gc_min:    float
    min_breakout_pct: float
    atr_sl_mult:  float
    atr_tp_mult:  float
    quick_sl_pct:  float
    quick_tp_pct:  float
    atr_pct_threshold: float
    trailing_act_pct:  float
    trailing_gap_pct:  float
    max_hold_candles: int
    description:     str   = ""
    typical_atr_pct: float = 0.5
    notes:           str   = ""
    confirmation_timeframe: str   = ""
    confirmation_weight:    float = 0.25
    confirmation_min_score: float = 45.0
    max_spread_pct: float = 0.15
    max_consecutive_losses: int   = 3
    consecutive_loss_size_mult: float = 0.5
    kelly_lookback_trades: int   = 50
    kelly_enabled:         bool  = True
    meta_learner_allowed:  bool = True
    meta_learner_level1:   bool = False
    meta_learner_level2:   bool = True
    
    param_bounds: ParameterBounds = field(default_factory=ParameterBounds)
    
    entry_threshold: float = 70.0
    
    primary_trigger_type: PrimaryTriggerType = PrimaryTriggerType.COMPOSITE

    allowed_regimes: List[str] = field(default_factory=lambda: ["trending_bull"])
    allowed_entry_on_transition:      bool  = False  # boleh entry saat regime transisi
    transition_size_mult:             float = 0.5    # size multiplier saat entry transisi
    regime_transition_sl_tighten_pct: float = 0.30  # % SL diperketat saat HOLD_TIGHTEN_SL
    regime_transition_sl_relax_pct:   float = 0.20  # % SL dilonggarkan saat HOLD_RELAX_SL

    _is_overridden: bool = field(default=False, repr=False)
    _override_source: str = field(default="", repr=False)
    _override_timestamp: Optional[str] = field(default=None, repr=False)

    @property
    def max_hold_seconds(self) -> int:
        if self.max_hold_candles <= 0:
            return 0
        tf_secs = {
            "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
            "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
        }
        secs = tf_secs.get(self.timeframe, 900)
        return self.max_hold_candles * secs

    @property
    def effective_confirmation_tf(self) -> str:
        if self.confirmation_timeframe:
            return self.confirmation_timeframe
        if self.timeframe_conf:
            return self.timeframe_conf
        _FALLBACK = {
            "1m": "5m", "3m": "15m", "5m": "15m",
            "15m": "1h", "30m": "1h",
            "1h": "4h", "2h": "4h",
            "4h": "1d", "6h": "1d", "8h": "1d",
            "12h": "1d", "1d": "1w",
        }
        return _FALLBACK.get(self.timeframe, "15m")

    @property
    def is_high_frequency(self) -> bool:
        return self.timeframe in ("1m", "3m", "5m")

    @property
    def is_swing(self) -> bool:
        return self.timeframe in ("1h", "2h", "4h", "6h", "8h", "12h", "3d", "1w")

    @property
    def risk_level(self) -> str:
        if self.profile == StrategyProfile.EXTREME_MOMENTUM:
            return "very_high"
        if self.quick_sl_pct <= 2.0 and self.max_hold_candles > 0 and self.max_hold_candles <= 12:
            return "high"
        if self.quick_sl_pct <= 3.0:
            return "medium"
        return "low"

    def validate(self) -> List[str]:
        errors: List[str] = []

        if self.rsi_min >= self.rsi_max:
            errors.append(
                f"rsi_min ({self.rsi_min}) harus < rsi_max ({self.rsi_max})"
            )

        if self.atr_sl_mult <= 0:
            errors.append(f"atr_sl_mult harus > 0, got {self.atr_sl_mult}")
        if self.atr_tp_mult <= 0:
            errors.append(f"atr_tp_mult harus > 0, got {self.atr_tp_mult}")

        if self.quick_sl_pct >= self.quick_tp_pct:
            errors.append(
                f"quick_sl_pct ({self.quick_sl_pct}) harus < quick_tp_pct ({self.quick_tp_pct})"
            )

        if not (40.0 <= self.entry_threshold <= 95.0):
            errors.append(
                f"entry_threshold harus dalam [40, 95], got {self.entry_threshold}"
            )

        if self.volume_spike <= self.volume_mult:
            errors.append(
                f"volume_spike ({self.volume_spike}) harus > volume_mult ({self.volume_mult})"
            )

        if self.trailing_act_pct <= self.trailing_gap_pct:
            errors.append(
                f"trailing_act_pct ({self.trailing_act_pct}) harus > trailing_gap_pct ({self.trailing_gap_pct})"
            )

        if not (0.0 <= self.confirmation_weight <= 1.0):
            errors.append(
                f"confirmation_weight harus dalam [0, 1], got {self.confirmation_weight}"
            )

        if self.kelly_lookback_trades < 10:
            errors.append(
                f"kelly_lookback_trades harus >= 10, got {self.kelly_lookback_trades}"
            )

        return errors

    def with_override(
        self,
        source: str,
        timestamp: str,
        **kwargs: Any,
    ) -> "CoinProfile":
        for param_name, new_value in kwargs.items():
            if not self.param_bounds.is_within_bounds(param_name, new_value):
                bounds = self.param_bounds.get_bounds(param_name)
                raise ValueError(
                    f"Override '{param_name}' = {new_value} di luar bounds {bounds} "
                    f"untuk profile {self.profile.value}/{self.symbol}. "
                    f"Meta_learner tidak boleh melebihi batas keamanan."
                )

        # [BUG-FIX] Sebelumnya pakai dataclasses.asdict(self) yang REKURSIF —
        # ini mengonversi semua nested dataclass (termasuk param_bounds, yang
        # tipenya ParameterBounds) jadi dict biasa. Akibatnya new_profile.param_bounds
        # jadi dict, bukan ParameterBounds lagi -> with_override() ke-2 kali
        # (override berantai, misal dari meta_learner yang adjust parameter
        # bertahap) CRASH: AttributeError 'dict' object has no attribute
        # 'is_within_bounds'. Terbukti reproduksi nyata sebelum fix ini.
        # Sekarang shallow-copy manual via dataclasses.fields() — param_bounds
        # & allowed_regimes tetap objek/reference asli (sudah dicek: tidak ada
        # mutasi in-place di mana pun, jadi shared reference ini aman).
        import dataclasses
        current_dict = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if not f.name.startswith("_")
        }

        current_dict.update(kwargs)

        new_profile = CoinProfile(**current_dict)
        new_profile._is_overridden      = True
        new_profile._override_source    = source
        new_profile._override_timestamp = timestamp

        errs = new_profile.validate()
        if errs:
            raise ValueError(
                f"Override menghasilkan profile tidak valid: {'; '.join(errs)}"
            )

        log.info(
            "Profile override: %s/%s | source=%s | changes=%s",
            self.profile.value, self.symbol,
            source, list(kwargs.keys()),
        )
        return new_profile

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol":              self.symbol,
            "profile":             self.profile.value,
            "timeframe":           self.timeframe,
            "timeframe_conf":      self.timeframe_conf,
            "volume_mult":         self.volume_mult,
            "volume_spike":        self.volume_spike,
            "rsi_min":             self.rsi_min,
            "rsi_max":             self.rsi_max,
            "rsi_gc_min":          self.rsi_gc_min,
            "min_breakout_pct":    self.min_breakout_pct,
            "atr_sl_mult":         self.atr_sl_mult,
            "atr_tp_mult":         self.atr_tp_mult,
            "quick_sl_pct":        self.quick_sl_pct,
            "quick_tp_pct":        self.quick_tp_pct,
            "atr_pct_threshold":   self.atr_pct_threshold,
            "trailing_act_pct":    self.trailing_act_pct,
            "trailing_gap_pct":    self.trailing_gap_pct,
            "max_hold_candles":    self.max_hold_candles,
            "typical_atr_pct":     self.typical_atr_pct,
            "entry_threshold":     self.entry_threshold,
            "max_spread_pct":      self.max_spread_pct,
            "confirmation_weight": self.confirmation_weight,
            "kelly_lookback_trades": self.kelly_lookback_trades,
        }

    def summary_line(self) -> str:
        return (
            f"{self.symbol:<14} | {self.profile.value:<22} | "
            f"TF={self.timeframe:<4} | "
            f"SL={self.quick_sl_pct:.1f}% TP={self.quick_tp_pct:.1f}% | "
            f"threshold={self.entry_threshold:.0f} | risk={self.risk_level}"
            + (" [OVERRIDE]" if self._is_overridden else "")
        )



class AdaptiveParams:
    @staticmethod
    def adjust_for_market(
        profile: CoinProfile,
        cur_atr: float,
        cur_price: float,
        cur_vol_ratio: float,
        cur_rsi: float,
    ) -> Dict[str, Any]:
        cur_atr_pct = (cur_atr / cur_price * 100) if cur_price > 0 else 0.0
        typical_atr = profile.typical_atr_pct or 1.0
        atr_ratio = cur_atr_pct / typical_atr if typical_atr > 0 else 1.0

        if atr_ratio > 2.0:
            sl_mult = profile.atr_sl_mult * 1.3
            tp_mult = profile.atr_tp_mult * 1.2
            mode = "HIGH_VOLATILITY"
        elif atr_ratio > 1.5:
            sl_mult = profile.atr_sl_mult * 1.15
            tp_mult = profile.atr_tp_mult * 1.1
            mode = "ELEVATED_VOLATILITY"
        elif atr_ratio < 0.5:
            sl_mult = profile.atr_sl_mult * 0.9
            tp_mult = profile.atr_tp_mult * 0.9
            mode = "LOW_VOLATILITY"
        else:
            sl_mult = profile.atr_sl_mult
            tp_mult = profile.atr_tp_mult
            mode = "NORMAL"

        vol_threshold = profile.volume_mult * (0.85 if cur_vol_ratio > 3.0 else 1.0)
        if atr_ratio > 1.5:
            rsi_min = max(profile.rsi_min - 5, 30)
            rsi_max = min(profile.rsi_max + 5, 85)
        else:
            rsi_min = profile.rsi_min
            rsi_max = profile.rsi_max

        sl_atr = cur_atr * sl_mult
        sl_pct = cur_price * (profile.quick_sl_pct / 100)
        sl_final = cur_price - max(sl_atr, sl_pct)

        tp_atr = cur_atr * tp_mult
        tp_pct = cur_price * (profile.quick_tp_pct / 100)
        tp_final = cur_price + max(tp_atr, tp_pct)

        return {
            "profile": profile.profile.value,
            "adaptive_mode": mode,
            "atr_ratio": round(atr_ratio, 2),
            "atr_pct": round(cur_atr_pct, 4),
            "sl_mult": round(sl_mult, 2),
            "tp_mult": round(tp_mult, 2),
            "sl": round(sl_final, 8),
            "tp": round(tp_final, 8),
            "vol_threshold": round(vol_threshold, 2),
            "rsi_min": rsi_min,
            "rsi_max": rsi_max,
            "timeframe": profile.timeframe,
            "trailing_act_pct": profile.trailing_act_pct,
            "trailing_gap_pct": profile.trailing_gap_pct,
        }

PROFILE_EMOJI: Dict[str, str] = {
    StrategyProfile.HODL_ACCUMULATE.value:  "🏦",
    StrategyProfile.TREND_FOLLOW.value:     "📈",
    StrategyProfile.BREAKOUT_SWIFT.value:   "⚡",
    StrategyProfile.SCALP_VOLATILE.value:   "🎯",
    StrategyProfile.MEAN_REVERT.value:      "↩️",
    StrategyProfile.EXTREME_MOMENTUM.value: "🚀",
}

PROFILE_TIMEFRAME: Dict[str, str] = {
    StrategyProfile.HODL_ACCUMULATE.value:  "1d",
    StrategyProfile.TREND_FOLLOW.value:     "4h",
    StrategyProfile.BREAKOUT_SWIFT.value:   "1h",
    StrategyProfile.SCALP_VOLATILE.value:   "15m",
    StrategyProfile.MEAN_REVERT.value:      "4h",
    StrategyProfile.EXTREME_MOMENTUM.value: "15m",
}

PROFILE_RISK_LABEL: Dict[str, str] = {
    StrategyProfile.HODL_ACCUMULATE.value:  "Low",
    StrategyProfile.TREND_FOLLOW.value:     "Medium",
    StrategyProfile.BREAKOUT_SWIFT.value:   "Medium-High",
    StrategyProfile.SCALP_VOLATILE.value:   "High",
    StrategyProfile.MEAN_REVERT.value:      "Medium",
    StrategyProfile.EXTREME_MOMENTUM.value: "Very High ⚠️",
}

TIMEFRAME_CONFIRMATION_MAP: Dict[str, str] = {
    "1m":  "5m",
    "3m":  "15m",
    "5m":  "15m",
    "15m": "1h",
    "30m": "1h",
    "1h":  "4h",
    "2h":  "4h",
    "4h":  "1d",
    "6h":  "1d",
    "8h":  "1d",
    "12h": "1d",
    "1d":  "1w",
    "3d":  "1w",
    "1w":  "1w",
}
