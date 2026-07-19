"""
spot/test_main_spot_entry_slot_reservation.py -- Test untuk try/finally
reserve/release _open_positions_count (Opsi 1) + refresh pasca-entry
(Opsi 2) di TradingBot._handle_buy() (spot/main_spot.py).

[ITEM #2 -- audit fungsional] Lihat docstring lengkap di
engine/test_risk_base.py untuk latar belakang penuh (race
_open_positions_count antar-worker GATE3_WORKERS). File itu menguji
reserve/release di level RiskManager.evaluate_order() langsung. File INI
menguji lapisan di atasnya -- _handle_buy() SUNGGUHAN (lewat method
UNBOUND, pola sama dgn test_main_spot_sl_tp_monitor.py) -- membuktikan:

1. Reservasi (sudah terjadi di dalam evaluate_order(), Opsi 1) DILEPAS
   lagi lewat release_position_slot() kalau execute_signal() gagal
   SETELAH approval (trade=None ATAU exception) -- bukan cuma di level
   RiskManager, tapi genuinely lewat try/finally yang membungkus seluruh
   alur _handle_buy() pasca-approval.
2. upsert_position() gagal (raise, spot-specific -- BEDA dari futures yg
   non-fatal, lihat test_main_future_entry_slot_reservation.py) juga
   melepas reservasi.
3. Entry SUKSES: reservasi TIDAK dilepas (tetap 1), DAN _refresh_portfolio()
   (Opsi 2) terpanggil tepat sekali.

    python3 -m unittest spot.test_main_spot_entry_slot_reservation -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from engine.core.models import SignalEvent, SignalType
from spot.main_spot import TradingBot
from spot.risk_spot import RiskManager


class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStrategy:
    """Stub minimal -- cuma atribut yang disentuh di ekor sukses
    _handle_buy() (register_position path)."""

    def __init__(self):
        self._lock = _FakeLock()
        self._last_entry_params = {"TEST/USDT": {"exit_mode": None, "p": {"dummy": True}}}
        self._pos_trackers = {}
        self.register_position = MagicMock()


def _make_signal(symbol="TEST/USDT", price=10.0):
    return SignalEvent(
        symbol=symbol, signal_type=SignalType.BUY, price=price,
        timestamp=datetime.now(timezone.utc), strategy="test_strategy",
        stop_loss=None, take_profit=None, metadata={},
    )


def _build_fake_self(risk_manager, execute_signal_return=None, execute_signal_side_effect=None,
                      upsert_side_effect=None):
    fake_self = SimpleNamespace()
    fake_self.portfolio_state = {"total_equity": 10000.0}
    fake_self.config = {"max_position_size_pct": 10.0}
    fake_self.risk_manager = risk_manager
    fake_self._equity_lock = asyncio.Lock()
    fake_self.strategy = _FakeStrategy()
    fake_self.exchange = SimpleNamespace(get_min_order_cost=MagicMock(return_value=None))

    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(
        return_value=execute_signal_return, side_effect=execute_signal_side_effect,
    ))

    fake_db = SimpleNamespace()
    fake_db.upsert_position = AsyncMock(side_effect=upsert_side_effect)
    fake_db.update_position_entry_fee = AsyncMock()
    fake_db.link_latest_signal_to_trade = AsyncMock(return_value=True)
    fake_db.get_open_position_by_symbol = AsyncMock(return_value=SimpleNamespace())
    fake_db.save_log = AsyncMock()
    fake_self.db = fake_db

    fake_self.notifier = SimpleNamespace(
        notify_trade_opened=AsyncMock(), notify_projection=AsyncMock(),
    )

    fake_self._refresh_portfolio = AsyncMock()

    return fake_self


def _make_trade(fee_cost=0.0, notes=""):
    return SimpleNamespace(
        filled=10.0, amount=10.0, executed_price=10.0, notes=notes,
        fee_cost=fee_cost, order_id="ORDER1", id=1, timestamp=None,
    )


class TestHandleBuySlotReservation(unittest.TestCase):

    def _approved_rm(self, max_open=1):
        rm = RiskManager({"max_open_positions": max_open})
        rm.update_portfolio_state(
            equity=10000.0, initial_equity=10000.0,
            free_balance=10000.0, open_positions_count=0,
        )
        return rm

    def test_execute_signal_returns_none_releases_reservation(self):
        """[Skenario persis diminta] execute_signal() gagal (trade=None)
        SETELAH risk_manager approve -- reservasi yang sudah dibuat di
        dalam evaluate_order() (Opsi 1) HARUS dilepas kembali."""
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(rm, execute_signal_return=None)
        signal = _make_signal()

        asyncio.run(TradingBot._handle_buy(fake_self, signal))

        self.assertEqual(
            rm._open_positions_count, 0,
            "Reservasi harus kembali ke 0 -- posisi TIDAK benar-benar terbuka",
        )
        # Slot harus bisa dipakai lagi oleh kandidat lain di siklus yang sama.
        second = asyncio.run(rm.evaluate_order(
            symbol="OTHER/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertTrue(second.is_approved)

    def test_execute_signal_raises_releases_reservation_and_propagates(self):
        """execute_signal() melempar exception (bukan cuma return None) --
        finally HARUS tetap melepas reservasi walau exception propagate
        keluar dari _handle_buy()."""
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(
            rm, execute_signal_side_effect=RuntimeError("exchange error"),
        )
        signal = _make_signal()

        with self.assertRaises(RuntimeError):
            asyncio.run(TradingBot._handle_buy(fake_self, signal))

        self.assertEqual(rm._open_positions_count, 0)

    def test_upsert_position_fails_releases_reservation_and_propagates(self):
        """[Spot-specific] upsert_position() raise -- spot's _handle_buy()
        SUDAH re-raise exception ini (beda dari futures yg non-fatal, lihat
        docstring modul ini) -- reservasi harus ikut dilepas."""
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(
            rm, execute_signal_return=_make_trade(),
            upsert_side_effect=RuntimeError("db write error"),
        )
        signal = _make_signal()

        with self.assertRaises(RuntimeError):
            asyncio.run(TradingBot._handle_buy(fake_self, signal))

        self.assertEqual(rm._open_positions_count, 0)

    def test_successful_entry_keeps_reservation_and_refreshes_portfolio(self):
        """Entry SUKSES penuh (execute_signal + upsert_position berhasil) --
        reservasi TIDAK dilepas (tetap 1, bukan Opsi-1 punya kesalahan-arah),
        DAN _refresh_portfolio() (Opsi 2) terpanggil tepat sekali sbg lapisan
        tambahan."""
        rm = self._approved_rm(max_open=2)
        fake_self = _build_fake_self(rm, execute_signal_return=_make_trade())
        signal = _make_signal()

        asyncio.run(TradingBot._handle_buy(fake_self, signal))

        self.assertEqual(
            rm._open_positions_count, 1,
            "Reservasi HARUS tetap ada -- entry genuinely sukses",
        )
        fake_self._refresh_portfolio.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
