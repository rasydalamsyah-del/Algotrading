"""
engine/intelligence/test_scorer_suggest_sl_tp_side_aware.py -- Test untuk
bug-fix _suggest_sl_tp() (engine/intelligence/scorer.py), ditemukan saat
investigasi audit item #19 (forecast/diagnosa bidirectional futures).

[LATAR BELAKANG] _suggest_sl_tp() SEBELUMNYA tidak punya parameter side sama
sekali -- SL selalu di bawah harga, TP selalu di atas, regardless apakah
caller (score_signal(), yang SUDAH punya parameter side sejak proyek MTF)
sedang men-scoring sinyal long atau short. Field ini (SignalScore.
suggested_sl/suggested_tp) advisory-only -- dipakai forecast/dashboard,
BUKAN SL/TP order sungguhan (itu dihitung terpisah oleh
RiskManager.evaluate_order(), sudah side-aware) -- jadi TIDAK mempengaruhi
eksekusi trade nyata, tapi salah utk ditampilkan ke operator/dashboard utk
sinyal short.

    python3 -m unittest engine.intelligence.test_scorer_suggest_sl_tp_side_aware -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from engine.intelligence.scorer import _suggest_sl_tp


def _profile_cfg(atr_sl_mult=1.5, atr_tp_mult=2.5, quick_sl_pct=1.0, quick_tp_pct=2.0):
    return SimpleNamespace(
        atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult,
        quick_sl_pct=quick_sl_pct, quick_tp_pct=quick_tp_pct,
    )


class TestSuggestSlTpLongRegression(unittest.TestCase):
    """side="long" (default) HARUS identik persis dgn perilaku sebelum fix."""

    def test_default_side_is_long(self):
        cfg = _profile_cfg()
        sl, tp = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg)
        self.assertLess(sl, 100.0)
        self.assertGreater(tp, 100.0)
        self.assertEqual(sl, round(100.0 - 2.0 * 1.5, 8))
        self.assertEqual(tp, round(100.0 + 2.0 * 2.5, 8))

    def test_explicit_long_matches_default(self):
        cfg = _profile_cfg()
        default_result  = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg)
        explicit_result = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg, side="long")
        self.assertEqual(default_result, explicit_result)

    def test_long_no_atr_uses_quick_pct(self):
        cfg = _profile_cfg(quick_sl_pct=1.0, quick_tp_pct=2.0)
        sl, tp = _suggest_sl_tp(100.0, atr=None, profile_cfg=cfg)
        self.assertEqual(sl, round(100.0 * 0.99, 8))
        self.assertEqual(tp, round(100.0 * 1.02, 8))

    def test_long_invalid_price_returns_none(self):
        cfg = _profile_cfg()
        self.assertEqual(_suggest_sl_tp(0.0, atr=2.0, profile_cfg=cfg), (None, None))
        self.assertEqual(_suggest_sl_tp(-5.0, atr=2.0, profile_cfg=cfg), (None, None))


class TestSuggestSlTpShortNewBehavior(unittest.TestCase):
    """[Regresi kunci -- bug-fix] side="short" HARUS SL di ATAS harga, TP
    di BAWAH harga -- kebalikan dari long, BUKAN formula long yang direplikasi
    apa adanya (itu bug-nya)."""

    def test_short_sl_above_tp_below(self):
        cfg = _profile_cfg()
        sl, tp = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg, side="short")
        self.assertGreater(sl, 100.0, "SL short HARUS di atas harga entry")
        self.assertLess(tp, 100.0, "TP short HARUS di bawah harga entry")
        self.assertEqual(sl, round(100.0 + 2.0 * 1.5, 8))
        self.assertEqual(tp, round(100.0 - 2.0 * 2.5, 8))

    def test_short_no_atr_uses_quick_pct_mirrored(self):
        cfg = _profile_cfg(quick_sl_pct=1.0, quick_tp_pct=2.0)
        sl, tp = _suggest_sl_tp(100.0, atr=None, profile_cfg=cfg, side="short")
        self.assertEqual(sl, round(100.0 * 1.01, 8))
        self.assertEqual(tp, round(100.0 * 0.98, 8))

    def test_short_invalid_price_returns_none(self):
        cfg = _profile_cfg()
        self.assertEqual(_suggest_sl_tp(0.0, atr=2.0, profile_cfg=cfg, side="short"), (None, None))

    def test_short_not_identical_to_long_same_inputs(self):
        """'Bukan cuma beda angka' -- pastikan short genuinely dihitung
        beda arah, bukan formula long yang kebetulan sama."""
        cfg = _profile_cfg()
        sl_long,  tp_long  = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg, side="long")
        sl_short, tp_short = _suggest_sl_tp(100.0, atr=2.0, profile_cfg=cfg, side="short")
        self.assertNotEqual(sl_long, sl_short)
        self.assertNotEqual(tp_long, tp_short)
        # Simetri: jarak SL/TP dari harga entry sama besar, cuma arah dibalik.
        self.assertAlmostEqual(sl_short - 100.0, 100.0 - sl_long, places=8)
        self.assertAlmostEqual(100.0 - tp_short, tp_long - 100.0, places=8)


class TestSuggestSlTpScoreSignalThreading(unittest.TestCase):
    """Pastikan score_signal() genuinely meneruskan side ke _suggest_sl_tp()
    -- bukan cuma _suggest_sl_tp() yang diperbaiki tapi caller-nya tidak
    dipanggil dgn side yang benar (persis pola bug momentum/strength
    copy-omission yang sudah 2x ditemukan sebelumnya di proyek ini)."""

    def test_score_signal_source_passes_side_to_suggest_sl_tp(self):
        import inspect
        from engine.intelligence import scorer as scorer_module
        src = inspect.getsource(scorer_module.score_signal)
        self.assertIn(
            "_suggest_sl_tp(price, atr, profile_cfg, side=side)", src,
            "score_signal() HARUS meneruskan side= ke _suggest_sl_tp() -- "
            "kalau tidak, fix di _suggest_sl_tp() sendiri tidak pernah "
            "genuinely terpakai untuk sinyal short.",
        )


if __name__ == "__main__":
    unittest.main()
