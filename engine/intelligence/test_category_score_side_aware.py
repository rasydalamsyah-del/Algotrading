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

import asyncio
import itertools
import random
import unittest

import numpy as np
import pandas as pd

from engine.core.models import IndicatorSet, PatternContext, PatternType, clamp_score
from engine.intelligence.scorer import _extract_indicator_scores, _pick_side_score
from engine.indicators.patterns import score_pattern, _score_single_pattern
from engine.indicators.strength import (
    _score_di, _score_volume, _score_adx, _score_mfi,
    calculate_adx, calculate_volume_analysis, calculate_money_flow,
    score_strength,
)
from engine.indicators.momentum import (
    _score_rsi, _score_macd, _score_stochrsi,
    calculate_rsi_enhanced, calculate_macd_enhanced, calculate_stochastic_rsi,
    score_momentum, _score_vwma,
)
from engine.indicators.oscillators import (
    score_cci, score_williams_r, score_roc, score_oscillators,
    calculate_cci, calculate_williams_r, calculate_roc,
    calculate_roc_slope, calculate_roc_slow, calculate_roc_crossover,
)
from engine.indicators.trend import (
    _score_supertrend_direction, calculate_supertrend, score_trend,
    _score_ema_stack, calculate_ema_stack, _calculate_supertrend_raw,
    _score_cross, calculate_golden_dead_cross,
    _score_vwap_zone, calculate_vwap, calculate_vwap_multiday,
)
from engine.indicators.structure import (
    score_ichimoku, score_structure, score_sar,
    score_pivot, calculate_pivot_points,
    score_fibonacci, score_market_structure, score_donchian,
)
from engine.indicators.orderbook import (
    _score_imbalance, _score_whale, _score_absorption,
    calculate_orderbook, score_orderbook, score_orderbook_data, reset_state,
    IMBALANCE_BULL, IMBALANCE_BEAR,
)
from engine.indicators.volatility import (
    score_volatility, calculate_bollinger_bands, calculate_keltner_channels,
    detect_squeeze, calculate_atr_enhanced, calculate_squeeze,
    _score_bb, _score_kc,
)
from engine.intelligence.observer import _compute_tf_score, observe, MarketObserver, clear_cache
from engine.strategy_base import VolumetricBreakoutStrategyBase


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

    def test_extract_indicator_scores_ob_score_now_wired_side_aware(self):
        """[UPDATE -- wiring composite Batch 7 SELESAI] Assert lama di sini
        menegaskan 'ob_score' fallback ke long krn composite belum wired --
        itu sudah stale. score_orderbook() sekarang side-aware penuh (baca
        imbalance_score_short/whale_score_short/absorption_score_short via
        suffix), jadi scorer.py 'ob_score' (_pick_side_score(iset.orderbook,
        "orderbook_score", side)) otomatis ikut beda antara long/short tanpa
        perlu ubah scorer.py sama sekali. Book di sini bid-heavy (imbalance
        condong long) -> ob_score long harus LEBIH TINGGI dari short."""
        reset_state("SCORERWIREOB/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        ob["symbol"] = "SCORERWIREOB/USDT"
        ind = score_orderbook_data(ob)

        iset = IndicatorSet(symbol="SCORERWIREOB/USDT", timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])
        self.assertGreater(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


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

    def test_extract_indicator_scores_ob_score_now_wired_side_aware(self):
        """[UPDATE -- wiring composite Batch 7 SELESAI] Sama spt catatan di
        TestBatch7ImbalanceScoreShort -- composite sekarang side-aware
        penuh, assert lama 'fallback ke long' sudah stale. Book di sini
        punya whale wall di bid -> ob_score long harus LEBIH TINGGI dari
        short."""
        reset_state("SCORERWIREWHALEOB/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        ob["symbol"] = "SCORERWIREWHALEOB/USDT"
        ind = score_orderbook_data(ob)

        iset = IndicatorSet(symbol="SCORERWIREWHALEOB/USDT", timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])
        self.assertGreater(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


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

    def test_extract_indicator_scores_ob_score_now_wired_side_aware(self):
        """[UPDATE -- BATCH 7 KOMPLIT, composite WIRED] Assert lama di sini
        menandai composite masih fallback ke long krn wiring composite belum
        dikerjakan -- itu sekarang stale, sudah diperbaiki. score_orderbook()
        sudah side-aware penuh (imbalance+whale+absorption semua baca versi
        _short via suffix). Book ini: ask wall diserap (absorbed_ask=True,
        breakout signal) -> absorption_score condong long -> ob_score long
        harus LEBIH TINGGI dari short."""
        sym = "SCORERWIREABSOB/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"], "symbol": sym}
        ind = score_orderbook_data(book2)

        iset = IndicatorSet(symbol=sym, timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertNotEqual(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])
        self.assertGreater(long_result["orderbook"]["ob_score"], short_result["orderbook"]["ob_score"])


