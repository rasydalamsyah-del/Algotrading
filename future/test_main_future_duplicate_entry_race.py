"""
future/test_main_future_duplicate_entry_race.py -- Test untuk re-check
get_open_position_by_symbol() tepat sebelum execute_signal() di
TradingBot._handle_entry() (future/main_future.py), yang menutup race
duplikat-entry ANTAR-DUA-JALUR (bukan intra-jalur seperti item #2).

[ITEM #1 -- audit fungsional] Dua jalur independen bisa memanggil
_handle_entry() untuk SIMBOL YANG SAMA tanpa saling tahu:
  - Jalur A: run_gate3_worker() -> _process_one() -> _handle_entry()
  - Jalur B: capital_allocator.reconcile_pending() -> _handle_entry()
Keduanya mengambil snapshot db.get_open_positions() SENDIRI-SENDIRI sebelum
gate G0_ALREADY_OPEN (engine/intelligence/commander.py) -- bisa sama-sama
lolos utk simbol yang sama sebelum salah satu commit upsert_position().
Window race mencakup set_leverage()+evaluate_order()+execute_signal() --
semuanya network I/O, bukan instan.

Fix: re-check db.get_open_position_by_symbol(symbol) TEPAT SEBELUM
execute_signal() (bukan sebelum upsert_position() -- itu sudah terlambat,
order NYATA sudah tereksekusi di exchange di titik itu), DI DALAM
_equity_lock yang SUDAH dipegang KEDUA jalur (keduanya funnel ke
_handle_entry() yang sama) -- tidak perlu lock baru/koordinasi eksplisit
antara run_gate3_worker & capital_allocator.

File ini menguji: (1) 2 panggilan "bersamaan" (asyncio.gather) ke
_handle_entry() UNTUK SIMBOL YANG SAMA -- simulasi Jalur A & Jalur B
"bertemu" -- hanya SATU yang genuinely execute_signal() (order nyata),
HANYA SATU db row. (2) sinergi eksplisit dgn release item #2: reservasi
_open_positions_count milik "pecundang" race harus ter-release lewat
finally yang SUDAH ADA dari item #2, TANPA kode tambahan.

    python3 -m unittest future.test_main_future_duplicate_entry_race -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.core.models import SignalEvent, SignalType
from future.main_future import TradingBot
from future.risk_future import RiskManager


def _make_signal(symbol="RACE/USDT", price=10.0):
    return SignalEvent(
        symbol=symbol, signal_type=SignalType.BUY, price=price,
        timestamp=datetime.now(timezone.utc), strategy="test_strategy",
        stop_loss=None, take_profit=None, metadata={},
    )


class _FakeDBWithRealState:
    """DB fake yang genuinely melacak state open-position per simbol --
    BUKAN AsyncMock statis -- supaya re-check di dalam _equity_lock benar-
    benar melihat hasil upsert_position() dari pemanggil sebelumnya, persis
    seperti SQLite riil."""

    def __init__(self):
        self._positions = {}

    async def get_open_position_by_symbol(self, symbol):
        return self._positions.get(symbol)

    async def upsert_position(self, symbol, data):
        self._positions[symbol] = SimpleNamespace(symbol=symbol, **data)


def _build_fake_self(risk_manager, db, execute_signal_call_log):
    fake_self = SimpleNamespace()
    fake_self.portfolio_state = {"total_equity": 10000.0}
    fake_self.config = {
        "max_position_size_pct": 10.0,
        "default_leverage": 5,
        "adaptive_leverage_enabled": False,
        "margin_mode": "isolated",
        "maintenance_margin_rate": 0.005,
        "timeframe": "15m",
    }
    fake_self.risk_manager = risk_manager
    fake_self._equity_lock = asyncio.Lock()
    fake_self.strategy = None
    fake_self.exchange = SimpleNamespace(set_leverage=AsyncMock())
    fake_self._pending_candidates = {}
    fake_self.db = db
    fake_self.notifier = SimpleNamespace(notify_trade_opened=AsyncMock())
    fake_self._refresh_portfolio = AsyncMock()

    async def _execute_signal(signal, assessment):
        # [KUNCI SIMULASI] execute_signal() = order NYATA di exchange --
        # inilah yang TIDAK BOLEH terpanggil dobel utk simbol yang sama.
        execute_signal_call_log.append(signal.symbol)
        return SimpleNamespace(
            filled=10.0, amount=10.0, executed_price=10.0, order_id="ORDER1",
        )

    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(side_effect=_execute_signal))
    return fake_self


class TestDuplicateEntryRaceAcrossTwoPaths(unittest.TestCase):

    def _approved_rm(self, max_open=5):
        """max_open sengaja BESAR (5) supaya gate agregat item #2 TIDAK
        jadi faktor pembatas -- yang harus menutup race di sini murni
        re-check per-simbol (item #1), bukan kebetulan kehabisan slot."""
        rm = RiskManager({"max_open_positions": max_open})
        rm.update_portfolio_state(
            equity=10000.0, initial_equity=10000.0,
            free_balance=10000.0, open_positions_count=0,
        )
        return rm

    def test_two_concurrent_handle_entry_same_symbol_only_one_execute_signal(self):
        """[REGRESI UTAMA, skenario persis dari audit] Jalur A & Jalur B
        "bertemu" di simbol yang sama -- dipanggil BERSAMAAN via
        asyncio.gather() pada _handle_entry() yang SAMA (persis situasi
        nyata: run_gate3_worker & capital_allocator sama-sama panggil
        method ini di instance bot yang sama). HANYA SATU execute_signal()
        (order nyata) yang boleh terjadi -- bukan cuma satu DB row."""
        rm = self._approved_rm()
        db = _FakeDBWithRealState()
        exec_log = []
        fake_self = _build_fake_self(rm, db, exec_log)

        signal_a = _make_signal(symbol="RACE/USDT")
        signal_b = _make_signal(symbol="RACE/USDT")

        async def _run():
            return await asyncio.gather(
                TradingBot._handle_entry(fake_self, signal_a, "long"),
                TradingBot._handle_entry(fake_self, signal_b, "long"),
            )

        with self.assertLogs("main_future", level="WARNING") as cm:
            asyncio.run(_run())

        self.assertEqual(
            len(exec_log), 1,
            "execute_signal() (order NYATA di exchange) HARUS terpanggil "
            "TEPAT SEKALI walau 2 jalur entry 'bertemu' di simbol yang sama.",
        )
        self.assertEqual(len(db._positions), 1)
        self.assertIn("RACE/USDT", db._positions)
        joined = "\n".join(cm.output)
        self.assertIn("dibatalkan", joined)
        self.assertIn("race dua jalur entry", joined)

    def test_release_synergy_with_item2_loser_reservation_freed(self):
        """[Sinergi eksplisit dgn item #2, DIMINTA secara eksplisit --
        BUKAN diasumsikan] Setelah race, _open_positions_count HARUS
        PERSIS 1 -- bukan 2. Reservasi milik pemanggil yang KALAH (dibatalkan
        oleh re-check item #1) harus ter-release lewat finally
        _slot_reserved_pending_release yang SUDAH ADA dari item #2, TANPA
        kode tambahan apapun ditulis khusus utk item #1."""
        rm = self._approved_rm()
        db = _FakeDBWithRealState()
        exec_log = []
        fake_self = _build_fake_self(rm, db, exec_log)

        signal_a = _make_signal(symbol="RACE/USDT")
        signal_b = _make_signal(symbol="RACE/USDT")

        async def _run():
            return await asyncio.gather(
                TradingBot._handle_entry(fake_self, signal_a, "long"),
                TradingBot._handle_entry(fake_self, signal_b, "long"),
            )

        asyncio.run(_run())

        self.assertEqual(
            rm._open_positions_count, 1,
            "HARUS persis 1 -- reservasi pemenang tetap ada, reservasi "
            "pecundang HARUS ter-release otomatis lewat finally item #2 "
            "(sinergi lintas-item, bukan kode baru).",
        )

    def test_three_way_collision_still_only_one_winner(self):
        """[Sanity tambahan] Bukan cuma 2 -- 3 pemanggil 'bersamaan' utk
        simbol yang sama, tetap cuma 1 yang menang."""
        rm = self._approved_rm()
        db = _FakeDBWithRealState()
        exec_log = []
        fake_self = _build_fake_self(rm, db, exec_log)

        signals = [_make_signal(symbol="RACE/USDT") for _ in range(3)]

        async def _run():
            return await asyncio.gather(*[
                TradingBot._handle_entry(fake_self, s, "long") for s in signals
            ])

        asyncio.run(_run())

        self.assertEqual(len(exec_log), 1)
        self.assertEqual(rm._open_positions_count, 1)

    def test_different_symbols_both_proceed_no_false_positive_block(self):
        """[Negative check] Race-guard TIDAK BOLEH salah blokir simbol
        BERBEDA yang kebetulan diproses bersamaan -- re-check di-keyed per
        simbol (get_open_position_by_symbol), bukan global."""
        rm = self._approved_rm()
        db = _FakeDBWithRealState()
        exec_log = []
        fake_self = _build_fake_self(rm, db, exec_log)

        signal_a = _make_signal(symbol="ALPHA/USDT")
        signal_b = _make_signal(symbol="BETA/USDT")

        async def _run():
            return await asyncio.gather(
                TradingBot._handle_entry(fake_self, signal_a, "long"),
                TradingBot._handle_entry(fake_self, signal_b, "long"),
            )

        asyncio.run(_run())

        self.assertEqual(len(exec_log), 2)
        self.assertEqual(set(db._positions.keys()), {"ALPHA/USDT", "BETA/USDT"})
        self.assertEqual(rm._open_positions_count, 2)

    def test_sequential_second_call_after_first_commits_is_also_blocked(self):
        """[Kasus non-race, murni sequential] Kalau simbol SUDAH punya
        posisi terbuka (bukan krn race, tapi genuinely sudah ada) --
        _handle_entry() harus tetap membatalkan, bukan cuma menutup skenario
        race yang persis simultan."""
        rm = self._approved_rm()
        db = _FakeDBWithRealState()
        exec_log = []
        fake_self = _build_fake_self(rm, db, exec_log)

        asyncio.run(TradingBot._handle_entry(fake_self, _make_signal(symbol="SEQ/USDT"), "long"))
        self.assertEqual(len(exec_log), 1)

        with self.assertLogs("main_future", level="WARNING") as cm:
            asyncio.run(TradingBot._handle_entry(fake_self, _make_signal(symbol="SEQ/USDT"), "long"))

        self.assertEqual(len(exec_log), 1, "Panggilan kedua tidak boleh execute_signal() lagi")
        self.assertIn("dibatalkan", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
