"""
future/test_item15_final_integration.py -- Tahap 3: TEST END-TO-END TUNGGAL
yang mensimulasikan race ASLI (persis narasi awal investigasi item #15,
Tahap 0/1) dan membuktikan hasil akhirnya BENAR dengan Temuan A + Opsi
C2 + Opsi C1 SEMUANYA aktif bersamaan -- bukan diuji terisolasi
per-mekanisme (itu sudah dilakukan di file test #15 lain: Temuan A di
test_item15_paper_position_race_simulation.py, C2 di
test_item15_verify_before_send.py, C1 di test_item15_reduce_only_backstop.py).

Narasi lengkap (persis laporan Tahap 0 awal, "TEMUAN UTAMA #3" investigasi
pertama):
  1. Posisi LONG 1.0 @ 100 ada di KEDUA sisi (DB & paper exchange).
  2. Siklus SL/TP monitor #1: harga menyentuh SL, _close_position_market()
     dipanggil. mark_position_closing() set is_closing=True. Order close
     (reduce_only=True) berhasil -- exchange genuinely flat sekarang. DB
     write GAGAL (kegagalan transien disimulasikan) setelah 3x retry --
     [Temuan A] is_closing DIRESET ke False (BUKAN macet True selamanya
     spt sebelum fix), supaya phantom detector bisa menangkapnya lagi.
  3. Siklus SL/TP monitor #2 (5 detik kemudian di produksi, RETRIGGER
     realistis krn DB masih is_open=True DAN in-memory _closing_symbols
     SUDAH lepas lewat finally): _close_position_market() dipanggil LAGI
     utk symbol yang SAMA. Kali ini disimulasikan TOCTOU race SEMPIT:
     [Opsi C2] verify-before-send SALAH melihat "masih ada" (snapshot
     basi/race, dipaksakan lewat mock utk menguji lapisan berikutnya --
     representasi celah race yang genuinely bisa terjadi di produksi
     antara cek dan kirim order sungguhan). Order reduce_only=True
     BENAR-BENAR dikirim lewat pipeline eksekusi ASLI (OrderExecutionManager,
     bukan stub) -- [Opsi C1] DITOLAK exchange krn genuinely tidak ada
     posisi utk direduce SAAT ITU. _do_close_position() menangkap
     ReduceOnlyRejected, LANGSUNG sinkron DB (reuse _sync_db_close_
     without_order(), BUKAN order baru, BUKAN retry biasa).

Hasil akhir yang dibuktikan:
  - DB: is_open=False (tersinkron benar).
  - Paper exchange: NOL posisi utk TEST/USDT -- BUKAN cuma "tidak ada
    posisi long lagi", tapi genuinely TIDAK ADA posisi arah manapun
    (long MAUPUN short) -- pembuktian langsung bahwa bug asli (posisi
    short baru tak tertracking) TIDAK terjadi lagi.
  - Nol order baru terkirim di siklus #2 (dibuktikan via _paper_orders
    count, bukan cuma "hasil akhirnya kebetulan nol posisi").

    python3 -m unittest future.test_item15_final_integration -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.database import DatabaseManager
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
    # Ticker mock DEKAT harga entry -- hindari slippage guard men-trigger
    # utk alasan yang tidak berkaitan dgn fix #15 yang sedang diuji
    # (pelajaran dari Tahap 2: exit_price yang jauh dari ticker mock
    # men-trigger MAX_SLIPPAGE_DEFAULT guard, membuat order gagal utk
    # alasan yang salah).
    ex.fetch_ticker = AsyncMock(return_value={"bid": 99.0, "ask": 99.1, "last": 99.05})
    return ex


def _make_position(**overrides):
    defaults = dict(
        symbol="TEST/USDT", side="long", entry_price=100.0, amount=1.0,
        strategy_name="test_strategy",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_fake_self(db, exchange):
    fake_self = SimpleNamespace()
    fake_self.db = db
    fake_self.exchange = exchange
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = OrderExecutionManager(exchange=exchange, db=db)
    fake_self._close_retry_count = {}
    fake_self._closing_lock = asyncio.Lock()
    fake_self._closing_symbols = set()
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )
    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()

    async def _bound_do_close_position(*a, **kw):
        return await TradingBot._do_close_position(fake_self, *a, **kw)
    fake_self._do_close_position = _bound_do_close_position

    async def _bound_verify(*a, **kw):
        return await TradingBot._verify_position_exists_at_exchange(fake_self, *a, **kw)
    fake_self._verify_position_exists_at_exchange = _bound_verify

    async def _bound_sync(*a, **kw):
        return await TradingBot._sync_db_close_without_order(fake_self, *a, **kw)
    fake_self._sync_db_close_without_order = _bound_sync

    return fake_self


class TestItem15FullRaceWithAllFixesActive(unittest.TestCase):
    """[TAHAP 3 -- bukti akhir #15] Race asli, end-to-end, lewat pipeline
    produksi SUNGGUHAN (bukan stub _simulate_order_fill langsung) --
    Temuan A + Opsi C2 + Opsi C1 semuanya genuinely aktif & berkontribusi
    dalam SATU alur yang sama."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_original_race_now_resolves_correctly_end_to_end(self):
        async def scenario():
            db = await _make_db()
            ex = _make_paper_exchange()

            # ── Setup: posisi LONG 1.0 @ 100 di KEDUA sisi ───────────────
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            self.assertIn("TEST/USDT", ex._paper_positions)

            fake_self = _build_fake_self(db, ex)
            pos = _make_position()

            orders_before_cycle1 = len(ex._paper_orders)

            # ── Siklus SL/TP #1: exchange sukses, DB GAGAL (transien) ───
            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("simulated transient DB failure")),
            ):
                await TradingBot._close_position_market(fake_self, pos, 99.0, "SL hit")

            # Exchange: genuinely closed lewat order reduce_only ASLI
            # (pipeline produksi penuh, C2 melihat posisi ADA di siklus
            # ini -- tidak ada race di titik ini -- order sukses normal).
            self.assertNotIn("TEST/USDT", ex._paper_positions)
            self.assertGreater(
                len(ex._paper_orders), orders_before_cycle1,
                "Siklus #1 seharusnya genuinely mengirim 1 order reduce-only sukses."
            )

            # [BUKTI Temuan A] DB masih is_open=True (write gagal), TAPI
            # is_closing SUDAH direset False -- BUKAN macet True selamanya.
            db_pos_after_cycle1 = await db.get_open_position_by_symbol("TEST/USDT")
            self.assertIsNotNone(db_pos_after_cycle1, "DB masih percaya posisi open (write gagal)")
            self.assertFalse(
                db_pos_after_cycle1.is_closing,
                "[Temuan A] is_closing harus sudah direset False setelah retry exhausted."
            )

            orders_before_cycle2 = len(ex._paper_orders)

            # ── Siklus SL/TP #2 (retrigger realistis, DB msh percaya open) ─
            # Simulasikan TOCTOU race SEMPIT: verify-before-send (C2)
            # melaporkan "masih ada" (snapshot basi) tepat di celah waktu
            # sempit -- representasi race genuinely mungkin di produksi.
            # Backstop C1 (reduce-only di order sungguhan) HARUS menutup
            # celah ini.
            ex.fetch_positions = AsyncMock(return_value=[
                {"symbol": "TEST/USDT", "side": "long", "amount": 1.0},
            ])

            with self.assertLogs("main_future", level="WARNING") as cm:
                await TradingBot._close_position_market(fake_self, pos, 99.0, "SL hit (retry cycle)")

            joined = "\n".join(cm.output)
            self.assertIn(
                "reduce-only DITOLAK", joined,
                "[Bukti C1 genuinely berkontribusi] C2 salah lihat 'ada' (race "
                "disimulasikan) -- C1 HARUS yang menutup celah di titik eksekusi."
            )

            # ── Hasil akhir: DB sinkron, exchange genuinely NOL posisi ──
            self.assertIsNone(
                await db.get_open_position_by_symbol("TEST/USDT"),
                "DB harus tersinkron closed (via _sync_db_close_without_order())."
            )
            self.assertNotIn(
                "TEST/USDT", ex._paper_positions,
                "[BUKTI UTAMA #15] TIDAK ADA posisi arah manapun (long/short) "
                "terbentuk di exchange -- bug asli (posisi short tak "
                "tertracking) TIDAK terjadi lagi."
            )
            # Siklus #2 TIDAK mengirim order baru sama sekali (rejection
            # terjadi DI DALAM upaya kirim order, tapi TIDAK ada fill/posisi
            # baru yang tercatat sbg order sukses -- _paper_orders cuma
            # bertambah utk order yang BERHASIL fill, order yg reject
            # sebelum masuk _paper_orders dict).
            self.assertEqual(
                len(ex._paper_orders), orders_before_cycle2,
                "Siklus #2 tidak boleh menghasilkan order baru yang sukses fill."
            )

            # [Bukti tambahan] _close_retry_count TIDAK bertambah --
            # rejection reduce-only BUKAN diperlakukan sbg kegagalan order
            # biasa (instruksi eksplisit Tahap 2).
            self.assertEqual(fake_self._close_retry_count, {})

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
