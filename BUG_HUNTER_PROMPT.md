# PROMPT BUG HUNTER LANJUTAN — COPY PASTE INI KE SESI CLAUDE BARU

Ini BUKAN audit dari nol. Ini adalah **cross-check tingkat tinggi** di atas audit yang
sudah selesai (semua 34 file inti + main.py sudah pernah dibaca penuh, lihat
AUDIT_STATE.json). Misi sesi ini: cari bug yang LOLOS dari audit pertama karena
audit pertama membaca file SATU PER SATU secara berurutan — sesi ini membaca file
DUA ARAH sekaligus (kode yang dipanggil DAN kode yang memanggil) memakai peta
jalur komunikasi yang sudah diekstrak langsung dari kode asli.

---

## SETUP — JALANKAN INI DULU

```bash
export GH_TOKEN=   # minta token baru ke user kalau kosong/expired
git clone https://x-access-token:${GH_TOKEN}@github.com/rasydalamsyah-del/algotrader /home/claude/algotrader
cd /home/claude/algotrader
git config user.email "rasydalamsyah@gmail.com"
git config user.name "rasydalamsyah-del"
git remote set-url origin https://x-access-token:${GH_TOKEN}@github.com/rasydalamsyah-del/algotrader.git
```

Baca 3 file berikut SEBELUM melakukan apa pun, dalam urutan ini:

1. **`AUDIT_STATE.json`** — histori audit kronologis. Untuk tiap file, berisi
   `bug_fixed`, `cross_file_check`, `catatan_investigasi` yang SUDAH ditemukan.
   **JANGAN mengulang temuan yang sudah tercatat di sini** — itu buang-buang waktu
   dan bisa membingungkan (seolah bug baru padahal sudah lama diketahui & difix).
2. **`hunter_bug.json`** — urutan prioritas cross-check berdasarkan BLAST RADIUS
   (jalur uang dulu → fondasi matematika → keputusan → config → pembelajaran
   adaptif → persistensi → orkestrator), BUKAN urutan kronologis. Field `status`
   per file menunjukkan progress cross-check (PENDING/IN_PROGRESS/CROSS_CHECKED_CLEAN/
   CROSS_CHECKED_FIXED). Field `fokus_cross_check` per file adalah HIPOTESIS AWAL —
   titik paling rawan yang sudah teridentifikasi — pakai sebagai starting point,
   BUKAN daftar lengkap (tetap harus baca keseluruhan jalur fungsi file itu).
3. **`jalur.json`** — peta pemanggilan NYATA hasil ekstraksi AST dari kode asli
   (bukan ditulis manual, jadi akurat). Untuk tiap fungsi/method di 34 file:
   - `calls_out`: fungsi/method APA SAJA yang dipanggil fungsi ini (akurat, dari
     analisis AST langsung terhadap body fungsi).
   - `referenced_in_files`: file APA SAJA yang menyebut nama fungsi ini (best-effort
     grep, BISA false-positive untuk nama umum — WAJIB diverifikasi manual, baca
     baris kode asli di file yang disebut, jangan percaya begitu saja).
   - Regenerasi peta ini kalau ada perubahan signature: `python3 tools/build_jalur.py`

```bash
python3 -c "
import json
d = json.load(open('hunter_bug.json'))
for tier in d['urutan_prioritas']:
    pending = [f['file'] for f in tier['files'] if f['status'] == 'PENDING']
    if pending:
        print(f\"Tier {tier['tier']} ({tier['nama_tier']}): {pending}\")
"
```

Lanjutkan dari file PENDING pertama sesuai urutan tier (jangan loncat tier kecuali
diminta user).

---

## FILOSOFI INTI: DUA ARAH, BUKAN SATU ARAH

Audit pertama (yang menghasilkan AUDIT_STATE.json) sebagian besar membaca:
**"file ini benar secara internal, lalu cek apakah fungsi yang DIPANGGIL-nya ada."**

Sesi ini menambahkan arah yang sering terlewat: **"untuk tiap fungsi PUBLIK di file
ini, cek SEMUA tempat yang MEMANGGILNYA — apakah caller itu masih menggunakan hasil/
efek samping fungsi ini dengan cara yang konsisten dengan implementasi SAAT INI?"**

Bug yang paling sering lolos dari audit satu-arah:
- Fungsi A mengubah return type/struktur datanya (mis. dari list jadi dict) saat
  diaudit & difix, tapi salah satu dari 3 caller-nya di file lain tidak diperiksa
  ulang dan masih memperlakukan hasilnya sebagai list.
- Fungsi B menambah parameter baru dengan default value aman, tapi caller yang
  SEHARUSNYA mengirim nilai custom untuk parameter itu tidak pernah diupdate,
  sehingga diam-diam selalu pakai default (kasus nyata: `min_trades_threshold` di
  `database.py` — sudah ditemukan & didokumentasikan, JADIKAN CONTOH pola yang dicari).
