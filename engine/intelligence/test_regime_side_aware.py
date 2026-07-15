"""
engine/intelligence/test_regime_side_aware.py — Regression test untuk
perbaikan bias regime/threshold short (2026-07-14).

Dijalankan pakai stdlib unittest: python3 -m unittest
engine.intelligence.test_regime_side_aware -v

Latar belakang bug yang diperbaiki: trending_bear (regime ideal utk short)
SELALU tertolak -- dua kali lipat, independen satu sama lain:
1. is_tradeable_regime() cuma pernah cek regime.allows_long, apapun side-nya
   (tidak ada allows_short sama sekali sebelum fix ini).
2. DYNAMIC_THRESHOLD_MATRIX["*"]["trending_bear"] = 999.0 utk SEMUA profil
   (mustahil dicapai skor manapun) -- tidak ada matrix mirror utk short.
3. ALLOWED_REGIMES["*"] tidak pernah memuat "trending_bear" -- gate whitelist
   independen yang dicek SEBELUM allows_long/allows_short.

4 kombinasi wajib (diminta eksplisit sebelum fix dianggap selesai):
  long + trending_bull  -> harus TETAP jalan (regresi check)
  long + trending_bear  -> harus TETAP diblokir (regresi check)
  short + trending_bear -> harus SEKARANG bisa lolos (fix utama)
  short + trending_bull -> harus diblokir (mirror simetris)
"""

from __future__ import annotations

import unittest

from engine.core.models import MarketRegime
from engine.intelligence.classifier import is_tradeable_regime
from engine.profiles.thresholds import (
    get_dynamic_threshold,
    ALLOWED_REGIMES,
    ALLOWED_REGIMES_SHORT,
    DYNAMIC_THRESHOLD_MATRIX,
    DYNAMIC_THRESHOLD_MATRIX_SHORT,
)


class TestMarketRegimeAllowsProperties(unittest.TestCase):

    def test_allows_long_unchanged(self):
        self.assertTrue(MarketRegime.TRENDING_BULL.allows_long)
        self.assertTrue(MarketRegime.RANGING.allows_long)
        self.assertTrue(MarketRegime.VOLATILE_EXPANSION.allows_long)
        self.assertFalse(MarketRegime.TRENDING_BEAR.allows_long)
        self.assertFalse(MarketRegime.UNDEFINED.allows_long)

    def test_allows_short_mirrors_allows_long(self):
        self.assertTrue(MarketRegime.TRENDING_BEAR.allows_short)
        self.assertTrue(MarketRegime.RANGING.allows_short)
        self.assertTrue(MarketRegime.VOLATILE_EXPANSION.allows_short)
        self.assertFalse(MarketRegime.TRENDING_BULL.allows_short)
        self.assertFalse(MarketRegime.UNDEFINED.allows_short)


class TestIsTradeableRegimeFourCombos(unittest.TestCase):
    """4 kombinasi wajib dari instruksi user."""

    def test_long_trending_bull_still_works(self):
        ok, reason = is_tradeable_regime(
            MarketRegime.TRENDING_BULL, confidence=0.8,
            allowed_regimes=["trending_bull"], side="long",
        )
        self.assertTrue(ok, reason)

    def test_long_trending_bear_still_blocked(self):
        ok, reason = is_tradeable_regime(
            MarketRegime.TRENDING_BEAR, confidence=0.8,
            allowed_regimes=["trending_bull"], side="long",
        )
        self.assertFalse(ok)

    def test_short_trending_bear_now_works(self):
        ok, reason = is_tradeable_regime(
            MarketRegime.TRENDING_BEAR, confidence=0.8,
            allowed_regimes=["trending_bear"], side="short",
        )
        self.assertTrue(ok, reason)

    def test_short_trending_bull_now_blocked(self):
        ok, reason = is_tradeable_regime(
            MarketRegime.TRENDING_BULL, confidence=0.8,
            allowed_regimes=["trending_bear"], side="short",
        )
        self.assertFalse(ok)

    def test_default_side_is_long_backward_compat(self):
        """Caller lama yang tidak eksplisit kirim side (spot) harus dapat
        perilaku identik dengan sebelum parameter side ada."""
        ok_bull, _ = is_tradeable_regime(
            MarketRegime.TRENDING_BULL, confidence=0.8, allowed_regimes=["trending_bull"],
        )
        ok_bear, _ = is_tradeable_regime(
            MarketRegime.TRENDING_BEAR, confidence=0.8, allowed_regimes=["trending_bull"],
        )
        self.assertTrue(ok_bull)
        self.assertFalse(ok_bear)

    def test_allowed_regimes_whitelist_still_enforced_for_short(self):
        """allowed_regimes (whitelist per-profil) tetap dicek DULUAN,
        independen dari allows_short -- short di trending_bear TETAP
        tertolak kalau trending_bear tidak ada di whitelist yang dioper."""
        ok, reason = is_tradeable_regime(
            MarketRegime.TRENDING_BEAR, confidence=0.8,
            allowed_regimes=["ranging"],  # trending_bear sengaja tidak disertakan
            side="short",
        )
        self.assertFalse(ok)
        self.assertIn("tidak diizinkan", reason)

    def test_confidence_gate_unaffected_by_side(self):
        ok, _ = is_tradeable_regime(
            MarketRegime.TRENDING_BEAR, confidence=0.1,
            allowed_regimes=["trending_bear"], min_confidence=0.4, side="short",
        )
        self.assertFalse(ok)


