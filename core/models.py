"""
core/models.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _new_id() -> str:
    return str(uuid.uuid4())

class MarketRegime(str, Enum):
    TRENDING_BULL     = "trending_bull"
    TRENDING_BEAR     = "trending_bear"
    RANGING           = "ranging"
    VOLATILE_EXPANSION = "volatile_expansion"
    UNDEFINED         = "undefined"
    
    @property
    def allows_long(self) -> bool:
        return self in (
            MarketRegime.TRENDING_BULL,
            MarketRegime.RANGING,
            MarketRegime.VOLATILE_EXPANSION,
        )

    @property
    def emoji(self) -> str:
        _MAP = {
            MarketRegime.TRENDING_BULL:      "🟢",
            MarketRegime.TRENDING_BEAR:      "🔴",
            MarketRegime.RANGING:            "🟡",
            MarketRegime.VOLATILE_EXPANSION: "🟠",
            MarketRegime.UNDEFINED:          "⚪",
        }
        return _MAP.get(self, "⚪")

    @property
    def display_name(self) -> str:
        _MAP = {
            MarketRegime.TRENDING_BULL:      "Trending Bull",
            MarketRegime.TRENDING_BEAR:      "Trending Bear",
            MarketRegime.RANGING:            "Ranging",
            MarketRegime.VOLATILE_EXPANSION: "Volatile Expansion",
            MarketRegime.UNDEFINED:          "Undefined",
        }
        return _MAP.get(self, "Unknown")


class DecisionAction(str, Enum):
    EXECUTE = "execute"
    WAIT    = "wait"
    REJECT  = "reject"
    
class SignalQuality(str, Enum):

    EXCELLENT = "excellent"
    GOOD      = "good"
    FAIR      = "fair"
    POOR      = "poor"

    @classmethod
    def from_score(cls, score: float, threshold: float) -> "SignalQuality":
        if score >= 85.0:
            return cls.EXCELLENT
        if score >= 70.0 and score > threshold:
            return cls.GOOD
        if score >= threshold:
            return cls.FAIR
        return cls.POOR


class PatternType(str, Enum):

    BULLISH_ENGULFING  = "bullish_engulfing"
    HAMMER             = "hammer"
    DRAGONFLY_DOJI     = "dragonfly_doji"
    MORNING_STAR       = "morning_star"

    BEARISH_ENGULFING  = "bearish_engulfing"
    SHOOTING_STAR      = "shooting_star"
    GRAVESTONE_DOJI    = "gravestone_doji"
    EVENING_STAR       = "evening_star"

    BULLISH_MARUBOZU   = "bullish_marubozu"
    BEARISH_MARUBOZU   = "bearish_marubozu"

    STANDARD_DOJI      = "standard_doji"
    SPINNING_TOP       = "spinning_top"

    BB_KC_SQUEEZE      = "bb_kc_squeeze"
    VOLUME_CLIMAX      = "volume_climax"
    NONE               = "none"
    
    @property
    def is_bullish(self) -> Optional[bool]:
        _BULLISH = {
            PatternType.BULLISH_ENGULFING,
            PatternType.HAMMER,
            PatternType.DRAGONFLY_DOJI,
            PatternType.MORNING_STAR,
            PatternType.BULLISH_MARUBOZU,
        }
        _BEARISH = {
            PatternType.BEARISH_ENGULFING,
            PatternType.SHOOTING_STAR,
            PatternType.GRAVESTONE_DOJI,
            PatternType.EVENING_STAR,
            PatternType.BEARISH_MARUBOZU,
        }
        if self in _BULLISH:
            return True
        if self in _BEARISH:
            return False
        return None


class SuggestionStatus(str, Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    APPLIED   = "applied"
    REVERTED  = "reverted"
    EXPIRED   = "expired"

class PatternContext(str, Enum):
    NEAR_SUPPORT    = "near_support"
    NEAR_RESISTANCE = "near_resistance"
    MID_RANGE       = "mid_range"
    UNKNOWN         = "unknown"

@dataclass
class TrendIndicators:
    ema9:   Optional[float] = None
    ema21:  Optional[float] = None
    ema50:  Optional[float] = None
    ema100: Optional[float] = None
    ema200: Optional[float] = None
    ema_stack_score: float = 50.0
    golden_cross_bars_ago: Optional[int]   = None
    dead_cross_bars_ago:   Optional[int]   = None
    cross_score:           float           = 50.0
    supertrend_value:     Optional[float]  = None
    supertrend_direction: Optional[int]    = None
    supertrend_score:     float            = 50.0
    vwap:          Optional[float] = None
    vwap_upper_1:  Optional[float] = None
    vwap_lower_1:  Optional[float] = None
    vwap_upper_2:  Optional[float] = None
    vwap_lower_2:  Optional[float] = None
    vwap_score:    float           = 50.0
    composite_score: float = 50.0
    
    def is_valid(self) -> bool:
        return all(v is not None for v in [self.ema9, self.ema21, self.ema50])


@dataclass
class MomentumIndicators:
    rsi:              Optional[float] = None
    rsi_slope:        Optional[float] = None
    rsi_divergence:   Optional[float] = None
    rsi_zone_exit:    Optional[str]   = None
    rsi_score:        float           = 50.0
    macd_line:         Optional[float] = None
    macd_signal:       Optional[float] = None
    macd_histogram:    Optional[float] = None
    macd_hist_prev:    Optional[float] = None
    macd_divergence:   Optional[float] = None
    macd_zero_cross:   Optional[bool]  = None
    macd_score:        float           = 50.0
    stoch_k:           Optional[float] = None
    stoch_d:           Optional[float] = None
    stoch_kd_cross:    Optional[str]   = None
    stoch_zone:        Optional[str]   = None
    stoch_score:       float           = 50.0
    composite_score: float = 50.0

    def is_valid(self) -> bool:
        return self.rsi is not None

@dataclass
class StrengthIndicators:
    adx:             Optional[float] = None
    plus_di:         Optional[float] = None
    minus_di:        Optional[float] = None
    adx_score:       float           = 50.0
    di_score:        float           = 50.0
    volume_ratio:        Optional[float] = None
    volume_spike:        bool            = False
    obv:                 Optional[float] = None
    obv_trend:           Optional[str]   = None
    volume_climax:       bool            = False
    volume_score:        float           = 50.0
    mfi:             Optional[float] = None
    mfi_divergence:  Optional[float] = None
    mfi_score:       float           = 50.0
    composite_score: float = 50.0

    def is_valid(self) -> bool:
        return self.volume_ratio is not None

@dataclass
class VolatilityIndicators:
    bb_upper:    Optional[float] = None
    bb_middle:   Optional[float] = None
    bb_lower:    Optional[float] = None
    bb_width:    Optional[float] = None
    bb_position: Optional[float] = None
    bb_trending: Optional[str]   = None
    bb_score:    float           = 50.0
    kc_upper:    Optional[float] = None
    kc_lower:    Optional[float] = None
    kc_middle:   Optional[float] = None
    kc_score:    float           = 50.0
    squeeze_active:    bool           = False
    squeeze_bars:      int            = 0
    squeeze_score:     float          = 50.0
    atr:              Optional[float] = None
    atr_pct:          Optional[float] = None
    atr_percentile:   Optional[float] = None
    atr_trend:        Optional[str]   = None
    atr_score:        float           = 50.0
    composite_score: float = 50.0

    def is_valid(self) -> bool:
        return self.atr is not None and self.bb_upper is not None

@dataclass
class PatternIndicators:
    primary_pattern:    PatternType   = PatternType.NONE
    secondary_pattern:  PatternType   = PatternType.NONE
    pattern_context:    PatternContext = PatternContext.UNKNOWN
    pattern_volume_confirmed: bool  = False
    pattern_body_pct:         float = 0.0
    distance_to_support:    Optional[float] = None
    distance_to_resistance: Optional[float] = None
    higher_tf_aligned:      Optional[bool]  = None
    pattern_score:    float = 50.0
    context_score:    float = 50.0
    composite_score:  float = 50.0

    def is_valid(self) -> bool:
        return True

@dataclass
class OscillatorIndicators:
    # ── CCI ───────────────────────────────────────────────────────────────────
    cci:              Optional[float] = None
    cci_score:        float           = 50.0
    cci_trend:        Optional[str]   = None   # "rising"|"falling"|"flat"
    cci_divergence:   Optional[float] = None   # bull(+) / bear(-) divergence score
    # ── Williams %R ───────────────────────────────────────────────────────────
    williams_r:       Optional[float] = None
    williams_r_score: float           = 50.0
    willr_trend:      Optional[str]   = None   # "rising"|"falling"|"flat"
    # ── ROC / Momentum ────────────────────────────────────────────────────────
    roc:              Optional[float] = None   # ROC fast (9-period)
    roc_slow:         Optional[float] = None   # ROC slow (21-period)
    roc_slope:        Optional[float] = None   # acceleration of fast ROC
    roc_crossover:    Optional[str]   = None   # "bullish"|"bearish"|None
    roc_score:        float           = 50.0
    # ── Composite ─────────────────────────────────────────────────────────────
    composite_score:  float           = 50.0

    def is_valid(self) -> bool:
        return self.cci is not None or self.roc is not None

@dataclass
class StructureIndicators:
    # Ichimoku
    tenkan:           Optional[float] = None
    kijun:            Optional[float] = None
    senkou_a:         Optional[float] = None
    senkou_b:         Optional[float] = None
    chikou:           Optional[float] = None
    cloud_top:        Optional[float] = None
    cloud_bottom:     Optional[float] = None
    price_vs_cloud:   Optional[str]   = None   # "above","below","inside"
    cloud_thickness:  Optional[float] = None
    tk_cross:         Optional[str]   = None   # "bullish","bearish",None
    ichimoku_score:   float           = 50.0
    # Parabolic SAR
    sar_value:        Optional[float] = None
    sar_direction:    Optional[str]   = None   # "up","down"
    sar_score:        float           = 50.0
    # Pivot Points
    pivot:            Optional[float] = None
    r1:               Optional[float] = None
    r2:               Optional[float] = None
    r3:               Optional[float] = None
    s1:               Optional[float] = None
    s2:               Optional[float] = None
    s3:               Optional[float] = None
    nearest_support:    Optional[float] = None
    nearest_resistance: Optional[float] = None
    price_vs_pivot:     Optional[str]   = None   # "above","below"
    pivot_score:        float           = 50.0
    # Fibonacci
    fib_swing_high:          Optional[float] = None
    fib_swing_low:           Optional[float] = None
    fib_236:                 Optional[float] = None
    fib_382:                 Optional[float] = None
    fib_500:                 Optional[float] = None
    fib_618:                 Optional[float] = None
    fib_786:                 Optional[float] = None
    nearest_fib_support:     Optional[float] = None
    nearest_fib_resistance:  Optional[float] = None
    fib_score:               float           = 50.0
    # [v2 NEW] Fibonacci trend-awareness (uptrend/downtrend retracement) + extension target
    fib_trend:               Optional[str]   = None   # "uptrend" | "downtrend"
    fib_ext_1272:            Optional[float] = None
    fib_ext_1618:            Optional[float] = None
    # [v2 NEW] Pivot — transparansi sumber data setelah fix daily-resample
    pivot_period:            Optional[str]   = None   # "daily" | "bar_fallback"
    # [v2 NEW] Market Structure — HH/HL/LH/LL, Break of Structure, Change of Character
    trend_structure:         Optional[str]   = None   # "bullish"|"bearish"|"choppy"|"undefined"
    structure_event:         Optional[str]   = None   # "BOS_bullish"|"BOS_bearish"|"CHoCH_bullish"|"CHoCH_bearish"|None
    last_swing_high:         Optional[float] = None
    last_swing_low:          Optional[float] = None
    swing_points:            List[dict]      = field(default_factory=list)
    market_structure_score:  float           = 50.0
    # [v2 NEW] Support/Resistance zone clustering (confluence pivot+fib+swing)
    sr_zones:                      List[dict]      = field(default_factory=list)
    nearest_structure_support:     Optional[float] = None
    nearest_structure_resistance:  Optional[float] = None
    # [v2 NEW] Donchian Channel (scalar) — pendamping df.ta.donchian() vectorized di ta_compat.py
    donchian_upper:          Optional[float] = None
    donchian_lower:          Optional[float] = None
    donchian_middle:         Optional[float] = None
    donchian_pct_b:          Optional[float] = None
    donchian_width_pct:      Optional[float] = None
    donchian_score:          float           = 50.0
    composite_score:         float           = 50.0

    def is_valid(self) -> bool:
        return self.tenkan is not None or self.pivot is not None

@dataclass
class OrderbookIndicators:
    # ── Raw data ──────────────────────────────────────────────────────────────
    bid_ask_imbalance:  Optional[float] = None   # 0-1, >0.62 bullish
    whale_bid_wall:     Optional[float] = None   # harga level whale bid terbesar
    whale_ask_wall:     Optional[float] = None   # harga level whale ask terbesar
    bid_wall_strength:  Optional[float] = None   # % dari total weighted volume
    ask_wall_strength:  Optional[float] = None
    cluster_bid_wall:   Optional[float] = None   # harga cluster bid wall
    cluster_bid_str:    Optional[float] = None   # kekuatan cluster bid (%)
    cluster_ask_wall:   Optional[float] = None   # harga cluster ask wall
    cluster_ask_str:    Optional[float] = None   # kekuatan cluster ask (%)
    spread_pct:         Optional[float] = None   # spread best bid-ask (%)
    absorbed_bid:       bool            = False   # bid wall diserap (breakdown)
    absorbed_ask:       bool            = False   # ask wall diserap (breakout)
    bid_wall_dist:      Optional[float] = None   # relevansi jarak bid wall (0-1)
    ask_wall_dist:      Optional[float] = None   # relevansi jarak ask wall (0-1)
    # ── Sub-scores (0-100) ────────────────────────────────────────────────────
    imbalance_score:    float           = 50.0   # bid/ask ratio score
    whale_score:        float           = 50.0   # whale wall + cluster score
    spread_score:       float           = 80.0   # likuiditas spread score
    absorption_score:   float           = 50.0   # absorption signal score
    liquidity_score:    float           = 50.0   # total depth USDT score
    spoofing_confidence: float          = 1.0    # 0-1, makin rendah makin banyak spoof
    # ── Composite ─────────────────────────────────────────────────────────────
    orderbook_score:    float           = 50.0
    composite_score:    float           = 50.0

    def is_valid(self) -> bool:
        return self.bid_ask_imbalance is not None


@dataclass
class IndicatorSet:
    symbol:      str
    timeframe:   str
    timestamp:   datetime = field(default_factory=_utcnow)
    bars_available: int   = 0
    is_primary_tf:  bool  = True
    trend:      TrendIndicators      = field(default_factory=TrendIndicators)
    momentum:   MomentumIndicators   = field(default_factory=MomentumIndicators)
    strength:   StrengthIndicators   = field(default_factory=StrengthIndicators)
    volatility: VolatilityIndicators = field(default_factory=VolatilityIndicators)
    patterns:     PatternIndicators     = field(default_factory=PatternIndicators)
    oscillators:  OscillatorIndicators  = field(default_factory=OscillatorIndicators)
    structure:    StructureIndicators   = field(default_factory=StructureIndicators)
    orderbook:    OrderbookIndicators   = field(default_factory=OrderbookIndicators)

    current_price:  float = 0.0
    open_price:     float = 0.0
    high_price:     float = 0.0
    low_price:      float = 0.0
    close_price:    float = 0.0
    volume:         float = 0.0
    quote_volume:   float = 0.0
    calculation_errors: List[str] = field(default_factory=list)

    def is_fully_valid(self) -> bool:
        return (
            self.trend.is_valid()
            and self.momentum.is_valid()
            and self.strength.is_valid()
            and self.volatility.is_valid()
            and self.patterns.is_valid()
        )

    def has_critical_errors(self) -> bool:
        critical = ["ema9", "ema21", "rsi", "atr"]
        return any(
            any(c in err.lower() for c in critical)
            for err in self.calculation_errors
        )

    def add_error(self, indicator: str, reason: str) -> None:
        self.calculation_errors.append(f"{indicator}: {reason}")

@dataclass
class ObservationReport:
    symbol:          str
    strategy_profile: str
    observed_at:     datetime = field(default_factory=_utcnow)
    primary_tf_indicators:      Optional[IndicatorSet] = None
    confirmation_tf_indicators: Optional[IndicatorSet] = None
    primary_tf_score:       float = 50.0
    confirmation_tf_score:  float = 50.0
    composite_raw_score:    float = 50.0
    primary_tf_valid:      bool = False
    confirmation_tf_valid: bool = False
    used_cached:           bool = False
    
    def is_tradeable(self) -> bool:
        return self.primary_tf_valid and not (
            self.primary_tf_indicators is not None
            and self.primary_tf_indicators.has_critical_errors()
        )

@dataclass
class ScoreBreakdown:
    trend_raw:      float = 0.0
    trend_weighted: float = 0.0
    trend_weight:   float = 0.0
    momentum_raw:      float = 0.0
    momentum_weighted: float = 0.0
    momentum_weight:   float = 0.0
    strength_raw:      float = 0.0
    strength_weighted: float = 0.0
    strength_weight:   float = 0.0
    volatility_raw:      float = 0.0
    volatility_weighted: float = 0.0
    volatility_weight:   float = 0.0
    pattern_raw:      float = 0.0
    pattern_weighted: float = 0.0
    pattern_weight:   float = 0.0
    oscillator_raw:      float = 0.0
    oscillator_weighted: float = 0.0
    oscillator_weight:   float = 0.0
    structure_raw:      float = 0.0
    structure_weighted: float = 0.0
    structure_weight:   float = 0.0
    orderbook_raw:      float = 0.0
    orderbook_weighted: float = 0.0
    orderbook_weight:   float = 0.0
    regime_modifier:  float = 1.0
    
    def total(self) -> float:
        raw_total = (
            self.trend_weighted
            + self.momentum_weighted
            + self.strength_weighted
            + self.volatility_weighted
            + self.pattern_weighted
            + self.oscillator_weighted
            + self.structure_weighted
            + self.orderbook_weighted
        )
        return round(raw_total * self.regime_modifier, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trend":      {"raw": self.trend_raw,      "weighted": self.trend_weighted,      "weight": self.trend_weight},
            "momentum":   {"raw": self.momentum_raw,   "weighted": self.momentum_weighted,   "weight": self.momentum_weight},
            "strength":   {"raw": self.strength_raw,   "weighted": self.strength_weighted,   "weight": self.strength_weight},
            "volatility": {"raw": self.volatility_raw, "weighted": self.volatility_weighted, "weight": self.volatility_weight},
            "pattern":    {"raw": self.pattern_raw,    "weighted": self.pattern_weighted,    "weight": self.pattern_weight},
            "oscillator": {"raw": self.oscillator_raw, "weighted": self.oscillator_weighted, "weight": self.oscillator_weight},
            "structure":  {"raw": self.structure_raw,  "weighted": self.structure_weighted,  "weight": self.structure_weight},
            "orderbook":  {"raw": self.orderbook_raw,  "weighted": self.orderbook_weighted,  "weight": self.orderbook_weight},
            "regime_modifier": self.regime_modifier,
            "total": self.total(),
        }

@dataclass
class ScoredSignal:
    observation:      ObservationReport = field(default_factory=lambda: ObservationReport("", ""))
    strategy_profile: str               = ""
    total_score:    float         = 0.0
    score_breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    confidence:     float         = 0.0
    signal_quality: SignalQuality = SignalQuality.POOR
    trigger_met:    bool          = False
    threshold_used: float         = 70.0
    threshold_gap:  float         = 0.0
    signal_type:    str           = "hold"
    suggested_sl:   Optional[float] = None
    suggested_tp:   Optional[float] = None
    regime:            MarketRegime = MarketRegime.UNDEFINED
    regime_confidence: float        = 0.0
    scoring_narrative: str       = ""
    validation_notes:  List[str] = field(default_factory=list)
    scored_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        self.threshold_gap = self.total_score - self.threshold_used
        self.signal_quality = SignalQuality.from_score(self.total_score, self.threshold_used)

    @property
    def symbol(self) -> str:
        return self.observation.symbol

    @property
    def is_actionable(self) -> bool:
        return self.trigger_met and self.total_score >= self.threshold_used

    def add_validation_note(self, note: str) -> None:
        self.validation_notes.append(note)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol":           self.symbol,
            "profile":          self.strategy_profile,
            "total_score":      round(self.total_score, 2),
            "threshold":        self.threshold_used,
            "threshold_gap":    round(self.threshold_gap, 2),
            "signal_quality":   self.signal_quality.value,
            "trigger_met":      self.trigger_met,
            "signal_type":      self.signal_type,
            "regime":           self.regime.value,
            "regime_confidence": round(self.regime_confidence, 3),
            "confidence":       round(self.confidence, 3),
            "breakdown":        self.score_breakdown.to_dict(),
            "suggested_sl":     self.suggested_sl,
            "suggested_tp":     self.suggested_tp,
            "narrative":        self.scoring_narrative,
            "validation_notes": self.validation_notes,
            "scored_at":        self.scored_at.isoformat(),
        }


@dataclass
class TradeDecision:
    scored_signal: ScoredSignal = field(default_factory=ScoredSignal)
    action:           DecisionAction = DecisionAction.REJECT
    rejection_reason: str            = ""
    final_sl:         Optional[float] = None
    final_tp:         Optional[float] = None
    position_size:    Optional[float] = None
    position_size_pct: Optional[float] = None
    gates_passed: List[str] = field(default_factory=list)
    gates_failed: List[str] = field(default_factory=list)
    decision_narrative: str = ""
    kelly_fraction:    Optional[float] = None
    kelly_method_used: str             = "fallback"
    correlated_symbols: List[str] = field(default_factory=list)
    correlation_penalty: float    = 0.0
    decided_at: datetime = field(default_factory=_utcnow)

    @property
    def symbol(self) -> str:
        return self.scored_signal.symbol

    @property
    def is_executable(self) -> bool:
        return self.action == DecisionAction.EXECUTE

    @property
    def sl(self) -> Optional[float]:
        return self.final_sl

    @property
    def tp(self) -> Optional[float]:
        return self.final_tp

    def add_gate_passed(self, gate_name: str) -> None:
        self.gates_passed.append(gate_name)

    def add_gate_failed(self, gate_name: str, reason: str) -> None:
        self.gates_failed.append(f"{gate_name}: {reason}")

    def to_summary(self) -> str:
        gates_str = f"[{len(self.gates_passed)}/{len(self.gates_passed)+len(self.gates_failed)} gates passed]"
        if self.action == DecisionAction.EXECUTE:
            return (
                f"EXECUTE {self.symbol} | score={self.scored_signal.total_score:.1f} "
                f"| sl={self.final_sl:.6f} tp={self.final_tp:.6f} "
                f"| size_pct={self.position_size_pct:.2f}% {gates_str}"
            )
        return (
            f"{self.action.value.upper()} {self.symbol} | score={self.scored_signal.total_score:.1f} "
            f"| reason={self.rejection_reason[:80]} {gates_str}"
        )

@dataclass
class RegimePerformance:
    regime:         MarketRegime = MarketRegime.UNDEFINED
    total_trades:   int          = 0
    win_count:      int          = 0
    loss_count:     int          = 0
    gross_profit:   float        = 0.0
    gross_loss:     float        = 0.0
    avg_score_wins: float        = 0.0
    avg_score_losses: float      = 0.0
    is_significant: bool         = False

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.win_count / self.total_trades * 100

    @property
    def profit_factor(self) -> float:
        if abs(self.gross_loss) < 1e-9:
            return float("inf") if self.gross_profit > 0 else 0.0
        return abs(self.gross_profit / self.gross_loss)

    @property
    def net_pnl(self) -> float:
        return self.gross_profit + self.gross_loss


@dataclass
class IndicatorEffectiveness:
    indicator_name:     str   = ""
    avg_score_in_wins:  float = 0.0
    avg_score_in_losses: float = 0.0
    score_differential: float = 0.0
    correlation:        float = 0.0
    sample_size:        int   = 0
    is_significant:     bool  = False

    @property
    def is_predictive(self) -> bool:
        return self.is_significant and abs(self.score_differential) >= 15.0

@dataclass
class AttributionReport:
    computed_at:    datetime = field(default_factory=_utcnow)
    lookback_days:  int      = 30
    scope:          str      = "global"
    total_trades:   int      = 0
    sufficient_data: bool    = False
    overall_win_rate:     float = 0.0
    overall_profit_factor: float = 0.0
    overall_avg_score:    float = 0.0
    regime_performance: List[RegimePerformance] = field(default_factory=list)
    best_regime:  Optional[MarketRegime] = None
    worst_regime: Optional[MarketRegime] = None
    indicator_effectiveness: List[IndicatorEffectiveness] = field(default_factory=list)
    most_predictive_indicator:  str = ""
    least_predictive_indicator: str = ""
    win_rate_trend:     str   = "stable"
    last_30_win_rate:   float = 0.0
    last_10_win_rate:   float = 0.0
    insights: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def get_regime_stats(self, regime: MarketRegime) -> Optional[RegimePerformance]:
        return next((r for r in self.regime_performance if r.regime == regime), None)

@dataclass
class ParameterSuggestion:
    suggestion_id: str      = field(default_factory=_new_id)
    created_at:    datetime = field(default_factory=_utcnow)
    status:        SuggestionStatus = SuggestionStatus.PENDING
    symbol:         str = ""
    profile:        str = ""
    parameter_name: str = ""
    current_value:   Any = None
    suggested_value: Any = None
    value_delta:     Any = None
    confidence:            float  = 0.0
    reasoning:             str    = ""
    supporting_data:       Dict[str, Any] = field(default_factory=dict)
    projected_improvement: float  = 0.0
    within_bounds:    bool = True
    bounds_min:       Any  = None
    bounds_max:       Any  = None
    approved_at:    Optional[datetime] = None
    approved_by:    str                = ""
    rejected_at:    Optional[datetime] = None
    rejection_note: str                = ""
    applied_at:     Optional[datetime] = None
    reverted_at:    Optional[datetime] = None
    revert_reason:  str                = ""
    trades_after_apply:      int   = 0
    win_rate_before:         float = 0.0
    win_rate_after:          float = 0.0
    outcome:                 str   = ""
    cooling_off_until: Optional[datetime] = None

    def to_telegram_summary(self) -> str:
        status_emoji = {
            SuggestionStatus.PENDING:  "⏳",
            SuggestionStatus.APPROVED: "✅",
            SuggestionStatus.REJECTED: "❌",
            SuggestionStatus.APPLIED:  "🔧",
            SuggestionStatus.REVERTED: "↩️",
            SuggestionStatus.EXPIRED:  "⌛",
        }.get(self.status, "❓")

        target = f"{self.symbol or 'ALL'}/{self.profile}"
        return (
            f"{status_emoji} `{self.suggestion_id[:8]}` — {target}\n"
            f"  Param: `{self.parameter_name}`\n"
            f"  {self.current_value} → {self.suggested_value}\n"
            f"  Conf: {self.confidence:.1%} | +{self.projected_improvement:.1f}% win rate\n"
            f"  {self.reasoning[:120]}"
        )

def validate_score(value: float, name: str = "score") -> float:
    if not (0.0 <= value <= 100.0):
        raise ValueError(
            f"Score '{name}' harus dalam range [0, 100], got: {value:.4f}. "
            f"Ini adalah bug kritis — periksa kalkulasi di indikator yang menghasilkannya."
        )
    return round(value, 4)

def validate_weight_table(weights: Dict[str, float], table_name: str = "") -> None:
    total = sum(weights.values())
    if not (0.999 <= total <= 1.001):
        raise ValueError(
            f"Weight table '{table_name}' jumlahnya {total:.6f}, harus 1.0 (±0.001). "
            f"Weights: {weights}. "
            f"Sistem refuse to start dengan weight yang tidak valid."
        )


def clamp_score(value: float) -> float:
    return max(0.0, min(100.0, round(value, 4)))
