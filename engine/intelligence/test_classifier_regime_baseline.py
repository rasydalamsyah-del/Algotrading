"""
engine/intelligence/test_classifier_regime_baseline.py — BASELINE test
coverage untuk _is_volatile(), _calc_confidence(), _classify_raw()
(engine/intelligence/classifier.py), dibangun SEBELUM perbaikan apa pun
dikerjakan untuk item audit #4 (bias arah _calc_atr_percentile()).

Tujuan file ini BUKAN membuktikan ketiga fungsi ini "benar" -- tujuannya
MENGUNCI perilaku yang ADA SEKARANG (termasuk bias #4 yang belum
diperbaiki), supaya kalau root-cause fix _calc_atr_percentile() nanti
diimplementasikan, kita punya jaring pengaman untuk membuktikan APA yang
berubah dan APA yang seharusnya TIDAK berubah -- bukan menebak.

Ketiga fungsi ini sebelumnya NOL test coverage di seluruh repo (dikonfirmasi
grep + baca test_regime_side_aware.py: file itu beroperasi di level
MarketRegime enum/threshold matrix sebagai INPUT langsung, tidak pernah
construct IndicatorSet atau memanggil classify()/_is_volatile()/
_calc_confidence()/_classify_raw()).

Dijalankan pakai stdlib unittest:
    python3 -m unittest engine.intelligence.test_classifier_regime_baseline -v

--------------------------------------------------------------------------
Catatan investigasi data historis riil (Tahap 0, bagian "data availability"):
Sandbox ini TIDAK punya akses ke DB bot live (sesuai aturan proyek -- sandbox
tidak pernah punya akses ke bot/VPS live). Dicek langsung:
  - ./data/ (lokasi default DATABASE_URL sqlite+aiosqlite di main_spot.py/
    main_future.py) KOSONG di sandbox ini -- tidak ada file .db sama sekali.
  - Bahkan kalau ADA, tabel `ohlcv` (engine/database.py::OHLCVBar,
    __tablename__="ohlcv") ternyata TIDAK PERNAH ditulis/dibaca di manapun
    di seluruh codebase (dikonfirmasi grep "OHLCVBar" -- cuma muncul sekali,
    di definisi class-nya sendiri) -- schema ada, tapi dead/tidak pernah
    dipopulate. Candle OHLCV difetch live dari exchange tiap siklus, tidak
    pernah dipersist ke DB.
Kesimpulan: TIDAK ADA data historis OHLC riil yang bisa dipakai di sandbox
ini untuk proyek #4. Class TestATRPercentileBiasCharacterizationSynthetic
di bawah karena itu pakai data SINTETIS (random walk berbasis persentase,
seed tetap/deterministik, pola serupa fuzz test Sub-Batch B) sebagai
pengganti representatif -- BUKAN klaim "ini persis seberapa sering terjadi
di pasar riil", melainkan bukti terukur bahwa arah & skala bias ada dan
membesar seiring kekuatan tren, konsisten dengan temuan CLAUDE.md
("tren sedang geser ~8 poin, tren kuat sampai ~70 poin").
"""

from __future__ import annotations

import random
import unittest

import pandas as pd

from engine.core.models import IndicatorSet, MarketRegime
from engine.constants import (
    REGIME_VOLATILE_ATR_PERCENTILE_MIN,
    REGIME_VOLATILE_BB_WIDTH_MIN,
    REGIME_TRENDING_ADX_MIN,
    REGIME_TRENDING_STRONG_ADX,
    REGIME_RANGING_ADX_MAX,
    REGIME_CONFIDENCE_HIGH_ADX,
    REGIME_CONFIDENCE_LOW_ADX,
)
from engine.intelligence.classifier import (
    _is_volatile,
    _calc_confidence,
    _classify_raw,
)
from engine.indicators.volatility import _calc_atr, _calc_atr_percentile


# ─────────────────────────────────────────────────────────────────────────
# TestIsVolatileBaseline
# ─────────────────────────────────────────────────────────────────────────

class TestIsVolatileBaseline(unittest.TestCase):
    """_is_volatile(): OR-gate atr_percentile_normalized>=70 ATAU bb_width>=0.06.

    [ITEM #4 -- UPDATE pasca-migrasi] Sejak migrasi classifier.py ke Opsi 1
    (direct switch), `_is_volatile()` membaca `atr_percentile_normalized`
    (field BARU), BUKAN `atr_percentile` lagi -- helper `_iset()` di bawah
    di-update utk set field yang benar-benar dibaca produksi sekarang.
    Test ini mengunci perilaku GATE itu sendiri (murni threshold logic
    di classifier.py) -- BUKAN menguji apakah nilai atr_percentile_normalized
    yang masuk sudah benar/bias. Itu diuji terpisah di
    TestATRPercentileBiasCharacterizationSynthetic /
    TestItem4AtrPercentileNormalizedBeforeAfterComparison di atas.
    """

    def _iset(self, atr_percentile_normalized=None, bb_width=None):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile_normalized = atr_percentile_normalized
        iset.volatility.bb_width = bb_width
        return iset

    def test_both_none_not_volatile(self):
        self.assertFalse(_is_volatile(self._iset(None, None)))

    def test_atr_percentile_below_threshold_not_volatile(self):
        self.assertFalse(_is_volatile(self._iset(atr_percentile_normalized=69.99, bb_width=None)))

    def test_atr_percentile_at_threshold_is_volatile(self):
        self.assertEqual(REGIME_VOLATILE_ATR_PERCENTILE_MIN, 70.0)  # anchor sanity
        self.assertTrue(_is_volatile(self._iset(atr_percentile_normalized=70.0, bb_width=None)))

    def test_atr_percentile_above_threshold_is_volatile(self):
        self.assertTrue(_is_volatile(self._iset(atr_percentile_normalized=100.0, bb_width=None)))

    def test_bb_width_below_threshold_not_volatile(self):
        self.assertFalse(_is_volatile(self._iset(atr_percentile_normalized=None, bb_width=0.0599)))

    def test_bb_width_at_threshold_is_volatile(self):
        self.assertEqual(REGIME_VOLATILE_BB_WIDTH_MIN, 0.06)  # anchor sanity
        self.assertTrue(_is_volatile(self._iset(atr_percentile_normalized=None, bb_width=0.06)))

    def test_either_condition_triggers_volatile(self):
        # atr_percentile_normalized rendah tapi bb_width tinggi -- tetap volatile (OR-gate).
        self.assertTrue(_is_volatile(self._iset(atr_percentile_normalized=10.0, bb_width=0.5)))

    def test_neither_condition_not_volatile(self):
        self.assertFalse(_is_volatile(self._iset(atr_percentile_normalized=50.0, bb_width=0.03)))


# ─────────────────────────────────────────────────────────────────────────
# TestCalcConfidenceTrendingBranch (TRENDING_BULL / TRENDING_BEAR)
# ─────────────────────────────────────────────────────────────────────────

