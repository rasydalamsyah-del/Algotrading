"""
spot/test_main_spot_gate4_score_reject_log_throttle.py -- Test untuk
bug-fix #23: Gate 4 ([ScoreThreshold]) di spot/main_spot.py sebelumnya
cuma log.debug() -- invisible tanpa DEBUG logging, padahal Gate4 adalah
titik reject volume TERTINGGI di pipeline (semua Gate3-survivor masuk
sini), BEDA dari Gate4.5 (Commander)/Gate5 (Risk final) yang sudah
log.info() polos -- membedah Gate4 ke INFO polos langsung berisiko
membanjiri log produksi (dianalisis eksplisit sebelum implementasi, opsi
dipilih user: rate-limit per key, BUKAN INFO polos/agregasi periodik).

Fix: KeyedLogThrottle (engine/event_bus.py, pola SAMA dgn
ThrottledTickerPublisher item #8) -- log.info() throttled MAKS 1x per
symbol per `gate4_score_reject_log_interval` detik (default 600 = 10 menit,
konfigurable via env GATE4_SCORE_REJECT_LOG_INTERVAL, pola sama dgn
ANALYTICS_REFRESH_INTERVAL/MARKET_CACHE_REFRESH_INTERVAL). log.debug()
detail penuh TIDAK diubah/dihapus -- ini murni TAMBAHAN.

Full run_gate3_worker()/_process_one() terlalu berat utk ditest langsung
(exchange connect, DB real, strategy scoring) -- pola sama dgn
test_main_spot_event_bus_wiring.py: verifikasi construction (throttle
attribute ada, dibangun dari config yang benar) + inspect.getsource() utk
membuktikan titik wiring genuinely ada di source, PLUS unit test perilaku
KeyedLogThrottle itu sendiri ada terpisah di engine/test_event_bus.py.

    python3 -m unittest spot.test_main_spot_gate4_score_reject_log_throttle -v
"""

from __future__ import annotations

import inspect
import os
import unittest

from engine.event_bus import KeyedLogThrottle
from spot.main_spot import TradingBot


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

    def test_fresh_throttle_instance_per_bot_not_shared(self):
        """[Non-regresi -- pola sama dgn event_bus] 2 instance TradingBot()
        TIDAK boleh berbagi throttle state yang sama."""
        bot_a = TradingBot()
        bot_b = TradingBot()
        self.assertIsNot(bot_a._gate4_reject_log_throttle, bot_b._gate4_reject_log_throttle)


class TestGate4ThrottleWiredInSource(unittest.TestCase):
    """Verifikasi source code genuinely memanggil throttle.allow() sebelum
    log.info() throttled di titik [ScoreThreshold] -- bukan cuma atribut
    throttle ada tapi tidak pernah dipakai (pola sama dgn audit item #19)."""

    def test_run_gate3_worker_calls_throttle_allow_with_symbol_key(self):
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'if self._gate4_reject_log_throttle.allow(symbol):', src,
            "log.info() throttled Gate4 harus digate oleh "
            "self._gate4_reject_log_throttle.allow(symbol) -- key symbol saja "
            "(spot tidak punya konsep side, beda dari futures).",
        )

    def test_run_gate3_worker_still_has_debug_log_unchanged(self):
        """[Non-regresi] log.debug() detail penuh TIDAK boleh hilang/berubah
        -- fix ini murni aditif (INFO throttled tambahan), bukan pengganti."""
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'log.debug("[ScoreThreshold] %s skor %.1f < threshold %.1f — skip",', src,
        )

    def test_throttled_info_log_uses_scorethreshold_tag(self):
        src = inspect.getsource(TradingBot.run_gate3_worker)
        self.assertIn(
            'log.info("[ScoreThreshold] %s skor %.1f < threshold %.1f — skip",', src,
        )


if __name__ == "__main__":
    unittest.main()
