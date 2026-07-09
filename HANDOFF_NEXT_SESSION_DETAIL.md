# HANDOFF PROMPT LENGKAP — LANJUTKAN SESI BUG HUNTING ALGOTRADER
## (Versi Super Detail — Copy-paste SELURUH isi file ini ke sesi Claude baru)

Kamu (Claude yang baru) melanjutkan pekerjaan bug-hunting yang **SUDAH BERJALAN
CUKUP JAUH** pada sebuah proyek nyata. Ini BUKAN proyek baru, BUKAN audit dari nol.
Jangan re-audit hal yang sudah selesai, jangan re-diskusi keputusan yang sudah
disepakati, jangan tanya "mau mulai dari mana" — semua sudah dijelaskan di bawah.
Baca dokumen ini SAMPAI HABIS, baris per baris, sebelum melakukan apa pun.

Dokumen ini sengaja ditulis SANGAT detail (bahkan menjelaskan istilah-istilah yang
mungkin terasa "sudah jelas") karena kamu tidak punya memori dari sesi sebelumnya.
Setiap istilah, setiap keputusan desain, dan setiap alasan di baliknya dijelaskan
supaya kamu tidak perlu menebak atau membuat asumsi sendiri.

---

## BAGIAN 0 — RINGKASAN SUPER SINGKAT (1 PARAGRAF) UNTUK ORIENTASI CEPAT

Ini adalah proyek audit bug (bug hunting) menyeluruh terhadap sebuah bot trading
crypto bernama **AlgoTrader**, ditulis dalam Python, di-hosting di repo GitHub privat/
publik milik user, dan proses audit dibagi menjadi beberapa **putaran** (round) dengan
metodologi berbeda-beda, dikerjakan **per tier** (kelompok file yang dikelompokkan
berdasarkan seberapa besar dampak kerusakannya kalau ada bug — "blast radius").

**⚠️ PERUBAHAN PENTING per sesi ini (baca `BAGIAN 5.6` dan `BAGIAN 5.7` di bawah
SEBELUM baca bagian lain manapun):** Status "SELESAI" yang tercatat di sesi-sesi
sebelumnya (termasuk Putaran 1 untuk 37 file, dan Tier 1 putaran 2-4) **TERBUKTI
TIDAK BISA DIPERCAYA BEGITU SAJA**. Audit dadakan (bukan bagian dari alur putaran
normal) terhadap SATU file yang sudah berstatus "SELESAI SEMUA PUTARAN" —
`execution.py` — dengan metode membaca ulang setiap baris tanpa asumsi, mengecek
dari SEMUA sudut pandang, dan membuktikan lewat eksperimen nyata, langsung
menemukan **2 bug kritis yang belum pernah tercatat**, termasuk satu yang sudah
dibuktikan secara eksperimental menghasilkan posisi trading fiktif (phantom) yang
tercatat di database seolah berhasil padahal order aslinya belum tereksekusi sama
sekali di exchange. Karena itu, **SELURUH sesi kerja mulai sekarang HARUS
MENGULANG audit dari 0 dengan metodologi yang jauh lebih ketat** (dijelaskan detail
di `BAGIAN 5.6` dan `BAGIAN 6.5`), **termasuk file-file yang sudah "SELESAI"**,
bukan langsung lompat ke Tier 2. Progress kerja sebelumnya (6 bug Tier 1 + bug-bug
Putaran 1) TETAP VALID dan tidak perlu diulang temuannya — tapi statusnya berubah
dari "SELESAI, aman dilanjut" menjadi "PERNAH DICEK, TAPI BELUM TENTU TUNTAS,
WAJIB diverifikasi ulang dengan metode baru sebelum dianggap benar-benar bersih."

---

## BAGIAN 1 — PENJELASAN ISTILAH KUNCI (SUPAYA TIDAK ADA SALAH PAHAM)

### Apa itu "TIER"?

"Tier" di sini BUKAN tingkat kesulitan, dan BUKAN urutan kronologis file dibuat.
Tier adalah **pengelompokan file berdasarkan BLAST RADIUS** — yaitu seberapa luas dan
seberapa parah dampak kerusakan yang ditimbulkan kalau ada bug di file tersebut, kalau
bug itu sampai lolos ke produksi (bot berjalan live dengan uang sungguhan di Binance).

Urutan tier disusun dari yang **paling berbahaya kalau salah** ke yang **paling ringan
kalau salah**:

- **Tier 1 — Jalur uang langsung** (`execution.py`, `risk.py`, `exchange.py`): kalau
  ada bug di sini, dampaknya LANGSUNG ke eksekusi order, ukuran posisi, atau koneksi
  ke exchange. Bug di sini bisa berarti order dobel, posisi kebesaran/kekecilan, atau
  bot buta terhadap harga real-time. Ini yang paling kritis makanya dikerjakan
  duluan. **STATUS DIUBAH: sebelumnya ditandai "SELESAI SEMUA PUTARAN", TAPI status
  ini TERBUKTI TIDAK VALID** — lihat `BAGIAN 5.6` untuk bukti konkretnya. Sekarang
  Tier 1 berstatus **WAJIB DI-RE-AUDIT TOTAL DARI 0** dengan metodologi baru yang
  jauh lebih ketat (`BAGIAN 6.5`), MESKIPUN 6 bug lama yang sudah ditemukan &
  diperbaiki tetap valid dan tidak perlu ditemukan ulang.
- **Tier 2 — Fondasi matematika** (`ta_compat.py`, dan semua isi folder `indicators/`:
  `trend.py`, `momentum.py`, `oscillators.py`, `volatility.py`, `strength.py`,
  `structure.py`, `patterns.py`, `orderbook.py`): file-file ini menghitung indikator
  teknikal (RSI, MACD, ATR, dll) yang JADI INPUT untuk keputusan trading di tier-tier
  atasnya. Kalau rumus salah di sini, semua keputusan yang dibangun di atasnya
  (scoring, sinyal, threshold) ikut salah tanpa terlihat jelas — makanya disebut
  "fondasi". **STATUS: SELESAI TOTAL (2026-07-09)** — semua 9 file sudah diaudit
  metode ketat (BAGIAN 6.5), 9 bug ditemukan & difix (rincian lengkap di
  `hunter_bug.json` tier 2 -> field `ringkasan`), termasuk verifikasi matematika
  independen untuk ADX/RSI/MACD/CCI/Williams%R/BB/Keltner/MFI/Supertrend/Ichimoku/
  SAR/Pivot/Fibonacci/MarketStructure/Donchian/OBV/EMA-Stack/VWAP/GoldenDeadCross/
  ROC/StochRSI/Engulfing/Hammer/Doji/Marubozu/VolumeClimax/Imbalance/WeightedVolume/
  Spoofing/Absorption/Liquidity. Regression test penuh (`simulate_test.py`) 103/104
  PASS (1 gagal = env-issue sandbox, bukan bug kode). **INI SUDAH SELESAI, JANGAN
  DIKERJAKAN ULANG DARI 0** — kalau sesi baru mau verifikasi tambahan, cek dulu
  `AUDIT_STATE.json`/`hunter_bug.json` entry masing-masing file utk lihat apa yang
  sudah dicek, supaya tidak buang waktu mengulang yang sudah terverifikasi.
- **Tier 3 — Keputusan** (`intelligence/*`, `strategy.py`): logika yang mengambil
  keputusan trading berdasarkan skor/indikator dari Tier 2. **INI YANG SEKARANG
  HARUS DIKERJAKAN**, dengan metodologi yang sama persis seperti Tier 1 & 2
  (BAGIAN 6.5): baca tuntas, verifikasi matematika/logika independen, cek jalur
  penghubung ke SEMUA caller, eksperimen pembuktian sebelum & sesudah fix, jangan
  percaya klaim/changelog lama tanpa verifikasi ulang.
- **Tier 4 — Config** (`profiles/*`): parameter dan threshold per-koin.
- **Tier 5 — Pembelajaran adaptif** (`learning/*`): sistem yang menyesuaikan
  parameter otomatis berdasarkan hasil trading historis.
- **Tier 6 — Persistensi/antarmuka** (`database.py`, `notifications.py`,
  `api_server.py`, `telegram_bot.py`, `smoke_api.py`): penyimpanan data dan
  antarmuka ke user/operator.
- **Tier 7 — Orkestrator** (`main.py`): file paling besar (3300+ baris) yang
  menyatukan semua komponen di atas, sengaja dikerjakan PALING TERAKHIR karena
  semua dependency-nya (Tier 1-6) harus sudah diverifikasi dulu sebelum
  mengaudit orkestratornya.

### Apa itu "PUTARAN"?

"Putaran" (round) adalah **jenis/metodologi pemeriksaan** yang dilakukan terhadap
tiap file, BUKAN urutan file. Setiap file dalam satu tier akan melewati sampai
4 putaran berbeda, masing-masing mencari JENIS bug yang berbeda:

- **Putaran 1 — Audit kronologis (SUDAH SELESAI, untuk SEMUA 37 file)**: audit
  pertama yang dilakukan file-per-file, satu per satu, dari file yang paling "dalam"
  (paling sedikit dependency, misal indicators individual) ke file yang paling
  "luar" (paling banyak dependency, `main.py` diaudit paling terakhir di putaran ini
  juga). Ini adalah audit umum/menyeluruh pertama kali terhadap tiap file secara
  independen. Bug-bug besar yang ditemukan di putaran ini tercatat di
  `AUDIT_STATE.json`.
