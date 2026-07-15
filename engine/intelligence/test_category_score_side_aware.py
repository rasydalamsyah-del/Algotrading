"""
engine/intelligence/test_category_score_side_aware.py — Regression &
mirror-correctness tests untuk perbaikan bias long di 8 kategori skor
(trend/momentum/strength/volatility/pattern/oscillator/structure/orderbook).

Satu file, ditambah test class per batch (batch 0 = foundation, batch 1 =
pattern_score, dst) supaya histori pengerjaan tetap terlihat & tiap batch
bisa dijalankan/direview terpisah:

    python3 -m unittest engine.intelligence.test_category_score_side_aware -v

Latar belakang: 19 dari 24 sub-score kategori yang dipakai score_signal()
ternyata cuma reward kondisi bullish (skor tinggi = bagus buat long), tanpa
ada logic mirror utk short sama sekali -- meski regime/threshold sudah
diperbaiki sebelumnya, total_score itu sendiri tetap bias ke long. Fix
dikerjakan per-kategori (batch 2-7), memakai fondasi (batch 0) + contoh
kasus termudah (batch 1, pattern_score) yang dikerjakan duluan.
"""

from __future__ import annotations

import itertools
import random
import unittest

import pandas as pd

from engine.core.models import IndicatorSet, PatternContext, PatternType, clamp_score
from engine.intelligence.scorer import _extract_indicator_scores, _pick_side_score
from engine.indicators.patterns import score_pattern, _score_single_pattern
from engine.indicators.strength import (
    _score_di, _score_volume, _score_adx, _score_mfi,
    calculate_adx, calculate_volume_analysis, calculate_money_flow,
)
from engine.indicators.momentum import (
    _score_rsi, _score_macd, _score_stochrsi,
    calculate_rsi_enhanced, calculate_macd_enhanced, calculate_stochastic_rsi,
)
from engine.indicators.oscillators import score_cci, score_williams_r, score_roc
from engine.indicators.trend import (
    _score_supertrend_direction, calculate_supertrend, score_trend,
    _score_ema_stack, calculate_ema_stack,
    _score_cross, calculate_golden_dead_cross,
    _score_vwap_zone, calculate_vwap, calculate_vwap_multiday,
)
from engine.indicators.structure import (
    score_ichimoku, score_structure, score_sar,
    score_pivot, calculate_pivot_points,
    score_fibonacci,
)
from engine.indicators.orderbook import (
    _score_imbalance, _score_whale, _score_absorption,
    calculate_orderbook, score_orderbook_data, reset_state,
    IMBALANCE_BULL, IMBALANCE_BEAR,
)


