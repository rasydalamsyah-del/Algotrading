"""
future/test_item15_reduce_only_backstop.py -- Test untuk Opsi C1
(reduce-only backstop) dari item audit #15, Temuan C.

C1 adalah backstop TOCTOU thd Opsi C2 (verify-before-send, sudah
diimplementasikan & dites terpisah di test_item15_verify_before_send.py):
antara _verify_position_exists_at_exchange() dicek dan order benar-benar
dikirim, posisi bisa berubah (race sempit). Order close SEKARANG selalu
dikirim dgn reduce_only=True (paper) / reduceOnly=True (params, native
live) -- kalau exchange (paper ATAU live) mendeteksi tidak ada posisi
cocok utk direduce SAAT ORDER DIEKSEKUSI, order DITOLAK (ReduceOnlyRejected
utk paper) alih-alih diam-diam membuka posisi baru arah berlawanan.

Desain eksplisit: rejection ini BUKAN kegagalan order biasa -- caller
(_do_close_position()) TIDAK retry order, langsung reuse
_sync_db_close_without_order() (Opsi C2, jalur yang SAMA, bukan baru).

    python3 -m unittest future.test_item15_reduce_only_backstop -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.core.models import SignalEvent, SignalType
from engine.database import DatabaseManager
from engine.exchange_base import ReduceOnlyRejected
from engine.execution_base import _reduce_only_params
from future.exchange_future import FutureExchangeConnector
from future.execution_future import OrderExecutionManager
from future.main_future import TradingBot
from future.risk_future import RiskManager


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_db() -> DatabaseManager:
    db = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db


def _make_paper_exchange(initial_capital: float = 10_000.0) -> FutureExchangeConnector:
    ex = FutureExchangeConnector(
        exchange_id="binance", api_key="", api_secret="",
        paper_trading=True, initial_capital=initial_capital,
    )
    ex.fetch_ticker = AsyncMock(return_value={"bid": 100.0, "ask": 100.1, "last": 100.05})
    return ex


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=100.0, amount=1.0,
        strategy_name="test_strategy",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ─────────────────────────────────────────────────────────────────────────
# 1. _simulate_order_fill(reduce_only=True) -- unit terisolasi (paper)
# ─────────────────────────────────────────────────────────────────────────

class TestSimulateOrderFillReduceOnly(unittest.TestCase):

    def test_reduce_only_no_existing_position_rejected(self):
        async def scenario():
            ex = _make_paper_exchange()
            with self.assertRaises(ReduceOnlyRejected):
                await ex._simulate_order_fill(
                    "TEST/USDT", "market", "sell", 1.0, None, reduce_only=True,
                )
            self.assertNotIn("TEST/USDT", ex._paper_positions)

        asyncio.run(scenario())

    def test_reduce_only_same_side_would_add_rejected(self):
        """Posisi ADA tapi order SEARAH (akan MENAMBAH, bukan mengurangi) --
        HARUS ditolak juga, bukan cuma kasus 'nol posisi'."""
        async def scenario():
            ex = _make_paper_exchange()
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)  # buka long
            with self.assertRaises(ReduceOnlyRejected):
                await ex._simulate_order_fill(
                    "TEST/USDT", "market", "buy", 0.5, None, reduce_only=True,
                )  # "buy" lagi -- searah, akan nambah, BUKAN reduce
            # Posisi long ASLI tidak berubah (order ditolak sebelum diproses).
            self.assertEqual(ex._paper_positions["TEST/USDT"]["amount"], 1.0)

        asyncio.run(scenario())

    def test_reduce_only_opposite_side_genuine_reduce_succeeds(self):
        """Kasus VALID -- posisi ada, order berlawanan arah (genuine close) --
        HARUS tetap berhasil normal, C1 tidak boleh mengganggu jalur sah."""
        async def scenario():
            ex = _make_paper_exchange()
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            order = await ex._simulate_order_fill(
                "TEST/USDT", "market", "sell", 1.0, None, reduce_only=True,
            )
            self.assertEqual(order["info"]["action"], "close")
            self.assertNotIn("TEST/USDT", ex._paper_positions)

        asyncio.run(scenario())

    def test_reduce_only_partial_reduce_succeeds(self):
        async def scenario():
            ex = _make_paper_exchange()
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 2.0, None)
            order = await ex._simulate_order_fill(
                "TEST/USDT", "market", "sell", 0.5, None, reduce_only=True,
            )
            self.assertEqual(order["info"]["action"], "reduce")
            self.assertAlmostEqual(ex._paper_positions["TEST/USDT"]["amount"], 1.5, places=6)

        asyncio.run(scenario())

    def test_reduce_only_false_default_unaffected_regression(self):
        """[Regresi] reduce_only default False -- perilaku LAMA (order tanpa
        posisi existing = OPEN baru) TIDAK BERUBAH sama sekali."""
        async def scenario():
            ex = _make_paper_exchange()
            order = await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            self.assertEqual(order["info"]["action"], "open")
            self.assertIn("TEST/USDT", ex._paper_positions)

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 2. create_order() dispatcher -- params -> reduce_only kwarg
# ─────────────────────────────────────────────────────────────────────────

class TestCreateOrderDispatchesReduceOnlyToPaper(unittest.TestCase):

    def test_reduceOnly_param_threaded_to_paper_simulate(self):
        async def scenario():
            ex = _make_paper_exchange()
            with self.assertRaises(ReduceOnlyRejected):
                await ex.create_order(
                    "TEST/USDT", "market", "sell", 1.0,
                    params={"reduceOnly": True},
                )

        asyncio.run(scenario())

    def test_no_params_defaults_reduce_only_false(self):
        async def scenario():
            ex = _make_paper_exchange()
            order = await ex.create_order("TEST/USDT", "market", "buy", 1.0)
            self.assertEqual(order["info"]["action"], "open")

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 3. _reduce_only_params() helper (engine/execution_base.py)
# ─────────────────────────────────────────────────────────────────────────

class TestReduceOnlyParamsHelper(unittest.TestCase):

    def _signal(self, metadata=None):
        return SignalEvent(
            symbol="TEST/USDT", signal_type=SignalType.CLOSE_LONG, price=100.0,
            timestamp=_utcnow(), strategy="test", metadata=metadata,
        )

    def test_reduce_only_true_in_metadata_returns_reduceOnly_dict(self):
        result = _reduce_only_params(self._signal({"reduce_only": True}))
        self.assertEqual(result, {"reduceOnly": True})

    def test_no_metadata_returns_none(self):
        result = _reduce_only_params(self._signal(None))
        self.assertIsNone(result)

    def test_metadata_without_reduce_only_key_returns_none(self):
        result = _reduce_only_params(self._signal({"exit_reason": "SL hit"}))
        self.assertIsNone(result)

    def test_reduce_only_false_explicit_returns_none(self):
        result = _reduce_only_params(self._signal({"reduce_only": False}))
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────
# 4. execute_signal() (OrderExecutionManager REAL) -- propagasi tidak ditelan
# ─────────────────────────────────────────────────────────────────────────

class TestExecuteSignalPropagatesReduceOnlyRejected(unittest.TestCase):
    """[Bukti C1 genuinely nyambung end-to-end lewat pipeline produksi ASLI
    -- bukan cuma _simulate_order_fill() terisolasi] OrderExecutionManager
    SUNGGUHAN (future/execution_future.py), bukan stub."""

    def _make_executor_and_db(self):
        ex = _make_paper_exchange()
        db_task = _make_db()
        return ex, db_task

    def test_reduce_only_rejection_propagates_not_swallowed(self):
        async def scenario():
            ex = _make_paper_exchange()
            db = await _make_db()
            executor = OrderExecutionManager(exchange=ex, db=db)

            # TIDAK ADA posisi existing -- order reduce-only PASTI ditolak.
            close_signal = SignalEvent(
                symbol="TEST/USDT", signal_type=SignalType.CLOSE_LONG, price=100.0,
                timestamp=_utcnow(), strategy="test",
                metadata={"exit_reason": "SL hit", "partial": False, "reduce_only": True},
            )
            from engine.risk_base import RiskAssessment, RiskDecision
            assessment = RiskAssessment(
                decision=RiskDecision.APPROVED, reason="SL hit",
                approved_size=1.0, stop_loss=None, take_profit=None,
            )

            with self.assertRaises(ReduceOnlyRejected):
                await executor.execute_signal(close_signal, assessment)

        asyncio.run(scenario())

    def test_normal_close_signal_without_reduce_only_unaffected(self):
        """[Regresi] Signal TANPA metadata reduce_only (mis. entry biasa,
        atau close lama sebelum fix ini) -- perilaku TIDAK BERUBA, order
        tetap terkirim & sukses membuka posisi normal."""
        async def scenario():
            ex = _make_paper_exchange()
            db = await _make_db()
            executor = OrderExecutionManager(exchange=ex, db=db)

            open_signal = SignalEvent(
                symbol="TEST/USDT", signal_type=SignalType.BUY, price=100.0,
                timestamp=_utcnow(), strategy="test", metadata={},
            )
            from engine.risk_base import RiskAssessment, RiskDecision
            assessment = RiskAssessment(
                decision=RiskDecision.APPROVED, reason="entry",
                approved_size=1.0, stop_loss=None, take_profit=None,
            )

            trade = await executor.execute_signal(open_signal, assessment)
            self.assertIsNotNone(trade)
            self.assertIn("TEST/USDT", ex._paper_positions)

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 5. _do_close_position() -- ReduceOnlyRejected -> sync DB, BUKAN retry
# ─────────────────────────────────────────────────────────────────────────

def _build_fake_self(db, exchange, executor=None):
    fake_self = SimpleNamespace()
    fake_self.db = db
    fake_self.exchange = exchange
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = executor or OrderExecutionManager(exchange=exchange, db=db)
    fake_self._close_retry_count = {}
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )
    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()

    async def _bound_verify(*a, **kw):
        return await TradingBot._verify_position_exists_at_exchange(fake_self, *a, **kw)
    fake_self._verify_position_exists_at_exchange = _bound_verify

    async def _bound_sync(*a, **kw):
        return await TradingBot._sync_db_close_without_order(fake_self, *a, **kw)
    fake_self._sync_db_close_without_order = _bound_sync

    return fake_self


class TestDoClosePositionHandlesReduceOnlyRejected(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_rejection_syncs_db_not_retry_counter(self):
        """[Instruksi eksplisit] JANGAN retry order biasa saat reduce-only
        ditolak -- langsung _sync_db_close_without_order(), _close_retry_count
        TIDAK BOLEH bertambah (itu jalur utk kegagalan order BIASA, beda
        kelas masalah)."""
        async def scenario():
            db = await _make_db()
            ex = _make_paper_exchange()

            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            # Verify-before-send akan lapor "ada" (kita override fetch_positions
            # supaya C2 tidak menutup jalur SEBELUM sampai C1) TAPI
            # _paper_positions ASLI genuinely kosong -- reduce-only order
            # akan ditolak persis di titik eksekusi.
            ex.fetch_positions = AsyncMock(return_value=[
                {"symbol": "TEST/USDT", "side": "long", "amount": 1.0},
            ])

            fake_self = _build_fake_self(db, ex)
            pos = _make_position()

            await TradingBot._do_close_position(fake_self, pos, 100.05, "SL hit")

            self.assertEqual(fake_self._close_retry_count, {}, "reduce-only rejection BUKAN retry biasa")
            self.assertIsNone(await db.get_open_position_by_symbol("TEST/USDT"))
            self.assertNotIn("TEST/USDT", ex._paper_positions)
            fake_self.notifier.notify_error.assert_not_awaited()  # bukan jalur CRITICAL "CLOSE GAGAL"

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 6. [BUKTI UTAMA Tahap 2] TOCTOU: posisi ADA saat verify, HILANG sebelum kirim
# ─────────────────────────────────────────────────────────────────────────

class TestReduceOnlyClosesToctouGap(unittest.TestCase):
    """Skenario spesifik diminta eksplisit: posisi MASIH ADA saat
    _verify_position_exists_at_exchange() (C2) dicek, TAPI berubah jadi
    TIDAK ADA tepat SEBELUM order benar-benar terkirim (race sempit
    antara cek dan kirim). C1 HARUS menolak order di titik eksekusi --
    BUKAN berhasil membuka posisi baru arah berlawanan."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_position_vanishes_between_verify_and_send_order_rejected_not_reopened(self):
        async def scenario():
            db = await _make_db()
            ex = _make_paper_exchange()

            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            self.assertIn("TEST/USDT", ex._paper_positions)

            # [Simulasi race TOCTOU -- PENTING: _do_close_position() cuma
            # panggil _verify_position_exists_at_exchange() SATU KALI per
            # eksekusi, jadi race di sini HARUS ditempel pada panggilan
            # fetch_positions() TUNGGAL itu -- bukan dipicu lewat panggilan
            # verify terpisah/tambahan (yang akan "memakai" race-nya duluan
            # sebelum _do_close_position() sempat jalan, membuat C2 sendiri
            # yang menutup celah -- bukan C1 yang mau dibuktikan di sini).]
            # fetch_positions() melaporkan posisi MASIH ADA (snapshot
            # pre-race, diambil SEBELUM pop) -- TAPI segera setelah snapshot
            # itu diambil, _paper_positions ASLI dihapus (simulasi: proses
            # lain BENAR-BENAR menutupnya tepat di celah waktu sempit ini),
            # SEBELUM order benar-benar diproses _simulate_order_fill()
            # (yang baca _paper_positions LANGSUNG, bukan lewat
            # fetch_positions()).
            original_fetch_positions = ex.fetch_positions

            async def _fetch_then_race(symbols=None):
                result = await original_fetch_positions(symbols)  # snapshot: masih ada
                ex._paper_positions.pop("TEST/USDT", None)  # race: hilang SEKARANG
                return result

            ex.fetch_positions = _fetch_then_race

            executor = OrderExecutionManager(exchange=ex, db=db)
            fake_self = _build_fake_self(db, ex, executor=executor)
            pos = _make_position()

            with self.assertLogs("main_future", level="WARNING") as cm:
                await TradingBot._do_close_position(fake_self, pos, 100.05, "SL hit")

            # [BUKTI presisi -- BUKAN cuma hasil akhir kebetulan benar]
            # Log HARUS menyebut rejection C1 spesifik ("reduce-only
            # DITOLAK exchange"), MEMBUKTIKAN C2 genuinely lolos duluan
            # (kalau C2 sendiri yang menolak, pesan log-nya BEDA -- "posisi
            # TIDAK ditemukan di exchange" tanpa "reduce-only DITOLAK").
            joined = "\n".join(cm.output)
            self.assertIn(
                "reduce-only DITOLAK", joined,
                "C1 (bukan C2) yang seharusnya menutup celah TOCTOU ini -- "
                "log rejection reduce-only spesifik harus muncul."
            )

            # [BUKTI UTAMA] TIDAK ADA posisi baru (short) terbentuk -- C1
            # menolak order di titik eksekusi persis saat race terjadi.
            self.assertNotIn(
                "TEST/USDT", ex._paper_positions,
                "[FIX C1] TOCTOU race seharusnya ditutup -- order HARUS "
                "ditolak exchange, BUKAN berhasil membuka posisi baru."
            )
            # DB tersinkron closed lewat jalur reuse _sync_db_close_without_order().
            self.assertIsNone(await db.get_open_position_by_symbol("TEST/USDT"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