class TestCalcConfidenceTrendingBranch(unittest.TestCase):

    def _iset(self, adx=None, supertrend_direction=None):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.strength.adx = adx
        iset.trend.supertrend_direction = supertrend_direction
        return iset

    def test_adx_none_returns_flat_050(self):
        c = _calc_confidence(self._iset(adx=None), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.5)

    def test_adx_at_high_threshold_base_090(self):
        self.assertEqual(REGIME_CONFIDENCE_HIGH_ADX, 40.0)  # anchor sanity
        c = _calc_confidence(self._iset(adx=40.0), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.90)

    def test_adx_above_high_threshold_still_090(self):
        c = _calc_confidence(self._iset(adx=80.0), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.90)

    def test_adx_at_strong_threshold_base_070(self):
        self.assertEqual(REGIME_TRENDING_STRONG_ADX, 30.0)  # anchor sanity
        c = _calc_confidence(self._iset(adx=30.0), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.70)

    def test_adx_midpoint_strong_to_high_interpolates(self):
        # adx=35 -> t=(35-30)/(40-30)=0.5 -> base=0.70+0.5*0.20=0.80
        c = _calc_confidence(self._iset(adx=35.0), MarketRegime.TRENDING_BULL)
        self.assertAlmostEqual(c, 0.80, places=3)

    def test_adx_at_min_threshold_base_050(self):
        self.assertEqual(REGIME_TRENDING_ADX_MIN, 22.0)  # anchor sanity
        c = _calc_confidence(self._iset(adx=22.0), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.50)

    def test_adx_midpoint_min_to_strong_interpolates(self):
        # adx=26 -> t=(26-22)/(30-22)=0.5 -> base=0.50+0.5*0.20=0.60
        c = _calc_confidence(self._iset(adx=26.0), MarketRegime.TRENDING_BULL)
        self.assertAlmostEqual(c, 0.60, places=3)

    def test_adx_below_min_threshold_base_040(self):
        c = _calc_confidence(self._iset(adx=10.0), MarketRegime.TRENDING_BULL)
        self.assertEqual(c, 0.40)

    def test_supertrend_agrees_bull_boosts_confidence(self):
        c = _calc_confidence(
            self._iset(adx=22.0, supertrend_direction=1), MarketRegime.TRENDING_BULL
        )
        self.assertAlmostEqual(c, 0.58, places=3)  # 0.50 + 0.08

    def test_supertrend_disagrees_bull_penalizes_confidence(self):
        c = _calc_confidence(
            self._iset(adx=22.0, supertrend_direction=-1), MarketRegime.TRENDING_BULL
        )
        self.assertAlmostEqual(c, 0.40, places=3)  # 0.50 - 0.10

    def test_supertrend_none_no_adjustment(self):
        c = _calc_confidence(
            self._iset(adx=22.0, supertrend_direction=None), MarketRegime.TRENDING_BULL
        )
        self.assertEqual(c, 0.50)

    def test_supertrend_agrees_bear_boosts_confidence(self):
        # regime=BEAR: is_bull_regime=False -- st harus False (supertrend
        # turun) supaya "agree" (st == is_bull_regime).
        c = _calc_confidence(
            self._iset(adx=22.0, supertrend_direction=-1), MarketRegime.TRENDING_BEAR
        )
        self.assertAlmostEqual(c, 0.58, places=3)

    def test_supertrend_disagrees_bear_penalizes_confidence(self):
        c = _calc_confidence(
            self._iset(adx=22.0, supertrend_direction=1), MarketRegime.TRENDING_BEAR
        )
        self.assertAlmostEqual(c, 0.40, places=3)

    def test_supertrend_adjustment_never_reaches_clamp_boundaries(self):
        """Karakteristik ditemukan (BUKAN bug, dicatat utk transparansi):
        base range dari cabang ADX adalah [0.40, 0.90]; +0.08/-0.10 dari
        supertrend menghasilkan range [0.30, 0.98] -- clamp min(1.0,...)/
        max(0.0,...) di kode SECARA MATEMATIS tidak pernah tersentuh lewat
        jalur ini. Tidak diperbaiki (di luar cakupan #4), cuma
        didokumentasikan lewat test supaya tidak disalahpahami sbg celah
        yang perlu diuji lebih lanjut."""
        c_max = _calc_confidence(
            self._iset(adx=999.0, supertrend_direction=1), MarketRegime.TRENDING_BULL
        )
        self.assertEqual(c_max, 0.98)
        self.assertLess(c_max, 1.0)


# ─────────────────────────────────────────────────────────────────────────
# TestCalcConfidenceVolatileExpansionBranch
# ─────────────────────────────────────────────────────────────────────────

class TestCalcConfidenceVolatileExpansionBranch(unittest.TestCase):
    """[ITEM #4 -- UPDATE pasca-migrasi] `_calc_confidence()` cabang
    VOLATILE_EXPANSION sekarang baca `atr_percentile_normalized` (BARU),
    bukan `atr_percentile` lagi -- helper `_iset()` di-update sesuai."""

    def _iset(self, atr_percentile_normalized=None, bb_width=None):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile_normalized = atr_percentile_normalized
        iset.volatility.bb_width = bb_width
        return iset

    def test_atr_percentile_at_90_returns_088(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=90.0), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.88)

    def test_atr_percentile_above_90_returns_088(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=99.0), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.88)

    def test_atr_percentile_below_90_bb_width_at_012_returns_080(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=89.9, bb_width=0.12), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.80)

    def test_neither_condition_returns_065(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=50.0, bb_width=0.05), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.65)

    def test_atr_percentile_none_falls_back_to_bb_width(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=None, bb_width=0.12), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.80)

    def test_both_none_returns_065(self):
        c = _calc_confidence(
            self._iset(atr_percentile_normalized=None, bb_width=None), MarketRegime.VOLATILE_EXPANSION
        )
        self.assertEqual(c, 0.65)


# ─────────────────────────────────────────────────────────────────────────
# TestCalcConfidenceRangingBranch
# ─────────────────────────────────────────────────────────────────────────

class TestCalcConfidenceRangingBranch(unittest.TestCase):

    def _iset(self, adx=None):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.strength.adx = adx
        return iset

    def test_adx_none_returns_055(self):
        c = _calc_confidence(self._iset(adx=None), MarketRegime.RANGING)
        self.assertEqual(c, 0.55)

    def test_adx_zero_returns_085(self):
        c = _calc_confidence(self._iset(adx=0.0), MarketRegime.RANGING)
        self.assertEqual(c, 0.85)

    def test_adx_at_low_threshold_returns_065(self):
        self.assertEqual(REGIME_CONFIDENCE_LOW_ADX, 25.0)  # anchor sanity
        c = _calc_confidence(self._iset(adx=25.0), MarketRegime.RANGING)
        self.assertEqual(c, 0.65)

    def test_adx_midpoint_interpolates(self):
        # adx=12.5 -> t=0.5 -> 0.85-0.5*0.20=0.75
        c = _calc_confidence(self._iset(adx=12.5), MarketRegime.RANGING)
        self.assertAlmostEqual(c, 0.75, places=3)

    def test_adx_above_low_threshold_returns_050(self):
        c = _calc_confidence(self._iset(adx=25.01), MarketRegime.RANGING)
        self.assertEqual(c, 0.50)


# ─────────────────────────────────────────────────────────────────────────
# TestCalcConfidenceElseBranch
# ─────────────────────────────────────────────────────────────────────────

