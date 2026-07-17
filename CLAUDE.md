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
- `volatility.py::bb_score`/`kc_score` — **KONTRARIAN/mean-reversion by
  design (formula LONG asli, TIDAK diubah, diverifikasi ulang di Sub-Batch
  B)**: reward `bb_position`/`kc_position` RENDAH (dekat lower band =
  "buy the dip"), BUKAN trend-following. Dibuktikan lewat fuzz 200 trial
  (choppy random-walk + bias, bukan cuma tren monoton lurus): rata-rata
  `bb_position` = 0.857 di fixture uptrend vs 0.165 di downtrend (SESUAI
  intuisi — uptrend memang mendorong harga dekat upper band) — TAPI karena
  formula scoring-nya kontrarian, `composite_score` (long, formula asli
  tidak berubah) rata-rata malah LEBIH TINGGI di downtrend (63.20) drpd
  uptrend (45.71). Akibatnya `composite_score_short` (role-swap exact dari
  formula yang sama) malah condong TINGGI di uptrend (long>short cuma
  5/200 trial uptrend, short>long cuma 11/200 trial downtrend) — kebalikan
  dari ekspektasi trend-following naif. Pola IDENTIK dgn temuan
  `momentum.py` (RSI+Stoch kontrarian dominan) & `oscillators.py`
  (CCI/Williams %R kontrarian) di atas — **arah composite volatility thd
  tren monoton/berbias TIDAK reliable buat interpretasi "favor long saat
  uptrend"**, sengaja TIDAK diperbaiki (perubahan formula long butuh izin
  eksplisit, di luar scope Sub-Batch B yang murni aditif). Test Sub-Batch B
  ditulis berdasarkan role-swap symmetry & arithmetic correctness yang
  TERBUKTI benar, BUKAN asumsi trend-following.

---

### ✅ Sub-Batch B (`volatility`) — TUNTAS (implementasi + test + regresi penuh PASS)

