"""
Ekstrak peta jalur komunikasi (call graph) NYATA dari 34 file yang sudah diaudit,
menggunakan ast module Python -- bukan ditulis manual supaya akurat 1:1 dengan kode asli.

Untuk tiap file:
  - docstring modul (kalau ada) sebagai "tujuan_file"
  - daftar import (from X import Y / import X) -> "imports"
  - daftar class & fungsi/method top-level dengan:
      - line number
      - args
      - docstring baris pertama (kalau ada)
      - "calls_out": daftar nama fungsi/method yang dipanggil DI DALAM body fungsi ini
        (hasil analisis ast.Call -> resolve Name/Attribute jadi string dotted, mis.
        "self.db.save_trade", "get_dynamic_threshold", "self.exchange.create_order")

Setelah AST per-file selesai, dibangun index terbalik ("dipanggil_dari") dengan
grep sederhana: untuk tiap nama fungsi/method (bagian akhir setelah titik terakhir),
cari file APA SAJA (selain file definisinya sendiri) yang menyebut nama itu --
ini index best-effort (nama umum seperti "get" bisa banyak match), tapi tetap
berguna sebagai sinyal awal untuk bug hunter menelusuri caller.
"""
import ast
import json
import os
import re

FILES = [
    "database.py","exchange.py","execution.py","risk.py","strategy.py","notifications.py",
    "intelligence/commander.py","intelligence/position_sync.py","intelligence/trade_guardian.py",
    "learning/analytics.py","learning/coin_swap.py","learning/cross_learn.py","learning/meta_learner.py",
    "profiles/registry.py","profiles/thresholds.py","telegram_bot.py","smoke_api.py","api_server.py",
    "core/models.py","indicators/momentum.py","indicators/orderbook.py","indicators/oscillators.py",
    "indicators/patterns.py","indicators/strength.py","indicators/structure.py","indicators/trend.py",
    "indicators/volatility.py","intelligence/classifier.py","intelligence/observer.py",
    "intelligence/scorer.py","intelligence/validator.py","main.py","profiles/base_profile.py",
    "profiles/weights.py","ta_compat.py",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dotted_name(node):
    """Resolve ast.Attribute/ast.Name chain jadi string dotted, mis. self.db.save_trade"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        # pola: foo()().bar -- jarang, ambil placeholder
        parts.append("<call>")
    else:
        parts.append("<expr>")
    return ".".join(reversed(parts))


def extract_calls(fn_node):
    calls = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            fname = dotted_name(node.func) if isinstance(node.func, (ast.Attribute, ast.Name)) else None
            if fname:
                calls.append(fname)
    # unique, preserve order
    seen = set()
    out = []
    for c in calls:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def get_docstring_summary(node, relpath=None):
    doc = ast.get_docstring(node)
    if not doc:
        return None
    lines = [l.strip() for l in doc.strip().split("\n") if l.strip()]
    if not lines:
        return None
    first = lines[0]
    # Kalau baris pertama cuma nama file itu sendiri (kurang informatif),
    # gabungkan dengan baris berikutnya biar tujuan_file lebih jelas.
    if relpath and first == os.path.basename(relpath) and len(lines) > 1:
        return f"{first} — {lines[1]}"
    return first


def analyze_file(relpath):
    path = os.path.join(ROOT, relpath)
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=relpath)
    except SyntaxError as e:
        return {"error": f"SyntaxError: {e}"}

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names]
            imports.append(f"from {mod} import {', '.join(names)}")
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
            imports.append(f"import {', '.join(names)}")

    functions = {}

    def visit_functions(node, class_prefix=None):
        for child in node.body if hasattr(node, "body") else []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                full_name = f"{class_prefix}.{child.name}" if class_prefix else child.name
                args = [a.arg for a in child.args.args]
                functions[full_name] = {
                    "line": child.lineno,
                    "async": isinstance(child, ast.AsyncFunctionDef),
                    "args": args,
                    "docstring": get_docstring_summary(child),
                    "calls_out": extract_calls(child),
                }
            elif isinstance(child, ast.ClassDef):
                visit_functions(child, class_prefix=child.name)

    visit_functions(tree)

    return {
        "tujuan_file": get_docstring_summary(tree, relpath),
        "imports": imports,
        "jumlah_fungsi": len(functions),
        "fungsi": functions,
    }


def build_reverse_index(per_file_data):
    """
    Untuk tiap fungsi/method (nama akhir saja, tanpa prefix class/self),
    grep di SEMUA file .py di repo (bukan cuma 34 file ini) untuk cari
    kemunculan nama itu di file LAIN selain file definisinya -- sinyal
    awal caller (best-effort, nama umum bisa banyak match).
    """
    # Kumpulkan semua nama fungsi terdefinisi beserta file asalnya
    all_py_files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if "/.git" in dirpath or dirpath.rstrip("/").endswith("/tools") or "/tools/" in dirpath:
            continue
        dirnames[:] = [d for d in dirnames if d not in (".git", "tools", "__pycache__")]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                all_py_files.append(rel)

    file_contents = {}
    for rel in all_py_files:
        try:
            with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
                file_contents[rel] = f.read()
        except Exception:
            pass

    reverse = {}  # (file, func_full_name) -> list of files that reference short name
    for relpath, data in per_file_data.items():
        if "fungsi" not in data:
            continue
        for full_name, meta in data["fungsi"].items():
            short = full_name.split(".")[-1]
            if len(short) < 4 or short.startswith("_") and len(short) < 6:
                pass  # tetap dicek, cuma nama pendek/underscore sering noisy -- biarkan tapi ditandai
            # [BUG-FIX] Sebelumnya regex negative lookbehind "(?<![\w.])" ikut
            # meng-exclude karakter titik SEBELUM nama fungsi — padahal pola
            # pemanggilan method paling umum justru PERSIS "self.executor.
            # execute_signal(" yang punya titik tepat sebelum nama fungsi.
            # Akibatnya SEMUA pemanggilan method via dot-notation (mayoritas
            # pemanggilan di codebase ini!) gagal terdeteksi -> referenced_in_files
            # kosong padahal caller-nya nyata ada (kefatalan: false negative,
            # bug hunter bisa salah simpulkan "tidak ada caller" padahal ada).
            # Sekarang: hanya exclude word-char sebelum nama (supaya tidak match
            # sebagai substring nama lain, mis. "get" di dalam "reset"), titik
            # tetap diizinkan karena itu justru pola valid.
            pattern = re.compile(r"(?<![\w])" + re.escape(short) + r"\s*\(")
            hits = []
            for other_rel, content in file_contents.items():
                if other_rel == relpath:
                    continue
                if pattern.search(content):
                    hits.append(other_rel)
            reverse[(relpath, full_name)] = sorted(hits)
    return reverse


def main():
    per_file_data = {}
    for relpath in FILES:
        per_file_data[relpath] = analyze_file(relpath)

    reverse = build_reverse_index(per_file_data)

    for relpath, data in per_file_data.items():
        if "fungsi" not in data:
            continue
        for full_name, meta in data["fungsi"].items():
            meta["referenced_in_files"] = reverse.get((relpath, full_name), [])

    output = {
        "_meta": {
            "nama_file": "jalur.json",
            "tujuan": (
                "Peta jalur komunikasi NYATA (call graph) hasil ekstraksi AST langsung dari kode, "
                "bukan ditulis manual -- supaya akurat 1:1. Untuk tiap fungsi: 'calls_out' adalah "
                "daftar fungsi/method yang DIPANGGIL oleh fungsi ini (dari analisis ast.Call di "
                "dalam body-nya sendiri, jadi ini akurat penuh). 'referenced_in_files' adalah hasil "
                "grep best-effort nama fungsi (bagian akhir setelah titik) di SEMUA file .py lain di "
                "repo -- pendekatan ini BISA false-positive untuk nama umum (mis. 'get', 'run', "
                "'process') karena grep tidak resolve tipe/namespace, tapi tetap berguna sebagai "
                "titik awal investigasi 'siapa mungkin memanggil fungsi ini'. WAJIB diverifikasi "
                "manual (baca kode asli di file yang disebut) sebelum disimpulkan sebagai caller "
                "yang benar."
            ),
            "cara_pakai_bug_hunter": [
                "1. Pilih file dari hunter_bug.json sesuai urutan tier.",
                "2. Buka entry file itu di jalur.json ini.",
                "3. Untuk tiap fungsi kritis (terutama yang disebut di 'fokus_cross_check' hunter_bug.json), baca 'calls_out' -- pastikan setiap pemanggilan itu VALID: fungsi yang dipanggil benar-benar ada, signature (jumlah/nama parameter) cocok, dan tipe return yang diasumsikan caller cocok dengan yang benar-benar dikembalikan.",
                "4. Baca 'referenced_in_files' -- untuk tiap file yang disebut, BUKA file itu dan cari baris yang benar-benar memanggil fungsi ini (grep manual dgn context), lalu verifikasi caller memakai hasilnya dengan cara yang konsisten dengan implementasi SAAT INI (bukan implementasi lama yang mungkin sudah berubah).",
                "5. Kalau ketemu mismatch (signature beda, return type diasumsikan salah, efek samping tidak dipakai/di-handle), itu kandidat bug -- validasi dengan test fungsional sebelum fix, ikuti protokol yang sama seperti sesi audit sebelumnya (baca AUDIT_STATE.json dulu, jangan mengulang temuan yang sudah ada)."
            ],
            "keterbatasan": (
                "AST calls_out hanya menangkap pemanggilan LANGSUNG dalam body fungsi (termasuk yang "
                "di dalam try/except/loop/comprehension di dalamnya), TIDAK menangkap: pemanggilan "
                "dinamis lewat getattr()/eval(), callback yang disimpan sebagai variabel dan dipanggil "
                "nanti di tempat lain (mis. on_trade_executed callback pattern), atau pemanggilan lewat "
                "HTTP/subprocess (telegram_bot.py <-> api_server.py, yang jalur endpoint-nya SUDAH "
                "dipetakan manual saat audit telegram_bot.py, lihat AUDIT_STATE.json)."
            )
        },
        "peta_per_file": per_file_data,
    }

    with open(os.path.join(ROOT, "jalur.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    total_fn = sum(d.get("jumlah_fungsi", 0) for d in per_file_data.values())
    print(f"jalur.json dibuat: {len(FILES)} file, {total_fn} fungsi/method total")


if __name__ == "__main__":
    main()