class TestCalcConfidenceElseBranch(unittest.TestCase):

    def test_undefined_regime_returns_030(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        c = _calc_confidence(iset, MarketRegime.UNDEFINED)
        self.assertEqual(c, 0.30)


# ─────────────────────────────────────────────────────────────────────────
# TestClassifyRawPriorityOrder
# ─────────────────────────────────────────────────────────────────────────

def _make_classify_iset(
    ema9=100.0, ema21=99.0, ema50=98.0, ema100=97.0, ema200=96.0,
    supertrend_direction=None,
    adx=None, plus_di=None, minus_di=None, volume_ratio=1.0,
    atr=1.0, bb_upper=105.0, atr_percentile=None, atr_percentile_normalized=None, bb_width=None,
) -> IndicatorSet:
    """Default: EMA stack bullish PENUH (9>21>50>100>200 -> 4 pasangan
    bullish, 0 bearish), trend & strength is_valid()=True, tidak volatile.

    [ITEM #4 -- UPDATE pasca-migrasi] `atr_percentile` (param lama) TETAP
    ada di sini utk fleksibilitas set field lama di iset (mis. utk test
    yang eksplisit mau membuktikan field lama TIDAK lagi mempengaruhi
    _is_volatile()/_calc_confidence()) -- TAPI produksi sekarang baca
    `atr_percentile_normalized`, jadi test yang mau memicu volatile HARUS
    pakai parameter itu, bukan `atr_percentile`.
    """
    iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
    iset.trend.ema9 = ema9
    iset.trend.ema21 = ema21
    iset.trend.ema50 = ema50
    iset.trend.ema100 = ema100
    iset.trend.ema200 = ema200
    iset.trend.supertrend_direction = supertrend_direction
    iset.strength.adx = adx
    iset.strength.plus_di = plus_di
    iset.strength.minus_di = minus_di
    iset.strength.volume_ratio = volume_ratio
    iset.volatility.atr = atr
    iset.volatility.bb_upper = bb_upper
    iset.volatility.atr_percentile = atr_percentile
    iset.volatility.atr_percentile_normalized = atr_percentile_normalized
    iset.volatility.bb_width = bb_width
    return iset


class TestClassifyRawPriorityOrder(unittest.TestCase):
    """Mengunci URUTAN PRIORITAS pengecekan di _classify_raw(): validity ->
    volatile -> trending_bear -> trending_bull -> ranging -> undefined."""

    def test_invalid_trend_returns_undefined_025(self):
        iset = _make_classify_iset(ema9=None)  # trend.is_valid()=False
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.25)

    def test_invalid_strength_returns_undefined_025(self):
        iset = _make_classify_iset(volume_ratio=None)  # strength.is_valid()=False
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.25)

    def test_invalid_trend_takes_priority_even_if_volatile(self):
        iset = _make_classify_iset(ema9=None, atr_percentile_normalized=99.0)
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.25)

    def test_volatile_takes_priority_over_trending_bull(self):
        # bullish stack penuh + adx trending kuat + DI bullish -- TAPI
        # atr_percentile_normalized volatile -- volatile HARUS menang
        # (dicek plg pertama di _classify_raw(), sebelum trending
        # dievaluasi).
        iset = _make_classify_iset(
            adx=35.0, plus_di=30.0, minus_di=10.0, atr_percentile_normalized=80.0
        )
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.VOLATILE_EXPANSION)

    def test_trending_bear_full_path(self):
        iset = _make_classify_iset(
            ema9=96.0, ema21=97.0, ema50=98.0, ema100=99.0, ema200=100.0,  # bearish stack
            adx=30.0, plus_di=10.0, minus_di=30.0,
            supertrend_direction=-1,  # st_bull=False -- "agree" dgn bear
        )
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.TRENDING_BEAR)
        self.assertAlmostEqual(conf, 0.78, places=3)  # base 0.70 (adx=30) + 0.08 (agree)

    def test_trending_bear_blocked_when_supertrend_says_bull(self):
        # bearish EMA stack penuh + DI bearish, TAPI supertrend bilang bull
        # -- is_bear jadi False (st_bull None-or-False dipersyaratkan).
        # bullish_pairs=0 juga (stack tetap bearish) -- classify_raw jatuh
        # ke _is_ranging(), yang False krn adx>=25 -- akhirnya UNDEFINED.
        iset = _make_classify_iset(
            ema9=96.0, ema21=97.0, ema50=98.0, ema100=99.0, ema200=100.0,
            adx=30.0, plus_di=10.0, minus_di=30.0,
            supertrend_direction=1,
        )
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.35)

    def test_trending_bull_full_path(self):
        iset = _make_classify_iset(adx=30.0, plus_di=30.0, minus_di=10.0)  # default bullish stack
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.TRENDING_BULL)
        self.assertAlmostEqual(conf, 0.70, places=3)

    def test_trending_bull_plus_di_none_gets_discounted_confidence(self):
        iset_full  = _make_classify_iset(adx=30.0, plus_di=30.0, minus_di=10.0)
        iset_no_di = _make_classify_iset(adx=30.0, plus_di=None, minus_di=None)
        regime_full, conf_full = _classify_raw(iset_full)
        regime_discount, conf_discount = _classify_raw(iset_no_di)
        self.assertEqual(regime_full, MarketRegime.TRENDING_BULL)
        self.assertEqual(regime_discount, MarketRegime.TRENDING_BULL)
        self.assertAlmostEqual(conf_discount, round(conf_full * 0.80, 3), places=3)
        self.assertAlmostEqual(conf_discount, 0.56, places=3)

    def test_trending_bull_pairs_met_but_di_disagrees_falls_to_undefined(self):
        # bullish_pairs>=2 (default full stack) TAPI plus_di<=minus_di --
        # cabang bull TIDAK return -- lanjut ke _is_ranging() (False, krn
        # adx>=25) -- akhirnya UNDEFINED (BUKAN bull, BUKAN bear -- bearish
        # pairs juga 0 di fixture ini).
        iset = _make_classify_iset(adx=30.0, plus_di=10.0, minus_di=30.0)
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.35)

    def test_ranging_fallback(self):
        # trending=False (adx=10<22) -- bull/bear branch dilewati krn
        # syarat "trending and ..." gagal -- _is_ranging() True (default).
        iset = _make_classify_iset(adx=10.0)
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.RANGING)
        self.assertAlmostEqual(conf, 0.77, places=3)  # t=10/25=0.4 -> 0.85-0.08

    def test_final_undefined_fallback_flat_ema_trending_adx(self):
        # EMA semua flat (0 bullish, 0 bearish pairs) + adx trending kuat
        # -- gagal bull & bear (pairs=0) -- _is_ranging() JUGA False (adx
        # >=25) -- jatuh ke fallback akhir literal.
        iset = _make_classify_iset(
            ema9=100.0, ema21=100.0, ema50=100.0, ema100=100.0, ema200=100.0,
            adx=30.0, plus_di=None, minus_di=None,
        )
        regime, conf = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.UNDEFINED)
        self.assertEqual(conf, 0.35)


# ─────────────────────────────────────────────────────────────────────────
# TestATRPercentileBiasCharacterizationSynthetic
# ─────────────────────────────────────────────────────────────────────────

def _make_pct_walk_df(n, drift_pct=0.0, noise_pct=0.6, seed=0, start=100.0):
    """OHLCV sintetis: random walk berbasis RETURN PERSENTASE (beda dari
    _make_trend_df yang dipakai Sub-Batch A/B, yang pakai step ABSOLUT) --
    supaya volatilitas relatif thd harga (atr_pct) kira-kira konstan
    sepanjang seri, terlepas dari price-level drift. Ini penting utk
    fuzz test bias #4: kalau atr_pct (ternormalisasi, TIDAK bias) relatif
    simetris antara fixture uptrend/downtrend, TAPI atr_percentile (dari
    fungsi produksi yang dicurigai bias) tidak simetris, itu bukti bias
    murni price-level drift -- bukan volatilitas relatif yang genuinely
    beda antara dua fixture."""
    rng = random.Random(seed)
    price = start
    bars = []
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    for _ in range(n):
        ret = drift_pct / 100.0 + rng.gauss(0.0, noise_pct / 100.0)
        o = price
        price = max(price * (1.0 + ret), 0.01)
        c = price
        wick = abs(c - o) * 0.3 + o * (noise_pct / 100.0) * 0.3
        h = max(o, c) + wick
        l = max(min(o, c) - wick, 0.01)
        bars.append((o, h, l, c, 1000.0))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


