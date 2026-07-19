"""
future/test_main_future_entry_slot_reservation.py -- Test untuk try/finally
reserve/release _open_positions_count (Opsi 1) + refresh pasca-entry
(Opsi 2) di TradingBot._handle_entry() (future/main_future.py).

[ITEM #2 -- audit fungsional] Lihat docstring lengkap di
engine/test_risk_base.py utk latar belakang penuh, dan
spot/test_main_spot_entry_slot_reservation.py utk mirror spot. File INI
menguji _handle_entry() SUNGGUHAN (method UNBOUND, pola sama dgn
test_main_future_sl_tp_monitor.py).

[BEDA KRUSIAL dari spot, ditemukan lewat investigasi kode sebelum menulis
test ini -- BUKAN bug, desain existing yg SENGAJA dipertahankan] Kalau
upsert_position() gagal di futures, kode SUDAH menangkapnya (log.critical +
save_log) TANPA re-raise -- posisi TETAP dianggap terbuka (order genuinely
FILLED di exchange, cuma tracking DB yang gagal). Reservasi_slot() TIDAK
BOLEH dilepas dalam kasus ini -- melepasnya akan membuat gate
max_open_positions berpikir ada slot kosong padahal ada posisi nyata
(untracked) yang menempatinya -- persis kelas bug over-approval yang lagi
diperbaiki di item #2 ini, cuma dari arah sebaliknya. Makanya
_slot_reserved_pending_release di-set False SEGERA setelah trade dikonfirmasi
(trade is not None), BUKAN setelah upsert_position sukses (beda dari spot).

    python3 -m unittest future.test_main_future_entry_slot_reservation -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from engine.core.models import SignalEvent, SignalType
from future.main_future import TradingBot
from future.risk_future import RiskManager


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

    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(
        return_value=execute_signal_return, side_effect=execute_signal_side_effect,
    ))

    fake_db = SimpleNamespace()
    fake_db.upsert_position = AsyncMock(side_effect=upsert_side_effect)
    fake_db.save_log = AsyncMock()
    # [ITEM #1] _handle_entry() sekarang re-check ini tepat sebelum
    # execute_signal() -- None berarti "belum ada posisi", tidak relevan
    # dgn skenario item #2 yang diuji file ini (single-caller, bukan race).
    fake_db.get_open_position_by_symbol = AsyncMock(return_value=None)
    fake_self.db = fake_db

    fake_self.notifier = SimpleNamespace(notify_trade_opened=AsyncMock())
    fake_self._refresh_portfolio = AsyncMock()

    return fake_self


def _make_trade():
    return SimpleNamespace(filled=10.0, amount=10.0, executed_price=10.0, order_id="ORDER1")


class TestHandleEntrySlotReservation(unittest.TestCase):

    def _approved_rm(self, max_open=1):
        rm = RiskManager({"max_open_positions": max_open})
        rm.update_portfolio_state(
            equity=10000.0, initial_equity=10000.0,
            free_balance=10000.0, open_positions_count=0,
        )
        return rm

    def test_execute_signal_returns_none_releases_reservation(self):
        """[Skenario persis diminta] execute_signal() gagal (trade=None)
        SETELAH risk_manager approve -- reservasi HARUS dilepas kembali."""
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(rm, execute_signal_return=None)
        signal = _make_signal()

        asyncio.run(TradingBot._handle_entry(fake_self, signal, "long"))

        self.assertEqual(rm._open_positions_count, 0)
        second = asyncio.run(rm.evaluate_order(
            symbol="OTHER/USDT", side="buy", price=10.0, quantity=10.0,
            leverage=5, existing_position_side=None,
        ))
        self.assertTrue(second.is_approved)

    def test_execute_signal_raises_releases_reservation_and_propagates(self):
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(
            rm, execute_signal_side_effect=RuntimeError("exchange error"),
        )
        signal = _make_signal()

        with self.assertRaises(RuntimeError):
            asyncio.run(TradingBot._handle_entry(fake_self, signal, "long"))

        self.assertEqual(rm._open_positions_count, 0)

    def test_upsert_position_fails_does_NOT_release_reservation_no_raise(self):
        """[BEDA KRUSIAL dari spot -- lihat docstring modul] upsert_position()
        gagal di futures TIDAK raise (non-fatal, posisi genuinely terbuka di
        exchange) -- reservasi harus TETAP ADA (bukan dilepas), krn slot itu
        genuinely terisi posisi nyata walau untracked di DB."""
        rm = self._approved_rm(max_open=1)
        fake_self = _build_fake_self(
            rm, execute_signal_return=_make_trade(),
            upsert_side_effect=RuntimeError("db write error"),
        )
        signal = _make_signal()

        # Tidak boleh raise -- futures menelan exception ini dgn log.critical.
        asyncio.run(TradingBot._handle_entry(fake_self, signal, "long"))

        self.assertEqual(
            rm._open_positions_count, 1,
            "Reservasi TIDAK boleh dilepas -- posisi genuinely terbuka di "
            "exchange walau DB gagal tercatat (beda dari spot).",
        )
        # Slot HARUS dianggap penuh utk kandidat lain -- posisi nyata (walau
        # untracked) tetap menempati slot itu.
        second = asyncio.run(rm.evaluate_order(
            symbol="OTHER/USDT", side="buy", price=10.0, quantity=10.0,
            leverage=5, existing_position_side=None,
        ))
        self.assertFalse(second.is_approved)

    def test_successful_entry_keeps_reservation_and_refreshes_portfolio(self):
        rm = self._approved_rm(max_open=2)
        fake_self = _build_fake_self(rm, execute_signal_return=_make_trade())
        signal = _make_signal()

        asyncio.run(TradingBot._handle_entry(fake_self, signal, "long"))

        self.assertEqual(rm._open_positions_count, 1)
        fake_self._refresh_portfolio.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