class TestBatch0PickSideScoreHelper(unittest.TestCase):
    """_pick_side_score(): selektor generik, dipakai SEMUA kategori."""

    def test_long_always_reads_base_field_even_if_short_exists(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend.supertrend_score = 85.0
        iset.trend.supertrend_score_short = 15.0
        self.assertEqual(
            _pick_side_score(iset.trend, "supertrend_score", "long"), 85.0
        )

    def test_short_reads_short_field_when_present(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend.supertrend_score = 85.0
        iset.trend.supertrend_score_short = 15.0
        self.assertEqual(
            _pick_side_score(iset.trend, "supertrend_score", "short"), 15.0
        )

    def test_short_falls_back_to_long_when_short_field_missing(self):
        """Kategori yang BELUM dapat batch mirror-nya -- field _short belum
        pernah di-set sama sekali (bukan cuma None eksplisit)."""
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.strength.adx_score = 62.0
        self.assertEqual(
            _pick_side_score(iset.strength, "adx_score", "short"), 62.0
        )

    def test_short_falls_back_when_short_field_explicitly_none(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.strength.adx_score = 62.0
        iset.strength.adx_score_short = None
        self.assertEqual(
            _pick_side_score(iset.strength, "adx_score", "short"), 62.0
        )


class TestBatch0ExtractIndicatorScoresFoundation(unittest.TestCase):
    """Sebelum SATU PUN kategori dapat field _short (kondisi hari ini,
    batch 0 baru menaruh fondasi) -- side='long' dan side='short' WAJIB
    menghasilkan dict IDENTIK. Ini bukti utama fallback mechanism aman utk
    rollout bertahap: sebelum batch 1-7 jalan, tidak ada perubahan nilai
    sama sekali di kedua arah."""

    def _make_populated_iset(self) -> IndicatorSet:
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend.ema_stack_score = 71.0
        iset.trend.cross_score = 68.0
        iset.trend.supertrend_score = 85.0
        iset.trend.vwap_score = 72.0
        iset.momentum.rsi_score = 64.0
        iset.momentum.macd_score = 78.0
        iset.momentum.stoch_score = 60.0
        iset.strength.adx_score = 55.0
        iset.strength.di_score = 80.0
        iset.strength.volume_score = 66.0
        iset.strength.mfi_score = 58.0
        iset.volatility.bb_score = 75.0
        iset.volatility.squeeze_score = 50.0
        iset.volatility.atr_score = 62.0
        iset.patterns.pattern_score = 70.0
        iset.patterns.context_score = 70.0
        iset.oscillators.cci_score = 80.0
        iset.oscillators.williams_r_score = 85.0
        iset.oscillators.roc_score = 74.0
        iset.oscillators.cci_trend = "bullish"
        iset.oscillators.willr_trend = "rising"
        iset.oscillators.roc_crossover = "bullish"
        iset.oscillators.cci_divergence = 5.0
        iset.structure.ichimoku_score = 78.0
        iset.structure.sar_score = 76.0
        iset.structure.pivot_score = 67.0
        iset.structure.fib_score = 62.0
        iset.orderbook.orderbook_score = 71.0
        return iset

    def test_long_side_unchanged_vs_pre_fix_shape(self):
        iset = self._make_populated_iset()
        result = _extract_indicator_scores(iset, side="long")
        self.assertEqual(result["trend"]["supertrend"], 85.0)
        self.assertEqual(result["momentum"]["rsi"], 64.0)
        self.assertEqual(result["strength"]["di"], 80.0)
        self.assertEqual(result["volatility"]["bb"], 75.0)
        self.assertEqual(result["pattern"]["pattern_score"], 70.0)
        self.assertEqual(result["oscillator"]["cci"], 80.0)
        self.assertEqual(result["oscillator"]["cci_trend"], "bullish")
        self.assertEqual(result["structure"]["ichimoku"], 78.0)
        self.assertEqual(result["orderbook"]["ob_score"], 71.0)

    def test_default_side_is_long(self):
        """Caller lama (kalau ada) yang tidak eksplisit kirim side harus
        dapat perilaku identik dengan side='long'."""
        iset = self._make_populated_iset()
        explicit = _extract_indicator_scores(iset, side="long")
        default = _extract_indicator_scores(iset)
        self.assertEqual(explicit, default)

    def test_short_identical_to_long_before_any_mirror_batch_lands(self):
        """INI test paling penting di batch 0: krn belum ada satupun field
        _short yang di-populate di manapun di codebase, short HARUS
        fallback ke long di SEMUA 24 key, tanpa terkecuali."""
        iset = self._make_populated_iset()
        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")
        self.assertEqual(long_result, short_result)

    def test_all_8_categories_present_both_sides(self):
        iset = self._make_populated_iset()
        for side in ("long", "short"):
            result = _extract_indicator_scores(iset, side=side)
            self.assertEqual(
                set(result.keys()),
                {"trend", "momentum", "strength", "volatility",
                 "pattern", "oscillator", "structure", "orderbook"},
            )


def _make_ohlcv(bars):
    idx = pd.date_range("2026-01-01", periods=len(bars), freq="15min")
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


# Fixture: bar[-3]=bearish kecil, bar[-2]=bullish engulfing besar -- memicu
# BULLISH_ENGULFING pada _detect_engulfing_raw() (butuh o_curr<=c_prev,
# c_curr>=o_prev, body_curr >= body_prev*COVERAGE_RATIO).
_BULLISH_ENGULFING_BARS = [
    (100, 101, 94, 99, 1000),
    (100, 101, 94, 99, 1000),
    (100, 101, 94, 95, 1000),   # prev: body=5, bearish
    (94, 107, 93, 106, 3000),   # curr: body=12, bullish, engulfs prev
    (106, 108, 104, 105, 1000),
]

# Mirror persis dari fixture bullish di atas (harga di-refleksikan).
_BEARISH_ENGULFING_BARS = [
    (100, 101, 94, 99, 1000),
    (100, 101, 94, 99, 1000),
    (95, 101, 94, 100, 1000),   # prev: body=5, bullish
    (101, 102, 88, 89, 3000),   # curr: body=12, bearish, engulfs prev
    (89, 90, 85, 86, 1000),
]


class TestBatch1PatternScoreShort(unittest.TestCase):
    """pattern_score (Class B): tidak ada reformulasi -- cuma
    pattern_score_short = clamp_score(100 - pattern_score), krn
    _score_single_pattern() SUDAH mirror-symmetric by construction."""

    # ── 1. Long regression: pattern_score sendiri TIDAK BOLEH berubah ──────

    def test_long_pattern_score_unchanged_bullish_fixture(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertEqual(res.primary_pattern, PatternType.BULLISH_ENGULFING)
        self.assertEqual(res.pattern_score, 90.0)

    def test_long_pattern_score_unchanged_bearish_fixture(self):
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertEqual(res.primary_pattern, PatternType.BEARISH_ENGULFING)
        self.assertEqual(res.pattern_score, 10.0)

    # ── 2. Swap-symmetry, multi-titik (bukan cuma satu angka) ───────────────

    def test_short_is_exact_complement_bullish_fixture(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertEqual(res.pattern_score_short, clamp_score(100.0 - res.pattern_score))
        self.assertEqual(res.pattern_score_short, 10.0)

    def test_short_is_exact_complement_bearish_fixture(self):
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertEqual(res.pattern_score_short, clamp_score(100.0 - res.pattern_score))
        self.assertEqual(res.pattern_score_short, 90.0)

    def test_complement_holds_across_score_range_independent_of_clamp_score_impl(self):
        """Verifikasi rumus x -> 100-x scr independen (angka literal, bukan
        cuma re-panggil clamp_score yang sama dgn kode produksi -- supaya
        test ini tetap mendeteksi kalau konstanta/formula produksi berubah
        tanpa sengaja, bukan tautologi)."""
        cases = [
            (0.0, 100.0), (10.0, 90.0), (25.0, 75.0), (50.0, 50.0),
            (62.3, 37.7), (90.0, 10.0), (100.0, 0.0),
        ]
        for pattern_score, expected_short in cases:
            got = clamp_score(100.0 - pattern_score)
            self.assertAlmostEqual(got, expected_short, places=6)

    def test_strong_bearish_pattern_scores_high_on_short_not_just_different(self):
        """Bukan cuma 'beda angka' -- pattern bearish kuat HARUS scoring
        TINGGI di pattern_score_short (indikasi short bagus), bukan rendah."""
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertGreater(res.pattern_score_short, 50.0)
        self.assertLess(res.pattern_score, 50.0)

    def test_strong_bullish_pattern_scores_low_on_short(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertLess(res.pattern_score_short, 50.0)
        self.assertGreater(res.pattern_score, 50.0)

    def test_complement_holds_for_multiple_quality_levels_via_score_single_pattern(self):
        """Multi-titik langsung di level _score_single_pattern() (dipanggil
        score_pattern() di dalamnya) -- beberapa nilai quality, bukan cuma
        yang kebetulan dihasilkan fixture OHLCV di atas. Bandingkan pasangan
        bullish/bearish pada quality SAMA -- harus persis komplementer satu
        sama lain (bukan cuma masing-masing dibanding dirinya sendiri)."""
        prev_bull_scores = []
        for quality in (0.2, 0.5, 0.8, 1.0):
            bull_score = _score_single_pattern(
                PatternType.BULLISH_ENGULFING, quality,
                PatternContext.MID_RANGE, volume_confirmed=True,
                higher_tf_aligned=None,
            )
            bear_score = _score_single_pattern(
                PatternType.BEARISH_ENGULFING, quality,
                PatternContext.MID_RANGE, volume_confirmed=True,
                higher_tf_aligned=None,
            )
            # _score_single_pattern() sendiri (bukan kode baru) sudah mirror
            # -- dipakai di sini sbg bukti independen bahwa asumsi "100-x
            # exact" di balik fix pattern_score_short memang berlandaskan
            # simetri nyata, bukan kebetulan di satu titik data saja.
            self.assertAlmostEqual(bull_score, 100.0 - bear_score, places=6,
                                    msg=f"quality={quality}")
            # Skor harus benar-benar bergerak seiring quality (bukan konstan)
            # -- kalau semua quality menghasilkan angka sama, test di atas
            # jadi kurang berarti (cuma kebetulan simetris di satu nilai).
            if prev_bull_scores:
                self.assertNotEqual(bull_score, prev_bull_scores[-1])
            prev_bull_scores.append(bull_score)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_when_no_pattern_detected(self):
        """Candle biasa berturut-turut warna sama, body/wick moderat --
        tidak memicu engulfing/hammer/doji/marubozu apa pun ->
        primary_pattern=NONE -> pattern_score=NETRAL utk kedua sisi."""
        normal_bars = [
            (100, 102, 98, 101, 1000),
            (101, 103, 99, 102, 1000),
            (102, 104, 100, 103, 1000),
            (103, 105, 101, 104, 1000),
            (104, 106, 102, 105, 1000),
        ]
        df = _make_ohlcv(normal_bars)
        res = score_pattern(df, context=PatternContext.MID_RANGE)
        self.assertEqual(res.primary_pattern, PatternType.NONE)
        self.assertEqual(res.pattern_score, 50.0)
        self.assertEqual(res.pattern_score_short, 50.0)

    def test_neutral_both_sides_when_insufficient_bars(self):
        df = _make_ohlcv([(100, 101, 99, 100, 1000), (100, 101, 99, 100, 1000)])
        res = score_pattern(df)
        self.assertEqual(res.pattern_score, 50.0)
        self.assertEqual(res.pattern_score_short, 50.0)

    # ── Integrasi dgn _pick_side_score() / _extract_indicator_scores() ─────

    def test_extract_indicator_scores_now_reads_real_short_value(self):
        """Beda dgn batch 0 (short selalu fallback ke long krn field _short
        belum ada) -- SEKARANG pattern kategori harus baca nilai short yang
        BENAR-BENAR BEDA dari long, membuktikan _pick_side_score() otomatis
        mulai pakai field baru tanpa perlu ubah scorer.py lagi."""
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        pat = score_pattern(df, context=PatternContext.MID_RANGE)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.patterns = pat

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["pattern"]["pattern_score"], 10.0)
        self.assertEqual(short_result["pattern"]["pattern_score"], 90.0)
        self.assertNotEqual(
            long_result["pattern"]["pattern_score"],
            short_result["pattern"]["pattern_score"],
        )


def _make_trend_df(n, direction=1, start=100.0, step=1.0, vol=1000):
    """OHLCV dgn tren harga jelas satu arah -- utk memicu +DI atau -DI
    dominan pada _calc_directional_movement()."""
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    bars = []
    for i in range(n):
        c = start + direction * step * i
        o = c - direction * step * 0.3
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        bars.append((o, h, l, c, vol))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


def _make_obv_trend_df(n, obv_direction=1, ratio_bump=1.5):
    """OHLCV dgn OBV rising (obv_direction=1) atau falling (=-1), plus
    volume ratio yg SAMA persis di kedua arah (5 bar terakhir dinaikkan
    ratio_bump x) -- supaya base/magnitude term bisa dibandingkan apple-to-
    apple, cuma OBV-trend term yang beda antar fixture."""
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    bars = []
    base_vol = 1000
    for i in range(n):
        c = 100 + obv_direction * 0.1 * i
        o = c - obv_direction * 0.05
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        v = base_vol if i < n - 5 else base_vol * ratio_bump
        bars.append((o, h, l, c, v))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


class TestBatch2StrengthDiVolumeShort(unittest.TestCase):
    """strength category: di_score (full mirror, plus_di/minus_di swap),
    volume_score (partial -- cuma OBV-trend term), adx_score TETAP tidak
    disentuh (genuinely direction-agnostic)."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_di_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(40, direction=1)
        res = calculate_adx(df)
        self.assertGreater(res.plus_di, res.minus_di)
        self.assertGreater(res.di_score, 50.0)

    def test_long_volume_score_unchanged_rising_obv_fixture(self):
        df = _make_obv_trend_df(30, obv_direction=1)
        res = calculate_volume_analysis(df)
        self.assertEqual(res.obv_trend, "rising")
        self.assertGreater(res.volume_score, 50.0)

    def test_long_adx_score_unchanged_by_this_batch(self):
        """adx_score dihitung dari fungsi yg sama sekali tidak disentuh
        batch ini -- murni regression check nilainya identik utk uptrend
        maupun downtrend (buktinya genuinely direction-agnostic)."""
        df_up = _make_trend_df(40, direction=1)
        df_down = _make_trend_df(40, direction=-1)
        res_up = calculate_adx(df_up)
        res_down = calculate_adx(df_down)
        self.assertAlmostEqual(res_up.adx_score, res_down.adx_score, places=6)

    # ── 2. Swap-symmetry, multi-titik ───────────────────────────────────────

    def test_di_score_swap_symmetry_multiple_ratios(self):
        """_score_di() langsung, beberapa pasangan plus_di/minus_di --
        long(a,b) harus persis == short(b,a) (swap peran, bukan cuma
        komplemen kebetulan)."""
        pairs = [(80.0, 20.0), (60.0, 40.0), (95.0, 5.0), (30.0, 70.0), (10.0, 90.0)]
        for plus_di, minus_di in pairs:
            long_val = _score_di(plus_di, minus_di, side="long")
            short_val_swapped_input = _score_di(minus_di, plus_di, side="long")
            short_val_via_side = _score_di(plus_di, minus_di, side="short")
            self.assertAlmostEqual(
                short_val_via_side, short_val_swapped_input, places=9,
                msg=f"plus_di={plus_di} minus_di={minus_di}: side='short' harus "
                    f"sama dgn manually swap plus_di<->minus_di lalu side='long'",
            )
            self.assertAlmostEqual(long_val, 100.0 - short_val_via_side, places=9)

    def test_di_score_short_high_when_minus_di_dominant(self):
        """Bukan cuma 'beda angka' -- minus_di dominan (bearish kuat) HARUS
        scoring TINGGI di sisi short."""
        df_down = _make_trend_df(40, direction=-1)
        res = calculate_adx(df_down)
        self.assertGreater(res.minus_di, res.plus_di)
        self.assertGreater(res.di_score_short, 50.0)
        self.assertLess(res.di_score, 50.0)

    def test_volume_score_obv_term_swap_symmetry(self):
        """base (ratio ladder) + climax penalty harus IDENTIK antara rising
        vs falling fixture (magnitude-only) -- cuma selisih 16 poin (±8 x2)
        dari term OBV yang di-swap."""
        df_rising = _make_obv_trend_df(30, obv_direction=1)
        df_falling = _make_obv_trend_df(30, obv_direction=-1)
        res_r = calculate_volume_analysis(df_rising)
        res_f = calculate_volume_analysis(df_falling)

        self.assertAlmostEqual(res_r.volume_ratio, res_f.volume_ratio, places=6)
        # rising-long == falling-short (obv term sama-sama +8), dan sebaliknya
        self.assertAlmostEqual(res_r.volume_score, res_f.volume_score_short, places=6)
        self.assertAlmostEqual(res_f.volume_score, res_r.volume_score_short, places=6)

    def test_volume_score_short_high_when_obv_falling(self):
        df_falling = _make_obv_trend_df(30, obv_direction=-1)
        res = calculate_volume_analysis(df_falling)
        self.assertEqual(res.obv_trend, "falling")
        self.assertGreater(res.volume_score_short, res.volume_score)

    def test_volume_magnitude_term_direction_agnostic_across_ratios(self):
        """base ladder (dari `ratio`) + climax penalty HARUS identik long vs
        short di banyak titik ratio berbeda -- hanya obv_trend='flat' dipakai
        (term directional netral) supaya base murni yang diuji."""
        for ratio in (0.3, 0.9, 1.4, 2.5, 4.0, 6.0):
            spike = ratio >= 3.0
            climax = ratio >= 5.0
            long_val = _score_volume(ratio, spike, climax, "flat", side="long")
            short_val = _score_volume(ratio, spike, climax, "flat", side="short")
            self.assertAlmostEqual(long_val, short_val, places=9, msg=f"ratio={ratio}")

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_di_score_neutral_both_sides_when_no_directional_movement(self):
        long_val = _score_di(0.0, 0.0, side="long")
        short_val = _score_di(0.0, 0.0, side="short")
        self.assertEqual(long_val, 50.0)
        self.assertEqual(short_val, 50.0)

    def test_di_score_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(5, direction=1)  # jauh di bawah min_bars ADX
        res = calculate_adx(df)
        self.assertEqual(res.di_score, 50.0)
        self.assertEqual(res.di_score_short, 50.0)

    def test_volume_score_neutral_both_sides_obv_flat(self):
        long_val = _score_volume(1.0, False, False, "flat", side="long")
        short_val = _score_volume(1.0, False, False, "flat", side="short")
        self.assertEqual(long_val, short_val)

    def test_volume_score_neutral_both_sides_insufficient_bars(self):
        df = _make_obv_trend_df(3, obv_direction=1)  # jauh di bawah min_bars
        res = calculate_volume_analysis(df)
        self.assertEqual(res.volume_score, 50.0)
        self.assertEqual(res.volume_score_short, 50.0)

    def test_adx_score_never_gets_a_short_field(self):
        """Konfirmasi ulang: adx_score TIDAK PERNAH punya varian _short --
        _pick_side_score() harus selalu fallback ke adx_score biasa utk
        kedua sisi, di SEMUA kondisi."""
        df = _make_trend_df(40, direction=-1)
        res = calculate_adx(df)
        self.assertFalse(hasattr(res, "adx_score_short"))

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_strength_short_differs_from_long(self):
        df_down = _make_trend_df(40, direction=-1)
        adx_res = calculate_adx(df_down)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.strength = adx_res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["strength"]["adx"], short_result["strength"]["adx"])
        self.assertNotEqual(long_result["strength"]["di"], short_result["strength"]["di"])
        self.assertGreater(short_result["strength"]["di"], long_result["strength"]["di"])
        # calculate_adx() sendiri tidak pernah mengisi field mfi -- object
        # ini murni dari calculate_adx(), jadi mfi_score_short-nya None,
        # fallback ke long di kedua sisi (terlepas dari batch mfi sendiri).
        self.assertEqual(long_result["strength"]["mfi"], short_result["strength"]["mfi"])


def _make_choppy_df(seed, bias, n=60, amp=0.6):
    """OHLCV random-walk dgn bias drift -- utk indikator momentum/oscillator
    yg butuh histori panjang & realistis (bukan tren monoton murni yg cepat
    saturate ke 0/100), supaya bisa dapat kasus NON-ekstrem yg representatif."""
    random.seed(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    bars = []
    c = 100.0
    for _ in range(n):
        move = bias + random.uniform(-amp, amp)
        o = c
        c = c + move
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        bars.append((o, h, l, c, 1000))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


class TestBatch3MomentumInputReflection(unittest.TestCase):
    """momentum category (rsi/macd/stoch) + mfi_score (strength, tapi teknik
    sama persis -- ditambahkan ke batch ini krn strength batch 2 sebelumnya
    tidak lengkap mencakupnya). Semua pakai input-reflection: formula SAMA,
    input dicerminkan ke titik tengahnya (RSI/Stoch/MFI: 100-x, MACD: -x)."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_rsi_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(60, direction=1, step=0.8)
        res = calculate_rsi_enhanced(df)
        self.assertGreater(res.rsi, 90.0)
        self.assertEqual(res.rsi_score, 10.0)

    def test_long_macd_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(60, direction=1, step=0.8)
        res = calculate_macd_enhanced(df)
        self.assertGreater(res.macd_histogram, 0)
        self.assertEqual(res.macd_score, 65.0)

    def test_long_stoch_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(60, direction=1, step=0.8)
        res = calculate_stochastic_rsi(df)
        self.assertGreaterEqual(res.stoch_k, 99.0)
        self.assertEqual(res.stoch_score, 25.0)

    def test_long_mfi_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(60, direction=1, step=0.8)
        res = calculate_money_flow(df)
        self.assertGreater(res.mfi, 90.0)
        self.assertEqual(res.mfi_score, 22.0)

    # ── 2. Swap-symmetry, multi-titik ───────────────────────────────────────

    def test_rsi_swap_symmetry_multiple_points(self):
        """side='short' harus PERSIS sama dgn manual mirror semua input lalu
        panggil side='long' -- membuktikan transformasi bukan reformulasi
        terpisah yg kebetulan mirip."""
        cases = [
            # (rsi_val, slope, divergence, zone_exit)
            (25.0, 1.5, 3.0, None),
            (50.0, -3.0, 0.0, None),
            (65.0, 0.0, -6.0, "overbought_exit"),
            (35.0, 2.5, 4.0, "oversold_exit"),
            (90.0, -1.0, -2.0, None),
        ]
        for rsi_val, slope, div, zone_exit in cases:
            via_side = _score_rsi(rsi_val, slope, div, zone_exit, side="short")
            via_manual_mirror = _score_rsi(
                100.0 - rsi_val, -slope, -div,
                {"oversold_exit": "overbought_exit", "overbought_exit": "oversold_exit"}.get(zone_exit, zone_exit),
                side="long",
            )
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"rsi={rsi_val} slope={slope} div={div} zone={zone_exit}")

    def test_macd_swap_symmetry_multiple_points(self):
        cases = [
            # (hist, hist_prev, macd_line, signal_line, zero_cross, bearish_zero_cross, divergence)
            (0.05, 0.03, 0.10, 0.05, False, False, 5.0),
            (-0.02, -0.01, -0.05, -0.03, False, False, -4.0),
            (0.01, -0.01, 0.02, 0.00, True, False, 0.0),
            (-0.01, 0.01, -0.02, 0.00, False, True, 0.0),
        ]
        for hist, hist_prev, macd_line, sig, zc, bzc, div in cases:
            via_side = _score_macd(hist, hist_prev, macd_line, sig, zc, bzc, div, side="short")
            via_manual_mirror = _score_macd(
                -hist, -hist_prev, -macd_line, -sig, bzc, zc, -div, side="long",
            )
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"hist={hist} macd_line={macd_line}")

    def test_stoch_swap_symmetry_multiple_points(self):
        cases = [
            # (k, d, kd_cross, zone)
            (25.0, 30.0, "bullish", "oversold"),
            (75.0, 70.0, "bearish", "overbought"),
            (50.0, 55.0, None, "neutral"),
            (85.0, 60.0, "bullish", "neutral"),
        ]
        for k, d, kd_cross, zone in cases:
            via_side = _score_stochrsi(k, d, kd_cross, zone, side="short")
            via_manual_mirror = _score_stochrsi(
                100.0 - k, 100.0 - d,
                {"bullish": "bearish", "bearish": "bullish"}.get(kd_cross, kd_cross),
                {"oversold": "overbought", "overbought": "oversold", "neutral": "neutral"}.get(zone, zone),
                side="long",
            )
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"k={k} d={d} cross={kd_cross} zone={zone}")

    def test_mfi_swap_symmetry_multiple_points(self):
        cases = [(15.0, 6.0), (50.0, 0.0), (65.0, -7.0), (88.0, 3.0)]
        for mfi_val, div in cases:
            via_side = _score_mfi(mfi_val, div, side="short")
            via_manual_mirror = _score_mfi(100.0 - mfi_val, -div, side="long")
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"mfi={mfi_val} div={div}")

    # ── "Bukan cuma beda angka" -- realistis, non-ekstrem, genuinely bagus utk short ──

    def test_rsi_and_macd_realistic_bearish_scenario_favors_short(self):
        df = _make_choppy_df(seed=5, bias=-0.05)
        r = calculate_rsi_enhanced(df)
        m = calculate_macd_enhanced(df)
        self.assertLess(r.rsi_score, 50.0)
        self.assertGreater(r.rsi_score_short, 50.0)
        self.assertLess(m.macd_score, 50.0)
        self.assertGreater(m.macd_score_short, 50.0)

    def test_stoch_realistic_scenario_favors_short(self):
        df = _make_choppy_df(seed=2, bias=-0.03)
        s = calculate_stochastic_rsi(df)
        self.assertLess(s.stoch_score, 50.0)
        self.assertGreater(s.stoch_score_short, 50.0)

    def test_mfi_realistic_scenario_favors_short(self):
        df = _make_choppy_df(seed=80, bias=-0.03)
        f = calculate_money_flow(df)
        self.assertLess(f.mfi_score, 50.0)
        self.assertGreater(f.mfi_score_short, 50.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_rsi_neutral_input_same_both_sides(self):
        """RSI=50 (midpoint persis) bukan berarti skor formula-nya == 50.0
        (base formula sendiri asimetris, skor jadi 64.0) -- tapi long DAN
        short harus SAMA persis krn keduanya representasi kondisi yg sama-
        sama netral/ambigu."""
        long_val = _score_rsi(50.0, 0.0, 0.0, None, side="long")
        short_val = _score_rsi(50.0, 0.0, 0.0, None, side="short")
        self.assertEqual(long_val, short_val)

    def test_macd_neutral_input_same_both_sides(self):
        long_val = _score_macd(0.0, 0.0, 0.0, 0.0, False, False, 0.0, side="long")
        short_val = _score_macd(0.0, 0.0, 0.0, 0.0, False, False, 0.0, side="short")
        self.assertEqual(long_val, short_val)

    def test_stoch_neutral_input_same_both_sides(self):
        long_val = _score_stochrsi(50.0, 50.0, None, "neutral", side="long")
        short_val = _score_stochrsi(50.0, 50.0, None, "neutral", side="short")
        self.assertEqual(long_val, short_val)

    def test_mfi_neutral_input_same_both_sides(self):
        long_val = _score_mfi(50.0, 0.0, side="long")
        short_val = _score_mfi(50.0, 0.0, side="short")
        self.assertEqual(long_val, short_val)

    def test_all_four_neutral_both_sides_insufficient_bars(self):
        tiny_df = _make_trend_df(3, direction=1)
        r = calculate_rsi_enhanced(tiny_df)
        m = calculate_macd_enhanced(tiny_df)
        s = calculate_stochastic_rsi(tiny_df)
        f = calculate_money_flow(tiny_df)
        self.assertEqual(r.rsi_score, 50.0)
        self.assertEqual(r.rsi_score_short, 50.0)
        self.assertEqual(m.macd_score, 50.0)
        self.assertEqual(m.macd_score_short, 50.0)
        self.assertEqual(s.stoch_score, 50.0)
        self.assertEqual(s.stoch_score_short, 50.0)
        self.assertEqual(f.mfi_score, 50.0)
        self.assertEqual(f.mfi_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_momentum_short_differs_from_long(self):
        df = _make_choppy_df(seed=5, bias=-0.05)
        r = calculate_rsi_enhanced(df)
        m = calculate_macd_enhanced(df)
        s = calculate_stochastic_rsi(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.momentum.rsi_score = r.rsi_score
        iset.momentum.rsi_score_short = r.rsi_score_short
        iset.momentum.macd_score = m.macd_score
        iset.momentum.macd_score_short = m.macd_score_short
        iset.momentum.stoch_score = s.stoch_score
        iset.momentum.stoch_score_short = s.stoch_score_short

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["momentum"]["rsi"], short_result["momentum"]["rsi"])
        self.assertNotEqual(long_result["momentum"]["macd"], short_result["momentum"]["macd"])
        self.assertGreater(short_result["momentum"]["rsi"], long_result["momentum"]["rsi"])
        self.assertGreater(short_result["momentum"]["macd"], long_result["momentum"]["macd"])


class TestBatch4OscillatorInputReflection(unittest.TestCase):
    """oscillator category: cci_score (mirror -CCI), williams_r_score
    (mirror -100-WR), roc_score (mirror -ROC + slope/crossover flip). Angka
    input langsung (bukan lewat OHLCV fixture/random search)."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_cci_score_unchanged(self):
        self.assertEqual(score_cci(-150.0), 57.5)
        self.assertEqual(score_cci(150.0), 42.5)
        self.assertEqual(score_cci(-250.0), 80.0)   # extreme oversold branch

    def test_long_williams_r_score_unchanged(self):
        self.assertEqual(score_williams_r(-5.0), 22.5)
        self.assertEqual(score_williams_r(-95.0), 81.25)

    def test_long_roc_score_unchanged(self):
        self.assertEqual(score_roc(6.0, 1.5, "bullish"), 83.0)
        self.assertEqual(score_roc(None), 50.0)

    # ── 2. Swap-symmetry, multi-titik ───────────────────────────────────────

    def test_cci_swap_symmetry_multiple_points(self):
        for cci_val in (-300.0, -150.0, -80.0, -10.0, 0.0, 30.0, 90.0, 180.0, 260.0):
            via_side = score_cci(cci_val, side="short")
            via_manual_mirror = score_cci(-cci_val, side="long")
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9, msg=f"cci={cci_val}")

    def test_williams_r_swap_symmetry_multiple_points(self):
        for wr_val in (0.0, -5.0, -15.0, -35.0, -50.0, -65.0, -85.0, -100.0):
            via_side = score_williams_r(wr_val, side="short")
            via_manual_mirror = score_williams_r(-100.0 - wr_val, side="long")
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9, msg=f"wr={wr_val}")

    def test_roc_swap_symmetry_multiple_points(self):
        cases = [
            # (roc, roc_slope, roc_crossover)
            (6.0, 1.5, "bullish"),
            (-6.0, -1.5, "bearish"),
            (3.0, -2.0, None),
            (-1.0, 0.5, "bullish"),
            (-8.0, 2.0, "bearish"),
        ]
        for roc, slope, crossover in cases:
            via_side = score_roc(roc, slope, crossover, side="short")
            mirrored_slope = -slope if slope is not None else None
            mirrored_crossover = {"bullish": "bearish", "bearish": "bullish"}.get(crossover, crossover)
            via_manual_mirror = score_roc(-roc, mirrored_slope, mirrored_crossover, side="long")
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"roc={roc} slope={slope} cross={crossover}")

    # ── "Bukan cuma beda angka" -- moderate/non-extreme, genuinely favor short ──

    def test_cci_moderate_bearish_favors_short(self):
        # CCI=-120: melewati CCI_OVERSOLD tapi TIDAK ekstrem -- moderate bearish.
        val = score_cci(-120.0)
        val_short = score_cci(-120.0, side="short")
        self.assertLess(val, 50.0)
        self.assertGreater(val_short, 50.0)

    def test_williams_r_moderate_overbought_favors_short(self):
        # WR=-25: dekat overbought (0) -- formula score_williams_r() (mirip
        # RSI) justru men-skor RENDAH utk long di sini (caution/risiko
        # reversal, BUKAN "bagus") -- verifikasi dulu arahnya lewat print
        # interaktif sebelum nulis assert (33.33 long, bukan >50 spt dugaan
        # awal). Utk short, ini justru zona favorable (fade overbought).
        val = score_williams_r(-25.0)
        val_short = score_williams_r(-25.0, side="short")
        self.assertLess(val, 50.0)
        self.assertGreater(val_short, 50.0)
        self.assertGreater(val_short, val)

    def test_roc_moderate_bearish_favors_short(self):
        val = score_roc(-3.0, -1.5, "bearish")
        val_short = score_roc(-3.0, -1.5, "bearish", side="short")
        self.assertLess(val, 50.0)
        self.assertGreater(val_short, 50.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_cci_neutral_input_same_both_sides(self):
        self.assertEqual(score_cci(0.0, side="long"), score_cci(0.0, side="short"))

    def test_williams_r_neutral_input_same_both_sides(self):
        self.assertEqual(score_williams_r(-50.0, side="long"), score_williams_r(-50.0, side="short"))

    def test_roc_neutral_input_same_both_sides(self):
        self.assertEqual(
            score_roc(0.0, 0.0, None, side="long"),
            score_roc(0.0, 0.0, None, side="short"),
        )

    def test_all_three_neutral_both_sides_when_none(self):
        self.assertEqual(score_cci(None, side="long"), 50.0)
        self.assertEqual(score_cci(None, side="short"), 50.0)
        self.assertEqual(score_williams_r(None, side="long"), 50.0)
        self.assertEqual(score_williams_r(None, side="short"), 50.0)
        self.assertEqual(score_roc(None, side="long"), 50.0)
        self.assertEqual(score_roc(None, side="short"), 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_oscillator_short_differs_from_long(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.oscillators.cci_score = score_cci(-120.0)
        iset.oscillators.cci_score_short = score_cci(-120.0, side="short")
        iset.oscillators.williams_r_score = score_williams_r(-25.0)
        iset.oscillators.williams_r_score_short = score_williams_r(-25.0, side="short")
        iset.oscillators.roc_score = score_roc(-3.0, -1.5, "bearish")
        iset.oscillators.roc_score_short = score_roc(-3.0, -1.5, "bearish", side="short")
        # field mentah non-skor -- TIDAK disentuh, harus tetap sama di kedua sisi
        iset.oscillators.cci_trend = "bearish"
        iset.oscillators.cci_divergence = -4.0

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertGreater(short_result["oscillator"]["cci"], long_result["oscillator"]["cci"])
        self.assertGreater(short_result["oscillator"]["williams"], long_result["oscillator"]["williams"])
        self.assertGreater(short_result["oscillator"]["roc"], long_result["oscillator"]["roc"])
        # field mentah non-skor tetap identik di kedua sisi
        self.assertEqual(long_result["oscillator"]["cci_trend"], short_result["oscillator"]["cci_trend"])
        self.assertEqual(
            long_result["oscillator"]["cci_divergence"], short_result["oscillator"]["cci_divergence"]
        )


class TestBatch5SupertrendScoreShort(unittest.TestCase):
    """trend category, fungsi 1/4: supertrend_score. Paling sederhana --
    direction cuma {1,-1}, mirror(direction)=-direction via
    _score_supertrend_direction()."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_supertrend_score_unchanged_uptrend(self):
        df = _make_trend_df(40, direction=1)
        res = calculate_supertrend(df)
        self.assertEqual(res.supertrend_direction, 1)
        self.assertEqual(res.supertrend_score, 85.0)

    def test_long_supertrend_score_unchanged_downtrend(self):
        df = _make_trend_df(40, direction=-1)
        res = calculate_supertrend(df)
        self.assertEqual(res.supertrend_direction, -1)
        self.assertEqual(res.supertrend_score, 15.0)

    def test_long_via_score_trend_unchanged(self):
        df = _make_trend_df(40, direction=1)
        res = score_trend(df, timeframe="15m")
        self.assertEqual(res.supertrend_score, 85.0)

    # ── 2. Swap-symmetry, multi-titik ───────────────────────────────────────

    def test_supertrend_swap_symmetry_both_directions(self):
        for direction in (1, -1):
            via_side = _score_supertrend_direction(direction, side="short")
            via_manual_mirror = _score_supertrend_direction(-direction, side="long")
            self.assertEqual(via_side, via_manual_mirror, msg=f"direction={direction}")

    def test_supertrend_short_exact_values(self):
        self.assertEqual(_score_supertrend_direction(1, side="short"), 15.0)
        self.assertEqual(_score_supertrend_direction(-1, side="short"), 85.0)

    def test_supertrend_swap_symmetry_via_real_ohlcv_both_directions(self):
        """Bukan cuma unit-level -- via calculate_supertrend() penuh dgn
        data OHLCV asli utk kedua arah tren."""
        df_up = _make_trend_df(40, direction=1)
        df_down = _make_trend_df(40, direction=-1)
        res_up = calculate_supertrend(df_up)
        res_down = calculate_supertrend(df_down)
        self.assertEqual(res_up.supertrend_score, res_down.supertrend_score_short)
        self.assertEqual(res_down.supertrend_score, res_up.supertrend_score_short)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_downtrend_favors_short_not_just_different(self):
        df = _make_trend_df(40, direction=-1)
        res = calculate_supertrend(df)
        self.assertGreater(res.supertrend_score_short, res.supertrend_score)
        self.assertEqual(res.supertrend_score_short, 85.0)

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(40, direction=1)
        res = calculate_supertrend(df)
        self.assertGreater(res.supertrend_score, res.supertrend_score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(3, direction=1)  # jauh di bawah period+1
        res = calculate_supertrend(df)
        self.assertIsNone(res.supertrend_direction)
        self.assertEqual(res.supertrend_score, 50.0)
        self.assertEqual(res.supertrend_score_short, 50.0)

    def test_direction_none_neutral_both_sides_unit_level(self):
        self.assertEqual(
            _score_supertrend_direction(None, side="long"),
            _score_supertrend_direction(None, side="short"),
        )

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_supertrend_short_differs_from_long(self):
        df = _make_trend_df(40, direction=-1)
        res = calculate_supertrend(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["trend"]["supertrend"], short_result["trend"]["supertrend"])
        self.assertGreater(short_result["trend"]["supertrend"], long_result["trend"]["supertrend"])
        # ema_stack/cross/vwap belum dapat batch-nya (belum diimplementasi di
        # fungsi ini turn) -- masih fallback ke long di kedua sisi
        self.assertEqual(long_result["trend"]["ema_stack"], short_result["trend"]["ema_stack"])
        self.assertEqual(long_result["trend"]["cross"], short_result["trend"]["cross"])
        self.assertEqual(long_result["trend"]["vwap"], short_result["trend"]["vwap"])


class TestBatch5EmaStackScoreShort(unittest.TestCase):
    """trend category, fungsi 2/4: ema_stack_score. Paling kompleks -- butuh
    flip cabang (fast>slow -> fast<slow) + gap_adj sign-flip, bukan sekadar
    transform nilai tunggal."""

    _BULL_VALS = {9: 110.0, 21: 100.0, 50: 95.0, 100: 90.0, 200: 85.0}   # semua fast>slow
    _BEAR_VALS = {9: 85.0, 21: 90.0, 50: 95.0, 100: 100.0, 200: 110.0}   # mirror persis dari _BULL_VALS

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_all_bull_pairs(self):
        self.assertEqual(_score_ema_stack(self._BULL_VALS), 100.0)

    def test_long_score_unchanged_all_bear_pairs(self):
        self.assertEqual(_score_ema_stack(self._BEAR_VALS), 0.0)

    def test_long_via_calculate_ema_stack_unchanged_uptrend_fixture(self):
        df = _make_trend_df(250, direction=1, step=0.5)
        res = calculate_ema_stack(df)
        self.assertEqual(res.ema_stack_score, 100.0)

    def test_long_via_score_trend_unchanged(self):
        df = _make_trend_df(250, direction=1, step=0.5)
        res = score_trend(df, timeframe="15m")
        self.assertEqual(res.ema_stack_score, 100.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_exact_mirror_ema_values(self):
        """_BULL_VALS dan _BEAR_VALS sengaja dikonstruksi sbg mirror persis
        satu sama lain -- bull(long) harus == bear(short) dan sebaliknya."""
        self.assertEqual(_score_ema_stack(self._BULL_VALS, side="long"),
                          _score_ema_stack(self._BEAR_VALS, side="short"))
        self.assertEqual(_score_ema_stack(self._BEAR_VALS, side="long"),
                          _score_ema_stack(self._BULL_VALS, side="short"))

    def test_swap_symmetry_partial_pairs_multiple_configs(self):
        """Beberapa kombinasi ema_values dgn SEBAGIAN pair None (data pendek)
        + campuran fast>slow/fast<slow -- side='short' dibandingkan dgn
        rekonstruksi independen formula (fast<slow rewarded + gap_adj
        dinegasi), BUKAN cuma memanggil ulang _score_ema_stack() itu sendiri
        (supaya bukan tes tautologis)."""
        from engine.constants import EMA_GAP_BONUS_MAX
        from engine.indicators.trend import EMA_STACK_WEIGHTS
        from engine.core.models import clamp_score as _cs

        pairs = ((9, 21, 0), (21, 50, 1), (50, 100, 2), (100, 200, 3))
        configs = [
            {9: 105.0, 21: 100.0, 50: None, 100: 90.0, 200: 95.0},
            {9: 98.0, 21: 100.0, 50: 102.0, 100: None, 200: None},
            {9: 100.0, 21: 100.0, 50: 100.0, 100: 100.0, 200: 100.0},  # semua flat
        ]
        for ema_values in configs:
            via_side = _score_ema_stack(ema_values, side="short")

            expected_score = 0.0
            avail = 0.0
            for fast_p, slow_p, widx in pairs:
                fv = ema_values.get(fast_p)
                sv = ema_values.get(slow_p)
                if fv is None or sv is None:
                    continue
                w = EMA_STACK_WEIGHTS[widx]
                avail += w
                if fv < sv:  # short: fast<slow yang direward
                    expected_score += w

            normalized = (expected_score / avail * 100) if avail > 0 else 0.0
            ema9, ema21 = ema_values.get(9), ema_values.get(21)
            gap_adj = 0.0
            if ema9 is not None and ema21 is not None and ema21 > 0:
                gap_pct = (ema9 - ema21) / ema21 * 100
                gap_adj = -min(EMA_GAP_BONUS_MAX, max(-EMA_GAP_BONUS_MAX, gap_pct * EMA_GAP_BONUS_MAX))
            expected = _cs(normalized + gap_adj)

            self.assertAlmostEqual(via_side, expected, places=9, msg=f"ema_values={ema_values}")

    def test_swap_symmetry_via_real_ohlcv_both_directions(self):
        df_up = _make_trend_df(250, direction=1, step=0.5)
        df_down = _make_trend_df(250, direction=-1, step=0.5)
        res_up = calculate_ema_stack(df_up)
        res_down = calculate_ema_stack(df_down)
        self.assertEqual(res_up.ema_stack_score, res_down.ema_stack_score_short)
        self.assertEqual(res_down.ema_stack_score, res_up.ema_stack_score_short)

    def test_gap_adj_sign_flip_equivalent_to_negating_gap_pct_before_clamp(self):
        """Verifikasi independen (BUKAN re-panggil clamp_score produksi)
        bahwa 'negasi gap_adj di akhir' == 'negasi gap_pct sebelum clamp' --
        klaim matematis yg mendasari implementasi gap_adj utk short."""
        from engine.constants import EMA_GAP_BONUS_MAX

        def manual_gap_adj(ema9, ema21, negate_before_clamp):
            gap_pct = (ema9 - ema21) / ema21 * 100
            if negate_before_clamp:
                gap_pct = -gap_pct
            return min(EMA_GAP_BONUS_MAX, max(-EMA_GAP_BONUS_MAX, gap_pct * EMA_GAP_BONUS_MAX))

        for ema9, ema21 in [(110.0, 100.0), (90.0, 100.0), (101.0, 100.0), (50.0, 100.0), (100.0, 100.0)]:
            negate_before = manual_gap_adj(ema9, ema21, negate_before_clamp=True)
            negate_after = -manual_gap_adj(ema9, ema21, negate_before_clamp=False)
            self.assertAlmostEqual(negate_before, negate_after, places=9, msg=f"ema9={ema9} ema21={ema21}")

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_downtrend_favors_short_not_just_different(self):
        df = _make_trend_df(250, direction=-1, step=0.5)
        res = calculate_ema_stack(df)
        self.assertGreater(res.ema_stack_score_short, res.ema_stack_score)
        self.assertEqual(res.ema_stack_score_short, 100.0)

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(250, direction=1, step=0.5)
        res = calculate_ema_stack(df)
        self.assertGreater(res.ema_stack_score, res.ema_stack_score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(3, direction=1)
        res = calculate_ema_stack(df)
        self.assertEqual(res.ema_stack_score, 50.0)
        self.assertEqual(res.ema_stack_score_short, 50.0)

    def test_neutral_both_sides_all_flat(self):
        """Semua EMA persis sama (fast==slow di tiap pair) -- tidak ada pair
        yg 'menang' fast>slow ATAU fast<slow, jadi stack_score=0 kedua sisi;
        gap_pct=0 juga (ema9==ema21) -- long dan short harus identik."""
        flat_vals = {9: 100.0, 21: 100.0, 50: 100.0, 100: 100.0, 200: 100.0}
        self.assertEqual(
            _score_ema_stack(flat_vals, side="long"),
            _score_ema_stack(flat_vals, side="short"),
        )

    def test_no_valid_pairs_returns_none_both_sides(self):
        empty_vals = {9: None, 21: None, 50: None, 100: None, 200: None}
        self.assertIsNone(_score_ema_stack(empty_vals, side="long"))
        self.assertIsNone(_score_ema_stack(empty_vals, side="short"))

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_ema_stack_short_differs_from_long(self):
        df = _make_trend_df(250, direction=-1, step=0.5)
        res = calculate_ema_stack(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["trend"]["ema_stack"], short_result["trend"]["ema_stack"])
        self.assertGreater(short_result["trend"]["ema_stack"], long_result["trend"]["ema_stack"])
        # supertrend belum dihitung di sini (calculate_ema_stack() tidak
        # menyentuhnya) -- masih fallback ke long di kedua sisi
        self.assertEqual(long_result["trend"]["supertrend"], short_result["trend"]["supertrend"])


def _make_reversal_df(n_flat, n_trend, direction=1, start=100.0, step=0.8, vol=1000):
    """OHLCV drift berlawanan arah dulu (n_flat bar), lalu benar-benar
    membalik (n_trend bar) -- utk memicu golden/dead cross NYATA dalam
    lookback (bukan cuma fallback 'no cross' branch)."""
    idx = pd.date_range("2026-01-01", periods=n_flat + n_trend, freq="15min")
    bars = []
    c = start
    for _ in range(n_flat):
        c = c - direction * 0.3
        o = c + direction * 0.1
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        bars.append((o, h, l, c, vol))
    for _ in range(n_trend):
        c = c + direction * step
        o = c - direction * step * 0.3
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        bars.append((o, h, l, c, vol))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


class TestBatch5CrossScoreShort(unittest.TestCase):
    """trend category, fungsi 3/4: cross_score. Input-reflection pada
    compound input (golden_bars_ago, dead_bars_ago, gap_pct) -- swap
    golden<->dead + negasi gap_pct, formula sama persis."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_recent_cross_case(self):
        self.assertEqual(_score_cross(3, None, 2.0, 50), 93.5)
        self.assertEqual(_score_cross(None, 3, -2.0, 50), 6.5)

    def test_long_score_unchanged_no_cross_case(self):
        self.assertEqual(_score_cross(None, None, 1.0, 50), 55.0)

    def test_long_via_calculate_golden_dead_cross_unchanged(self):
        df = _make_reversal_df(30, 30, direction=1)
        gc, dc, score, _ = calculate_golden_dead_cross(df)
        self.assertEqual(gc, 23)
        self.assertIsNone(dc)
        self.assertEqual(score, 88.5)

    def test_long_via_score_trend_unchanged(self):
        df = _make_reversal_df(30, 30, direction=1)
        res = score_trend(df, timeframe="15m")
        self.assertEqual(res.cross_score, 88.5)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_unit_level_multiple_points(self):
        """side='short' harus PERSIS sama dgn manual swap gc<->dc + negasi
        gap_pct lalu panggil side='long'."""
        cases = [
            # (golden_bars_ago, dead_bars_ago, gap_pct)
            (3, None, 2.0),
            (None, 3, -2.0),
            (10, None, 0.5),
            (None, 15, -4.0),
            (None, None, 1.5),
            (None, None, -0.8),
        ]
        for gc, dc, gap_pct in cases:
            via_side = _score_cross(gc, dc, gap_pct, lookback=50, side="short")
            via_manual_mirror = _score_cross(dc, gc, -gap_pct, lookback=50, side="long")
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9,
                                    msg=f"gc={gc} dc={dc} gap_pct={gap_pct}")

    def test_swap_symmetry_via_real_ohlcv_golden_and_dead_fixture(self):
        """Fixture yg BENAR-BENAR memicu deteksi golden/dead cross nyata
        (bukan cuma fallback 'no cross'), utk kedua arah."""
        df_golden = _make_reversal_df(30, 30, direction=1)
        df_dead = _make_reversal_df(30, 30, direction=-1)
        r_golden = calculate_golden_dead_cross(df_golden)
        r_dead = calculate_golden_dead_cross(df_dead)

        golden_gc, golden_dc, golden_score, golden_score_short = r_golden
        dead_gc, dead_dc, dead_score, dead_score_short = r_dead

        self.assertEqual(golden_gc, 23)
        self.assertIsNone(golden_dc)
        self.assertEqual(dead_dc, 23)
        self.assertIsNone(dead_gc)

        self.assertEqual(golden_score, dead_score_short)
        self.assertEqual(dead_score, golden_score_short)

    def test_swap_symmetry_via_real_ohlcv_no_cross_fallback(self):
        df_up = _make_trend_df(60, direction=1, step=0.8)
        df_down = _make_trend_df(60, direction=-1, step=0.8)
        r_up = calculate_golden_dead_cross(df_up)
        r_down = calculate_golden_dead_cross(df_down)
        self.assertIsNone(r_up[0])
        self.assertIsNone(r_up[1])
        self.assertEqual(r_up[2], r_down[3])
        self.assertEqual(r_down[2], r_up[3])

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_dead_cross_favors_short_not_just_different(self):
        df = _make_reversal_df(30, 30, direction=-1)
        gc, dc, score, score_short = calculate_golden_dead_cross(df)
        self.assertGreater(score_short, score)
        self.assertEqual(score_short, 88.5)
        self.assertEqual(score, 11.5)

    def test_golden_cross_favors_long_not_short(self):
        df = _make_reversal_df(30, 30, direction=1)
        gc, dc, score, score_short = calculate_golden_dead_cross(df)
        self.assertGreater(score, score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_gap_pct_zero_no_cross(self):
        long_val = _score_cross(None, None, 0.0, lookback=50, side="long")
        short_val = _score_cross(None, None, 0.0, lookback=50, side="short")
        self.assertEqual(long_val, 50.0)
        self.assertEqual(short_val, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(3, direction=1)
        gc, dc, score, score_short = calculate_golden_dead_cross(df)
        self.assertIsNone(gc)
        self.assertIsNone(dc)
        self.assertEqual(score, 50.0)
        self.assertEqual(score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_cross_short_differs_from_long(self):
        df = _make_reversal_df(30, 30, direction=-1)
        gc, dc, score, score_short = calculate_golden_dead_cross(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend.golden_cross_bars_ago = gc
        iset.trend.dead_cross_bars_ago = dc
        iset.trend.cross_score = score
        iset.trend.cross_score_short = score_short

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["trend"]["cross"], short_result["trend"]["cross"])
        self.assertGreater(short_result["trend"]["cross"], long_result["trend"]["cross"])
        # ema_stack/supertrend belum dihitung di iset ini -- fallback ke long
        self.assertEqual(long_result["trend"]["ema_stack"], short_result["trend"]["ema_stack"])
        self.assertEqual(long_result["trend"]["supertrend"], short_result["trend"]["supertrend"])


def _make_vwap_test_df(final_close, n=60, base=100.0, vol=1000):
    """n-1 bar flat di `base` (bangun vwap stabil di sekitar `base`), lalu 1
    bar terakhir ditutup di `final_close` -- utk uji zona vwap tertentu via
    OHLCV asli, bukan cuma unit-level."""
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    bars = []
    for _ in range(n - 1):
        bars.append((base, base + 0.3, base - 0.3, base, vol))
    bars.append((base, max(base, final_close) + 0.2, min(base, final_close) - 0.2, final_close, vol))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


class TestBatch5VwapScoreShort(unittest.TestCase):
    """trend category, fungsi 4/4 (TERAKHIR di Batch 5): vwap_score.
    Input-reflection via mirror harga di sekitar vwap_val
    (last_close' = 2*vwap_val - last_close), ladder 6-zona sama persis."""

    _VWAP, _U1, _U2, _L1, _L2 = 100.0, 105.0, 110.0, 95.0, 90.0

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_all_zones(self):
        cases = [
            (112.0, 30.0),  # extreme overbought
            (107.0, 55.0),  # upper band zone
            (102.0, 72.0),  # above vwap
            (97.0, 45.0),   # lower band zone
            (92.0, 35.0),   # below lower_1
            (88.0, 65.0),   # extreme oversold
        ]
        for close, expected in cases:
            self.assertEqual(
                _score_vwap_zone(close, self._VWAP, self._U1, self._U2, self._L1, self._L2),
                expected, msg=f"close={close}",
            )

    def test_long_via_calculate_vwap_unchanged(self):
        df = _make_vwap_test_df(115.0)
        res = calculate_vwap(df)
        self.assertEqual(res.vwap_score, 30.0)

    def test_long_via_score_trend_unchanged(self):
        df = _make_vwap_test_df(115.0)
        res = score_trend(df, timeframe="15m")
        self.assertEqual(res.vwap_score, 30.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_unit_level_multiple_points(self):
        """side='short' harus PERSIS sama dgn manual reflect
        (2*vwap-close) lalu panggil side='long', utk banyak titik close."""
        for close in (112.0, 107.0, 102.0, 100.0, 97.0, 92.0, 88.0, 82.0):
            via_side = _score_vwap_zone(close, self._VWAP, self._U1, self._U2, self._L1, self._L2, side="short")
            mirrored_close = 2.0 * self._VWAP - close
            via_manual_mirror = _score_vwap_zone(
                mirrored_close, self._VWAP, self._U1, self._U2, self._L1, self._L2, side="long"
            )
            self.assertAlmostEqual(via_side, via_manual_mirror, places=9, msg=f"close={close}")

    def test_swap_symmetry_zone_ladder_flips_top_to_bottom(self):
        """Tiap zona hari ini (long) harus persis == zona mirrornya (short)
        -- extreme_OB<->extreme_OS, upper_1<->lower_1, dst."""
        # (close, mirror_close) pairs -- mirror_close = 2*100 - close
        zone_pairs = [
            (112.0, 88.0),   # extreme_OB <-> extreme_OS
            (107.0, 93.0),   # upper_1_zone <-> lower_1_zone
            (102.0, 98.0),   # above_vwap <-> lower_1_zone
        ]
        for close, mirror_close in zone_pairs:
            long_at_close = _score_vwap_zone(close, self._VWAP, self._U1, self._U2, self._L1, self._L2)
            short_at_mirror_close = _score_vwap_zone(
                mirror_close, self._VWAP, self._U1, self._U2, self._L1, self._L2, side="short"
            )
            self.assertEqual(long_at_close, short_at_mirror_close, msg=f"close={close}")

    def test_swap_symmetry_via_real_ohlcv_multiple_zones(self):
        """Verifikasi lewat data OHLCV asli (bukan unit-level) utk beberapa
        zona berbeda -- extreme OB, upper band, above-vwap."""
        for spike_close in (115.0, 107.0, 101.0):
            df = _make_vwap_test_df(spike_close)
            res = calculate_vwap(df)
            mirror_close = 2.0 * res.vwap - spike_close
            df_mirror = _make_vwap_test_df(mirror_close)
            res_mirror = calculate_vwap(df_mirror)
            self.assertAlmostEqual(res.vwap_score, res_mirror.vwap_score_short, places=1,
                                    msg=f"spike_close={spike_close}")

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_extreme_overbought_favors_short_not_just_different(self):
        df = _make_vwap_test_df(115.0)
        res = calculate_vwap(df)
        self.assertLess(res.vwap_score, 50.0)
        self.assertGreater(res.vwap_score_short, 50.0)
        self.assertEqual(res.vwap_score, 30.0)
        self.assertEqual(res.vwap_score_short, 65.0)

    def test_extreme_oversold_favors_long_not_short(self):
        df = _make_vwap_test_df(85.0)
        res = calculate_vwap(df)
        self.assertGreater(res.vwap_score, res.vwap_score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_close_exactly_at_vwap(self):
        """close persis di vwap_val -- long dan short harus SAMA (bukan
        berarti harus 50.0, tapi harus SEPAKAT)."""
        long_val = _score_vwap_zone(100.0, self._VWAP, self._U1, self._U2, self._L1, self._L2, side="long")
        short_val = _score_vwap_zone(100.0, self._VWAP, self._U1, self._U2, self._L1, self._L2, side="short")
        self.assertEqual(long_val, short_val)
        self.assertEqual(long_val, 72.0)  # bukan 50 -- tapi tetap sepakat kedua sisi

    def test_neutral_both_sides_insufficient_data(self):
        tiny = pd.DataFrame([{"open": 1, "high": 1, "low": 1, "close": 1}])
        res = calculate_vwap(tiny)
        self.assertIsNone(res.vwap)
        self.assertEqual(res.vwap_score, 50.0)
        self.assertEqual(res.vwap_score_short, 50.0)

    def test_neutral_both_sides_skip_vwap_daily_timeframe(self):
        df = _make_vwap_test_df(115.0)
        res = score_trend(df, timeframe="1d")
        self.assertEqual(res.vwap_score, 50.0)
        self.assertEqual(res.vwap_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_vwap_short_differs_from_long(self):
        df = _make_vwap_test_df(115.0)
        res = calculate_vwap(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.trend = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["trend"]["vwap"], short_result["trend"]["vwap"])
        self.assertGreater(short_result["trend"]["vwap"], long_result["trend"]["vwap"])
        # ema_stack/cross/supertrend tidak dihitung calculate_vwap() -- fallback ke long
        self.assertEqual(long_result["trend"]["ema_stack"], short_result["trend"]["ema_stack"])
        self.assertEqual(long_result["trend"]["cross"], short_result["trend"]["cross"])
        self.assertEqual(long_result["trend"]["supertrend"], short_result["trend"]["supertrend"])


class TestBatch6IchimokuScoreShort(unittest.TestCase):
    """structure category, fungsi 1/4: ichimoku_score. Branch/role swap
    (bukan input-reflection) -- tiap term diidentifikasi label mana yg
    'selaras' dgn side, lalu swap yg mana memicu bonus."""

    _DATA_BULL = {
        "price_vs_cloud": "above", "tenkan": 110.0, "kijun": 105.0,
        "tk_cross": "bullish", "cloud_thickness": 2.0,
    }
    _DATA_BEAR = {
        "price_vs_cloud": "below", "tenkan": 95.0, "kijun": 100.0,
        "tk_cross": "bearish", "cloud_thickness": 2.0,
    }
    _PRICE_BULL = 112.0
    _PRICE_BEAR = 90.0

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_bull_data(self):
        self.assertEqual(score_ichimoku(self._DATA_BULL, self._PRICE_BULL), 100.0)

    def test_long_score_unchanged_bear_data(self):
        self.assertEqual(score_ichimoku(self._DATA_BEAR, self._PRICE_BEAR), 12.0)

    def test_long_via_score_structure_unchanged_uptrend_fixture(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.6)
        res = score_structure(df)
        self.assertEqual(res.price_vs_cloud, "above")
        self.assertEqual(res.ichimoku_score, 93.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_exact_mirror_data(self):
        """_DATA_BULL/_DATA_BEAR sengaja dikonstruksi sbg mirror persis satu
        sama lain (label ditukar, harga ditukar) -- bull.long harus ==
        bear.short dan sebaliknya."""
        self.assertEqual(
            score_ichimoku(self._DATA_BULL, self._PRICE_BULL, side="long"),
            score_ichimoku(self._DATA_BEAR, self._PRICE_BEAR, side="short"),
        )
        self.assertEqual(
            score_ichimoku(self._DATA_BEAR, self._PRICE_BEAR, side="long"),
            score_ichimoku(self._DATA_BULL, self._PRICE_BULL, side="short"),
        )

    def test_swap_symmetry_multiple_configs_independent_reconstruction(self):
        """Beberapa kombinasi data, dibandingkan dgn rekonstruksi independen
        (bukan cuma manggil score_ichimoku lagi) dari definisi mirror."""
        configs = [
            ({"price_vs_cloud": "above", "tenkan": 100.0, "kijun": 90.0,
              "tk_cross": "bullish", "cloud_thickness": 1.5}, 105.0),
            ({"price_vs_cloud": "inside", "tenkan": 100.0, "kijun": 100.0,
              "tk_cross": None, "cloud_thickness": None}, 100.0),
            ({"price_vs_cloud": "below", "tenkan": 80.0, "kijun": 85.0,
              "tk_cross": None, "cloud_thickness": 3.0}, 78.0),
        ]
        for data, price in configs:
            via_side = score_ichimoku(data, price, side="short")

            score = 50.0
            pvc = data.get("price_vs_cloud")
            if pvc == "below":
                score += 20.0
            elif pvc == "above":
                score -= 20.0
            tenkan, kijun = data.get("tenkan"), data.get("kijun")
            if tenkan and kijun:
                score += 8.0 if tenkan < kijun else -8.0
                if price < tenkan:
                    score += 5.0
                if price < kijun:
                    score += 5.0
            tk_cross = data.get("tk_cross")
            if tk_cross == "bearish":
                score += 10.0
            elif tk_cross == "bullish":
                score -= 10.0
            ct = data.get("cloud_thickness")
            if ct and pvc == "below":
                score += min(ct / price * 500, 5.0)
            from engine.core.models import clamp_score as _cs
            expected = _cs(score)

            self.assertAlmostEqual(via_side, expected, places=9, msg=f"data={data} price={price}")

    def test_swap_symmetry_via_real_ohlcv_both_directions(self):
        df_up = _make_trend_df(80, direction=1, start=150.0, step=0.6)
        df_down = _make_trend_df(80, direction=-1, start=150.0, step=0.6)
        res_up = score_structure(df_up)
        res_down = score_structure(df_down)
        self.assertEqual(res_up.ichimoku_score, res_down.ichimoku_score_short)
        self.assertEqual(res_down.ichimoku_score, res_up.ichimoku_score_short)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_downtrend_favors_short_not_just_different(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.6)
        res = score_structure(df)
        self.assertGreater(res.ichimoku_score_short, res.ichimoku_score)
        self.assertEqual(res.ichimoku_score, 22.0)
        self.assertEqual(res.ichimoku_score_short, 93.0)

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.6)
        res = score_structure(df)
        self.assertGreater(res.ichimoku_score, res.ichimoku_score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_empty_data(self):
        self.assertEqual(
            score_ichimoku({}, 100.0, side="long"),
            score_ichimoku({}, 100.0, side="short"),
        )

    def test_neutral_both_sides_inside_cloud(self):
        """tenkan==kijun persis -- cabang is_aligned (tenkan<kijun utk short,
        tenkan>kijun utk long) SAMA-SAMA False saat seri, jadi KEDUA sisi
        kena else-penalty(-8) yg sama -- hasilnya 42.0, BUKAN 50.0 (verifikasi
        dulu lewat run interaktif, bukan diasumsikan) -- tapi tetap harus
        SEPAKAT antara long & short, itu inti yang diuji di sini."""
        data = {"price_vs_cloud": "inside", "tenkan": 100.0, "kijun": 100.0}
        long_val = score_ichimoku(data, 100.0, side="long")
        short_val = score_ichimoku(data, 100.0, side="short")
        self.assertEqual(long_val, short_val)
        self.assertEqual(long_val, 42.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_ichimoku_short_differs_from_long(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.6)
        res = score_structure(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.structure = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["structure"]["ichimoku"], short_result["structure"]["ichimoku"])
        self.assertGreater(short_result["structure"]["ichimoku"], long_result["structure"]["ichimoku"])
        # [UPDATE -- sar_score diimplementasi belakangan di batch yg sama
        # (fungsi 2/4), res berasal dari score_structure() PENUH jadi
        # otomatis ikut menghitung sar_score_short juga -- assert lama
        # "fallback ke long" utk sar sudah stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["sar"], short_result["structure"]["sar"])
        # [UPDATE -- pivot_score diimplementasi belakangan di batch yg sama
        # (fungsi 3/4), res berasal dari score_structure() PENUH jadi
        # otomatis ikut menghitung pivot_score_short juga -- assert lama
        # "fallback ke long" utk pivot sudah stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["pivot"], short_result["structure"]["pivot"])
        # [UPDATE -- fib_score diimplementasi belakangan di batch yg sama
        # (fungsi 4/4, TERAKHIR di Batch 6) -- assert lama "fallback ke long"
        # utk fibonacci sudah stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["fibonacci"], short_result["structure"]["fibonacci"])


class TestBatch6SarScoreShort(unittest.TestCase):
    """structure category, fungsi 2/4: sar_score. gap_pct dihitung sama
    (tergantung sar_direction AKTUAL), yang di-swap cuma formula (reward
    3-tier vs penalty 2-tier) mana yang dipakai arah mana."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_up_direction(self):
        self.assertEqual(score_sar(95.0, "up", 100.0), 75.0)

    def test_long_score_unchanged_down_direction(self):
        self.assertEqual(score_sar(105.0, "down", 100.0), 25.0)

    def test_long_score_unchanged_across_gap_magnitudes(self):
        cases = [(0.5, 55.0), (2.0, 72.0), (4.0, 73.5), (8.0, 79.5)]
        for gap, expected in cases:
            sar_up = 100.0 - gap
            self.assertEqual(score_sar(sar_up, "up", 100.0), expected, msg=f"gap={gap}")

    def test_long_via_score_structure_unchanged_uptrend_fixture(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertEqual(res.sar_direction, "up")
        # nilai exact tergantung gap_pct hasil kalkulasi SAR asli -- cukup
        # pastikan formula reward (>50, arah up) yang terpakai, bukan angka
        # ajaib yg di-hardcode dari 1 eksperimen.
        self.assertGreater(res.sar_score, 50.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_exact_mirror_points(self):
        """(sar_value, direction, price) vs (mirror_sar_value, arah
        berlawanan, price) yg gap_pct-nya SAMA -- long(a) harus == short(b)."""
        cases = [
            (95.0, "up", 100.0, 105.0, "down", 100.0),    # gap=5% keduanya
            (99.0, "up", 100.0, 101.0, "down", 100.0),    # gap=1% keduanya
            (90.0, "up", 100.0, 110.0, "down", 100.0),    # gap=10% keduanya
            (99.7, "up", 100.0, 100.3, "down", 100.0),    # gap=0.3% (zona waspada)
        ]
        for sar_up, dir_up, price_up, sar_down, dir_down, price_down in cases:
            long_val = score_sar(sar_up, dir_up, price_up, side="long")
            short_val = score_sar(sar_down, dir_down, price_down, side="short")
            self.assertEqual(long_val, short_val, msg=f"sar_up={sar_up} sar_down={sar_down}")

    def test_swap_symmetry_multiple_configs_independent_reconstruction(self):
        """side='short' dibandingkan dgn rekonstruksi independen dari
        definisi mirror (good_direction='down'), bukan cuma manggil ulang
        score_sar() itu sendiri."""
        configs = [
            (95.0, "up", 100.0),
            (105.0, "down", 100.0),
            (99.5, "up", 100.0),
            (100.5, "down", 100.0),
        ]
        for sar_value, sar_direction, price in configs:
            via_side = score_sar(sar_value, sar_direction, price, side="short")

            if sar_direction == "up":
                gap_pct = (price - sar_value) / price * 100
            else:
                gap_pct = (sar_value - price) / price * 100

            good_direction = "down"
            if sar_direction == good_direction:
                if gap_pct > 3.0:
                    expected = 72.0 + min(gap_pct - 3.0, 5.0) * 1.5
                elif gap_pct > 1.0:
                    expected = 62.0 + gap_pct * 5.0
                else:
                    expected = 55.0
            else:
                if gap_pct > 3.0:
                    expected = 28.0 - min(gap_pct - 3.0, 5.0) * 1.5
                else:
                    expected = 38.0

            self.assertAlmostEqual(via_side, max(0.0, min(100.0, expected)), places=9,
                                    msg=f"sar_value={sar_value} dir={sar_direction}")

    def test_swap_symmetry_via_real_ohlcv_both_directions(self):
        """[KOREKSI SETELAH VERIFIKASI] Percobaan pertama membandingkan
        res_up.sar_score langsung dgn res_down.sar_score_short dari DUA
        fixture terpisah GAGAL -- diverifikasi lewat cetak nilai interaktif:
        gap_pct hasil algoritma Parabolic SAR (mekanisme akselerasi AF)
        TIDAK persis simetris antara tren naik vs turun biarpun fixture
        dibangun dgn parameter "mirror" (gap uptrend=1.64%, gap
        downtrend=4.03% -- beda genuinely, bukan bug). Jadi dua fixture
        TERPISAH tidak bisa diharapkan match persis di sini (beda dgn
        cross_score/vwap_score yg formulanya benar-benar simetris di bawah
        reflection). Test yg benar: ambil hasil REAL dari calculate_sar()
        (via score_structure()), lalu verifikasi score_short-nya lewat
        rekonstruksi independen dari nol -- bukan cross-check ke fixture lain."""
        df_up = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        df_down = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res_up = score_structure(df_up)
        res_down = score_structure(df_down)
        self.assertEqual(res_up.sar_direction, "up")
        self.assertEqual(res_down.sar_direction, "down")

        for res, df in ((res_up, df_up), (res_down, df_down)):
            real_price = float(df["close"].iloc[-1])
            if res.sar_direction == "up":
                gap_pct = (real_price - res.sar_value) / real_price * 100
            else:
                gap_pct = (res.sar_value - real_price) / real_price * 100

            good_direction = "down"  # side="short"
            if res.sar_direction == good_direction:
                if gap_pct > 3.0:
                    expected = 72.0 + min(gap_pct - 3.0, 5.0) * 1.5
                elif gap_pct > 1.0:
                    expected = 62.0 + gap_pct * 5.0
                else:
                    expected = 55.0
            else:
                if gap_pct > 3.0:
                    expected = 28.0 - min(gap_pct - 3.0, 5.0) * 1.5
                else:
                    expected = 38.0
            expected = max(0.0, min(100.0, expected))

            self.assertAlmostEqual(res.sar_score_short, expected, places=6,
                                    msg=f"direction={res.sar_direction} sar_value={res.sar_value}")

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_downtrend_favors_short_not_just_different(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertGreater(res.sar_score_short, res.sar_score)
        self.assertGreater(res.sar_score_short, 50.0)
        self.assertLess(res.sar_score, 50.0)

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertGreater(res.sar_score, res.sar_score_short)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_no_data(self):
        long_val = score_sar(None, None, 100.0, side="long")
        short_val = score_sar(None, None, 100.0, side="short")
        self.assertEqual(long_val, 50.0)
        self.assertEqual(short_val, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(2, direction=1)
        res = score_structure(df)
        self.assertIsNone(res.sar_direction)
        self.assertEqual(res.sar_score, 50.0)
        self.assertEqual(res.sar_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_sar_short_differs_from_long(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.structure = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["structure"]["sar"], short_result["structure"]["sar"])
        self.assertGreater(short_result["structure"]["sar"], long_result["structure"]["sar"])
        # ichimoku SUDAH dapat batch (fungsi 1/4) -- res dari score_structure()
        # penuh jadi ichimoku_score_short juga terhitung, harus berbeda dari long.
        self.assertNotEqual(long_result["structure"]["ichimoku"], short_result["structure"]["ichimoku"])
        # [UPDATE -- pivot_score diimplementasi belakangan di batch yg sama
        # (fungsi 3/4) -- assert lama "fallback ke long" utk pivot sudah
        # stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["pivot"], short_result["structure"]["pivot"])
        # [UPDATE -- fib_score diimplementasi belakangan di batch yg sama
        # (fungsi 4/4) -- assert lama "fallback ke long" utk fibonacci sudah
        # stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["fibonacci"], short_result["structure"]["fibonacci"])


class TestBatch6PivotScoreShort(unittest.TestCase):
    """structure category, fungsi 3/4: pivot_score. nearest_support &
    nearest_resistance TIDAK simetris jarak dari pivot secara umum (r1-p =
    p-l, p-s1 = h-p, cuma sama kalau close persis di tengah high-low) --
    role-swap (near_level='ideal entry', far_level='mentok/ruang gerak'),
    bukan input-reflection harga spt vwap_score."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_near_support_far_resistance(self):
        data = {"pivot": 100.0, "nearest_support": 97.0, "nearest_resistance": 103.0}
        self.assertEqual(score_pivot(data, 99.0), 45.0)

    def test_long_score_unchanged_far_support_near_resistance(self):
        data = {"pivot": 100.0, "nearest_support": 90.0, "nearest_resistance": 101.0}
        self.assertEqual(score_pivot(data, 101.0), 40.0)

    def test_long_score_unchanged_missing_support(self):
        data = {"pivot": 100.0, "nearest_support": None, "nearest_resistance": 103.0}
        self.assertEqual(score_pivot(data, 99.0), 45.0)

    def test_long_score_unchanged_across_random_fuzz(self):
        """2000 kasus acak dibandingkan thd reimplementasi formula SEBELUM
        side param ada -- byte-identical, verifikasi refactor jadi role-swap
        tidak mengubah perilaku long sedikit pun (pelajaran dari sar_score:
        jangan asumsikan, verifikasi lewat fuzz sebelum percaya)."""
        def score_pivot_old(data, current_price):
            if data.get("pivot") is None:
                return 50.0
            score = 50.0
            pivot = data["pivot"]
            ns = data.get("nearest_support")
            nr = data.get("nearest_resistance")
            if current_price >= pivot:
                score += 10.0
            else:
                score -= 10.0
            if ns and current_price > 0:
                d = (current_price - ns) / current_price * 100
                if d < 1.0: score += 12.0
                elif d < 2.0: score += 6.0
                elif d > 5.0: score -= 5.0
            if nr and current_price > 0:
                d = (nr - current_price) / current_price * 100
                if d < 1.0: score -= 15.0
                elif d < 2.0: score -= 7.0
                elif d > 4.0: score += 5.0
            return clamp_score(score)

        rng = random.Random(42)
        for _ in range(2000):
            pivot = rng.uniform(50, 200)
            price = pivot * rng.uniform(0.9, 1.1)
            ns = pivot * rng.uniform(0.85, 1.0) if rng.random() > 0.1 else None
            nr = pivot * rng.uniform(1.0, 1.15) if rng.random() > 0.1 else None
            data = {"pivot": pivot, "nearest_support": ns, "nearest_resistance": nr}
            self.assertAlmostEqual(score_pivot(data, price), score_pivot_old(data, price), places=9,
                                    msg=f"data={data} price={price}")

    def test_long_via_score_structure_unchanged_uptrend_fixture(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertEqual(res.price_vs_pivot, "above")
        self.assertEqual(res.pivot_score, 57.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_multiple_configs_independent_reconstruction(self):
        """side='short' dibandingkan dgn rekonstruksi independen dari
        definisi role-swap (near_level=nearest_resistance, far_level=
        nearest_support), bukan cuma manggil ulang score_pivot() itu sendiri."""
        configs = [
            ({"pivot": 100.0, "nearest_support": 97.0, "nearest_resistance": 103.0}, 99.0),
            ({"pivot": 100.0, "nearest_support": 99.5, "nearest_resistance": 100.5}, 99.0),
            ({"pivot": 100.0, "nearest_support": 90.0, "nearest_resistance": 101.0}, 101.0),
            ({"pivot": 100.0, "nearest_support": None, "nearest_resistance": 103.0}, 99.0),
            ({"pivot": 100.0, "nearest_support": 97.0, "nearest_resistance": None}, 101.0),
        ]
        for data, price in configs:
            via_side = score_pivot(data, price, side="short")

            pivot = data["pivot"]
            ns = data.get("nearest_support")
            nr = data.get("nearest_resistance")
            score = 50.0
            if price < pivot:      # utk short, di bawah pivot yg favorable
                score += 10.0
            else:
                score -= 10.0
            if nr and price > 0:   # nearest_resistance = "ideal entry" utk short
                d = (nr - price) / price * 100
                if d < 1.0: score += 12.0
                elif d < 2.0: score += 6.0
                elif d > 5.0: score -= 5.0
            if ns and price > 0:   # nearest_support = "mentok/ruang gerak" utk short
                d = (price - ns) / price * 100
                if d < 1.0: score -= 15.0
                elif d < 2.0: score -= 7.0
                elif d > 4.0: score += 5.0
            expected = clamp_score(score)

            self.assertAlmostEqual(via_side, expected, places=9, msg=f"data={data} price={price}")

    def test_swap_symmetry_via_real_ohlcv_both_directions(self):
        """Sama spt pelajaran sar_score: nearest_support/resistance dari
        calculate_pivot_points() TIDAK simetris jarak dari pivot pd data
        riil, jadi dua fixture tren berlawanan arah TIDAK bisa diharap match
        silang satu sama lain. Verifikasi lewat rekonstruksi independen dari
        nilai REAL (res.pivot/nearest_support/nearest_resistance) hasil
        score_structure(), bukan cross-check ke fixture lain."""
        df_up = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        df_down = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res_up = score_structure(df_up)
        res_down = score_structure(df_down)

        for res, df in ((res_up, df_up), (res_down, df_down)):
            price = float(df["close"].iloc[-1])
            pivot = res.pivot
            ns = res.nearest_support
            nr = res.nearest_resistance

            score = 50.0
            if price < pivot:
                score += 10.0
            else:
                score -= 10.0
            if nr and price > 0:
                d = (nr - price) / price * 100
                if d < 1.0: score += 12.0
                elif d < 2.0: score += 6.0
                elif d > 5.0: score -= 5.0
            if ns and price > 0:
                d = (price - ns) / price * 100
                if d < 1.0: score -= 15.0
                elif d < 2.0: score -= 7.0
                elif d > 4.0: score += 5.0
            expected = clamp_score(score)

            self.assertAlmostEqual(res.pivot_score_short, expected, places=6,
                                    msg=f"pivot={pivot} price={price}")

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_downtrend_favors_short_not_just_different(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertGreater(res.pivot_score_short, res.pivot_score)
        self.assertGreater(res.pivot_score_short, 50.0)
        self.assertLess(res.pivot_score, 50.0)
        self.assertEqual(res.pivot_score, 45.0)
        self.assertEqual(res.pivot_score_short, 51.0)

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertGreater(res.pivot_score, res.pivot_score_short)
        self.assertEqual(res.pivot_score, 57.0)
        self.assertEqual(res.pivot_score_short, 37.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_no_pivot_data(self):
        long_val = score_pivot({}, 100.0, side="long")
        short_val = score_pivot({}, 100.0, side="short")
        self.assertEqual(long_val, 50.0)
        self.assertEqual(short_val, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(1, direction=1)
        res = score_structure(df)
        self.assertIsNone(res.pivot)
        self.assertEqual(res.pivot_score, 50.0)
        self.assertEqual(res.pivot_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_pivot_short_differs_from_long(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.structure = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["structure"]["pivot"], short_result["structure"]["pivot"])
        self.assertGreater(short_result["structure"]["pivot"], long_result["structure"]["pivot"])
        # ichimoku & sar sudah dapat batch sebelumnya -- harus berbeda juga.
        self.assertNotEqual(long_result["structure"]["ichimoku"], short_result["structure"]["ichimoku"])
        self.assertNotEqual(long_result["structure"]["sar"], short_result["structure"]["sar"])
        # [UPDATE -- fib_score diimplementasi belakangan di batch yg sama
        # (fungsi 4/4, TERAKHIR di Batch 6) -- assert lama "fallback ke long"
        # utk fibonacci sudah stale, diperbaiki di sini.
        self.assertNotEqual(long_result["structure"]["fibonacci"], short_result["structure"]["fibonacci"])


class TestBatch6FibScoreShort(unittest.TestCase):
    """structure category, fungsi 4/4 (TERAKHIR di Batch 6): fib_score.
    Diverifikasi EMPIRIS dulu (bukan diasumsikan) apakah level retracement
    FIB_LEVELS=[0.236,0.382,0.500,0.618,0.786] simetris di sekitar titik
    tengah: 1-0.382=0.618 & 1-0.500=0.500 COCOK (self-symmetric), tapi
    1-0.236=0.764 != 0.786 & 1-0.786=0.214 != 0.236 -- set level TIDAK
    simetris sempurna. Jadi pola yang dipakai role-swap (sama persis dgn
    score_pivot), BUKAN input-reflection harga spt vwap_score."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_near_support_level(self):
        data = {"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
                 "nearest_fib_resistance": 124.72, "fib_618": 108.56}
        self.assertEqual(score_fibonacci(data, 115.6), 67.0)

    def test_long_score_unchanged_near_resistance_level(self):
        data = {"fib_swing_high": 140.0, "nearest_fib_support": None,
                 "nearest_fib_resistance": 124.72, "fib_618": 108.56}
        self.assertEqual(score_fibonacci(data, 124.4), 38.0)

    def test_long_score_unchanged_golden_ratio_bonus(self):
        """fib_618 persis dekat current_price (<0.5%) -- cabang bonus +15
        menggantikan tier jarak biasa, HARUS tetap terpicu lewat cabang
        near_level(=ns) utk long, identik dgn perilaku asli."""
        data = {"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
                 "nearest_fib_resistance": 124.72, "fib_618": 115.28}
        self.assertEqual(score_fibonacci(data, 115.5), 70.0)

    def test_long_score_unchanged_across_random_fuzz(self):
        """20000 kasus acak (termasuk kasus fib_618 dekat price supaya
        cabang golden-ratio ikut ke-exercise) dibandingkan thd
        reimplementasi formula SEBELUM side param ada -- byte-identical.
        Pelajaran dari sar_score & pivot_score: jangan asumsikan, fuzz dulu
        sebelum percaya refactor tidak mengubah perilaku long."""
        def score_fibonacci_old(data, current_price):
            if data.get("fib_swing_high") is None:
                return 50.0
            score = 50.0
            ns = data.get("nearest_fib_support")
            nr = data.get("nearest_fib_resistance")
            if ns and current_price > 0:
                dist_pct = (current_price - ns) / current_price * 100
                fib618 = data.get("fib_618")
                if fib618 and abs(current_price - fib618) / current_price < 0.005:
                    score += 15.0
                elif dist_pct < 0.5:
                    score += 12.0
                elif dist_pct < 1.5:
                    score += 7.0
                elif dist_pct > 6.0:
                    score -= 5.0
            if nr and current_price > 0:
                dist_pct = (nr - current_price) / current_price * 100
                if dist_pct < 1.0:
                    score -= 12.0
                elif dist_pct < 2.0:
                    score -= 6.0
                elif dist_pct > 5.0:
                    score += 5.0
            return clamp_score(score)

        rng = random.Random(7)
        for _ in range(20000):
            has_swing = rng.random() > 0.05
            swing_high = rng.uniform(100, 300) if has_swing else None
            price = rng.uniform(50, 350)
            ns = price * rng.uniform(0.85, 1.0) if rng.random() > 0.1 else None
            nr = price * rng.uniform(1.0, 1.2) if rng.random() > 0.1 else None
            fib618 = None
            if rng.random() > 0.3:
                fib618 = (price * rng.uniform(0.99, 1.01) if rng.random() > 0.5
                           else price * rng.uniform(0.7, 1.3))
            data = {"fib_swing_high": swing_high, "nearest_fib_support": ns,
                    "nearest_fib_resistance": nr, "fib_618": fib618}
            self.assertAlmostEqual(score_fibonacci(data, price), score_fibonacci_old(data, price),
                                    places=9, msg=f"data={data} price={price}")

    def test_long_via_score_structure_unchanged_downtrend_fixture(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)
        self.assertEqual(res.fib_score, 55.0)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via OHLCV asli) ───────

    def test_swap_symmetry_multiple_configs_independent_reconstruction(self):
        """side='short' dibandingkan dgn rekonstruksi independen dari
        definisi role-swap (near_level=nearest_fib_resistance, far_level=
        nearest_fib_support, golden-ratio tetap dicek murni dari fib_618 vs
        price tapi di cabang near_level) -- bukan cuma manggil ulang
        score_fibonacci() itu sendiri."""
        configs = [
            ({"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
              "nearest_fib_resistance": 124.72, "fib_618": 115.28}, 115.5),
            ({"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
              "nearest_fib_resistance": 124.72, "fib_618": 124.72}, 124.6),
            ({"fib_swing_high": 140.0, "nearest_fib_support": None,
              "nearest_fib_resistance": 124.72, "fib_618": 108.56}, 120.0),
            ({"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
              "nearest_fib_resistance": None, "fib_618": None}, 118.0),
        ]
        for data, price in configs:
            via_side = score_fibonacci(data, price, side="short")

            ns = data.get("nearest_fib_support")
            nr = data.get("nearest_fib_resistance")
            fib618 = data.get("fib_618")
            near_level, far_level = nr, ns   # role-swap utk short
            score = 50.0
            if near_level and price > 0:
                d = (near_level - price) / price * 100   # negasi dari (price-near)/price
                if fib618 and abs(price - fib618) / price < 0.005:
                    score += 15.0
                elif d < 0.5:
                    score += 12.0
                elif d < 1.5:
                    score += 7.0
                elif d > 6.0:
                    score -= 5.0
            if far_level and price > 0:
                d = (price - far_level) / price * 100   # negasi dari (far-price)/price
                if d < 1.0:
                    score -= 12.0
                elif d < 2.0:
                    score -= 6.0
                elif d > 5.0:
                    score += 5.0
            expected = clamp_score(score)

            self.assertAlmostEqual(via_side, expected, places=9, msg=f"data={data} price={price}")

    def test_swap_symmetry_via_real_ohlcv_both_directions(self):
        """Sama spt pelajaran sar_score & pivot_score: nearest_fib_support/
        resistance dari calculate_fibonacci() TIDAK simetris jarak dari
        harga pd data riil, jadi dua fixture tren berlawanan arah TIDAK bisa
        diharap match silang. Verifikasi lewat rekonstruksi independen dari
        nilai REAL (res.nearest_fib_support/resistance/fib_618) hasil
        score_structure(), bukan cross-check ke fixture lain."""
        df_up = _make_trend_df(80, direction=1, start=150.0, step=0.8)
        df_down = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res_up = score_structure(df_up)
        res_down = score_structure(df_down)

        for res, df in ((res_up, df_up), (res_down, df_down)):
            price = float(df["close"].iloc[-1])
            ns = res.nearest_fib_support
            nr = res.nearest_fib_resistance
            fib618 = res.fib_618

            near_level, far_level = nr, ns
            score = 50.0
            if near_level and price > 0:
                d = (near_level - price) / price * 100
                if fib618 and abs(price - fib618) / price < 0.005:
                    score += 15.0
                elif d < 0.5:
                    score += 12.0
                elif d < 1.5:
                    score += 7.0
                elif d > 6.0:
                    score -= 5.0
            if far_level and price > 0:
                d = (price - far_level) / price * 100
                if d < 1.0:
                    score -= 12.0
                elif d < 2.0:
                    score -= 6.0
                elif d > 5.0:
                    score += 5.0
            expected = clamp_score(score)

            self.assertAlmostEqual(res.fib_score_short, expected, places=6,
                                    msg=f"ns={ns} nr={nr} price={price}")

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_near_support_level_favors_long_not_short(self):
        """[VERIFIKASI DULU] fixture tren OHLCV standar TERNYATA tidak
        selalu menghasilkan fib_score/short yg beda scr bermakna (fractal
        swing detector gagal cari titik interior pd data monoton murni,
        fallback default 'uptrend' utk KEDUA arah -- lihat catatan di
        test_swap_symmetry_via_real_ohlcv_both_directions). Jadi 'bukan
        cuma beda angka' diuji lewat data fib level yg dikontrol langsung,
        bukan lewat fixture tren yg hasilnya tidak dapat diprediksi arahnya."""
        data = {"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
                 "nearest_fib_resistance": 124.72, "fib_618": 108.56}
        price = 115.6
        long_val = score_fibonacci(data, price, side="long")
        short_val = score_fibonacci(data, price, side="short")
        self.assertGreater(long_val, short_val)
        self.assertGreater(long_val, 50.0)
        self.assertLess(short_val, 50.0)

    def test_near_resistance_level_favors_short_not_long(self):
        data = {"fib_swing_high": 140.0, "nearest_fib_support": 115.28,
                 "nearest_fib_resistance": 124.72, "fib_618": 108.56}
        price = 124.4
        long_val = score_fibonacci(data, price, side="long")
        short_val = score_fibonacci(data, price, side="short")
        self.assertGreater(short_val, long_val)
        self.assertGreater(short_val, 50.0)
        self.assertLess(long_val, 50.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_no_fib_data(self):
        long_val = score_fibonacci({}, 100.0, side="long")
        short_val = score_fibonacci({}, 100.0, side="short")
        self.assertEqual(long_val, 50.0)
        self.assertEqual(short_val, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(5, direction=1)
        res = score_structure(df)
        self.assertIsNone(res.fib_swing_high)
        self.assertEqual(res.fib_score, 50.0)
        self.assertEqual(res.fib_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_extract_indicator_scores_fib_short_differs_from_long(self):
        df = _make_trend_df(80, direction=-1, start=150.0, step=0.8)
        res = score_structure(df)

        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.structure = res

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["structure"]["fibonacci"], short_result["structure"]["fibonacci"])
        # ichimoku, sar, pivot semua sudah dapat batch sebelumnya -- Batch 6
        # sekarang KOMPLIT (4/4 fungsi structure category side-aware).
        self.assertNotEqual(long_result["structure"]["ichimoku"], short_result["structure"]["ichimoku"])
        self.assertNotEqual(long_result["structure"]["sar"], short_result["structure"]["sar"])
        self.assertNotEqual(long_result["structure"]["pivot"], short_result["structure"]["pivot"])


def _make_synthetic_book(bid_qty_mult=1.0, ask_qty_mult=1.0, n=20, seed=1,
                          bid_price=100.0, ask_price=100.1):
    """Orderbook sintetis dgn qty acak (seeded, deterministik) -- rasio
    bid/ask dikontrol via bid_qty_mult/ask_qty_mult, dipakai utk uji
    imbalance_score lewat jalur calculate_orderbook() PENUH (bukan cuma
    unit-level dict), termasuk _weighted_volume/_filter_min_size dst."""
    rng = random.Random(seed)
    bids, asks = [], []
    for i in range(n):
        bp = bid_price - i * 0.05
        ap = ask_price + i * 0.05
        bq = rng.uniform(0.5, 2.0) * bid_qty_mult
        aq = rng.uniform(0.5, 2.0) * ask_qty_mult
        bids.append((bp, bq))
        asks.append((ap, aq))
    return {"bids": bids, "asks": asks}


class TestBatch7ImbalanceScoreShort(unittest.TestCase):
    """orderbook category, fungsi 1/3: imbalance_score. DIVERIFIKASI EMPIRIS
    dulu (200rb sampel acak + titik boundary, lihat riwayat sesi): formula
    ini genuinely simetris -- score_imbalance(imb) + score_imbalance(1-imb)
    == 100.0 PERSIS di seluruh range [0,1], TIDAK seperti sar/pivot/fib yg
    ternyata TIDAK simetris sempurna. Jadi pendekatan yg dipakai reflection
    literal (imb -> 1-imb), pola sama dgn _score_vwap_zone di trend.py --
    BUKAN role-swap.

    [CAKUPAN] imbalance_score/_short SUDAH dihitung & di-expose lewat
    OrderbookIndicators, TAPI score_orderbook() (composite) & scorer.py
    'ob_score' BELUM disentuh -- baru bisa side-aware yg benar setelah
    whale_score & absorption_score (fungsi 2/3 & 3/3 Batch 7) juga selesai.
    spread_score & liquidity_score sudah dikonfirmasi aman, tidak diubah."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_bull_boundary(self):
        self.assertEqual(_score_imbalance(IMBALANCE_BULL), 54.8)

    def test_long_score_unchanged_bear_boundary(self):
        self.assertEqual(_score_imbalance(IMBALANCE_BEAR), 45.2)

    def test_long_score_unchanged_extremes(self):
        self.assertEqual(_score_imbalance(1.0), 90.0)
        self.assertEqual(_score_imbalance(0.0), 10.0)

    def test_long_score_unchanged_neutral(self):
        self.assertEqual(_score_imbalance(0.5), 50.0)

    def test_long_score_unchanged_across_random_fuzz(self):
        """20000 kasus acak (termasuk out-of-range utk cek clamp) dibandingkan
        thd reimplementasi formula SEBELUM side param & sebelum diekstrak
        dari inline calculate_orderbook() -- byte-identical. Pelajaran dari
        sar/pivot/fib: jangan asumsikan refactor aman, fuzz dulu."""
        def score_imbalance_old(imb):
            # [PENTING] orderbook.py punya clamp_score SENDIRI (max/min
            # murni, TANPA round()) -- BEDA dari clamp_score yg di-import
            # dari engine.core.models di puncak file ini (round ke 4
            # desimal, dipakai test pattern_score krn produksinya memang
            # pakai clamp_score itu). Formula imbalance KONTINU (interpolasi
            # linear via t), jadi round(...,4) BUKAN no-op di sini -- beda
            # dgn sar/pivot/fib yg cuma menjumlah konstanta diskrit sehingga
            # kebetulan tidak kepengaruh rounding. Pakai max/min manual biar
            # cocok persis dgn clamp_score lokal orderbook.py.
            _anchor_bull = 50.0 + (IMBALANCE_BULL - 0.5) * 40.0
            _anchor_bear = 50.0 + (IMBALANCE_BEAR - 0.5) * 40.0
            if imb >= IMBALANCE_BULL:
                t = (imb - IMBALANCE_BULL) / (1.0 - IMBALANCE_BULL)
                return max(0.0, min(100.0, _anchor_bull + t * (90.0 - _anchor_bull)))
            elif imb <= IMBALANCE_BEAR:
                t = (IMBALANCE_BEAR - imb) / IMBALANCE_BEAR
                return max(0.0, min(100.0, _anchor_bear - t * (_anchor_bear - 10.0)))
            else:
                return max(0.0, min(100.0, 50.0 + (imb - 0.5) * 40.0))

        rng = random.Random(3)
        for _ in range(20000):
            imb = rng.uniform(-0.2, 1.2)
            self.assertAlmostEqual(_score_imbalance(imb), score_imbalance_old(imb),
                                    places=9, msg=f"imb={imb}")

    def test_long_via_calculate_orderbook_unchanged_bid_heavy_fixture(self):
        reset_state("TESTBATCH7-LONGREG/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        res = calculate_orderbook(ob, symbol="TESTBATCH7-LONGREG/USDT")
        self.assertEqual(res["bid_ask_imbalance"], 0.7565)
        self.assertAlmostEqual(res["imbalance_score"], 67.44421052631579, places=9)

    # ── 2. Swap-symmetry, multi-titik (unit-level DAN via data real) ────────

    def test_swap_symmetry_exact_reflection_multiple_points(self):
        """Reflection EKSAK (bukan cuma dekat) -- score_imbalance(imb,
        side='short') harus == score_imbalance(1-imb, side='long') PERSIS,
        di titik manapun termasuk boundary."""
        for imb in (0.0, 0.1, 0.38, 0.38 - 1e-6, 0.5, 0.62, 0.62 + 1e-6, 0.9, 1.0):
            short_val = _score_imbalance(imb, side="short")
            mirrored_long = _score_imbalance(1.0 - imb, side="long")
            self.assertEqual(short_val, mirrored_long, msg=f"imb={imb}")

    def test_swap_symmetry_sum_to_100_multiple_configs(self):
        """side='short' dibandingkan dgn properti aljabar yg diverifikasi
        DULU sebelum implementasi (bukan cuma di-assert setelah fakta):
        long+short harus == 100.0 PERSIS di titik manapun."""
        rng = random.Random(99)
        for _ in range(2000):
            imb = rng.uniform(0.0, 1.0)
            long_val = _score_imbalance(imb, side="long")
            short_val = _score_imbalance(imb, side="short")
            self.assertAlmostEqual(long_val + short_val, 100.0, places=9, msg=f"imb={imb}")

    def test_swap_symmetry_via_real_orderbook_both_directions(self):
        """Verifikasi lewat data orderbook sintetis yg lolos jalur
        calculate_orderbook() PENUH (_weighted_volume, _filter_min_size,
        dst) -- bukan cuma unit-level dict. Krn formula sudah dibuktikan
        simetris sempurna, cukup verifikasi long+short == 100 dari HASIL
        RIIL (tidak butuh rekonstruksi independen manual spt sar/pivot/fib
        krn tidak ada asimetri tersembunyi yg perlu di-reconstruct)."""
        for label, bid_mult, ask_mult in (("bid-heavy", 3.0, 1.0), ("ask-heavy", 1.0, 3.0),
                                            ("balanced", 1.0, 1.0)):
            reset_state(f"SYM-{label}/USDT")
            ob = _make_synthetic_book(bid_qty_mult=bid_mult, ask_qty_mult=ask_mult)
            res = calculate_orderbook(ob, symbol=f"SYM-{label}/USDT")
            self.assertAlmostEqual(res["imbalance_score"] + res["imbalance_score_short"], 100.0,
                                    places=6, msg=label)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_bid_heavy_favors_long_not_short(self):
        reset_state("BIDHEAVY/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        res = calculate_orderbook(ob, symbol="BIDHEAVY/USDT")
        self.assertGreater(res["imbalance_score"], res["imbalance_score_short"])
        self.assertGreater(res["imbalance_score"], 50.0)
        self.assertLess(res["imbalance_score_short"], 50.0)
        self.assertAlmostEqual(res["imbalance_score"], 67.44421052631579, places=9)
        self.assertAlmostEqual(res["imbalance_score_short"], 32.555789473684214, places=9)

    def test_ask_heavy_favors_short_not_long(self):
        reset_state("ASKHEAVY/USDT")
        ob = _make_synthetic_book(bid_qty_mult=1.0, ask_qty_mult=3.0)
        res = calculate_orderbook(ob, symbol="ASKHEAVY/USDT")
        self.assertGreater(res["imbalance_score_short"], res["imbalance_score"])
        self.assertGreater(res["imbalance_score_short"], 50.0)
        self.assertLess(res["imbalance_score"], 50.0)
        self.assertAlmostEqual(res["imbalance_score"], 33.76926315789474, places=9)
        self.assertAlmostEqual(res["imbalance_score_short"], 66.23073684210527, places=9)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_empty_orderbook(self):
        reset_state("EMPTYOB/USDT")
        res = calculate_orderbook({}, symbol="EMPTYOB/USDT")
        self.assertIsNone(res["bid_ask_imbalance"])
        self.assertEqual(res["imbalance_score"], 50.0)
        self.assertEqual(res["imbalance_score_short"], 50.0)

    def test_neutral_both_sides_missing_side(self):
        reset_state("NOBIDSOB/USDT")
        res = calculate_orderbook({"bids": [], "asks": [(100.0, 1.0)]}, symbol="NOBIDSOB/USDT")
        self.assertEqual(res["imbalance_score"], 50.0)
        self.assertEqual(res["imbalance_score_short"], 50.0)

    def test_neutral_both_sides_perfectly_balanced_book(self):
        reset_state("PERFBALOB/USDT")
        res = calculate_orderbook({"bids": [(100.0, 5.0)], "asks": [(100.1, 5.0)]}, symbol="PERFBALOB/USDT")
        self.assertAlmostEqual(res["imbalance_score"], 50.0, delta=0.05)
        self.assertAlmostEqual(res["imbalance_score_short"], 50.0, delta=0.05)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_score_orderbook_data_wires_imbalance_score_short(self):
        reset_state("WIREOB/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        ob["symbol"] = "WIREOB/USDT"
        ind = score_orderbook_data(ob)
        self.assertIsNotNone(ind.imbalance_score_short)
        self.assertNotEqual(ind.imbalance_score, ind.imbalance_score_short)
        self.assertAlmostEqual(ind.imbalance_score + ind.imbalance_score_short, 100.0, places=6)

    def test_extract_indicator_scores_ob_score_still_fallback_composite_not_wired_yet(self):
        """[CAKUPAN] orderbook_score (composite) & scorer.py 'ob_score' BELUM
        disentuh di batch/fungsi ini -- whale_score & absorption_score
        (fungsi 2/3 & 3/3 Batch 7) belum side-aware, jadi composite tetap
        fallback ke long utk SEMENTARA. Ini bukti disengaja (bukan bug lupa
        wiring) -- akan diperbaiki setelah whale & absorption selesai,
        seperti pola 'assert lama jadi stale, diperbaiki' di Batch 6."""
        reset_state("SCORERWIREOB/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        ob["symbol"] = "SCORERWIREOB/USDT"
        ind = score_orderbook_data(ob)

        iset = IndicatorSet(symbol="SCORERWIREOB/USDT", timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


def _make_book_with_wall(wall_side="bid", wall_mult=15.0, n=20, seed=1,
                          bid_price=100.0, ask_price=100.1):
    """Orderbook sintetis dgn satu level (index 2) diperbesar wall_mult x
    supaya lolos WHALE_WALL_PCT -- dipakai utk uji whale_score/cluster.
    wall_side=None => tidak ada wall sama sekali (baseline netral)."""
    rng = random.Random(seed)
    bids, asks = [], []
    for i in range(n):
        bp = bid_price - i * 0.05
        ap = ask_price + i * 0.05
        bq = rng.uniform(0.5, 2.0)
        aq = rng.uniform(0.5, 2.0)
        if wall_side == "bid" and i == 2:
            bq *= wall_mult
        if wall_side == "ask" and i == 2:
            aq *= wall_mult
        bids.append((bp, bq))
        asks.append((ap, aq))
    return {"bids": bids, "asks": asks}


class TestBatch7WhaleScoreShort(unittest.TestCase):
    """orderbook category, fungsi 2/3: whale_score. DIVERIFIKASI EMPIRIS
    dulu (200rb sampel acak, role-swap PENUH bid<->ask): deviasi dari
    score+score_short==100 cuma ~1e-14 (float noise, bukan asimetri nyata)
    -- KARENA koefisien bid (0.3/cap 8.0 whale, 0.2/cap 5.0 cluster) IDENTIK
    dgn koefisien ask (beda dgn score_pivot yg near/far-nya pakai koefisien
    BEDA sehingga TIDAK sum-to-100). Jadi pendekatan yg dipakai role-swap
    PENUH (5 pasangan bid<->ask: wb_str, bid_dist_factor, cb_str, cb_price,
    wb_price -- ditukar dgn wa_str, ask_dist_factor, ca_str, ca_price,
    wa_price), bukan reflection skalar tunggal spt imbalance_score (tidak
    ada satu angka kontinu utk direfleksikan, datanya inherently dua-sisi).

    [CAKUPAN] whale_score/_short SUDAH dihitung & di-expose lewat
    OrderbookIndicators, TAPI score_orderbook() (composite) & scorer.py
    'ob_score' BELUM disentuh -- baru bisa side-aware yg benar setelah
    absorption_score (fungsi 3/3 Batch 7) juga selesai."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_whale_bid_wall_only(self):
        self.assertEqual(
            _score_whale(20.0, 1.0, None, None, 100.0, None, 1.0, None, None, None),
            56.0,
        )

    def test_long_score_unchanged_whale_ask_wall_plus_bid_cluster(self):
        self.assertEqual(
            _score_whale(10.0, 0.8, 15.0, 99.0, 100.0, 25.0, 1.0, None, None, 101.0),
            47.3,
        )

    def test_long_score_unchanged_cluster_same_price_as_whale_skips_bonus(self):
        """cb_price == wb_price -- bonus cluster TIDAK boleh dobel-hitung
        dgn whale wall yang sama, harus identik dgn whale-only."""
        self.assertEqual(
            _score_whale(20.0, 1.0, 12.0, 100.0, 100.0, None, 1.0, None, None, None),
            56.0,
        )

    def test_long_score_unchanged_across_random_fuzz(self):
        """20000 kasus acak (termasuk None utk whale/cluster, cb_price ==
        wb_price sengaja utk exercise kondisi != ) dibandingkan thd
        reimplementasi formula SEBELUM side param & sebelum diekstrak dari
        inline calculate_orderbook() -- byte-identical. Pakai clamp_score
        LOKAL orderbook.py (bukan yg di-import dari engine.core.models) --
        pelajaran dari bug test imbalance_score kemarin."""
        def whale_score_old(wb_str, bid_dist_factor, cb_str, cb_price, wb_price,
                             wa_str, ask_dist_factor, ca_str, ca_price, wa_price):
            score = 50.0
            if wb_str:
                score += min(wb_str * 0.3, 8.0) * bid_dist_factor
            if wa_str:
                score -= min(wa_str * 0.3, 8.0) * ask_dist_factor
            if cb_str and cb_price != wb_price:
                score += min(cb_str * 0.2, 5.0) * bid_dist_factor
            if ca_str and ca_price != wa_price:
                score -= min(ca_str * 0.2, 5.0) * ask_dist_factor
            return max(0.0, min(100.0, score))

        rng = random.Random(55)
        for _ in range(20000):
            wb_str = rng.uniform(0, 60) if rng.random() > 0.15 else None
            wa_str = rng.uniform(0, 60) if rng.random() > 0.15 else None
            bid_dist = rng.uniform(0.0, 1.2)
            ask_dist = rng.uniform(0.0, 1.2)
            wb_price = rng.uniform(90, 100) if wb_str is not None else None
            wa_price = rng.uniform(100, 110) if wa_str is not None else None
            cb_str = rng.uniform(0, 40) if rng.random() > 0.3 else None
            ca_str = rng.uniform(0, 40) if rng.random() > 0.3 else None
            cb_price = ((wb_price if (rng.random() > 0.5 and wb_price is not None) else rng.uniform(90, 100))
                        if cb_str is not None else None)
            ca_price = ((wa_price if (rng.random() > 0.5 and wa_price is not None) else rng.uniform(100, 110))
                        if ca_str is not None else None)

            old = whale_score_old(wb_str, bid_dist, cb_str, cb_price, wb_price,
                                   wa_str, ask_dist, ca_str, ca_price, wa_price)
            new = _score_whale(wb_str, bid_dist, cb_str, cb_price, wb_price,
                                wa_str, ask_dist, ca_str, ca_price, wa_price, side="long")
            self.assertAlmostEqual(new, old, places=9,
                                    msg=f"wb={wb_str} wa={wa_str} cb={cb_str} ca={ca_str}")

    def test_long_via_calculate_orderbook_unchanged_bid_wall_fixture(self):
        reset_state("TESTBATCH7-WHALE-LONGREG/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        res = calculate_orderbook(ob, symbol="TESTBATCH7-WHALE-LONGREG/USDT")
        self.assertEqual(res["whale_bid_wall"], 99.9)
        self.assertAlmostEqual(res["whale_score"], 54.235, places=9)

    # ── 2. Swap-symmetry (role-swap), multi-titik (unit-level DAN via data real) ──

    def test_swap_symmetry_multiple_configs_independent_reconstruction(self):
        """side='short' dibandingkan dgn rekonstruksi independen: role-swap
        MANUAL (bukan cuma manggil ulang _score_whale() itu sendiri) --
        tukar 5 pasangan bid<->ask lalu jalankan formula yg SAMA."""
        configs = [
            (20.0, 1.0, None, None, 100.0, None, 1.0, None, None, None),
            (10.0, 0.8, 15.0, 99.0, 100.0, 25.0, 1.0, None, None, 101.0),
            (20.0, 1.0, 12.0, 100.0, 100.0, None, 1.0, None, None, None),
            (None, 0.5, None, None, None, 30.0, 0.9, 10.0, 105.0, 105.0),
        ]
        for wb_str, bid_dist, cb_str, cb_price, wb_price, wa_str, ask_dist, ca_str, ca_price, wa_price in configs:
            via_side = _score_whale(wb_str, bid_dist, cb_str, cb_price, wb_price,
                                     wa_str, ask_dist, ca_str, ca_price, wa_price, side="short")

            # role-swap manual: ask jadi "bid slot", bid jadi "ask slot"
            score = 50.0
            if wa_str:
                score += min(wa_str * 0.3, 8.0) * ask_dist
            if wb_str:
                score -= min(wb_str * 0.3, 8.0) * bid_dist
            if ca_str and ca_price != wa_price:
                score += min(ca_str * 0.2, 5.0) * ask_dist
            if cb_str and cb_price != wb_price:
                score -= min(cb_str * 0.2, 5.0) * bid_dist
            expected = max(0.0, min(100.0, score))

            self.assertAlmostEqual(via_side, expected, places=9,
                                    msg=f"wb={wb_str} wa={wa_str}")

    def test_swap_symmetry_sum_to_100_multiple_configs(self):
        rng = random.Random(21)
        for _ in range(2000):
            wb_str = rng.uniform(0, 50) if rng.random() > 0.15 else None
            wa_str = rng.uniform(0, 50) if rng.random() > 0.15 else None
            bid_dist = rng.uniform(0.2, 1.0)
            ask_dist = rng.uniform(0.2, 1.0)
            wb_price = rng.uniform(90, 100) if wb_str is not None else None
            wa_price = rng.uniform(100, 110) if wa_str is not None else None
            cb_str = rng.uniform(0, 30) if rng.random() > 0.3 else None
            ca_str = rng.uniform(0, 30) if rng.random() > 0.3 else None
            cb_price = ((wb_price if (rng.random() > 0.5 and wb_price is not None) else rng.uniform(90, 100))
                        if cb_str is not None else None)
            ca_price = ((wa_price if (rng.random() > 0.5 and wa_price is not None) else rng.uniform(100, 110))
                        if ca_str is not None else None)

            long_val = _score_whale(wb_str, bid_dist, cb_str, cb_price, wb_price,
                                     wa_str, ask_dist, ca_str, ca_price, wa_price, side="long")
            short_val = _score_whale(wb_str, bid_dist, cb_str, cb_price, wb_price,
                                      wa_str, ask_dist, ca_str, ca_price, wa_price, side="short")
            self.assertAlmostEqual(long_val + short_val, 100.0, places=6)

    def test_swap_symmetry_via_real_orderbook_multiple_fixtures(self):
        for label in ("bid", "ask", None):
            reset_state(f"WHALESYM-{label}/USDT")
            ob = _make_book_with_wall(wall_side=label)
            res = calculate_orderbook(ob, symbol=f"WHALESYM-{label}/USDT")
            self.assertAlmostEqual(res["whale_score"] + res["whale_score_short"], 100.0,
                                    places=6, msg=label)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_bid_wall_favors_long_not_short(self):
        reset_state("WHALE-BIDWALL/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        res = calculate_orderbook(ob, symbol="WHALE-BIDWALL/USDT")
        self.assertGreater(res["whale_score"], res["whale_score_short"])
        self.assertGreater(res["whale_score"], 50.0)
        self.assertLess(res["whale_score_short"], 50.0)
        self.assertAlmostEqual(res["whale_score"], 54.235, places=9)
        self.assertAlmostEqual(res["whale_score_short"], 45.765, places=9)

    def test_ask_wall_favors_short_not_long(self):
        reset_state("WHALE-ASKWALL/USDT")
        ob = _make_book_with_wall(wall_side="ask")
        res = calculate_orderbook(ob, symbol="WHALE-ASKWALL/USDT")
        self.assertGreater(res["whale_score_short"], res["whale_score"])
        self.assertGreater(res["whale_score_short"], 50.0)
        self.assertLess(res["whale_score"], 50.0)
        self.assertAlmostEqual(res["whale_score"], 45.375, places=9)
        self.assertAlmostEqual(res["whale_score_short"], 54.625, places=9)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_no_wall_at_all(self):
        self.assertEqual(
            _score_whale(None, 0.5, None, None, None, None, 0.5, None, None, None, side="long"),
            50.0,
        )
        self.assertEqual(
            _score_whale(None, 0.5, None, None, None, None, 0.5, None, None, None, side="short"),
            50.0,
        )

    def test_neutral_both_sides_empty_orderbook(self):
        reset_state("EMPTYWHALEOB/USDT")
        res = calculate_orderbook({}, symbol="EMPTYWHALEOB/USDT")
        self.assertEqual(res["whale_score"], 50.0)
        self.assertEqual(res["whale_score_short"], 50.0)

    def test_neutral_both_sides_missing_side(self):
        reset_state("NOBIDSWHALEOB/USDT")
        res = calculate_orderbook({"bids": [], "asks": [(100.0, 1.0)]}, symbol="NOBIDSWHALEOB/USDT")
        self.assertEqual(res["whale_score"], 50.0)
        self.assertEqual(res["whale_score_short"], 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_score_orderbook_data_wires_whale_score_short(self):
        reset_state("WIREWHALEOB/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        ob["symbol"] = "WIREWHALEOB/USDT"
        ind = score_orderbook_data(ob)
        self.assertIsNotNone(ind.whale_score_short)
        self.assertNotEqual(ind.whale_score, ind.whale_score_short)
        self.assertAlmostEqual(ind.whale_score + ind.whale_score_short, 100.0, places=6)

    def test_extract_indicator_scores_ob_score_still_fallback_composite_not_wired_yet(self):
        """[CAKUPAN] Sama spt catatan di TestBatch7ImbalanceScoreShort --
        absorption_score (fungsi 3/3 Batch 7) belum side-aware, composite
        tetap fallback ke long utk SEMENTARA, disengaja bukan lupa."""
        reset_state("SCORERWIREWHALEOB/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        ob["symbol"] = "SCORERWIREWHALEOB/USDT"
        ind = score_orderbook_data(ob)

        iset = IndicatorSet(symbol="SCORERWIREWHALEOB/USDT", timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


def _absorption_wall_book(big=True):
    """Book dgn whale wall (level 0) besar/tidak -- dipakai dua tick berturut
    (besar lalu hilang) utk memicu deteksi absorption."""
    bids = [(100.0, 50.0 if big else 1.0)] + [(100.0 - i * 0.05, 1.0) for i in range(1, 20)]
    asks = [(100.1, 50.0 if big else 1.0)] + [(100.1 + i * 0.05, 1.0) for i in range(1, 20)]
    return {"bids": bids, "asks": asks}


def _flat_book():
    return {"bids": [(100.0 - i * 0.05, 1.0) for i in range(20)],
            "asks": [(100.1 + i * 0.05, 1.0) for i in range(20)]}


class TestBatch7AbsorptionScoreShort(unittest.TestCase):
    """orderbook category, fungsi 3/3 (TERAKHIR di Batch 7): absorption_score.
    DIVERIFIKASI EMPIRIS dulu (domain finite -- cuma 2 boolean, 4 kombinasi,
    di-cek EXHAUSTIVE bukan cuma sampel acak): score+score_short==100 PERSIS
    di semua 4 kombinasi -- koefisien +15/-15 identik di kedua sisi & range
    pre-clamp [35,65] tidak pernah menyentuh batas [0,100]. Jadi pendekatan
    yg dipakai role-swap PENUH (absorbed_bid<->absorbed_ask), pola sama dgn
    _score_whale, BUKAN reflection skalar tunggal spt imbalance_score (tidak
    ada angka kontinu di sini, cuma 2 flag diskrit).

    [CAKUPAN] absorption_score/_short SUDAH dihitung & di-expose lewat
    OrderbookIndicators -- ini fungsi TERAKHIR dari 3/3 Batch 7. Composite
    score_orderbook()/'ob_score' scorer.py MASIH belum diwiring di sini
    (langkah terpisah berikutnya, sesuai rencana yg didiskusikan)."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_ask_absorbed_only(self):
        self.assertEqual(_score_absorption(False, True), 65.0)

    def test_long_score_unchanged_bid_absorbed_only(self):
        self.assertEqual(_score_absorption(True, False), 35.0)

    def test_long_score_unchanged_both_absorbed(self):
        self.assertEqual(_score_absorption(True, True), 50.0)

    def test_long_score_unchanged_none_absorbed(self):
        self.assertEqual(_score_absorption(False, False), 50.0)

    def test_long_score_unchanged_exhaustive_vs_old_formula(self):
        """Domain finite (2 boolean) -- exhaustive, bukan cuma fuzz acak.
        Pakai clamp_score LOKAL orderbook.py (bukan dari engine.core.models)
        -- pelajaran dari bug test imbalance_score."""
        def absorption_score_old(absorbed_bid, absorbed_ask):
            score = 50.0
            if absorbed_ask:
                score += 15.0
            if absorbed_bid:
                score -= 15.0
            return max(0.0, min(100.0, score))

        for absorbed_bid, absorbed_ask in itertools.product([False, True], repeat=2):
            self.assertEqual(
                _score_absorption(absorbed_bid, absorbed_ask, side="long"),
                absorption_score_old(absorbed_bid, absorbed_ask),
                msg=f"bid={absorbed_bid} ask={absorbed_ask}",
            )

    def test_long_via_calculate_orderbook_unchanged_ask_absorbed_fixture(self):
        sym = "TESTBATCH7-ABS-LONGREG/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        # cuma ask wall yg hilang (jadi flat) -- bid wall tetap besar, supaya
        # HANYA absorbed_ask yg True.
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"]}
        res = calculate_orderbook(book2, symbol=sym)
        self.assertTrue(res["absorbed_ask"])
        self.assertFalse(res["absorbed_bid"])
        self.assertEqual(res["absorption_score"], 65.0)

    # ── 2. Swap-symmetry (role-swap), exhaustive DAN via data real ──────────

    def test_swap_symmetry_exhaustive_independent_reconstruction(self):
        """side='short' dibandingkan dgn rekonstruksi independen: role-swap
        MANUAL (bukan cuma manggil ulang _score_absorption() itu sendiri) --
        tukar absorbed_bid<->absorbed_ask lalu jalankan formula yg SAMA."""
        for absorbed_bid, absorbed_ask in itertools.product([False, True], repeat=2):
            via_side = _score_absorption(absorbed_bid, absorbed_ask, side="short")

            # role-swap manual
            swapped_bid, swapped_ask = absorbed_ask, absorbed_bid
            score = 50.0
            if swapped_ask:
                score += 15.0
            if swapped_bid:
                score -= 15.0
            expected = max(0.0, min(100.0, score))

            self.assertEqual(via_side, expected, msg=f"bid={absorbed_bid} ask={absorbed_ask}")

    def test_swap_symmetry_sum_to_100_exhaustive(self):
        for absorbed_bid, absorbed_ask in itertools.product([False, True], repeat=2):
            long_val = _score_absorption(absorbed_bid, absorbed_ask, side="long")
            short_val = _score_absorption(absorbed_bid, absorbed_ask, side="short")
            self.assertEqual(long_val + short_val, 100.0, msg=f"bid={absorbed_bid} ask={absorbed_ask}")

    def test_swap_symmetry_via_real_orderbook_both_scenarios(self):
        for label, wall_big_seq in (("ask-absorbed", ("ask", False)), ("bid-absorbed", ("bid", False))):
            side_absorbed = wall_big_seq[0]
            sym = f"ABSSYM-{label}/USDT"
            reset_state(sym)
            calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
            if side_absorbed == "ask":
                book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"]}
            else:
                book2 = {"bids": _flat_book()["bids"], "asks": _absorption_wall_book(big=True)["asks"]}
            res = calculate_orderbook(book2, symbol=sym)
            self.assertAlmostEqual(res["absorption_score"] + res["absorption_score_short"], 100.0,
                                    places=6, msg=label)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_ask_absorbed_favors_long_not_short(self):
        """Ask wall diserap = breakout signal -- bagus utk long, buruk utk short."""
        sym = "ABS-ASKFAVOR/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"]}
        res = calculate_orderbook(book2, symbol=sym)
        self.assertTrue(res["absorbed_ask"])
        self.assertGreater(res["absorption_score"], res["absorption_score_short"])
        self.assertEqual(res["absorption_score"], 65.0)
        self.assertEqual(res["absorption_score_short"], 35.0)

    def test_bid_absorbed_favors_short_not_long(self):
        """Bid wall diserap = breakdown signal -- bagus utk short, buruk utk long."""
        sym = "ABS-BIDFAVOR/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _flat_book()["bids"], "asks": _absorption_wall_book(big=True)["asks"]}
        res = calculate_orderbook(book2, symbol=sym)
        self.assertTrue(res["absorbed_bid"])
        self.assertGreater(res["absorption_score_short"], res["absorption_score"])
        self.assertEqual(res["absorption_score"], 35.0)
        self.assertEqual(res["absorption_score_short"], 65.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_no_absorption(self):
        self.assertEqual(_score_absorption(False, False, side="long"), 50.0)
        self.assertEqual(_score_absorption(False, False, side="short"), 50.0)

    def test_neutral_both_sides_empty_orderbook(self):
        reset_state("EMPTYABSOB/USDT")
        res = calculate_orderbook({}, symbol="EMPTYABSOB/USDT")
        self.assertEqual(res["absorption_score"], 50.0)
        self.assertEqual(res["absorption_score_short"], 50.0)

    def test_neutral_both_sides_first_tick_no_prior_snapshot(self):
        """Tick pertama (belum ada state.prev_ts) -- absorbed_bid/ask selalu
        False krn belum ada pembanding, absorption_score netral kedua sisi."""
        sym = "ABS-FIRSTTICK/USDT"
        reset_state(sym)
        res = calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        self.assertFalse(res["absorbed_bid"])
        self.assertFalse(res["absorbed_ask"])
        self.assertEqual(res["absorption_score"], 50.0)
        self.assertEqual(res["absorption_score_short"], 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_score_orderbook_data_wires_absorption_score_short(self):
        sym = "WIREABSOB/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"], "symbol": sym}
        ind = score_orderbook_data(book2)
        self.assertIsNotNone(ind.absorption_score_short)
        self.assertNotEqual(ind.absorption_score, ind.absorption_score_short)
        self.assertAlmostEqual(ind.absorption_score + ind.absorption_score_short, 100.0, places=6)

    def test_extract_indicator_scores_ob_score_still_fallback_composite_not_wired_yet(self):
        """[CAKUPAN -- BATCH 7 KOMPLIT 3/3] absorption_score adalah fungsi
        TERAKHIR dari 3 sub-score Batch 7 yg perlu jadi side-aware. Composite
        score_orderbook()/'ob_score' scorer.py MASIH fallback ke long di
        sini -- wiring composite adalah langkah terpisah berikutnya (semua
        3 sub-score sudah siap dipakai composite sekarang)."""
        sym = "SCORERWIREABSOB/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"], "symbol": sym}
        ind = score_orderbook_data(book2)

        iset = IndicatorSet(symbol=sym, timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


if __name__ == "__main__":
    unittest.main()