Hipotesis awal CLAUDE.md ("volatility mungkin genuinely arah-agnostic
semua") **TERBUKTI SALAH SEBAGIAN** setelah diverifikasi lewat kode +
fuzz test (bukan diasumsikan):

| Sub-skor | Arah-agnostic? | Kesimpulan |
|---|---|---|
| `bb_score` | ❌ TIDAK | Directional (favor `bb_position` rendah = dekat lower band = long-favorable, "buy the dip"). Confirmed via fuzz. |
| `kc_score` | ❌ TIDAK | Directional, pola sama seperti `bb_score` (favor `kc_position` rendah). Confirmed via fuzz. |
| `squeeze_score` | ✅ YA | Murni fungsi durasi/state squeeze (recency-based), tidak bergantung arah harga. Confirmed via fuzz (up=50.083 vs down=50.000, beda diabaikan). |
| `atr_score` | ⚠️ KASUS KHUSUS | Formula `_score_atr()` sendiri TIDAK punya logic arah eksplisit — tapi inputnya, `atr_percentile` (dari `_calc_atr_percentile()`), py ranking ATR **absolut** (dolar) terhadap window historisnya sendiri, BUKAN `atr_pct` yang sudah dinormalisasi harga. Di pasar yang trending kuat, ini menghasilkan bias sistematis terkait ARAH tren (percentile inflated saat uptrend, deflated saat downtrend) semata karena price-level drift, bukan perubahan volatilitas relatif yang genuine. Lihat detail lengkap di bagian "TEMUAN TERPISAH" di bawah. |

**Keputusan untuk Sub-Batch B (diambil pemilik proyek setelah investigasi
blast-radius, lihat bagian TEMUAN TERPISAH di bawah) — SUDAH diimplementasikan:**
- `bb_score_short` — role-swap `_score_bb(1 - bb_position, bb_width, bb_trending)`, wired di `calculate_bollinger_bands()`.
- `kc_score_short` — role-swap via helper `_score_kc()` yang diekstrak dari ladder inline lama (diverifikasi byte-identical thd ladder lama lewat fuzz 5000 trial sebelum dipakai), wired di `calculate_keltner_channels()`.
- `squeeze_score_short` — alias `= squeeze_score` (genuinely arah-agnostic, confirmed), wired di `detect_squeeze()`/`calculate_squeeze()`/`score_volatility()`.
- `atr_score_short` — alias `= atr_score` (BUKAN diklaim arah-agnostic — didokumentasikan eksplisit sebagai known limitation bias-terukur; root-cause fix di `_calc_atr_percentile()` DITUNDA, lihat bagian TEMUAN TERPISAH), wired di `calculate_atr_enhanced()`.
- `composite_score_short` — wired di `score_volatility()`, weighted average sama seperti long (BB 0.30, KC+Squeeze 0.30, ATR 0.40), termasuk early-return paths (default `SCORE_NEUTRAL`, bukan `None`).
- **models.py**: 5 field baru ditambahkan ke `VolatilityIndicators` (`bb_score_short`, `kc_score_short`, `squeeze_score_short`, `atr_score_short`, `composite_score_short`), semua default `None` (konsisten konvensi `_pick_side_score()`).
- **Test**: `TestMTFSubBatchBVolatilityCompositeShort`, 16 test baru di
  `test_category_score_side_aware.py` (static values, fuzz byte-identical
  extraction `_score_kc`, role-swap independent reconstruction, alias fuzz
  squeeze/atr, dokumentasi bug `atr_percentile` via geometric mirror,
  kontrarian "bukan cuma beda angka", neutral-alignment, integrasi
  `_extract_indicator_scores()`). Total test SEKARANG: **351** (296 di
  `test_category_score_side_aware.py` + 23 `test_regime_side_aware.py` +
  32 `future/test_capital_allocator.py`), semua PASS, regresi penuh bersih
  + import sweep (`observer.py`/`scorer.py`/`classifier.py`/`strategy_base.py`) OK.

---

## 📌 TEMUAN TERPISAH (BUKAN bagian Sub-Batch B/proyek MTF): blast-radius `_calc_atr_percentile()`

Ditemukan saat Tahap 0 Sub-Batch B, tapi scope-nya jauh lebih besar dari
wiring `composite_score_short` — **DITUNDA sebagai proyek terpisah**,
dicatat di sini supaya tidak hilang. **Jangan dikerjakan tanpa investigasi
awal & sign-off eksplisit terpisah**, karena menyentuh regime classification
yang dipakai bot **spot production live** (lihat memory
`project_spot_bot_production.md`: jangan restart bot live tanpa konfirmasi).

**Root cause:** `engine/indicators/volatility.py::_calc_atr_percentile()`
me-ranking `current_atr` (ATR **absolut**/dolar) terhadap window historis
ATR absolut juga (`atr_series.iloc[-lookback:]`), BUKAN `atr_pct` (ATR
sebagai % dari close, field yang SUDAH benar dinormalisasi harga dan
dipakai luas & aman di seluruh kode lain). Akibatnya, di pasar yang
sedang trending kuat, percentile ini bias mengikuti arah tren murni
karena price-level drift — bukan perubahan volatilitas relatif yang
genuine. Efek terukur dari fuzz test: tren sedang → geser ~8 poin
(54→46); tren kuat → geser ~70 poin (83→14), cukup besar untuk membalik
ambang `>=70` di kedua arah tergantung arah tren.

**1. Semua konsumen `atr_percentile`:**

| Konsumen | Tipe |
|---|---|
| `classifier.py::_is_volatile()` | **Decision-making.** Dicek PALING PERTAMA di `_classify_raw()`, sebelum trending bull/bear — bisa mem-preempt trend classification. |
| `classifier.py::_calc_confidence()` | **Decision-making.** Untuk regime `VOLATILE_EXPANSION`, `atr_percentile>=90` → confidence 0.88, gate `REGIME_MIN_CONFIDENCE_TO_TRADE`. |
| `volatility.py::_score_atr()` | Feeds `atr_score` → composite → `scorer.py` → strategy scoring (& nanti `_compute_tf_score()`). |
| `spot/api_server_spot.py:1951` | Read-only, expose ke API/dashboard, bukan gating. |
| `ta_compat.py::.ta.atr_percentile()` | Pola sama, TAPI **nol call site** di luar file itu sendiri — kode mati, tidak menambah blast radius. |

**2. Magic number "dikalibrasi terhadap bias"?** Tidak ditemukan bukti.
`REGIME_VOLATILE_ATR_PERCENTILE_MIN=70.0`, ambang `90.0` di
`_calc_confidence`, `0.12` BB-width — semua literal polos tanpa komentar
kalibrasi. Tidak bisa dibuktikan/disangkal lebih dalam tanpa git blame /
riwayat backtest (di luar scope investigasi statis).

**3. Dampak ke `test_regime_side_aware.py` (23 test):** **NOL test kena.**
Semua 23 test beroperasi di level `MarketRegime` enum/threshold matrix
sebagai INPUT langsung (`is_tradeable_regime()`, `ALLOWED_REGIMES`,
`DYNAMIC_THRESHOLD_MATRIX`) — tidak pernah construct `IndicatorSet` atau
panggil `classify()`/`_is_volatile()`/`_calc_confidence()`. TAPI ini juga
mengungkap: **`classifier.py`'s fungsi klasifikasi (`_is_volatile`,
`_calc_confidence`, `_classify_raw`) NOL test coverage di seluruh repo**
— perbaikan di sini butuh test baru dari nol, bukan divalidasi test yang
sudah ada.

