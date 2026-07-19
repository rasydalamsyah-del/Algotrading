"""
spot/test_main_spot_close_position_retry.py -- Test untuk integrasi
close_position_with_retry() + try/except (SEBELUMNYA NIHIL SAMA SEKALI) di
TradingBot._do_close_position() (spot/main_spot.py).

[ITEM #4 -- audit fungsional, mitigasi root-cause phantom position]
Mirror future/test_main_future_close_position_retry.py -- lihat docstring
di sana untuk latar belakang lengkap. BEDA KRUSIAL dari futures (temuan
investigasi, ditemukan SEBELUM implementasi): spot SEBELUMNYA tidak punya
try/except SAMA SEKALI di sekitar db.close_position() (futures minimal
sudah log.critical telanjang) -- exception propagate diam-diam, cuma
tertangkap generik oleh backstop item #3 di run_sl_tp_monitor(). Sekarang:
close_position_with_retry() + log.critical/save_log/notify_error kalau
semua retry gagal, LALU exception TETAP di-raise (BEDA dari futures yang
lanjut) -- supaya perilaku abort-sisa-fungsi TIDAK berubah (equity update,
notify_trade_closed, _refresh_portfolio() di bawahnya HARUS ikut ter-skip,
persis seperti sebelum fix ini, backstop item #3 & jalur lain yang
mengandalkan propagasi ini tetap berfungsi sama).

    python3 -m unittest spot.test_main_spot_close_position_retry -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spot.main_spot import TradingBot
from spot.risk_spot import RiskManager


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=50.0, amount=1.0,
        strategy_name="test_strategy", entry_fee_actual=0.05, unrealized_pnl=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trade():
    return SimpleNamespace(filled=1.0, executed_price=55.0, fee_cost=0.05)


def _build_fake_self(close_position_side_effect=None):
    fake_self = SimpleNamespace()
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(return_value=_make_trade()))
    fake_self._close_retry_count = {}
    fake_self._equity_lock = asyncio.Lock()
    fake_self.portfolio_state = {"total_equity": 10000.0, "open_pnl": 0.0}
    fake_self.strategy = None
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )
    fake_self._refresh_portfolio = AsyncMock()

    fake_self.exchange = SimpleNamespace(
        fetch_balance=AsyncMock(return_value={}),
        get_taker_fee=lambda symbol: 0.001,
    )

    fake_db = SimpleNamespace()
    fake_db.get_open_position_by_symbol = AsyncMock(return_value=SimpleNamespace())
    fake_db.close_position_with_retry = AsyncMock(side_effect=close_position_side_effect)
    fake_db.save_log = AsyncMock()
    fake_self.db = fake_db

    return fake_self


class TestDoClosePositionRetryIntegration(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_close_position_succeeds_full_flow_completes(self):
        fake_self = _build_fake_self(close_position_side_effect=None)
        pos = _make_position()

        asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        fake_self.db.close_position_with_retry.assert_awaited_once()
        # save_log() TETAP terpanggil pada sukses (log "INFO" biasa, sudah
        # ada sejak lama) -- yang diverifikasi di sini: bukan level CRITICAL.
        save_log_calls = [c.args[0] for c in fake_self.db.save_log.await_args_list]
        self.assertNotIn("CRITICAL", save_log_calls)
        fake_self.notifier.notify_error.assert_not_awaited()
        fake_self.notifier.notify_trade_closed.assert_awaited_once()
        fake_self._refresh_portfolio.assert_awaited_once()

    def test_close_position_fails_all_retries_triggers_critical_notify_then_raises(self):
        """[REGRESI UTAMA, temuan krusial spot vs futures] Exception HARUS
        tetap ter-raise SETELAH log.critical/save_log/notify_error --
        sisa fungsi (equity update, notify_trade_closed, _refresh_portfolio())
        HARUS ikut ter-skip -- BEDA dari futures yang lanjut."""
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        pos = _make_position()

        with self.assertLogs("main", level="CRITICAL") as cm:
            with self.assertRaises(RuntimeError):
                asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        joined = "\n".join(cm.output)
        self.assertIn("close_position (DB) GAGAL", joined)

        fake_self.db.save_log.assert_awaited_once()
        save_log_args = fake_self.db.save_log.call_args[0]
        self.assertEqual(save_log_args[0], "CRITICAL")
        self.assertEqual(save_log_args[1], "main")

        fake_self.notifier.notify_error.assert_awaited_once()
        notify_args = fake_self.notifier.notify_error.call_args[0]
        self.assertEqual(notify_args[0], "close_position_db_failed")

        # [Beda krusial dari futures] sisa fungsi TIDAK boleh jalan.
        fake_self.notifier.notify_trade_closed.assert_not_awaited()
        fake_self._refresh_portfolio.assert_not_awaited()

    def test_save_log_failure_inside_except_does_not_crash_before_raise(self):
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        fake_self.db.save_log = AsyncMock(side_effect=RuntimeError("save_log also broken"))
        pos = _make_position()

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        # Exception yang ter-raise HARUS exception close_position asli
        # (bukan tertutupi oleh kegagalan save_log di dalam except block).
        self.assertEqual(str(cm.exception), "db locked")
        fake_self.notifier.notify_error.assert_awaited_once()

    def test_notify_error_failure_does_not_crash_before_raise(self):
        fake_self = _build_fake_self(close_position_side_effect=RuntimeError("db locked"))
        fake_self.notifier.notify_error = AsyncMock(side_effect=RuntimeError("telegram down"))
        pos = _make_position()

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        self.assertEqual(str(cm.exception), "db locked")


if __name__ == "__main__":
    unittest.main()