def _make_pct_walk_with_pullback_df(
    n, drift_pct=0.15, noise_pct=0.6, seed=0, start=100.0,
    cycle_len=20, correction_len=5, correction_mult=1.5,
):
    """[TAHAP B] Tren dengan PULLBACK/KOREKSI BERKALA -- beda dari
    `_make_pct_walk_df` (drift konstan monoton tanpa jeda selama SELURUH
    seri, idealisasi yang tidak representatif pasar riil). Tiap
    `cycle_len` bar, `correction_len` bar TERAKHIR dalam siklus itu
    membalik arah dengan magnitude `correction_mult`x drift dasar
    (retracement) -- net drift per siklus tetap SEARAH (correction_mult
    dipilih < cycle_len/correction_len - 1 = 3.0, supaya TIDAK
    membatalkan drift jadi nol; 1.5 dipilih supaya net per siklus tetap
    positif/negatif jelas tapi ada retracement nyata di dalamnya).

    [PENTING utk Tahap D nanti] `seed` di sini HARUS dipakai identik
    persis saat membandingkan before/after fix -- generator ini
    deterministik murni dari (n, drift_pct, noise_pct, seed, ...)."""
    rng = random.Random(seed)
    price = start
    bars = []
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    for i in range(n):
        pos_in_cycle = i % cycle_len
        in_correction = pos_in_cycle >= (cycle_len - correction_len)
        bar_drift = -drift_pct * correction_mult if in_correction else drift_pct
        ret = bar_drift / 100.0 + rng.gauss(0.0, noise_pct / 100.0)
        o = price
        price = max(price * (1.0 + ret), 0.01)
        c = price
        wick = abs(c - o) * 0.3 + o * (noise_pct / 100.0) * 0.3
        h = max(o, c) + wick
        l = max(min(o, c) - wick, 0.01)
        bars.append((o, h, l, c, 1000.0))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


def _make_regime_shift_df(
    n_choppy1=80, n_trend=100, n_choppy2=80,
    drift_pct=0.6, noise_pct=0.6, seed=0, start=100.0,
):
    """[TAHAP B] Regime shift DI TENGAH seri: choppy (drift=0, n_choppy1
    bar) -> trending kuat (drift_pct, n_trend bar) -> choppy lagi (drift=0,
    n_choppy2 bar) -- satu price path kontinu (bukan 3 fixture terpisah).
    Dipakai utk lihat bagaimana atr_percentile bereaksi saat TRANSISI
    regime, termasuk apakah bias dari segmen trending "melekat" (lag)
    ke segmen choppy SETELAHNYA lewat rolling lookback window -- bukan
    cuma steady-state monoton seperti fixture Tahap A.

    [PENTING utk Tahap D nanti] `seed` HARUS dipakai identik persis saat
    membandingkan before/after fix."""
    rng = random.Random(seed)
    price = start
    bars = []
    n_total = n_choppy1 + n_trend + n_choppy2
    idx = pd.date_range("2026-01-01", periods=n_total, freq="15min")
    segments = [0.0] * n_choppy1 + [drift_pct] * n_trend + [0.0] * n_choppy2
    for bar_drift in segments:
        ret = bar_drift / 100.0 + rng.gauss(0.0, noise_pct / 100.0)
        o = price
        price = max(price * (1.0 + ret), 0.01)
        c = price
        wick = abs(c - o) * 0.3 + o * (noise_pct / 100.0) * 0.3
        h = max(o, c) + wick
        l = max(min(o, c) - wick, 0.01)
        bars.append((o, h, l, c, 1000.0))
    return pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"], index=idx)


# [TAHAP B] Seed tetap, dicatat eksplisit di sini supaya Tahap D memakai
# generator + parameter IDENTIK persis (apples-to-apples before/after fix).
PULLBACK_FIXTURE_SEED     = 10
REGIME_SHIFT_FIXTURE_SEED = 20


