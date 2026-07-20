"""
future/test_main_future_reduce_position_amount_retry.py -- Test untuk
bug-fix #28: TradingBot._do_close_position() (future/main_future.py) jalur
PARTIAL close memanggil db.reduce_position_amount() polos, TIDAK PERNAH
dapat retry-wrapper -- BEDA dari jalur full close yang sudah diperbaiki di
item #4 (close_position_with_retry()).

[ROOT CAUSE] Order partial-close SUDAH commit fill di exchange SEBELUM
reduce_position_amount() dipanggil (urutan tidak bisa dibalik, sama persis
prinsip item #4) -- kalau write DB gagal karena kegagalan TRANSIEN (lock
SQLite/hiccup koneksi), DB nyangkut `amount` LAMA (full, terlalu besar)
padahal exchange sudah lebih kecil.

[BEDA KRUSIAL dari full-close, WAJIB diuji terpisah] Kegagalan partial
close TIDAK terdeteksi otomatis oleh run_position_sync_loop() seperti
phantom position full-close -- phantom detector cuma bandingkan KEBERADAAN
posisi (symbol ada/tidak, is_open), BUKAN amount. is_open tetap True valid
di kedua sisi setelah partial close gagal ditulis -- mismatch amount ini
SENYAP, tidak pernah self-heal. Pesan critical HARUS eksplisit menyebut ini
(bukan re-pakai pesan full-close yang salah mengklaim "akan terdeteksi
otomatis via run_position_sync_loop()").

Fix: reduce_position_amount_with_retry() (engine/database.py, retry-backoff
3x, pola identik close_position_with_retry()) + pesan critical terpisah
untuk cabang is_partial.

Dites lewat method UNBOUND (TradingBot._do_close_position(fake_self, ...)),
pola sama persis dgn test_main_future_close_position_retry.py.

    python3 -m unittest future.test_main_future_reduce_position_amount_retry -v
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


def _build_fake_self(reduce_side_effect=None, close_side_effect=None):
    fake_self = SimpleNamespace()
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(return_value=_make_trade()))
    fake_self._close_retry_count = {}
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )

    fake_db = SimpleNamespace()
    fake_db.get_open_position_by_symbol = AsyncMock(return_value=SimpleNamespace())
    fake_db.reduce_position_amount_with_retry = AsyncMock(side_effect=reduce_side_effect)
    fake_db.close_position_with_retry = AsyncMock(side_effect=close_side_effect)
    fake_db.save_log = AsyncMock()
    fake_db.update_trade_pnl = AsyncMock()
    fake_self.db = fake_db

    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()

    # [ITEM #15 -- Temuan C, Opsi C2] _do_close_position() SEKARANG verify-
    # before-send lewat self._verify_position_exists_at_exchange() sebelum
    # kirim order -- exchange fake di sini SELALU punya posisi cocok, supaya
    # test file ini TETAP menguji jalur "posisi genuinely ada, order dikirim
    # spt biasa" (perilaku lama utk partial-close, tidak berubah). Skenario
    # "posisi sudah tidak ada" diuji terpisah di
    # future/test_item15_verify_before_send.py.
    fake_self.exchange = SimpleNamespace(
        fetch_positions=AsyncMock(return_value=[
            {"symbol": "TEST/USDT", "side": "long", "amount": 1.0},
        ]),
    )
    fake_self._verify_position_exists_at_exchange = (
        lambda symbol, side: TradingBot._verify_position_exists_at_exchange(fake_self, symbol, side)
    )
    fake_self._sync_db_close_without_order = (
        lambda *a, **kw: TradingBot._sync_db_close_without_order(fake_self, *a, **kw)
    )

    return fake_self


class TestPartialCloseUsesRetryWrapper(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_partial_close_calls_reduce_with_retry_not_plain(self):
        """[Regresi kunci -- inti bug #28] Partial close (close_amount <
        pos.amount) HARUS memanggil reduce_position_amount_with_retry(),
        BUKAN reduce_position_amount() polos."""
        fake_self = _build_fake_self()
        pos = _make_position(amount=1.0)

        asyncio.run(
            TradingBot._do_close_position(fake_self, pos, 55.0, "partial TP", close_amount=0.4)
        )

        fake_self.db.reduce_position_amount_with_retry.assert_awaited_once()
        kwargs = fake_self.db.reduce_position_amount_with_retry.call_args.kwargs
        self.assertEqual(kwargs["reduce_amount"], 0.4)
        # Full close TIDAK boleh ikut terpanggil untuk skenario partial.
        fake_self.db.close_position_with_retry.assert_not_awaited()

    def test_full_close_still_uses_close_position_with_retry(self):
        """[Non-regresi] Full close (close_amount=None / == pos.amount)
        TETAP lewat close_position_with_retry(), bukan reduce_*."""
        fake_self = _build_fake_self()
        pos = _make_position(amount=1.0)

        asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        fake_self.db.close_position_with_retry.assert_awaited_once()
        fake_self.db.reduce_position_amount_with_retry.assert_not_awaited()

    def test_partial_close_succeeds_no_critical_path(self):
        fake_self = _build_fake_self(reduce_side_effect=None)
        pos = _make_position(amount=1.0)

        asyncio.run(
            TradingBot._do_close_position(fake_self, pos, 55.0, "partial TP", close_amount=0.4)
        )

        fake_self.db.save_log.assert_not_awaited()
        fake_self.notifier.notify_error.assert_not_awaited()
        fake_self.notifier.notify_trade_closed.assert_awaited_once()

    def test_partial_close_fails_all_retries_triggers_critical_with_distinct_message(self):
        """[Regresi kunci -- pesan HARUS beda dari full-close] Kalau
        reduce_position_amount_with_retry() exhausted (semua retry gagal),
        pesan critical HARUS menyebut amount mismatch & eksplisit bilang
        TIDAK terdeteksi otomatis oleh run_position_sync_loop() -- BUKAN
        klaim "akan terdeteksi otomatis" seperti pesan full-close (klaim itu
        salah untuk kasus partial, phantom detector cuma cek keberadaan
        posisi bukan amount)."""
        fake_self = _build_fake_self(reduce_side_effect=RuntimeError("db locked"))
        pos = _make_position(amount=1.0)

        with self.assertLogs("main_future", level="CRITICAL") as cm:
            asyncio.run(
                TradingBot._do_close_position(fake_self, pos, 55.0, "partial TP", close_amount=0.4)
            )

        joined = "\n".join(cm.output)
        self.assertIn("reduce_position_amount (DB) GAGAL", joined)
        self.assertIn("TIDAK terdeteksi otomatis", joined)
        self.assertNotIn("akan terdeteksi otomatis via run_position_sync_loop", joined)

        fake_self.db.save_log.assert_awaited_once()
        save_log_args = fake_self.db.save_log.call_args[0]
        self.assertEqual(save_log_args[0], "CRITICAL")
        self.assertEqual(save_log_args[1], "main_future")

        fake_self.notifier.notify_error.assert_awaited_once()
        notify_args = fake_self.notifier.notify_error.call_args[0]
        self.assertEqual(notify_args[0], "close_position_db_failed")

        # [Desain existing futures, TIDAK diubah] fungsi tetap lanjut.
        fake_self.notifier.notify_trade_closed.assert_awaited_once()

    def test_full_close_failure_message_unchanged(self):
        """[Non-regresi] Pesan full-close TETAP seperti item #4 -- klaim
        "akan terdeteksi otomatis via run_position_sync_loop()" MASIH valid
        untuk full-close (is_open memang berubah, phantom detector memang
        bisa mendeteksinya)."""
        fake_self = _build_fake_self(close_side_effect=RuntimeError("db locked"))
        pos = _make_position(amount=1.0)

        with self.assertLogs("main_future", level="CRITICAL") as cm:
            asyncio.run(TradingBot._do_close_position(fake_self, pos, 55.0, "SL hit"))

        joined = "\n".join(cm.output)
        self.assertIn("close_position (DB) GAGAL", joined)
        self.assertIn("akan terdeteksi otomatis via run_position_sync_loop", joined)

    def test_save_log_failure_inside_partial_except_does_not_crash(self):
        fake_self = _build_fake_self(reduce_side_effect=RuntimeError("db locked"))
        fake_self.db.save_log = AsyncMock(side_effect=RuntimeError("save_log also broken"))
        pos = _make_position(amount=1.0)

        asyncio.run(
            TradingBot._do_close_position(fake_self, pos, 55.0, "partial TP", close_amount=0.4)
        )

        fake_self.notifier.notify_error.assert_awaited_once()


class TestReducePositionAmountWithRetryUnit(unittest.TestCase):
    """Unit test langsung terhadap engine/database.py::
    reduce_position_amount_with_retry() -- pola identik dgn
    close_position_with_retry() (item #4), diverifikasi terpisah dari
    integrasi _do_close_position() di atas."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_retries_transient_failure_then_succeeds(self):
        from engine.database import DatabaseManager

        db = DatabaseManager.__new__(DatabaseManager)
        call_count = 0

        async def _flaky_reduce(symbol, reduce_amount, realized_pnl_partial, exit_price):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("db locked")
            return SimpleNamespace(symbol=symbol, amount=0.6)

        db.reduce_position_amount = _flaky_reduce

        result = asyncio.run(
            db.reduce_position_amount_with_retry(
                "TEST/USDT", reduce_amount=0.4, realized_pnl_partial=1.0, exit_price=55.0,
            )
        )
        self.assertEqual(call_count, 3)
        self.assertEqual(result.amount, 0.6)
        self.assertEqual(self.mock_sleep.await_count, 2)

    def test_raises_last_exception_after_exhausting_retries(self):
        from engine.database import DatabaseManager

        db = DatabaseManager.__new__(DatabaseManager)

        async def _always_fails(symbol, reduce_amount, realized_pnl_partial, exit_price):
            raise RuntimeError("db locked permanently")

        db.reduce_position_amount = _always_fails

        with self.assertRaises(RuntimeError):
            asyncio.run(
                db.reduce_position_amount_with_retry(
                    "TEST/USDT", reduce_amount=0.4, realized_pnl_partial=1.0, exit_price=55.0,
                    retries=3,
                )
            )


if __name__ == "__main__":
    unittest.main()