**4. Skala pekerjaan:** BUKAN skala 1 kategori Sub-Batch A (yang murni
aditif, field `_short` baru, nol dampak ke consumer long yang sudah ada).
Fix `_calc_atr_percentile()` MENGUBAH nilai yang sudah dikonsumsi jalur
keputusan LIVE (`_is_volatile()` dicek pertama di `_classify_raw()`) —
bisa mengubah hasil `regime`/`confidence` bot spot production & future
yang sedang live. Kalau dikerjakan nanti: perlu investigasi terpisah,
test baru di level classifier (belum ada sama sekali), dan sign-off
eksplisit terpisah karena risiko produksi — bukan sesuatu yang aman
dilipat ke pekerjaan wiring `composite_score_short`.

**Status:** DITUNDA. Sub-Batch B jalan terus dengan `atr_score_short`
di-alias ke `atr_score` (known limitation, didokumentasikan, BUKAN
diklaim arah-agnostic).

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
- **Sub-Batch B** (`volatility`) — **✅ TUNTAS.** Tahap 0 membuktikan
  `bb_score`/`kc_score` directional (bukan arah-agnostic seperti dugaan),
  `squeeze_score` genuinely arah-agnostic, `atr_score` known-limitation
  (bias `_calc_atr_percentile`, fix DITUNDA sbg proyek terpisah). Semua
  `_short` wired + 16 test baru + regresi penuh 351 test PASS. Lihat detail
  lengkap di bagian status Sub-Batch B di atas. Lanjutkan ke Sub-Batch C.
