"""
engine/test_risk_base_halt_resume_event_bus.py -- Test untuk publish
event "halt_changed" dari BaseRiskManager.halt_trading()/_resume()
(audit item #8, langkah 4/4 sebagian -- halt/resume transition).

[LATAR BELAKANG] halt_trading()/resume_trading()/_resume() dipanggil dari
BANYAK titik berbeda (manual API /api/bot/halt, panic-close, DAN otomatis
saat breach drawdown/daily-loss/low-balance di update_portfolio_state()).
Semua jalur OTOMATIS dikonfirmasi sudah guard `and not self._halted`
sebelum manggil halt_trading() (dan sebaliknya utk _resume()) -- artinya
method ini SELALU genuinely dipanggil pas transisi, tidak pernah berulang
tiap cycle. Publish di SATU titik (_publish_halt_event(), dipanggil dari
halt_trading() & _resume()) otomatis cover SEMUA caller tanpa wiring
terpisah per titik panggil.

self._db.event_bus dipakai (BUKAN atribut event_bus terpisah di
RiskManager) krn self._db SELALU instance DatabaseManager yang SAMA dgn
milik bot (RiskManager(..., db=self.db) di kedua bot).

    python3 -m unittest engine.test_risk_base_halt_resume_event_bus -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from engine.event_bus import EventBus
from engine.risk_base import BaseRiskManager, HaltReason


async def _noop_set_bot_state(*a, **kw):
    return None


async def _noop_clear_bot_state(*a, **kw):
    return None


def _make_risk_manager(event_bus=None):
    fake_db = SimpleNamespace(
        event_bus=event_bus,
        set_bot_state=_noop_set_bot_state,
        clear_bot_state=_noop_clear_bot_state,
    )
    rm = BaseRiskManager({}, db=fake_db)
    return rm


class TestHaltPublishesEvent(unittest.TestCase):

    def test_halt_trading_publishes_halt_changed_true(self):
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            rm = _make_risk_manager(event_bus=bus)

            rm.halt_trading(HaltReason.MAX_DRAWDOWN, "drawdown 20% >= limit 15%")

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "halt_changed")
            self.assertTrue(event.data["halted"])
            self.assertEqual(event.data["reason"], "max_drawdown_breached")
            self.assertIn("drawdown", event.data["detail"])

        asyncio.run(scenario())

    def test_resume_publishes_halt_changed_false(self):
        async def scenario():
            bus = EventBus()
            rm = _make_risk_manager(event_bus=bus)
            rm.halt_trading(HaltReason.MANUAL, "manual test")
            sub = bus.subscribe()  # subscribe SETELAH halt, biar queue bersih

            rm._resume()

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "halt_changed")
            self.assertFalse(event.data["halted"])

        asyncio.run(scenario())

    def test_resume_trading_blocked_for_max_drawdown_does_not_publish(self):
        """[Regresi kunci] resume_trading() (jalur API manual) MENOLAK
        resume dari MAX_DRAWDOWN -- tidak boleh publish event resume krn
        state genuinely TIDAK berubah."""
        async def scenario():
            bus = EventBus()
            rm = _make_risk_manager(event_bus=bus)
            rm.halt_trading(HaltReason.MAX_DRAWDOWN, "breach")
            sub = bus.subscribe()

            rm.resume_trading()  # ditolak internal, TIDAK memanggil _resume()

            self.assertTrue(sub.queue.empty(), "resume yang ditolak tidak boleh publish event")
            self.assertTrue(rm.is_halted)

        asyncio.run(scenario())

    def test_no_event_bus_on_db_does_not_crash(self):
        """[Non-regresi] db tanpa atribut event_bus (mis. Mock lama/objek
        lain) TIDAK boleh crash halt_trading()."""
        fake_db = SimpleNamespace(
            set_bot_state=_noop_set_bot_state, clear_bot_state=_noop_clear_bot_state,
        )  # nol event_bus
        rm = BaseRiskManager({}, db=fake_db)
        rm.halt_trading(HaltReason.MANUAL, "test")  # tidak boleh raise AttributeError

    def test_db_none_does_not_crash(self):
        rm = BaseRiskManager({}, db=None)
        rm.halt_trading(HaltReason.MANUAL, "test")  # tidak boleh raise

    def test_automatic_drawdown_breach_via_update_portfolio_state_publishes(self):
        """[Integrasi -- jalur otomatis SUNGGUHAN, bukan panggil
        halt_trading() langsung] update_portfolio_state() breach drawdown
        HARUS ikut memicu publish lewat jalur yang sama."""
        async def scenario():
            bus = EventBus()
            sub = bus.subscribe()
            rm = _make_risk_manager(event_bus=bus)
            rm._max_drawdown_pct = 10.0

            rm.update_portfolio_state(
                equity=100.0, initial_equity=100.0,
                free_balance=100.0, open_positions_count=0,
            )
            rm.update_portfolio_state(
                equity=85.0, initial_equity=100.0,  # drawdown 15% >= limit 10%
                free_balance=85.0, open_positions_count=0,
            )

            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(event.type, "halt_changed")
            self.assertTrue(event.data["halted"])
            self.assertEqual(event.data["reason"], "max_drawdown_breached")

        asyncio.run(scenario())

    def test_repeated_breach_checks_while_already_halted_does_not_republish(self):
        """[Regresi kunci] update_portfolio_state() dipanggil BERULANG
        (tiap refresh cycle) sementara SUDAH halted -- guard `not
        self._halted` yang sudah ada HARUS mencegah publish berulang."""
        async def scenario():
            bus = EventBus()
            rm = _make_risk_manager(event_bus=bus)
            rm._max_drawdown_pct = 10.0
            rm.update_portfolio_state(equity=100.0, initial_equity=100.0,
                                       free_balance=100.0, open_positions_count=0)
            rm.update_portfolio_state(equity=85.0, initial_equity=100.0,
                                       free_balance=85.0, open_positions_count=0)
            sub = bus.subscribe()

            for _ in range(5):
                rm.update_portfolio_state(equity=80.0, initial_equity=100.0,
                                           free_balance=80.0, open_positions_count=0)

            self.assertTrue(sub.queue.empty(), "sudah halted -- tidak boleh publish lagi tiap cycle")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
