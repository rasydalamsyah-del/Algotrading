"""
spot/test_exchange_spot_auto_scan_validation.py -- Test untuk bug-fix #35:
auto_scan_and_populate() (spot/exchange_spot.py) tidak punya validasi
is_valid_symbol sebelum menulis hasil scan ke universe.json/
universe_overrides -- BEDA dari #31 (endpoint manual POST /api/universe/add,
sudah diperbaiki) -- ini jalur AUTO-SCAN (dipicu flag DB
auto_scan_universe, dipanggil sekali saat startup bot).

[ROOT CAUSE] scan_binance_universe() hit REST Binance mentah, independen
dari ccxt -- bisa menghasilkan simbol yang secara teknis listing di
Binance tapi tidak dikenali objek ccxt yang benar-benar dipakai bot
(kelas masalah sama dgn insiden EVAA/USDT di futures, yang sudah
diperbaiki lewat auto_scan_and_populate_futures()). Jalur spot ini
sebelumnya TIDAK PERNAH dapat perbaikan yang sama, dikonfirmasi baca kode
langsung.

Fix: parameter is_valid_symbol opsional (default None, non-breaking utk
caller lama) -- kalau diisi, filter coins SEBELUM ditulis ke
universe.json/universe_overrides. main_spot.py sekarang mengirim
self.exchange.is_symbol_supported, pola sama persis dgn
future/main_future.py.

    python3 -m unittest spot.test_exchange_spot_auto_scan_validation -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from spot.exchange_spot import auto_scan_and_populate


def _make_coins(symbols):
    return [{"symbol": s, "volume_24h": 1_000_000.0} for s in symbols]


def _build_fake_db(scan_flag="true", db_overrides=None):
    db = AsyncMock()
    db.get_bot_state = AsyncMock(return_value=scan_flag)
    db.set_bot_state = AsyncMock()
    db.get_active_universe_overrides = AsyncMock(return_value=db_overrides or [])
    db.deactivate_universe_override = AsyncMock()
    db.upsert_universe_override = AsyncMock()
    return db


class TestAutoScanValidation(unittest.TestCase):

    def test_invalid_symbols_excluded_from_result_and_db_write(self):
        """[Regresi kunci -- inti bug #35] Symbol yang scan_binance_
        universe() temukan tapi is_valid_symbol() tolak HARUS TIDAK masuk
        hasil akhir, DAN tidak pernah ditulis ke universe_overrides."""
        db = _build_fake_db()
        coins = _make_coins(["BTC/USDT", "FAKECOIN/USDT", "ETH/USDT"])

        def is_valid(sym):
            return sym != "FAKECOIN/USDT"

        with patch("spot.exchange_spot.scan_binance_universe", return_value=coins), \
             patch("spot.exchange_spot.save_universe_json") as mock_save:
            result = asyncio_run(auto_scan_and_populate(db, is_valid_symbol=is_valid))

        self.assertNotIn("FAKECOIN/USDT", result)
        self.assertIn("BTC/USDT", result)
        self.assertIn("ETH/USDT", result)

        written_symbols = [call.kwargs.get("symbol") for call in db.upsert_universe_override.await_args_list]
        self.assertNotIn("FAKECOIN/USDT", written_symbols)
        self.assertIn("BTC/USDT", written_symbols)

        saved_coins = mock_save.call_args[0][0]
        saved_symbols = [c["symbol"] for c in saved_coins]
        self.assertNotIn("FAKECOIN/USDT", saved_symbols, "symbol invalid tidak boleh masuk universe.json juga")

    def test_no_is_valid_symbol_keeps_old_behavior_unfiltered(self):
        """[Non-regresi -- default None] Caller lama yang tidak kirim
        is_valid_symbol HARUS tetap dapat semua hasil scan apa adanya."""
        db = _build_fake_db()
        coins = _make_coins(["BTC/USDT", "WEIRD/USDT"])

        with patch("spot.exchange_spot.scan_binance_universe", return_value=coins), \
             patch("spot.exchange_spot.save_universe_json"):
            result = asyncio_run(auto_scan_and_populate(db))

        self.assertIn("BTC/USDT", result)
        self.assertIn("WEIRD/USDT", result)

    def test_all_symbols_valid_no_filtering_effect(self):
        db = _build_fake_db()
        coins = _make_coins(["BTC/USDT", "ETH/USDT"])

        with patch("spot.exchange_spot.scan_binance_universe", return_value=coins), \
             patch("spot.exchange_spot.save_universe_json"):
            result = asyncio_run(auto_scan_and_populate(db, is_valid_symbol=lambda s: True))

        self.assertEqual(set(result), {"BTC/USDT", "ETH/USDT"})

    def test_all_symbols_invalid_falls_back_without_crash(self):
        """Kalau SEMUA hasil scan tertolak, `coins` jadi kosong -> masuk
        jalur fallback yang SUDAH ADA (load_universe_json() lalu DB
        overrides) -- sama seperti skenario "scan gagal" biasa, bukan
        crash. Fallback itu sendiri di-mock kosong di sini supaya
        terisolasi murni dari file universe.json real."""
        db = _build_fake_db()
        coins = _make_coins(["FAKE1/USDT", "FAKE2/USDT"])

        with patch("spot.exchange_spot.scan_binance_universe", return_value=coins), \
             patch("spot.exchange_spot.save_universe_json") as mock_save, \
             patch("spot.exchange_spot.load_universe_json", return_value=[]):
            result = asyncio_run(auto_scan_and_populate(db, is_valid_symbol=lambda s: False))

        self.assertEqual(result, [])
        db.upsert_universe_override.assert_not_awaited()
        mock_save.assert_not_called()

    def test_scan_flag_not_true_skips_scan_and_validation_entirely(self):
        """Kalau flag auto_scan_universe bukan 'true', tidak ada scan sama
        sekali -- is_valid_symbol tidak boleh dipanggil (tidak ada hasil
        scan untuk divalidasi)."""
        db = _build_fake_db(scan_flag="false")
        is_valid = AsyncMock()  # kalau kepanggil, test gagal krn ini bukan callable sync valid

        with patch("spot.exchange_spot.scan_binance_universe") as mock_scan, \
             patch("spot.exchange_spot.load_universe_json", return_value=["BTC/USDT"]):
            result = asyncio_run(auto_scan_and_populate(db, is_valid_symbol=is_valid))

        mock_scan.assert_not_called()
        self.assertEqual(result, ["BTC/USDT"])


class TestMainSpotWiresIsValidSymbol(unittest.TestCase):
    """[Regresi kunci -- pola bug 'lupa nyambungin caller' yang sudah 3x
    ditemukan di sesi ini] Fix di exchange_spot.py sendiri TIDAK CUKUP kalau
    main_spot.py::start() tidak genuinely meneruskan is_valid_symbol= --
    verifikasi via inspect.getsource(), sama pola dgn test_scorer_suggest_
    sl_tp_side_aware.py sebelumnya."""

    def test_start_source_passes_is_symbol_supported(self):
        import inspect
        from spot.main_spot import TradingBot
        src = inspect.getsource(TradingBot.start)
        self.assertIn(
            "is_valid_symbol=self.exchange.is_symbol_supported", src,
            "start() harus meneruskan self.exchange.is_symbol_supported ke "
            "auto_scan_and_populate() -- kalau tidak, fix di exchange_spot.py "
            "sendiri tidak pernah genuinely terpakai.",
        )


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
