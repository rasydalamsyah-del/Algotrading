"""
spot/test_main_spot_event_bus_wiring.py -- Test untuk wiring EventBus &
ThrottledTickerPublisher di spot/main_spot.py (audit item #8, langkah 3/4).

Full start() terlalu berat utk di-test langsung (exchange connect, DB
real, dst) -- test ini verifikasi via inspect.getsource() bahwa titik
wiring genuinely ada di source (pola sama dgn
test_scorer_suggest_sl_tp_side_aware.py::test_score_signal_source_passes_side_to_suggest_sl_tp
di sesi sebelumnya), PLUS test langsung ke TradingBot() (construction
ringan, tidak butuh I/O) utk atribut event_bus.

    python3 -m unittest spot.test_main_spot_event_bus_wiring -v
"""

from __future__ import annotations

import inspect
import unittest

from engine.event_bus import EventBus
from spot.main_spot import TradingBot


class TestEventBusAttributePresent(unittest.TestCase):

    def test_bot_has_event_bus_instance_after_construction(self):
        bot = TradingBot()
        self.assertIsInstance(bot.event_bus, EventBus)

    def test_event_bus_is_fresh_instance_per_bot_not_shared(self):
        """[Regresi kunci] 2 instance TradingBot() TIDAK boleh berbagi
        EventBus yang sama -- tiap proses/instance harus punya bus sendiri."""
        bot_a = TradingBot()
        bot_b = TradingBot()
        self.assertIsNot(bot_a.event_bus, bot_b.event_bus)


class TestStartWiringInSource(unittest.TestCase):
    """[Pola sama dgn audit item #19] Verifikasi source code genuinely
    menyambungkan event_bus ke db & ws_feed -- bukan cuma atributnya ada
    tapi tidak pernah dipakai di jalur start()."""

    def test_db_event_bus_assigned_in_start(self):
        src = inspect.getsource(TradingBot.start)
        self.assertIn(
            "self.db.event_bus = self.event_bus", src,
            "start() harus assign self.db.event_bus = self.event_bus setelah "
            "DatabaseManager dikonstruksi -- kalau tidak, Tier 1 publish di "
            "database.py tidak akan pernah terpicu.",
        )

    def test_ws_feed_on_ticker_wired_in_start(self):
        src = inspect.getsource(TradingBot.start)
        self.assertIn(
            'on_ticker=ThrottledTickerPublisher(self.event_bus, market_type="spot")', src,
            "WebSocketFeed(...) di start() harus dikirimi on_ticker= -- "
            "hook itu ada sejak awal di WebSocketFeed tapi mati kalau tidak disambungkan.",
        )

    def test_ws_feed_reinit_in_config_watcher_also_wires_on_ticker(self):
        """[Regresi kunci] Reinit WebSocketFeed di run_config_watcher()
        (dipicu hot-reload config/credential) HARUS ikut menyambungkan
        ulang on_ticker -- kalau tidak, tick stream mati diam-diam setelah
        reinit tanpa error apa pun."""
        src = inspect.getsource(TradingBot.run_config_watcher)
        self.assertIn(
            'on_ticker=ThrottledTickerPublisher(self.event_bus, market_type="spot")', src,
            "Reinit WebSocketFeed di config watcher harus tetap kirim on_ticker=.",
        )


if __name__ == "__main__":
    unittest.main()