- **Putaran 2 — Cross-check dua arah**: setelah putaran 1 selesai untuk semua file,
  putaran 2 memeriksa HUBUNGAN ANTAR FILE. Untuk tiap fungsi publik di sebuah file,
  dicek dua arah:
  - **Arah "keluar" (caller → callee, pakai `calls_out` di `jalur.json`)**: apakah
    fungsi yang dipanggil oleh file ini benar-benar ada, apakah signature-nya cocok
    (jumlah parameter, tipe, default value), apakah konsisten async/sync, dan apakah
    return value yang diharapkan benar-benar sesuai dengan apa yang SEBENARNYA
    di-return oleh fungsi tersebut (bukan asumsi).
  - **Arah "masuk" (callee ← caller, pakai `referenced_in_files` di `jalur.json`)**:
    file lain mana saja yang KEMUNGKINAN memanggil fungsi ini (hasil grep,
    bisa false-positive untuk nama fungsi umum), lalu diverifikasi MANUAL dengan
    membaca langsung kode di file pemanggil tersebut — tidak boleh percaya hasil grep
    begitu saja.
  Tujuannya menangkap bug seperti: parameter yang diterima tapi tidak pernah dipakai,
  field response yang salah dicek oleh caller, referensi objek yang sudah basi
  (stale reference) setelah reinit, dll.
- **Putaran 3 — Validasi matematika**: khusus mencocokkan RUMUS yang diimplementasikan
  dalam kode dengan DEFINISI BAKU/standar dari rumus tersebut (misalnya rumus Calmar
  ratio, Sharpe ratio, RSI, ATR, dll — dicek apakah implementasinya benar secara
  matematis, termasuk soal skala/unit seperti 0-100 vs 0-1, per-hari vs annualized).
  File yang tidak memiliki kalkulasi signifikan (misalnya file config murni) ditandai
  `SKIP_TIDAK_RELEVAN` untuk putaran ini — tidak perlu dipaksakan.
- **Putaran 4 — Kondisi khusus (edge case)**: menguji skenario tidak biasa yang jarang
  terjadi tapi bisa fatal kalau tidak ditangani: harga 0 atau negatif, cold start (data
  historis belum cukup), race condition (dua proses jalan bersamaan mengubah state
  yang sama), file config/`.env`/JSON yang korup, response API dari exchange yang
  berubah bentuk/field-nya, network timeout di tengah eksekusi order, dll.
- **Putaran 5 — Regresi akhir**: dijalankan PALING TERAKHIR, SETELAH semua tier
  (Tier 1 sampai Tier 7) selesai putaran 2, 3, dan 4 masing-masing. Ini adalah
  pengecekan regresi menyeluruh untuk memastikan tidak ada bug baru yang muncul
  akibat SEMUA perubahan yang sudah dilakukan sepanjang proses audit.

### Kenapa strateginya "HYBRID depth-first PER TIER" (bukan breadth-first murni, bukan per-file individual)?

Artinya: untuk SATU tier yang sedang dikerjakan (misalnya Tier 2 sekarang), kerjakan
SEMUA file dalam tier itu untuk **putaran 2 dulu sampai tuntas semua file**, baru
lanjut ke **putaran 3 untuk semua file di tier yang sama** (skip yang
`SKIP_TIDAK_RELEVAN`), baru **putaran 4 untuk semua file di tier yang sama** — baru
setelah itu semua, pindah ke tier berikutnya (Tier 3) dan ulangi pola yang sama
(putaran 2 semua file → putaran 3 semua file → putaran 4 semua file). Alasannya:
mengerjakan satu jenis pemeriksaan (misalnya cross-check) untuk semua file dalam satu
tier sekaligus membuat pola bug yang berulang lebih mudah terlihat (misalnya kalau
satu jenis bug muncul di beberapa file indicator dengan pola yang mirip), dibanding
loncat-loncat antar jenis pemeriksaan untuk satu file lalu pindah file.

---

## BAGIAN 2 — SETUP AWAL TEKNIS — JALANKAN INI DULU SEBELUM APA PUN LAIN

```bash
export GH_TOKEN=   # MINTA TOKEN BARU KE USER DULU — JANGAN LANJUT TANPA INI
git clone https://x-access-token:${GH_TOKEN}@github.com/rasydalamsyah-del/algotrader /home/claude/algotrader
cd /home/claude/algotrader
git config user.email "rasydalamsyah@gmail.com"
git config user.name "rasydalamsyah-del"
git remote set-url origin https://x-access-token:${GH_TOKEN}@github.com/rasydalamsyah-del/algotrader.git
```

**Aturan token — WAJIB DIPATUHI:**
- User akan memberi token GitHub BARU (personal access token, format `ghp_...`) di
  setiap sesi kerja, dan **akan me-revoke token itu sendiri setelah sesi ini selesai**.
- **JANGAN PERNAH menampilkan token di chat, di log, atau menuliskannya ke dalam
  commit message.** Token hanya boleh dipakai untuk keperluan git clone/push, disimpan
  cukup di git remote config lokal sandbox.
- Kalau `git push` gagal dengan error autentikasi, itu tandanya token sudah
  expired/di-revoke oleh user — jangan panik, JANGAN paksa terus mencoba, cukup minta
  token baru ke user. Commit yang sudah dibuat secara lokal TETAP AMAN tersimpan di
  sandbox, menunggu token baru untuk di-push.
- **SELALU jalankan `git fetch origin` lalu cek `git log HEAD..origin/main --oneline`
  SEBELUM setiap kali commit** — ada kemungkinan ada commit baru yang masuk dari sesi
  kerja lain (misalnya kalau user membuka sesi paralel), supaya tidak terjadi konflik
  atau kamu menimpa pekerjaan orang lain.

---

## BAGIAN 3 — KONTEKS PROYEK: APA ITU "ALGOTRADER"

**AlgoTrader** adalah bot algorithmic trading crypto berbasis Python, untuk pasar
**spot** (bukan futures, bukan margin — jadi tidak ada leverage/likuidasi), berjalan
di server atau di Termux (aplikasi terminal Android), melakukan trading di exchange
**Binance** melalui library **ccxt**.

Struktur arsitektur garis besar:

| File / Folder | Peran |
|---|---|
| `main.py` (3300+ baris) | Orkestrator utama: scanner loop, pipeline Gate 1-5, strategy loop (SL/TP/trailing stop), portfolio monitor, config watcher (hot-reload tanpa restart) |
| `execution.py` | Eksekusi order — market order, limit order, iceberg order (untuk order besar yang dipecah jadi beberapa chunk supaya tidak menggerakkan harga pasar terlalu jauh) |
| `risk.py` | Position sizing (ukuran posisi), perhitungan SL/TP, logic drawdown/halt, perhitungan metrik performa (Sharpe, Sortino, Calmar, dll) |
| `exchange.py` | `ExchangeConnector` (koneksi REST API ke Binance) + `WebSocketFeed` (data pasar real-time via WebSocket) |
| `indicators/*` | Indikator teknikal: trend, momentum, oscillator, volatility, strength, structure, patterns, orderbook |
| `intelligence/*` | `scorer` (penilai sinyal), `classifier` (klasifikasi kondisi pasar), `validator`, `commander` (Gate 4.5 — gerbang keputusan tingkat lanjut), `trade_guardian` (ATG — semacam pengawas posisi terbuka) |
| `strategy.py` | Generate sinyal trading, tracker untuk trailing stop |
| `profiles/*` | Konfigurasi per-koin: `base_profile`, `weights` (bobot indikator), `thresholds`, `registry` |
| `learning/*` | Pembelajaran adaptif: `coin_swap` (tukar koin performa buruk/baik antar-bot), `cross_learn` (belajar dari bot lain), `analytics`, `meta_learner` (auto-tuning parameter) |
| `database.py`, `core/models.py` | Persistensi data & dataclass model |
| `notifications.py`, `telegram_bot.py`, `api_server.py`, `smoke_api.py` | Antarmuka ke operator/user |

**Catatan arsitektur penting**: `telegram_bot.py` **BUKAN** diimpor oleh `main.py` —
ia adalah **proses terpisah** yang berkomunikasi dengan `api_server.py` lewat HTTP
REST (`BOT_API=http://localhost:8000/api`). Ini penting supaya kamu tidak salah
mengira ada import langsung antara keduanya saat melakukan cross-check putaran 2.

---

## BAGIAN 4 — TIGA FILE TRACKING + 1 DOKUMEN METODOLOGI — WAJIB DIBACA DI AWAL, URUTANNYA PENTING

### a) `AUDIT_STATE.json` — Histori audit putaran 1 (SUDAH SELESAI 100%, 37 file)

Berisi hasil audit kronologis pertama untuk semua 37 file (file-per-file, dari yang
dependency-nya paling sedikit sampai `main.py` di akhir). Untuk tiap file berisi field
`bug_fixed`, `cross_file_check`, `catatan_investigasi`. **JANGAN mengulang temuan yang
sudah tercatat di sini** — cek dulu sebelum melaporkan sesuatu sebagai "bug baru".

Cara cek entry untuk file tertentu:
```bash
python3 -c "
import json
d = json.load(open('AUDIT_STATE.json'))
print(json.dumps(d['file_sudah_diaudit_dan_fix'].get('<NAMA_FILE>'), indent=2, ensure_ascii=False))
"
```

### b) `hunter_bug.json` — Prioritas & status cross-check putaran 2-5, diurutkan per Tier

Struktur field `putaran` per file berisi 4 sub-field status:
- `putaran_2_cross_check_dua_arah`
- `putaran_3_validasi_matematika`
- `putaran_4_kondisi_khusus`
- `putaran_5_regresi_akhir`

Tiap sub-field punya `status` (`PENDING`, `CROSS_CHECKED_FIXED`,
`CROSS_CHECKED_CLEAN`, atau `SKIP_TIDAK_RELEVAN`) dan `catatan`.

Baca `_meta.strategi_urutan_kerja` di dalam file JSON ini untuk detail lengkap strategi
hybrid depth-first per tier yang sudah dijelaskan di Bagian 1 di atas.

