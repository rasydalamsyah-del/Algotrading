"""
future/test_main_future_sl_tp_monitor.py -- Test untuk isolasi per-posisi di
TradingBot.run_sl_tp_monitor() (future/main_future.py, ~baris 1591-1862).

[ITEM #3 -- audit fungsional] Sebelum fix ini, SATU try/except besar
membungkus SELURUH `for pos in positions:`, bukan per-posisi. Beberapa
langkah (fetch_mark_price, ATR live, blok ATG) sudah punya try/except
sendiri, tapi check_breakeven_sl(), check_trailing_sl(),
db.update_position_sl(), dan _close_position_market() untuk
hit_sl/hit_tp/trailing_reason TIDAK dibungkus. Kalau posisi PERTAMA dalam
`positions` melempar exception di titik-titik itu (mis. data korup --
entry_price None/non-numerik), SEMUA posisi setelahnya di list itu tidak
dicek SAMA SEKALI di siklus itu -- kehilangan proteksi SL/TP tanpa batas
waktu sampai "poison pill" itu ditangani manual.

Fix (Opsi C -- disepakati): outer try/except PER POSISI (backstop, isolasi
posisi lain + posisi baru yang ditambahkan nanti) + inner try/except di
titik-titik yang sudah teridentifikasi (breakeven, trailing, close -- utk
graceful degradation, posisi yang sama tetap dapat proteksi maksimal di
siklus yang sama). Log level ERROR (bukan debug seperti pola lama di
ATG/mark_price) supaya tidak jadi silent-swallow kelas baru.

Dites lewat method UNBOUND (TradingBot.run_sl_tp_monitor(fake_self)) dengan
`self` stub minimal + BaseRiskManager SUNGGUHAN (bukan mock) supaya
skenario "entry_price korup non-numerik" genuinely melempar TypeError asli
dari engine/risk_base.py, bukan exception yang disimulasikan.

    python3 -m unittest future.test_main_future_sl_tp_monitor -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.risk_base import BaseRiskManager
from future.main_future import TradingBot


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT",
        side="long",
        entry_price=50.0,
        stop_loss_price=45.0,
        take_profit_price=100.0,
        highest_price=50.0,
        atr_at_entry=1.0,
        liquidation_price=None,
        strategy_profile="mean_revert",
        entry_regime="undefined",
        entry_score=None,
        amount=1.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeDB:
    def __init__(self, positions, stop_after_fetch=True):
        self._positions = positions
        self._stop_after_fetch = stop_after_fetch
        self.sl_updates = []
        self.highest_price_updates = []
        self.highest_price_side_effect = {}

    async def get_open_positions(self):
        return self._positions

    async def update_position_sl(self, symbol, new_sl):
        self.sl_updates.append((symbol, new_sl))

    async def update_position_highest_price(self, symbol, price):
        if symbol in self.highest_price_side_effect:
            raise self.highest_price_side_effect[symbol]
        self.highest_price_updates.append((symbol, price))


class _FakeExchange:
    def __init__(self):
        self.fetch_ohlcv = AsyncMock(return_value=[])
        self.fetch_mark_price = AsyncMock(return_value=0.0)


def _build_fake_self(positions, price_map, close_side_effect=None):
    """`self` stub minimal -- run_sl_tp_monitor() cuma menyentuh atribut yang
    di-set di sini. is_running otomatis diset False setelah get_open_positions()
    dipanggil sekali, supaya loop `while self.is_running` jalan TEPAT 1 siklus."""
    fake_self = SimpleNamespace()
    fake_self.is_running = True
    fake_self.SL_TP_CHECK_INTERVAL = 0
    fake_self.LIQUIDATION_EMERGENCY_PROXIMITY_PCT = 1.0
    fake_self.db = _FakeDB(positions)
    fake_self._closing_lock = asyncio.Lock()
    fake_self._closing_symbols = set()
    fake_self.risk_manager = BaseRiskManager({})
    fake_self.exchange = _FakeExchange()
    fake_self.strategy = None
    fake_self.notifier = None

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
        """[Skenario persis dari audit] entry_price korup non-numerik ("corrupt",
        BUKAN None) -- check_breakeven_sl()/check_trailing_sl() genuinely
        melempar TypeError asli dari BaseRiskManager. hit_sl TIDAK bergantung
        pada entry_price (cuma stop_loss_price vs current price) -- inner
        try/except (Opsi C) harus membuat posisi ini TETAP dapat proteksi
        hit_sl/close walau langkah breakeven/trailing di atasnya gagal."""
        pos = _make_position(
            symbol="POISON/USDT", entry_price="corrupt",
            stop_loss_price=100.0, take_profit_price=150.0,
            highest_price=100.0, atr_at_entry=1.0,
        )
        fake_self = _build_fake_self([pos], {"POISON/USDT": 90.0})  # <= SL 100 -> hit_sl

        with self.assertLogs("main_future", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        self.assertEqual(
            fake_self._close_calls, [("POISON/USDT", 90.0, "Stop-loss hit @ 100.000000")]
        )
        joined = "\n".join(cm.output)
        self.assertIn("Breakeven SL check gagal", joined)

    def test_poison_pill_does_not_block_next_position_in_same_cycle(self):
        """[REGRESI UTAMA -- kasus yang dilaporkan di audit] Posisi PERTAMA
        gagal di langkah yang SENGAJA TIDAK dibungkus inner try/except
        (update_position_highest_price -- mengandalkan backstop OUTER),
        posisi KEDUA harus TETAP diproses & di-close di siklus yang SAMA.
        Sebelum fix: posisi kedua tidak akan pernah dicek."""
        # highest_price DI BAWAH price baru -- supaya update_position_highest_price()
        # genuinely dipanggil (sekaligus titik kegagalan yang disimulasikan).
        pos1 = _make_position(symbol="POISON/USDT", stop_loss_price=45.0, highest_price=10.0)
        pos2 = _make_position(symbol="GOOD/USDT", stop_loss_price=45.0, highest_price=10.0)
        fake_self = _build_fake_self(
            [pos1, pos2],
            {"POISON/USDT": 40.0, "GOOD/USDT": 40.0},  # keduanya <= SL -> hit_sl
        )
        fake_self.db.highest_price_side_effect = {
            "POISON/USDT": RuntimeError("simulated DB corruption")
        }

        with self.assertLogs("main_future", level="ERROR") as cm:
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
        (mis. exchange order error) untuk posisi pertama -- posisi kedua
        HARUS tetap di-attempt close, TIDAK dibatalkan."""
        pos1 = _make_position(symbol="FAIL_CLOSE/USDT", stop_loss_price=45.0)
        pos2 = _make_position(symbol="OK_CLOSE/USDT", stop_loss_price=45.0)
        fake_self = _build_fake_self(
            [pos1, pos2],
            {"FAIL_CLOSE/USDT": 40.0, "OK_CLOSE/USDT": 40.0},
            close_side_effect={"FAIL_CLOSE/USDT": RuntimeError("exchange order gagal")},
        )

        with self.assertLogs("main_future", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        closed_symbols = [c[0] for c in fake_self._close_calls]
        self.assertIn("FAIL_CLOSE/USDT", closed_symbols, "close TETAP di-attempt (lalu gagal)")
        self.assertIn("OK_CLOSE/USDT", closed_symbols, "posisi kedua tidak boleh ikut batal")
        joined = "\n".join(cm.output)
        self.assertIn("Close posisi gagal", joined)

    def test_trailing_step_failure_does_not_block_hit_sl_same_position(self):
        """Isolasi khusus langkah trailing: take_profit_price=None supaya
        breakeven aman (guard all([...]) return None, tidak crash), tapi
        entry_price korup tetap membuat check_trailing_sl() crash sendiri.
        hit_sl posisi yang SAMA tetap harus jalan."""
        pos = _make_position(
            symbol="TRAIL_POISON/USDT", entry_price="corrupt",
            take_profit_price=None, stop_loss_price=100.0, atr_at_entry=1.0,
        )
        fake_self = _build_fake_self([pos], {"TRAIL_POISON/USDT": 90.0})

        with self.assertLogs("main_future", level="ERROR") as cm:
            asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))

        self.assertEqual(len(fake_self._close_calls), 1)
        self.assertEqual(fake_self._close_calls[0][0], "TRAIL_POISON/USDT")
        joined = "\n".join(cm.output)
        self.assertIn("Trailing SL check gagal", joined)

    def test_cancelled_error_not_treated_as_poison_pill(self):
        """asyncio.CancelledError HARUS tetap menghentikan loop via
        `except asyncio.CancelledError: break` yang SUDAH ADA di level while
        -- BUKAN tertangkap oleh except Exception baru (yang akan salah
        mencatatnya sbg 'gagal proses posisi')."""
        pos = _make_position(symbol="CANCEL/USDT")
        fake_self = _build_fake_self([pos], {"CANCEL/USDT": 40.0})

        async def _raise_cancelled(symbol):
            raise asyncio.CancelledError()
        fake_self._get_current_price = _raise_cancelled

        # Tidak boleh raise keluar (outer while sudah break dgn bersih),
        # dan tidak ada posisi yang sempat di-close.
        asyncio.run(TradingBot.run_sl_tp_monitor(fake_self))
        self.assertEqual(fake_self._close_calls, [])


if __name__ == "__main__":
    unittest.main()
