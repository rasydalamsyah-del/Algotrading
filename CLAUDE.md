# Rutinitas Kerja Standar — Proyek Algotrader (Side-Aware Scoring)

Instruksi ini merangkum pola kerja yang konsisten dipakai sejak Batch 0–7
(proyek "24 sub-score, 8 batch", SUDAH SELESAI) dan sekarang dilanjutkan
untuk proyek baru: **MTF composite side-aware** (`_compute_tf_score()` +
MTF gate). Ikuti alur ini setiap kali mengerjakan fungsi/batch baru,
kecuali diarahkan lain.

---

## ✅ STATUS: PROYEK "24 SUB-SKOR, 8 BATCH" (Batch 0–7) — SELESAI & CLOSED

Diverifikasi nyata (bukan asumsi), commit terakhir di GitHub `main`:
`c3dbaa1` ("Add files via upload", 2026-07-16 01:54:03 +0700) — berisi
`test_category_score_side_aware.py` versi final, di-push manual oleh
pemilik proyek dan sudah dicek `git diff origin/main` = kosong (identik).

Ringkasan akhir:
- **277 test PASS** di seluruh repo (0 gagal, 0 skip):
  - `test_category_score_side_aware.py`: 222 test
  - `test_regime_side_aware.py`: 23 test
  - `future/test_capital_allocator.py`: 32 test
- **Batch 0** — fondasi: `_pick_side_score()`, `_extract_indicator_scores()`.
- **Batch 1** — `pattern_score` (reflection: `100 - pattern_score`).
- **Batch 2** — `strength`: `di_score`, `volume_score`, `mfi_score`.
- **Batch 3** — `momentum`: `rsi_score`, `macd_score`, `stochrsi_score` (input-reflection).
- **Batch 4** — `oscillators`: `cci_score`, `williams_r_score`, `roc_score` (input-reflection).
- **Batch 5** — `trend`: `supertrend_score`, `ema_stack_score`, `cross_score`, `vwap_score`.
- **Batch 6** — `structure`: `ichimoku_score`, `sar_score`, `pivot_score`, `fib_score`.
- **Batch 7** — `orderbook`: `imbalance_score`, `whale_score`, `absorption_score`,
  + wiring composite `score_orderbook()` (langkah terakhir, tuntas sesi lalu).
- Semua fungsi skor per-indikator sekarang side-aware end-to-end sampai ke
  `scorer.py` (`_pick_side_score` → `ob_score` dkk), tanpa `scorer.py`
  perlu diubah lagi untuk kategori manapun.
- **Tidak ada bot/proses live yang di-restart** selama seluruh proyek ini
  dikerjakan dari sandbox — semua verifikasi dilakukan lewat clone repo
  lokal (git clone + `python3 -m unittest`), tidak ada akses ke VPS/proses
  produksi dari sandbox manapun.

**Catatan penting soal cakupan:** kategori `orderbook` punya
`orderbook_score_short` (composite side-aware), TAPI field `composite_score`
generik miliknya TETAP alias long-only ke `orderbook_score` (disengaja,
lihat kesimpulan Tahap 0 Batch 7 lama). Ini relevan untuk proyek baru di
bawah, karena `_compute_tf_score()` membaca `.composite_score`, BUKAN
`orderbook_score_short` — jadi orderbook pun ikut kena isu long-bias di
mekanisme MTF, walau sub-score-nya sendiri sudah tuntas side-aware.

Proyek "24 sub-score, 8 batch" ini **CLOSED, tidak ada Batch 8 di
dalamnya**. Pekerjaan lanjutan di bawah ini adalah **proyek terpisah**
dengan cakupan & penomoran batch sendiri (mulai dari 0 lagi), sengaja
dipisah supaya tidak tercampur dengan proyek yang sudah selesai di atas.

---

## 🔜 PROYEK BARU: MTF Composite Side-Aware (`_compute_tf_score` + MTF gate)

### Latar belakang masalah (sudah diverifikasi langsung ke kode, bukan catatan lama)

`engine/intelligence/observer.py`, fungsi `_compute_tf_score(iset: IndicatorSet) -> float`
(baris ~256–318): menghitung satu skor gabungan per timeframe dari 8 kategori
(`trend` 30%, `momentum` 25%, `strength` 25%, `volatility` 10%, `patterns` 10%,
`oscillators` 7%, `structure` 7%, `orderbook` 10% — dinormalisasi via
`total_weight`). **Fungsi ini TIDAK punya parameter `side` sama sekali** —
selalu membaca `iset.<kategori>.composite_score` (field long-only).

