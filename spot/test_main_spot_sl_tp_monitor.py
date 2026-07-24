"""
spot/test_main_spot_sl_tp_monitor.py -- Test untuk isolasi per-posisi di
TradingBot.run_sl_tp_monitor() (spot/main_spot.py, ~baris 1912-2336).

[ITEM #3 -- audit fungsional] Mirror persis dari future/test_main_future_sl_tp_monitor.py
-- lihat docstring di sana untuk latar belakang lengkap. Spot punya struktur
sedikit berbeda (blok ATG/Early Exit/Regime Transition sudah py-wrapped
sejak awal, dan ada 2 titik pemanggilan _close_position_market() terpisah
untuk trailing_reason vs hit_sl/hit_tp, digabung dalam SATU try/except baru
di sini) tapi celah initinya identik: check_breakeven_sl(), check_trailing_sl(),
db.update_position_sl(), dan _close_position_market() TIDAK dibungkus
sebelum fix ini.

    python3 -m unittest spot.test_main_spot_sl_tp_monitor -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.risk_base import BaseRiskManager
from spot.main_spot import TradingBot


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT",
        side="long",
        entry_price=50.0,
        stop_loss_price=45.0,
        take_profit_price=100.0,
        highest_price=50.0,
        atr_at_entry=1.0,
        strategy_profile="mean_revert",
        entry_regime="undefined",
        entry_score=None,
        amount=1.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeDB:
    def __init__(self, positions):
        self._positions = positions
        self.sl_updates = []
        self.highest_price_updates = []
        self.price_updates = []
        self.highest_price_side_effect = {}
        self.get_latest_signal_score = AsyncMock(return_value=None)

    async def get_open_positions(self):
        return self._positions

    async def update_position_sl(self, symbol, new_sl):
        self.sl_updates.append((symbol, new_sl))

    async def update_position_highest_price(self, symbol, price):
        if symbol in self.highest_price_side_effect:
            raise self.highest_price_side_effect[symbol]
        self.highest_price_updates.append((symbol, price))

    async def update_position_price(self, symbol, price, upnl, upnl_pct):
        self.price_updates.append((symbol, price, upnl, upnl_pct))


class _FakeExchange:
    def __init__(self):
        self.fetch_ohlcv = AsyncMock(return_value=[])


def _build_fake_self(positions, price_map, close_side_effect=None):
    """`self` stub minimal, pola identik test futures -- lihat docstring di
    sana. `strategy=None` supaya trailing_reason/get_profile jalur di-skip
    aman (hasattr/truthy guard sudah ada di kode produksi)."""
    fake_self = SimpleNamespace()
    # [DOUBLE-COUNT FIX] _do_close_position() kini memegang _equity_lock
    # (mirror _handle_entry) -- stub wajib menyediakannya.
    fake_self._equity_lock = asyncio.Lock()
    fake_self.is_running = True
    fake_self.SL_TP_CHECK_INTERVAL = 0
    fake_self.db = _FakeDB(positions)
    fake_self._closing_lock = asyncio.Lock()
    fake_self._closing_symbols = set()
    fake_self.risk_manager = BaseRiskManager({})
    fake_self.exchange = _FakeExchange()
    fake_self.strategy = None
    fake_self.notifier = SimpleNamespace(notify_sl_tp_hit=AsyncMock())

    async def _get_price(symbol):
        return price_map.get(symbol)
    fake_self._get_current_price = _get_price

    close_calls = []

    async def _close(pos, price, reason):
        close_calls.append((pos.symbol, price, reason))
        if close_side_effect and pos.symbol in close_side_effect:
            raise close_side_effect[pos.symbol]
    fake_self._close_position_market = _close
    fake_self._close_calls = close_calls

    orig_get_positions = fake_self.db.get_open_positions

    async def _get_positions_and_stop():
        fake_self.is_running = False
        return await orig_get_positions()
    fake_self.db.get_open_positions = _get_positions_and_stop

    return fake_self


class TestSlTpMonitorPerPositionIsolation(unittest.TestCase):

    def test_poison_pill_corrupt_entry_price_still_gets_hit_sl_protection(self):
        """[Skenario persis dari audit] entry_price korup non-numerik --
        check_breakeven_sl() genuinely melempar TypeError asli dari
        BaseRiskManager.

        [Beda dgn futures, ditemukan lewat test ini] Di spot, blok close
        SENDIRI juga menghitung est_pnl dari pos.entry_price SEBELUM
        memanggil _close_position_market() (utk notify_sl_tp_hit) -- jadi
        entry_price korup membuat langkah close utk POSISI INI JUGA gagal
        (bukan cuma breakeven), BUKAN bug baru, itu genuinely butuh
        entry_price valid utk estimasi PnL. Yang dibuktikan di sini: kedua
        kegagalan tertangkap rapi (masing-masing log ERROR-nya sendiri),
        TIDAK ada exception yang lolos ke luar siklus (asyncio.run selesai
        normal, tidak crash)."""
        pos = _make_position(
            symbol="POISON/USDT", entry_price="corrupt",
            stop_loss_price=100.0, take_profit_price=150.0,
            highest_price=100.0, atr_at_entry=1.0,
        )
        fake_self = _build_fake_self([pos], {"POISON/USDT": 90.0})  # <= SL 100 -> hit_sl

        with self.assertLogs("main", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))  # tidak boleh raise

        joined = "\n".join(cm.output)
        self.assertIn("Breakeven SL check gagal", joined)
        self.assertIn("Close posisi gagal", joined)

    def test_poison_pill_does_not_block_next_position_in_same_cycle(self):
        """[REGRESI UTAMA] Posisi pertama gagal di langkah yang SENGAJA
        TIDAK dibungkus inner try/except (update_position_highest_price --
        mengandalkan backstop OUTER) -- posisi kedua harus tetap diproses
        & di-close di siklus yang sama."""
        pos1 = _make_position(symbol="POISON/USDT", stop_loss_price=45.0, highest_price=10.0)
        pos2 = _make_position(symbol="GOOD/USDT", stop_loss_price=45.0, highest_price=10.0)
        fake_self = _build_fake_self(
            [pos1, pos2],
            {"POISON/USDT": 40.0, "GOOD/USDT": 40.0},
        )
        fake_self.db.highest_price_side_effect = {
            "POISON/USDT": RuntimeError("simulated DB corruption")
        }

        with self.assertLogs("main", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        closed_symbols = [c[0] for c in fake_self._close_calls]
        self.assertIn(
            "GOOD/USDT", closed_symbols,
            "Posisi kedua HARUS tetap diproses walau posisi pertama gagal "
            "di langkah yang tidak dibungkus inner try/except",
        )
        joined = "\n".join(cm.output)
        self.assertIn("gagal proses posisi POISON/USDT", joined)

    def test_close_position_market_failure_isolated_from_other_positions(self):
        """['titik PALING kritis' dari audit] _close_position_market() gagal
        untuk posisi pertama -- posisi kedua HARUS tetap di-attempt close."""
        pos1 = _make_position(symbol="FAIL_CLOSE/USDT", stop_loss_price=45.0)
        pos2 = _make_position(symbol="OK_CLOSE/USDT", stop_loss_price=45.0)
        fake_self = _build_fake_self(
            [pos1, pos2],
            {"FAIL_CLOSE/USDT": 40.0, "OK_CLOSE/USDT": 40.0},
            close_side_effect={"FAIL_CLOSE/USDT": RuntimeError("exchange order gagal")},
        )

        with self.assertLogs("main", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        closed_symbols = [c[0] for c in fake_self._close_calls]
        self.assertIn("FAIL_CLOSE/USDT", closed_symbols, "close TETAP di-attempt (lalu gagal)")
        self.assertIn("OK_CLOSE/USDT", closed_symbols, "posisi kedua tidak boleh ikut batal")
        joined = "\n".join(cm.output)
        self.assertIn("Close posisi gagal", joined)

    def test_trailing_step_failure_does_not_block_hit_sl_same_position(self):
        """Isolasi khusus langkah trailing -- risk_manager.check_trailing_sl()
        di-inject supaya raise langsung (bukan lewat korupsi entry_price/
        stop_loss_price, krn keduanya dipakai juga di hit_sl/close -- lihat
        catatan di test poison-pill di atas). entry_price & stop_loss_price
        TETAP valid -- membuktikan hit_sl & close (yang butuh keduanya)
        tetap berjalan normal utk posisi yang sama walau trailing gagal."""
        class _TrailingCrashRiskManager(BaseRiskManager):
            def check_trailing_sl(self, *a, **kw):
                raise RuntimeError("simulated trailing failure")

        pos = _make_position(
            symbol="TRAIL_POISON/USDT",
            take_profit_price=None,  # breakeven aman (guard all([...]) -> None, no-op)
            stop_loss_price=100.0, atr_at_entry=1.0,
        )
        fake_self = _build_fake_self([pos], {"TRAIL_POISON/USDT": 90.0})
        fake_self.risk_manager = _TrailingCrashRiskManager({})

        with self.assertLogs("main", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        self.assertEqual(len(fake_self._close_calls), 1)
        self.assertEqual(fake_self._close_calls[0][0], "TRAIL_POISON/USDT")
        joined = "\n".join(cm.output)
        self.assertIn("Trailing SL check gagal", joined)

    def test_cancelled_error_not_treated_as_poison_pill(self):
        """asyncio.CancelledError HARUS tetap menghentikan loop via
        `except asyncio.CancelledError: break` yang SUDAH ADA di level
        while -- BUKAN tertangkap oleh except Exception baru."""
        pos = _make_position(symbol="CANCEL/USDT")
        fake_self = _build_fake_self([pos], {"CANCEL/USDT": 40.0})

        async def _raise_cancelled(symbol):
            raise asyncio.CancelledError()
        fake_self._get_current_price = _raise_cancelled

        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))
        self.assertEqual(fake_self._close_calls, [])


if __name__ == "__main__":
    unittest.main()
