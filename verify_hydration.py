"""Simulasi insiden 2026-07-20 21:48: restart paper -> hidrasi -> saldo/posisi kembali."""
import sys
sys.path.insert(0, ".")
from types import SimpleNamespace

FAILS = []
def check(label, cond, detail=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" | {detail}" if detail else ""))
    if not cond: FAILS.append(label)

print("=== SPOT ===")
from spot.exchange_spot import ExchangeConnector as SpotConn
import inspect
sig = inspect.signature(SpotConn.__init__)
kw = {}
for n, p in sig.parameters.items():
    if n == "self": continue
    if "paper" in n: kw[n] = True
    elif "initial" in n: kw[n] = 1000.0
    elif "quote" in n: kw[n] = "USDT"
    elif p.default is inspect.Parameter.empty: kw[n] = None
spot = SpotConn(**kw)
# DB pasca-insiden: BERA & JTO open (persis baseline 21:46)
db_positions = [
    SimpleNamespace(symbol="BERA/USDT", amount=534.336, entry_price=0.187, side="long", leverage=1),
    SimpleNamespace(symbol="JTO/USDT",  amount=160.0,   entry_price=0.6249, side="long", leverage=1),
]
n = spot.hydrate_from_positions(db_positions)
bal = spot._paper_balance
check("2 posisi terhidrasi", n == 2, f"n={n}")
check("BERA balance terisi", abs(bal.get("BERA", 0) - 534.336) < 1e-6, f"{bal.get('BERA')}")
check("JTO balance terisi",  abs(bal.get("JTO", 0)  - 160.0)  < 1e-6, f"{bal.get('JTO')}")
exp_usdt = 1000.0 - 534.336*0.187 - 160.0*0.6249
check("USDT terdebit cost basis", abs(bal.get("USDT", 0) - exp_usdt) < 0.01, f"{bal.get('USDT'):.4f} vs {exp_usdt:.4f}")
# Inti insiden: reconcile bertanya saldo -> sekarang TIDAK nol
check("reconcile tidak akan menutup (saldo >= threshold 1%)", bal.get("BERA", 0) >= 534.336*0.01)

print("=== FUTURES ===")
from future.exchange_future import FutureExchangeConnector as FutConn
sig = inspect.signature(FutConn.__init__)
kw = {}
for n_, p in sig.parameters.items():
    if n_ == "self": continue
    if "paper" in n_: kw[n_] = True
    elif "initial" in n_: kw[n_] = 1000.0
    elif "quote" in n_: kw[n_] = "USDT"
    elif p.default is inspect.Parameter.empty: kw[n_] = None
fut = FutConn(**kw)
db_positions_f = [
    SimpleNamespace(symbol="WIF/USDT", amount=648.0, entry_price=0.1542, side="long", leverage=5),
    SimpleNamespace(symbol="AIGENSYN/USDT", amount=4176.0, entry_price=0.02393, side="long", leverage=9),
]
n = fut.hydrate_from_positions(db_positions_f)
check("2 posisi futures terhidrasi", n == 2, f"n={n}")
check("WIF ada di _paper_positions", "WIF/USDT" in fut._paper_positions)
check("AIGENSYN ada di _paper_positions", "AIGENSYN/USDT" in fut._paper_positions)
exp_margin = 648.0*0.1542/5 + 4176.0*0.02393/9
mb = fut._paper_margin_balance
check("margin terdebit benar (~31.09 spt locked_balance asli)",
      abs((1000.0 - mb) - exp_margin) < 0.01, f"debit={1000.0-mb:.4f} vs {exp_margin:.4f}")
lev = fut._paper_positions["WIF/USDT"]["leverage"]
check("leverage WIF terekonstruksi 5x", lev == 5, f"{lev}")

print()
if FAILS:
    print(f"HASIL: {len(FAILS)} KEGAGALAN"); [print("  -", f) for f in FAILS]; sys.exit(1)
print("HASIL: SEMUA TES LOLOS ✅"); sys.exit(0)