Hasil `_compute_tf_score()` dipakai di `observe()` untuk mengisi
`report.primary_tf_score` dan `report.confirmation_tf_score` (baris ~356, ~377).
Keduanya lalu dipakai di **hard MTF gate** di `engine/strategy_base.py`
(baris ~1042–1054):

```python
if confirmation_df is not None and confirmation_timeframe:
    if (not observation.confirmation_tf_valid) or (
        float(observation.confirmation_tf_score or 0.0) < float(profile.confirmation_min_score)
    ):
        return None  # sinyal diblokir
```

**Masalahnya:** `side` SUDAH tersedia di scope yang sama (dipakai beberapa
baris di atas untuk manggil `self._scorer.score(..., side)`, baris ~1036),
tapi gate ini membandingkan `confirmation_tf_score` — yang SELALU dihitung
versi long — terlepas dari apakah sinyal yang sedang dievaluasi itu long
atau short. Akibatnya: untuk sinyal **short**, gate ini mengevaluasi
"apakah timeframe konfirmasi cukup bullish", padahal yang seharusnya
dicek adalah "apakah timeframe konfirmasi cukup bearish" — bias long
yang sama persis dengan yang sudah diperbaiki di 24 sub-score, tapi di
level composite/MTF.

### Temuan investigasi per kategori (SUDAH diverifikasi baca kode langsung)

Dicek satu-satu: apakah `composite_score_short` sudah ada di masing-masing
`*Indicators` dataclass (`engine/core/models.py`), dan apakah fungsi
`score_<kategori>()` sudah punya parameter `side`:

| Kategori | Sub-score `_short` sudah ada? | `composite_score_short`? | Fungsi utama punya `side`? |
|---|---|---|---|
| `trend` | ✅ (Batch 5: ema_stack, supertrend, cross, vwap) | ❌ TIDAK ADA | ❌ `score_trend(df, errors, timeframe)` — tidak ada `side` |
| `momentum` | ✅ (Batch 3: rsi, macd, stochrsi) | ❌ TIDAK ADA | ❌ `score_momentum(...)` — tidak ada `side` |
| `strength` | ✅ (Batch 2: di, volume, mfi) | ❌ TIDAK ADA | ❌ `score_strength(...)` — tidak ada `side` |
| `patterns` | ✅ (Batch 1: pattern_score_short) | ❌ TIDAK ADA | ❌ `score_pattern(...)` — tidak ada `side` |
| `oscillators` | ✅ (Batch 4: cci, williams_r, roc) | ❌ TIDAK ADA | ❌ `score_oscillators(...)` — tidak ada `side` (walau sub-fungsi `score_cci` dkk sudah punya) |
| `structure` | ✅ (Batch 6: ichimoku, sar, pivot, fib) | ❌ TIDAK ADA | ❌ `score_structure(df, errors)` — tidak ada `side` (walau sub-fungsi `score_ichimoku` dkk sudah punya) |
| `volatility` | ❌ **TIDAK ADA SAMA SEKALI** (bb_score, kc_score, squeeze_score, atr_score — nol field `_short`, tidak pernah masuk 24 sub-score/8-batch) | ❌ TIDAK ADA | ❌ `score_volatility(...)` — tidak ada `side` |
| `orderbook` | ✅ (Batch 7, 3 sub-score) | N/A — `orderbook_score_short` ADA tapi bukan lewat field bernama `composite_score_short`, dan `.composite_score` tetap alias long-only | ✅ `score_orderbook(data, side)` sudah ada |

**Pola yang berulang persis seperti Batch 7 SEBELUM langkah terakhir
dikerjakan**: untuk `trend`, `momentum`, `strength`, `patterns`, sub-score
individual SUDAH side-aware lengkap (field `_short` terisi benar di
`score_<kategori>()` masing-masing — sudah diverifikasi lewat `grep`
langsung ke kode, bukan asumsi), TAPI baris `composite_score = ...` di
akhir tiap fungsi HANYA dihitung sekali dari field long, tidak ada
versi `_short`. Untuk `oscillators` dan `structure`: sub-fungsi tingkat
rendah (`score_cci`, `score_ichimoku`, dst) sudah punya parameter `side`
dan field `_short` sudah terisi di composite kategori — tapi
`score_oscillators()`/`score_structure()` sendiri (fungsi pembungkus yang
menghasilkan `composite_score`) masih belum punya `side`.

