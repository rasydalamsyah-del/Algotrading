"""
engine/test_risk_base.py -- Test untuk reserve_position_slot()/release_position_slot()
(engine/risk_base.py) dan penutupan race _open_positions_count di
spot/risk_spot.py + future/risk_future.py.

[ITEM #2 -- audit fungsional] _open_positions_count sebelumnya cuma
diperbarui lewat _refresh_portfolio() (DB re-fetch, di-trigger di close/tiap
900 detik) -- TIDAK PERNAH langsung saat entry disetujui. Worker pool
GATE3_WORKERS (default 3, konkuren per simbol) bisa membuat beberapa
evaluate_order() APPROVE bersamaan terhadap counter yang sama-sama basi,
melewati max_open_positions.

Fix (Opsi 1+2, disepakati):
- Opsi 1: reserve_position_slot() dipanggil ATOMIK di tail
  _evaluate_order_locked() (spot & futures), masih di dalam _evaluate_lock
  yang sama dengan pengecekan max_open_positions -- menutup race SEPENUHNYA
  untuk evaluate_order(), terlepas apakah caller-nya genuinely konkuren atau
  cuma sequential-tapi-cepat (bug lama muncul di KEDUA kasus, krn akar
  masalahnya TOCTOU lintas panggilan, bukan interleaving murni).
- Opsi 2: _refresh_portfolio() dipanggil di akhir entry sukses (main_spot.py/
  main_future.py) -- lapisan tambahan, diuji terpisah di
  test_main_spot_entry_slot_reservation.py / test_main_future_entry_slot_reservation.py.

File ini fokus ke Opsi 1: reserve/release di level RiskManager itu sendiri,
lewat evaluate_order() SUNGGUHAN (bukan reimplementasi logic gate).

    python3 -m unittest engine.test_risk_base -v
"""

from __future__ import annotations

import asyncio
import unittest

from engine.risk_base import BaseRiskManager, RiskDecision
from future.risk_future import RiskManager as FuturesRiskManager
from spot.risk_spot import RiskManager as SpotRiskManager


def _make_ready_rm(cls, max_open: int, open_count: int = 0, equity: float = 10000.0):
    rm = cls({"max_open_positions": max_open})
    rm.update_portfolio_state(
        equity=equity, initial_equity=equity,
        free_balance=equity, open_positions_count=open_count,
    )
    return rm


class TestReservePositionSlotUnit(unittest.TestCase):
    """Section 1 -- unit-level reserve_position_slot()/release_position_slot()
    langsung di BaseRiskManager, tanpa lewat evaluate_order()."""

    def test_reserve_increments_by_one(self):
        rm = BaseRiskManager({})
        self.assertEqual(rm._open_positions_count, 0)
        rm.reserve_position_slot()
        self.assertEqual(rm._open_positions_count, 1)
        rm.reserve_position_slot()
        self.assertEqual(rm._open_positions_count, 2)

    def test_release_decrements_by_one(self):
        rm = BaseRiskManager({})
        rm._open_positions_count = 3
        rm.release_position_slot()
        self.assertEqual(rm._open_positions_count, 2)

    def test_release_at_zero_floors_and_logs_warning(self):
        """Invariant: counter TIDAK BOLEH negatif (akan membuat gate
        max_open_positions salah LONGGAR, lebih berbahaya drpd salah
        ketat) -- floor di 0 + warning sbg sinyal reserve/release tidak
        seimbang di caller."""
        rm = BaseRiskManager({})
        self.assertEqual(rm._open_positions_count, 0)
        with self.assertLogs("risk_base", level="WARNING") as cm:
            rm.release_position_slot()
        self.assertEqual(rm._open_positions_count, 0)
        self.assertIn("tidak seimbang", "\n".join(cm.output))


