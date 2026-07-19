"""
future/test_main_future_close_position_retry.py -- Test untuk integrasi
close_position_with_retry() + notify di TradingBot._do_close_position()
(future/main_future.py).

[ITEM #4 -- audit fungsional, mitigasi root-cause phantom position]
Sebelumnya: db.close_position() gagal setelah order sukses tereksekusi
cuma di-log.critical() TELANJANG (tanpa db.save_log/notifier.notify_error,
tanpa retry) -- ditemukan lewat investigasi kode. Sekarang: close_position_
with_retry() (retry-backoff 3x, engine/database.py) + kalau semua retry
tetap gagal, log.critical + db.save_log("CRITICAL",...) + notifier.notify_
error() (reuse pola yang sudah konvensi utk kelas masalah "butuh intervensi
manusia"). Fungsi TETAP lanjut sesudahnya (desain existing futures --
posisi genuinely sudah closed di exchange, TIDAK di-revert).

Dites lewat method UNBOUND (TradingBot._do_close_position(fake_self, ...))
dgn RiskManager futures SUNGGUHAN (bukan mock) + db/executor/notifier fake
minimal.

    python3 -m unittest future.test_main_future_close_position_retry -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from future.main_future import TradingBot
from future.risk_future import RiskManager


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=50.0, amount=1.0,
        strategy_name="test_strategy",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trade():
    return SimpleNamespace(executed_price=55.0, order_id="ORDER1")


def _build_fake_self(close_position_side_effect=None):
    fake_self = SimpleNamespace()
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(return_value=_make_trade()))
    fake_self._close_retry_count = {}
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )

    fake_db = SimpleNamespace()
    fake_db.get_open_position_by_symbol = AsyncMock(return_value=SimpleNamespace())
    fake_db.close_position_with_retry = AsyncMock(side_effect=close_position_side_effect)
    fake_db.save_log = AsyncMock()
    fake_db.update_trade_pnl = AsyncMock()
    fake_self.db = fake_db

    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()

    return fake_self


class TestDoClosePositionRetryIntegration(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_close_position_succeeds_no_critical_path_triggered(self):
        fake_self = _build_fake_self(close_position_side_effect=None)
        pos = _make_position()

        asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        fake_self.db.close_position_with_retry.assert_awaited_once()
        fake_self.db.save_log.assert_not_awaited()
        fake_self.notifier.notify_error.assert_not_awaited()
        fake_self.notifier.notify_trade_closed.assert_awaited_once()

    def test_close_position_fails_all_retries_triggers_critical_notify(self):
        """[REGRESI UTAMA] db.close_position_with_retry() genuinely
        exhausted (semua retry gagal) -- HARUS log.critical + db.save_log
        ('CRITICAL', 'main_future', ...) + notifier.notify_error(). Fungsi
        TETAP lanjut sesudahnya (desain existing futures, TIDAK diubah)."""
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        pos = _make_position()

        with self.assertLogs("main_future", level="CRITICAL") as cm:
            asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        joined = "\n".join(cm.output)
        self.assertIn("close_position (DB) GAGAL", joined)

        fake_self.db.save_log.assert_awaited_once()
        save_log_args = fake_self.db.save_log.call_args[0]
        self.assertEqual(save_log_args[0], "CRITICAL")
        self.assertEqual(save_log_args[1], "main_future")

        fake_self.notifier.notify_error.assert_awaited_once()
        notify_args = fake_self.notifier.notify_error.call_args[0]
        self.assertEqual(notify_args[0], "close_position_db_failed")

        # [Desain existing futures, TIDAK diubah] fungsi tetap lanjut --
        # notify_trade_closed() TETAP terpanggil walau DB gagal ditulis.
        fake_self.notifier.notify_trade_closed.assert_awaited_once()

    def test_save_log_failure_inside_except_does_not_crash(self):
        """save_log() SENDIRI gagal (mis. DB juga lagi bermasalah) --
        tidak boleh membuat _do_close_position() crash total."""
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        fake_self.db.save_log = AsyncMock(side_effect=RuntimeError("save_log also broken"))
        pos = _make_position()

        asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        fake_self.notifier.notify_error.assert_awaited_once()

    def test_notify_error_failure_does_not_crash(self):
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        fake_self.notifier.notify_error = AsyncMock(side_effect=RuntimeError("telegram down"))
        pos = _make_position()

        asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        fake_self.notifier.notify_trade_closed.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
