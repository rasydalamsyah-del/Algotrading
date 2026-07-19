"""
future/test_main_future_gate4_score_reject_log_throttle.py -- Test untuk
bug-fix #23: Gate 4 ([ScoreThreshold]) di future/main_future.py sebelumnya
cuma log.debug() -- sama persis gap-nya dgn spot (lihat
spot/test_main_spot_gate4_score_reject_log_throttle.py utk latar belakang
lengkap: kenapa BUKAN INFO polos, kenapa rate-limit per key yang dipilih).

[BEDA STRUKTURAL dari spot, WAJIB diuji terpisah] futures bisa evaluasi
DUA sisi (long & short) utk simbol yang sama dlm satu siklus
(`for cand_side in candidate_sides:`) -- kalau throttle di-key symbol
SAJA (spt spot), reject long akan diam-diam menahan visibilitas reject
short utk simbol yang sama (silent cross-side contamination) -- kelas bug
IDENTIK dgn item #25 (_SIGNAL_CONFIRM_BUFFER). Fix di sini pakai key
tuple (symbol, cand_side), pola sama dgn `self._invalidation_signals.get(
(symbol, cand_side))` yang SUDAH established di file yang sama.

    python3 -m unittest future.test_main_future_gate4_score_reject_log_throttle -v
"""

from __future__ import annotations

import inspect
import os
import unittest

from engine.event_bus import KeyedLogThrottle
from future.main_future import TradingBot


class TestGate4ThrottleAttributePresent(unittest.TestCase):

    def test_bot_has_gate4_reject_log_throttle_instance(self):
        bot = TradingBot()
        self.assertIsInstance(bot._gate4_reject_log_throttle, KeyedLogThrottle)

    def test_throttle_interval_matches_config_default(self):
        bot = TradingBot()
        self.assertEqual(bot._gate4_reject_log_throttle._interval, 600.0)

    def test_throttle_interval_respects_env_override(self):
        old = os.environ.get("GATE4_SCORE_REJECT_LOG_INTERVAL")
        os.environ["GATE4_SCORE_REJECT_LOG_INTERVAL"] = "120"
        try:
            bot = TradingBot()
            self.assertEqual(bot.config["gate4_score_reject_log_interval"], 120)
            self.assertEqual(bot._gate4_reject_log_throttle._interval, 120.0)
        finally:
            if old is None:
                os.environ.pop("GATE4_SCORE_REJECT_LOG_INTERVAL", None)
            else:
                os.environ["GATE4_SCORE_REJECT_LOG_INTERVAL"] = old

    def test_shared_env_key_not_futures_suffixed(self):
        """[Keputusan desain, diverifikasi eksplisit] Key env TIDAK pakai
        suffix _FUTURES -- ini murni knob observability (pola sama dgn
        MARKET_CACHE_REFRESH_INTERVAL), bukan feature toggle yang butuh
        independen per bot (beda dari META_LEARNER_ENABLED_FUTURES dkk)."""
        old = os.environ.get("GATE4_SCORE_REJECT_LOG_INTERVAL")
        os.environ["GATE4_SCORE_REJECT_LOG_INTERVAL"] = "999"
        try:
            bot = TradingBot()
            self.assertEqual(bot.config["gate4_score_reject_log_interval"], 999)
        finally:
            if old is None:
                os.environ.pop("GATE4_SCORE_REJECT_LOG_INTERVAL", None)
            else:
                os.environ["GATE4_SCORE_REJECT_LOG_INTERVAL"] = old

    def test_fresh_throttle_instance_per_bot_not_shared(self):
        bot_a = TradingBot()
        bot_b = TradingBot()
        self.assertIsNot(bot_a._gate4_reject_log_throttle, bot_b._gate4_reject_log_throttle)


class TestGate4ThrottleWiredInSourceWithSideKey(unittest.TestCase):
    """Verifikasi source code genuinely memanggil throttle.allow() dgn key
    TUPLE (symbol, cand_side) -- BUKAN symbol saja spt spot -- sebelum
    log.info() throttled di titik [ScoreThreshold]."""

    def test_run_gate3_worker_calls_throttle_allow_with_symbol_side_tuple_key(self):
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'if self._gate4_reject_log_throttle.allow((symbol, cand_side)):', src,
            "log.info() throttled Gate4 (futures) harus digate oleh "
            "self._gate4_reject_log_throttle.allow((symbol, cand_side)) -- "
            "key TUPLE (symbol, side), BUKAN symbol saja -- kalau symbol "
            "saja, reject long akan menahan visibilitas reject short utk "
            "simbol yang sama (cross-side contamination, kelas bug sama "
            "persis dgn item #25).",
        )

    def test_run_gate3_worker_still_has_debug_log_unchanged(self):
        """[Non-regresi] log.debug() detail penuh TIDAK boleh hilang/berubah."""
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'log.debug("[ScoreThreshold] %s (%s) skor %.1f < threshold %.1f — skip",', src,
        )

    def test_throttled_info_log_uses_scorethreshold_tag_with_side(self):
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'log.info("[ScoreThreshold] %s (%s) skor %.1f < threshold %.1f — skip",', src,
        )


if __name__ == "__main__":
    unittest.main()