- Fungsi C dipanggil dari 2 tempat berbeda dengan asumsi field response berbeda
  (kasus nyata: `cmd_approve_suggestion` di `telegram_bot.py` cek field `status`
  yang selalu sama, padahal hasil asli ada di field `applied` — sudah difix,
  JADIKAN CONTOH pola yang dicari di endpoint/fungsi LAIN yang belum dicek).
- Fungsi D disimpan sebagai referensi object sekali di awal (constructor/injection),
  lalu object itu diganti di runtime tapi referensi lama di komponen lain tidak
  diupdate (kasus nyata: `run_config_watcher` reinit exchange/ws_feed tanpa
  propagate ke `executor`/`commander`/`strategy` — sudah difix, JADIKAN CONTOH
  pola "stale reference setelah reinit" yang dicari di tempat lain).

---

## METODOLOGI PER FILE

### Langkah 1 — Ambil konteks
```bash
python3 -c "
import json
d = json.load(open('AUDIT_STATE.json'))
print(json.dumps(d['file_sudah_diaudit_dan_fix'].get('<NAMA_FILE>'), indent=2, ensure_ascii=False))
"
python3 -c "
import json
d = json.load(open('jalur.json'))
fd = d['peta_per_file'].get('<NAMA_FILE>')
print('tujuan:', fd['tujuan_file'])
print('jumlah fungsi:', fd['jumlah_fungsi'])
"
```
Baca juga entry file ini di `hunter_bug.json` untuk `fokus_cross_check`.

### Langkah 2 — Baca file lengkap (kalau belum pernah/lama tidak dibaca)
Sama seperti audit pertama: pahami tujuan, class utama, state, error handling.

### Langkah 3 — Untuk TIAP fungsi publik (dipanggil dari file lain), cek DUA ARAH

**Arah keluar (calls_out di jalur.json):**
- Fungsi yang dipanggil benar-benar ADA (bukan typo/nama lama yang sudah di-rename).
- Signature (jumlah & nama parameter) cocok dengan definisi fungsi tsb saat ini.
- Async/sync konsisten (`await` dipakai kalau target async; tidak dipakai kalau sync).
- Return value yang diasumsikan (tipe, struktur, field) cocok dengan yang BENAR-BENAR
  di-return oleh fungsi saat ini — buka definisi fungsi target, jangan asumsi dari nama.

**Arah masuk (referenced_in_files di jalur.json):**
- Untuk TIAP file yang muncul di `referenced_in_files`, BUKA file itu, cari baris
  yang benar-benar memanggil fungsi ini (`grep -n "<nama_fungsi>(" <file>`).
- Verifikasi manual: apakah ini benar caller asli, atau false-positive grep (nama
  metode lain yang kebetulan sama, komentar, string literal)?
- Kalau caller asli: apakah parameter yang dikirim cocok dengan signature SAAT INI?
- Apakah caller memproses return value/efek samping dengan asumsi yang masih valid?
- KHUSUS untuk fungsi yang baru saja difix di sesi-sesi sebelumnya (cek AUDIT_STATE.json)
  — apakah SEMUA caller sudah ikut diverifikasi ulang, atau cuma satu yang dicek?

### Langkah 4 — Pola bug spesifik yang dicari (selain checklist audit pertama)

- **Stale reference**: object disimpan sekali di constructor/init, lalu sumber
  aslinya diganti di runtime (reconnect, reinit, reload config) tapi referensi
  lama di komponen lain tidak ikut diupdate.
- **Field response ambigu**: endpoint/fungsi selalu return field status yang sama
  ("success"/"ok"/"done") terlepas dari hasil asli, hasil sebenarnya ada di field
  lain yang tidak dicek caller.
- **Parameter hantu**: parameter diterima fungsi tapi tidak pernah dipakai di
  dalam body (dead parameter) — ATAU sebaliknya, caller mengira mengirim override
  tapi fungsi diam-diam pakai sumber lain (env var, config global).
- **Skala/unit mismatch**: satu file pakai skala 0-100, file lain 0-1, atau desimal
  vs persen, tanpa konversi eksplisit di titik pertemuan.
- **Cache/state yang tidak pernah invalidate ATAU tidak pernah dipakai sama sekali**
  (dideklarasikan tapi logic pemakaiannya tidak pernah ditulis — kasus nyata:
  `cross_learn.py`).
- **Guardrail yang bocor untuk sub-kasus tertentu**: guardrail (cooldown, validasi,
  rate limit) berlaku untuk kebanyakan jalur tapi ada 1-2 cabang kode yang return
  lebih awal sebelum sempat kena guardrail (kasus nyata: `meta_learner.py` weight_*
  bypass cooldown).
- **Notifikasi/log yang salah label**: pesan ke user/log menyebut hasil yang tidak
  sesuai kondisi aktual (kasus nyata: trigger `take_profit` di-hardcode padahal
  PnL negatif).