- **Sub-Batch C** (`orderbook`, penyesuaian kecil) — **✅ TUNTAS.**
  `_compute_tf_score(iset, side="long")` (`observer.py`) sekarang menerima
  parameter `side` (default `"long"`, non-breaking) dan baca baris orderbook
  lewat `_pick_side_score(iset.orderbook, "orderbook_score", side)` (helper
  yang sama dipakai `scorer.py`, diimpor lintas modul — dicek dulu tidak ada
  circular import) — bukan lagi `.composite_score` yang alias long-only.
  `side="long"` hasilnya IDENTIK persis dengan sebelum perubahan (dibuktikan
  test, bukan diasumsikan). **7 kategori lain (trend dst) SENGAJA belum
  disentuh** — masih baca `.composite_score` polos terlepas dari `side`,
  itu cakupan Sub-Batch D. Tidak ada perubahan di `orderbook.py`/`models.py`
  (sesuai batasan). 6 test baru (`TestMTFSubBatchCObserverOrderbookShort`)
  — termasuk fallback saat `orderbook_score_short=None`, isolasi 7 kategori
  lain tidak ikut berubah oleh `side`, dan default-call (tanpa argumen
  `side`) identik dengan `side="long"` eksplisit. **Catatan implementasi:**
  `PatternIndicators.is_valid()` ternyata `return True` tanpa syarat
  (quirk pre-existing, ditemukan saat menulis test — patterns SELALU ikut
  weighted average dengan `composite_score` default 50.0, weight 0.10,
  terlepas dari apa yang di-set) — bukan bug baru, di luar cakupan Sub-Batch
  C, tapi WAJIB diperhitungkan kalau menulis test baru untuk
  `_compute_tf_score()` di sub-batch berikutnya (expected value harus
  menyertakan kontribusi 0.10×50.0 dari patterns kalau tidak di-override).
  Regresi penuh 361 test PASS + import sweep (`observer.py`/`scorer.py`/
  `strategy_base.py`/`main_spot.py`/`main_future.py`) OK. Lanjutkan ke
  Sub-Batch D.
- **Sub-Batch D** — **✅ TUNTAS.** `_compute_tf_score()` sekarang side-aware
  penuh di ke-8 kategori (7 kategori lain + orderbook dari Sub-Batch C),
  semua lewat `_pick_side_score(iset.<kategori>, "composite_score", side)` —
  pola seragam, tidak menyentuh `indicators/*.py`/`models.py` sama sekali
  (semua field `_short` sudah wired sejak Sub-Batch A/B).
  `observe()` (module-level) & `MarketObserver.observe()` (method, dipanggil
  `run_in_executor` dari `strategy_base.py::get_scored_signal()`) SEKARANG
  menerima `side: str = "long"` dan meneruskannya ke KEDUA panggilan
  `_compute_tf_score()` (primary & confirmation TF). `get_scored_signal()`
  SUDAH punya parameter `side` sejak lama (dukungan short sebelumnya) tapi
  **TIDAK PERNAH** meneruskannya ke `observer.observe()` — sekarang
  diteruskan (`strategy_base.py` baris ~938-951).
  **Temuan krusial Tahap 0 (bukan di rencana awal CLAUDE.md, ditemukan saat
  investigasi caching):** `_OBSERVATION_CACHE` (module-level, dipakai lintas
  thread via `run_in_executor`) sebelumnya di-key HANYA
  `symbol|timeframe|bar_timestamp` — TANPA `side`. Begitu `primary_tf_score`
  genuinely beda per side (persis efek Sub-Batch D ini), cache hit untuk
  `side="short"` bisa diam-diam mengembalikan `ObservationReport` yang
  dihitung untuk `side="long"` dari request sebelumnya pada bar yang sama
  (silent cross-side contamination) — kalau tidak diperbaiki, MTF gate di
  Sub-Batch E bisa membaca skor sisi yang SALAH tanpa ada error apapun.
  Fix: `_cache_key()` sekarang menyertakan `side` sebagai SUFFIX (bukan
  disisipkan di tengah), supaya `get_cached_observation()`/`clear_cache()`
  (yang match via `key.startswith(f"{symbol}|{timeframe}|")`) tetap benar
  tanpa perlu diubah. Diverifikasi via test eksplisit (panggil long→short→
  long berturut-turut pada bar yang sama, pastikan tidak ada cross-
  contamination DAN cache-hit versi long masih benar di panggilan ketiga).
  side="long" (default, SEMUA caller existing —
  `position_sync_futures.py`/`position_sync_spot.py` tidak pernah kirim
  side ke `observe()`, tetap aman krn keduanya tidak konsumsi
  `primary_tf_score`/`confirmation_tf_score`) hasilnya IDENTIK PERSIS dgn
  sebelum Sub-Batch D — diverifikasi test, bukan diasumsikan.
  **Catatan minor, di luar cakupan:** `BaseStrategy.get_scored_signal()`
  (abstract method, baris ~237) signature-nya TIDAK menyertakan `side`
  (beda dari implementasi konkret di baris ~913) — inkonsistensi
  pre-existing, Python ABC tidak menegakkan signature match jadi tidak
  crash, TIDAK disentuh (di luar cakupan Sub-Batch D).
  11 test baru (`TestMTFSubBatchCObserverOrderbookShort` diperluas +7,
  `TestMTFSubBatchDObserveSideThreading` baru +5, 1 test lama dari
  Sub-Batch C di-update krn asersinya sengaja mengunci perilaku SEMENTARA
  Sub-Batch C yang sekarang sudah berubah oleh Sub-Batch D). Regresi penuh
  367 test PASS + import sweep (`observer.py`/`scorer.py`/`strategy_base.py`/
  `main_spot.py`/`main_future.py`/`position_sync_futures.py`/
  `position_sync_spot.py`) OK. Lanjutkan ke Sub-Batch E.
