"""
future/test_main_future_event_bus_wiring.py -- Test untuk wiring EventBus &
ThrottledTickerPublisher di future/main_future.py (audit item #8, langkah 3/4).

Pola sama persis dgn spot/test_main_spot_event_bus_wiring.py -- futures
cuma punya SATU titik instansiasi WebSocketFeed (dikonfirmasi grep, tidak
ada reinit terpisah spt config_watcher spot).

    python3 -m unittest future.test_main_future_event_bus_wiring -v
"""

from __future__ import annotations

import inspect
import unittest

from engine.event_bus import EventBus
from future.main_future import TradingBot


class TestEventBusAttributePresent(unittest.TestCase):

    def test_bot_has_event_bus_instance_after_construction(self):
        bot = TradingBot()
        self.assertIsInstance(bot.event_bus, EventBus)

    def test_event_bus_is_fresh_instance_per_bot_not_shared(self):
        bot_a = TradingBot()
        bot_b = TradingBot()
        self.assertIsNot(bot_a.event_bus, bot_b.event_bus)

    def test_event_bus_not_shared_with_spot_module(self):
        """[Regresi kunci -- prasyarat desain #8] Bus futures HARUS
        instance TERPISAH dari bus spot -- 2 proses, 2 bus, tidak boleh
        ada state global yang kebetulan dishare lintas modul."""
        from spot.main_spot import TradingBot as SpotTradingBot
        futures_bot = TradingBot()
        spot_bot    = SpotTradingBot()
        self.assertIsNot(futures_bot.event_bus, spot_bot.event_bus)


class TestStartWiringInSource(unittest.TestCase):

    def test_db_event_bus_assigned_in_start(self):
        src = inspect.getsource(TradingBot.start)
        self.assertIn(
            "self.db.event_bus = self.event_bus", src,
            "start() harus assign self.db.event_bus = self.event_bus setelah "
            "DatabaseManager dikonstruksi.",
        )

    def test_ws_feed_on_ticker_wired_in_start(self):
        src = inspect.getsource(TradingBot.start)
        self.assertIn(
            'on_ticker=ThrottledTickerPublisher(self.event_bus, market_type="futures")', src,
            "WebSocketFeed(...) di start() harus dikirimi on_ticker= dgn market_type='futures'.",
        )

    def test_default_type_future_still_present_after_wiring(self):
        """[Non-regresi] Penambahan on_ticker TIDAK boleh menghilangkan
        default_type='future' -- itu fix terpisah (item lama) yang
        mencegah feed ticker/orderbook diam-diam query market SPOT."""
        src = inspect.getsource(TradingBot.start)
        self.assertIn('default_type="future"', src)


if __name__ == "__main__":
    unittest.main()