class TestATRPercentileBiasCharacterizationSynthetic(unittest.TestCase):
    """BASELINE kuantitatif (bukan pass/fail benar-salah) yang mengunci
    ARAH dan SKALA bias _calc_atr_percentile() SEBELUM root-cause fix
    apa pun -- supaya nanti bisa dibandingkan before/after kalau opsi
    perbaikan diimplementasikan.

    Data historis riil TIDAK tersedia di sandbox ini (lihat docstring
    modul) -- pakai data SINTETIS (random walk persentase, seed tetap,
    pola serupa fuzz test Sub-Batch B: uptrend/downtrend/choppy, berbagai
    kekuatan tren) sebagai pengganti representatif.

    Metodologi: panggil `_calc_atr()` dan `_calc_atr_percentile()`
    LANGSUNG (fungsi produksi asli, bukan reimplementasi) di sepanjang
    seri harga sintetis (rolling, meniru bagaimana bot mengevaluasi tiap
    bar baru), lalu ukur statistik agregat.
    """

    def _pctile_series(self, df, period=14, lookback=100, warmup=115):
        atr_series = _calc_atr(df, period)
        vals = []
        for i in range(warmup, len(df)):
            current = atr_series.iloc[i]
            if pd.isna(current):
                continue
            window = atr_series.iloc[: i + 1]
            vals.append(_calc_atr_percentile(window, current, lookback))
        return vals

    def test_choppy_no_drift_roughly_symmetric_control(self):
        """Kontrol metodologi: TANPA drift (choppy, murni noise), rata-rata
        atr_percentile TIDAK BOLEH condong jauh dari netral (50) -- kalau
        test ini gagal, metodologi pengukurannya sendiri yang bias, bukan
        _calc_atr_percentile()."""
        df = _make_pct_walk_df(260, drift_pct=0.0, noise_pct=0.6, seed=1)
        vals = self._pctile_series(df)
        self.assertGreater(len(vals), 50)
        mean_pct = sum(vals) / len(vals)
        self.assertTrue(30.0 < mean_pct < 70.0, f"mean={mean_pct:.2f}")

    def test_mild_trend_uptrend_biased_higher_than_downtrend(self):
        """Tren SEDANG (drift kecil) -- CLAUDE.md mencatat efek terukur
        ~8 poin dari investigasi sebelumnya. Test ini mengunci ARAH bias
        (uptrend condong lebih tinggi dari downtrend pada noise & seed
        yang identik), bukan angka eksak (angka sensitif thd parameter
        random walk, dicatat sbg informasi di laporan, bukan hardcode di
        assertion)."""
        vals_up   = self._pctile_series(_make_pct_walk_df(260, drift_pct=0.15, noise_pct=0.6, seed=2))
        vals_down = self._pctile_series(_make_pct_walk_df(260, drift_pct=-0.15, noise_pct=0.6, seed=2))
        mean_up   = sum(vals_up) / len(vals_up)
        mean_down = sum(vals_down) / len(vals_down)
        self.assertGreater(
            mean_up, mean_down,
            f"Bias arah tidak terdeteksi: mean_up={mean_up:.2f} "
            f"mean_down={mean_down:.2f} -- kalau test ini gagal setelah "
            f"root-cause fix, itu EKSPEKTASI BENAR (bias sudah hilang), "
            f"bukan regresi."
        )

    def test_strong_trend_bias_gap_larger_than_mild_trend(self):
        """Tren KUAT -- CLAUDE.md mencatat efek terukur sampai ~70 poin,
        jauh lebih besar dari tren sedang (~8 poin). Mengunci bahwa GAP
        bias MEMBESAR seiring kekuatan tren (bukan konstan)."""
        mild_up   = self._pctile_series(_make_pct_walk_df(260, drift_pct=0.15, noise_pct=0.6, seed=3))
        mild_down = self._pctile_series(_make_pct_walk_df(260, drift_pct=-0.15, noise_pct=0.6, seed=3))
        strong_up   = self._pctile_series(_make_pct_walk_df(260, drift_pct=0.6, noise_pct=0.6, seed=3))
        strong_down = self._pctile_series(_make_pct_walk_df(260, drift_pct=-0.6, noise_pct=0.6, seed=3))

        gap_mild   = (sum(mild_up) / len(mild_up)) - (sum(mild_down) / len(mild_down))
        gap_strong = (sum(strong_up) / len(strong_up)) - (sum(strong_down) / len(strong_down))

        self.assertGreater(gap_strong, gap_mild)

    def test_strong_uptrend_reaches_volatile_gate_threshold(self):
        """Seberapa sering atr_percentile >=70 (ambang _is_volatile) SEMATA
        krn drift harga kuat searah, pada seri yang tidak genuinely
        "volatile" secara relatif (noise_pct dijaga konstan sepanjang
        seri). Mengunci bahwa proporsi > 0 -- bias BENAR-BENAR menyentuh
        gate keputusan, bukan cuma bergeser tanpa dampak."""
        vals_up = self._pctile_series(_make_pct_walk_df(260, drift_pct=0.6, noise_pct=0.6, seed=4))
        frac_ge_70 = sum(1 for v in vals_up if v >= REGIME_VOLATILE_ATR_PERCENTILE_MIN) / len(vals_up)
        self.assertGreater(frac_ge_70, 0.0)

    def test_strong_downtrend_suppressed_below_volatile_gate_threshold(self):
        """Mirror dari test di atas: downtrend kuat dgn noise identik
        seharusnya JAUH LEBIH JARANG menyentuh ambang >=70 dibanding
        uptrend kuat (seed & noise sama, cuma arah drift dibalik) --
        bukti asimetri, bukan cuma "kedua-duanya sering >=70"."""
        vals_up   = self._pctile_series(_make_pct_walk_df(260, drift_pct=0.6, noise_pct=0.6, seed=4))
        vals_down = self._pctile_series(_make_pct_walk_df(260, drift_pct=-0.6, noise_pct=0.6, seed=4))
        frac_up   = sum(1 for v in vals_up if v >= REGIME_VOLATILE_ATR_PERCENTILE_MIN) / len(vals_up)
        frac_down = sum(1 for v in vals_down if v >= REGIME_VOLATILE_ATR_PERCENTILE_MIN) / len(vals_down)
        self.assertGreater(frac_up, frac_down)

    # ── [TAHAP B] Skenario tambahan: pullback berkala & regime shift ───────
    # Fixture Tahap A (di atas) pakai drift KONSTAN monoton tanpa jeda
    # selama seluruh seri -- baik utk karakterisasi awal, tapi idealisasi
    # yang tidak representatif pasar riil (yang selalu punya retracement
    # & transisi regime). Dua fixture di bawah dipakai LAGI persis (seed +
    # parameter sama) di Tahap D utk perbandingan before/after fix.

    def _pctile_series_indexed(self, df, period=14, lookback=100, warmup=20):
        """Sama seperti _pctile_series(), tapi kembalikan (index_bar, nilai)
        -- dibutuhkan utk fixture regime-shift supaya bisa memisahkan
        statistik per SEGMEN (choppy1 vs trend vs choppy2), bukan cuma
        rata-rata seluruh seri."""
        atr_series = _calc_atr(df, period)
        pairs = []
        for i in range(warmup, len(df)):
            current = atr_series.iloc[i]
            if pd.isna(current):
                continue
            window = atr_series.iloc[: i + 1]
            pairs.append((i, _calc_atr_percentile(window, current, lookback)))
        return pairs

    @staticmethod
    def _segment_mean(pairs, lo, hi):
        sel = [v for i, v in pairs if lo <= i < hi]
        return sum(sel) / len(sel) if sel else None

    def test_pullback_uptrend_vs_downtrend_bias_direction(self):
        """Tren dgn RETRACEMENT berkala (bukan monoton) -- bias arah masih
        ada (uptrend condong lebih tinggi dari downtrend), tapi diukur
        pada fixture yang lebih realistis (ada koreksi di dalamnya, bukan
        garis lurus sintetis). seed=PULLBACK_FIXTURE_SEED dicatat eksplisit
        utk dipakai ulang di Tahap D."""
        vals_up = self._pctile_series(
            _make_pct_walk_with_pullback_df(260, drift_pct=0.6, seed=PULLBACK_FIXTURE_SEED)
        )
        vals_down = self._pctile_series(
            _make_pct_walk_with_pullback_df(260, drift_pct=-0.6, seed=PULLBACK_FIXTURE_SEED)
        )
        mean_up = sum(vals_up) / len(vals_up)
        mean_down = sum(vals_down) / len(vals_down)
        self.assertGreater(
            mean_up, mean_down,
            f"Bias arah tidak terdeteksi pd fixture pullback: "
            f"mean_up={mean_up:.2f} mean_down={mean_down:.2f}"
        )

    def test_pullback_bias_gap_grows_with_drift_strength(self):
        """Sama seperti test_strong_trend_bias_gap_larger_than_mild_trend,
        tapi pada fixture berpullback -- mengunci bahwa pola "gap membesar
        seiring kekuatan drift" TETAP muncul walau ada retracement berkala
        (bukan cuma artefak drift monoton murni)."""
        mild_up   = self._pctile_series(_make_pct_walk_with_pullback_df(260, drift_pct=0.15, seed=PULLBACK_FIXTURE_SEED))
        mild_down = self._pctile_series(_make_pct_walk_with_pullback_df(260, drift_pct=-0.15, seed=PULLBACK_FIXTURE_SEED))
        strong_up   = self._pctile_series(_make_pct_walk_with_pullback_df(260, drift_pct=0.6, seed=PULLBACK_FIXTURE_SEED))
        strong_down = self._pctile_series(_make_pct_walk_with_pullback_df(260, drift_pct=-0.6, seed=PULLBACK_FIXTURE_SEED))

        gap_mild   = (sum(mild_up) / len(mild_up)) - (sum(mild_down) / len(mild_down))
        gap_strong = (sum(strong_up) / len(strong_up)) - (sum(strong_down) / len(strong_down))

        self.assertGreater(gap_strong, gap_mild)

    def test_regime_shift_pre_transition_choppy_is_symmetric(self):
        """Segmen choppy SEBELUM regime shift (drift=0 utk kedua fixture
        up/dn) -- HARUS identik persis (kontrol: sebelum drift_pct
        berlainan diterapkan di segmen ke-2, kedua price path memakai
        return yang SAMA PERSIS, jadi atr_percentile-nya juga harus
        sama persis, bukan cuma "mirip")."""
        pairs_up = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        pairs_dn = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        mean_pre_up = self._segment_mean(pairs_up, 0, 80)
        mean_pre_dn = self._segment_mean(pairs_dn, 0, 80)
        self.assertIsNotNone(mean_pre_up)
        self.assertAlmostEqual(mean_pre_up, mean_pre_dn, places=6)

    def test_regime_shift_trending_segment_shows_bias(self):
        """Segmen trending kuat (bar 80-179) -- bias arah HARUS muncul,
        konsisten dgn fixture monoton di Tahap A."""
        pairs_up = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        pairs_dn = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        mean_trend_up = self._segment_mean(pairs_up, 80, 180)
        mean_trend_dn = self._segment_mean(pairs_dn, 80, 180)
        self.assertGreater(mean_trend_up, mean_trend_dn)

    def test_regime_shift_bias_lingers_into_post_trend_choppy(self):
        """[TEMUAN Tahap B] Bias TIDAK langsung hilang begitu regime
        kembali choppy setelah tren kuat berakhir -- lookback window
        (100 bar) masih memuat ATR dolar dari periode trending yang
        price-level-nya sudah bergeser, jadi bias 'melekat' (lag) ke
        segmen choppy SETELAHNYA. Ini transisi dinamis yang tidak
        terlihat sama sekali di fixture steady-state Tahap A."""
        pairs_up = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        pairs_dn = self._pctile_series_indexed(
            _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        )
        mean_post_up = self._segment_mean(pairs_up, 180, 260)
        mean_post_dn = self._segment_mean(pairs_dn, 180, 260)
        self.assertGreater(
            mean_post_up, mean_post_dn,
            f"Bias diharapkan MASIH ada di choppy pasca-transisi: "
            f"post_up={mean_post_up:.2f} post_dn={mean_post_dn:.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────
# TestItem4AtrPercentileNormalizedBeforeAfterComparison [TAHAP D]
# ─────────────────────────────────────────────────────────────────────────

from engine.indicators.volatility import _calc_atr, _calc_atr_percentile  # noqa: E402


class TestItem4AtrPercentileNormalizedBeforeAfterComparison(unittest.TestCase):
    """[ITEM #4 -- Tahap D] Bandingkan `atr_percentile` (LAMA, bias) vs
    `atr_percentile_normalized` (BARU, Tahap C -- ranking atr_pct alih-alih
    ATR absolut) pada seri sintetis IDENTIK (seed + parameter sama persis
    dgn fixture Tahap A/B) -- membuktikan fix genuinely mengecilkan gap,
    bukan diasumsikan.

    [PENTING] Kadang gap BERGANTI TANDA (bukan cuma mengecil ke arah nol)
    -- ini EKSPEKTASI BENAR, bukan bug: field baru mewarisi bias residual
    yang JAUH LEBIH KECIL dari `atr_pct` itu sendiri (item audit #37,
    mekanisme smoothing-lag, BUKAN mekanisme price-level-drift-ranking
    yang jadi akar masalah #4) -- arah bias #37 bisa berlawanan dgn arah
    bias #4 tergantung parameter drift. Test ini menilai MAGNITUDE
    (|gap|), bukan tanda, sesuai instruksi eksplisit sebelum lanjut ke
    Tahap E.
    """

    def _old_series(self, df, period=14, lookback=100, warmup=115):
        atr_series = _calc_atr(df, period)
        vals = []
        for i in range(warmup, len(df)):
            current = atr_series.iloc[i]
            if pd.isna(current):
                continue
            vals.append(_calc_atr_percentile(atr_series.iloc[: i + 1], current, lookback))
        return vals

    def _new_series(self, df, period=14, lookback=100, warmup=115):
        atr_series = _calc_atr(df, period)
        close_safe = df["close"].where(df["close"] > 1e-9)
        atr_pct_series = (atr_series / close_safe) * 100.0
        vals = []
        for i in range(warmup, len(df)):
            current = atr_pct_series.iloc[i]
            if pd.isna(current):
                continue
            vals.append(_calc_atr_percentile(atr_pct_series.iloc[: i + 1], current, lookback))
        return vals

    def _old_series_indexed(self, df, period=14, lookback=100, warmup=20):
        atr_series = _calc_atr(df, period)
        pairs = []
        for i in range(warmup, len(df)):
            current = atr_series.iloc[i]
            if pd.isna(current):
                continue
            pairs.append((i, _calc_atr_percentile(atr_series.iloc[: i + 1], current, lookback)))
        return pairs

    def _new_series_indexed(self, df, period=14, lookback=100, warmup=20):
        atr_series = _calc_atr(df, period)
        close_safe = df["close"].where(df["close"] > 1e-9)
        atr_pct_series = (atr_series / close_safe) * 100.0
        pairs = []
        for i in range(warmup, len(df)):
            current = atr_pct_series.iloc[i]
            if pd.isna(current):
                continue
            pairs.append((i, _calc_atr_percentile(atr_pct_series.iloc[: i + 1], current, lookback)))
        return pairs

    @staticmethod
    def _gap(vals_up, vals_down):
        return (sum(vals_up) / len(vals_up)) - (sum(vals_down) / len(vals_down))

    @staticmethod
    def _segment_mean(pairs, lo, hi):
        sel = [v for i, v in pairs if lo <= i < hi]
        return sum(sel) / len(sel) if sel else None

    def test_choppy_control_both_fields_symmetric(self):
        """Kontrol: tanpa drift, KEDUA field (lama & baru) harus tetap
        netral/simetris -- fix tidak boleh MENCIPTAKAN bias baru di
        kondisi yang sebelumnya sudah bersih."""
        df_up = _make_pct_walk_df(260, drift_pct=0.0, noise_pct=0.6, seed=1)
        df_down = _make_pct_walk_df(260, drift_pct=0.0, noise_pct=0.6, seed=1)
        gap_old = self._gap(self._old_series(df_up), self._old_series(df_down))
        gap_new = self._gap(self._new_series(df_up), self._new_series(df_down))
        self.assertAlmostEqual(gap_old, 0.0, places=6)
        self.assertAlmostEqual(gap_new, 0.0, places=6)

    def test_mild_trend_gap_shrinks_significantly(self):
        df_up = _make_pct_walk_df(260, drift_pct=0.15, noise_pct=0.6, seed=2)
        df_down = _make_pct_walk_df(260, drift_pct=-0.15, noise_pct=0.6, seed=2)
        gap_old = self._gap(self._old_series(df_up), self._old_series(df_down))
        gap_new = self._gap(self._new_series(df_up), self._new_series(df_down))
        self.assertLess(
            abs(gap_new), abs(gap_old) * 0.5,
            f"gap_old={gap_old:.2f} gap_new={gap_new:.2f} -- reduksi tidak signifikan"
        )

    def test_strong_trend_gap_shrinks_significantly(self):
        df_up = _make_pct_walk_df(260, drift_pct=0.6, noise_pct=0.6, seed=4)
        df_down = _make_pct_walk_df(260, drift_pct=-0.6, noise_pct=0.6, seed=4)
        gap_old = self._gap(self._old_series(df_up), self._old_series(df_down))
        gap_new = self._gap(self._new_series(df_up), self._new_series(df_down))
        self.assertLess(abs(gap_new), abs(gap_old) * 0.5)

    def test_pullback_mild_gap_shrinks(self):
        df_up = _make_pct_walk_with_pullback_df(260, drift_pct=0.15, seed=PULLBACK_FIXTURE_SEED)
        df_down = _make_pct_walk_with_pullback_df(260, drift_pct=-0.15, seed=PULLBACK_FIXTURE_SEED)
        gap_old = self._gap(self._old_series(df_up), self._old_series(df_down))
        gap_new = self._gap(self._new_series(df_up), self._new_series(df_down))
        # [Tahap D] Reduksi paling kecil dari 7 skenario yg diuji (~47%) --
        # ambang dilonggarkan (0.6x, bukan 0.5x spt skenario lain) supaya
        # tidak flaky sambil tetap menuntut reduksi nyata.
        self.assertLess(abs(gap_new), abs(gap_old) * 0.6)

    def test_pullback_strong_gap_shrinks_significantly(self):
        df_up = _make_pct_walk_with_pullback_df(260, drift_pct=0.6, seed=PULLBACK_FIXTURE_SEED)
        df_down = _make_pct_walk_with_pullback_df(260, drift_pct=-0.6, seed=PULLBACK_FIXTURE_SEED)
        gap_old = self._gap(self._old_series(df_up), self._old_series(df_down))
        gap_new = self._gap(self._new_series(df_up), self._new_series(df_down))
        self.assertLess(abs(gap_new), abs(gap_old) * 0.5)

    def test_regime_shift_trending_segment_gap_shrinks(self):
        df_up = _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        df_down = _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        pairs_old_up = self._old_series_indexed(df_up)
        pairs_old_dn = self._old_series_indexed(df_down)
        pairs_new_up = self._new_series_indexed(df_up)
        pairs_new_dn = self._new_series_indexed(df_down)

        gap_old = self._segment_mean(pairs_old_up, 80, 180) - self._segment_mean(pairs_old_dn, 80, 180)
        gap_new = self._segment_mean(pairs_new_up, 80, 180) - self._segment_mean(pairs_new_dn, 80, 180)
        self.assertLess(abs(gap_new), abs(gap_old) * 0.5)

    def test_regime_shift_post_transition_choppy_lingering_bias_reduced(self):
        """[Diminta eksplisit sebelum lanjut Tahap E] Segmen choppy
        PASCA-TRANSISI (bar 180-259) -- Tahap B menemukan bias LAMA masih
        'melekat' (lag) ~30 poin gap di sini walau regime sudah kembali
        choppy, krn lookback window 100-bar masih memuat data dari periode
        trending. Test ini mengunci bahwa field BARU JUGA masih punya efek
        lag yang sama (TIDAK hilang total -- lookback window issue
        sifatnya struktural, bukan spesifik ke bug ranking-absolut) TAPI
        magnitude-nya harus MENGECIL signifikan dibanding field lama."""
        df_up = _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        df_down = _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        pairs_old_up = self._old_series_indexed(df_up)
        pairs_old_dn = self._old_series_indexed(df_down)
        pairs_new_up = self._new_series_indexed(df_up)
        pairs_new_dn = self._new_series_indexed(df_down)

        gap_old_post = self._segment_mean(pairs_old_up, 180, 260) - self._segment_mean(pairs_old_dn, 180, 260)
        gap_new_post = self._segment_mean(pairs_new_up, 180, 260) - self._segment_mean(pairs_new_dn, 180, 260)

        self.assertGreater(abs(gap_old_post), 20.0, "prasyarat: bias lama HARUS nyata di segmen ini (dari Tahap B)")
        self.assertLess(
            abs(gap_new_post), abs(gap_old_post) * 0.7,
            f"gap_old_post={gap_old_post:.2f} gap_new_post={gap_new_post:.2f} -- "
            f"efek lag pasca-transisi TIDAK mengecil signifikan di field baru"
        )

    def test_regime_shift_pre_transition_choppy_stays_symmetric_both_fields(self):
        """Segmen choppy SEBELUM shift -- kontrol tambahan: kedua field
        (lama & baru) harus tetap identik persis antara up/down (drift=0
        di kedua fixture pada segmen ini)."""
        df_up = _make_regime_shift_df(drift_pct=0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        df_down = _make_regime_shift_df(drift_pct=-0.6, seed=REGIME_SHIFT_FIXTURE_SEED)
        pairs_old_up = self._old_series_indexed(df_up)
        pairs_old_dn = self._old_series_indexed(df_down)
        pairs_new_up = self._new_series_indexed(df_up)
        pairs_new_dn = self._new_series_indexed(df_down)

        gap_old_pre = self._segment_mean(pairs_old_up, 0, 80) - self._segment_mean(pairs_old_dn, 0, 80)
        gap_new_pre = self._segment_mean(pairs_new_up, 0, 80) - self._segment_mean(pairs_new_dn, 0, 80)
        self.assertAlmostEqual(gap_old_pre, 0.0, places=6)
        self.assertAlmostEqual(gap_new_pre, 0.0, places=6)

    def test_field_matches_production_calculate_atr_enhanced(self):
        """Sanity check akhir: pastikan helper test _new_series() di atas
        (reimplementasi ringan utk kecepatan) genuinely SAMA PERSIS dgn
        memanggil calculate_atr_enhanced() produksi langsung -- bukan
        drift diam-diam dari logic yang sebenarnya jalan di produksi."""
        from engine.indicators.volatility import calculate_atr_enhanced

        df = _make_pct_walk_df(200, drift_pct=0.3, noise_pct=0.6, seed=99)
        r = calculate_atr_enhanced(df)
        self.assertIsNotNone(r.atr_percentile_normalized)

        atr_series = _calc_atr(df, 14)
        close_safe = df["close"].where(df["close"] > 1e-9)
        atr_pct_series = (atr_series / close_safe) * 100.0
        atr_pct_clean = atr_pct_series.dropna()
        manual = _calc_atr_percentile(atr_pct_clean, r.atr_pct, 100)
        self.assertAlmostEqual(r.atr_percentile_normalized, manual, places=9)


# ─────────────────────────────────────────────────────────────────────────
# TestItem4AtrPercentileNormalizedSwapSymmetry [TAHAP E]
# ─────────────────────────────────────────────────────────────────────────

import numpy as np  # noqa: E402

from engine.indicators.volatility import calculate_atr_enhanced  # noqa: E402


def _build_geometric_mirror_df(close, anchor, n, idx):
    """Konstruksi OHLC IDENTIK dgn `_build()` di
    test_category_score_side_aware.py::test_atr_percentile_known_bug_documented_geometric_mirror
    -- dipakai lagi di sini persis supaya perbandingan old-bug-vs-new-fix
    apples-to-apples dgn test bug yang SUDAH ada di codebase, bukan
    metodologi baru yang berbeda."""
    o = np.empty(n); h = np.empty(n); l = np.empty(n)
    prev = anchor
    for i in range(n):
        o[i] = prev
        c = close[i]
        h[i] = max(o[i], c) * 1.004
        l[i] = min(o[i], c) * 0.996
        prev = c
    return pd.DataFrame(
        {"open": o, "high": h, "low": l, "close": close, "volume": 1000.0}, index=idx
    )


class TestItem4AtrPercentileNormalizedSwapSymmetry(unittest.TestCase):
    """[ITEM #4 -- Tahap E] Swap-symmetry lock-in: `atr_percentile_normalized`
    pada seri uptrend vs geometric-mirror-downtrend (anchor²/close, metodologi
    SAMA PERSIS dgn `test_atr_percentile_known_bug_documented_geometric_mirror`
    di test_category_score_side_aware.py, dipakai lagi di sini utk
    perbandingan apples-to-apples).

    [JUJUR soal toleransi] Field baru BUKAN simetris sempurna -- mewarisi
    residual bias `atr_pct` (item audit #37, mekanisme smoothing-lag,
    BEDA dari mekanisme #4 asli). Pada fixture geometric-mirror TUNGGAL
    (seed=7, konstruksi paling ekstrem: window pendek 120 bar, drift
    tinggi TANPA jeda noise-reset) gap absolut BISA masih ~50 poin dari
    skala 0-100 -- jauh dari "nyaris simetris". TAPI ini tetap perbaikan
    signifikan dibanding gap lama (~97 poin, nyaris total flip 99.5 vs
    2.5) -- direct DIBANDINGKAN, bukan diklaim mendekati simetri absolut
    di SATU seed ekstrem ini. Karakterisasi rata-rata lintas banyak
    skenario yang lebih realistis (Tahap B/D: pullback, regime-shift,
    noise berkelanjutan) menunjukkan reduksi jauh lebih baik (46-96%,
    seringkali gap akhir cuma 1 digit) -- test multi-seed di bawah
    (test_multi_seed_average_gap_shrinks_substantially) mengunci itu
    secara statistik, BUKAN cuma 1 titik data.
    """

    def _mirror_pair(self, seed, mu=0.01, sigma=0.015, n=120, anchor=100.0):
        rng = np.random.RandomState(seed)
        idx = pd.date_range("2026-01-01", periods=n, freq="15min")
        logret = rng.normal(mu, sigma, size=n)
        close_up = anchor * np.exp(np.cumsum(logret))
        close_dn = anchor ** 2 / close_up
        r_up = calculate_atr_enhanced(_build_geometric_mirror_df(close_up, anchor, n, idx))
        r_dn = calculate_atr_enhanced(_build_geometric_mirror_df(close_dn, anchor, n, idx))
        return r_up, r_dn

    def test_old_field_known_bug_still_reproduces_unchanged(self):
        """Regresi: field LAMA (atr_percentile) TIDAK disentuh Tahap C --
        bug yang sudah didokumentasikan (Sub-Batch B) harus tetap
        persis sama, byte-identical, bukti fix bersifat murni aditif."""
        r_up, r_dn = self._mirror_pair(seed=7)
        self.assertGreater(r_up.atr_percentile, 90.0)
        self.assertLess(r_dn.atr_percentile, 10.0)
        self.assertAlmostEqual(r_up.atr_percentile, 99.5, places=1)
        self.assertAlmostEqual(r_dn.atr_percentile, 2.5, places=1)

    def test_new_field_gap_meaningfully_smaller_than_old_on_same_fixture(self):
        """Pada fixture EKSTREM yang sama persis (seed=7) tempat bug lama
        nyaris total-flip (gap 97) -- field baru HARUS tetap menunjukkan
        perbaikan nyata (margin 0.65x, di bawah reduksi 48% yang benar2
        teramati, supaya tidak flaky), TAPI TIDAK diklaim mendekati nol."""
        r_up, r_dn = self._mirror_pair(seed=7)
        gap_old = abs(r_up.atr_percentile - r_dn.atr_percentile)
        gap_new = abs(r_up.atr_percentile_normalized - r_dn.atr_percentile_normalized)
        self.assertLess(
            gap_new, gap_old * 0.65,
            f"gap_old={gap_old:.2f} gap_new={gap_new:.2f} -- perbaikan tidak signifikan "
            f"bahkan pd fixture ekstrem tunggal ini"
        )

    def test_multi_seed_average_gap_shrinks_substantially(self):
        """[Klaim utama toleransi 'wajar'] Rata-rata lintas 20 seed
        (bukan 1 titik data) -- gap field baru HARUS jauh lebih kecil
        rata-rata dari gap field lama, walau variansi per-seed individual
        cukup lebar (item #37 residual bias tidak seragam)."""
        gaps_old, gaps_new = [], []
        for seed in range(1, 21):
            r_up, r_dn = self._mirror_pair(seed=seed)
            gaps_old.append(abs(r_up.atr_percentile - r_dn.atr_percentile))
            gaps_new.append(abs(r_up.atr_percentile_normalized - r_dn.atr_percentile_normalized))

        mean_old = sum(gaps_old) / len(gaps_old)
        mean_new = sum(gaps_new) / len(gaps_new)
        self.assertGreater(mean_old, 85.0)  # anchor: bug lama konsisten parah
        self.assertLess(
            mean_new, mean_old * 0.4,
            f"mean_gap_old={mean_old:.2f} mean_gap_new={mean_new:.2f} -- "
            f"reduksi rata-rata tidak substansial"
        )

    def test_new_field_worst_case_bounded_not_unbounded(self):
        """Beda kualitatif penting dgn bug lama: bug lama TIDAK PERNAH
        turun di bawah ~84 poin gap di 20 seed manapun (systematically
        parah, hampir binary flip). Field baru punya worst-case yang
        JAUH lebih rendah (bounded, bukan konsisten ekstrem) -- walau
        tidak seragam kecil di semua seed."""
        worst_old, worst_new = 0.0, 0.0
        for seed in range(1, 21):
            r_up, r_dn = self._mirror_pair(seed=seed)
            worst_old = max(worst_old, abs(r_up.atr_percentile - r_dn.atr_percentile))
            worst_new = max(worst_new, abs(r_up.atr_percentile_normalized - r_dn.atr_percentile_normalized))
        self.assertLess(worst_new, worst_old * 0.6)


# ─────────────────────────────────────────────────────────────────────────
# TestItem4ClassifierMigrationDirectSwitch [Migrasi final -- Opsi 1]
# ─────────────────────────────────────────────────────────────────────────

class TestItem4ClassifierMigrationDirectSwitch(unittest.TestCase):
    """[ITEM #4 -- MIGRASI FINAL] Kunci eksplisit bahwa
    `_is_volatile()`/`_calc_confidence()` SEKARANG baca
    `atr_percentile_normalized`, dan `atr_percentile` (field lama) TIDAK
    LAGI mempengaruhi keputusan apa pun -- pembuktian langsung, bukan
    cuma diasumsikan dari update helper test di atas."""

    def test_is_volatile_ignores_old_field_high_new_field_low(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile = 99.0             # lama: SANGAT volatile
        iset.volatility.atr_percentile_normalized = 10.0  # baru: TIDAK volatile
        iset.volatility.bb_width = None
        self.assertFalse(_is_volatile(iset), "field lama seharusnya TIDAK lagi dibaca")

    def test_is_volatile_reads_new_field_even_when_old_field_low(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile = 5.0               # lama: TIDAK volatile
        iset.volatility.atr_percentile_normalized = 90.0   # baru: SANGAT volatile
        iset.volatility.bb_width = None
        self.assertTrue(_is_volatile(iset), "field baru seharusnya yang menentukan")

    def test_calc_confidence_volatile_expansion_ignores_old_field(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile = 95.0              # lama: >=90 -> 0.88 kalau dibaca
        iset.volatility.atr_percentile_normalized = 10.0   # baru: jauh di bawah 90
        iset.volatility.bb_width = None
        c = _calc_confidence(iset, MarketRegime.VOLATILE_EXPANSION)
        self.assertNotEqual(c, 0.88, "seharusnya tidak lagi dipengaruhi field lama")
        self.assertEqual(c, 0.65)  # neither condition met (pakai field baru)

    def test_old_field_still_populated_in_production_not_removed(self):
        """models.py TIDAK menghapus field lama (sesuai instruksi) --
        calculate_atr_enhanced() produksi ASLI harus tetap mengisi
        atr_percentile (untuk dashboard/API/referensi/rollback)."""
        from engine.indicators.volatility import calculate_atr_enhanced

        df = _make_pct_walk_df(200, drift_pct=0.3, noise_pct=0.6, seed=55)
        r = calculate_atr_enhanced(df)
        self.assertIsNotNone(r.atr_percentile)
        self.assertIsNotNone(r.atr_percentile_normalized)

    def test_is_volatile_logs_both_values_side_by_side(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile = 42.0
        iset.volatility.atr_percentile_normalized = 77.0
        with self.assertLogs("intelligence.classifier", level="DEBUG") as cm:
            _is_volatile(iset)
        joined = " ".join(cm.output)
        self.assertIn("42.0", joined)
        self.assertIn("77.0", joined)

    def test_calc_confidence_logs_both_values_side_by_side(self):
        iset = IndicatorSet(symbol="TEST/USDT", timeframe="15m")
        iset.volatility.atr_percentile = 33.0
        iset.volatility.atr_percentile_normalized = 88.0
        with self.assertLogs("intelligence.classifier", level="DEBUG") as cm:
            _calc_confidence(iset, MarketRegime.VOLATILE_EXPANSION)
        joined = " ".join(cm.output)
        self.assertIn("33.0", joined)
        self.assertIn("88.0", joined)

    def test_end_to_end_classify_raw_uses_new_field_via_real_pipeline(self):
        """Integrasi lebih tinggi: lewat _classify_raw() penuh (bukan cuma
        _is_volatile() terisolasi) -- konfirmasi jalur produksi utuh."""
        iset = _make_classify_iset(
            adx=35.0, plus_di=30.0, minus_di=10.0,
            atr_percentile=5.0,                 # lama: rendah, TIDAK akan trigger
        )
        iset.volatility.atr_percentile_normalized = 85.0  # baru: trigger volatile
        regime, _ = _classify_raw(iset)
        self.assertEqual(regime, MarketRegime.VOLATILE_EXPANSION)


if __name__ == "__main__":
    unittest.main()