### Langkah 5 — Fix, Test, Dokumentasi (SAMA seperti protokol audit pertama)

1. Tambah komentar `# [BUG-FIX]` di kode: sebelumnya apa, sekarang apa, kenapa.
2. Test: `python3 -m py_compile <file>`, test import, test fungsional (bikin mock/
   simulasi kalau perlu koneksi eksternal seperti exchange/DB/Telegram).
3. Cross-check ulang: `python3 -m py_compile` untuk SEMUA file yang ikut disentuh
   (termasuk file caller kalau ikut difix).
4. **Kalau ada perubahan signature fungsi** (nama parameter, jumlah parameter,
   return type): jalankan ulang `python3 tools/build_jalur.py` supaya `jalur.json`
   tetap akurat untuk sesi berikutnya.
5. Update **KEDUA** file tracking:
   - `AUDIT_STATE.json`: tambahkan ke `bug_fixed_tambahan_dari_audit_file_lain`
     pada entry file yang terdampak (pola yang sama seperti sesi-sesi sebelumnya).
   - `hunter_bug.json`: update `status` file ini jadi `CROSS_CHECKED_FIXED` atau
     `CROSS_CHECKED_CLEAN`, isi field `bug_baru_ditemukan` (tambahkan field ini
     kalau belum ada) dengan ringkasan singkat + referensi ke commit.
6. Commit & push (SELALU `git fetch` dulu):
   ```bash
   git fetch origin
   git log HEAD..origin/main --oneline   # merge dulu kalau ada commit baru
   git add <file_yang_berubah> AUDIT_STATE.json hunter_bug.json jalur.json
   git commit -m "Bug hunt cross-check: <nama_file> — <ringkasan temuan>"
   git push
   ```

---

## FORMAT LAPORAN PER FILE

```
=== BUG HUNT CROSS-CHECK: <nama_file> ===
TIER: <angka> (<nama_tier>)
SUDAH DIAUDIT SEBELUMNYA: <ringkasan singkat dari AUDIT_STATE.json>

CEK ARAH KELUAR (calls_out):
  ✅ Semua pemanggilan valid — atau —
  ❌ Mismatch: <fungsi_ini> memanggil <target> dengan asumsi <X>, tapi <target>
     sebenarnya <Y> [baris N]

CEK ARAH MASUK (referenced_in_files, diverifikasi manual):
  ✅ Semua caller konsisten — atau —
  ❌ <file_caller> memanggil <fungsi> tapi <masalah spesifik> [baris N di file_caller]

POLA BUG SPESIFIK DITEMUKAN:
  <stale reference / field ambigu / parameter hantu / dll, atau "tidak ada">

PERBAIKAN: <N> bug baru, file yang ikut disentuh: <daftar>
TEST: ✅ Syntax OK | ✅ Import OK | ✅ Functional PASS
JALUR.JSON DIREGENERASI: ya/tidak (perlu kalau ada perubahan signature)
PUSH: git commit "<pesan>" ✅

STATUS AKHIR: CROSS_CHECKED_CLEAN / CROSS_CHECKED_FIXED
```

---

## ATURAN KETAT

- **Jangan ulangi temuan lama.** Kalau sebuah "bug" yang ditemukan ternyata sudah
  ada di `bug_fixed`/`catatan_investigasi` AUDIT_STATE.json, itu bukan temuan baru
  — cukup catat "sudah pernah ditemukan & difix di commit sebelumnya, terverifikasi
  masih benar", jangan buat seolah baru.
- **`referenced_in_files` di jalur.json BUKAN kebenaran mutlak** — itu hasil grep
  nama fungsi, bisa salah untuk nama umum/pendek. SELALU buka file aslinya dan baca
  baris kode sungguhan sebelum menyimpulkan ada/tidaknya caller.
- **Jangan fix sesuatu yang "terlihat aneh" tanpa bukti fungsional.** Tulis test
  singkat yang membuktikan bug benar-benar terjadi SEBELUM fix, dan membuktikan
  sudah benar SETELAH fix — pola yang sama seperti seluruh sesi audit sebelumnya.
- **Kalau ragu apakah sesuatu bug atau desain yang disengaja**, cari konteks
  (baca komentar sekitar, cek apakah ada test lain yang bergantung pada perilaku
  itu) sebelum mengubah. Kalau tetap ragu, dokumentasikan sebagai
  `catatan_investigasi` (bukan bug fix) — sama seperti pola `weight_`/`disable_regime_`
  di `meta_learner.py` yang ternyata by-design, bukan bug.
- **Token GitHub**: user akan kasih token baru tiap sesi dan revoke setelah selesai
  — jangan simpan/tampilkan token di laporan atau commit message.
- **Satu file boleh disentuh lagi meski sudah "DONE/SERIUS"** di AUDIT_STATE.json —
  status itu untuk audit KRONOLOGIS pertama, bukan berarti file itu kebal dari
  temuan baru di cross-check dua-arah ini.
