"""
intelligence/classifier.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

from constants import (
    REGIME_VOLATILE_ATR_PERCENTILE_MIN,
    REGIME_VOLATILE_BB_WIDTH_MIN,
    REGIME_TRENDING_ADX_MIN,
    REGIME_TRENDING_STRONG_ADX,
    REGIME_BULL_EMA_REQUIRED_PAIRS,
    REGIME_BEAR_EMA_REQUIRED_PAIRS,
    REGIME_RANGING_ADX_MAX,
    REGIME_RANGING_BB_WIDTH_MAX,
    REGIME_HYSTERESIS_BARS,
    REGIME_CONFIDENCE_HIGH_ADX,
    REGIME_CONFIDENCE_LOW_ADX,
    REGIME_MIN_CONFIDENCE_TO_TRADE,
)
from core.models import (
    IndicatorSet,
    MarketRegime,
    ObservationReport,
)

log = logging.getLogger("intelligence.classifier")

@dataclass
class _RegimeBuffer:
    current_regime: MarketRegime = MarketRegime.UNDEFINED
    current_confidence: float = 0.0
    pending_regime: MarketRegime = MarketRegime.UNDEFINED
    pending_count: int = 0
    history: Deque[MarketRegime] = field(default_factory=lambda: deque(maxlen=10))

    def propose(self, new_regime: MarketRegime, confidence: float, hysteresis: int) -> Tuple[MarketRegime, float]:
        self.history.append(new_regime)

        if new_regime == self.current_regime:
            self.current_confidence = confidence
            self.pending_regime = MarketRegime.UNDEFINED
            self.pending_count = 0
            return self.current_regime, self.current_confidence

        if new_regime == self.pending_regime:
            self.pending_count += 1
        else:
            self.pending_regime = new_regime
            self.pending_count = 1

        if self.pending_count >= hysteresis:
            old = self.current_regime
            self.current_regime = new_regime
            self.current_confidence = confidence
            self.pending_regime = MarketRegime.UNDEFINED
            self.pending_count = 0
            log.info(
                "Regime changed: %s → %s (confidence=%.2f, confirmed after %d bars)",
                old.value, new_regime.value, confidence, hysteresis,
            )

        return self.current_regime, self.current_confidence

_REGIME_BUFFERS: Dict[str, _RegimeBuffer] = {}

def _get_buffer(symbol: str) -> _RegimeBuffer:
    if symbol not in _REGIME_BUFFERS:
        _REGIME_BUFFERS[symbol] = _RegimeBuffer()
    return _REGIME_BUFFERS[symbol]

def _count_bullish_ema_pairs(iset: IndicatorSet) -> int:
    pairs = [
        (iset.trend.ema9,   iset.trend.ema21),
        (iset.trend.ema21,  iset.trend.ema50),
        (iset.trend.ema50,  iset.trend.ema100),
        (iset.trend.ema100, iset.trend.ema200),
    ]
    count = 0
    for fast, slow in pairs:
        if fast is not None and slow is not None:
            if fast > slow:
                count += 1
    return count

def _count_bearish_ema_pairs(iset: IndicatorSet) -> int:
    pairs = [
        (iset.trend.ema9,   iset.trend.ema21),
        (iset.trend.ema21,  iset.trend.ema50),
        (iset.trend.ema50,  iset.trend.ema100),
        (iset.trend.ema100, iset.trend.ema200),
    ]
    count = 0
    for fast, slow in pairs:
        if fast is not None and slow is not None:
            if fast < slow:
                count += 1
    return count

def _supertrend_is_bull(iset: IndicatorSet) -> Optional[bool]:
    if iset.trend.supertrend_direction is None:
        return None
    return iset.trend.supertrend_direction == 1

def _is_volatile(iset: IndicatorSet) -> bool:
    vol = iset.volatility
    atr_pct = vol.atr_percentile
    bb_w    = vol.bb_width

    if atr_pct is not None and atr_pct >= REGIME_VOLATILE_ATR_PERCENTILE_MIN:
        return True
    if bb_w is not None and bb_w >= REGIME_VOLATILE_BB_WIDTH_MIN:
        return True
    return False

def _is_ranging(iset: IndicatorSet) -> bool:
    vol = iset.volatility
    str_ = iset.strength

    adx  = str_.adx
    bb_w = vol.bb_width

    if adx is not None and adx >= REGIME_RANGING_ADX_MAX:
        return False
    if bb_w is not None and bb_w >= REGIME_RANGING_BB_WIDTH_MAX:
        return False

    return True

def _calc_confidence(iset: IndicatorSet, regime: MarketRegime) -> float:
    adx = iset.strength.adx
    atr_pct = iset.volatility.atr_percentile
    bb_w    = iset.volatility.bb_width

    if regime in (MarketRegime.TRENDING_BULL, MarketRegime.TRENDING_BEAR):
        if adx is None:
            return 0.5
        if adx >= REGIME_CONFIDENCE_HIGH_ADX:
            base = 0.90
        elif adx >= REGIME_TRENDING_STRONG_ADX:
            t = (adx - REGIME_TRENDING_STRONG_ADX) / (REGIME_CONFIDENCE_HIGH_ADX - REGIME_TRENDING_STRONG_ADX)
            base = 0.70 + t * 0.20
        elif adx >= REGIME_TRENDING_ADX_MIN:
            t = (adx - REGIME_TRENDING_ADX_MIN) / (REGIME_TRENDING_STRONG_ADX - REGIME_TRENDING_ADX_MIN)
            base = 0.50 + t * 0.20
        else:
            base = 0.40

        st = _supertrend_is_bull(iset)
        if st is not None:
            is_bull_regime = regime == MarketRegime.TRENDING_BULL
            if st == is_bull_regime:
                base = min(1.0, base + 0.08)
            else:
                base = max(0.0, base - 0.10)

        return round(base, 3)

    elif regime == MarketRegime.VOLATILE_EXPANSION:
        if atr_pct is not None and atr_pct >= 90.0:
            return 0.88
        if bb_w is not None and bb_w >= 0.12:
            return 0.80
        return 0.65

    elif regime == MarketRegime.RANGING:
        if adx is None:
            return 0.55
        if adx <= REGIME_CONFIDENCE_LOW_ADX:
            t = adx / REGIME_CONFIDENCE_LOW_ADX
            return round(0.85 - t * 0.20, 3)
        return 0.50

    else:
        return 0.30

def _classify_raw(iset: IndicatorSet) -> Tuple[MarketRegime, float]:
    if not iset.trend.is_valid() or not iset.strength.is_valid():
        return MarketRegime.UNDEFINED, 0.25

    if _is_volatile(iset):
        conf = _calc_confidence(iset, MarketRegime.VOLATILE_EXPANSION)
        return MarketRegime.VOLATILE_EXPANSION, conf

    adx       = iset.strength.adx
    plus_di   = iset.strength.plus_di
    minus_di  = iset.strength.minus_di
    has_adx   = adx is not None

    bullish_pairs = _count_bullish_ema_pairs(iset)
    bearish_pairs = _count_bearish_ema_pairs(iset)
    st_bull       = _supertrend_is_bull(iset)

    trending = has_adx and adx >= REGIME_TRENDING_ADX_MIN

    is_bear = (
        bearish_pairs >= REGIME_BEAR_EMA_REQUIRED_PAIRS
        and (st_bull is None or st_bull is False)
    )
    if trending and is_bear and plus_di is not None and minus_di is not None:
        if minus_di > plus_di:
            conf = _calc_confidence(iset, MarketRegime.TRENDING_BEAR)
            return MarketRegime.TRENDING_BEAR, conf

    is_bull = bullish_pairs >= REGIME_BULL_EMA_REQUIRED_PAIRS
    if trending and is_bull:
        if plus_di is not None and minus_di is not None and plus_di > minus_di:
            conf = _calc_confidence(iset, MarketRegime.TRENDING_BULL)
            return MarketRegime.TRENDING_BULL, conf
        elif plus_di is None:
            conf = _calc_confidence(iset, MarketRegime.TRENDING_BULL) * 0.80
            return MarketRegime.TRENDING_BULL, round(conf, 3)

    if _is_ranging(iset):
        conf = _calc_confidence(iset, MarketRegime.RANGING)
        return MarketRegime.RANGING, conf

    return MarketRegime.UNDEFINED, 0.35

def classify_regime(
    symbol: str,
    iset: IndicatorSet,
    hysteresis_bars: int = REGIME_HYSTERESIS_BARS,
    db_manager=None,
) -> Tuple[MarketRegime, float]:
    raw_regime, raw_confidence = _classify_raw(iset)

    buf = _get_buffer(symbol)
    effective_regime, effective_confidence = buf.propose(
        raw_regime, raw_confidence, hysteresis_bars
    )

    log.debug(
        "%s: raw=%s(%.2f) → effective=%s(%.2f) | pending=%s(%d/%d)",
        symbol,
        raw_regime.value, raw_confidence,
        effective_regime.value, effective_confidence,
        buf.pending_regime.value if buf.pending_regime != MarketRegime.UNDEFINED else "-",
        buf.pending_count, hysteresis_bars,
    )
    return effective_regime, effective_confidence

def classify_from_observation(
    observation: ObservationReport,
    hysteresis_bars: int = REGIME_HYSTERESIS_BARS,
    db_manager=None,
) -> Tuple[MarketRegime, float]:
    iset = observation.primary_tf_indicators
    if iset is None:
        log.warning(
            "%s: Tidak ada primary_tf_indicators di ObservationReport.",
            observation.symbol,
        )
        return MarketRegime.UNDEFINED, 0.0

    return classify_regime(
        symbol=observation.symbol,
        iset=iset,
        hysteresis_bars=hysteresis_bars,
        db_manager=db_manager,
    )

def is_tradeable_regime(
    regime: MarketRegime,
    confidence: float,
    allowed_regimes: Optional[list] = None,
    min_confidence: float = REGIME_MIN_CONFIDENCE_TO_TRADE,
) -> Tuple[bool, str]:
    if confidence < min_confidence:
        return False, (
            f"Regime confidence terlalu rendah: {confidence:.2f} < {min_confidence:.2f}"
        )

    if allowed_regimes is not None:
        if regime.value not in allowed_regimes:
            return False, (
                f"Regime '{regime.value}' tidak diizinkan. "
                f"Allowed: {allowed_regimes}"
            )

    # [BUG-FIX] Sebelumnya hardcode "if regime == TRENDING_BEAR" di sini —
    # padahal MarketRegime.allows_long property sudah ada persis untuk
    # tujuan ini (dan sudah lama jadi dead code karena tak dipakai siapa pun).
    # Pakai allows_long sebagai satu sumber kebenaran: otomatis ikut kalau
    # daftar regime yang diizinkan long berubah di masa depan, tidak perlu
    # sinkronkan manual di 2 tempat berbeda.
    if not regime.allows_long:
        return False, (
            f"Regime '{regime.value}' tidak mengizinkan posisi long "
            f"(allows_long=False)."
        )

    return True, f"Regime {regime.value} OK (confidence={confidence:.2f})"

def get_regime_history(symbol: str, last_n: int = 10) -> list:
    buf = _get_buffer(symbol)
    return list(buf.history)[-last_n:]

def get_current_regime(symbol: str) -> Tuple[MarketRegime, float]:
    buf = _REGIME_BUFFERS.get(symbol)
    if buf is None:
        return MarketRegime.UNDEFINED, 0.0
    return buf.current_regime, buf.current_confidence

def reset_regime(symbol: str) -> None:
    if symbol in _REGIME_BUFFERS:
        del _REGIME_BUFFERS[symbol]
        log.info("Regime buffer reset: %s", symbol)

def summarize_all_regimes() -> str:
    if not _REGIME_BUFFERS:
        return "⚪ Belum ada data regime. Jalankan strategy loop terlebih dahulu."

    lines = ["🌐 Market Regimes:"]
    for symbol, buf in sorted(_REGIME_BUFFERS.items()):
        emoji = buf.current_regime.emoji
        name  = buf.current_regime.display_name
        conf  = buf.current_confidence
        lines.append(f"  {emoji} {symbol:<12} {name:<20} (conf={conf:.0%})")

    return "\n".join(lines)


class MarketClassifier:
    def classify(self, observation: ObservationReport, db_manager=None) -> Tuple[MarketRegime, float]:
        return classify_from_observation(observation, db_manager=db_manager)
async def restore_regimes_from_db(symbols: list, db) -> None:
    """Load regime terakhir tiap symbol dari DB ke buffer saat startup."""
    restored = 0
    for symbol in symbols:
        try:
            record = await db.get_latest_regime(symbol)
            if record is None:
                continue
            buf = _get_buffer(symbol)
            buf.current_regime     = MarketRegime(record.regime)
            buf.current_confidence = record.regime_confidence
            buf.pending_regime     = MarketRegime.UNDEFINED
            buf.pending_count      = 0
            log.info(
                "Regime restored [%s]: %s (conf=%.2f) dari %s",
                symbol, record.regime, record.regime_confidence, record.timestamp,
            )
            restored += 1
        except Exception as exc:
            log.warning("Gagal restore regime [%s]: %s", symbol, exc)
    log.info("Regime restore selesai: %d/%d symbol.", restored, len(symbols))

def is_regime_transition(symbol: str) -> tuple:
    """Cek apakah regime symbol sedang dalam transisi (pending != current).
    Return: (is_transitioning: bool, from_regime: str, to_regime: str)
    """
    buf = _get_buffer(symbol)
    if buf.pending_regime == MarketRegime.UNDEFINED:
        return False, buf.current_regime.value, buf.current_regime.value
    return True, buf.current_regime.value, buf.pending_regime.value
