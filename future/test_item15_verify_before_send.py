"""
future/test_item15_verify_before_send.py -- Test untuk Opsi C2
(verify-before-send) dari item audit #15, Temuan C.

_do_close_position() (future/main_future.py) SEKARANG cek langsung ke
exchange (_verify_position_exists_at_exchange()) SEBELUM mengirim order
close apa pun. Kalau exchange sudah genuinely tidak punya posisi ini lagi
(mis. attempt close sebelumnya sukses di exchange tapi gagal tercatat DB
-- Temuan A), order BARU tidak dikirim sama sekali -- DB diselaraskan
langsung via _sync_db_close_without_order() memakai harga terakhir yang
diketahui, TANPA order baru yang bisa disalahartikan sbg "buka posisi
baru arah berlawanan" (Temuan C, bug lama).

Dites lewat method UNBOUND (pola sama dgn file test #15 lain di paket
ini), db REAL (sqlite in-memory) + exchange fake (AsyncMock fetch_positions,
TIDAK perlu FutureExchangeConnector penuh utk test unit terisolasi ini --
integrasi end-to-end dgn paper connector ASLI sudah dites terpisah di
future/test_item15_paper_position_race_simulation.py).

    python3 -m unittest future.test_item15_verify_before_send -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.database import DatabaseManager
from future.main_future import TradingBot
from future.risk_future import RiskManager


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_db() -> DatabaseManager:
    db = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=100.0, amount=1.0,
        strategy_name="test_strategy",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_trade():
    return SimpleNamespace(executed_price=105.0, order_id="ORDER1")


# ─────────────────────────────────────────────────────────────────────────
# 1. _verify_position_exists_at_exchange() -- unit terisolasi
# ─────────────────────────────────────────────────────────────────────────

class TestVerifyPositionExistsAtExchange(unittest.TestCase):

    def _fake_self(self, fetch_positions_return=None, fetch_positions_side_effect=None):
        fake_self = SimpleNamespace()
        fake_self.exchange = SimpleNamespace(
            fetch_positions=AsyncMock(
                return_value=fetch_positions_return,
                side_effect=fetch_positions_side_effect,
            ),
        )
        return fake_self

    def _verify(self, fake_self, symbol, side):
        return TradingBot._verify_position_exists_at_exchange(fake_self, symbol, side)

    def test_matching_position_returns_true(self):
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "TEST/USDT", "side": "long", "amount": 1.0},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertTrue(result)

    def test_no_position_at_all_returns_false(self):
        fake_self = self._fake_self(fetch_positions_return=[])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertFalse(result)

    def test_wrong_side_returns_false(self):
        """Posisi ADA tapi side-nya BEDA (mis. sudah kebalik jadi short
        akibat race lain) -- BUKAN posisi yang kita maksud, harus False."""
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "TEST/USDT", "side": "short", "amount": 1.0},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertFalse(result)

    def test_zero_amount_treated_as_not_existing(self):
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "TEST/USDT", "side": "long", "amount": 0.0},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertFalse(result)

    def test_contracts_field_fallback_ccxt_style(self):
        """ccxt live pakai key 'contracts', bukan 'amount' -- fallback
        harus benar (pola sama dgn fetch_binance_futures_positions())."""
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "TEST/USDT", "side": "long", "contracts": 2.5},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertTrue(result)

    def test_other_symbol_in_list_ignored(self):
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "OTHER/USDT", "side": "long", "amount": 5.0},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertFalse(result)

    def test_fetch_failure_fails_safe_to_true(self):
        """[Keputusan desain eksplisit] fetch gagal -> True (anggap MASIH
        ada), BUKAN False -- False akan bikin caller skip order & langsung
        tulis DB closed, yang BERBAHAYA kalau ternyata posisi ASLI masih
        ada (bikin phantom arah sebaliknya)."""
        fake_self = self._fake_self(fetch_positions_side_effect=ConnectionError("timeout"))
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "long"))
        self.assertTrue(result)

    def test_short_side_matching_works_symmetrically(self):
        fake_self = self._fake_self(fetch_positions_return=[
            {"symbol": "TEST/USDT", "side": "short", "amount": 3.0},
        ])
        result = asyncio.run(self._verify(fake_self, "TEST/USDT", "short"))
        self.assertTrue(result)


# ─────────────────────────────────────────────────────────────────────────
# 2. _sync_db_close_without_order() -- unit terisolasi
# ─────────────────────────────────────────────────────────────────────────

class TestSyncDbCloseWithoutOrder(unittest.TestCase):

    def _fake_self(self, db):
        fake_self = SimpleNamespace()
        fake_self.db = db
        fake_self.notifier = SimpleNamespace(notify_trade_closed=AsyncMock())
        return fake_self

    def test_long_position_realized_pnl_computed_correctly(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 2.0,
                "side": "long", "is_open": True,
            })
            fake_self = self._fake_self(db)
            pos = _make_position(side="long", entry_price=100.0, amount=2.0)

            await TradingBot._sync_db_close_without_order(fake_self, pos, 110.0, "SL hit", 2.0)

            self.assertIsNone(await db.get_open_position_by_symbol("TEST/USDT"))
            fake_self.notifier.notify_trade_closed.assert_awaited_once()
            _, kwargs = fake_self.notifier.notify_trade_closed.call_args
            self.assertAlmostEqual(kwargs["realized_pnl"], 20.0, places=6)  # (110-100)*2

        asyncio.run(scenario())

    def test_short_position_realized_pnl_computed_correctly(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 2.0,
                "side": "short", "is_open": True,
            })
            fake_self = self._fake_self(db)
            pos = _make_position(side="short", entry_price=100.0, amount=2.0)

            await TradingBot._sync_db_close_without_order(fake_self, pos, 90.0, "SL hit", 2.0)

            fake_self.notifier.notify_trade_closed.assert_awaited_once()
            _, kwargs = fake_self.notifier.notify_trade_closed.call_args
            self.assertAlmostEqual(kwargs["realized_pnl"], 20.0, places=6)  # (100-90)*2

        asyncio.run(scenario())

    def test_reason_tagged_as_verify_sync(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = self._fake_self(db)
            pos = _make_position()

            await TradingBot._sync_db_close_without_order(fake_self, pos, 105.0, "SL hit", 1.0)

            _, kwargs = fake_self.notifier.notify_trade_closed.call_args
            self.assertIn("disinkronkan tanpa order", kwargs["reason"])

        asyncio.run(scenario())

    def test_db_write_failure_logs_critical_does_not_crash(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = self._fake_self(db)
            pos = _make_position()

            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("db also broken")),
            ), patch("engine.database.asyncio.sleep", new=AsyncMock()):
                with self.assertLogs("main_future", level="CRITICAL") as cm:
                    await TradingBot._sync_db_close_without_order(fake_self, pos, 105.0, "SL hit", 1.0)

            self.assertIn("close_position (DB) GAGAL", "\n".join(cm.output))
            # Notify_trade_closed TIDAK terpanggil krn DB write gagal duluan.
            fake_self.notifier.notify_trade_closed.assert_not_awaited()

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 3. Integrasi _do_close_position() -- verify menentukan jalur mana
# ─────────────────────────────────────────────────────────────────────────

def _build_fake_self_for_do_close(db, fetch_positions_return):
    fake_self = SimpleNamespace()
    fake_self.db = db
    fake_self.exchange = SimpleNamespace(
        fetch_positions=AsyncMock(return_value=fetch_positions_return),
    )
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = SimpleNamespace(execute_signal=AsyncMock(return_value=_make_trade()))
    fake_self._close_retry_count = {}
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )
    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()

    # [SimpleNamespace tidak auto-bind method class] _do_close_position()
    # panggil self._verify_position_exists_at_exchange()/self._sync_db_
    # close_without_order() -- sambungkan manual, pola sama dgn test #15 lain.
    async def _bound_verify(*a, **kw):
        return await TradingBot._verify_position_exists_at_exchange(fake_self, *a, **kw)
    fake_self._verify_position_exists_at_exchange = _bound_verify

    async def _bound_sync_close(*a, **kw):
        return await TradingBot._sync_db_close_without_order(fake_self, *a, **kw)
    fake_self._sync_db_close_without_order = _bound_sync_close

    return fake_self


class TestDoClosePositionRoutingViaVerify(unittest.TestCase):
    """[Bukti utama Tahap 1] Skenario lama (exchange flat, DB masih open)
    SEKARANG tidak lagi mengirim order sia-sia -- dibuktikan LANGSUNG lewat
    assert executor.execute_signal TIDAK PERNAH dipanggil sama sekali."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_position_missing_at_exchange_skips_order_entirely(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self_for_do_close(db, fetch_positions_return=[])
            pos = _make_position()

            await TradingBot._do_close_position(fake_self, pos, 105.0, "SL hit")

            fake_self.executor.execute_signal.assert_not_awaited()
            self.assertIsNone(await db.get_open_position_by_symbol("TEST/USDT"))

        asyncio.run(scenario())

    def test_position_still_exists_proceeds_with_order_as_before(self):
        """Regresi: kalau exchange MEMANG masih punya posisi ini, jalur
        LAMA (kirim order via execute_signal) tetap berjalan apa adanya --
        C2 tidak mengganggu skenario normal."""
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self_for_do_close(
                db, fetch_positions_return=[{"symbol": "TEST/USDT", "side": "long", "amount": 1.0}],
            )
            pos = _make_position()

            await TradingBot._do_close_position(fake_self, pos, 105.0, "SL hit")

            fake_self.executor.execute_signal.assert_awaited_once()
            self.assertIsNone(await db.get_open_position_by_symbol("TEST/USDT"))

        asyncio.run(scenario())

    def test_position_missing_uses_last_known_price_not_fresh_fetch(self):
        """Instruksi eksplisit: pakai 'harga terakhir yang diketahui' --
        exit_price yang diteruskan caller, BUKAN fetch harga baru."""
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            fake_self = _build_fake_self_for_do_close(db, fetch_positions_return=[])
            pos = _make_position()

            await TradingBot._do_close_position(fake_self, pos, 123.456, "SL hit")

            _, kwargs = fake_self.notifier.notify_trade_closed.call_args
            self.assertEqual(kwargs["exit_price"], 123.456)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