**`volatility` adalah kasus berbeda dan BELUM diverifikasi hipotesisnya:**
dugaan awal (BELUM dikonfirmasi, harus diinvestigasi ulang di Tahap 0 sesi
berikutnya) adalah bahwa `bb_score`, `kc_score`, `squeeze_score`, `atr_score`
mengukur **besaran volatilitas** (magnitude), bukan **arah** — sama-sama
relevan untuk long maupun short (analog dengan `spread_score`/
`liquidity_score` di orderbook yang terbukti arah-agnostic di Batch 7).
Kalau hipotesis ini benar setelah diverifikasi, `volatility` mungkin TIDAK
perlu sub-score `_short` sama sekali, dan `composite_score_short`-nya bisa
langsung disamakan dengan `composite_score` (atau kategori ini dikecualikan
dari pembobotan side-aware). **JANGAN diasumsikan benar tanpa dicek ulang
lewat kode & fuzz test** — ini baru dugaan dari investigasi sesi ini.

### 🚧 STATUS TERKINI PROYEK MTF (update setelah Sub-Batch A SELESAI 6/6)

**Base commit:** `c3dbaa1` (origin/main, sudah berisi test Batch 7 final).
Semua pekerjaan MTF di bawah ini **BELUM di-push ke GitHub** — masih berupa
perubahan lokal di sandbox terakhir. Kalau membuka sesi baru, file produksi
(`trend.py`, `momentum.py`, `strength.py`, `patterns.py`, `oscillators.py`,
`structure.py`, `models.py`, `test_category_score_side_aware.py`) versi
TERBARU ada di file yang sudah diunduh dari chat sebelumnya (diff per
sub-batch atau file utuh) — **APPLY DULU ke repo sebelum melanjutkan**,
jangan clone `c3dbaa1` mentah-mentah dan mulai dari nol lagi.

**✅ Sub-Batch A — SELESAI 6/6 kategori:**

| Kategori | Status | Catatan |
|---|---|---|
| `trend` | ✅ | `composite_score_short` wired. 11 test. |
| `momentum` | ✅ | wired + **1 bug produksi kritis diperbaiki** + `vwma_score_short` baru. 12 test. |
| `strength` | ✅ | wired + **1 bug produksi kritis diperbaiki** (identik momentum). 9 test. |
| `patterns` | ✅ | wired + `context_score_short` baru (provably sum-to-100 exact). 9 test. |
| `oscillators` | ✅ | wired. `roc_score` & komposit penuh tidak reliable dari tren monoton (kontrarian CCI/WR). 7 test. |
| `structure` | ✅ | wired + `market_structure_score_short` & `donchian_score_short` baru (provably symmetric). Bug kecil (composite_score_short None di early-return) ditemukan & diperbaiki sendiri. 10 test. |

Total test saat ini: **335** (277 lama + 58 test baru MTF), semua PASS,
regresi penuh bersih di setiap langkah.

**Sub-Batch A tuntas total — SEMUA 7 kategori (trend/momentum/strength/
patterns/oscillators/structure + orderbook dari Batch 7) sekarang punya
`composite_score_short` yang wired dan teruji.**

**🔴 BUG PRODUKSI KRITIS DITEMUKAN & DIPERBAIKI (di luar rencana awal,
penting utk konteks sesi berikutnya):**

1. **`momentum.py::score_momentum()`** — `rsi_score_short`, `macd_score_short`,
   `stoch_score_short` **TIDAK PERNAH disalin** dari sub-fungsi ke `result`.
   Sejak Batch 3, di produksi (`observer.py`→`score_momentum()`), field ini
   SELALU `None`. Batch 3 lolos test krn test integrasinya bypass
   `score_momentum()`. **Sudah diperbaiki.**
2. **`strength.py::score_strength()`** — bug IDENTIK (`di_score_short`,
   `volume_score_short`, `mfi_score_short`), sejak Batch 2. **Sudah diperbaiki.**
3. **`momentum.py` — `vwma_score`** (bobot 0.13, tidak pernah dapat
   treatment Batch 3) — dibuatkan `_score_vwma()` + `vwma_score_short`.
4. **`patterns.py` — `context_score`** (bobot 0.30, tidak pernah dapat
   treatment Batch 1) — dibuatkan `context_score_short` (reflection 100-x).