class TestBatch7OrderbookScoreCompositeShort(unittest.TestCase):
    """orderbook category, WIRING COMPOSITE (langkah terakhir Batch 7):
    score_orderbook() -- gabungan imbalance(40%)+whale(25%)+absorption(20%)
    +spread(10%)+liquidity(5%) -- sekarang menerima side dan membaca
    imbalance_score{suffix}/whale_score{suffix}/absorption_score{suffix}.
    spread_score, liquidity_score, spoofing_confidence dipakai IDENTIK di
    kedua sisi (arah-agnostic, dikonfirmasi Tahap 0 & fuzz 20rb kasus di
    bawah) -- TIDAK ada versi _short utk ketiganya, dan itu benar.

    [PENTING -- gate bid_ask_imbalance] score_orderbook() punya guard
    `if data.get("bid_ask_imbalance") is None: return 50.0` di AWAL,
    SEBELUM suffix dipilih -- guard ini sama utk long maupun short (bukan
    fallback per-side). Artinya kalau order book kosong/tidak valid,
    KEDUA sisi netral 50.0, bukan cuma salah satu.

    [Simetri] BUKAN sum-to-100 seperti imbalance_score (spread_score &
    liquidity_score tidak dipasangkan long/short, jadi long+short komposit
    TIDAK konstan 100 secara umum). Yang genuinely benar & diverifikasi:
    role-swap MANUAL -- tukar imbalance/whale/absorption (base<->_short)
    sendiri di data dict, spread/liquidity/spoofing_confidence TETAP sama,
    lalu score_orderbook(swapped, 'long') == score_orderbook(original,
    'short') PERSIS (dan sebaliknya). Diverifikasi 20000 kasus fuzy acak,
    0 mismatch -- lihat test_role_swap_manual_reconstruction_fuzz."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_unchanged_static_scenario(self):
        """Skenario imbalance-dominan dari checkpoint sesi: imb 90/10,
        whale/absorption netral 50, spread 80 (default), liquidity 50
        (default), spoof 1.0 (tanpa penalti) -> long harus 69.0 PERSIS
        (36 + 12.5 + 10 + 8 + 2.5)."""
        data = {
            "bid_ask_imbalance": 0.9,
            "imbalance_score": 90.0, "imbalance_score_short": 10.0,
            "whale_score": 50.0, "whale_score_short": 50.0,
            "absorption_score": 50.0, "absorption_score_short": 50.0,
            "spread_score": 80.0,
            "liquidity_score": 50.0,
            "spoofing_confidence": 1.0,
        }
        self.assertEqual(score_orderbook(data, side="long"), 69.0)

    def test_long_score_unchanged_default_side_is_long(self):
        """side param defaultnya 'long' -- tidak boleh berubah perilaku
        pemanggilan lama score_orderbook(data) tanpa argumen side."""
        data = {
            "bid_ask_imbalance": 0.9,
            "imbalance_score": 90.0, "imbalance_score_short": 10.0,
            "whale_score": 50.0, "whale_score_short": 50.0,
            "absorption_score": 50.0, "absorption_score_short": 50.0,
            "spread_score": 80.0, "liquidity_score": 50.0,
            "spoofing_confidence": 1.0,
        }
        self.assertEqual(score_orderbook(data), score_orderbook(data, side="long"))
        self.assertEqual(score_orderbook(data), 69.0)

    def test_long_score_unchanged_spoofing_penalty_applied(self):
        """spoofing_confidence < 1.0 menarik skor ke netral 50 -- perilaku
        ini tidak berubah dgn adanya side param. spoof=0.5 -> score jadi
        rata2 raw & 50 (raw*0.5 + 50*0.5)."""
        data = {
            "bid_ask_imbalance": 0.9,
            "imbalance_score": 90.0, "imbalance_score_short": 10.0,
            "whale_score": 50.0, "whale_score_short": 50.0,
            "absorption_score": 50.0, "absorption_score_short": 50.0,
            "spread_score": 80.0, "liquidity_score": 50.0,
            "spoofing_confidence": 0.5,
        }
        self.assertEqual(score_orderbook(data, side="long"), 59.5)
        self.assertEqual(score_orderbook(data, side="short"), 43.5)

    def test_long_score_unchanged_across_random_fuzz_vs_pre_batch7_formula(self):
        """20000 kasus acak (termasuk bid_ask_imbalance=None utk cek gate)
        dibandingkan thd reimplementasi formula SEBELUM side param
        ditambahkan (selalu baca field base, tanpa suffix) -- byte-identical.
        Pelajaran dari sar/pivot/fib/imbalance: jangan asumsikan wiring
        composite aman tanpa fuzz, walau "cuma nambah parameter"."""
        def old_formula(data):
            if data.get("bid_ask_imbalance") is None:
                return 50.0
            imb = data.get("imbalance_score", 50.0)
            whl = data.get("whale_score", 50.0)
            abso = data.get("absorption_score", 50.0)
            spr = data.get("spread_score", 80.0)
            liq = data.get("liquidity_score", 50.0)
            spoof = data.get("spoofing_confidence", 1.0)
            raw = imb * 0.40 + whl * 0.25 + abso * 0.20 + spr * 0.10 + liq * 0.05
            score = raw * spoof + 50.0 * (1.0 - spoof)
            return max(0.0, min(100.0, score))

        rng = random.Random(7)
        for _ in range(20000):
            data = {
                "bid_ask_imbalance": rng.choice([None, rng.uniform(0.0, 1.0)]),
                "imbalance_score": rng.uniform(0.0, 100.0),
                "imbalance_score_short": rng.uniform(0.0, 100.0),
                "whale_score": rng.uniform(0.0, 100.0),
                "whale_score_short": rng.uniform(0.0, 100.0),
                "absorption_score": rng.uniform(0.0, 100.0),
                "absorption_score_short": rng.uniform(0.0, 100.0),
                "spread_score": rng.uniform(0.0, 100.0),
                "liquidity_score": rng.uniform(0.0, 100.0),
                "spoofing_confidence": rng.uniform(0.0, 1.0),
            }
            self.assertEqual(score_orderbook(data), old_formula(data), msg=f"data={data}")

    def test_long_via_calculate_orderbook_unchanged_bid_heavy_fixture(self):
        reset_state("TESTBATCH7-COMPOSITE-LONGREG/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        data = calculate_orderbook(ob, symbol="TESTBATCH7-COMPOSITE-LONGREG/USDT")
        self.assertAlmostEqual(score_orderbook(data, side="long"), 58.25268421052632, places=9)

    # ── 2. Swap-symmetry, role-swap MANUAL (bukan sum-to-100) ───────────────

    def test_role_swap_manual_reconstruction_fuzz(self):
        """[Independent reconstruction] score_orderbook TIDAK genuinely
        sum-to-100 (spread/liquidity/spoof arah-agnostic, tidak dipasangkan
        long/short) -- jadi verifikasi yg benar bukan long+short==100,
        tapi role-swap MANUAL: tukar imbalance/whale/absorption (base<->
        _short) sendiri di dict, spread/liquidity/spoof TETAP, lalu
        score_orderbook(swapped, 'long') harus == score_orderbook(asli,
        'short') PERSIS -- dan sebaliknya. 20000 kasus acak, 0 mismatch."""
        rng = random.Random(11)
        for _ in range(20000):
            data = {
                "bid_ask_imbalance": rng.uniform(0.0, 1.0),  # non-None -- gate harus lolos
                "imbalance_score": rng.uniform(0.0, 100.0),
                "imbalance_score_short": rng.uniform(0.0, 100.0),
                "whale_score": rng.uniform(0.0, 100.0),
                "whale_score_short": rng.uniform(0.0, 100.0),
                "absorption_score": rng.uniform(0.0, 100.0),
                "absorption_score_short": rng.uniform(0.0, 100.0),
                "spread_score": rng.uniform(0.0, 100.0),
                "liquidity_score": rng.uniform(0.0, 100.0),
                "spoofing_confidence": rng.uniform(0.0, 1.0),
            }
            swapped = dict(data)
            swapped["imbalance_score"], swapped["imbalance_score_short"] = (
                data["imbalance_score_short"], data["imbalance_score"])
            swapped["whale_score"], swapped["whale_score_short"] = (
                data["whale_score_short"], data["whale_score"])
            swapped["absorption_score"], swapped["absorption_score_short"] = (
                data["absorption_score_short"], data["absorption_score"])

            self.assertEqual(score_orderbook(data, side="short"),
                              score_orderbook(swapped, side="long"), msg=f"data={data}")
            self.assertEqual(score_orderbook(data, side="long"),
                              score_orderbook(swapped, side="short"), msg=f"data={data}")

    def test_swap_symmetry_via_real_orderbook_both_directions(self):
        """Verifikasi lewat data orderbook sintetis yg lolos jalur
        calculate_orderbook() PENUH -- bid-heavy vs ask-heavy harus
        menghasilkan arah komposit yang berlawanan (bukan sum-to-100,
        krn itu properti yg SALAH utk composite -- lihat docstring
        kelas)."""
        reset_state("SYM-COMPOSITE-BIDHEAVY/USDT")
        ob_bid = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        data_bid = calculate_orderbook(ob_bid, symbol="SYM-COMPOSITE-BIDHEAVY/USDT")
        long_bid = score_orderbook(data_bid, side="long")
        short_bid = score_orderbook(data_bid, side="short")

        reset_state("SYM-COMPOSITE-ASKHEAVY/USDT")
        ob_ask = _make_synthetic_book(bid_qty_mult=1.0, ask_qty_mult=3.0)
        data_ask = calculate_orderbook(ob_ask, symbol="SYM-COMPOSITE-ASKHEAVY/USDT")
        long_ask = score_orderbook(data_ask, side="long")
        short_ask = score_orderbook(data_ask, side="short")

        self.assertGreater(long_bid, short_bid)
        self.assertGreater(short_ask, long_ask)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_bid_heavy_favors_long_not_short(self):
        reset_state("COMPOSITE-BIDHEAVY/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        data = calculate_orderbook(ob, symbol="COMPOSITE-BIDHEAVY/USDT")
        long_val = score_orderbook(data, side="long")
        short_val = score_orderbook(data, side="short")
        self.assertGreater(long_val, short_val)
        self.assertGreater(long_val, 50.0)
        self.assertLess(short_val, 50.0)

    def test_whale_bid_wall_favors_long_not_short(self):
        reset_state("COMPOSITE-WHALEBID/USDT")
        ob = _make_book_with_wall(wall_side="bid")
        data = calculate_orderbook(ob, symbol="COMPOSITE-WHALEBID/USDT")
        long_val = score_orderbook(data, side="long")
        short_val = score_orderbook(data, side="short")
        self.assertGreater(long_val, short_val)

    def test_absorbed_ask_breakout_favors_long_not_short(self):
        """ask wall diserap (absorbed_ask=True) = breakout signal utk long
        (harga menembus ask) -- composite harus condong long, bukan short."""
        sym = "COMPOSITE-ABSORBED-ASK/USDT"
        reset_state(sym)
        calculate_orderbook(_absorption_wall_book(big=True), symbol=sym)
        book2 = {"bids": _absorption_wall_book(big=True)["bids"], "asks": _flat_book()["asks"]}
        data = calculate_orderbook(book2, symbol=sym)
        self.assertTrue(data["absorbed_ask"])
        long_val = score_orderbook(data, side="long")
        short_val = score_orderbook(data, side="short")
        self.assertGreater(long_val, short_val)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_empty_orderbook(self):
        reset_state("EMPTYCOMPOSITEOB/USDT")
        data = calculate_orderbook({}, symbol="EMPTYCOMPOSITEOB/USDT")
        self.assertIsNone(data["bid_ask_imbalance"])
        self.assertEqual(score_orderbook(data, side="long"), 50.0)
        self.assertEqual(score_orderbook(data, side="short"), 50.0)

    def test_neutral_both_sides_missing_side_in_book(self):
        reset_state("NOBIDSCOMPOSITEOB/USDT")
        data = calculate_orderbook({"bids": [], "asks": [(100.0, 1.0)]}, symbol="NOBIDSCOMPOSITEOB/USDT")
        self.assertEqual(score_orderbook(data, side="long"), 50.0)
        self.assertEqual(score_orderbook(data, side="short"), 50.0)

    def test_neutral_both_sides_perfectly_balanced_book(self):
        """Book seimbang sempurna -- semua sub-score netral 50 kedua sisi,
        spread/liquidity identik -- composite long harus == short."""
        reset_state("PERFBALCOMPOSITEOB/USDT")
        data = calculate_orderbook({"bids": [(100.0, 5.0)], "asks": [(100.1, 5.0)]},
                                    symbol="PERFBALCOMPOSITEOB/USDT")
        long_val = score_orderbook(data, side="long")
        short_val = score_orderbook(data, side="short")
        self.assertAlmostEqual(long_val, short_val, delta=0.1)

    # ── Integrasi (end-to-end via score_orderbook_data & scorer.py) ─────────

    def test_score_orderbook_data_wires_orderbook_score_short(self):
        """Entry point publik score_orderbook_data() -- orderbook_score
        (long) & orderbook_score_short harus keduanya terisi dan berbeda
        utk book yg skewed, TANPA meng-ubah composite_score (alias lama,
        tetap = orderbook_score demi backward-compat)."""
        reset_state("WIRECOMPOSITEOB/USDT")
        ob = _make_synthetic_book(bid_qty_mult=3.0, ask_qty_mult=1.0)
        ob["symbol"] = "WIRECOMPOSITEOB/USDT"
        ind = score_orderbook_data(ob)
        self.assertIsNotNone(ind.orderbook_score_short)
        self.assertNotEqual(ind.orderbook_score, ind.orderbook_score_short)
        self.assertGreater(ind.orderbook_score, ind.orderbook_score_short)
        self.assertEqual(ind.composite_score, ind.orderbook_score)

    def test_extract_indicator_scores_ob_score_side_aware_end_to_end(self):
        """[BATCH 7 KOMPLIT -- verifikasi akhir menyeluruh] Full pipeline:
        book skewed -> calculate_orderbook -> score_orderbook_data ->
        IndicatorSet -> _extract_indicator_scores(side=...) -> scorer.py
        'ob_score' (_pick_side_score) -- semua harus konsisten side-aware
        tanpa scorer.py disentuh sama sekali."""
        reset_state("E2ECOMPOSITEOB/USDT")
        ob = _make_book_with_wall(wall_side="ask")  # ask-heavy -> harus condong short
        ob["symbol"] = "E2ECOMPOSITEOB/USDT"
        ind = score_orderbook_data(ob)

        iset = IndicatorSet(symbol="E2ECOMPOSITEOB/USDT", timeframe="15m")
        iset.orderbook = ind

        long_result = _extract_indicator_scores(iset, side="long")
        short_result = _extract_indicator_scores(iset, side="short")

        self.assertEqual(long_result["orderbook"]["ob_score"], ind.orderbook_score)
        self.assertEqual(short_result["orderbook"]["ob_score"], ind.orderbook_score_short)
        self.assertGreater(short_result["orderbook"]["ob_score"], long_result["orderbook"]["ob_score"])


class TestMTFSubBatchATrendCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A, kategori trend]
    score_trend(): composite_score (bobot dinamis ema 40% / cross 20% /
    supertrend 25% / vwap 15%, dinormalisasi thd sub-indikator yg valid)
    sekarang punya composite_score_short, dihitung pakai raw_weights (ok
    flags) yang SAMA persis dgn long -- ok flags cuma soal ketersediaan
    data (ema9/ema21 not None, dst), bukan soal arah, jadi valid dipakai
    ulang. 4 sub-score (_short) sendiri SUDAH tuntas sejak Batch 5 (proyek
    24 sub-score/8 batch, closed) -- langkah ini cuma wiring composite,
    persis pola Batch 7 utk orderbook.

    [PENTING -- swap-symmetry TIDAK exact di data riil] ema_stack_score,
    supertrend_score, vwap_score terbukti swap PERSIS antara uptrend vs
    downtrend fixture (100<->0, 85<->15, 55<->35), TAPI cross_score dari
    calculate_golden_dead_cross() TERBUKTI TIDAK exact mirror-symmetric
    pada data riil (mis. uptrend cross_score=53.67 vs downtrend
    cross_score_short=58.06 pada seed tertentu -- beda ~4.4 poin). Ini
    perilaku bawaan Batch 5 (di luar cakupan Sub-Batch A), BUKAN bug baru
    dari wiring composite ini -- didokumentasikan eksplisit di sini spy
    tidak diasumsikan simetris di masa depan. Konsekuensinya: composite
    swap-symmetry via data riil TIDAK exact (cuma dekat), jadi test di
    bawah verifikasi ARAH (bukan cuma beda angka) + fuzz ARITMATIKA
    (independent reimplementation ok-flags via helper function yang sama)
    utk buktikan wiring-nya sendiri benar."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_composite_score_unchanged_uptrend_fixture(self):
        df = _make_trend_df(60, direction=1)
        result = score_trend(df, timeframe="15m")
        # Nilai ini HARUS identik dgn versi score_trend() SEBELUM Sub-Batch A
        # (composite_score tidak diubah sama sekali, cuma ditambah baris baru)
        self.assertAlmostEqual(result.composite_score, 82.5, places=4)

    def test_long_composite_score_unchanged_downtrend_fixture(self):
        df = _make_trend_df(60, direction=-1)
        result = score_trend(df, timeframe="15m")
        self.assertAlmostEqual(result.composite_score, 16.0, places=4)

    def test_composite_score_short_static_value_uptrend(self):
        """Skenario terverifikasi manual: ema=0.0(40%)+cross=35.0(20%)+
        supertrend=15.0(25%)+vwap=35.0(15%) = 0+7+3.75+5.25 = 16.0"""
        df = _make_trend_df(60, direction=1)
        result = score_trend(df, timeframe="15m")
        self.assertAlmostEqual(result.composite_score_short, 16.0, places=4)

    def test_composite_score_short_arithmetic_fuzz_vs_independent_reimplementation(self):
        """[Independent reconstruction] 300 fixture acak (n/step/start/vol/tf/
        direction bervariasi) -- ok flags (ema_ok/cross_ok/st_ok/vwap_ok)
        DIHITUNG ULANG SECARA TERPISAH lewat pemanggilan langsung
        calculate_ema_stack/calculate_golden_dead_cross/
        _calculate_supertrend_raw/calculate_vwap_multiday (fungsi yg SAMA
        dipakai score_trend, tapi dipanggil independen di test ini) --
        lalu composite_score_short direkonstruksi manual dan dibandingkan
        thd hasil asli. 0 mismatch = bukti wiring composite BENAR secara
        aritmatika, terlepas dari simetri/tidaknya sub-score itu sendiri."""
        rng = random.Random(42)
        for _ in range(300):
            n = rng.randint(60, 300)
            step = rng.uniform(0.1, 5.0)
            start = rng.uniform(50.0, 500.0)
            vol = rng.uniform(500, 5000)
            tf = rng.choice(["15m", "1h", "4h"])
            direction = rng.choice([1, -1])
            df = _make_trend_df(n, direction=direction, start=start, step=step, vol=vol)

            r = score_trend(df, timeframe=tf)

            errs = []
            ema_result = calculate_ema_stack(df, errs)
            ema_ok = ema_result.ema9 is not None and ema_result.ema21 is not None
            errors_before_cross = len(errs)
            calculate_golden_dead_cross(df, errors=errs)
            cross_ok = len(errs) == errors_before_cross
            st_val, _st_dir, _st_score = _calculate_supertrend_raw(df, errors=errs)
            st_ok = st_val is not None
            skip_vwap = tf in ("1d", "3d", "1w")
            if skip_vwap:
                vwap_ok = False
            else:
                vwap_val, _bands, _vwap_score = calculate_vwap_multiday(df, errs)
                vwap_ok = vwap_val is not None

            raw_weights = {"ema": (40.0, ema_ok), "cross": (20.0, cross_ok),
                           "supertrend": (25.0, st_ok), "vwap": (15.0, vwap_ok)}
            raw_scores_short = {"ema": r.ema_stack_score_short, "cross": r.cross_score_short,
                                 "supertrend": r.supertrend_score_short, "vwap": r.vwap_score_short}
            total_w = sum(w for w, ok in raw_weights.values() if ok)
            if total_w < 1e-6:
                expected = 50.0
            else:
                comp = sum(raw_scores_short[k] * w / total_w for k, (w, ok) in raw_weights.items() if ok)
                expected = max(0.0, min(100.0, round(comp, 4)))
            self.assertAlmostEqual(expected, r.composite_score_short, places=9,
                                    msg=f"n={n} step={step} start={start} vol={vol} tf={tf} dir={direction}")

    # ── 2. Swap-symmetry (near, TIDAK exact -- lihat docstring kelas) ───────

    def test_swap_near_symmetry_via_real_fixture_default_params(self):
        """Utk parameter default (n=60, step=1.0, start=100.0) kebetulan
        exact (semua sub-score termasuk cross_score mirror persis) --
        dicatat sbg bukti bahwa formulanya BISA exact simetris, hanya saja
        tidak DIJAMIN di semua kasus (krn cross_score)."""
        df_up = _make_trend_df(60, direction=1)
        df_down = _make_trend_df(60, direction=-1)
        r_up = score_trend(df_up, timeframe="15m")
        r_down = score_trend(df_down, timeframe="15m")
        self.assertAlmostEqual(r_up.composite_score, r_down.composite_score_short, places=4)
        self.assertAlmostEqual(r_up.composite_score_short, r_down.composite_score, places=4)

    def test_swap_symmetry_not_exact_in_general_documented_cross_score_cause(self):
        """[DOKUMENTASI EKSPLISIT] Fixture n=223/step=0.6455/start=383.70
        (ditemukan lewat fuzz) membuktikan swap TIDAK exact secara umum --
        selisih berasal murni dari cross_score, BUKAN dari ema/supertrend/
        vwap (yang tetap swap persis), dan BUKAN dari wiring composite di
        Sub-Batch A ini (arithmetic fuzz di atas 0 mismatch)."""
        n, step, start, vol, tf = 223, 0.645522234011834, 383.6977248919248, 1602.013342115643, "15m"
        df_up = _make_trend_df(n, direction=1, start=start, step=step, vol=vol)
        df_down = _make_trend_df(n, direction=-1, start=start, step=step, vol=vol)
        r_up = score_trend(df_up, timeframe=tf)
        r_down = score_trend(df_down, timeframe=tf)

        # ema/supertrend/vwap: swap PERSIS
        self.assertAlmostEqual(r_up.ema_stack_score, r_down.ema_stack_score_short, places=6)
        self.assertAlmostEqual(r_up.supertrend_score, r_down.supertrend_score_short, places=6)
        self.assertAlmostEqual(r_up.vwap_score, r_down.vwap_score_short, places=6)
        # cross_score: TIDAK swap persis (selisih nyata, bukan floating-point noise)
        self.assertGreater(abs(r_up.cross_score - r_down.cross_score_short), 1.0)
        # composite pun ikut tidak exact -- tapi selisihnya harus SEKECIL
        # kontribusi cross_score (bobot 20%), bukan meleset jauh
        diff = abs(r_up.composite_score - r_down.composite_score_short)
        self.assertLess(diff, 20.0 * 0.25)  # batas atas longgar, bukan angka presisi

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(80, direction=1)
        result = score_trend(df, timeframe="15m")
        self.assertGreater(result.composite_score, result.composite_score_short)
        self.assertGreater(result.composite_score, 50.0)
        self.assertLess(result.composite_score_short, 50.0)

    def test_downtrend_favors_short_not_long(self):
        df = _make_trend_df(80, direction=-1)
        result = score_trend(df, timeframe="15m")
        self.assertGreater(result.composite_score_short, result.composite_score)
        self.assertGreater(result.composite_score_short, 50.0)
        self.assertLess(result.composite_score, 50.0)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        result = score_trend(empty_df, timeframe="15m")
        self.assertEqual(result.composite_score, 50.0)
        self.assertEqual(result.composite_score_short, 50.0)

    def test_neutral_both_sides_single_row_df(self):
        one_row = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0],
                                 "close": [100.0], "volume": [1000.0]})
        result = score_trend(one_row, timeframe="15m")
        self.assertEqual(result.composite_score, 50.0)
        self.assertEqual(result.composite_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(80, direction=1)
        result = score_trend(df, timeframe="15m")
        self.assertIsNotNone(result.composite_score_short)
        self.assertNotEqual(result.composite_score, result.composite_score_short)


class TestMTFSubBatchAMomentumCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A.2, momentum]

    [BUG-FIX KRITIS DITEMUKAN SESI INI -- bukan cuma wiring composite]
    score_momentum() -- jalur PRODUKSI ASLI yg dipanggil observer.py -- SEJAK
    BATCH 3 tidak pernah menyalin rsi_score_short/macd_score_short/
    stoch_score_short dari calculate_rsi_enhanced()/calculate_macd_enhanced()/
    calculate_stochastic_rsi() ke objek result yang dikembalikan. Artinya
    iset.momentum.rsi_score_short dkk SELALU None di produksi, walau fungsi
    sub-kalkulasinya sendiri sudah benar sejak Batch 3. Batch 3 lolos test
    krn test integrasinya membangun IndicatorSet MANUAL (copy field satu2
    dari calculate_rsi_enhanced() dkk langsung ke iset.momentum), bypass
    score_momentum() -- tidak pernah menguji jalur produksi yg sesungguhnya.
    Test di bawah SEKARANG menguji lewat score_momentum() end-to-end supaya
    tidak terulang.

    [SUB-SKOR BARU] vwma_score (VWMA vs SMA, confirm volume-weighted momentum)
    TIDAK PERNAH dapat treatment side-aware di Batch 3 (yg cuma cakup rsi/
    macd/stochrsi) -- ditambahkan skrng via _score_vwma() (input-reflection,
    pola sama dgn rsi/macd/stochrsi), field baru vwma_score_short.

    [Composite] composite_score_short diwiring reuse ok-flags & bobot yg
    SAMA dgn long (0.35/0.30/0.22/0.13 utk rsi/macd/stoch/vwma)."""

    # ── Bug-fix regression (PALING PENTING) ──────────────────────────────────

    def test_bugfix_rsi_macd_stoch_score_short_no_longer_none_via_score_momentum(self):
        """Sebelum fix: ketiga field ini SELALU None lewat score_momentum(),
        walau data cukup (ok=True). Sesudah fix: harus berupa float nyata."""
        df = _make_trend_df(80, direction=1)
        r = score_momentum(df)
        self.assertIsNotNone(r.rsi_score_short)
        self.assertIsNotNone(r.macd_score_short)
        self.assertIsNotNone(r.stoch_score_short)
        self.assertIsInstance(r.rsi_score_short, float)
        self.assertIsInstance(r.macd_score_short, float)
        self.assertIsInstance(r.stoch_score_short, float)

    def test_bugfix_values_match_direct_subfunction_calls(self):
        """rsi_score_short dkk hasil score_momentum() HARUS identik dgn
        pemanggilan langsung calculate_rsi_enhanced() dkk (sumber asli)."""
        df = _make_trend_df(80, direction=-1)
        r = score_momentum(df)
        rsi_direct = calculate_rsi_enhanced(df)
        macd_direct = calculate_macd_enhanced(df)
        stoch_direct = calculate_stochastic_rsi(df)
        self.assertEqual(r.rsi_score_short, rsi_direct.rsi_score_short)
        self.assertEqual(r.macd_score_short, macd_direct.macd_score_short)
        self.assertEqual(r.stoch_score_short, stoch_direct.stoch_score_short)

    # ── vwma_score_short (sub-skor baru) ─────────────────────────────────────

    def test_vwma_score_reflection_symmetric_around_midpoint(self):
        for dp in [2.0, 1.0, -1.0, -2.0]:
            long_s = _score_vwma(dp, side="long")
            short_s = _score_vwma(dp, side="short")
            # midpoint band-nya 51.5 (bukan 50 persis -- quirk bawaan formula
            # asli yg direfactor verbatim, BUKAN diperkenalkan sesi ini)
            self.assertAlmostEqual(long_s + short_s, 103.0, delta=1.5)

    def test_vwma_score_short_is_reflection_of_long(self):
        self.assertEqual(_score_vwma(2.0, side="short"), _score_vwma(-2.0, side="long"))
        self.assertEqual(_score_vwma(-2.0, side="short"), _score_vwma(2.0, side="long"))

    def test_vwma_score_short_populated_in_score_momentum(self):
        df = _make_trend_df(80, direction=1)
        r = score_momentum(df)
        self.assertIsNotNone(r.vwma_score_short)

    def test_vwma_score_short_neutral_when_insufficient_data(self):
        """< 20 bar -> vwma_ok=False -> vwma_score_short harus fallback
        neutral (SCORE_NEUTRAL), bukan None (konsisten dgn vwma_score long
        yg juga fallback ke default 50.0)."""
        df = _make_trend_df(10, direction=1)
        r = score_momentum(df)
        self.assertEqual(r.vwma_score, 50.0)
        self.assertEqual(r.vwma_score_short, 50.0)

    # ── Composite arithmetic fuzz (independent reimplementation) ────────────

    def test_composite_score_short_arithmetic_fuzz_vs_independent_reimplementation(self):
        rng = random.Random(99)
        _RSI_W, _MACD_W, _STOCH_W, _VWMA_W = 0.35, 0.30, 0.22, 0.13
        for _ in range(300):
            n = rng.randint(10, 300)
            step = rng.uniform(0.01, 5.0)
            start = rng.uniform(20.0, 500.0)
            vol = rng.uniform(100, 5000)
            direction = rng.choice([1, -1])
            df = _make_trend_df(n, direction=direction, start=start, step=step, vol=vol)

            r = score_momentum(df)

            rsi_res = calculate_rsi_enhanced(df)
            macd_res = calculate_macd_enhanced(df)
            stoch_res = calculate_stochastic_rsi(df)
            rsi_ok = rsi_res.rsi is not None
            macd_ok = macd_res.macd_line is not None
            stoch_ok = stoch_res.stoch_k is not None

            vwma_ok = False
            vwma_score_short = None
            try:
                if "close" in df.columns and "volume" in df.columns and len(df) >= 20:
                    period = 20
                    pv = df["close"] * df["volume"]
                    vwma_val = float(
                        pv.rolling(period, min_periods=period).sum().iloc[-1]
                        / df["volume"].rolling(period, min_periods=period).sum().replace(0, float("nan")).iloc[-1]
                    )
                    sma_val = float(df["close"].rolling(period, min_periods=period).mean().iloc[-1])
                    if not (pd.isna(vwma_val) or pd.isna(sma_val)):
                        diff_pct = (vwma_val - sma_val) / sma_val * 100 if sma_val > 0 else 0.0
                        vwma_score_short = _score_vwma(diff_pct, side="short")
                        vwma_ok = True
            except Exception:
                pass

            raw_weights = {"rsi": (_RSI_W, rsi_ok), "macd": (_MACD_W, macd_ok),
                           "stoch": (_STOCH_W, stoch_ok), "vwma": (_VWMA_W, vwma_ok)}
            raw_scores_short = {"rsi": rsi_res.rsi_score_short, "macd": macd_res.macd_score_short,
                                 "stoch": stoch_res.stoch_score_short, "vwma": vwma_score_short}
            total_w = sum(w for w, ok in raw_weights.values() if ok)
            if total_w < 1e-6:
                expected = 50.0
            else:
                comp = sum(raw_scores_short[k] * w / total_w for k, (w, ok) in raw_weights.items() if ok)
                expected = max(0.0, min(100.0, round(comp, 4)))
            self.assertAlmostEqual(expected, r.composite_score_short, places=6,
                                    msg=f"n={n} step={step} start={start} vol={vol} dir={direction}")

    # ── Swap-symmetry via data riil ──────────────────────────────────────────

    def test_swap_symmetry_via_real_fixture(self):
        df_up = _make_trend_df(80, direction=1)
        df_down = _make_trend_df(80, direction=-1)
        r_up = score_momentum(df_up)
        r_down = score_momentum(df_down)
        self.assertAlmostEqual(r_up.composite_score, r_down.composite_score_short, places=4)
        self.assertAlmostEqual(r_up.composite_score_short, r_down.composite_score, places=4)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────
    # [TEMUAN -- didokumentasikan, bukan bug] rsi_score+stoch_score (bobot
    # gabungan 0.57) bersifat KONTRARIAN (RSI/Stoch ekstrem overbought =
    # skor RENDAH/bearish, ekspektasi reversal) -- lebih berat drpd
    # macd_score (0.30, trend-following murni). Akibatnya: uptrend
    # MONOTON KUAT (RSI/Stoch overbought ekstrem) justru bikin composite
    # condong ke SHORT, bukan long -- kebalikan dari intuisi naif "uptrend
    # = bullish = long". Ini bukan bug wiring composite (fuzz aritmatika
    # di atas 0 mismatch); ini karakteristik desain kontrarian yg sudah ada
    # sejak Batch 3 pada rsi_score/stoch_score itu sendiri. Tetap
    # membuktikan "bukan cuma beda angka" krn arahnya genuinely bermakna
    # (overbought ekstrem -> ekspektasi reversal), cuma bukan arah yg
    # intuitif kalau tidak paham desainnya.

    def test_extreme_monotonic_uptrend_favors_short_not_long_contrarian(self):
        df = _make_trend_df(80, direction=1)
        r = score_momentum(df)
        self.assertGreater(r.rsi_score_short, r.rsi_score)   # RSI overbought ekstrem -> kontrarian
        self.assertGreater(r.stoch_score_short, r.stoch_score)
        self.assertGreater(r.composite_score_short, r.composite_score)

    def test_extreme_monotonic_downtrend_favors_long_not_short_contrarian(self):
        df = _make_trend_df(80, direction=-1)
        r = score_momentum(df)
        self.assertGreater(r.rsi_score, r.rsi_score_short)   # RSI oversold ekstrem -> kontrarian
        self.assertGreater(r.stoch_score, r.stoch_score_short)
        self.assertGreater(r.composite_score, r.composite_score_short)

    # ── Neutral-alignment ─────────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        r = score_momentum(empty_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(80, direction=1)
        r = score_momentum(df)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)


class TestMTFSubBatchAStrengthCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A.3, strength]

    score_strength(): composite (adx 35% + di 15% + volume 35% + mfi 15%,
    dinormalisasi thd sub-indikator valid) sekarang punya composite_score_short.

    [BUG-FIX KRITIS -- sudah diperbaiki sesi lalu, direverifikasi di sini]
    di_score_short/volume_score_short/mfi_score_short SEBELUMNYA tidak
    pernah disalin dari calculate_adx()/calculate_volume_analysis()/
    calculate_money_flow() ke result di score_strength() -- bug identik
    dgn momentum.py, sejak Batch 2/3. Sudah diperbaiki; test di bawah
    mengunci perbaikan itu supaya tidak regresi.

    [adx_score ARAH-AGNOSTIC] adx_score cuma ukur MAGNITUDE kekuatan tren
    (ADX selalu >=0, tidak ada arah) -- TIDAK punya versi _short, dipakai
    IDENTIK di kedua sisi composite, sama seperti spread_score/
    liquidity_score di orderbook (Batch 7). di_score/volume_score/
    mfi_score genuinely directional, pakai versi _short masing2.

    [Swap-symmetry] TERBUKTI EXACT via fuzz 200 fixture uptrend/downtrend
    riil (beda dgn trend yg ada quirk cross_score) -- strength tidak
    punya sub-score bermasalah spt itu."""

    # ── Bug-fix regression ───────────────────────────────────────────────────

    def test_bugfix_di_volume_mfi_score_short_no_longer_none_via_score_strength(self):
        df = _make_trend_df(80, direction=1)
        r = score_strength(df)
        self.assertIsNotNone(r.di_score_short)
        self.assertIsNotNone(r.volume_score_short)
        self.assertIsNotNone(r.mfi_score_short)

    def test_bugfix_values_match_direct_subfunction_calls(self):
        df = _make_trend_df(80, direction=-1)
        r = score_strength(df)
        adx_direct = calculate_adx(df)
        vol_direct = calculate_volume_analysis(df)
        mfi_direct = calculate_money_flow(df)
        self.assertEqual(r.di_score_short, adx_direct.di_score_short)
        self.assertEqual(r.volume_score_short, vol_direct.volume_score_short)
        self.assertEqual(r.mfi_score_short, mfi_direct.mfi_score_short)

    # ── adx_score arah-agnostic ──────────────────────────────────────────────

    def test_adx_score_identical_both_sides_uptrend(self):
        df = _make_trend_df(80, direction=1)
        r = score_strength(df)
        # adx_score TIDAK ada versi _short -- dipakai identik, dibuktikan
        # lewat composite yg reuse result.adx_score utk kedua sisi
        self.assertFalse(hasattr(r, "adx_score_short"))

    # ── Composite arithmetic fuzz (independent reimplementation) ────────────

    def test_composite_score_short_arithmetic_fuzz_vs_independent_reimplementation(self):
        rng = random.Random(123)
        _ADX_W, _DI_W, _VOL_W, _MFI_W = 0.35, 0.15, 0.35, 0.15
        for i in range(300):
            if i % 2 == 0:
                n = rng.randint(5, 300)
                step = rng.uniform(0.01, 5.0)
                start = rng.uniform(20.0, 500.0)
                vol = rng.uniform(100, 5000)
                direction = rng.choice([1, -1])
                df = _make_trend_df(n, direction=direction, start=start, step=step, vol=vol)
            else:
                seed = rng.randint(0, 10000)
                bias = rng.uniform(-0.2, 0.2)
                n = rng.randint(5, 200)
                df = _make_choppy_df(seed=seed, bias=bias, n=n)

            r = score_strength(df)

            adx_res = calculate_adx(df)
            vol_res = calculate_volume_analysis(df)
            mfi_res = calculate_money_flow(df)
            adx_ok = adx_res.adx is not None
            di_ok = adx_res.plus_di is not None
            vol_ok = vol_res.volume_ratio is not None
            mfi_ok = mfi_res.mfi is not None

            raw_weights = {"adx": (_ADX_W, adx_ok), "di": (_DI_W, di_ok),
                           "vol": (_VOL_W, vol_ok), "mfi": (_MFI_W, mfi_ok)}
            raw_scores_short = {"adx": adx_res.adx_score, "di": adx_res.di_score_short,
                                 "vol": vol_res.volume_score_short, "mfi": mfi_res.mfi_score_short}
            total_w = sum(w for w, ok in raw_weights.values() if ok)
            if total_w < 1e-6:
                expected = 50.0
            else:
                comp = sum(raw_scores_short[k] * w / total_w for k, (w, ok) in raw_weights.items() if ok)
                expected = max(0.0, min(100.0, round(comp, 4)))
            self.assertAlmostEqual(expected, r.composite_score_short, places=6, msg=f"n={n}")

    # ── Swap-symmetry via data riil (exact) ──────────────────────────────────

    def test_swap_symmetry_exact_via_real_fixture_fuzz(self):
        rng = random.Random(55)
        for _ in range(200):
            n = rng.randint(30, 250)
            step = rng.uniform(0.1, 5.0)
            start = rng.uniform(50.0, 500.0)
            vol = rng.uniform(500, 5000)
            df_up = _make_trend_df(n, direction=1, start=start, step=step, vol=vol)
            df_down = _make_trend_df(n, direction=-1, start=start, step=step, vol=vol)
            r_up = score_strength(df_up)
            r_down = score_strength(df_down)
            self.assertAlmostEqual(r_up.composite_score, r_down.composite_score_short, places=6)
            self.assertAlmostEqual(r_up.composite_score_short, r_down.composite_score, places=6)
            # adx_score harus identik persis (arah-agnostic)
            self.assertAlmostEqual(r_up.adx_score, r_down.adx_score, places=6)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(80, direction=1)
        r = score_strength(df)
        self.assertGreater(r.composite_score, r.composite_score_short)

    def test_downtrend_favors_short_not_long(self):
        df = _make_trend_df(80, direction=-1)
        r = score_strength(df)
        self.assertGreater(r.composite_score_short, r.composite_score)

    # ── Neutral-alignment ─────────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        r = score_strength(empty_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(80, direction=1)
        r = score_strength(df)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)


class TestMTFSubBatchAPatternsCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A.4, patterns]

    score_pattern(): composite = pattern_score*0.70 + context_score*0.30.

    [SUB-SKOR BARU] context_score (NEAR_SUPPORT=70/NEAR_RESISTANCE=32/
    MID_RANGE=52/UNKNOWN=50) TIDAK PERNAH dapat treatment side-aware di
    Batch 1 (yg cuma cakup pattern_score) -- padahal genuinely directional:
    dekat support = bullish (bagus long), dekat resistance = bearish (bagus
    short). Dibuatkan context_score_short via input-reflection (100-x),
    pola SAMA PERSIS dgn pattern_score_short (Batch 1, Class B) krn
    context_score_map juga sudah dianggap "mirror-symmetric by construction"
    (tiap tipe konteks punya makna berlawanan yg jelas utk sisi lain).

    [Provably sum-to-100] KARENA pattern_score_short DAN context_score_short
    SAMA-SAMA reflection (100-x), composite_score_short = 100 -
    composite_score EXACT utk skenario yang SAMA (bukan cuma via swap
    skenario beda spt trend/momentum/strength) -- properti aljabar murni:
    (100-p)*0.7 + (100-c)*0.3 = 100 - (p*0.7+c*0.3). Ini kasus PALING BERSIH
    di antara semua kategori MTF sejauh ini."""

    # ── Sub-skor baru: context_score_short ───────────────────────────────────

    def test_context_score_short_is_exact_complement(self):
        """[CATATAN] context=UNKNOWN TIDAK bisa dipaksakan lewat parameter
        kalau df >= 3 bar -- score_pattern() otomatis manggil
        get_pattern_context() utk auto-detect (baris awal fungsi), jadi
        UNKNOWN yang di-pass otomatis tertimpa hasil deteksi (MID_RANGE di
        fixture ini). Bukan bug -- perilaku existing sejak awal. Makanya
        case UNKNOWN literal tidak diuji di sini (di luar cakupan), cukup
        3 context yang genuinely reachable."""
        cases_ctx = [
            (PatternContext.NEAR_SUPPORT, 70.0, 30.0),
            (PatternContext.NEAR_RESISTANCE, 32.0, 68.0),
            (PatternContext.MID_RANGE, 52.0, 48.0),
        ]
        for ctx, expected_long, expected_short in cases_ctx:
            df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
            r = score_pattern(df, context=ctx)
            self.assertAlmostEqual(r.context_score, expected_long, places=6)
            self.assertAlmostEqual(r.context_score_short, expected_short, places=6)

    # ── Composite arithmetic fuzz (independent reimplementation) ────────────

    def test_composite_score_short_arithmetic_fuzz(self):
        rng = random.Random(77)
        contexts = [PatternContext.NEAR_SUPPORT, PatternContext.NEAR_RESISTANCE,
                    PatternContext.MID_RANGE, PatternContext.UNKNOWN]
        context_score_map = {
            PatternContext.NEAR_SUPPORT: 70.0, PatternContext.NEAR_RESISTANCE: 32.0,
            PatternContext.MID_RANGE: 52.0, PatternContext.UNKNOWN: 50.0,
        }
        for _ in range(300):
            n = rng.randint(3, 60)
            base = rng.uniform(20, 500)
            ctx = rng.choice(contexts)
            higher_tf = rng.choice([True, False, None])
            closes = [base + rng.uniform(-2, 2) for _ in range(n)]
            opens  = [c + rng.uniform(-1, 1) for c in closes]
            highs  = [max(o, c) + rng.uniform(0, 1) for o, c in zip(opens, closes)]
            lows   = [min(o, c) - rng.uniform(0, 1) for o, c in zip(opens, closes)]
            vols   = [rng.uniform(500, 5000) for _ in range(n)]
            df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                                "close": closes, "volume": vols})

            r = score_pattern(df, context=ctx, higher_tf_aligned=higher_tf)

            context_score = context_score_map.get(r.pattern_context, 50.0)
            context_score_short = max(0.0, min(100.0, round(100.0 - context_score, 4)))

            if r.primary_pattern == PatternType.NONE:
                expected_long  = max(0.0, min(100.0, round(50.0 * 0.7 + context_score * 0.3, 4)))
                expected_short = max(0.0, min(100.0, round(50.0 * 0.7 + context_score_short * 0.3, 4)))
            else:
                expected_long  = max(0.0, min(100.0, round(r.pattern_score * 0.7 + context_score * 0.3, 4)))
                expected_short = max(0.0, min(100.0, round(r.pattern_score_short * 0.7 + context_score_short * 0.3, 4)))

            self.assertAlmostEqual(expected_long, r.composite_score, places=6)
            self.assertAlmostEqual(expected_short, r.composite_score_short, places=6)

    # ── Sum-to-100 exact (properti aljabar murni utk kategori ini) ──────────

    def test_composite_sum_to_100_exact_bullish_near_support(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        r = score_pattern(df, context=PatternContext.NEAR_SUPPORT)
        self.assertAlmostEqual(r.composite_score + r.composite_score_short, 100.0, places=6)

    def test_composite_sum_to_100_exact_bearish_near_resistance(self):
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        r = score_pattern(df, context=PatternContext.NEAR_RESISTANCE)
        self.assertAlmostEqual(r.composite_score + r.composite_score_short, 100.0, places=6)

    def test_composite_sum_to_100_exact_fuzz(self):
        rng = random.Random(88)
        contexts = [PatternContext.NEAR_SUPPORT, PatternContext.NEAR_RESISTANCE,
                    PatternContext.MID_RANGE, PatternContext.UNKNOWN]
        for _ in range(300):
            n = rng.randint(3, 60)
            base = rng.uniform(20, 500)
            ctx = rng.choice(contexts)
            closes = [base + rng.uniform(-2, 2) for _ in range(n)]
            opens  = [c + rng.uniform(-1, 1) for c in closes]
            highs  = [max(o, c) + rng.uniform(0, 1) for o, c in zip(opens, closes)]
            lows   = [min(o, c) - rng.uniform(0, 1) for o, c in zip(opens, closes)]
            vols   = [rng.uniform(500, 5000) for _ in range(n)]
            df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                                "close": closes, "volume": vols})
            r = score_pattern(df, context=ctx)
            self.assertAlmostEqual(r.composite_score + r.composite_score_short, 100.0, places=4)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_bullish_pattern_near_support_favors_long(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        r = score_pattern(df, context=PatternContext.NEAR_SUPPORT)
        self.assertGreater(r.composite_score, r.composite_score_short)
        self.assertGreater(r.composite_score, 50.0)

    def test_bearish_pattern_near_resistance_favors_short(self):
        df = _make_ohlcv(_BEARISH_ENGULFING_BARS)
        r = score_pattern(df, context=PatternContext.NEAR_RESISTANCE)
        self.assertGreater(r.composite_score_short, r.composite_score)
        self.assertGreater(r.composite_score_short, 50.0)

    # ── Neutral-alignment ─────────────────────────────────────────────────────

    def test_neutral_both_sides_insufficient_bars(self):
        tiny_df = _make_ohlcv(_BULLISH_ENGULFING_BARS).iloc[:2]
        r = score_pattern(tiny_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)
        self.assertEqual(r.context_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_ohlcv(_BULLISH_ENGULFING_BARS)
        r = score_pattern(df, context=PatternContext.NEAR_SUPPORT)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)


class TestMTFSubBatchAOscillatorsCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A.5, oscillators]

    score_oscillators(): composite = weighted-avg(cci 0.35, williams 0.25,
    roc 0.40), exclude None + renormalize (bug-fix lama, tidak disentuh).
    cci_score_short/williams_r_score_short/roc_score_short SUDAH dihitung
    inline sejak Batch 4 (tidak ada bug copy-omission spt momentum/strength).
    composite_score_short diwiring reuse available_w (ok-flags) yang SAMA.

    [PENTING] oscillators.py punya clamp_score() LOKAL SENDIRI (cuma clip
    0-100, TANPA round ke 4 desimal) -- BEDA dari clamp_score di models.py.
    Ini perilaku lama (composite_score long jg begitu), bukan bug baru.
    Test di bawah pakai places=9 (bukan places=4) utk fuzz krn presisi
    penuh, bukan dibulatkan.

    [PENTING -- swap-symmetry TIDAK exact, LEBIH parah dari trend] cci_score
    & williams_r_score swap PERSIS, TAPI roc_score TIDAK (dikonfirmasi 200/200
    fixture fuzz mismatch) -- roc_score dihitung dari ROC (rate-of-change),
    PERSENTASE relatif thd harga N-bar lalu, sama seperti gap_pct/cross_score
    di trend: anchor harga beda antara uptrend/downtrend (walau harga absolut
    mirror) bikin persentase tidak simetris. roc berbobot PALING BESAR (0.40)
    jadi dampaknya ke composite lebih terasa drpd cross_score di trend. Bukan
    bug wiring (fuzz aritmatika independen 0 mismatch di bawah), karakteristik
    calculate_roc() dari Batch 4, di luar cakupan Sub-Batch A."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_composite_score_unchanged_uptrend(self):
        df = _make_trend_df(80, direction=1)
        r = score_oscillators(df)
        # nilai ini HARUS sama dgn versi score_oscillators() SEBELUM
        # Sub-Batch A.5 (composite_score/long tidak diubah sama sekali)
        expected = (r.cci_score * 0.35 + r.williams_r_score * 0.25 + r.roc_score * 0.40)
        self.assertAlmostEqual(r.composite_score, max(0.0, min(100.0, expected)), places=9)

    # ── Composite arithmetic fuzz (independent reimplementation) ────────────

    def test_composite_score_short_arithmetic_fuzz(self):
        rng = random.Random(321)
        _W = {"cci": 0.35, "williams": 0.25, "roc": 0.40}
        for i in range(300):
            if i % 2 == 0:
                n = rng.randint(3, 200)
                step = rng.uniform(0.01, 5.0)
                start = rng.uniform(20.0, 500.0)
                direction = rng.choice([1, -1])
                df = _make_trend_df(n, direction=direction, start=start, step=step, vol=1000)
            else:
                seed = rng.randint(0, 10000)
                bias = rng.uniform(-0.2, 0.2)
                n = rng.randint(3, 150)
                df = _make_choppy_df(seed=seed, bias=bias, n=n)

            r = score_oscillators(df)

            cci = calculate_cci(df)
            wr = calculate_williams_r(df)
            roc = calculate_roc(df)
            roc_slope = calculate_roc_slope(df)
            roc_slow = calculate_roc_slow(df)
            roc_crossover = calculate_roc_crossover(roc, roc_slow)

            roc_score_short = score_roc(roc, roc_slope, roc_crossover, side="short") if roc is not None else None
            cci_score_short = score_cci(cci, side="short") if cci is not None else None
            wr_score_short = score_williams_r(wr, side="short") if wr is not None else None

            available_w = {}
            available_val_short = {}
            if cci is not None:
                available_w["cci"] = _W["cci"]; available_val_short["cci"] = cci_score_short
            if wr is not None:
                available_w["williams"] = _W["williams"]; available_val_short["williams"] = wr_score_short
            if roc is not None:
                available_w["roc"] = _W["roc"]; available_val_short["roc"] = roc_score_short

            total_w = sum(available_w.values())
            if total_w <= 0:
                expected = 50.0
            else:
                raw = sum(available_val_short[k] * available_w[k] for k in available_w) / total_w
                expected = max(0.0, min(100.0, raw))
            self.assertAlmostEqual(expected, r.composite_score_short, places=9)

    # ── Swap-symmetry per komponen (isolasi sumber asimetri) ────────────────

    def test_cci_and_williams_swap_exact_but_roc_does_not(self):
        df_up = _make_trend_df(80, direction=1)
        df_down = _make_trend_df(80, direction=-1)
        r_up = score_oscillators(df_up)
        r_down = score_oscillators(df_down)
        self.assertAlmostEqual(r_up.cci_score, r_down.cci_score_short, places=6)
        self.assertAlmostEqual(r_up.williams_r_score, r_down.williams_r_score_short, places=6)
        # roc_score TIDAK swap persis -- didokumentasikan, bukan bug
        self.assertGreater(abs(r_up.roc_score - r_down.roc_score_short), 1.0)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────
    # [TEMUAN -- didokumentasikan, LEBIH parah dari momentum] cci_score &
    # williams_r_score BERSIFAT KONTRARIAN (overbought/oversold ekstrem =
    # skor rendah/tinggi, ekspektasi reversal) sementara roc_score TREND-
    # FOLLOWING murni DAN tidak exact mirror (lihat temuan di atas) --
    # kombinasi keduanya bikin arah composite PENUH thd tren monoton
    # GENUINELY TIDAK RELIABLE/konsisten (diverifikasi: dari 200 fixture
    # uptrend acak, cuma 4 yang composite>55, 0 yang <45 -- mayoritas
    # nyaris netral/ambigu; downtrend malah 108/200 composite>55, JUSTRU
    # SERING kebalikan dari intuisi). Bukan bug wiring (fuzz aritmatika di
    # atas 0 mismatch) -- ini karakteristik desain gabungan kontrarian+
    # trend-following+asimetri ROC. "Bukan cuma beda angka" dibuktikan di
    # level KOMPONEN (test_cci_and_williams_swap_exact_but_roc_does_not di
    # atas -- cci/williams beda ARAH scr bermakna & exact), bukan dipaksakan
    # di level composite penuh yang memang ambigu by design.

    def test_component_level_direction_is_meaningful_even_if_composite_is_ambiguous(self):
        """cci_score & williams_r_score individually HARUS beda arah scr
        bermakna antara uptrend/downtrend (bukan cuma beda angka), meski
        composite gabungannya bisa ambigu krn tarik-menarik kontrarian vs
        trend-following."""
        df_up = _make_trend_df(80, direction=1)
        df_down = _make_trend_df(80, direction=-1)
        r_up = score_oscillators(df_up)
        r_down = score_oscillators(df_down)
        # cci: uptrend sedikit condong long (mild, trend-following character)
        self.assertGreater(r_up.cci_score, r_up.cci_score_short)
        self.assertGreater(r_down.cci_score_short, r_down.cci_score)
        # williams_r: uptrend overbought -> kontrarian short; downtrend
        # oversold -> kontrarian long (KEBALIKAN arah dari cci, sengaja)
        self.assertGreater(r_up.williams_r_score_short, r_up.williams_r_score)
        self.assertGreater(r_down.williams_r_score, r_down.williams_r_score_short)

    # ── Neutral-alignment ─────────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        r = score_oscillators(empty_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(5, direction=1)
        r = score_oscillators(df)
        self.assertIsNone(r.cci)   # CCI butuh >=20 bar
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(80, direction=1)
        r = score_oscillators(df)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)


class TestMTFSubBatchAStructureCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch A.6 (TERAKHIR di
    Sub-Batch A), structure]

    score_structure(): composite = Ichimoku(0.25)+MarketStructure(0.20)+
    SAR(0.15)+Pivot(0.15)+Donchian(0.15)+Fib(0.10), exclude+renormalize
    ok-flags (bug-fix lama, tidak disentuh).

    [SUB-SKOR BARU] market_structure_score & donchian_score TIDAK PERNAH
    dapat treatment side-aware sebelumnya (di luar cakupan Batch 6 yg cuma
    cakup ichimoku/sar/pivot/fib). Keduanya genuinely directional dan
    PROVABLY mirror-symmetric by construction (diverifikasi aljabar):
    - market_structure_score: bullish/bearish + BOS/CHoCH events semua
      berpasangan simetris persis di sekitar 50 -- side="short" cukup
      komplemen 100-x, exact.
    - donchian_score: PROVABLY simetris di bawah transform pct_b->1-pct_b
      (diverifikasi per-cabang secara aljabar) -- side="short"
      diimplementasi via role-swap pct_b (bukan reformulasi cabang),
      konsisten gaya sar/pivot/fib.

    [PENTING] structure.py punya clamp_score() LOKAL (tanpa round ke 4
    desimal, sama spt oscillators.py) -- fuzz di bawah pakai presisi penuh.

    [PENTING -- swap-symmetry TIDAK exact, TAPI BUKAN dari komponen baru]
    ichimoku/market_structure/pivot/donchian SEMUA swap PERSIS di data riil.
    sar_score & fib_score TIDAK exact (SUDAH didokumentasikan sejak Batch 6
    -- "distance dari titik acuan" tidak simetris utk trend monoton) --
    diverifikasi ULANG di sini, bukan regresi baru dari Sub-Batch A.6."""

    # ── Sub-skor baru: market_structure_score_short & donchian_score_short ──

    def test_market_structure_score_short_exact_complement(self):
        cases = [
            ({"trend_structure": "bullish", "structure_event": "BOS_bullish"}, 80.0, 20.0),
            ({"trend_structure": "bearish", "structure_event": "BOS_bearish"}, 20.0, 80.0),
            ({"trend_structure": "bullish", "structure_event": None}, 65.0, 35.0),
            ({"trend_structure": "choppy", "structure_event": None}, 50.0, 50.0),
            ({"trend_structure": None, "structure_event": None}, 50.0, 50.0),
        ]
        for data, expected_long, expected_short in cases:
            self.assertAlmostEqual(score_market_structure(data, side="long"), expected_long, places=6)
            self.assertAlmostEqual(score_market_structure(data, side="short"), expected_short, places=6)

    def test_donchian_score_short_role_swap_verified(self):
        cases = [
            ({"donchian_pct_b": 1.0}, 83.0, 17.0),
            ({"donchian_pct_b": 0.0}, 17.0, 83.0),
            ({"donchian_pct_b": 0.7}, 58.0, 42.0),
            ({"donchian_pct_b": None}, 50.0, 50.0),
        ]
        for data, expected_long, expected_short in cases:
            self.assertAlmostEqual(score_donchian(data, side="long"), expected_long, places=6)
            self.assertAlmostEqual(score_donchian(data, side="short"), expected_short, places=6)

    def test_new_subscores_algebraic_symmetry_fuzz(self):
        rng = random.Random(999)
        for _ in range(300):
            trend = rng.choice(["bullish", "bearish", "choppy", None])
            event = rng.choice(["BOS_bullish", "BOS_bearish", "CHoCH_bullish", "CHoCH_bearish", None])
            data = {"trend_structure": trend, "structure_event": event}
            long_s = score_market_structure(data, side="long")
            short_s = score_market_structure(data, side="short")
            self.assertAlmostEqual(long_s + short_s, 100.0, places=6)

            pct_b = rng.uniform(0.0, 1.0)
            d = {"donchian_pct_b": pct_b}
            long_d = score_donchian(d, side="long")
            short_d = score_donchian(d, side="short")
            self.assertAlmostEqual(long_d + short_d, 100.0, places=6)

    # ── Composite arithmetic fuzz (independent reimplementation) ────────────

    def test_composite_score_short_arithmetic_fuzz(self):
        rng = random.Random(555)
        for i in range(300):
            if i % 2 == 0:
                n = rng.randint(0, 200)
                step = rng.uniform(0.01, 5.0)
                start = rng.uniform(20.0, 500.0)
                direction = rng.choice([1, -1])
                df = (_make_trend_df(n, direction=direction, start=start, step=step, vol=1000)
                      if n > 0 else pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []}))
            else:
                seed = rng.randint(0, 10000)
                bias = rng.uniform(-0.2, 0.2)
                n = rng.randint(3, 150)
                df = _make_choppy_df(seed=seed, bias=bias, n=n)

            r = score_structure(df)

            weighted_components_short = [
                (r.ichimoku_score_short,         0.25, r.tenkan is not None),
                (r.market_structure_score_short, 0.20, r.trend_structure not in (None, "undefined")),
                (r.sar_score_short,              0.15, r.sar_value is not None),
                (r.pivot_score_short,            0.15, r.pivot is not None),
                (r.donchian_score_short,         0.15, r.donchian_upper is not None),
                (r.fib_score_short,              0.10, r.fib_swing_high is not None),
            ]
            available_weight = sum(w for _, w, ok in weighted_components_short if ok)
            if available_weight <= 0:
                expected = 50.0
            else:
                weighted_sum = sum(s * w for s, w, ok in weighted_components_short if ok)
                expected = max(0.0, min(100.0, weighted_sum / available_weight))

            self.assertAlmostEqual(expected, r.composite_score_short, places=9)

    # ── Swap-symmetry per komponen (isolasi sumber asimetri) ────────────────

    def test_new_components_swap_exact_old_ones_documented_not_exact(self):
        df_up = _make_trend_df(100, direction=1)
        df_down = _make_trend_df(100, direction=-1)
        r_up = score_structure(df_up)
        r_down = score_structure(df_down)
        # Komponen BARU (Sub-Batch A.6): swap PERSIS
        self.assertAlmostEqual(r_up.market_structure_score, r_down.market_structure_score_short, places=6)
        self.assertAlmostEqual(r_up.donchian_score, r_down.donchian_score_short, places=6)
        # ichimoku (Batch 6 lama): swap PERSIS
        self.assertAlmostEqual(r_up.ichimoku_score, r_down.ichimoku_score_short, places=6)
        # sar & fib (Batch 6 lama): TIDAK exact -- didokumentasikan sejak dulu,
        # diverifikasi ulang di sini, bukan regresi baru
        self.assertGreater(abs(r_up.sar_score - r_down.sar_score_short) + abs(r_up.fib_score - r_down.fib_score_short), 0.01)

    # ── "Bukan cuma beda angka" ──────────────────────────────────────────────

    def test_uptrend_favors_long_not_short(self):
        df = _make_trend_df(100, direction=1)
        r = score_structure(df)
        self.assertGreater(r.composite_score, r.composite_score_short)
        self.assertGreater(r.composite_score, 50.0)

    def test_downtrend_favors_short_not_long(self):
        df = _make_trend_df(100, direction=-1)
        r = score_structure(df)
        self.assertGreater(r.composite_score_short, r.composite_score)
        self.assertGreater(r.composite_score_short, 50.0)

    # ── Neutral-alignment ─────────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        r = score_structure(empty_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    def test_neutral_both_sides_zero_price(self):
        zero_price = pd.DataFrame({"open": [0.0], "high": [0.0], "low": [0.0],
                                    "close": [0.0], "volume": [1000.0]})
        r = score_structure(zero_price)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(100, direction=1)
        r = score_structure(df)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)


class TestMTFSubBatchBVolatilityCompositeShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch B, volatility]

    score_volatility(): composite = BB(0.30) + (KC+Squeeze)/2 (0.30) + ATR(0.40),
    exclude+renormalize ok-flags (bug-fix lama `kc_squeeze_valid`, tidak disentuh).

    [TAHAP 0] Hipotesis awal CLAUDE.md ("volatility genuinely arah-agnostic
    semua") TERBUKTI SALAH SEBAGIAN setelah verifikasi kode + fuzz:
    - `bb_score`/`kc_score`: DIRECTIONAL (bukan arah-agnostic). Short dibuat
      via role-swap posisi (1 - bb_position / 1 - kc_position) lewat formula
      LONG yang SAMA persis (`_score_bb`/`_score_kc`), gaya sar/pivot/fib/
      donchian -- BUKAN reformulasi cabang baru.
    - `squeeze_score`: genuinely arah-agnostic (murni durasi/state squeeze).
      `squeeze_score_short` = alias langsung.
    - `atr_score`: formula `_score_atr()` sendiri TIDAK directional, TAPI
      input `atr_percentile`-nya (dari `_calc_atr_percentile()`) me-ranking
      ATR ABSOLUT (bukan `atr_pct` yg dinormalisasi harga) -- bias terukur
      di pasar trending. `atr_score_short` = alias + KNOWN LIMITATION
      (didokumentasikan eksplisit, BUKAN diklaim arah-agnostic). Root-cause
      fix DITUNDA sbg proyek terpisah (lihat CLAUDE.md "TEMUAN TERPISAH") --
      blast radius menyentuh regime classification LIVE (classifier.py),
      di luar scope aditif Sub-Batch B.

    [KARAKTERISTIK DESAIN -- BUKAN bug] `bb_score`/`kc_score` (formula LONG
    asli, TIDAK diubah) ternyata KONTRARIAN/mean-reversion: reward posisi
    RENDAH (dekat lower band = "buy the dip"), BUKAN trend-following. Fuzz
    200 trial (choppy random-walk berbias, BUKAN cuma tren monoton lurus)
    membuktikan `composite_score` (long, tidak berubah) rata-rata LEBIH
    TINGGI di fixture downtrend (63.20) drpd uptrend (45.71) -- kebalikan
    ekspektasi trend-following naif. Krn `composite_score_short` adalah
    role-swap EXACT dari formula yg sama, ia mewarisi sifat kontrarian yg
    sama (condong tinggi di uptrend, BUKAN downtrend). Pola IDENTIK dgn
    `momentum.py` (RSI+Stoch kontrarian) & `oscillators.py` (CCI/Williams %R
    kontrarian) yg sudah didokumentasikan sebelumnya -- SENGAJA TIDAK
    diperbaiki (mengubah formula long di luar scope aditif). Test di bawah
    ditulis berdasarkan role-swap symmetry & arithmetic correctness yang
    TERBUKTI benar, BUKAN asumsi "uptrend favors long"."""

    # ── 1. Long regression ──────────────────────────────────────────────────

    def test_long_score_bb_static_values_unchanged(self):
        # [Diverifikasi manual via shell sebelum ditulis -- lihat investigasi
        # Tahap 0] width=0.05 persis di batas BB_WIDTH_NORMAL (bukan < 0.05),
        # jadi tidak kena adjustment lebar band -- baseline paling bersih.
        cases = [
            (0.0,   80.0), (0.175, 77.5), (0.35, 75.0), (0.425, 70.0),
            (0.5,   65.0), (0.575, 58.5), (0.65, 52.0), (0.725, 46.0),
            (0.8,   40.0), (0.9,   32.5), (1.0,  25.0),
        ]
        for pos, expected in cases:
            self.assertAlmostEqual(_score_bb(pos, 0.05, "flat"), expected, places=6)

    def test_long_score_kc_static_values_unchanged(self):
        cases = [
            (-0.5, 62.0), (-0.01, 62.0), (0.0, 70.0), (0.2, 70.0), (0.34, 70.0),
            (0.35, 55.0), (0.5, 55.0), (0.64, 55.0), (0.65, 42.0), (0.8, 42.0),
            (1.0, 42.0), (1.01, 30.0), (1.5, 30.0),
        ]
        for pos, expected in cases:
            self.assertAlmostEqual(_score_kc(pos), expected, places=6)

    def test_long_score_kc_matches_old_inline_ladder_fuzz(self):
        """[REGRESI -- Sub-Batch B] `_score_kc()` diekstrak dari ladder inline
        lama di `calculate_keltner_channels()`. Fuzz ini membuktikan hasilnya
        byte-identical thd reimplementasi independen ladder ASLI (sebelum
        diekstrak), supaya ekstraksi terbukti aditif murni -- bukan asumsi."""
        def _old_inline_ladder(kc_position):
            if kc_position < 0.0:
                score = 62.0
            elif kc_position < 0.35:
                score = 70.0
            elif kc_position < 0.65:
                score = 55.0
            elif kc_position <= 1.0:
                score = 42.0
            else:
                score = 30.0
            return max(0.0, min(100.0, round(score, 4)))

        rng = random.Random(2024)
        for _ in range(5000):
            pos = rng.uniform(-3.0, 3.0)
            self.assertAlmostEqual(_score_kc(pos), _old_inline_ladder(pos), places=9)

    def test_long_composite_score_arithmetic_fuzz(self):
        """Fuzz composite_score (long, TIDAK diubah) lewat score_volatility()
        vs reimplementasi independen formula renormalisasi -- membuktikan
        refactor _score_kc tidak mengganggu long end-to-end."""
        rng = random.Random(4242)
        for i in range(200):
            n = rng.randint(0, 150)
            if n == 0:
                df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
            elif i % 2 == 0:
                df = _make_trend_df(n, direction=rng.choice([1, -1]),
                                     start=rng.uniform(20.0, 500.0), step=rng.uniform(0.05, 3.0))
            else:
                df = _make_choppy_df(seed=rng.randint(0, 10000), bias=rng.uniform(-0.3, 0.3), n=n)

            r = score_volatility(df)

            bb_ok = r.bb_upper is not None
            kc_ok = r.kc_upper is not None
            combined_kc_sq = (r.kc_score + r.squeeze_score) / 2.0 if kc_ok else r.squeeze_score
            weighted = [
                (0.30, bb_ok, r.bb_score),
                (0.30, True,  combined_kc_sq),
                (0.40, r.atr is not None, r.atr_score),
            ]
            avail = sum(w for w, ok, _ in weighted if ok)
            if avail < 1e-6:
                expected = 50.0
            else:
                # [PENTING] Urutan operasi HARUS sama persis dgn score_volatility()
                # (adjusted_w = base_w/avail per-term, baru dikali skor, baru
                # diakumulasi) -- urutan float berbeda bisa geser digit ke-4
                # stlh round(), walau matematis ekuivalen.
                raw = 0.0
                for w, ok, s in weighted:
                    if not ok:
                        continue
                    raw += s * (w / avail)
                expected = max(0.0, min(100.0, round(raw, 4)))  # matches clamp_score() exactly
            self.assertAlmostEqual(expected, r.composite_score, places=6)

    # ── 2. Swap-symmetry (role-swap, via data real) ──────────────────────────

    def test_bb_score_short_independent_role_swap_reconstruction(self):
        """Role-swap MANUAL: ambil bb_position/bb_width/bb_trending dari
        calculate_bollinger_bands(), lalu panggil _score_bb(1-pos, ...)
        SENDIRI (bukan cuma baca field bb_score_short) -- reconstruction
        independen, bukan tautology."""
        for direction in (1, -1):
            df = _make_trend_df(100, direction=direction, step=0.7)
            r = calculate_bollinger_bands(df)
            manual_short = _score_bb(1.0 - r.bb_position, r.bb_width, r.bb_trending)
            self.assertAlmostEqual(r.bb_score_short, manual_short, places=9)

        for seed in range(30):
            df = _make_choppy_df(seed=seed, bias=random.Random(seed).uniform(-0.3, 0.3), n=100)
            r = calculate_bollinger_bands(df)
            manual_short = _score_bb(1.0 - r.bb_position, r.bb_width, r.bb_trending)
            self.assertAlmostEqual(r.bb_score_short, manual_short, places=9)

    def test_kc_score_short_independent_role_swap_reconstruction(self):
        for direction in (1, -1):
            df = _make_trend_df(100, direction=direction, step=0.7)
            r = calculate_keltner_channels(df)
            kc_range = r.kc_upper - r.kc_lower
            close_val = df["close"].iloc[-1]
            kc_position = (close_val - r.kc_lower) / kc_range if kc_range > 1e-9 else 0.5
            manual_short = _score_kc(1.0 - kc_position)
            self.assertAlmostEqual(r.kc_score_short, manual_short, places=9)

    def test_bb_kc_swap_symmetry_not_exact_sum_to_100_documented(self):
        """[KARAKTERISTIK DESAIN -- bukan bug] Ladder bb/kc TIDAK simetris di
        sekitar 0.5 (beda dgn pattern.py/context_score yg exact reflection
        100-x) -- didokumentasikan eksplisit, konsisten gaya sar/fib/roc."""
        bb_sum_0 = _score_bb(0.0, 0.05, "flat") + _score_bb(1.0, 0.05, "flat")
        bb_sum_35 = _score_bb(0.35, 0.05, "flat") + _score_bb(0.65, 0.05, "flat")
        self.assertNotAlmostEqual(bb_sum_0, 100.0, places=1)
        self.assertNotAlmostEqual(bb_sum_35, 100.0, places=1)

        kc_sum = _score_kc(0.2) + _score_kc(0.8)
        self.assertNotAlmostEqual(kc_sum, 100.0, places=1)

    def test_squeeze_score_short_alias_exact_fuzz(self):
        rng = random.Random(1357)
        for _ in range(150):
            n = rng.randint(45, 150)
            df = (_make_trend_df(n, direction=rng.choice([1, -1]), step=rng.uniform(0.05, 2.0))
                  if rng.random() < 0.5 else
                  _make_choppy_df(seed=rng.randint(0, 10000), bias=rng.uniform(-0.3, 0.3), n=n))
            active, bars, sq_score = detect_squeeze(df)
            r = score_volatility(df)
            self.assertEqual(r.squeeze_score, r.squeeze_score_short)
            self.assertAlmostEqual(r.squeeze_score, sq_score, places=6)

    def test_atr_score_short_alias_exact_fuzz(self):
        rng = random.Random(9753)
        for _ in range(150):
            n = rng.randint(30, 150)
            df = (_make_trend_df(n, direction=rng.choice([1, -1]), step=rng.uniform(0.05, 2.0))
                  if rng.random() < 0.5 else
                  _make_choppy_df(seed=rng.randint(0, 10000), bias=rng.uniform(-0.3, 0.3), n=n))
            r = calculate_atr_enhanced(df)
            self.assertEqual(r.atr_score, r.atr_score_short)

    def test_atr_percentile_known_bug_documented_geometric_mirror(self):
        """[KNOWN LIMITATION -- didokumentasikan, TIDAK diperbaiki di sini]
        `_calc_atr_percentile()` me-ranking ATR ABSOLUT (dolar), bukan
        `atr_pct` (dinormalisasi harga). Dibuktikan lewat geometric mirror
        (mclose = anchor**2/close, negasi log-return EXACT): dua seri dgn
        profil return relatif yg identik-berlawanan seharusnya (kalau
        genuinely price-normalized) punya atr_percentile yg simetris --
        pada kenyataannya SANGAT TIDAK (99.50 vs 2.50, padahal atr_pct
        cuma beda 1.91 vs 2.70) krn ATR absolut ikut price-level drift.
        Root-cause fix DITUNDA sbg proyek terpisah (lihat CLAUDE.md)."""
        n = 120
        anchor = 100.0
        rng = np.random.RandomState(7)
        idx = pd.date_range("2026-01-01", periods=n, freq="15min")
        logret = rng.normal(0.01, 0.015, size=n)
        close_up = anchor * np.exp(np.cumsum(logret))
        close_dn = anchor ** 2 / close_up   # exact geometric mirror

        def _build(close):
            o = np.empty(n); h = np.empty(n); l = np.empty(n)
            prev = anchor
            for i in range(n):
                o[i] = prev
                c = close[i]
                h[i] = max(o[i], c) * 1.004
                l[i] = min(o[i], c) * 0.996
                prev = c
            return pd.DataFrame({"open": o, "high": h, "low": l, "close": close,
                                  "volume": 1000.0}, index=idx)

        r_up = calculate_atr_enhanced(_build(close_up))
        r_dn = calculate_atr_enhanced(_build(close_dn))

        self.assertGreater(r_up.atr_percentile, 90.0)
        self.assertLess(r_dn.atr_percentile, 10.0)
        # atr_pct (dinormalisasi harga, field yg AMAN) TIDAK sedramatis itu --
        # membuktikan divergensi di atas murni artefak ranking absolut.
        self.assertLess(abs(r_up.atr_pct - r_dn.atr_pct), 2.0)

    # ── "Bukan cuma beda angka" (kontrarian, BUKAN trend-following) ─────────

    def test_extended_up_favors_short_composite_not_long(self):
        """[KARAKTERISTIK DESAIN] Krn bb/kc kontrarian, fixture yg mendorong
        harga dekat UPPER band (uptrend berbias) secara KONSISTEN membuat
        composite_score_short > composite_score -- BUKAN sebaliknya."""
        n_short_favored = 0
        trials = 60
        for i in range(trials):
            bias = random.Random(i + 500).uniform(0.1, 0.4)
            df = _make_choppy_df(seed=i, bias=bias, n=100)
            r = score_volatility(df)
            if r.composite_score_short > r.composite_score:
                n_short_favored += 1
        self.assertGreater(n_short_favored, trials * 0.8)

    def test_extended_down_favors_long_composite_not_short(self):
        n_long_favored = 0
        trials = 60
        for i in range(trials):
            bias = random.Random(i + 500).uniform(0.1, 0.4)
            df = _make_choppy_df(seed=i, bias=-bias, n=100)
            r = score_volatility(df)
            if r.composite_score > r.composite_score_short:
                n_long_favored += 1
        self.assertGreater(n_long_favored, trials * 0.8)

    # ── 3. Neutral-alignment ─────────────────────────────────────────────────

    def test_neutral_both_sides_empty_df(self):
        empty_df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
        r = score_volatility(empty_df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)

    def test_neutral_both_sides_insufficient_bars(self):
        df = _make_trend_df(5, direction=1)
        r = score_volatility(df)
        self.assertEqual(r.composite_score, 50.0)
        self.assertEqual(r.composite_score_short, 50.0)
        self.assertEqual(r.bb_score_short, 50.0)
        self.assertEqual(r.kc_score_short, 50.0)
        self.assertEqual(r.atr_score_short, 50.0)

    # ── Integrasi ────────────────────────────────────────────────────────────

    def test_composite_score_short_field_exists_and_differs(self):
        df = _make_trend_df(100, direction=1, step=0.7)
        r = score_volatility(df)
        self.assertIsNotNone(r.composite_score_short)
        self.assertNotEqual(r.composite_score, r.composite_score_short)

    def test_extract_indicator_scores_reads_short_fields(self):
        df = _make_choppy_df(seed=11, bias=0.2, n=100)
        r = score_volatility(df)
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility = r

        scores_long  = _extract_indicator_scores(iset, side="long")
        scores_short = _extract_indicator_scores(iset, side="short")

        self.assertAlmostEqual(scores_long["volatility"]["bb"], r.bb_score, places=6)
        self.assertAlmostEqual(scores_short["volatility"]["bb"], r.bb_score_short, places=6)
        self.assertAlmostEqual(scores_long["volatility"]["squeeze"], r.squeeze_score, places=6)
        self.assertAlmostEqual(scores_short["volatility"]["squeeze"], r.squeeze_score_short, places=6)
        self.assertAlmostEqual(scores_long["volatility"]["atr"], r.atr_score, places=6)
        self.assertAlmostEqual(scores_short["volatility"]["atr"], r.atr_score_short, places=6)


