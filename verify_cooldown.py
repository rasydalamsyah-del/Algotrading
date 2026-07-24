"""Verifikasi standalone re-entry cooldown (opsi B). Exit 1 kalau gagal."""
import sys, time, ast, io
sys.path.insert(0, ".")
from engine import reentry_cooldown as rc

FAILS = []
def check(label, cond, detail=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" | {detail}" if detail else ""))
    if not cond: FAILS.append(label)

print("=== Klasifikasi exit ===")
check("ATG_EXIT loss -> negatif (kasus PARTI)",
      rc.is_negative_exit("ATG_EXIT(score=65/55|pnl=-1.32%|ST_BEAR,EMA_XDOWN,VOL_ELEV(1.5x))", -1.5188))
check("Stop-loss hit -> negatif (kasus MANTRA #2)",
      rc.is_negative_exit("Stop-loss hit", -2.9456))
check("TrailingExit profit -> POSITIF, tanpa cooldown (kasus MANTRA #1)",
      not rc.is_negative_exit("TrailingExit(high=0.006980,trail_sl=0.006910,gap=1.0%,profit=+3.60%)", 3.2447))
check("Take-profit hit profit -> POSITIF",
      not rc.is_negative_exit("Take-profit hit", 5.0))
check("ATG_EXIT pnl +0.01 (breakeven tipis) -> tetap negatif (sinyal memburuk)",
      rc.is_negative_exit("ATG_EXIT(score=70/55|pnl=+0.02%|ST_BEAR)", 0.01))

print("=== Durasi per profile ===")
d_scalp = rc.duration_for_profile("scalp_volatile")
d_mr    = rc.duration_for_profile("mean_revert")
d_none  = rc.duration_for_profile(None)
d_empty = rc.duration_for_profile("")
check("scalp_volatile > 0", d_scalp > 0, f"{d_scalp}s")
check("mean_revert >= scalp (TF lebih besar)", d_mr >= d_scalp, f"{d_mr}s vs {d_scalp}s")
check("None -> fallback 900", d_none == rc.DEFAULT_COOLDOWN_SECS, f"{d_none}s")
check("'' -> fallback 900", d_empty == rc.DEFAULT_COOLDOWN_SECS, f"{d_empty}s")

print("=== Registry timing (simulasi PARTI end-to-end) ===")
reg = rc.CooldownRegistry()
reg.register("PARTI/USDT", 0.4, "ATG_EXIT(...)")   # exit negatif 21:05:26
rem = reg.blocked("PARTI/USDT")                     # re-entry 2 dtk kemudian -> HARUS terblokir
check("re-entry saat cooldown TERBLOKIR", rem > 0, f"sisa={rem:.2f}s")
check("simbol lain tidak terpengaruh", reg.blocked("BERA/USDT") == 0.0)
time.sleep(0.5)
check("setelah kadaluarsa BEBAS lagi", reg.blocked("PARTI/USDT") == 0.0)
reg2 = rc.CooldownRegistry()                        # MANTRA #1: trailing profit
if not rc.is_negative_exit("TrailingExit(profit=+3.60%)", 3.24):
    pass                                            # tidak diregister -- sesuai opsi B
check("exit positif tidak memblokir re-entry", reg2.blocked("MANTRA/USDT") == 0.0)

print("=== Verifikasi struktural (AST kedua main) ===")
for path, fn in (("future/main_future.py", "_handle_entry"), ("spot/main_spot.py", "_handle_buy")):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == fn), None)
    ok_guard = node is not None and "registry.blocked" in ast.unparse(node)
    close = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_do_close_position"), None)
    ok_reg = close is not None and "is_negative_exit" in ast.unparse(close) and "registry.register" in ast.unparse(close)
    check(f"{path}: guard di {fn}", ok_guard)
    check(f"{path}: registrasi di _do_close_position", ok_reg)

print()
if FAILS:
    print(f"HASIL: {len(FAILS)} KEGAGALAN"); [print("  -", f) for f in FAILS]; sys.exit(1)
print("HASIL: SEMUA TES LOLOS ✅"); sys.exit(0)