- **Sub-Batch E** — **✅ TUNTAS. VERIFIKASI SAJA, NOL PERUBAHAN KODE PRODUKSI.**
  MTF gate (`engine/strategy_base.py::get_scored_signal()`, ~baris 1048-1060)
  dicek 2 hal, keduanya terbukti SUDAH BENAR tanpa perlu diubah:
  1. **`observation` yang dibaca gate sudah side-aware** — `observation`
     adalah hasil LANGSUNG dari `self._observer.observe(..., side)`
     beberapa baris di atas, DI DALAM FUNGSI YANG SAMA — otomatis benar
     begitu Sub-Batch D selesai, tidak ada kode tambahan diperlukan di sini.
  2. **Arah perbandingan `< profile.confirmation_min_score` TIDAK PERLU
     dibalik untuk short** — dibuktikan lewat data riil (bukan penalaran
     teoretis), lewat `get_scored_signal()` SUNGGUHAN (bukan mock) via
     instance `VolumetricBreakoutStrategyBase` konkret, fixture downtrend
     250 bar riil: `confirmation_tf_score` long=38.94 vs short=54.53 (pada
     bar & data yang SAMA PERSIS) — gate yang membandingkan `< threshold`
     (40.0) BENAR memblokir long (38.94<40) dan meloloskan short
     (54.53≥40) TANPA perlu dibalik arahnya, karena seluruh 8 kategori
     sub-skor `_short` sudah dibangun dengan konvensi "makin tinggi = makin
     favorable untuk sisi itu" sejak Sub-Batch A-C. Fixture uptrend
     (mirror) dicek juga: blokir short, loloskan long. `profile.
     confirmation_min_score` sendiri dikonfirmasi TIDAK punya varian
     `_short` di `thresholds.py` — BENAR by design, threshold-nya flat,
     yang side-aware adalah skor yang dibandingkan.
  6 test baru (`TestMTFSubBatchEGateVerification`), semua lewat jalur
  produksi ASLI end-to-end (observer→classifier→scorer→MTF gate, bukan
  reimplementasi logic gate). Regresi penuh 370 test PASS + import sweep OK.
- **✅ SEMUA SUB-BATCH (A-E) TUNTAS.** Proyek MTF Composite Side-Aware
  selesai — lihat ringkasan akhir di bagian bawah CLAUDE.md ini.

---

## ✅ STATUS: PROYEK "MTF COMPOSITE SIDE-AWARE" (Sub-Batch A–E) — SELESAI & CLOSED

Base commit `c3dbaa1` (origin/main). Semua pekerjaan Sub-Batch A-E **BELUM
di-push ke GitHub** — masih lokal di sandbox terakhir, perlu di-apply dulu
ke repo baru sebelum sesi berikutnya lanjut ke pekerjaan lain.

Ringkasan akhir:
- **370 test PASS** di seluruh repo (0 gagal, 0 skip): 296 (Sub-Batch A/B,
  `test_category_score_side_aware.py`) + 74 test baru Sub-Batch C/D/E +
  23 (`test_regime_side_aware.py`) + 32 (`future/test_capital_allocator.py`)
  + 4 (`engine/test_exchange_base.py`, dari perbaikan `load_markets`
  terpisah) — total breakdown persis: `test_category_score_side_aware.py`
  370-23-32-4 = 311 test.