Cara cek tier mana yang harus dikerjakan sekarang (jalankan ini di awal sesi):
```bash
python3 -c "
import json
d = json.load(open('hunter_bug.json'))
for tier in d['urutan_prioritas']:
    belum = []
    for f in tier['files']:
        for pk, pv in f['putaran'].items():
            if pv['status'] == 'PENDING':
                belum.append((f['file'], pk))
    if belum:
        print(f\"Tier {tier['tier']} ({tier['nama_tier']}) BELUM SELESAI:\")
        for file, putaran in belum:
            print(f'  {file} -> {putaran}')
        print('KERJAKAN TIER INI DULU.')
        break
else:
    print('Semua tier selesai putaran 2-4 -> saatnya putaran 5 (regresi akhir).')
"
```

### c) `jalur.json` — Call graph (peta pemanggilan fungsi) hasil ekstraksi AST otomatis

Peta ini **BUKAN ditulis manual** — hasil ekstraksi otomatis dari Abstract Syntax
Tree kode, jadi **akurat 1:1** dengan kode yang sebenarnya (bukan dokumentasi yang
bisa basi). Mencakup 34 file inti. Tiap fungsi/method punya dua field:
- `calls_out`: daftar fungsi yang DIPANGGIL oleh fungsi ini (akurat penuh, hasil
  analisis AST langsung).
- `referenced_in_files`: daftar file yang KEMUNGKINAN memanggil fungsi ini (hasil
  grep best-effort). **BISA false-positive** untuk nama fungsi yang umum (misalnya
  fungsi bernama `get_score` yang ada di banyak class berbeda) — **WAJIB diverifikasi
  manual** dengan membaca langsung kode aslinya di file tersebut sebelum menyimpulkan
  ada relasi caller-callee yang nyata.

**Keterbatasan penting** (baca juga `_meta.keterbatasan` di dalam file JSON ini):
peta ini TIDAK menangkap:
- Pemanggilan dinamis (misalnya lewat `getattr()`),
- Callback yang disimpan sebagai referensi lalu dipanggil di tempat lain,
- Komunikasi lewat HTTP (khusus untuk `telegram_bot.py` ↔ `api_server.py`, relasi ini
  sudah dipetakan secara manual terpisah, lihat entry `telegram_bot.py` di
  `AUDIT_STATE.json`).

Kalau ada perubahan signature fungsi (menambah/menghapus parameter, mengubah nama
fungsi), jalankan ulang: `python3 tools/build_jalur.py` untuk regenerasi file ini.

### d) `BUG_HUNTER_PROMPT.md` — Metodologi lengkap putaran 2 (WAJIB dibaca penuh)

Dokumen ini (`HANDOFF_NEXT_SESSION_DETAIL.md` / handoff) hanyalah RINGKASAN
ORIENTASI — bukan pengganti `BUG_HUNTER_PROMPT.md`. Di dalam `BUG_HUNTER_PROMPT.md`
dijelaskan detail lengkap: cara melakukan cross-check dua arah langkah-per-langkah,
pola-pola bug spesifik yang biasa dicari, format laporan temuan yang standar, dan
aturan update ke file tracking. **Baca file ini secara penuh sebelum mulai kerja.**

---

## BAGIAN 5 — STATUS PROGRESS SAAT INI SECARA DETAIL (JANGAN DIULANG / JANGAN DITEMUKAN LAGI SEBAGAI "BUG BARU")

### Putaran 1 (audit kronologis) — SELESAI 100% untuk 37 file

Bug-bug besar yang SUDAH diperbaiki di putaran ini (referensi historis, jangan
re-temukan sebagai bug "baru"):

1. **`learning/coin_swap.py`** — Key `.env` yang dicek/ditulis salah: kode memakai key
   `WATCHLIST`, padahal seluruh sistem lain (`main.py`, `telegram_bot.py`, bahkan
   fungsi `_restart_bots()` di file yang sama) memakai key `UNIVERSE_WATCHLIST`.
   Akibatnya `self_wl`/`peer_wl` selalu kosong, sehingga `run_cycle()` dan
   `_triggered_swap()` selalu abort di pengecekan "universe kosong" — **fitur coin
   swap tidak pernah benar-benar berjalan** meskipun `COIN_SWAP_ENABLED=true`.
   Sudah difix: key disamakan jadi `UNIVERSE_WATCHLIST`.
2. **`learning/cross_learn.py`** — Cache (`_cache`/`_cache_ts`, TTL 30 menit)
   dideklarasikan di `__init__` tapi logic pemakaiannya tidak pernah ditulis — setiap
   pemanggilan method fetch selalu buka koneksi baru & query ulang tanpa henti.
   Sudah difix: cache per-key benar-benar dipakai di `get_peer_trades`,
   `get_peer_signal_scores`, `get_peer_regime_stats`.
3. **`learning/meta_learner.py`** — Dua bug: (a) guardrail cooldown 1 jam bocor
   untuk parameter jenis `weight_*` dan `disable_regime_*` (cabang kode ini
   `return` lebih dulu sebelum sempat dicek cooldown-nya, sehingga suggestion bisa
   diterapkan berkali-kali tanpa jeda — berisiko weight drift tak terkendali); (b)
   auto-revert gagal setelah restart proses karena data suggestion yang mau
   di-revert hanya dicari di memory (`self._pending`), yang otomatis kosong tiap kali
   restart. Sudah difix: cooldown dicek di awal fungsi untuk SEMUA jenis parameter;
   fallback rekonstruksi `ParameterSuggestion` dari record database kalau tidak ada
   di memory.
4. **`telegram_bot.py` + `api_server.py`** — Approve/reject config selalu melaporkan
   "sukses" ke user walau sebenarnya gagal, karena caller mengecek field response
   yang salah (`status` padahal hasil sebenarnya ada di field `applied`/`rejected`).
   Juga ditemukan whitelist config yang kurang 3 key penting. Sudah difix.
5. **`intelligence/commander.py`** — Gate pengecekan spread (bagian dari Gate 4.5)
   mati total karena bug pengecekan `__dict__` padahal seharusnya memanggil method
   class. Ini bug yang SUDAH DIKETAHUI dan **masih PENDING untuk di-fix secara
   menyeluruh** (lihat Bagian 6 — file ini termasuk salah satu dari 5 file yang
   PENDING versi lama sebelum sistem tier dibuat; perlu dicek ulang statusnya di
   `hunter_bug.json`/`AUDIT_STATE.json` apakah sudah tercatat selesai atau belum
   sebelum diasumsikan).
6. **`main.py`** — Beberapa bug ditemukan dan difix di putaran ini: referensi
   `exchange` yang jadi basi (stale) setelah config di-reinit lewat hot-reload;
   notifikasi ATG (trade guardian)/trailing stop yang salah trigger; log Gate 4.5
   yang menyesatkan (menampilkan info yang tidak sesuai kondisi sebenarnya).

### Tier 1 (`execution.py`, `risk.py`, `exchange.py`) — SELESAI SEMUA PUTARAN 2, 3, 4

**Putaran 2 (cross-check dua arah) — 2 bug ditemukan & difix:**
- Perhitungan iceberg order (order besar yang dipecah jadi beberapa chunk) untuk
  `entry_price`/`amount` hanya memakai data dari chunk PERTAMA saja, padahal
  seharusnya dihitung sebagai **weighted-average** dari SEMUA chunk yang sudah
  ter-fill. Fix dilakukan di `execution.py` DAN `main.py` (karena `main.py` ikut
  memakai nilai ini).
- `RiskManager._update_config` di `risk.py` hanya me-refresh 5 dari 13 field
  konfigurasi yang seharusnya, dan trigger update di `main.py` hanya mengecek 3
  key saja (bukan semua key yang relevan). Fix dilakukan di `risk.py` DAN `main.py`.

**Putaran 3 (validasi matematika) — 1 bug ditemukan & difix:**
- `compute_calmar_ratio` di `risk.py` (dipakai lewat `api_server.py`) menghasilkan
  nilai Calmar ratio yang **understated (terlalu rendah) sekitar 150x** karena
  return yang dipakai adalah return mentah untuk periode singkat (~5.2 hari) tanpa
  di-annualized (di-CAGR-kan) dulu, padahal rumus baku Calmar ratio membutuhkan
  return tahunan. Fix di `api_server.py`.

**Putaran 4 (kondisi khusus/edge case) — 2 bug signifikan ditemukan & difix:**
- **Risiko double-fill di `_execute_limit`** (`execution.py`): saat limit order
  timeout (tidak ter-fill dalam batas waktu), kode mencoba `cancel_order`. Kalau
  `cancel_order` ini GAGAL (misalnya karena network error, BUKAN karena order sudah
  ter-fill), kode LAMA tetap lanjut mengasumsikan "mungkin sudah filled" dan
  langsung submit MARKET ORDER BARU untuk jumlah PENUH. Kalau ternyata limit order
  yang lama sebenarnya MASIH HIDUP di exchange (cancel gagal genuine, bukan karena
  sudah filled) dan nanti ter-fill juga, maka posisi jadi DOBEL. Fix: sebelum
  memutuskan fallback ke market order, kode sekarang memverifikasi status order
  yang SEBENARNYA lewat `fetch_order` terlebih dahulu.
- **`_watch_tickers_all` (jalur WebSocket UTAMA untuk Binance, menangani SEMUA
  simbol sekaligus dalam satu koneksi termultipleks) mati PERMANEN setelah
  mencapai `max_retries` kegagalan berturut-turut, TANPA mekanisme restart apa
  pun.** Ini adalah **bug paling serius yang ditemukan sepanjang proses bug hunting
  ini** — efeknya, satu gangguan sementara pada WebSocket (misalnya maintenance
  singkat di sisi Binance) akan men-downgrade SELURUH sistem secara PERMANEN ke
  REST-only polling (update harga tiap 10 detik, bukan real-time) sampai bot
  di-restart manual oleh operator, tanpa ada indikasi/notifikasi apa pun bahwa hal
  ini terjadi. Fix: implementasi self-healing — task WebSocket akan otomatis
  di-restart dengan exponential backoff (30 detik → 60 detik → 120 detik → dst,
  maksimum 10 menit antar-percobaan).

