"""
profiles/weights.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

log = logging.getLogger("profiles.weights")

Level1Weights = Dict[str, float]
Level2Weights = Dict[str, float]

def _validate_weights(
    weights: Dict[str, float],
    context: str,
    tolerance: float = 0.001,
) -> None:
    total = sum(weights.values())
    if not (1.0 - tolerance <= total <= 1.0 + tolerance):
        raise ValueError(
            f"Weight table '{context}' jumlahnya {total:.6f}, harus 1.0 (±{tolerance}). "
            f"Weights: {weights}. "
            f"SISTEM TIDAK BISA DIMULAI dengan weight yang tidak valid — "
            f"ini adalah bug kritis dalam konfigurasi."
        )

    for key, val in weights.items():
        if val < 0.0:
            raise ValueError(
                f"Weight '{key}' dalam '{context}' bernilai negatif ({val}). "
                f"Weight tidak boleh negatif."
            )

    for key, val in weights.items():
        if 0.0 < val < 0.005:
            log.warning(
                "Weight '%s' dalam '%s' sangat kecil (%.4f). "
                "Pastikan ini disengaja dan bukan typo.",
                key, context, val,
            )

def validate_all_weight_tables() -> None:
    for profile_name, w1 in LEVEL1_WEIGHTS.items():
        _validate_weights(w1, f"L1/{profile_name}")

    for profile_name, cats in LEVEL2_WEIGHTS.items():
        for cat_name, w2 in cats.items():
            _validate_weights(w2, f"L2/{profile_name}/{cat_name}")

    # [BUG-FIX KRITIS] Cross-check L1 vs L2 category coverage. Sebelumnya
    # validasi di atas cuma cek sum=1.0 PER TABEL YANG ADA — tidak pernah
    # cek apakah SEMUA kategori L1 punya pasangan L2. Inilah yang membuat
    # bug "mean_revert hilang 3 kategori L2 (oscillator/structure/orderbook)"
    # lolos tanpa terdeteksi sampai crash nyata di runtime (KeyError di
    # compute_category_score, tertangkap diam-diam oleh except umum di
    # strategy.py — mean_revert jadi tidak pernah bisa menghasilkan sinyal).
    # Sekarang validasi ini fail-fast di import time, bukan diam-diam di
    # runtime produksi.
    for profile_name, l1_cats in LEVEL1_WEIGHTS.items():
        l2_cats = LEVEL2_WEIGHTS.get(profile_name)
        if l2_cats is None:
            raise ValueError(
                f"Profile '{profile_name}' ada di LEVEL1_WEIGHTS tapi TIDAK "
                f"ADA SAMA SEKALI di LEVEL2_WEIGHTS. "
                f"SISTEM TIDAK BISA DIMULAI — ini bug kritis konfigurasi."
            )
        missing = set(l1_cats.keys()) - set(l2_cats.keys())
        if missing:
            raise ValueError(
                f"Profile '{profile_name}': kategori L1 {sorted(missing)} "
                f"TIDAK ADA pasangannya di LEVEL2_WEIGHTS. "
                f"compute_category_score() akan crash KeyError untuk kategori "
                f"ini kalau profile sedang dipakai trading. "
                f"SISTEM TIDAK BISA DIMULAI dengan weight table yang tidak "
                f"lengkap — ini adalah bug kritis dalam konfigurasi."
            )

    log.debug(
        "Semua weight table valid: %d profile × %d kategori.",
        len(LEVEL1_WEIGHTS),
        len(next(iter(LEVEL2_WEIGHTS.values()))) if LEVEL2_WEIGHTS else 0,
    )

LEVEL1_WEIGHTS: Dict[str, Level1Weights] = {

    "hodl_accumulate": {
        "trend":      0.33,
        "momentum":   0.12,
        "strength":   0.17,
        "volatility": 0.08,
        "pattern":    0.10,
        "oscillator": 0.07,
        "structure":  0.08,
        "orderbook":  0.05,
    },

    "trend_follow": {
        "trend": 0.23,
        "momentum": 0.1604,
        "strength": 0.2673,
        "volatility": 0.0856,
        "pattern": 0.0428,
        "oscillator": 0.0642,
        "structure": 0.0963,
        "orderbook": 0.0535,
    },

    "breakout_swift": {
        "trend":      0.15,
        "momentum":   0.20,
        "strength":   0.28,
        "volatility": 0.04,
        "pattern":    0.10,
        "oscillator": 0.06,
        "structure":  0.05,
        "orderbook":  0.12,
    },

    "scalp_volatile": {
        "trend":      0.15,
        "momentum":   0.32,
        "strength":   0.20,
        "volatility": 0.08,
        "pattern":    0.04,
        "oscillator": 0.08,
        "structure":  0.03,
        "orderbook":  0.10,
    },

    "mean_revert": {
        "trend":      0.08,
        "momentum":   0.32,
        "strength":   0.16,
        "volatility": 0.08,
        "pattern":    0.15,
        "oscillator": 0.10,
        "structure":  0.07,
        "orderbook":  0.04,
    },

    "extreme_momentum": {
        "trend":      0.15,
        "momentum":   0.25,
        "strength":   0.32,
        "volatility": 0.04,
        "pattern":    0.04,
        "oscillator": 0.07,
        "structure":  0.04,
        "orderbook":  0.09,
    },
}

LEVEL2_WEIGHTS: Dict[str, Dict[str, Level2Weights]] = {

    "hodl_accumulate": {
        "trend": {
            "ema_stack":   0.40,
            "cross":       0.10,
            "supertrend":  0.35,
            "vwap":        0.15,
        },
        "momentum": {
            "rsi":      0.55,
            "macd":     0.30,
            "stochrsi": 0.15,
        },
        "strength": {
            "adx":    0.30,
            "di":     0.20,
            "volume": 0.40,
            "mfi":    0.10,
        },
        "volatility": {
            "bb":      0.35,
            "squeeze": 0.25,
            "atr":     0.40,
        },
        "pattern": {
            "pattern_score": 0.70,
            "context_score": 0.30,
        },
        "oscillator": {
            "cci": 0.30, "williams": 0.20, "roc": 0.50,
        },
        "structure": {
            "ichimoku": 0.40, "sar": 0.25, "pivot": 0.25, "fibonacci": 0.10,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },

    "trend_follow": {
        "trend": {
            "ema_stack":   0.35,
            "cross":       0.15,
            "supertrend":  0.40,
            "vwap":        0.10,
        },
        "momentum": {
            "rsi":      0.40,
            "macd":     0.40,
            "stochrsi": 0.20,
        },
        "strength": {
            "adx":    0.40,
            "di":     0.25,
            "volume": 0.25,
            "mfi":    0.10,
        },
        "volatility": {
            "bb":      0.30,
            "squeeze": 0.20,
            "atr":     0.50,
        },
        "pattern": {
            "pattern_score": 0.60,
            "context_score": 0.40,
        },
        "oscillator": {
            "cci": 0.25, "williams": 0.15, "roc": 0.60,
        },
        "structure": {
            "ichimoku": 0.45, "sar": 0.30, "pivot": 0.20, "fibonacci": 0.05,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },

    "breakout_swift": {
        "trend": {
            "ema_stack":   0.45,
            "cross":       0.10,
            "supertrend":  0.35,
            "vwap":        0.10,
        },
        "momentum": {
            "rsi":      0.35,
            "macd":     0.45,
            "stochrsi": 0.20,
        },
        "strength": {
            "adx":    0.25,
            "di":     0.15,
            "volume": 0.50,
            "mfi":    0.10,
        },
        "volatility": {
            "bb":      0.30,
            "squeeze": 0.35,
            "atr":     0.35,
        },
        "pattern": {
            "pattern_score": 0.65,
            "context_score": 0.35,
        },
        "oscillator": {
            "cci": 0.25, "williams": 0.15, "roc": 0.60,
        },
        "structure": {
            "ichimoku": 0.30, "sar": 0.20, "pivot": 0.30, "fibonacci": 0.20,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },

    "scalp_volatile": {
        "trend": {
            "ema_stack":   0.50,
            "cross":       0.15,
            "supertrend":  0.30,
            "vwap":        0.05,
        },
        "momentum": {
            "rsi":      0.40,
            "macd":     0.35,
            "stochrsi": 0.25,
        },
        "strength": {
            "adx":    0.20,
            "di":     0.15,
            "volume": 0.55,
            "mfi":    0.10,
        },
        "volatility": {
            "bb":      0.30,
            "squeeze": 0.25,
            "atr":     0.45,
        },
        "pattern": {
            "pattern_score": 0.55,
            "context_score": 0.45,
        },
        "oscillator": {
            "cci": 0.35, "williams": 0.30, "roc": 0.35,
        },
        "structure": {
            "ichimoku": 0.20, "sar": 0.40, "pivot": 0.30, "fibonacci": 0.10,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },

    "mean_revert": {
        "trend": {
            "ema_stack":   0.30,
            "cross":       0.25,
            "supertrend":  0.25,
            "vwap":        0.20,
        },
        "momentum": {
            "rsi":      0.60,
            "macd":     0.25,
            "stochrsi": 0.15,
        },
        "strength": {
            "adx":    0.15,
            "di":     0.20,
            "volume": 0.50,
            "mfi":    0.15,
        },
        "volatility": {
            "bb":      0.50,
            "squeeze": 0.15,
            "atr":     0.35,
        },
        "pattern": {
            "pattern_score": 0.55,
            "context_score": 0.45,
        },
        # [BUG-FIX KRITIS] 3 kategori ini sebelumnya HILANG dari mean_revert,
        # padahal LEVEL1_WEIGHTS["mean_revert"] punya semua 8 kategori dan
        # _calc_weighted_breakdown() di scorer.py hardcode loop ke 8 kategori
        # utk SEMUA profil tanpa kecuali. Akibatnya setiap kali primary
        # trigger mean_revert terpenuhi -> compute_category_score() crash
        # KeyError di kategori 'oscillator' -> tertangkap diam-diam oleh
        # except umum di strategy.py -> mean_revert TIDAK PERNAH menghasilkan
        # sinyal trading sama sekali, tanpa ada yang sadar (cuma log ERROR).
        # Bobot di bawah disesuaikan filosofi mean-reversion: oscillator
        # overbought/oversold seimbang, structure berat ke pivot/fibonacci
        # (bukan ichimoku/supertrend yang trend-following).
        "oscillator": {
            "cci": 0.35, "williams": 0.30, "roc": 0.35,
        },
        "structure": {
            "ichimoku": 0.15, "sar": 0.20, "pivot": 0.45, "fibonacci": 0.20,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },

    "extreme_momentum": {
        "trend": {
            "ema_stack":   0.55,
            "cross":       0.05,
            "supertrend":  0.35,
            "vwap":        0.05,
        },
        "momentum": {
            "rsi":      0.45,
            "macd":     0.40,
            "stochrsi": 0.15,
        },
        "strength": {
            "adx":    0.20,
            "di":     0.10,
            "volume": 0.65,
            "mfi":    0.05,
        },
        "volatility": {
            "bb":      0.20,
            "squeeze": 0.15,
            "atr":     0.65,
        },
        "pattern": {
            "pattern_score": 0.50,
            "context_score": 0.50,
        },
        "oscillator": {
            "cci": 0.20, "williams": 0.10, "roc": 0.70,
        },
        "structure": {
            "ichimoku": 0.25, "sar": 0.40, "pivot": 0.25, "fibonacci": 0.10,
        },
        "orderbook": {
            "ob_score": 1.00,
        },
    },
}

REGIME_MODIFIERS_PER_PROFILE: Dict[str, Dict[str, float]] = {
    "hodl_accumulate": {
        "trending_bull":       1.00,
        "trending_bear":       0.00,
        "ranging":             0.75,
        "volatile_expansion":  0.60,
        "undefined":           0.70,
    },
    "trend_follow": {
        "trending_bull":       1.00,
        "trending_bear":       0.00,
        "ranging":             0.50,
        "volatile_expansion":  0.65,
        "undefined":           0.65,
    },
    "breakout_swift": {
        "trending_bull":       1.00,
        "trending_bear":       0.00,
        "ranging":             0.80,
        "volatile_expansion":  0.85,
        "undefined":           0.70,
    },
    "scalp_volatile": {
        "trending_bull":       1.00,
        "trending_bear":       0.00,
        "ranging":             0.85,
        "volatile_expansion":  0.75,
        "undefined":           0.70,
    },
    "mean_revert": {
        "trending_bull":       0.80,
        "trending_bear":       0.00,
        "ranging":             1.00,
        "volatile_expansion":  0.50,
        "undefined":           0.75,
    },
    "extreme_momentum": {
        "trending_bull":       1.00,
        "trending_bear":       0.00,
        "ranging":             0.40,
        "volatile_expansion":  1.00,
        "undefined":           0.50,
    },
}

def get_level1_weights(profile_name: str) -> Level1Weights:
    if profile_name not in LEVEL1_WEIGHTS:
        raise KeyError(
            f"Profile '{profile_name}' tidak ada di LEVEL1_WEIGHTS. "
            f"Profile yang tersedia: {list(LEVEL1_WEIGHTS.keys())}"
        )
    return LEVEL1_WEIGHTS[profile_name]

def get_level2_weights(profile_name: str, category: str) -> Level2Weights:
    if profile_name not in LEVEL2_WEIGHTS:
        raise KeyError(
            f"Profile '{profile_name}' tidak ada di LEVEL2_WEIGHTS."
        )
    cat_weights = LEVEL2_WEIGHTS[profile_name]
    if category not in cat_weights:
        raise KeyError(
            f"Category '{category}' tidak ada dalam profile '{profile_name}'. "
            f"Category yang tersedia: {list(cat_weights.keys())}"
        )
    return cat_weights[category]

def get_regime_modifier(profile_name: str, regime_value: str) -> float:
    profile_mods = REGIME_MODIFIERS_PER_PROFILE.get(profile_name, {})
    return profile_mods.get(regime_value, 0.75)

def compute_weighted_score(
    profile_name: str,
    category_scores: Dict[str, float],
    regime_value: str = "undefined",
) -> Tuple[float, Dict[str, float]]:
    l1_weights      = get_level1_weights(profile_name)
    regime_modifier = get_regime_modifier(profile_name, regime_value)

    weighted_breakdown: Dict[str, float] = {}
    raw_total = 0.0

    for category, weight in l1_weights.items():
        score = category_scores.get(category, 50.0)
        weighted = score * weight
        weighted_breakdown[category] = round(weighted, 4)
        raw_total += weighted

    final_total = raw_total * regime_modifier

    final_total = max(0.0, min(100.0, round(final_total, 4)))

    return final_total, weighted_breakdown

def compute_category_score(
    profile_name: str,
    category: str,
    indicator_scores: Dict[str, float],
) -> float:
    l2_weights = get_level2_weights(profile_name, category)

    available = {
        k: v for k, v in l2_weights.items()
        if k in indicator_scores
    }

    if not available:
        return 50.0

    total_available_weight = sum(available.values())
    if total_available_weight < 1e-9:
        return 50.0

    weighted_sum = 0.0
    for indicator, weight in available.items():
        score       = indicator_scores[indicator]
        adjusted_w  = weight / total_available_weight
        weighted_sum += score * adjusted_w

    return max(0.0, min(100.0, round(weighted_sum, 4)))

def update_level2_weight(
    profile_name: str,
    category: str,
    indicator: str,
    new_weight: float,
) -> Dict[str, float]:
    if new_weight < 0:
        raise ValueError(f"new_weight tidak boleh negatif: {new_weight}")

    current = dict(get_level2_weights(profile_name, category))

    if indicator not in current:
        raise KeyError(
            f"Indicator '{indicator}' tidak ada dalam L2/{profile_name}/{category}. "
            f"Available: {list(current.keys())}"
        )

    current[indicator] = new_weight

    total = sum(current.values())
    if total < 1e-9:
        raise ValueError(
            f"Semua weight menjadi 0 setelah update. "
            f"Tidak bisa normalize weight table kosong."
        )

    normalized = {k: round(v / total, 6) for k, v in current.items()}

    rounding_err = 1.0 - sum(normalized.values())
    if abs(rounding_err) > 1e-9:
        largest_key = max(normalized, key=lambda k: normalized[k])
        normalized[largest_key] = round(normalized[largest_key] + rounding_err, 6)

    _validate_weights(normalized, f"updated L2/{profile_name}/{category}")

    return normalized

try:
    validate_all_weight_tables()
except ValueError as _weight_err:
    raise ImportError(
        f"FATAL: Weight table tidak valid — sistem tidak bisa dimulai.\n"
        f"Detail: {_weight_err}\n"
        f"Perbaiki profiles/weights.py sebelum menjalankan bot."
    ) from _weight_err