- **Sub-Batch A** — `composite_score_short` wired di 6 kategori (trend/
  momentum/strength/patterns/oscillators/structure), +2 bug produksi
  kritis ditemukan&diperbaiki (momentum/strength copy-omission field
  `_short` tidak pernah tersalin ke `result`), +5 sub-skor baru yang
  sebelumnya tidak pernah dapat treatment side-aware sama sekali (vwma,
  context, market_structure, donchian).
- **Sub-Batch B** (`volatility`) — hipotesis awal "genuinely arah-agnostic"
  TERBUKTI SALAH SEBAGIAN: `bb_score`/`kc_score` directional (kontrarian/
  mean-reversion by design), `squeeze_score` genuinely arah-agnostic,
  `atr_score` known-limitation (bias `_calc_atr_percentile`, root-cause fix
  DITUNDA sbg proyek terpisah — lihat bagian "TEMUAN TERPISAH" di atas,
  BELUM dikerjakan, blast radius menyentuh regime classification live).
- **Sub-Batch C** (`orderbook`) — `_compute_tf_score()` baca
  `orderbook_score_short` lewat `_pick_side_score()`, bukan lagi
  `.composite_score` yang alias long-only.
- **Sub-Batch D** — `_compute_tf_score()` diperluas side-aware penuh ke 7
  kategori lain + `observe()`/`MarketObserver.observe()` menerima & meneruskan
  `side`, threading dari `strategy_base.py::get_scored_signal()` (yang
  SUDAH punya parameter `side` tapi TIDAK PERNAH meneruskannya ke
  `observe()` sebelum ini). **Bug tambahan ditemukan & diperbaiki di jalan**
  (di luar rencana awal): `_OBSERVATION_CACHE` tidak menyertakan `side` di
  cache key — berpotensi silent cross-side contamination begitu skor
  genuinely beda per side. Fixed via suffix `|side` di `_cache_key()`.
- **Sub-Batch E** — MTF gate diverifikasi (BUKAN diubah) — `observation`
  sudah otomatis side-aware dari Sub-Batch D, arah perbandingan
  `< confirmation_min_score` TERBUKTI benar utk short via data riil
  (fixture downtrend: long=38.94 diblokir, short=54.53 diloloskan, threshold
  40.0) tanpa perlu dibalik. Nol perubahan kode produksi di sub-batch ini.
- **Prinsip yang terbukti berulang kali krusial:** verifikasi lewat DATA
  RIIL (fuzz test, fixture OHLCV sungguhan, `get_scored_signal()` end-to-end
  sungguhan) di atas penalaran teoretis semata — beberapa kali asumsi awal
  (volatility arah-agnostic, gate perlu dibalik) TERBUKTI SALAH atau
  TERBUKTI BENAR hanya setelah dicek dgn cara ini, bukan ditebak.
- **Tidak ada bot/proses live yang di-restart** selama seluruh proyek MTF
  ini — sesuai prinsip yang sama dgn proyek 24 sub-score sebelumnya.

**Yang SENGAJA di luar cakupan proyek MTF ini (dicatat di bagian "TEMUAN
TERPISAH" di atas, JANGAN dikerjakan tanpa investigasi & sign-off terpisah):**
root-cause fix `_calc_atr_percentile()` (bias ranking ATR absolut, bukan
`atr_pct`) — blast radius menyentuh regime classification yang dipakai bot
**spot production live** (lihat memory `project_spot_bot_production.md`).

Proyek "MTF Composite Side-Aware" ini **CLOSED**. Pekerjaan lanjutan
(root-cause `_calc_atr_percentile`, atau proyek baru lainnya) adalah
**proyek terpisah** dengan penomoran sub-batch sendiri, sengaja dipisah
supaya tidak tercampur dengan proyek yang sudah selesai di atas.

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
