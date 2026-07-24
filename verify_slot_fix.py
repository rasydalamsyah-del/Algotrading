"""
verify_slot_fix.py -- Verifikasi standalone (tanpa pytest) untuk SLOT-LEAK FIX.

Menguji, pada RiskManager FUTURES dan SPOT:
  (a) evaluate_order(reserve_slot=False)  -> counter TIDAK naik (mode probe)
  (b) evaluate_order() default            -> counter NAIK tepat 1 (mode eksekusi)
  (c) release_position_slot()             -> counter kembali seimbang (simulasi entry gagal)

Exit code 0 = semua lolos. Exit code 1 = ada kegagalan.
Jalankan dari root project: python3 verify_slot_fix.py
"""
import asyncio
import inspect
import sys

sys.path.insert(0, ".")

CONFIG = {
    "max_open_positions":     3,
    "max_position_size_pct":  50.0,
    "min_order_value_usdt":   1.0,
    "max_drawdown_pct":       99.0,
    "daily_loss_limit_pct":   99.0,
    "default_leverage":       5,
    "max_leverage":           20,
}

FAILS = []

def check(label, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" | {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)

def build_manager(cls):
    """Instansiasi RiskManager dgn introspeksi signature -- config diisi,
    parameter wajib lain diisi None (event_bus dll tidak dipakai di jalur
    evaluate_order yang kita uji)."""
    sig = inspect.signature(cls.__init__)
    kwargs = {}
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if name == "config":
            kwargs[name] = dict(CONFIG)
        elif p.default is inspect.Parameter.empty:
            kwargs[name] = None
    return cls(**kwargs)

def prime_portfolio(rm):
    """Set equity/balance via update_portfolio_state dgn introspeksi signature."""
    sig = inspect.signature(rm.update_portfolio_state)
    kwargs = {}
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if "initial" in name:
            kwargs[name] = 1000.0
        elif "equity" in name:
            kwargs[name] = 1000.0
        elif "free" in name or "balance" in name:
            kwargs[name] = 1000.0
        elif "count" in name or "position" in name:
            kwargs[name] = 0
        elif p.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[name] = 0
    rm.update_portfolio_state(**kwargs)

async def test_futures():
    print("\n=== FUTURES: future.risk_future.RiskManager ===")
    from future.risk_future import RiskManager
    rm = build_manager(RiskManager)
    prime_portfolio(rm)

    base_kwargs = dict(
        symbol="TEST/USDT", side="buy", price=1.0, quantity=100.0,
        leverage=5, existing_position_side=None,
        stop_loss=None, take_profit=None, atr=None,
    )

    # (a) PROBE: reserve_slot=False tidak menaikkan counter
    before = rm._open_positions_count
    a = await rm.evaluate_order(**base_kwargs, reserve_slot=False)
    check("(a) probe approved (prasyarat tes valid)", a.is_approved, f"decision={a.decision} reason={a.reason}")
    check("(a) probe TIDAK menaikkan counter",
          rm._open_positions_count == before,
          f"before={before} after={rm._open_positions_count}")

    # (b) DEFAULT (eksekusi): counter naik tepat 1
    before = rm._open_positions_count
    b = await rm.evaluate_order(**base_kwargs)
    check("(b) eksekusi approved (prasyarat tes valid)", b.is_approved, f"decision={b.decision} reason={b.reason}")
    check("(b) eksekusi default menaikkan counter tepat 1",
          rm._open_positions_count == before + 1,
          f"before={before} after={rm._open_positions_count}")

    # (c) RELEASE: simulasi entry gagal -> seimbang kembali
    before = rm._open_positions_count
    rm.release_position_slot()
    check("(c) release mengembalikan counter tepat 1",
          rm._open_positions_count == before - 1,
          f"before={before} after={rm._open_positions_count}")

    # (d) SKENARIO ADA: 3x probe beruntun saat 2 posisi terbuka
    #     -> counter TETAP 2, eksekusi ke-3 TIDAK tertolak oleh probe.
    prime_portfolio(rm)
    rm._open_positions_count = 2
    for _ in range(3):
        await rm.evaluate_order(**base_kwargs, reserve_slot=False)
    check("(d) 3x probe saat 2/3 posisi: counter tetap 2 (regresi kasus ADA)",
          rm._open_positions_count == 2,
          f"count={rm._open_positions_count}")
    d = await rm.evaluate_order(**base_kwargs)
    check("(d) eksekusi setelah probe TIDAK tertolak Max-open-positions",
          d.is_approved, f"decision={d.decision} reason={d.reason}")
    check("(d) counter jadi 3 hanya karena eksekusi, bukan probe",
          rm._open_positions_count == 3,
          f"count={rm._open_positions_count}")

async def test_spot():
    print("\n=== SPOT: spot.risk_spot.RiskManager ===")
    from spot.risk_spot import RiskManager
    rm = build_manager(RiskManager)
    prime_portfolio(rm)

    base_kwargs = dict(
        symbol="TEST/USDT", side="buy", price=1.0, quantity=100.0,
        stop_loss=None, take_profit=None, atr=None,
    )

    before = rm._open_positions_count
    a = await rm.evaluate_order(**base_kwargs, reserve_slot=False)
    check("(a) probe approved (prasyarat tes valid)", a.is_approved, f"decision={a.decision} reason={a.reason}")
    check("(a) probe TIDAK menaikkan counter",
          rm._open_positions_count == before,
          f"before={before} after={rm._open_positions_count}")

    before = rm._open_positions_count
    b = await rm.evaluate_order(**base_kwargs)
    check("(b) eksekusi approved (prasyarat tes valid)", b.is_approved, f"decision={b.decision} reason={b.reason}")
    check("(b) eksekusi default menaikkan counter tepat 1",
          rm._open_positions_count == before + 1,
          f"before={before} after={rm._open_positions_count}")

    before = rm._open_positions_count
    rm.release_position_slot()
    check("(c) release mengembalikan counter tepat 1",
          rm._open_positions_count == before - 1,
          f"before={before} after={rm._open_positions_count}")

async def main():
    await test_futures()
    await test_spot()
    print()
    if FAILS:
        print(f"HASIL: {len(FAILS)} KEGAGALAN:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("HASIL: SEMUA TES LOLOS ✅")
    sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