**Catatan khusus `exchange.py` putaran 2**: hasil pengecekan = `CROSS_CHECKED_CLEAN`
(tidak ditemukan bug). Ada 2 potongan kode yang awalnya terlihat seperti dead-code,
tapi setelah diinvestigasi ternyata BUKAN bug — `WebSocketFeed` memang sengaja
memiliki koneksi ccxt yang terpisah dari `ExchangeConnector` (keputusan desain yang
disengaja, sudah didokumentasikan sebagai `catatan_investigasi`, bukan bug).

**Ringkasan total Tier 1**: 6 bug ditemukan & diperbaiki di seluruh putaran 2-4
(Iceberg entry_price salah, `_update_config` field tidak lengkap, Calmar ratio
understated ~150x, risiko double-fill, WS mati permanen — dihitung sebagai 2 bug
karena mencakup dua kasus edge case terpisah di putaran 4).

### TIER 2 — STATUS: SELESAI TOTAL (2026-07-09)

Semua 9 file berikut sudah diaudit metode ketat penuh (bukan cuma putaran
cross-check parsial seperti rencana lama di bawah ini — sudah mencakup baca
tuntas + verifikasi matematika independen + cek jalur penghubung + eksperimen
pembuktian untuk SEMUA fungsi publik):

```
ta_compat.py          -- DONE (1 fix konsistensi RSI seed)
indicators/trend.py    -- DONE (1 fix lama cross_ok, diverifikasi ulang clean)
indicators/momentum.py -- DONE (1 fix RSI seed method, keputusan desain user)
indicators/oscillators.py -- DONE (1 fix lama exclude+renormalize, diverifikasi ulang)
indicators/volatility.py  -- DONE (2 fix: ADX seed + _wilder_smooth defensive)
indicators/strength.py    -- DONE (2 fix: ADX seed + MFI asimetris)
indicators/structure.py   -- DONE (1 fix: composite_score exclude+renormalize)
indicators/patterns.py    -- DONE (2 fix: asimetri scoring + arah HTF terbalik)
indicators/orderbook.py   -- DONE (1 fix: spoofing detection tidak aktif)
```

Total 9 bug ditemukan & difix di Tier 2. Rincian lengkap tiap file ada di
`hunter_bug.json` (field `putaran_ulang_metode_ketat` per file) dan
`AUDIT_STATE.json`. Ada juga 2 temuan arsitektur yang DIDOKUMENTASIKAN tapi
SENGAJA TIDAK diubah (butuh backtest data historis nyata utk validasi
sebelum diubah, tidak tersedia di sandbox): (1) `market_structure_score`/
`donchian_score` tidak masuk `LEVEL2_WEIGHTS` di `profiles/weights.py`,
(2) — cek `AUDIT_STATE.json` utk detail lengkap kalau ada temuan lain yg
serupa.

**JANGAN kerjakan ulang Tier 2 dari 0.** Kalau ingin verifikasi tambahan,
mulai dari baca `AUDIT_STATE.json`/`hunter_bug.json` per file dulu.

### SEKARANG GILIRAN: Tier 3 — Keputusan (`intelligence/*`, `strategy.py`)

File-file di `intelligence/` (observer.py, scorer.py, validator.py, dan
lainnya) serta `strategy.py` — ini lapisan yang mengambil keputusan trading
berdasarkan skor/indikator yang sudah diverifikasi di Tier 2. Terapkan
metodologi yang SAMA PERSIS seperti Tier 1 & Tier 2 (BAGIAN 6.5): baca
tuntas semua fungsi, verifikasi matematika/logika independen di mana
relevan, cek jalur penghubung ke SEMUA caller (bukan cuma 1-2), jangan
percaya klaim/changelog lama tanpa verifikasi ulang, eksperimen pembuktian
before/after untuk setiap fix, dan regression test (`simulate_test.py`)
setelah setiap perubahan.

Perhatian khusus untuk Tier 3: file-file ini SUDAH beberapa kali disentuh
secara tidak langsung selama audit Tier 2 (misal ditemukan bahwa RSI mentah
dipakai sebagai hard threshold gate di `scorer.py` untuk 4 jenis trigger,
dan `nearest_structure_support/resistance` dipakai di `validator.py`) — jadi
sebagian jalur penghubung sudah terverifikasi, tapi audit MENYELURUH untuk
file-file ini sendiri (bukan cuma dari sudut pandang caller Tier 2) belum
pernah dilakukan.

---

## BAGIAN 5.5 — ATURAN KERAS TAMBAHAN (WAJIB DIPATUHI, INI PENEKANAN KHUSUS DARI USER)

Tiga aturan ini SENGAJA ditulis terpisah dan tegas karena user secara eksplisit
mengkhawatirkan sesi baru akan malas/asal-asalan. **Ini bukan saran, ini kewajiban.**

### 5.5.1 — JANGAN PERCAYA CATATAN BEGITU SAJA — SELALU VERIFIKASI KE KODE ASLI

Semua isi `AUDIT_STATE.json`, `hunter_bug.json`, `jalur.json`, dan dokumen handoff
ini sendiri (termasuk seluruh isi Bagian 5 di atas soal bug yang "sudah difix") harus
diperlakukan sebagai **KLAIM, bukan fakta yang otomatis benar**. Sebelum melanjutkan
pekerjaan baru di atas asumsi bahwa sesuatu "sudah beres":
- **Buka file kode aslinya langsung**, baca baris yang relevan, dan konfirmasi sendiri
  bahwa fix yang diklaim benar-benar ada di kode saat ini (bukan cuma di catatan).
  Ini penting karena bisa saja ada commit lain yang menimpa fix tersebut, atau
  catatan salah tulis, atau ada perubahan yang lupa disinkronkan ke tracking.
- Jangan pernah menyimpulkan "file X sudah CLEAN di putaran Y" hanya karena
  `hunter_bug.json` bilang begitu — untuk file yang SEDANG dikerjakan sekarang,
  tetap baca ulang kode itu sendiri dari awal sampai akhir, jangan modal ringkasan.
  Ringkasan/catatan hanya berguna sebagai PETA AWAL supaya tidak buta arah, BUKAN
  sebagai pengganti membaca kode.
- Untuk `referenced_in_files` di `jalur.json`: SELALU buka file yang dirujuk dan baca
  baris kodenya langsung — grep bisa salah, false-positive untuk nama fungsi umum.
- Kalau ada ketidakcocokan antara apa yang tertulis di catatan/tracking dan apa yang
  benar-benar ada di kode — **kode adalah kebenaran (source of truth), bukan
  catatan**. Laporkan ketidakcocokan itu ke user, jangan diam-diam mengikuti
  catatan yang ternyata salah.
- Cek dari **berbagai sudut pandang**, bukan cuma jalur "bahagia" (happy path):
  cek dari sisi pemanggil (caller) DAN yang dipanggil (callee), cek dari sisi
  data normal DAN data corrupt/kosong/ekstrem, cek dari sisi kode berhasil DAN
  kode gagal (exception, timeout, network error), cek konsistensi antar-file yang
  saling terkait, bukan cuma dalam satu file saja.

### 5.5.2 — PERBAIKAN HARUS TUNTAS SAMPAI AKAR MASALAH, BUKAN TAMBAL SULAM (PATCH-HOLE)

Yang **DILARANG** saat memperbaiki bug:
- Menutupi gejala tanpa membenahi akar masalah — contoh yang DILARANG: menangkap
  exception lalu cuma `pass`/`log.warning()` tanpa menangani apa yang harusnya
  terjadi kalau error itu muncul; kasih `default value`/fallback angka aman
  supaya tidak crash TAPI logic yang salah di baliknya tetap tidak dibenahi;
  menambah `try/except` generik di sekeliling kode bermasalah cuma supaya tidak
  error, padahal fungsinya jadi diam-diam gagal (silent failure).
- Fix yang cuma "kebetulan bikin test lewat" tapi tidak benar-benar menyelesaikan
  skenario yang lebih luas dari kasus spesifik yang ditemukan (misal cuma
  menambal untuk 1 simbol/1 kondisi tertentu, padahal bug-nya general untuk semua
  kasus serupa).
- Solusi setengah jalan yang "yang penting jalan dulu" — kalau akar masalah
  membutuhkan perubahan yang lebih besar (misalnya menyentuh 2-3 file sekaligus,
  seperti kasus iceberg order atau `_watch_tickers_all` di Tier 1), maka itu HARUS
  dilakukan sepenuhnya, bukan dipotong jadi versi minimal yang "cukup untuk lolos
  test doang".

Yang **DIWAJIBKAN** saat memperbaiki bug:
- Cari dan benahi **akar masalah (root cause)**, bukan gejalanya saja.
- Pikirkan apakah fix ini menutup SEMUA jalur/skenario di mana bug itu bisa terjadi,
  bukan cuma skenario spesifik yang kebetulan ditemukan saat testing. Contoh yang
  BENAR (sudah dilakukan di Tier 1): bug `_watch_tickers_all` tidak cuma "ditangkap
  exception-nya", tapi benar-benar dibuatkan mekanisme self-healing restart dengan
  exponential backoff — solusi yang menuntaskan masalah untuk jangka panjang, bukan
  sekadar mencegah crash sesaat.
- Kalau ada trade-off desain (misalnya soal performa vs keamanan, atau soal
  seberapa agresif retry-nya), JELASKAN trade-off itu ke user secara singkat,
  bukan diam-diam memilih opsi yang paling gampang dikerjakan.
- Setelah fix, tanyakan pada diri sendiri: "Kalau bug ini muncul lagi dengan
  variasi kondisi yang sedikit berbeda, apakah fix saya masih akan menangani itu
  dengan benar, atau cuma menutup 1 kasus spesifik?" Kalau jawabannya "cuma
  menutup 1 kasus", perbaiki lagi sampai solusinya general.