class TestMTFSubBatchCObserverOrderbookShort(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch C]

    observer.py::_compute_tf_score(iset, side="long") -- penyesuaian KECIL,
    SENGAJA cuma menyentuh baris orderbook (bukan 7 kategori lain, itu
    cakupan Sub-Batch D). `iset.orderbook.composite_score` TETAP alias
    long-only (models.py, tidak disentuh sama sekali di Sub-Batch C ini) --
    fix-nya di sisi consumer: baca lewat `_pick_side_score(iset.orderbook,
    "orderbook_score", side)` (helper yg sama dipakai scorer.py) supaya
    side="short" kebagian `orderbook_score_short` (Batch 7, sudah wired),
    dgn fallback aman ke `orderbook_score` kalau field itu masih None.

    side="long" (default, SEMUA caller `observe()` existing saat ini)
    HARUS identik persis dgn sebelum perubahan ini -- diverifikasi eksplisit
    di bawah, bukan diasumsikan."""

    def _base_iset(self) -> IndicatorSet:
        # [PENTING] PatternIndicators.is_valid() SELALU return True TANPA
        # SYARAT (models.py, quirk pre-existing, di luar cakupan Sub-Batch C)
        # -- patterns SELALU ikut ke weighted average dgn composite_score
        # default 50.0 (weight 0.10), terlepas dari apapun yg di-set di
        # iset. Supaya test di bawah bisa isolasi kontribusi orderbook
        # dgan angka bersih, patterns.composite_score dipatok eksplisit ke
        # 50.0 (=default, tidak mengubah apapun) dan expected value HARUS
        # ikut menghitung kontribusi 0.10 x 50.0 ini -- diverifikasi manual
        # lewat regresi arithmetic fuzz di kelas lain sebelumnya, di sini
        # cukup dihitung tangan krn cuma 1-2 kategori aktif per test.
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.bars_available = 100
        iset.patterns.composite_score = 50.0
        return iset

    def test_side_long_reads_orderbook_score_unchanged(self):
        iset = self._base_iset()
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 72.0
        iset.orderbook.composite_score = 72.0
        iset.orderbook.orderbook_score_short = 18.0
        result = _compute_tf_score(iset, side="long")
        # patterns (selalu valid, w=0.10, score=50.0) + orderbook (w=0.10, 72.0)
        expected = round((72.0 * 0.10 + 50.0 * 0.10) / 0.20, 2)
        self.assertAlmostEqual(result, expected, places=2)

    def test_side_short_reads_orderbook_score_short(self):
        iset = self._base_iset()
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 72.0
        iset.orderbook.composite_score = 72.0
        iset.orderbook.orderbook_score_short = 18.0
        result = _compute_tf_score(iset, side="short")
        expected = round((18.0 * 0.10 + 50.0 * 0.10) / 0.20, 2)
        self.assertAlmostEqual(result, expected, places=2)

    def test_side_short_falls_back_when_short_field_none(self):
        """orderbook_score_short belum diisi (None, mis. kategori lama yg
        belum wired) -- side='short' TIDAK BOLEH crash atau diam-diam pakai
        0, harus fallback ke orderbook_score (sama seperti long)."""
        iset = self._base_iset()
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 65.0
        iset.orderbook.composite_score = 65.0
        iset.orderbook.orderbook_score_short = None
        result_long  = _compute_tf_score(iset, side="long")
        result_short = _compute_tf_score(iset, side="short")
        expected = round((65.0 * 0.10 + 50.0 * 0.10) / 0.20, 2)
        self.assertAlmostEqual(result_long, expected, places=2)
        self.assertAlmostEqual(result_short, expected, places=2)
        self.assertEqual(result_long, result_short)

    def test_default_call_no_side_arg_unchanged(self):
        """Caller lama (observe(), belum di-thread side -- itu Sub-Batch D)
        manggil _compute_tf_score(iset) TANPA argumen side sama sekali --
        harus identik dgn side='long' eksplisit."""
        iset = self._base_iset()
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 40.0
        iset.orderbook.composite_score = 40.0
        iset.orderbook.orderbook_score_short = 90.0
        result_default = _compute_tf_score(iset)
        result_explicit_long = _compute_tf_score(iset, side="long")
        expected = round((40.0 * 0.10 + 50.0 * 0.10) / 0.20, 2)
        self.assertAlmostEqual(result_default, expected, places=2)
        self.assertEqual(result_default, result_explicit_long)

    def test_trend_also_side_aware_since_sub_batch_d(self):
        """[SUPERSEDED oleh Sub-Batch D] Sub-Batch C dulu memastikan trend
        dkk BELUM side-aware di level ini -- sekarang (Sub-Batch D) trend
        SUDAH baca composite_score_short juga, lewat _pick_side_score() yang
        sama persis dgn orderbook. Test ini diupdate utk mengunci perilaku
        BARU yang benar, bukan lagi perilaku lama Sub-Batch C."""
        iset = self._base_iset()
        iset.trend.ema9, iset.trend.ema21, iset.trend.ema50 = 105.0, 100.0, 95.0
        iset.trend.composite_score = 80.0
        iset.trend.composite_score_short = 20.0
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 50.0
        iset.orderbook.composite_score = 50.0
        iset.orderbook.orderbook_score_short = 50.0   # disamakan biar isolasi trend jelas

        result_long  = _compute_tf_score(iset, side="long")
        result_short = _compute_tf_score(iset, side="short")

        # trend weight=0.30, orderbook weight=0.10 (sama di kedua sisi di
        # test ini) + patterns (selalu valid, w=0.10, score=50.0 default).
        expected_long  = round((80.0 * 0.30 + 50.0 * 0.10 + 50.0 * 0.10) / 0.50, 2)
        expected_short = round((20.0 * 0.30 + 50.0 * 0.10 + 50.0 * 0.10) / 0.50, 2)
        self.assertAlmostEqual(result_long, expected_long, places=2)
        self.assertAlmostEqual(result_short, expected_short, places=2)
        self.assertNotAlmostEqual(result_long, result_short, places=2)

    def test_all_7_categories_side_aware_end_to_end(self):
        """Populate ke-7 kategori (trend/momentum/strength/volatility/
        patterns/oscillators/structure) dengan composite_score vs
        composite_score_short yang beda jauh, plus orderbook -- pastikan
        SEMUANYA ikut terbaca lewat side, bukan cuma trend (test di atas)
        atau orderbook (Sub-Batch C)."""
        iset = self._base_iset()
        iset.trend.ema9, iset.trend.ema21, iset.trend.ema50 = 105.0, 100.0, 95.0
        cats_weights = [
            ("trend", 0.30), ("momentum", 0.25), ("strength", 0.25),
            ("volatility", 0.10), ("patterns", 0.10), ("oscillators", 0.07),
            ("structure", 0.07),
        ]
        long_val, short_val = 90.0, 10.0
        for cat, _ in cats_weights:
            obj = getattr(iset, cat)
            obj.composite_score = long_val
            obj.composite_score_short = short_val
        # Field validitas tambahan per kategori (selain trend yg sudah di-set
        # via ema9/21/50 di atas) -- volume_ratio(strength)/rsi(momentum)/
        # atr+bb_upper(volatility) supaya is_valid() masing2 True.
        iset.momentum.rsi = 55.0
        iset.strength.volume_ratio = 1.2
        iset.volatility.atr = 1.0
        iset.volatility.bb_upper = 105.0
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = long_val
        iset.orderbook.composite_score = long_val
        iset.orderbook.orderbook_score_short = short_val

        result_long  = _compute_tf_score(iset, side="long")
        result_short = _compute_tf_score(iset, side="short")

        # patterns/oscillators/structure is_valid() default True/tergantung
        # kategori masing2 -- oscillators & structure is_valid() perlu dicek;
        # kalau ternyata tidak valid (skip), weighted-avg tetap konsisten
        # krn long_val==short_val==90/10 seragam di SEMUA kategori yg valid
        # -- jadi result HARUS persis sama dgn long_val (side=long) dan
        # short_val (side=short), tak peduli subset kategori mana yg valid.
        self.assertAlmostEqual(result_long, long_val, places=2)
        self.assertAlmostEqual(result_short, short_val, places=2)

    def test_neutral_insufficient_bars_regardless_of_side(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.bars_available = 5
        iset.orderbook.bid_ask_imbalance = 0.55
        iset.orderbook.orderbook_score = 90.0
        iset.orderbook.orderbook_score_short = 10.0
        self.assertEqual(_compute_tf_score(iset, side="long"), 50.0)
        self.assertEqual(_compute_tf_score(iset, side="short"), 50.0)


class TestMTFSubBatchDObserveSideThreading(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch D]

    observer.py::observe() (module-level) & MarketObserver.observe() (method,
    dipanggil via run_in_executor dari strategy_base.py::get_scored_signal())
    sekarang punya parameter `side`, diteruskan ke _compute_tf_score() untuk
    primary DAN confirmation TF.

    [TEMUAN KRUSIAL Tahap 0] _OBSERVATION_CACHE (module-level, key sebelumnya
    HANYA symbol|timeframe|timestamp) TIDAK menyertakan side -- begitu
    primary_tf_score/confirmation_tf_score genuinely beda per side (Sub-Batch
    D), cache hit utk side="short" bisa diam-diam mengembalikan
    ObservationReport yang dihitung utk side="long" dari request sebelumnya
    pada bar yang sama (silent cross-side contamination). Fix: _cache_key()
    sekarang menyertakan side sbg SUFFIX (bukan prefix, supaya
    get_cached_observation()/clear_cache() yang match via startswith(symbol|
    timeframe|) tetap benar tanpa perlu diubah). Diverifikasi eksplisit di
    bawah, bukan diasumsikan."""

    def setUp(self):
        clear_cache()  # _OBSERVATION_CACHE global module-level -- isolasi antar test

    def tearDown(self):
        clear_cache()

    def test_default_side_long_unchanged_from_before(self):
        """Caller lama (observe() tanpa argumen side sama sekali) HARUS
        identik dgn side='long' eksplisit."""
        df = _make_trend_df(100, direction=1, step=0.7)
        r_default = observe("TEST/USDT", "universal", df, "15m", use_cache=False)
        clear_cache()
        r_long = observe("TEST/USDT", "universal", df, "15m", use_cache=False, side="long")
        self.assertEqual(r_default.primary_tf_score, r_long.primary_tf_score)

    def test_observe_side_threading_end_to_end(self):
        """side='short' vs side='long' pada df & parameter yang SAMA PERSIS
        harus menghasilkan primary_tf_score BERBEDA -- membuktikan side
        genuinely nyampe ke _compute_tf_score() lewat seluruh rantai
        observe(), bukan cuma diterima lalu diabaikan diam-diam."""
        df = _make_choppy_df(seed=42, bias=0.15, n=100)
        r_long  = observe("TEST/USDT", "universal", df, "15m", use_cache=False, side="long")
        r_short = observe("TEST/USDT", "universal", df, "15m", use_cache=False, side="short")
        self.assertNotAlmostEqual(r_long.primary_tf_score, r_short.primary_tf_score, places=2)

    def test_cache_does_not_cross_contaminate_between_sides(self):
        """[FIX UTAMA Sub-Batch D] Panggil side='long' (cached), lalu
        side='short' (df & bar SAMA PERSIS) -- side='short' TIDAK BOLEH
        mengembalikan primary_tf_score versi long dari cache. Lalu panggil
        side='long' LAGI -- harus tetap dapat cache hit versi long yang
        benar (bukan malah rusak jadi tidak pernah cache-hit sama sekali)."""
        df = _make_choppy_df(seed=7, bias=0.2, n=100)

        r_long_1 = observe("TEST/USDT", "universal", df, "15m", use_cache=True, side="long")
        r_short  = observe("TEST/USDT", "universal", df, "15m", use_cache=True, side="short")
        r_long_2 = observe("TEST/USDT", "universal", df, "15m", use_cache=True, side="long")

        self.assertNotAlmostEqual(r_long_1.primary_tf_score, r_short.primary_tf_score, places=2)
        # side="long" kedua kali harus dapat NILAI yang sama (cache hit yang
        # benar mengembalikan report versi long, bukan versi short yang
        # barusan di-cache di antaranya).
        self.assertEqual(r_long_1.primary_tf_score, r_long_2.primary_tf_score)

    def test_market_observer_method_threads_side(self):
        """MarketObserver.observe() (dipanggil via run_in_executor di
        strategy_base.py) meneruskan side ke observe() module-level."""
        from types import SimpleNamespace
        df = _make_choppy_df(seed=99, bias=0.1, n=100)
        profile = SimpleNamespace(
            profile=SimpleNamespace(value="universal"),
            timeframe="15m",
            confirmation_weight=0.25,
        )
        mo = MarketObserver()
        r_long  = mo.observe("TEST/USDT", df, profile, side="long")
        clear_cache()
        r_short = mo.observe("TEST/USDT", df, profile, side="short")
        self.assertNotAlmostEqual(r_long.primary_tf_score, r_short.primary_tf_score, places=2)

    def test_market_observer_default_side_long(self):
        """MarketObserver.observe() dipanggil TANPA side (pola lama, semua
        caller sebelum Sub-Batch D) -- harus default ke long, identik
        dgn caller yang eksplisit side='long'."""
        from types import SimpleNamespace
        df = _make_choppy_df(seed=99, bias=0.1, n=100)
        profile = SimpleNamespace(
            profile=SimpleNamespace(value="universal"),
            timeframe="15m",
            confirmation_weight=0.25,
        )
        mo = MarketObserver()
        r_default = mo.observe("TEST/USDT", df, profile)
        clear_cache()
        r_long = mo.observe("TEST/USDT", df, profile, side="long")
        self.assertEqual(r_default.primary_tf_score, r_long.primary_tf_score)