5. **`structure.py` — `market_structure_score` & `donchian_score`** (bobot
   0.20+0.15, tidak pernah dapat treatment Batch 6) — dibuatkan versi
   `_short` (keduanya provably symmetric by construction/aljabar).
6. **`structure.py::score_structure()`** — `composite_score_short` tetap
   `None` di early-return (`len(df)==0`, `current_price<=0`) — bug kecil
   ditemukan & diperbaiki SENDIRI di sesi ini (bukan warisan lama), dengan
   default aman `= 50.0` di awal fungsi.

**Pola penting utk sesi berikutnya (Sub-Batch B/C/D/E):** setiap kali masuk
kategori/fungsi baru, WASPADAI 2 pola bug yang sudah terbukti berulang:
(a) sub-fungsi return objek besar → hand-copy field ke `result`, field
`_short` KELUPAAN disalin (momentum/strength); (b) ada sub-indikator dlm
composite yang TIDAK PERNAH dapat treatment side-aware sebelumnya, padahal
genuinely directional (vwma di momentum, context di patterns,
market_structure+donchian di structure) — **JANGAN asumsikan semua
sub-indikator dlm satu composite otomatis sudah side-aware, cek SATU-SATU.**

**Karakteristik desain ditemukan (didokumentasikan, BUKAN bug, sengaja
tidak diperbaiki):**
- `trend.py::cross_score` — swap-symmetry tidak exact (gap_pct scale-
  dependent thd current_close).
- `momentum.py` komposit penuh — RSI+Stoch kontrarian (0.57) dominan atas
  MACD trend-following (0.30) → tren monoton KUAT malah condong short.
- `oscillators.py::roc_score` — swap-symmetry TIDAK exact SAMA SEKALI
  (200/200 fixture mismatch, akar sama dgn cross_score: ROC persentase
  relatif current-price). Ditambah `cci_score`/`williams_r_score` JUGA
  kontrarian (spt RSI/Stoch) → arah composite penuh thd tren monoton
  GENUINELY TIDAK RELIABLE (200 fixture: cuma 4/200 uptrend jelas favor
  long; downtrend malah 108/200 favor long, kebalikan intuisi).
- `structure.py::sar_score` & `fib_score` — TIDAK exact simetris (sudah
  didokumentasikan SEJAK Batch 6, diverifikasi ulang, bukan regresi baru).
- `strength.py::adx_score` — arah-agnostic (magnitude only), dipakai
  identik kedua sisi, TIDAK punya `_short`.
- `oscillators.py` & `structure.py` — punya `clamp_score()` LOKAL sendiri
  (TANPA `round(...,4)`, beda dari `models.py`) — bukan bug, tapi PENTING
  diperhatikan kalau menulis fuzz test baru (pakai presisi penuh, bukan
  `round(...,4)`, utk 2 file ini).
- `patterns.py` — composite PROVABLY sum-to-100 exact (kasus paling
  bersih, krn KEDUA komponennya reflection 100-x).

---

### Cakupan pekerjaan (status terkini per sub-batch)

Dipecah jadi sub-batch, ikuti alur kerja standar di bagian bawah file ini
untuk masing-masing (Tahap 0 investigasi → implementasi → test lengkap →
regresi penuh sebelum lanjut ke sub-batch berikutnya):

- **Sub-Batch A** (6 kategori: `trend`✅, `momentum`✅, `strength`✅,
  `patterns`✅, `oscillators`✅, `structure`✅) — wiring `composite_score_short`
  di masing-masing, persis pola Batch 7 langkah terakhir. **✅ TUNTAS 6/6**
  (lihat status detail di atas, termasuk 5 sub-skor baru & 2 bug produksi
  kritis yang ditemukan & diperbaiki di jalan). Lanjutkan ke Sub-Batch B.
- **Sub-Batch B** (`volatility`) — investigasi dulu (Tahap 0) apakah genuinely
  arah-agnostic seperti dugaan di atas; kalau tidak, baru perlu treatment
  sub-score `_short` penuh (setara 1 batch baru sendiri) sebelum composite
  bisa diwiring.
- **Sub-Batch C** (`orderbook`, penyesuaian kecil) — `_compute_tf_score()`
  perlu baca `orderbook_score_short` untuk short (bukan `.composite_score`
  yang alias long-only), TANPA mengubah apapun di `orderbook.py`/`models.py`
  itu sendiri (sudah closed di proyek lama).
