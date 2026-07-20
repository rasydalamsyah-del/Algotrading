"""
future/test_item15_reconcile_phantom_startup.py -- Test untuk
TradingBot._reconcile_phantom_positions_on_startup() (item audit #15,
Temuan B, Opsi B2 minimal).

Latar belakang: futures sebelumnya TIDAK PUNYA padanan
spot::_reconcile_positions_on_startup() sama sekali -- gap ditemukan
investigasi item #15. Kombinasi dengan Temuan A (is_closing stuck
forever setelah retry exhausted, SUDAH diperbaiki) berarti sebelum fix
ini, phantom position futures tidak pernah self-heal bahkan lewat
restart bot. Fix ini SENGAJA MINIMAL -- hanya phantom_candidates,
auto-close TANPA debounce (aman krn dipanggil sebelum task periodik
manapun dibuat), `untracked`/`amount_mismatches` TETAP di luar scope.

Dites lewat method UNBOUND (TradingBot._reconcile_phantom_positions_on_
startup(fake_self)) dengan db REAL (sqlite in-memory, pola sama dgn
future/test_item15_paper_position_race_simulation.py) + exchange fake
minimal + notifier AsyncMock.

    python3 -m unittest future.test_item15_reconcile_phantom_startup -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.database import DatabaseManager
from future.main_future import TradingBot


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_db() -> DatabaseManager:
    db = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db


def _build_fake_self(db, exchange=None, notifier=None):
    fake_self = SimpleNamespace()
    fake_self.db = db
    fake_self.exchange = exchange if exchange is not None else SimpleNamespace(
        fetch_mark_price=AsyncMock(side_effect=RuntimeError("no mark price in test")),
    )
    fake_self.notifier = notifier if notifier is not None else SimpleNamespace(
        notify_trade_closed=AsyncMock(),
    )
    return fake_self


async def _reconcile(fake_self):
    return await TradingBot._reconcile_phantom_positions_on_startup(fake_self)


class TestReconcilePhantomStartupBasic(unittest.TestCase):
    """Kasus dasar: phantom terdeteksi -> auto-close TANPA debounce
    (langsung di 1 panggilan, beda dari jalur periodik yang butuh 2
    siklus)."""

    def test_phantom_position_closed_immediately_no_debounce(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("GHOST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True, "current_price": 105.0,
            })
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),  # exchange genuinely kosong
            ):
                await _reconcile(fake_self)

            pos_after = await db.get_open_position_by_symbol("GHOST/USDT")
            self.assertIsNone(
                pos_after,
                "[Opsi B2] Phantom HARUS langsung di-close di 1 panggilan "
                "(TANPA debounce), beda dari jalur periodik yang butuh 2 siklus."
            )
            fake_self.notifier.notify_trade_closed.assert_awaited_once()

        asyncio.run(scenario())

    def test_no_phantom_no_action(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("REAL/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[
                    {"symbol": "REAL/USDT", "side": "long", "amount": 1.0,
                     "entry_price": 100.0, "price": 100.0, "leverage": 10,
                     "margin_mode": "isolated", "liquidation_price": None,
                     "usdt_value": 100.0},
                ]),
            ):
                await _reconcile(fake_self)

            pos_after = await db.get_open_position_by_symbol("REAL/USDT")
            self.assertIsNotNone(pos_after, "Posisi yang genuinely ada di exchange TIDAK boleh disentuh.")
            fake_self.notifier.notify_trade_closed.assert_not_awaited()

        asyncio.run(scenario())

    def test_no_open_positions_at_all_no_crash(self):
        async def scenario():
            db = await _make_db()
            fake_self = _build_fake_self(db)
            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),
            ):
                await _reconcile(fake_self)  # tidak boleh raise

        asyncio.run(scenario())


class TestReconcilePhantomStartupFetchFailure(unittest.TestCase):
    """[WAJIB diminta eksplisit] fetch_binance_futures_positions() bisa
    RAISE (item #4 lama, bukan diam-diam return []) -- startup reconcile
    HARUS log warning + skip + LANJUT (bukan crash total)."""

    def test_fetch_failure_logs_warning_and_returns_gracefully(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("SOMESYM/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(side_effect=ConnectionError("exchange API down at startup")),
            ):
                with self.assertLogs("main_future", level="WARNING") as cm:
                    await _reconcile(fake_self)  # HARUS TIDAK raise

            joined = "\n".join(cm.output)
            self.assertIn("fetch posisi exchange gagal", joined)

        asyncio.run(scenario())

    def test_fetch_failure_leaves_all_positions_untouched(self):
        """Kegagalan fetch TIDAK BOLEH menganggap semua posisi DB sbg
        phantom (bahaya arah yang sudah diwanti-wanti item #4 lama) --
        posisi HARUS tetap open, tidak di-close sama sekali."""
        async def scenario():
            db = await _make_db()
            await db.upsert_position("A/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await db.upsert_position("B/USDT", {
                "entry_time": _utcnow(), "entry_price": 50.0, "amount": 2.0,
                "side": "short", "is_open": True,
            })
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(side_effect=TimeoutError("rate limited")),
            ):
                await _reconcile(fake_self)

            self.assertIsNotNone(await db.get_open_position_by_symbol("A/USDT"))
            self.assertIsNotNone(await db.get_open_position_by_symbol("B/USDT"))

        asyncio.run(scenario())

    def test_fetch_failure_does_not_prevent_startup_continuing(self):
        """Simulasikan urutan start() -- reconcile gagal fetch, TAPI kode
        SETELAHNYA (mis. is_running=True) tetap harus bisa jalan (tidak
        ada exception yang lolos ke pemanggil)."""
        async def scenario():
            db = await _make_db()
            fake_self = _build_fake_self(db)
            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(side_effect=RuntimeError("total outage")),
            ):
                await _reconcile(fake_self)
            # Kalau baris ini tercapai tanpa exception, startup TIDAK crash.
            marker_reached = True
            self.assertTrue(marker_reached)

        asyncio.run(scenario())


class TestReconcilePhantomStartupOutOfScopeUntouched(unittest.TestCase):
    """[Opsi B2 -- eksplisit di luar scope] untracked & amount_mismatches
    TIDAK BOLEH disentuh fungsi ini sama sekali."""

    def test_untracked_position_not_adopted(self):
        """Posisi yang ADA di exchange tapi TIDAK ADA di DB (untracked) --
        fungsi ini TIDAK PERNAH meng-adopt/menulis apa pun ke DB untuk
        symbol itu (di luar scope B2, tetap tanggung jawab jalur periodik)."""
        async def scenario():
            db = await _make_db()
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[
                    {"symbol": "UNTRACKED/USDT", "side": "long", "amount": 1.0,
                     "entry_price": 100.0, "price": 100.0, "leverage": 10,
                     "margin_mode": "isolated", "liquidation_price": None,
                     "usdt_value": 100.0},
                ]),
            ):
                await _reconcile(fake_self)

            pos = await db.get_open_position_by_symbol("UNTRACKED/USDT")
            self.assertIsNone(pos, "Fungsi B2 TIDAK BOLEH meng-adopt posisi untracked.")

        asyncio.run(scenario())

    def test_amount_mismatch_not_corrected(self):
        """Symbol ADA di kedua sisi tapi amount beda jauh -- fungsi ini
        TIDAK BOLEH mengoreksi amount (bukan phantom, di luar scope B2)."""
        async def scenario():
            db = await _make_db()
            await db.upsert_position("MISMATCH/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 10.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[
                    {"symbol": "MISMATCH/USDT", "side": "long", "amount": 1.0,
                     "entry_price": 100.0, "price": 100.0, "leverage": 10,
                     "margin_mode": "isolated", "liquidation_price": None,
                     "usdt_value": 100.0},
                ]),
            ):
                await _reconcile(fake_self)

            pos = await db.get_open_position_by_symbol("MISMATCH/USDT")
            self.assertIsNotNone(pos)
            self.assertEqual(
                pos.amount, 10.0,
                "Fungsi B2 TIDAK BOLEH mengoreksi amount mismatch -- itu "
                "tanggung jawab item #36 di jalur periodik."
            )

        asyncio.run(scenario())


class TestReconcilePhantomStartupResilience(unittest.TestCase):
    """Ketahanan per-symbol -- 1 symbol error tidak boleh menghentikan
    pemrosesan symbol lain, notify gagal tidak boleh crash."""

    def test_one_symbol_error_does_not_block_others(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("BAD/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await db.upsert_position("GOOD/USDT", {
                "entry_time": _utcnow(), "entry_price": 50.0, "amount": 2.0,
                "side": "short", "is_open": True,
            })

            call_count = {"n": 0}
            real_get = db.get_open_position_by_symbol

            async def flaky_get(symbol):
                if symbol == "BAD/USDT":
                    call_count["n"] += 1
                    raise RuntimeError("simulated per-symbol failure")
                return await real_get(symbol)

            # [PENTING] Monkeypatch method PADA INSTANCE db yang SAMA
            # (bukan ganti fake_self.db dgn objek baru) -- find_untracked_
            # positions() di dalam _reconcile_phantom_positions_on_startup()
            # butuh db.get_open_positions() (method LAIN, tetap real) utk
            # bekerja; cuma get_open_position_by_symbol yang perlu flaky.
            db.get_open_position_by_symbol = flaky_get
            fake_self = _build_fake_self(db)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),
            ):
                await _reconcile(fake_self)  # tidak boleh raise krn BAD/USDT

            self.assertEqual(call_count["n"], 1)
            good_after = await db.get_open_position_by_symbol("GOOD/USDT")
            self.assertIsNone(good_after, "GOOD/USDT tetap harus diproses walau BAD/USDT error.")

        asyncio.run(scenario())

    def test_notify_failure_does_not_crash(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("GHOST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            notifier = SimpleNamespace(
                notify_trade_closed=AsyncMock(side_effect=RuntimeError("telegram down")),
            )
            fake_self = _build_fake_self(db, notifier=notifier)

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),
            ):
                await _reconcile(fake_self)  # tidak boleh raise

            pos_after = await db.get_open_position_by_symbol("GHOST/USDT")
            self.assertIsNone(pos_after, "Close tetap harus sukses walau notify gagal.")

        asyncio.run(scenario())

    def test_mark_price_fetch_failure_falls_back_to_current_price(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("GHOST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True, "current_price": 108.0,
            })
            fake_self = _build_fake_self(db)  # fetch_mark_price sudah AsyncMock raise di helper

            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),
            ):
                await _reconcile(fake_self)

            fake_self.notifier.notify_trade_closed.assert_awaited_once()
            _, kwargs = fake_self.notifier.notify_trade_closed.call_args
            self.assertEqual(kwargs["exit_price"], 108.0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