class TestDynamicThresholdFourCombos(unittest.TestCase):

    def test_long_trending_bull_reasonable(self):
        v = get_dynamic_threshold("trend_follow", "trending_bull", side="long")
        self.assertEqual(v, 58.0)

    def test_long_trending_bear_effectively_impossible(self):
        v = get_dynamic_threshold("trend_follow", "trending_bear", side="long")
        self.assertEqual(v, 999.0)

    def test_short_trending_bear_now_reasonable(self):
        v = get_dynamic_threshold("trend_follow", "trending_bear", side="short")
        self.assertEqual(v, 58.0)  # mirror dari long trending_bull

    def test_short_trending_bull_now_impossible(self):
        v = get_dynamic_threshold("trend_follow", "trending_bull", side="short")
        self.assertEqual(v, 999.0)

    def test_default_side_long_backward_compat(self):
        v_explicit = get_dynamic_threshold("scalp_volatile", "ranging", side="long")
        v_default  = get_dynamic_threshold("scalp_volatile", "ranging")
        self.assertEqual(v_explicit, v_default)

    def test_neutral_regimes_identical_both_matrices(self):
        """ranging/volatile_expansion/undefined SENGAJA sama persis di kedua
        matrix -- cuma trending_bull/trending_bear yang ditukar."""
        for profile in DYNAMIC_THRESHOLD_MATRIX:
            for regime in ("ranging", "volatile_expansion", "undefined"):
                long_v  = DYNAMIC_THRESHOLD_MATRIX[profile][regime]
                short_v = DYNAMIC_THRESHOLD_MATRIX_SHORT[profile][regime]
                self.assertEqual(
                    long_v, short_v,
                    f"{profile}/{regime}: long={long_v} short={short_v} -- harusnya sama",
                )

    def test_all_profiles_have_short_matrix_entry(self):
        for profile in DYNAMIC_THRESHOLD_MATRIX:
            self.assertIn(profile, DYNAMIC_THRESHOLD_MATRIX_SHORT)


class TestAllowedRegimesShortMirror(unittest.TestCase):

    def test_all_profiles_have_short_entry(self):
        for profile in ALLOWED_REGIMES:
            self.assertIn(profile, ALLOWED_REGIMES_SHORT)

    def test_trending_bull_bear_swapped_per_profile(self):
        for profile, long_list in ALLOWED_REGIMES.items():
            short_list = ALLOWED_REGIMES_SHORT[profile]
            self.assertIn("trending_bull", long_list)
            self.assertNotIn("trending_bear", long_list)
            self.assertIn("trending_bear", short_list)
            self.assertNotIn("trending_bull", short_list)

    def test_neutral_regimes_identical_per_profile(self):
        for profile, long_list in ALLOWED_REGIMES.items():
            short_list = ALLOWED_REGIMES_SHORT[profile]
            long_neutral  = {r for r in long_list if r not in ("trending_bull", "trending_bear")}
            short_neutral = {r for r in short_list if r not in ("trending_bull", "trending_bear")}
            self.assertEqual(long_neutral, short_neutral, f"profile={profile}")


class TestGateRegimeIntegration(unittest.TestCase):
    """Integrasi lebih tinggi: commander.py::_gate_regime() + pemilihan
    allowed_regimes sesuai side di decide() -- dites lewat pemanggilan
    langsung _gate_regime (lebih ringan drpd construct seluruh prasyarat
    decide() end-to-end: supertrend gate, score/trigger gate, dst)."""

    def _make_signal(self, regime: MarketRegime, confidence: float = 0.8):
        from engine.core.models import ScoredSignal
        sig = ScoredSignal()
        sig.regime = regime
        sig.regime_confidence = confidence
        return sig

    def test_gate_regime_long_trending_bull(self):
        from engine.intelligence.commander import _gate_regime
        from engine.core.models import TradeDecision
        sig = self._make_signal(MarketRegime.TRENDING_BULL)
        decision = TradeDecision(scored_signal=sig)
        ok = _gate_regime(sig, decision, allowed_regimes=["trending_bull"], side="long")
        self.assertTrue(ok)

    def test_gate_regime_long_trending_bear_blocked(self):
        from engine.intelligence.commander import _gate_regime
        from engine.core.models import TradeDecision
        sig = self._make_signal(MarketRegime.TRENDING_BEAR)
        decision = TradeDecision(scored_signal=sig)
        ok = _gate_regime(sig, decision, allowed_regimes=["trending_bull"], side="long")
        self.assertFalse(ok)
        self.assertTrue(any("G2_REGIME" in g for g in decision.gates_failed))

    def test_gate_regime_short_trending_bear_now_ok(self):
        from engine.intelligence.commander import _gate_regime
        from engine.core.models import TradeDecision
        sig = self._make_signal(MarketRegime.TRENDING_BEAR)
        decision = TradeDecision(scored_signal=sig)
        ok = _gate_regime(sig, decision, allowed_regimes=["trending_bear"], side="short")
        self.assertTrue(ok)

    def test_gate_regime_short_trending_bull_blocked(self):
        from engine.intelligence.commander import _gate_regime
        from engine.core.models import TradeDecision
        sig = self._make_signal(MarketRegime.TRENDING_BULL)
        decision = TradeDecision(scored_signal=sig)
        ok = _gate_regime(sig, decision, allowed_regimes=["trending_bear"], side="short")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
