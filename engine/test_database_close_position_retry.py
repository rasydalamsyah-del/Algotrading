"""
engine/test_database_close_position_retry.py -- Test untuk
DatabaseManager.close_position_with_retry() (engine/database.py).

[ITEM #4 -- audit fungsional, mitigasi root-cause phantom position] Root
cause phantom yang terdokumentasi: exchange (paper/asli) sudah commit fill
SEBELUM close_position() dipanggil (urutan tidak bisa dibalik -- DB harus
mencerminkan REALITAS fill). Kalau close_position() gagal karena kegagalan
TRANSIEN (lock SQLite, hiccup koneksi), DB nyangkut is_open=True permanen.
close_position_with_retry() menambah retry-backoff 3x (pola sama dgn
BaseExchangeConnector._retry() dari item #6, TAPI generik -- semua Exception
di-retry, bukan cuma tipe ccxt-spesifik) sebelum caller (_do_close_position()
di kedua bot) menyerah & masuk ke jalur log.critical/save_log/notify_error.

Diuji lewat method UNBOUND (DatabaseManager.close_position_with_retry(fake_self, ...))
dgn fake_self.close_position sebagai AsyncMock -- tidak perlu DB nyata.
asyncio.sleep di-patch supaya test tidak menunggu backoff sungguhan.

    python3 -m unittest engine.test_database_close_position_retry -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.database import DatabaseManager


class TestClosePositionWithRetry(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_succeeds_first_try_no_retry_no_sleep(self):
        fake_self = SimpleNamespace()
        fake_self.close_position = AsyncMock(return_value=SimpleNamespace(symbol="TEST/USDT"))

        import asyncio
        result = asyncio.run(DatabaseManager.close_position_with_retry(
            fake_self, "TEST/USDT", 10.0, 5.0,
        ))

        self.assertEqual(result.symbol, "TEST/USDT")
        self.assertEqual(fake_self.close_position.call_count, 1)
        self._mock_sleep.assert_not_awaited()

    def test_fails_twice_then_succeeds_on_third_attempt(self):
        fake_self = SimpleNamespace()
        fake_self.close_position = AsyncMock(side_effect=[
            RuntimeError("database is locked"),
            RuntimeError("database is locked"),
            SimpleNamespace(symbol="TEST/USDT"),
        ])

        import asyncio
        result = asyncio.run(DatabaseManager.close_position_with_retry(
            fake_self, "TEST/USDT", 10.0, 5.0,
        ))

        self.assertEqual(result.symbol, "TEST/USDT")
        self.assertEqual(fake_self.close_position.call_count, 3)
        self.assertEqual(self._mock_sleep.await_count, 2, "Backoff sebelum attempt 2 & 3, tidak setelah sukses")

    def test_exhausts_all_retries_raises_last_exception(self):
        fake_self = SimpleNamespace()
        exc1 = RuntimeError("transient error 1")
        exc2 = RuntimeError("transient error 2")
        exc3 = RuntimeError("transient error 3 -- final")
        fake_self.close_position = AsyncMock(side_effect=[exc1, exc2, exc3])

        import asyncio
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(DatabaseManager.close_position_with_retry(
                fake_self, "TEST/USDT", 10.0, 5.0,
            ))

        self.assertIs(cm.exception, exc3, "Exception TERAKHIR yang di-raise, bukan yang pertama")
        self.assertEqual(fake_self.close_position.call_count, 3)
        self.assertEqual(self._mock_sleep.await_count, 2, "Tidak ada sleep SETELAH percobaan terakhir yang gagal")

    def test_retries_parameter_respected(self):
        fake_self = SimpleNamespace()
        fake_self.close_position = AsyncMock(side_effect=RuntimeError("always fails"))

        import asyncio
        with self.assertRaises(RuntimeError):
            asyncio.run(DatabaseManager.close_position_with_retry(
                fake_self, "TEST/USDT", 10.0, 5.0, retries=5,
            ))

        self.assertEqual(fake_self.close_position.call_count, 5)


if __name__ == "__main__":
    unittest.main()