### 5.5.3 — SETIAP TEMUAN: JELASKAN SINGKAT KE USER, TES DULU, BARU COMMIT

Alur wajib untuk SETIAP bug yang ditemukan (bukan cuma di akhir sesi/tier):
1. **Jelaskan temuan secara ringkas ke user** dalam bahasa yang mudah dipahami:
   apa bug-nya, di baris/fungsi mana, kenapa itu bug (bukan desain sengaja), dan
   apa dampaknya kalau tidak diperbaiki. Tidak perlu bertele-tele, tapi jangan
   langsung diam-diam commit tanpa penjelasan sama sekali.
2. **Jelaskan rencana fix-nya** secara singkat sebelum atau sesudah menerapkan
   (boleh sekaligus dengan laporan hasil fix) — supaya user tahu pendekatan macam
   apa yang dipakai, bukan cuma "sudah saya perbaiki" tanpa detail.
3. **Jalankan pengetesan nyata** (bukan klaim tanpa bukti): syntax check
   (`py_compile`), test fungsional yang membuktikan bug memang terjadi sebelum fix
   dan sudah benar setelah fix, dan compile-check semua file yang ikut tersentuh.
   Tampilkan hasil test ini ke user (ringkas), jangan cuma bilang "sudah dites".
4. **Baru setelah itu commit & push** (dengan `git fetch` dulu sesuai aturan di
   Bagian 2).
5. Kalau menemukan banyak bug sekaligus dalam satu file, boleh dikumpulkan
   penjelasannya jadi satu laporan per file (seperti format `=== AUDIT: <file> ===`
   yang sudah dipakai di sesi-sesi sebelumnya) — tidak harus terlalu granular per
   baris, yang penting user tetap tahu apa yang terjadi dan kenapa, sebelum kode
   itu di-push ke repo.

---

## BAGIAN 5.6 — EVALUASI JUJUR: KENAPA BUG MASIH LOLOS DI SESI-SESI SEBELUMNYA, DIBUKTIKAN DENGAN KASUS NYATA

User pernah bertanya langsung: "kenapa masalah itu tidak ditemukan pada sesi
sebelumnya, apakah perintahnya kurang tegas?" Jawaban jujurnya: **bukan
perintahnya yang kurang tegas, tapi CARA EKSEKUSINYA yang berhenti terlalu
cepat.** Ini bukti konkretnya, supaya sesi berikutnya tidak mengulangi pola yang
sama:

### Kasus nyata: `execution.py` — file yang sudah berstatus "SELESAI SEMUA PUTARAN 2/3/4"

Setelah Tier 1 dinyatakan selesai total (6 bug ditemukan & diperbaiki), dilakukan
audit dadakan terhadap SATU file yang sama (`execution.py`) dengan cara: baca
ULANG dari baris 1 sampai baris terakhir tanpa asumsi apa pun bahwa "ini sudah
pernah dicek jadi pasti aman", lalu cross-check ke SEMUA fungsi yang dipanggil
(`exchange.py`) dan SEMUA fungsi yang memanggil (`main.py`) satu per satu tanpa
kecuali. Hasilnya: **ditemukan 2 bug kritis yang BELUM PERNAH tercatat**, salah
satunya berhasil DIBUKTIKAN lewat eksperimen nyata (bukan cuma dugaan) bahwa bot
bisa mencatat sebuah posisi trading sebagai "berhasil dibuka" di database padahal
order aslinya di exchange **belum tereksekusi sama sekali** (status masih
`"open"`, bukan `closed`/`filled`) — ini karena:
1. `_execute_market` tidak pernah mengecek `order.get("status")` sama sekali
   sebelum menganggap order itu berhasil.
2. `_process_fill` memakai pola `filled = order.get("filled") or order.get("amount") or ...`
   — kalau `filled` yang sebenarnya adalah `0` (order belum jalan), nilai `0` itu
   dianggap "kosong" oleh Python (`0` adalah falsy) sehingga kode salah jatuh ke
   nilai fallback (jumlah yang DIMINTA, bukan yang benar-benar tereksekusi).
3. Tidak ada satu pun lapisan lain di seluruh codebase (`database.py`, `main.py`,
   `intelligence/position_sync.py`) yang menangkap kesalahan ini.

### Analisis akar penyebab kenapa ini lolos di sesi sebelumnya

Ditelusuri dari transkrip sesi sebelumnya (`uhuii.txt`), ditemukan pola yang
berulang di setiap putaran 4 (edge case) Tier 1:
> *"Given time constraints for this putaran, let me check a few more concrete
> edge cases quickly"*
> *"Given the depth achieved, let's also check one more important edge case in
> exchange.py"*

Sesi sebelumnya **secara eksplisit membatasi diri sendiri karena pertimbangan
waktu** — begitu menemukan 1-2 bug yang "cukup besar/menarik" per file, sesi itu
berpindah ke file berikutnya atau bahkan menyimpulkan tier selesai, alih-alih
menuntaskan pengecekan SEMUA fungsi tanpa kecuali. Ini pola spesifik yang gagal:
- **Berhenti setelah "cukup" ketemu bug**, bukan setelah benar-benar menyisir
  SEMUA fungsi dalam file.
- **Cross-check putaran 2 mengikuti hipotesis awal** (`fokus_cross_check` di
  `hunter_bug.json`) sebagai batas pencarian, bukan sebagai titik awal yang lalu
  tetap diperluas ke seluruh fungsi lain yang tidak masuk hipotesis.
- **Tidak pernah membuat eksperimen/script pembuktian nyata** untuk memverifikasi
  bahwa TIDAK ADA bug lain di luar yang sudah ditemukan — kesimpulan
  "CROSS_CHECKED_CLEAN" diambil dari pembacaan kode + penalaran manual saja,
  tanpa uji coba aktif mencoba "menjebol" fungsi tersebut dengan skenario aneh.