- **Sub-Batch D** — `observer.py::_compute_tf_score(iset, side="long")`:
  tambah parameter `side`, baca `composite_score_short`/`orderbook_score_short`
  sesuai kategori ketika `side="short"`. Lalu `observe()` perlu dipanggil
  dengan `side` yang benar untuk mengisi `primary_tf_score`/
  `confirmation_tf_score` — **cek dulu apakah `observe()` punya akses ke
  `side` di titik panggilnya, atau perlu di-thread lagi dari
  `strategy_base.py`.**
- **Sub-Batch E** — `engine/strategy_base.py`, MTF gate (baris ~1042–1054):
  pastikan gate membaca skor sisi yang benar (butuh `observation` yang
  sudah dihitung dengan `side` yang tepat dari Sub-Batch D). Verifikasi
  logika perbandingan `< profile.confirmation_min_score` MASIH benar untuk
  short (harus tetap valid SELAMA `confirmation_tf_score_short` didesain
  dengan konvensi yang sama seperti semua field `_short` lain di proyek
  ini: "makin tinggi = makin favorable untuk sisi itu" — bukan cerminan
  negatif). **Verifikasi ini secara eksplisit, jangan asumsi gate perlu
  dibalik arahnya.**
- Setelah semua sub-batch selesai: regresi penuh seluruh test repo, lalu
  tulis ringkasan akhir proyek baru ini (sama seperti ringkasan Batch 0–7).

### Yang HARUS diverifikasi ulang dulu di awal sesi berikutnya (jangan percaya tabel di atas mentah-mentah)

1. Baca ulang kode `_compute_tf_score()` di `observer.py` — pastikan baris,
   bobot, dan daftar kategori masih sama seperti kutipan di atas (kode bisa
   saja berubah kalau ada commit lain masuk).
2. Baca ulang MTF gate di `strategy_base.py` — pastikan nomor baris & logika
   gate masih sama.
3. Jalankan ulang `grep composite_score` di semua file `engine/indicators/*.py`
   untuk konfirmasi tabel temuan di atas masih akurat.
4. `git log -1` dan `git fetch origin main` dulu sebelum mulai — pastikan
   commit yang dipakai adalah commit terbaru (`c3dbaa1` atau lebih baru),
   bukan clone lama yang basi.

---

## Prinsip Kerja Utama

- **Jangan asumsikan simetri atau perilaku kode.** Setiap fungsi bisa punya
  perilaku tidak simetris (contoh: `sar_score`, `pivot_score`, `fib_score`
  TIDAK simetris jarak dari titik acuan; `imbalance_score`, `vwap_score`
  genuinely simetris). Selalu verifikasi dulu lewat data riil sebelum menulis
  test atau mengubah kode.
- **Perubahan aditif.** Jangan hapus/ubah perilaku long yang sudah established
  kecuali eksplisit diminta. Tambahkan side-aware behavior di atasnya.
- **Testing menyeluruh sebelum percaya.** Fuzz test ribuan-puluh ribu kasus
  acak dibandingkan reimplementasi formula lama, bukan cuma sampel manual.
- **Jangan restart tanpa rencana matang.** Kalau ragu di titik mana pun,
  berhenti dan tanya dulu daripada lanjut dengan asumsi.
- **Sandbox tidak pernah punya akses ke bot/VPS live.** Semua verifikasi
  dilakukan lewat clone repo lokal — tidak ada risiko restart proses
  produksi dari sesi manapun yang memakai file ini.

## Alur Kerja per Fungsi/Batch

### 0. Investigasi (Tahap 0)
- Baca file terkait yang akan diubah.
- Grep/search seluruh codebase untuk memastikan pemakaian fungsi yang
  relevan (siapa yang manggil, di mana, apakah decision-making atau cuma
  logging).
- Jalankan sanity check interaktif via shell (`python3 -c "..."`) untuk
  verifikasi formula ASLI sebelum menulis test apa pun — jangan menebak.
- Kalau ketemu temuan penting (bug lama, ketidaksimetrisan, cakupan yang
  belum lengkap), dokumentasikan eksplisit sebagai catatan/keputusan,
  jangan diam-diam "diperbaiki" tanpa disebut.

### 1. Persiapan
- Update baris import di file test untuk menyertakan fungsi/module baru.
- Buat helper data generator kalau perlu (contoh: `_make_trend_df`,
  `_make_synthetic_book`, `_make_book_with_wall`, `_flat_book`) — selalu
  deterministik pakai `seed` tetap.