def _make_hourly_trend_df(n, direction=1, start=500.0, step=0.15):
    """Sama persis pola _make_trend_df, cuma freq='1h' (bukan '15min') --
    dipakai sbg confirmation_df di TestMTFSubBatchEGateVerification, krn
    _make_trend_df hardcode 15min."""
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    bars = []
    for i in range(n):
        c = start + direction * step * i
        o = c - direction * step * 0.3
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        bars.append((o, h, l, c, 1000))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


class _DummyStrategyForGateTest(VolumetricBreakoutStrategyBase):
    """Subclass minimal cuma utk implement generate_signals() abstract --
    tidak dipakai di test ini, get_scored_signal() sudah konkret di base."""
    async def generate_signals(self, symbol, df):
        return []


class TestMTFSubBatchEGateVerification(unittest.TestCase):
    """[PROYEK BARU -- MTF composite side-aware, Sub-Batch E]

    Verifikasi hard MTF gate (engine/strategy_base.py, di dalam
    get_scored_signal(), ~baris 1048-1060):
        if (not observation.confirmation_tf_valid) or (
            float(observation.confirmation_tf_score or 0.0)
            < float(profile.confirmation_min_score)
        ):
            return None  # sinyal diblokir

    [TAHAP 0 -- 2 hal yang WAJIB diverifikasi, BUKAN diasumsikan]
    1. Apakah `observation` di titik gate ini sudah dihitung dgn `side` yang
       benar? YA -- `observation` adalah HASIL LANGSUNG dari
       `self._observer.observe(..., side)` beberapa baris di atas DI DALAM
       FUNGSI YANG SAMA (get_scored_signal()) -- sudah dibereskan Sub-Batch D
       (side dulu TIDAK PERNAH diteruskan ke observe(), sekarang diteruskan).
       Tidak ada perubahan kode lagi yang diperlukan utk poin ini.
    2. Apakah logika perbandingan `< profile.confirmation_min_score` MASIH
       benar utk short, atau perlu dibalik arahnya? TERBUKTI MASIH BENAR,
       TIDAK PERLU DIBALIK -- dibuktikan lewat data riil di bawah (bukan
       penalaran teoretis semata): fixture downtrend riil menghasilkan
       confirmation_tf_score LEBIH TINGGI utk short (54.53) drpd long
       (38.94) pada bar yg SAMA PERSIS -- karena SEMUA 8 kategori sub-skor
       `_short` sudah dibangun dgn konvensi "makin tinggi = makin favorable
       utk sisi itu" (Sub-Batch A/B/C, konvensi `_pick_side_score()`), gate
       yang MEMBLOKIR skor RENDAH bekerja simetris utk kedua sisi tanpa
       perlu dibalik. `profile.confirmation_min_score` sendiri TIDAK punya
       varian `_short` (dicek engine/profiles/thresholds.py) -- ini BENAR
       by design krn threshold-nya flat, yang side-aware adalah SKOR-nya.

    Test di bawah memanggil get_scored_signal() SUNGGUHAN (bukan re-
    implementasi logic gate, bukan mock observer/scorer) lewat instance
    VolumetricBreakoutStrategyBase konkret -- jalur produksi asli end-to-end
    (observer -> classifier -> scorer -> MTF gate)."""

    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def _make_strategy(self):
        strat = _DummyStrategyForGateTest(symbols=["BTC/USDT"], timeframe="15m")
        # [PENTING] Profile default BTC/USDT adalah "hodl_accumulate" dgn
        # timeframe="1d" -- dipaksa "15m" di sini spy primary_df (freq
        # 15min, dibangun _make_trend_df) genuinely dipakai sbg timeframe
        # yang benar oleh observe() (yang baca profile.timeframe, BUKAN
        # argumen timeframe yang dioper ke get_scored_signal()).
        strat._profiles["BTC/USDT"].timeframe = "15m"
        return strat

    def test_downtrend_blocks_long_passes_short(self):
        """Fixture downtrend riil (250 bar primary 15m + 250 bar
        confirmation 1h, arah SAMA) -- confirmation_min_score default
        profile hodl_accumulate = 40.0."""
        strat = self._make_strategy()
        threshold = strat._profiles["BTC/USDT"].confirmation_min_score
        self.assertAlmostEqual(threshold, 40.0, places=1)  # asumsi Tahap 0, dikunci

        primary_df = _make_trend_df(250, direction=-1, start=500.0, step=0.3)
        conf_df    = _make_hourly_trend_df(250, direction=-1, start=500.0, step=0.15)

        async def _run(side):
            clear_cache()
            return await strat.get_scored_signal(
                symbol="BTC/USDT", df=primary_df,
                confirmation_df=conf_df, confirmation_timeframe="1h",
                side=side,
            )

        scored_long  = asyncio.run(_run("long"))
        scored_short = asyncio.run(_run("short"))

        self.assertIsNone(scored_long, "MTF gate SEHARUSNYA blokir long saat downtrend "
                                        "(confirmation_tf_score long < threshold)")
        self.assertIsNotNone(scored_short, "MTF gate SEHARUSNYA meloloskan short saat "
                                            "downtrend (confirmation_tf_score short >= threshold)")

    def test_uptrend_blocks_short_passes_long(self):
        """Mirror dari test di atas -- fixture uptrend, arah gate terbalik."""
        strat = self._make_strategy()

        primary_df = _make_trend_df(250, direction=1, start=500.0, step=0.3)
        conf_df    = _make_hourly_trend_df(250, direction=1, start=500.0, step=0.15)

        async def _run(side):
            clear_cache()
            return await strat.get_scored_signal(
                symbol="BTC/USDT", df=primary_df,
                confirmation_df=conf_df, confirmation_timeframe="1h",
                side=side,
            )

        scored_long  = asyncio.run(_run("long"))
        scored_short = asyncio.run(_run("short"))

        self.assertIsNotNone(scored_long, "MTF gate SEHARUSNYA meloloskan long saat uptrend")
        self.assertIsNone(scored_short, "MTF gate SEHARUSNYA blokir short saat uptrend")

    def test_confirmation_tf_score_values_documented(self):
        """[REGRESI -- kunci angka riil, bukan cuma lolos/blokir] Nilai
        confirmation_tf_score persis yang membuktikan temuan Tahap 0 --
        diverifikasi manual sebelum ditulis di sini (bukan ditebak)."""
        primary_df = _make_trend_df(250, direction=-1, start=500.0, step=0.3)
        conf_df    = _make_hourly_trend_df(250, direction=-1, start=500.0, step=0.15)

        clear_cache()
        r_long = observe(
            "BTC/USDT", "hodl_accumulate", primary_df, "15m",
            confirmation_df=conf_df, confirmation_timeframe="1h",
            use_cache=False, side="long",
        )
        clear_cache()
        r_short = observe(
            "BTC/USDT", "hodl_accumulate", primary_df, "15m",
            confirmation_df=conf_df, confirmation_timeframe="1h",
            use_cache=False, side="short",
        )

        self.assertTrue(r_long.confirmation_tf_valid)
        self.assertTrue(r_short.confirmation_tf_valid)
        self.assertAlmostEqual(r_long.confirmation_tf_score, 38.94, places=1)
        self.assertAlmostEqual(r_short.confirmation_tf_score, 54.53, places=1)
        self.assertGreater(r_short.confirmation_tf_score, r_long.confirmation_tf_score)


if __name__ == "__main__":
    unittest.main()