class TestEvaluateOrderReservesOnApproval(unittest.TestCase):
    """Section 2 -- evaluate_order() SUNGGUHAN (spot & futures) mereservasi
    slot tepat saat approve, TIDAK saat reject."""

    def test_spot_buy_approved_reserves_slot(self):
        rm = _make_ready_rm(SpotRiskManager, max_open=2)
        assessment = asyncio.run(rm.evaluate_order(
            symbol="TEST/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertTrue(assessment.is_approved)
        self.assertEqual(rm._open_positions_count, 1)

    def test_spot_buy_rejected_by_other_gate_does_not_reserve(self):
        """Reject krn alasan LAIN (bukan max_open_positions) -- mis. halted --
        TIDAK BOLEH ikut mereservasi slot."""
        rm = _make_ready_rm(SpotRiskManager, max_open=2)
        rm.halt_trading()
        assessment = asyncio.run(rm.evaluate_order(
            symbol="TEST/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertFalse(assessment.is_approved)
        self.assertEqual(rm._open_positions_count, 0)

    def test_spot_sell_never_reserves(self):
        """side="sell" di spot SELALU menutup posisi existing, bukan
        posisi baru -- tidak boleh ikut menaikkan counter."""
        rm = _make_ready_rm(SpotRiskManager, max_open=2)
        assessment = asyncio.run(rm.evaluate_order(
            symbol="TEST/USDT", side="sell", price=10.0, quantity=10.0,
        ))
        self.assertTrue(assessment.is_approved)
        self.assertEqual(rm._open_positions_count, 0)

    def test_futures_opening_new_approved_reserves_slot(self):
        rm = _make_ready_rm(FuturesRiskManager, max_open=2)
        assessment = asyncio.run(rm.evaluate_order(
            symbol="TEST/USDT", side="buy", price=10.0, quantity=10.0,
            leverage=5, existing_position_side=None,
        ))
        self.assertTrue(assessment.is_approved)
        self.assertEqual(rm._open_positions_count, 1)

    def test_futures_closing_or_reducing_does_not_reserve(self):
        """existing_position_side diisi & berlawanan arah -> close/reduce,
        tidak konsumsi slot baru (lihat is_closing_or_reducing di
        risk_future.py)."""
        rm = _make_ready_rm(FuturesRiskManager, max_open=2, open_count=1)
        assessment = asyncio.run(rm.evaluate_order(
            symbol="TEST/USDT", side="sell", price=10.0, quantity=10.0,
            leverage=5, existing_position_side="long",
        ))
        self.assertTrue(assessment.is_approved)
        self.assertEqual(rm._open_positions_count, 1, "count tidak boleh naik utk close/reduce")


class TestConcurrentEvaluateOrderRaceClosure(unittest.TestCase):
    """Section 3 -- [REGRESI UTAMA, skenario persis dari audit] Simulasi
    N evaluate_order() "bersamaan" (asyncio.gather) utk simbol berbeda-beda,
    TANPA _refresh_portfolio() di antaranya -- persis situasi GATE3_WORKERS
    berlomba dalam window sebelum DB re-fetch berikutnya. Sebelum fix:
    SEMUA bisa approved (counter tidak pernah naik di antara panggilan).
    Sesudah fix: PERSIS sebanyak slot yang tersedia yang approved."""

    def test_spot_five_concurrent_buys_two_slots_only_two_approved(self):
        rm = _make_ready_rm(SpotRiskManager, max_open=2, open_count=0)

        async def _run():
            return await asyncio.gather(*[
                rm.evaluate_order(symbol=f"SYM{i}/USDT", side="buy", price=10.0, quantity=10.0)
                for i in range(5)
            ])

        results = asyncio.run(_run())
        approved = [r for r in results if r.is_approved]
        rejected = [r for r in results if not r.is_approved]
        self.assertEqual(
            len(approved), 2,
            "Harus PERSIS sebanyak slot yang tersedia (2) walau 5 evaluate_order() "
            "dipanggil 'bersamaan' -- sebelum fix ini bisa 5/5 approved (over-limit).",
        )
        self.assertEqual(len(rejected), 3)
        self.assertEqual(rm._open_positions_count, 2)
        for r in rejected:
            self.assertIn("Max open positions", r.reason)

    def test_futures_five_concurrent_opens_two_slots_only_two_approved(self):
        rm = _make_ready_rm(FuturesRiskManager, max_open=2, open_count=0)

        async def _run():
            return await asyncio.gather(*[
                rm.evaluate_order(
                    symbol=f"SYM{i}/USDT", side="buy", price=10.0, quantity=10.0,
                    leverage=5, existing_position_side=None,
                ) for i in range(5)
            ])

        results = asyncio.run(_run())
        approved = [r for r in results if r.is_approved]
        rejected = [r for r in results if not r.is_approved]
        self.assertEqual(len(approved), 2)
        self.assertEqual(len(rejected), 3)
        self.assertEqual(rm._open_positions_count, 2)
        for r in rejected:
            self.assertEqual(r.decision, RiskDecision.REJECTED_INSUFFICIENT_CAPITAL)

    def test_spot_race_closure_at_various_capacities(self):
        """[Sanity tambahan] Bukan cuma 1 titik data -- capacity 0, 1, 3
        slot tersisa dari 5 kandidat, hasil approved harus PERSIS = capacity."""
        for max_open, open_count in [(0, 0), (1, 0), (3, 0), (3, 2)]:
            with self.subTest(max_open=max_open, open_count=open_count):
                remaining_capacity = max(0, max_open - open_count)
                rm = _make_ready_rm(SpotRiskManager, max_open=max_open, open_count=open_count)

                async def _run():
                    return await asyncio.gather(*[
                        rm.evaluate_order(symbol=f"SYM{i}/USDT", side="buy", price=10.0, quantity=10.0)
                        for i in range(5)
                    ])

                results = asyncio.run(_run())
                approved = [r for r in results if r.is_approved]
                self.assertEqual(len(approved), min(remaining_capacity, 5))


class TestReleasePositionSlotIntegration(unittest.TestCase):
    """Section 4 -- release_position_slot() dipanggil manual (simulasi
    caller _handle_buy()/_handle_entry() setelah execute_signal() gagal)
    mengembalikan counter ke nilai sebelum reserve, dan slot itu bisa
    dipakai lagi oleh evaluate_order() berikutnya."""

    def test_spot_release_after_approval_frees_slot_for_next_call(self):
        rm = _make_ready_rm(SpotRiskManager, max_open=1, open_count=0)

        first = asyncio.run(rm.evaluate_order(
            symbol="FIRST/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertTrue(first.is_approved)
        self.assertEqual(rm._open_positions_count, 1)

        # Slot penuh -- kandidat kedua HARUS ditolak selama belum di-release.
        second_before_release = asyncio.run(rm.evaluate_order(
            symbol="SECOND/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertFalse(second_before_release.is_approved)

        # Simulasi execute_signal() gagal untuk FIRST -- caller release.
        rm.release_position_slot()
        self.assertEqual(rm._open_positions_count, 0)

        # Sekarang kandidat lain HARUS bisa lolos lagi.
        third_after_release = asyncio.run(rm.evaluate_order(
            symbol="THIRD/USDT", side="buy", price=10.0, quantity=10.0,
        ))
        self.assertTrue(third_after_release.is_approved)
        self.assertEqual(rm._open_positions_count, 1)


if __name__ == "__main__":
    unittest.main()