### 2. Struktur Test Class

Format kelas: `TestBatchN{NamaFungsi}Short(unittest.TestCase)`, isinya urut:

1. **Docstring** — jelaskan fungsi apa yang diuji, temuan investigasi kunci,
   kesimpulan simetris/tidak, cakupan yang sudah/belum diselesaikan.
2. **Section 1 — Long regression**
   - Test nilai statis (angka spesifik yang sudah diverifikasi).
   - Fuzz test byte-identical vs reimplementasi formula lama (ribuan—puluh
     ribu kasus acak).
   - Test lewat `score_structure()` / fixture OHLCV asli, bukan cuma
     unit-level dict.
3. **Section 2 — Swap-symmetry (multi-titik, via data real)**
   - Exact mirror points (kalau fungsi genuinely simetris).
   - Independent reconstruction: role-swap MANUAL (tukar variabel sendiri,
     bukan cuma manggil ulang fungsi yang sama) lalu jalankan formula yang
     sama.
   - Sum-to-100 check kalau relevan (HATI-HATI: sudah terbukti di Batch 7
     bahwa composite TIDAK selalu sum-to-100 kalau ada komponen arah-agnostic
     di dalamnya — cek dulu sebelum asumsi).
   - Verifikasi lewat data OHLCV/orderbook riil dua arah (uptrend vs
     downtrend, atau bid-heavy vs ask-heavy).
4. **"Bukan cuma beda angka"**
   - Pastikan short bukan cuma beda nilai tapi beda ARAH secara bermakna
     (favors short saat downtrend/bearish, favors long saat uptrend/bullish).
5. **Section 3 — Neutral-alignment**
   - Data kosong / tidak cukup bar / orderbook kosong / sisi hilang →
     long & short harus sama-sama netral (biasanya 50.0).
6. **Integrasi**
   - Test lewat `_extract_indicator_scores()` end-to-end.
   - Pastikan field `_short` benar-benar terisi beda dari long (bukan
     fallback diam-diam), atau kalau memang sengaja fallback sementara,
     tulis test yang mendokumentasikan itu eksplisit dengan catatan
     kenapa dan kapan akan diperbaiki.

### 3. Validasi
- Compile check dulu (`python3 -m py_compile <file>`).
- Jalankan test class baru saja.
- Jalankan regresi gabungan penuh: semua file test terkait di
  package + import sweep module-module utama, pastikan tidak ada yang
  pecah.
- Minta approval eksplisit sebelum menjalankan shell command yang bisa
  berdampak (terutama yang butuh append/modify banyak file).

### 4. Bersih-bersih & Lanjut
- Hapus dead code / komentar sisa proses reasoning yang ketinggalan
  (baca ulang diff sebelum lanjut).
- Update catatan cakupan (`[CAKUPAN]` di docstring) — apa yang sudah
  side-aware, apa yang masih fallback ke long sementara.
- Baru lanjut ke fungsi/batch berikutnya.

## Cara Pakai Prompt Ini

Di awal sesi (terutama di sandbox claude.ai, bukan Claude Code CLI),
kirim file ini beserta instruksi singkat, misalnya:

> "Lanjutkan proyek MTF composite side-aware. Ikuti rutinitas kerja standar
> di CLAUDE.md yang saya upload. Repo ada di [link GitHub] — clone dulu ke
> sandbox (`git fetch origin main`), lalu APPLY dulu SEMUA diff/file
> Sub-Batch A yang sudah saya kirim sebelumnya (trend/momentum/strength/
> patterns/oscillators/structure — SEMUA 6 kategori sudah selesai, lihat
> bagian STATUS TERKINI di atas) sebelum mulai. Sub-Batch A sudah TUNTAS
> 6/6 -- lanjut ke Sub-Batch B, kategori `volatility`, Tahap 0 (investigasi
> ulang, jangan percaya tabel di CLAUDE.md mentah-mentah — terutama cek
> ulang apakah bb_score/kc_score/squeeze_score/atr_score genuinely
> arah-agnostic seperti dugaan, dan waspadai 2 pola bug yang sudah terbukti
> berulang di Sub-Batch A: copy-omission & sub-indikator yang belum pernah
> dapat treatment side-aware)."

Kalau sandbox butuh clone dari GitHub, pastikan "Allow network egress" ke
package manager (termasuk GitHub) sudah aktif di Settings > Capabilities.