- **Pola bug "truthy-check vs `is not None`" sebenarnya SUDAH PERNAH ditemukan**
  di file lain (`cross_learn.py`, `threshold_used=0.0`) tapi **tidak pernah
  dijadikan checklist wajib untuk dicek ulang di file-file lain** yang punya pola
  serupa (`execution.py` punya pola identik di `filled = ... or ... or ...`,
  lolos karena tidak ada mekanisme "kalau nemu 1 pola bug, cek SEMUA file lain
  untuk pola yang sama").

**Kesimpulan**: masalahnya bukan instruksi tertulis kurang tegas — instruksi soal
"cek semua sudut pandang" sudah ada dari awal. Masalahnya adalah TIDAK ADA
penegasan yang cukup KERAS untuk MELARANG berhenti lebih awal, dan tidak ada
KEWAJIBAN membuat eksperimen pembuktian sebelum menyimpulkan sebuah file/putaran
selesai. `BAGIAN 5.7` dan `BAGIAN 6.5` di bawah ini dibuat khusus untuk menutup
celah ini.

---

## BAGIAN 5.7 — RESET TOTAL: ULANGI SEMUA PENGECEKAN DARI 0, TERMASUK FILE YANG SUDAH "SELESAI"

Ini adalah **instruksi eksplisit dari user**, berlaku mulai sesi ini dan
seterusnya sampai user bilang berhenti:

1. **SEMUA status "SELESAI"/"CROSS_CHECKED_CLEAN"/"CROSS_CHECKED_FIXED" di
   `AUDIT_STATE.json` dan `hunter_bug.json` — untuk PUTARAN 1 (37 file) MAUPUN
   Tier 1 (putaran 2, 3, 4) — dianggap TIDAK CUKUP TERVERIFIKASI**, terbukti dari
   kasus `execution.py` di `BAGIAN 5.6`. Ini BUKAN berarti kerjaan lama dibuang —
   bug-bug yang SUDAH ditemukan & difix (daftar lengkap di Bagian 5 atas) TETAP
   VALID dan TIDAK PERLU ditemukan ulang. Yang berubah adalah: **status "file ini
   sudah pasti bersih" dicabut**, diganti jadi "file ini pernah dicek, hasil
   pengecekan lama tetap dipakai sebagai referensi, TAPI file ini harus dibaca
   ulang dan dicek ulang total dengan metode BAGIAN 6.5 sebelum dianggap benar-
   benar tuntas."
2. **Urutan pengerjaan ulang**: mulai dari Tier 1 (`execution.py`, `risk.py`,
   `exchange.py`) — karena baru terbukti masih ada bug lolos di sana, tier ini
   yang PALING mendesak diverifikasi ulang duluan — baru lanjut ke Tier 2 sampai
   Tier 7 sesuai urutan asli, tapi SEKARANG untuk SETIAP tier (termasuk yang
   sudah pernah "selesai"), jalankan ulang metodologi lengkap di `BAGIAN 6.5`
   dari 0, bukan cuma percaya status lama.
3. **Untuk `execution.py` secara spesifik**: sudah ada 5 temuan konkret dari sesi
   ini yang BELUM diperbaiki (lihat tabel lengkap di `BAGIAN 5.8` di bawah) —
   ini harus jadi prioritas PERTAMA yang dikerjakan (fix + tes + commit) sebelum
   melanjutkan re-audit file lain, supaya tidak ada bug kritis yang dibiarkan
   menggantung.
4. **`risk.py` dan `exchange.py` BELUM sempat diaudit ulang dengan metode
   ketat ini** (baru `execution.py` yang sudah). Kedua file itu WAJIB
   menjalani proses yang SAMA PERSIS seperti `execution.py` di `BAGIAN 5.6`:
   baca total baris-per-baris tanpa asumsi, cross-check ke SEMUA pemanggil &
   yang dipanggil, dan buat eksperimen pembuktian untuk tiap kecurigaan bug
   sebelum menyimpulkan aman.
5. Setelah Tier 1 benar-benar tuntas dengan metode baru (termasuk `risk.py` &
   `exchange.py`), lanjut Tier 2 sampai Tier 7 — SEMUANYA memakai metodologi
   `BAGIAN 6.5`, bukan metodologi lama yang sudah terbukti bolong.
6. **WAJIB dicatat ke GitHub**: setiap kali menyelesaikan re-audit sebuah file
   dengan metode baru (baik ketemu bug baru maupun benar-benar bersih), update
   `AUDIT_STATE.json`/`hunter_bug.json` dengan field baru
   `putaran_ulang_metode_ketat` berisi tanggal, apa yang dicek, dan hasilnya —
   supaya sesi berikutnya tahu file mana yang SUDAH melalui metode baru ini dan
   mana yang belum. Commit & push seperti biasa (lihat Bagian 2 & 5.5.3).

## BAGIAN 5.8 — TEMUAN KONKRET SESI INI DI `execution.py` — BELUM DIPERBAIKI, JADI PRIORITAS PERTAMA

Lima temuan berikut ditemukan lewat audit ulang total (bukan cross-check
hipotesis) terhadap `execution.py`. **Belum satupun yang difix** — ini PENDING,
harus dikerjakan lebih dulu sebelum melanjutkan file lain manapun:

| # | Lokasi | Temuan | Severity | Bukti |
|---|---|---|---|---|
| 1 | `_execute_market` (baris ~261-284) | Tidak pernah mengecek `order.get("status")` sebelum menganggap order berhasil filled — beda dengan jalur limit order yang eksplisit cek status via `_poll_fill` | **KRITIS** | Dibuktikan lewat script eksperimen (fake exchange + fake DB): order dengan `status="open", filled=0` tetap tercatat sebagai `filled=100.0` (fully filled) |
| 2 | `_process_fill` (baris ~600-602) | `filled = order.get("filled") or order.get("amount") or assessment.approved_size` — truthy-check salah, `filled=0` yang valid (order belum jalan) jatuh ke fallback jumlah yang DIMINTA, bukan yang benar-benar tereksekusi | **KRITIS** (memperparah #1) | Sama seperti di atas — bug #1 dan #2 saling memperparah, harus difix bersamaan |
| 3 | `_check_slippage` (baris ~221-236) | Kalau WS feed mati DAN REST `fetch_ticker` juga exception, kode `return True, 0.0, 0.0` — slippage guard dilewati TOTAL, order diizinkan tanpa info harga sama sekali, justru saat kondisi API bermasalah | Sedang — **butuh keputusan desain dari user**: tetap fail-open (izinkan order supaya bot tidak macet) atau diubah fail-closed (blokir order kalau data harga sama sekali tidak tersedia)? | Ditemukan dari pembacaan kode, belum diuji eksperimen |
| 4 | `create_order` di `exchange.py` (baris ~221) dipanggil dari `_execute_market`/`_execute_limit`/`_execute_iceberg` | `amount_to_precision()` (pembulatan step-size exchange) terjadi SETELAH validasi `min_amount`/`min_cost` di `execute_signal` (baris ~130-155) memakai amount belum dibulatkan — kalau amount sangat dekat boundary, hasil pembulatan bisa jatuh di bawah minimum meski lolos validasi awal | Rendah (exchange akan reject via exception, tertangkap aman oleh try/except, tapi order jadi gagal tanpa alasan yang jelas ke operator) | Ditemukan dari pembacaan kode, belum diuji eksperimen |
| 5 | `_build_signal_origin` (baris ~717-718) | `sv.startswith("v")` akan `AttributeError` kalau `metadata["strategy_version"]` bukan tipe string (misal angka) — crash ini terjadi SEBELUM `save_trade`, jadi bisa menggagalkan pencatatan trade yang SUDAH benar-benar tereksekusi di exchange | Rendah-sedang (jarang terjadi, tapi dampaknya besar kalau kejadian: trade nyata gagal tercatat) | Ditemukan dari pembacaan kode, belum diuji eksperimen |

**Rencana fix yang disarankan untuk #1+#2** (harus dikonfirmasi/didiskusikan ke
user dulu sebelum diterapkan, sesuai `BAGIAN 5.5.3`): tambahkan pengecekan
eksplisit `order.get("status")` di `_execute_market` — kalau bukan
`closed`/`filled`, JANGAN langsung anggap gagal total (order market biasanya
langsung closed, tapi beri sedikit toleransi/polling singkat mirip `_poll_fill`
untuk kasus eventual-consistency), dan ganti pola `or` jadi pengecekan
`is not None` eksplisit untuk field `filled`. Solusi ini harus root-cause
(sesuai `BAGIAN 5.5.2`), bukan sekadar membungkus dengan try/except tambahan.

---

## BAGIAN 6 — METODOLOGI KERJA RINGKAS PER FILE (detail lengkap tetap ada di `BUG_HUNTER_PROMPT.md`, WAJIB dibaca juga)

Untuk SETIAP file yang dikerjakan, langkah-langkahnya:

1. **Baca konteks dulu**: entry `AUDIT_STATE.json` (histori audit putaran 1 untuk
   file ini), entry `jalur.json` (peta pemanggilan fungsi file ini), dan
   `fokus_cross_check` di `hunter_bug.json` untuk file ini.
2. **Baca file secara lengkap** kalau belum pernah dibaca di sesi ini atau sudah
   lama tidak dibaca ulang (jangan mengandalkan ingatan dari ringkasan saja).
3. **Untuk tiap fungsi publik dalam file**, cek dua arah:
   - `calls_out`: apakah target fungsi yang dipanggil benar-benar ada di file
     tujuannya? Apakah signature-nya (jumlah & tipe parameter, default value) cocok?
     Apakah konsisten async/sync (tidak ada fungsi async dipanggil tanpa `await`,
     atau sebaliknya)? Apakah return value yang diasumsikan oleh pemanggil BENAR-BENAR
     sesuai dengan apa yang SEBENARNYA di-return oleh fungsi tersebut (bukan
     asumsi/tebakan)?
   - `referenced_in_files`: buka SETIAP file yang terdaftar di sini, VERIFIKASI
     MANUAL dengan membaca baris kode aslinya — JANGAN percaya hasil grep begitu
     saja karena bisa false-positive untuk nama fungsi yang umum.
4. **Kalau ditemukan bug, LANGSUNG diperbaiki saat itu juga** (bukan sekadar dicatat
   untuk "nanti"). Tulis komentar `# [BUG-FIX]` di kode yang menjelaskan: sebelumnya
   bagaimana, sekarang bagaimana, dan kenapa. Lalu WAJIB dites:
   - `python3 -m py_compile <file>` — pastikan tidak ada syntax error.
   - Buat test fungsional yang MEMBUKTIKAN bug tersebut benar-benar terjadi SEBELUM
     fix (reproduce bug-nya), dan terbukti sudah benar SETELAH fix — bukan
     asumsi/spekulasi tanpa bukti nyata.
   - Compile-check SEMUA file lain yang ikut tersentuh oleh perubahan ini (termasuk
     file-file yang memanggil fungsi yang diubah).
5. **KALAU fix menyentuh LEBIH DARI 1 FILE — WAJIB dicatat di SEMUA file yang
   tersentuh** di `AUDIT_STATE.json` (pakai field
   `bug_fixed_tambahan_dari_audit_file_lain`), bukan hanya di file yang sedang jadi
   fokus utama. **Ini poin yang secara eksplisit pernah jadi masalah**: user pernah
   menemukan sesi sebelumnya lupa mencatat perubahan yang terjadi di `main.py`
   padahal fix utamanya dilakukan di `execution.py`/`risk.py`. **JANGAN ulangi
   kesalahan ini** — selalu cek dan catat dua sisi (atau lebih, kalau menyentuh
   banyak file).
6. **Kalau ada perubahan signature fungsi** (nama fungsi, jumlah/urutan parameter),
   jalankan `python3 tools/build_jalur.py` untuk regenerasi `jalur.json` supaya peta
   pemanggilan tetap akurat untuk audit selanjutnya.
7. **Update `hunter_bug.json`**: isi field `putaran.<nama_putaran>.status` menjadi
   `CROSS_CHECKED_FIXED` (kalau ada bug ditemukan & diperbaiki) atau
   `CROSS_CHECKED_CLEAN` (kalau tidak ada bug ditemukan setelah pengecekan
   menyeluruh), plus `catatan` ringkas yang menjelaskan apa yang dicek dan
   hasilnya.
8. **Commit & push**: SELALU `git fetch origin` dulu, cek `git log HEAD..origin/main
   --oneline` untuk memastikan tidak ada commit baru dari sesi lain sebelum commit
   (lihat Bagian 2).

### Pola bug yang paling sering ditemukan di sesi-sesi sebelumnya (pakai ini sebagai checklist mental saat cross-check):

- **Stale reference**: sebuah objek diganti saat runtime (misalnya lewat hot-reload
  config), tapi komponen lain masih memegang referensi ke objek versi LAMA.
- **Field response API yang selalu sama** (misalnya selalu bernilai `"success"` atau
  field `status` yang generic) terlepas dari hasil yang sebenarnya — hasil yang
  sesungguhnya ada di field LAIN yang tidak dicek oleh si pemanggil.
- **Parameter dead** (diterima fungsi tapi tidak pernah benar-benar dipakai di
  dalam fungsi tersebut), atau caller yang MENGIRA sedang meng-override sesuatu
  padahal sebenarnya diabaikan begitu saja.
- **Skala/unit mismatch**: contoh 0-100 vs 0-1 (persentase vs desimal), per-hari
  vs annualized (tahunan). **WAJIB dicek ekstra ketat untuk Tier 2** karena banyak
  formula matematis indikator teknikal di sana yang rawan salah skala.
- **Cache/state yang dideklarasikan tapi logic pemakaiannya tidak pernah ditulis**
  (dideklarasikan sebagai variable tapi tidak pernah benar-benar dibaca/ditulis
  ulang di alur eksekusi normal).
- **Guardrail yang bocor** untuk 1-2 cabang kode spesifik (kode melakukan `return`
  lebih awal SEBELUM sempat melewati pengecekan guardrail-nya).
- **Notifikasi/log yang salah label** (menyebutkan hasil/kondisi yang tidak sesuai
  dengan apa yang sebenarnya terjadi di sistem).
- **Cancel/rollback yang gagal tapi kode tetap melanjutkan proses dengan asumsi
  berhasil** — berisiko double-action (contoh nyata: bug double-fill di
  `execution.py` Tier 1 putaran 4 di atas).
- **Task/loop background yang mati permanen tanpa mekanisme restart** (self-healing)
  — contoh nyata: bug `_watch_tickers_all` mati permanen di `exchange.py` Tier 1
  putaran 4 di atas, bug paling serius yang ditemukan sejauh ini.

---

## BAGIAN 6.5 — METODOLOGI KETAT MAKSIMAL (WAJIB, MENGGANTIKAN/MENAMBAH BAGIAN 6 UNTUK RE-AUDIT)

Ini metodologi baru yang WAJIB dipakai untuk re-audit total (`BAGIAN 5.7`), dibuat
khusus supaya pola kegagalan di `BAGIAN 5.6` tidak terulang. **Ini bukan
pengganti `BAGIAN 6`, tapi lapisan tambahan yang WAJIB ditempelkan di atasnya.**

### 6.5.1 — DILARANG KERAS berhenti "setelah cukup menemukan bug"

- Tidak ada istilah "sudah ketemu 1-2 bug besar, cukup, lanjut file berikutnya."
  **SETIAP fungsi publik DAN privat** dalam file yang sedang diaudit harus
  diperiksa sampai selesai, terlepas dari berapa banyak bug yang sudah ditemukan
  duluan. Bug besar yang ditemukan di awal TIDAK mengurangi kewajiban memeriksa
  sisa fungsi lain di file yang sama.
- Tidak boleh ada pembatasan diri sendiri karena "pertimbangan waktu" seperti yang
  terjadi di sesi sebelumnya (lihat kutipan di `BAGIAN 5.6`). Kalau sebuah file
  besar (misal `main.py` 3300+ baris) butuh waktu lama untuk diperiksa tuntas,
  itu WAJAR — **lebih baik memeriksa 1 file benar-benar tuntas dalam waktu lama,
  daripada memeriksa banyak file secara dangkal dalam waktu singkat.**
- Kalau sebuah sesi harus berhenti karena keterbatasan lain (token API habis,
  dll), CATAT DENGAN JELAS di tracking fungsi/bagian mana persis yang BELUM
  sempat diperiksa dalam file yang sedang dikerjakan — jangan menandai file itu
  "selesai" hanya karena sesi harus berhenti.

### 6.5.2 — Checklist WAJIB per fungsi (tidak boleh ada yang dilewati)

Untuk **SETIAP** fungsi (bukan cuma fungsi yang "terlihat penting" atau yang
masuk hipotesis `fokus_cross_check`), jawab SEMUA pertanyaan berikut secara
eksplisit sebelum menyimpulkan fungsi itu aman:

1. **Nilai falsy vs missing**: apakah ada tempat yang memakai `or`/`if x:` untuk
   mengecek keberadaan sebuah nilai, padahal nilai `0`, `0.0`, `""`, `False`,
   atau list/dict kosong itu VALID secara data (bukan berarti "tidak ada")?
   Kalau ya, itu harus pakai `is not None` / `is not missing`, bukan truthy-check.
2. **Status/hasil operasi dicek atau diasumsikan?**: setiap kali memanggil
   fungsi lain yang bisa gagal SEBAGIAN (bukan exception total, tapi
   mengembalikan status/field yang menunjukkan gagal), apakah status itu
   BENAR-BENAR dicek sebelum melanjutkan seolah berhasil?
3. **Tipe data diasumsikan atau divalidasi?**: apakah kode mengasumsikan tipe
   data tertentu (string, angka, dict dengan key tertentu) dari data eksternal
   (response API, `.env`, metadata dict, config JSON) tanpa validasi, yang bisa
   crash atau salah kalau tipe/bentuknya berbeda dari asumsi?
4. **Urutan operasi**: kalau ada beberapa langkah berurutan (validasi → proses →
   simpan), apakah data yang divalidasi di langkah awal adalah PERSIS data yang
   sama yang dipakai di langkah akhir (bukan versi yang sudah diubah/dibulatkan
   di tengah jalan, seperti kasus `amount_to_precision` yang ditemukan)?
5. **Semua caller sudah dicek, bukan cuma yang "terlihat relevan"**: `jalur.json`
   `referenced_in_files` dan grep manual manual harus dicek SEMUA, termasuk
   caller yang keliatannya "tidak penting" — jangan menyaring caller mana yang
   dicek berdasarkan tebakan relevansi.
6. **Exception handling — fail-open atau fail-closed, dan apakah itu keputusan
   sadar atau kebetulan?**: setiap `try/except` yang menangkap error, tanyakan:
   kalau blok ini gagal, sistem lanjut jalan (fail-open) atau berhenti/menolak
   (fail-closed)? Apakah pilihan itu masuk akal untuk konteksnya (misalnya
   fail-open masuk akal untuk logging, tapi berbahaya untuk validasi keamanan
   seperti slippage guard)?
7. **Konsistensi lintas file untuk pola yang sama**: begitu menemukan SATU pola
   bug (misalnya truthy-check yang salah), **WAJIB grep seluruh codebase**
   untuk pola serupa (`or self\.`, `or 0`, `if \w+:` diikuti angka/field yang
   bisa legitimately 0) dan cek satu-satu apakah pola yang sama berulang di
   file/fungsi lain — jangan anggap itu kasus terisolasi hanya karena
   ditemukan di 1 tempat.

### 6.5.3 — WAJIB bikin eksperimen pembuktian, bukan cuma penalaran manual

- Untuk SETIAP kecurigaan bug yang levelnya signifikan (bisa berdampak ke uang,
  posisi, atau keputusan trading), **buat script eksperimen kecil** yang
  mensimulasikan skenario tersebut dengan fake object/mock (seperti
  `FakeExchange`/`FakeDB` yang sudah dicontohkan sesi ini) dan JALANKAN — jangan
  cuma menyimpulkan dari membaca kode + logika di kepala. Tunjukkan hasil
  eksperimen (before/after) ke user sebagai bukti, bukan cuma klaim.
- Untuk kesimpulan "file ini CLEAN, tidak ada bug" pada fungsi-fungsi yang
  menangani uang/eksekusi/posisi (Tier 1 & Tier 3 terutama), pertimbangkan juga
  membuat eksperimen NEGATIF — coba aktif "menjebol" fungsi itu dengan input
  aneh/ekstrem (nilai 0, None, string kosong, dict tanpa field yang diharapkan,
  angka negatif, response API yang dipotong/malformed) untuk membuktikan bahwa
  fungsi itu BENAR-BENAR menangani semua kasus itu dengan baik, bukan cuma
  "kelihatannya aman" dari membaca sekilas.
- Hapus semua file eksperimen sementara sebelum commit (sama seperti aturan file
  `_update_tracking.py` di Bagian 7) — eksperimen adalah alat verifikasi
  sesaat, bukan bagian dari kode produksi yang di-commit.

### 6.5.4 — Definisi "SELESAI" yang baru untuk sebuah file

Sebuah file HANYA boleh ditandai selesai untuk sebuah putaran/re-audit kalau
SEMUA hal berikut terpenuhi (bukan cuma "sudah dibaca sekilas"):
- Semua fungsi (bukan cuma yang di `fokus_cross_check`) sudah dijawab checklist
  6.5.2 di atas.
- Semua caller (dari `jalur.json` DAN grep manual tambahan) sudah dibuka dan
  dibaca langsung kodenya.
- Semua callee (fungsi yang dipanggil file ini, termasuk ke exchange/database
  layer) sudah diverifikasi ada, signature cocok, dan perilaku sebenarnya (bukan
  yang diasumsikan) sudah dipahami.
- Untuk temuan yang levelnya signifikan, sudah ada eksperimen pembuktian nyata
  (6.5.3), bukan cuma penalaran.
- Hasil akhirnya (bug ditemukan & sudah difix + tes, ATAU benar-benar clean
  setelah semua langkah di atas) sudah dicatat ke `AUDIT_STATE.json`/
  `hunter_bug.json` DAN sudah di-push ke GitHub.

---

## BAGIAN 7 — HAL-HAL PENTING LAIN YANG JANGAN SAMPAI TERLEWAT

- **Bahasa**: seluruh komunikasi dengan user DAN seluruh komentar di dalam kode
  memakai **Bahasa Indonesia**, mengikuti gaya yang sudah konsisten dipakai
  sepanjang sesi kerja ini.
- **Nada komunikasi**: profesional, teliti, transparan. Kalau ragu apakah sesuatu
  itu benar-benar bug atau memang desain yang disengaja — INVESTIGASI dulu (baca
  komentar di sekitar kode, pahami konteksnya) SEBELUM mengubah kode apa pun. Kalau
  setelah investigasi masih ragu, dokumentasikan sebagai `catatan_investigasi` di
  `AUDIT_STATE.json` — JANGAN memaksakan sebuah fix kalau sebenarnya itu bukan bug,
  cukup dokumentasikan keraguannya.
- **Semua bug yang ditemukan LANGSUNG diperbaiki saat itu juga**, TIDAK BOLEH hanya
  dicatat untuk dikerjakan "nanti"/ditaruh sebagai TODO list. User pernah secara
  eksplisit menanyakan hal ini di sesi sebelumnya, dan jawabannya konsisten: ya,
  langsung fix → tes → commit → push, bukan sekadar catatan.
- **Tidak ada audit yang bisa dianggap "100% pasti bersih total"** — ini poin meta
  yang pernah didiskusikan secara eksplisit dengan user: 5 putaran (1 sampai 5)
  menangkap SEBAGIAN BESAR kategori bug yang berbeda-beda, tapi ada titik
  diminishing returns setelah itu (semakin banyak putaran tambahan, semakin kecil
  kemungkinan menemukan bug baru dibanding usaha yang dikeluarkan). **Jujur soal
  keterbatasan ini kalau user bertanya** — jangan mengklaim kode 100% bebas bug.
- **Jangan membuat file tracking baru lagi.** Sudah cukup dengan struktur yang ada:
  3 file tracking (`AUDIT_STATE.json`, `hunter_bug.json`, `jalur.json`) + 1 dokumen
  metodologi (`BUG_HUNTER_PROMPT.md`) + dokumen handoff ini sendiri. **Jangan
  memecahnya lagi menjadi lebih banyak file** — ini juga poin yang secara eksplisit
  diminta user untuk dijaga.
- **Skill dokumen (docx/pptx/xlsx/dll) TIDAK RELEVAN untuk pekerjaan ini** — seluruh
  pekerjaan murni berupa bash/python/git di dalam sandbox, BUKAN pembuatan dokumen
  kantor.
- **File Python sementara** (misalnya script kecil untuk update isi file JSON
  tracking): buat di root repo dengan prefix underscore, contoh
  `_update_tracking.py`, jalankan sampai selesai, lalu **HAPUS file itu SEBELUM
  commit** (`rm _update_tracking.py`) — pastikan file sementara semacam ini
  **TIDAK ikut ter-commit** ke GitHub.
- **Kalau butuh install dependency Python untuk keperluan testing** (misalnya
  `pip install <nama_paket> --break-system-packages`), itu hal yang WAJAR dan sudah
  biasa dilakukan di sesi-sesi sebelumnya. Banyak dependency yang sudah pernah
  di-install sebelumnya untuk keperluan test import `main.py`/`telegram_bot.py`,
  di antaranya: `aiohttp`, `uvicorn`, `fastapi`, `ccxt`, `sqlalchemy`, `pandas-ta`,
  `aiosqlite`, `asyncio_throttle`, `websockets`, dan lain-lain. Kalau sesi baru ini
  jalan di sandbox baru (fresh), dependency-dependency ini mungkin perlu
  di-install ulang.

---

## BAGIAN 8 — CATATAN GOTCHA / HAL TEKNIS KECIL YANG PERNAH JADI ISU (supaya tidak terulang)

- Saat mengedit fungsi di `learning/cross_learn.py` untuk mengimplementasikan
  caching, sempat terjadi masalah di mana blok `try` pada method
  `get_peer_signal_scores` ikut terpotong secara tidak sengaja akibat proses edit —
  ini kemudian diperiksa ulang dan diperbaiki. **Pelajaran**: setelah melakukan edit
  yang mengubah struktur blok kode (menambah `try`/`if` pembungkus di sekitar kode
  lama), WAJIB baca ulang HASIL LENGKAP fungsi tersebut setelah edit untuk
  memastikan tidak ada blok yang ikut terpotong/rusak secara tidak sengaja — jangan
  hanya percaya pada asumsi bahwa str_replace berjalan sesuai rencana.
  `get_peer_regime_stats` diverifikasi TIDAK ikut terpotong (edit tersebut tidak
  mengganggu blok try-nya).
- Untuk pengecekan race condition/concurrency (relevan lagi kalau ditemukan pola
  serupa di tier lain): sebelum menyimpulkan ada risiko race condition, WAJIB cek
  apakah sudah ada mekanisme locking yang memadai (`asyncio.Lock()`, dedup set/queue,
  dll) — jangan langsung menyimpulkan "ini bug race condition" tanpa membaca kode
  locking yang mungkin sudah ada. Contoh: di Tier 1 putaran 4, awalnya dicurigai ada
  risiko race condition untuk eksekusi BUY simultan pada simbol yang sama, tapi
  setelah dicek lebih dalam ternyata SUDAH ADA mekanisme dedup (`_pipeline_active`,
  `_queued_symbols`) dan worker tunggal (single `run_gate3_worker`) yang membuat hal
  ini AMAN — bukan bug.
- Saat mengecek mekanisme cleanup state (misalnya `_pipeline_active.discard(symbol)`),
  WAJIB pastikan cleanup tersebut ada di dalam blok `finally` (bukan hanya di jalur
  "sukses" normal) supaya tetap ter-cleanup meskipun terjadi exception atau
  `asyncio.CancelledError` — kalau tidak, bisa menyebabkan symbol "stuck" selamanya
  dan tidak pernah bisa di-scan ulang.
- Ketika mengevaluasi apakah sebuah "dead code" yang terlihat mencurigakan itu bug
  atau bukan (contoh: `exchange.py` yang punya dua koneksi ccxt terpisah untuk
  `WebSocketFeed` dan `ExchangeConnector`), selalu baca konteks/komentar di
  sekitarnya dan pertimbangkan kemungkinan itu memang keputusan desain yang
  disengaja sebelum melabelinya sebagai bug.

---

## BAGIAN 9 — LANGKAH PERTAMA YANG HARUS DILAKUKAN SETELAH MEMBACA SEMUA INI

**⚠️ URUTAN INI BERBEDA DARI VERSI SEBELUMNYA — jangan langsung lompat ke Tier 2.**

1. Lakukan setup awal di Bagian 2 (minta token ke user, clone repo, config git).
2. Jalankan `git fetch origin` dan cek apakah ada commit baru sejak handoff ini
   dibuat (termasuk cek apakah temuan `BAGIAN 5.8` sudah sempat di-fix & commit
   oleh sesi lain sebelum sesi ini — kalau sudah, update Bagian 5.8 jadi
   "SUDAH DIFIX" dan jangan dikerjakan ulang).
3. Baca `BUG_HUNTER_PROMPT.md` secara PENUH di repo (dokumen metodologi detail),
   DAN baca ulang `BAGIAN 5.6`, `5.7`, `6.5` di dokumen ini sampai benar-benar
   paham — ini bagian PALING PENTING di seluruh handoff ini.
4. **Prioritas #1 — fix 5 temuan `execution.py` di `BAGIAN 5.8`** (terutama #1
   dan #2 yang kritis): diskusikan rencana fix ke user dulu (terutama untuk
   temuan #3 yang butuh keputusan desain), lalu fix root-cause + tes + commit +
   push, sesuai `BAGIAN 5.5.2` dan `BAGIAN 5.5.3`.
5. **Prioritas #2 — re-audit total `risk.py` dan `exchange.py`** dengan
   metodologi `BAGIAN 6.5` (belum pernah dilakukan dengan metode ketat ini),
   sama persis seperti yang sudah dilakukan ke `execution.py`: baca total,
   cross-check semua caller/callee, checklist per fungsi, eksperimen pembuktian
   untuk kecurigaan signifikan.
6. **Prioritas #3 — setelah Tier 1 benar-benar tuntas dengan metode baru**, baru
   lanjut re-audit Tier 2 sampai Tier 7 dengan metodologi yang sama (`BAGIAN
   6.5`), menggantikan rencana lama yang cuma mengandalkan `hunter_bug.json`
   `fokus_cross_check` sebagai batas pencarian.
7. **WAJIB commit progress secara berkala ke GitHub**, bukan ditunda sampai
   akhir sesi — setiap file selesai di-re-audit (baik ketemu bug atau bersih),
   langsung update `AUDIT_STATE.json`/`hunter_bug.json` (field
   `putaran_ulang_metode_ketat`, lihat `BAGIAN 5.7` poin 6) dan push.
8. **TIDAK PERLU bertanya ulang ke user "mau mulai dari mana"** — urutan di atas
   sudah final, kecuali user secara eksplisit meminta mengubah urutan.

---

**CATATAN PENTING soal dokumen handoff ini sendiri**: versi ini (dengan Bagian
5.6, 5.7, 5.8, dan 6.5) dibuat SETELAH ditemukan bug nyata di `execution.py`
lewat audit dadakan di luar alur putaran normal, sebagai reaksi langsung
terhadap pertanyaan user "kenapa bug itu masih bisa lolos". Dokumen final ini
**BELUM SEMPAT di-commit ke file tracking di GitHub** (`AUDIT_STATE.json`/
`hunter_bug.json` masih berisi status LAMA yang menyatakan Tier 1 "selesai
total") karena sesi yang membuat dokumen ini tidak sedang memegang token
push. **Tugas pertama sesi berikutnya (sebelum langkah 1-8 di atas): minta
token, lalu update `AUDIT_STATE.json` dan `hunter_bug.json` supaya statusnya
SINKRON dengan isi dokumen ini** (Tier 1 dari "SELESAI" jadi "PERLU RE-AUDIT",
tambahkan 5 temuan `BAGIAN 5.8` sebagai entry PENDING baru), commit dengan pesan
yang jelas, baru lanjut ke pekerjaan teknisnya.

---

**SELESAI. Ini adalah keseluruhan konteks yang perlu kamu ketahui untuk melanjutkan
sesi ini tanpa kehilangan satu pun detail penting: latar belakang proyek, definisi
istilah tier & putaran, status progres lengkap, bug-bug yang sudah ditemukan &
diperbaiki (jangan ditemukan ulang), TEMUAN BARU yang belum difix (Bagian 5.8),
evaluasi jujur kenapa bug bisa lolos sebelumnya (Bagian 5.6), instruksi reset
total audit (Bagian 5.7), metodologi ketat maksimal yang wajib dipakai mulai
sekarang (Bagian 6.5), aturan-aturan teknis (token, bahasa, file tracking),
gotcha teknis dari sesi sebelumnya, dan langkah kerja pertama yang harus segera
dilakukan.**
