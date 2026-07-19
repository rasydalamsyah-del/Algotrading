"""
engine/intelligence/test_scorer_signal_confirm_buffer_side_aware.py --
Test untuk bug-fix #25: _SIGNAL_CONFIRM_BUFFER (engine/intelligence/scorer.py)
sebelumnya di-key HANYA by `symbol` -- pola bug sama persis dgn
_OBSERVATION_CACHE (engine/intelligence/observer.py) sebelum diperbaiki di
Sub-Batch D (proyek MTF Composite Side-Aware), TAPI di luar cakupan proyek
itu, jadi masih ada sampai fix ini.

[ROOT CAUSE] main_future.py bisa mengevaluasi kedua sisi (long & short)
utk simbol yang sama dalam satu siklus lewat score_signal(..., side=).
Karena buffer konfirmasi (menghitung berapa siklus berturut-turut skor
melewati threshold sebelum genuinely emit signal_type="buy") cuma di-key
`symbol`, permintaan side="short" bisa diam-diam membaca/menimpa progres
konfirmasi milik side="long" utk simbol yang sama -- silent cross-side
contamination. Belum termanifestasi di produksi krn belum ada short yang
lolos threshold utk memicu buffer ini saat gap ini ditemukan.

[BEDA dari _OBSERVATION_CACHE, TIDAK straight-copy pola fix-nya]
_OBSERVATION_CACHE pakai string key dgn `side` sbg SUFFIX krn ADA consumer
lain yang match key via `key.startswith(f"{symbol}|{timeframe}|")`
(get_cached_observation()/clear_cache()) -- suffix dipilih supaya
prefix-matching itu tetap jalan. _SIGNAL_CONFIRM_BUFFER TIDAK punya
consumer serupa (diverifikasi lewat grep repo-wide -- scorer.py
satu-satunya pembaca/penulis, tidak ada clear_cache()-like function,
tidak ada prefix-matching manapun), jadi fix di sini pakai tuple key
(symbol, side) -- lebih sederhana, tidak perlu asumsi symbol tidak pernah
mengandung karakter delimiter apa pun.

Fix: `_SIGNAL_CONFIRM_BUFFER[symbol]` -> `_SIGNAL_CONFIRM_BUFFER[(symbol, side)]`
di kelima titik akses (get/set/reset).

[Pola test] score_signal() dipanggil SUNGGUHAN (bukan reimplementasi logic
buffer), tapi gate-gate di sekitarnya (_check_primary_trigger,
_extract_indicator_scores, _calc_weighted_breakdown, get_dynamic_threshold,
SIGNAL_CONFIRMATION_MATRIX) di-patch supaya deterministik mencapai baris
buffer tanpa perlu membangun IndicatorSet/profile realistis penuh --
konsisten dgn prinsip proyek ("verifikasi lewat jalur produksi asli",
bukan reimplementasi, tapi bagian yang tidak relevan dgn bug ini boleh
disederhanakan supaya fokus & deterministik).

    python3 -m unittest engine.intelligence.test_scorer_signal_confirm_buffer_side_aware -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engine.core.models import IndicatorSet, MarketRegime, ObservationReport, ScoreBreakdown
from engine.intelligence import scorer as scorer_module
from engine.intelligence.scorer import score_signal


def _profile_cfg(confirmation_min_score=0.0):
    return SimpleNamespace(
        atr_sl_mult=1.5, atr_tp_mult=2.5, quick_sl_pct=1.0, quick_tp_pct=2.0,
        confirmation_min_score=confirmation_min_score,
    )


def _observation(symbol="TEST/USDT", conf_score=80.0):
    iset = IndicatorSet(symbol=symbol, timeframe="15m", current_price=100.0)
    return ObservationReport(
        symbol=symbol, strategy_profile="mean_revert",
        primary_tf_indicators=iset, primary_tf_valid=True,
        confirmation_tf_score=conf_score, confirmation_tf_valid=True,
    )


class _BufferTestBase(unittest.TestCase):
    """Bypass seluruh gate scoring di score_signal() (trigger, indicator
    extraction, weighted breakdown, dynamic threshold) supaya total_score
    SELALU 100.0 dan SELALU >= threshold(0.0) -- deterministik mencapai
    baris buffer konfirmasi di setiap panggilan, terlepas dari `side`."""

    REQUIRED = 3

    def setUp(self):
        scorer_module._SIGNAL_CONFIRM_BUFFER.clear()
        patched_matrix = {
            "ranging": self.REQUIRED, "trending_bull": self.REQUIRED,
            "trending_bear": 999, "volatile_expansion": self.REQUIRED,
            "undefined": self.REQUIRED,
        }
        self._patchers = [
            patch.object(scorer_module, "_check_primary_trigger", return_value=(True, "forced")),
            patch.object(scorer_module, "_extract_indicator_scores", return_value={}),
            patch.object(scorer_module, "_calc_weighted_breakdown",
                         return_value=ScoreBreakdown(trend_weighted=100.0)),
            patch.object(scorer_module, "get_dynamic_threshold", return_value=0.0),
            patch.object(scorer_module, "SIGNAL_CONFIRMATION_MATRIX", patched_matrix),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])
        self.addCleanup(scorer_module._SIGNAL_CONFIRM_BUFFER.clear)

    def _score(self, symbol, side, conf_score=80.0):
        return score_signal(
            _observation(symbol, conf_score=conf_score), MarketRegime.RANGING, 0.8,
            profile_override=_profile_cfg(), side=side,
        )


class TestSignalConfirmBufferKeyedByTuple(_BufferTestBase):

    def test_buffer_key_is_symbol_side_tuple_not_symbol_alone(self):
        """[Regresi kunci -- inti bug #25] Setelah 1 panggilan side='long',
        buffer HARUS tersimpan di key (symbol, 'long'), BUKAN di key
        symbol polos (string) -- membuktikan fix genuinely tuple-keyed."""
        symbol = "AAA/USDT"
        self._score(symbol, side="long")

        self.assertIn((symbol, "long"), scorer_module._SIGNAL_CONFIRM_BUFFER)
        self.assertNotIn(symbol, scorer_module._SIGNAL_CONFIRM_BUFFER)
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 1)


class TestNoCrossSideContamination(_BufferTestBase):

    def test_short_call_does_not_inherit_long_progress(self):
        """[Regresi kunci -- inti bug #25] symbol yang sama, long dipanggil
        2x (progres 2/3), LALU short dipanggil pertama kali utk symbol yang
        sama -- short HARUS mulai dari count=1 (fresh), BUKAN mewarisi
        count=2 milik long."""
        symbol = "BBB/USDT"
        self._score(symbol, side="long")
        self._score(symbol, side="long")
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 2)

        self._score(symbol, side="short")

        self.assertEqual(
            scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "short")]["count"], 1,
            "short HARUS punya counter independen, bukan mewarisi progres long",
        )
        # Progres long TIDAK BOLEH tersentuh/direset oleh panggilan short.
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 2)

    def test_long_confirms_independently_while_short_still_accumulating(self):
        """Long mencapai REQUIRED (3) dan emit 'buy' + reset ke 0, SEMENTARA
        short (diselang-seling di antara panggilan long utk symbol yang
        sama) tetap di progresnya sendiri, tidak ikut ter-reset."""
        symbol = "CCC/USDT"

        self._score(symbol, side="long")   # long: 1
        self._score(symbol, side="short")  # short: 1
        self._score(symbol, side="long")   # long: 2
        self._score(symbol, side="short")  # short: 2
        result_long = self._score(symbol, side="long")  # long: 3 -> confirm+reset

        self.assertEqual(result_long.signal_type, "buy")
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 0)
        # Short TIDAK BOLEH ikut ter-reset oleh konfirmasi long.
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "short")]["count"], 2)

    def test_interleaved_confirmations_both_sides_reach_buy_independently(self):
        """Kedua sisi genuinely bisa mencapai konfirmasi 'buy' independen
        satu sama lain utk symbol yang sama, tanpa saling mengganggu hitungan."""
        symbol = "DDD/USDT"

        for _ in range(self.REQUIRED - 1):
            self._score(symbol, side="long")
            self._score(symbol, side="short")

        result_long  = self._score(symbol, side="long")
        result_short = self._score(symbol, side="short")

        self.assertEqual(result_long.signal_type, "buy")
        self.assertEqual(result_short.signal_type, "buy")

    def test_different_symbols_same_side_remain_independent(self):
        """[Non-regresi] Dua symbol berbeda, side sama -- tetap independen
        seperti perilaku lama (sebelum fix, ini sudah benar; pastikan tuple
        key tidak merusaknya)."""
        self._score("EEE/USDT", side="long")
        self._score("EEE/USDT", side="long")
        self._score("FFF/USDT", side="long")

        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[("EEE/USDT", "long")]["count"], 2)
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[("FFF/USDT", "long")]["count"], 1)


class TestSingleSideCallerBackwardCompatible(_BufferTestBase):
    """[Non-regresi -- risiko yang diminta dicek eksplisit] Caller lama
    yang cuma pernah pakai side='long' default (spot, SEMUA caller sebelum
    proyek futures) HARUS berperilaku identik dgn sebelum fix -- cuma
    bentuk key internal yang berubah (tuple, bukan symbol polos), progres
    counting & hasil akhir 'buy' TIDAK berubah sama sekali."""

    def test_default_side_long_confirms_after_required_count_identical_to_before(self):
        symbol = "GGG/USDT"
        results = [self._score(symbol, side="long") for _ in range(self.REQUIRED)]

        for r in results[:-1]:
            self.assertEqual(r.signal_type, "hold")
        self.assertEqual(results[-1].signal_type, "buy")

    def test_score_below_threshold_resets_buffer_same_as_before(self):
        symbol = "HHH/USDT"
        self._score(symbol, side="long")
        self._score(symbol, side="long")
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 2)

        with patch.object(scorer_module, "get_dynamic_threshold", return_value=1000.0):
            result = self._score(symbol, side="long")

        self.assertEqual(result.signal_type, "hold")
        self.assertEqual(scorer_module._SIGNAL_CONFIRM_BUFFER[(symbol, "long")]["count"], 0)


if __name__ == "__main__":
    unittest.main()
