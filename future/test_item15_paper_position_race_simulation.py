"""
future/test_item15_paper_position_race_simulation.py -- Simulasi race
condition/konkurensi utk item audit #15 ("_paper_positions futures adalah
sumber kebenaran ke-3 independen -- urutan operasi close TIDAK atomic
lintas paper-state & DB, mekanisme KONKRET penyebab posisi phantom").

[PENTING -- beda kategori dari #4] Ini BUKAN soal magnitude bias statistik
-- ini soal urutan operasi TIDAK ATOMIC lintas dua sistem (paper-exchange
state vs DB) saat proses close gagal SEBAGIAN. Metodologi data-riil
Binance (dipakai #4) TIDAK relevan di sini -- semua test di bawah pakai
kode produksi ASLI (FutureExchangeConnector._simulate_order_fill(),
DatabaseManager.close_position_with_retry(), TradingBot._do_close_position(),
find_untracked_positions()) dengan kegagalan DB disimulasikan lewat
monkeypatch, BUKAN observasi bot live (bot sedang mati, DB/log sudah
dihapus manual -- lihat CLAUDE.md).

Tiga kelompok test, membangun dari unit -> integrasi penuh:

1. TestPaperPositionReopenOnStaleCloseRetry -- FutureExchangeConnector
   paper mode LANGSUNG (bukan lewat bot). Membuktikan: begitu
   _paper_positions[symbol] sudah dihapus (posisi ditutup), order "close"
   KEDUA untuk symbol yang sama TIDAK ditolak/no-op -- disalahartikan
   sebagai MEMBUKA posisi baru arah berlawanan.

2. TestIsClosingStuckMasksPhantomDetection -- DatabaseManager REAL
   (sqlite in-memory). Membuktikan: kalau close_position_with_retry()
   exhausted (semua retry gagal), is_closing TIDAK PERNAH direset ke
   False -- filter `not p.is_closing` di find_untracked_positions()
   PERMANEN mengecualikan symbol itu dari phantom_candidates, walau
   exchange genuinely sudah flat.

3. TestFullEndToEndCompoundingRaceViaDoClosePosition -- gabungan #1+#2
   lewat TradingBot._do_close_position() (unbound method, pola sama
   dgn future/test_main_future_close_position_retry.py) dipanggil DUA
   KALI berturut (mensimulasikan siklus SL/TP monitor berikutnya
   me-retrigger close pada posisi yang DB-nya masih is_open=True) --
   membuktikan hasil akhir: DB berpikir posisi closed dgn benar, TAPI
   exchange (paper) punya posisi BARU arah berlawanan yang genuinely
   TIDAK diketahui bot (sampai untracked-position sync menangkapnya di
   siklus berikutnya).

    python3 -m unittest future.test_item15_paper_position_race_simulation -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.core.models import SignalType
from engine.database import DatabaseManager
from future.exchange_future import FutureExchangeConnector
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


class _PaperFillExecutorStub:
    """Executor stub yang memanggil _simulate_order_fill() SUNGGUHAN
    (production code, bukan mock) -- bypass slippage/market-filter checks
    milik OrderExecutionManager penuh (di luar cakupan simulasi #15 ini),
    TAPI logic paper-fill (termasuk del self._paper_positions[symbol],
    inti mekanisme race #15) genuinely dieksekusi."""

    def __init__(self, exchange: FutureExchangeConnector):
        self.exchange = exchange

    async def execute_signal(self, signal, assessment):
        side = "buy" if signal.signal_type == SignalType.CLOSE_SHORT else "sell"
        order = await self.exchange._simulate_order_fill(
            symbol=signal.symbol, order_type="market", side=side,
            amount=assessment.approved_size, price=signal.price,
        )
        return SimpleNamespace(executed_price=order["price"], order_id=order["id"])


def _build_fake_self(db, exchange):
    fake_self = SimpleNamespace()
    # [DOUBLE-COUNT FIX] _do_close_position() kini memegang _equity_lock
    # (mirror _handle_entry) -- stub wajib menyediakannya.
    fake_self._equity_lock = asyncio.Lock()
    fake_self.db = db
    fake_self.exchange = exchange
    fake_self.risk_manager = RiskManager({})
    fake_self.executor = _PaperFillExecutorStub(exchange)
    fake_self._close_retry_count = {}
    fake_self._closing_lock = asyncio.Lock()
    fake_self._closing_symbols = set()
    fake_self.notifier = SimpleNamespace(
        notify_error=AsyncMock(), notify_trade_closed=AsyncMock(),
    )
    fake_self._refresh_portfolio = AsyncMock()
    fake_self._reconcile_pending_candidates = AsyncMock()
    # [PENTING] _close_position_market() (dipanggil via unbound method di
    # bawah) internal memanggil self._do_close_position(...) -- SimpleNamespace
    # tidak otomatis bind method class, jadi disambungkan manual di sini
    # supaya _close_position_market() bisa dipanggil apa adanya (bukan cuma
    # _do_close_position() langsung) -- realisme lebih tinggi, termasuk
    # _closing_lock/_closing_symbols/mark_position_closing() yang genuinely
    # dilewati _close_position_market().
    async def _bound_do_close_position(*a, **kw):
        return await TradingBot._do_close_position(fake_self, *a, **kw)
    fake_self._do_close_position = _bound_do_close_position
    # [ITEM #15 -- Temuan C, Opsi C2] _do_close_position() sekarang panggil
    # self._verify_position_exists_at_exchange()/self._sync_db_close_
    # without_order() -- sambungkan manual (SimpleNamespace, sama alasan
    # dgn _do_close_position di atas). Exchange yang dioper KE SINI adalah
    # FutureExchangeConnector paper mode SUNGGUHAN, jadi verify ini genuinely
    # cek _paper_positions asli, bukan mock buatan.
    async def _bound_verify(*a, **kw):
        return await TradingBot._verify_position_exists_at_exchange(fake_self, *a, **kw)
    fake_self._verify_position_exists_at_exchange = _bound_verify

    async def _bound_sync_close(*a, **kw):
        return await TradingBot._sync_db_close_without_order(fake_self, *a, **kw)
    fake_self._sync_db_close_without_order = _bound_sync_close
    return fake_self


# ─────────────────────────────────────────────────────────────────────────
# 1. FutureExchangeConnector paper mode LANGSUNG
# ─────────────────────────────────────────────────────────────────────────

class TestPaperPositionReopenOnStaleCloseRetry(unittest.TestCase):
    """[TEMUAN UTAMA #1] _simulate_order_fill() TIDAK punya konsep
    "reduce-only" -- kalau _paper_positions[symbol] sudah kosong (posisi
    genuinely sudah closed), order "close" berikutnya utk symbol yang
    SAMA disalahartikan sbg MEMBUKA posisi baru arah berlawanan. Ini
    BUKAN unik ke paper-mode -- exchange REAL punya sifat identik (order
    sell tanpa reduceOnly, tanpa posisi existing, akan OPEN SHORT baru di
    exchange manapun) -- dikonfirmasi lewat grep: TIDAK ADA penanganan
    reduceOnly di manapun di execution_base.py/risk_future.py."""

    def test_close_then_second_close_reopens_opposite_side(self):
        async def scenario():
            ex = _make_paper_exchange()

            # Buka LONG 1.0 @ ~100
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            self.assertIn("TEST/USDT", ex._paper_positions)
            self.assertEqual(ex._paper_positions["TEST/USDT"]["side"], "long")

            # Tutup PENUH (sell 1.0) -- posisi genuinely hilang dari _paper_positions
            await ex._simulate_order_fill("TEST/USDT", "market", "sell", 1.0, None)
            self.assertNotIn("TEST/USDT", ex._paper_positions)

            # [RACE #15] Retry-triggered close KEDUA utk symbol yang SAMA
            # (mis. krn DB masih is_open=True stlh attempt pertama gagal
            # tercommit) -- order "sell" yg SAMA dikirim lagi.
            await ex._simulate_order_fill("TEST/USDT", "market", "sell", 1.0, None)

            # BUKAN no-op, BUKAN error -- posisi BARU arah berlawanan
            # (short) diam-diam terbuka.
            self.assertIn(
                "TEST/USDT", ex._paper_positions,
                "Order close kedua seharusnya idealnya no-op/ditolak, "
                "TAPI produksi saat ini membuka posisi baru -- test ini "
                "MENDOKUMENTASIKAN perilaku saat ini, bukan mengklaim benar."
            )
            self.assertEqual(
                ex._paper_positions["TEST/USDT"]["side"], "short",
                "Order 'sell' kedua disalahartikan sbg OPEN SHORT baru, "
                "krn tidak ada existing position utk dibandingkan."
            )

        asyncio.run(scenario())

    def test_partial_close_retry_after_full_close_also_reopens(self):
        """Variasi: retry partial-close (bukan full) setelah full-close
        pertama sudah genuinely menghapus posisi -- hasil SAMA, posisi
        baru arah berlawanan terbuka (bukan reduce dari nol)."""
        async def scenario():
            ex = _make_paper_exchange()
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 2.0, None)
            await ex._simulate_order_fill("TEST/USDT", "market", "sell", 2.0, None)
            self.assertNotIn("TEST/USDT", ex._paper_positions)

            await ex._simulate_order_fill("TEST/USDT", "market", "sell", 0.5, None)
            self.assertIn("TEST/USDT", ex._paper_positions)
            self.assertEqual(ex._paper_positions["TEST/USDT"]["side"], "short")
            self.assertAlmostEqual(ex._paper_positions["TEST/USDT"]["amount"], 0.5, places=6)

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 2. DatabaseManager REAL (sqlite in-memory) -- is_closing stuck forever
# ─────────────────────────────────────────────────────────────────────────

class TestIsClosingStuckMasksPhantomDetection(unittest.TestCase):
    """[TEMUAN A -- DIPERBAIKI] Sebelum fix: close_position() HANYA reset
    is_closing=False di jalur SUKSES -- close_position_with_retry() yang
    exhausted (raise setelah semua retry gagal) TIDAK PERNAH menyentuh
    is_closing, tertinggal True SELAMANYA, memblokir find_untracked_
    positions() (filter `not p.is_closing`) permanen.

    [FIX -- Opsi A2] close_position_with_retry() (engine/database.py)
    sekarang reset is_closing=False lewat _reset_position_closing_flag()
    SETELAH retries habis, SEBELUM raise -- terpusat, berlaku otomatis ke
    kedua bot. Test di bawah SEKARANG mengunci perilaku SETELAH fix
    (sebelumnya mengunci bug -- lihat riwayat file ini kalau perlu
    baca versi lama)."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_is_closing_resets_to_false_after_retry_exhausted(self):
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await db.mark_position_closing("TEST/USDT")

            pos = await db.get_open_position_by_symbol("TEST/USDT")
            self.assertTrue(pos.is_closing)

            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("simulated permanent DB failure")),
            ):
                with self.assertRaises(RuntimeError):
                    await db.close_position_with_retry("TEST/USDT", exit_price=105.0, realized_pnl=5.0)

            pos_after = await db.get_open_position_by_symbol("TEST/USDT")
            self.assertIsNotNone(pos_after, "Posisi HARUS tetap is_open=True (DB gagal ditulis)")
            self.assertFalse(
                pos_after.is_closing,
                "[FIX Temuan A] is_closing HARUS direset ke False setelah retry "
                "exhausted -- supaya phantom detector bisa menangkapnya lagi."
            )

        asyncio.run(scenario())

    def test_reset_symbol_now_included_in_phantom_candidates(self):
        """Integrasi dgn find_untracked_positions() ASLI (future/position_
        sync_futures.py) -- bukan reimplementasi filter is_closing sendiri."""
        from future.position_sync_futures import find_untracked_positions

        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await db.mark_position_closing("TEST/USDT")

            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("simulated permanent DB failure")),
            ):
                with self.assertRaises(RuntimeError):
                    await db.close_position_with_retry("TEST/USDT", exit_price=105.0, realized_pnl=5.0)

            # exchange genuinely FLAT (posisi ini SUDAH tidak ada sama sekali
            # di exchange -- persis skenario phantom yang seharusnya terdeteksi).
            fake_exchange = SimpleNamespace()
            with patch(
                "future.position_sync_futures.fetch_binance_futures_positions",
                new=AsyncMock(return_value=[]),
            ):
                result = await find_untracked_positions(fake_exchange, db)

            self.assertIn(
                "TEST/USDT", result["phantom_candidates"],
                "[FIX Temuan A] symbol yang is_closing-nya sudah direset HARUS "
                "sekarang muncul di phantom_candidates -- mekanisme #10 bisa "
                "menangkapnya lagi di siklus sync berikutnya (masih dgn debounce "
                "2 siklus normal, tidak berubah)."
            )

        asyncio.run(scenario())

    def test_reset_failure_does_not_mask_original_exception(self):
        """[Kehati-hatian eksplisit diminta pemilik proyek] Kalau
        _reset_position_closing_flag() ITU SENDIRI juga gagal (mis. DB
        genuinely down total, bukan cuma transient) -- close_position_
        with_retry() HARUS tetap raise exception ASLI (RuntimeError dari
        close_position()), BUKAN exception dari kegagalan reset."""
        async def scenario():
            db = await _make_db()
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await db.mark_position_closing("TEST/USDT")

            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("original close failure")),
            ), patch.object(
                DatabaseManager, "_reset_position_closing_flag",
                new=AsyncMock(side_effect=RuntimeError("reset ALSO broken")),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    await db.close_position_with_retry("TEST/USDT", exit_price=105.0, realized_pnl=5.0)

            self.assertEqual(str(cm.exception), "original close failure")

        asyncio.run(scenario())


# ─────────────────────────────────────────────────────────────────────────
# 3. End-to-end: TradingBot._do_close_position() dipanggil 2x berturut
# ─────────────────────────────────────────────────────────────────────────

class TestFullEndToEndCompoundingRaceViaDoClosePosition(unittest.TestCase):
    """[TEMUAN C -- DIPERBAIKI, Opsi C2 verify-before-send] Mensimulasikan
    siklus SL/TP monitor: attempt pertama close GAGAL di layer DB (padahal
    exchange sukses) -> posisi tetap is_open=True di DB -> siklus
    berikutnya (5 detik kemudian di produksi) coba close LAGI utk symbol
    yang sama (krn DB masih bilang open).

    SEBELUM fix C2: attempt kedua ini fill di exchange TAPI
    _paper_positions sudah kosong dari attempt pertama -> disalahartikan
    jadi OPEN posisi baru arah berlawanan (BUG lama, lihat riwayat file
    ini kalau perlu baca versi sebelum fix).

    SETELAH fix C2: _do_close_position() SEKARANG verify-before-send --
    cek _verify_position_exists_at_exchange() SEBELUM kirim order apa
    pun. Attempt kedua mendeteksi exchange sudah genuinely flat -> SKIP
    kirim order -> langsung sinkronkan DB via _sync_db_close_without_order()
    -> HASIL AKHIR: DB closed dgn benar, TIDAK ADA posisi baru terbentuk
    di exchange sama sekali (bukan cuma "untracked", genuinely nol).

    [BUKTI EMPIRIS dependency A/C -- bukan asumsi] Fix Temuan A (reset
    is_closing) SUDAH aktif saat test ini jalan (lihat assertFalse utk
    is_closing setelah attempt 1) -- TAPI itu SENDIRI TIDAK mencegah
    kompounding bug (dibuktikan sebelum fix C2 ditambahkan: hasil akhir
    tetap sama walau is_closing sudah direset). C2 genuinely dibutuhkan
    terpisah -- persis seperti dianalisis di Tahap 1 (gerbang retrigger
    di run_sl_tp_monitor() baca _closing_symbols in-memory, bukan kolom
    is_closing ini)."""

    def setUp(self):
        self._sleep_patcher = patch("engine.database.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_two_close_attempts_no_longer_creates_opposite_position(self):
        async def scenario():
            db = await _make_db()
            ex = _make_paper_exchange()

            # Setup: posisi LONG 1.0 @ 100 ada di KEDUA sisi (DB & paper exchange).
            await db.upsert_position("TEST/USDT", {
                "entry_time": _utcnow(), "entry_price": 100.0, "amount": 1.0,
                "side": "long", "is_open": True,
            })
            await ex._simulate_order_fill("TEST/USDT", "market", "buy", 1.0, None)
            self.assertIn("TEST/USDT", ex._paper_positions)

            fake_self = _build_fake_self(db, ex)
            pos = _make_position(symbol="TEST/USDT", side="long", entry_price=100.0, amount=1.0)

            # ── Attempt 1: exchange fill SUKSES, DB write GAGAL ──────────
            with patch.object(
                DatabaseManager, "close_position",
                new=AsyncMock(side_effect=RuntimeError("simulated transient DB failure")),
            ):
                await TradingBot._close_position_market(fake_self, pos, 105.0, "SL hit")

            # Paper exchange: posisi SUDAH hilang (exchange-level genuinely closed).
            self.assertNotIn("TEST/USDT", ex._paper_positions)
            # DB: MASIH is_open=True (write gagal). is_closing sudah direset
            # False oleh fix Temuan A -- TAPI (dibuktikan di bawah) itu
            # SENDIRI tidak cukup; C2 yang genuinely mencegah compounding.
            db_pos_after_attempt1 = await db.get_open_position_by_symbol("TEST/USDT")
            self.assertIsNotNone(db_pos_after_attempt1)
            self.assertFalse(
                db_pos_after_attempt1.is_closing,
                "[FIX Temuan A aktif] is_closing seharusnya sudah direset False."
            )

            # Hitung jumlah order yang benar-benar terkirim ke paper exchange
            # SEBELUM attempt 2 -- utk membuktikan attempt 2 TIDAK menambah
            # order baru sama sekali (bukan cuma "hasil akhirnya kebetulan
            # nol posisi").
            orders_before_attempt2 = len(ex._paper_orders)

            # ── Attempt 2 (siklus SL/TP berikutnya, DB msh percaya open) ─
            # Kali ini DB write SUKSES (masalah transient sudah hilang).
            await TradingBot._close_position_market(fake_self, pos, 106.0, "SL hit (retry cycle)")

            # DB sekarang percaya posisi closed dgn benar.
            db_pos_final = await db.get_open_position_by_symbol("TEST/USDT")
            self.assertIsNone(db_pos_final, "DB seharusnya sekarang is_open=False (attempt 2 tersinkron)")

            # [FIX C2] TIDAK ADA posisi baru terbentuk di exchange -- genuinely
            # kosong, bukan cuma "untracked".
            self.assertNotIn(
                "TEST/USDT", ex._paper_positions,
                "[FIX Temuan C] attempt 2 TIDAK BOLEH membuka posisi baru apa pun "
                "-- verify-before-send seharusnya mendeteksi exchange sudah flat "
                "dan skip pengiriman order sama sekali."
            )
            # [FIX C2] Buktikan TIDAK ADA order baru terkirim sama sekali
            # (bukan cuma "kebetulan side-nya sama lagi") -- jumlah paper
            # order TIDAK bertambah dari attempt 2.
            self.assertEqual(
                len(ex._paper_orders), orders_before_attempt2,
                "[FIX Temuan C] attempt 2 seharusnya SKIP pengiriman order sama "
                "sekali (verify-before-send), bukan cuma kebetulan hasil akhirnya "
                "nol posisi."
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
