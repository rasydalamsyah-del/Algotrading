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

**Status:** ✅ SELESAI (dikerjakan sbg proyek terpisah 19-20 Juli) — lihat
bagian "PROYEK: Root-Cause Fix `_calc_atr_percentile()` (Item Audit #4)"
di bawah utk detail lengkap. `classifier.py` SUDAH bermigrasi ke
`atr_percentile_normalized` (Opsi 1, direct switch), field lama tetap
ada sbg jalan mundur, regresi 824/824 PASS. **Bot BELUM direstart** —
fix ini belum aktif di produksi sampai restart dilakukan. Sub-Batch B
(MTF, proyek lama, sudah CLOSED) tetap jalan dengan `atr_score_short`
di-alias ke `atr_score` seperti semula — tidak disentuh oleh pekerjaan
#4 ini (`atr_score`/`atr_score_short` beda field dari `atr_percentile`,
lihat item audit #37 utk kaitannya).

---

## ✅ PROYEK: Root-Cause Fix `_calc_atr_percentile()` (Item Audit #4) — SELESAI, MENUNGGU RESTART

Proyek terpisah dari MTF Composite Side-Aware (yang sudah CLOSED di atas)
dan terpisah dari "24 sub-score, 8 batch". Base: `_calc_atr_percentile()`
me-ranking ATR **absolut** (dolar) terhadap window historis, bukan
`atr_pct` (ternormalisasi harga) — bias arah yang mempengaruhi regime
detection LIVE (`classifier.py::_is_volatile()`/`_calc_confidence()`).
Full detail blast-radius & investigasi awal ada di bagian "TEMUAN
TERPISAH" di atas. Entri backlog: `docs/AUDIT_FUNGSIONAL_MENDALAM.md`
item #4.

**Aturan kerja khusus proyek ini** (disepakati eksplisit karena menyentuh
regime detection live): dikerjakan tahap-per-tahap dengan checkpoint —
(a) investigasi Tahap 0, (b) test baseline dari NOL sebelum kode apa pun
diubah, (c) opsi perbaikan + trade-off dijelaskan, TUNGGU keputusan, (d)
implementasi, (e) regresi penuh, (f) update dokumentasi HANYA setelah
kode+test lolos. Tidak ada bot yang di-restart selama proyek ini
dikerjakan.

### ✅ Tahap 0 — Investigasi (selesai)
Baca tuntas `_is_volatile()`, `_calc_confidence()`, `_classify_raw()`,
`_calc_atr_percentile()`. Blast radius dikonfirmasi ulang via grep (bukan
cuma warisan catatan lama): `classifier.py` (decision-making, live),
`volatility.py::_score_atr()` (feeds composite), `spot/future
api_server_*.py` (read-only), `ta_compat.py::atr_percentile()` (dead
code, nol call site di luar filenya sendiri). Tidak ada bukti magic
number regime dikalibrasi thd bias (literal polos, nol komentar
kalibrasi).

### ✅ Data historis riil — TIDAK tersedia di sandbox
`./data/` kosong (lokasi default `DATABASE_URL` sqlite), dan bahkan
seandainya ada, tabel `ohlcv` (`engine/database.py::OHLCVBar`) adalah
**dead schema** — didefinisikan tapi tidak pernah ditulis/dibaca di
manapun di seluruh codebase (candle difetch live dari exchange, tidak
pernah dipersist). Validasi proyek ini karena itu pakai data SINTETIS
(random walk persentase, seed tetap, pola fuzz Sub-Batch B) —
didokumentasikan eksplisit di setiap test sebagai representatif, BUKAN
klaim "persis begini di pasar riil".

### ✅ Test baseline (50 test, `engine/intelligence/test_classifier_regime_baseline.py`)
`_is_volatile()`, `_calc_confidence()`, `_classify_raw()` sebelumnya NOL
test coverage di seluruh repo (dikonfirmasi: `test_regime_side_aware.py`
23 test beroperasi di level `MarketRegime` enum langsung, tidak pernah
construct `IndicatorSet`/panggil fungsi ini). Test baseline dibangun
MENGUNCI perilaku SAAT INI (termasuk bias yang belum diperbaiki) sbg
jaring pengaman before/after — mencakup semua breakpoint ADX, urutan
prioritas penuh `_classify_raw()` (validity → volatile → bear → bull →
ranging → undefined), plus karakterisasi kuantitatif bias `_calc_atr_percentile()`
via data sintetis (choppy kontrol, tren sedang/kuat) sebelum fix apa pun.

### ✅ Tahap A — Validasi independen `atr_pct` SEBELUM dipakai sbg basis fix
1. **Formula dikonfirmasi identik referensi resmi**: `atr_pct =
   (atr/close)*100` cocok PERSIS dgn source code `pandas_ta`
   (`atr(..., percent=True)` → `atr *= 100/close`, dicek langsung dari
   source, bukan dokumentasi/asumsi).
2. **Swap-symmetry test (geometric mirror, `anchor²/close`) menemukan
   `atr_pct` SENDIRI punya bias residual** — BEDA KATEGORI & JAUH LEBIH
   KECIL dari bug `_calc_atr_percentile()`: root cause smoothing-lag
   (ATR dolar di-Wilder-smooth dari bar-bar lampau yg harganya sudah
   beda level, baru dibagi close HARI INI). Terukur: tanpa drift ~0-8%
   relatif (noise-level); drift sedang ~10-25%; drift kuat sustained
   ~40-70%. Dikonfirmasi mekanismenya via sweep `period` (period=1 →
   ~2% nyaris nol; period=14 produksi → ~50%; period=28 → ~101%) — bukan
   formula salah, karakteristik struktural yg SAMA dgn `pandas_ta`
   (`atr(..., percent=True)` konstruksinya identik).
3. **Keputusan pemilik proyek**: LANJUT pakai `atr_pct` apa adanya
   sbg basis ranking (Opsi 1) — bias ini order-of-magnitude lebih kecil
   dari #4 & bukan blocker. Didokumentasikan sbg **item audit #37**
   terpisah di `docs/AUDIT_FUNGSIONAL_MENDALAM.md` (blast radius LEBIH
   LUAS dari #4: `risk_base.py` dynamic daily limit LIVE,
   `compute_adaptive_leverage()` LIVE, `validator.py`) — status "diketahui,
   di luar scope #4, risiko rendah di kondisi pasar normal", BUKAN
   diperbaiki di proyek ini.

### ✅ Tahap B — Perluas skenario data uji (2 fixture baru, 5 test baru)
Fixture Tahap A (drift konstan monoton) diperluas dgn 2 skenario lebih
realistis, seed TETAP (`PULLBACK_FIXTURE_SEED=10`, `REGIME_SHIFT_FIXTURE_SEED=20`,
dicatat eksplisit di kode utk dipakai ulang Tahap D):
- **Pullback berkala** (tren + retracement tiap 20 bar) — bias tetap ada,
  gap 13-50 poin tergantung kekuatan drift (lebih kecil & lebih "berisik"
  dari drift monoton murni, tapi arahnya konsisten).
- **Regime shift** (choppy 80 bar → trending kuat 100 bar → choppy 80 bar)
  — temuan baru: bias TIDAK langsung hilang saat regime kembali choppy,
  "melekat" (lag) ~30 poin gap di segmen choppy PASCA-transisi krn
  lookback window 100-bar masih memuat data periode trending. Segmen
  choppy SEBELUM shift jadi kontrol yg identik 100% (drift=0 di kedua
  arah) — bukti metodologi pengukuran sendiri tidak bias.

### ✅ Tahap C — Implementasi Opsi B (dual-field aditif)
`engine/indicators/volatility.py::calculate_atr_enhanced()`: `atr_pct`
dihitung sbg SERIES penuh (`atr_pct_series = atr_series/close_safe*100`,
bukan cuma bar terakhir), field baru `atr_percentile_normalized` diisi
via `_calc_atr_percentile(atr_pct_series, ...)` — REUSE fungsi ranking
yang SAMA persis (generic, tidak spesifik ke "ATR" apa pun), cuma input
series-nya beda dari `atr_percentile` lama. `engine/core/models.py`:
field baru `atr_percentile_normalized: Optional[float] = None` di
`VolatilityIndicators`, default `None` di semua early-return path
(konsisten konvensi `atr_percentile`).
**Bug copy-omission ditemukan & diperbaiki sendiri** (pola yg sama
persis diwanti-wanti CLAUDE.md sejak Sub-Batch A): `score_volatility()`
hand-copy field dari `calculate_atr_enhanced()` ke `result` satu-satu —
`atr_percentile_normalized` KETINGGALAN di baris copy tsb, ditemukan
sebelum jadi bug produksi (bukan sesudah), 1 baris fix.
`classifier.py` **TIDAK disentuh** (field lama `atr_percentile` tetap
dipakai, sesuai instruksi eksplisit). Field lama (`atr_percentile`,
`composite_score`, dst) dibuktikan byte-identical via regresi penuh
(murni aditif).

### ✅ Tahap D — Before/after pada seri IDENTIK (9 test baru)
Reuse 5 fixture Tahap A + 2 fixture Tahap B (seed sama persis), ukur gap
`atr_percentile` (lama) vs `atr_percentile_normalized` (baru):

| Skenario | gap lama | gap baru | reduksi |
|---|---|---|---|
| Choppy kontrol | 0.00 | 0.00 | — (tetap simetris) |
| Tren sedang | 36.67 | 1.81 | 95.1% |
| Tren kuat | 78.24 | -6.12 | 92.2% |
| Pullback sedang | 13.37 | -7.14 | 46.6% |
| Pullback kuat | 50.11 | -2.08 | 95.9% |
| Regime-shift, trending | 48.73 | 5.36 | 89.0% |
| Regime-shift, choppy PASCA-transisi | 29.75 | 8.00 | 73.1% |

Semua 7 skenario reduksi signifikan (46.6-95.9%), nol yang membesar —
kriteria sukses TERPENUHI. **Catatan penting**: beberapa skenario tanda
gap BERBALIK (bukan cuma mengecil ke nol) — EKSPEKTASI BENAR (field
baru mewarisi bias residual `atr_pct`/item #37, mekanisme berbeda dari
#4), dinilai berdasar MAGNITUDE (`|gap|`), bukan tanda. Efek "lingering"
Tahap B (bias melekat ke choppy pasca-transisi) TIDAK hilang total di
field baru, tapi tereduksi 73.1% — konsisten dgn sifat strukturalnya
(lookback window, bukan mekanisme ranking-absolut #4).

### ✅ Tahap E — Swap-symmetry lock-in (4 test baru)
Reuse metodologi geometric-mirror `test_atr_percentile_known_bug_documented_geometric_mirror`
(existing, Sub-Batch B) persis apples-to-apples. Field lama dikonfirmasi
TIDAK berubah (99.5 vs 2.5, byte-identical). Field baru: pada fixture
EKSTREM tunggal (seed=7, window pendek 120 bar drift tinggi tanpa
noise-reset) gap masih ~50 poin -- **BUKAN diklaim mendekati simetri
sempurna di titik ekstrem ini**, TAPI signifikan lebih baik dari gap
lama 97 poin (~48% reduksi bahkan di skenario terburuk). Karakterisasi
multi-seed (20 seed) lebih meyakinkan: gap lama konsisten parah (mean
>85, tidak pernah < 84 poin), gap baru mean ~80% lebih kecil & worst-case
jauh lebih rendah (bounded, bukan konsisten near-total-flip spt bug
lama).

Total test s.d. Tahap E: 68 (`test_classifier_regime_baseline.py`).
Regresi penuh: engine 504/504, spot 107/107, future 206/206 — nol
regresi, `classifier.py` dikonfirmasi byte-identical (belum disentuh
sampai titik ini).

### ✅ Tahap F — Validasi data riil (Binance, read-only, 2 bagian)

**Bagian 1 — temuan sampingan dicatat sbg item audit #38** (BUKAN
dikerjakan di proyek ini): validasi dampak riil utk temuan bias
Sub-Batch A/B (`bb_score`/`kc_score`/`atr_score_short`) ternyata BELUM
PERNAH dilakukan — semua divalidasi cuma via fuzz test sintetis, sama
seperti #4 sebelum Tahap F ini. Dicatat di
`docs/AUDIT_FUNGSIONAL_MENDALAM.md`, status "diketahui, bukan blocker,
tidak mendesak" — proyek MTF (Sub-Batch A/B) CLOSED, TIDAK disentuh
ulang.

**Bagian 2 — validasi data riil diperluas, 7 simbol × 22 periode ×
9 tahun histori** (BTC sejak 2017, DOGE/LINK/XRP/SOL/BONK/ARB dari
watchlist aktual `.env`, dipilih algoritmik dari statistik return/vol
riil — BUKAN cherry-pick):

| Kondisi riil | Simbol/tahun | Return |
|---|---|---|
| Bull run historis | BTC 2017 | +416% (90 hari) |
| Crash historis | BTC 2018 | -61.3% (90 hari) |
| Crash COVID | LINK Mar 2020 | -54.4% |
| Crypto winter Terra/Luna | LINK & SOL Apr-Jul 2022 (window kalender SAMA) | -65.6% / -75.6% |
| Krisis FTX | SOL Nov 2022 | -63.7% |
| Bull run awal 2021 | XRP | +528% (90 hari) |
| Pump meme ekstrem | DOGE Nov 2024, BONK 2025 | +106% / +227% |

Hasil: gap `atr_percentile` (lama) vs `atr_percentile_normalized` (baru)
pada 8 pasangan up-vs-down riil: **|gap| 0.34-3.59 poin** — jauh lebih
kecil dari sintetis (13-97 poin), krn pasar riil bergerak lewat ledakan
volatilitas singkat (volatility clustering) diselingi konsolidasi,
BUKAN drift konstan sepanjang seluruh window lookback 100-bar (25 jam)
spt fixture sintetis idealisasi (dibuktikan lewat zoom ke breakout BTC
Nov 2024: metrik melonjak benar saat breakout asli, lalu ternormalisasi
lagi setelah lewat — bukan bias, tapi volatilitas riil yg terdeteksi
benar). **TAPI arah bias tetap konsisten scr agregat**: gap lama rata2
bertanda **+1.68** (6 dari 8 pasangan searah hipotesis bug — uptrend
lbh tinggi dari downtrend), gap baru rata2 **-0.08** (nyaris nol, tilt
sistematis hilang) — fix genuinely menghilangkan bias arah rata-rata,
meski di level periode individual efeknya kecil dibanding noise pasar.
Semua data mentah (23 CSV, script fetch+analisis) tersimpan di
scratchpad sesi, bisa direproduksi ulang.

### ✅ Tahap G — Migrasi `classifier.py` (keputusan final: Opsi 1, direct switch)

**Keputusan pemilik proyek**: migrasi LANGSUNG (Opsi 1), BUKAN
staged-rollout via config flag (Opsi C yg tadinya dipertimbangkan) —
dasar keputusan: dampak riil kecil (Tahap F, gap 0.34-3.59 poin, bukan
puluhan poin spt sintetis) + arah bias tetap konsisten scr agregat
(worth fixing) + dual logging (bukan flag) sudah cukup sbg jaring
pengaman ringan tanpa kompleksitas tambahan.

**Implementasi**: `_is_volatile()` & `_calc_confidence()`
(`engine/intelligence/classifier.py`) sekarang baca
`iset.volatility.atr_percentile_normalized`, BUKAN `atr_percentile`
lagi. Field lama **TIDAK dihapus** dari `models.py` — tetap terisi
produksi (dashboard/API/referensi, jalan mundur kalau perlu rollback
tanpa perlu code change, cukup baca field lama lagi). 1 baris
`log.debug()` ditambahkan di tiap titik pemakaian (`_is_volatile()`
& `_calc_confidence()`), mencatat KEDUA nilai (lama vs baru)
berdampingan setiap panggilan — jejak retrospektif pasca-restart nanti,
tanpa flag config terpisah.

**Test**: 5 test lama (`TestIsVolatileBaseline`,
`TestCalcConfidenceVolatileExpansionBranch`,
`TestClassifyRawPriorityOrder`) di-update dari set field lama →
field baru (sesuai field yg SEKARANG benar-benar dibaca produksi) +
6 test baru (`TestItem4ClassifierMigrationDirectSwitch`) yg secara
eksplisit membuktikan: field lama diabaikan, field baru yg menentukan,
field lama tetap terisi di `calculate_atr_enhanced()` (bukti non-removal),
`log.debug()` mencatat kedua nilai, dan alur `_classify_raw()` end-to-end
memakai field baru. Total test proyek: **75**
(`test_classifier_regime_baseline.py`).

**Regresi penuh (dijalankan ULANG dari nol setelah sesi sempat
terputus di tengah jalan, integritas file diverifikasi dulu sebelum
regresi ulang)**: engine 511/511, spot 107/107, future 206/206 —
**824/824 total PASS, 0 gagal, 0 error**. Import sweep tambahan
(`classifier.py`, `observer.py`, `scorer.py`, `commander.py`,
`strategy_base.py`, `main_spot.py`, `main_future.py`,
`position_sync_spot.py`, `position_sync_futures.py`) — semua OK.

**Status akhir #4: SELESAI (implementasi + migrasi + regresi lolos).**
`atr_percentile` (lama) tetap ada di `models.py` sbg jalan mundur.
**Bot BELUM direstart** — keputusan restart TERPISAH, menunggu
pemilik proyek. **Belum di-push ke GitHub.**

---

## ✅ PROYEK: Root-Cause Fix `_paper_positions` Phantom Position (Item Audit #15) — SELESAI, MENUNGGU RESTART

Proyek terpisah dari #4, dikerjakan setelahnya (urutan disepakati:
#4 dulu, baru #15). Base temuan: `_paper_positions` (futures,
`FutureExchangeConnector`) adalah sumber kebenaran ke-3 independen dari
DB — urutan operasi close TIDAK atomic lintas paper-state & DB,
mekanisme KONKRET penyebab posisi phantom (temuan #10 lama). Investigasi
Tahap 0 (read-only) + simulasi (Tahap 1, tanpa bot live — bot sedang
mati, DB/log sudah dihapus manual) menemukan TIGA temuan konkret lewat
pembacaan kode langsung + reproduksi via test, BUKAN observasi live:

**Temuan A** — `close_position_with_retry()` (engine/database.py) TIDAK
PERNAH reset `is_closing=False` kalau semua retry gagal (cuma direset di
jalur SUKSES) — filter `not p.is_closing` di phantom detector (#10)
PERMANEN mengecualikan symbol itu, walau exchange genuinely sudah flat.

**Temuan B** — futures TIDAK PUNYA padanan
`spot::_reconcile_positions_on_startup()` sama sekali — beda dari spot,
phantom futures tidak self-heal bahkan lewat restart.

**Temuan C (paling kritis, berlaku exchange REAL)** — retry-close pada
posisi yang exchange-nya sudah flat (krn Temuan A) disalahartikan
`_simulate_order_fill()` sbg MEMBUKA posisi baru arah berlawanan —
dikonfirmasi genuinely retrigger via `run_sl_tp_monitor()` (gerbang
anti-duplikat cuma in-memory `_closing_symbols`, lepas otomatis lewat
`finally` terlepas dari kolom DB `is_closing`).

**Dependency A/C dibuktikan, bukan diasumsikan**: fix Temuan A (reset
`is_closing`) TIDAK mencegah Temuan C — dibuktikan empiris (test tetap
gagal dgn hasil sama sebelum C2/C1 ada, walau A sudah aktif) — keduanya
independen krn gerbang retrigger baca `_closing_symbols` in-memory,
bukan kolom `is_closing`.

### Opsi yang dipilih per temuan

- **Temuan A → Opsi A2**: reset `is_closing=False` terpusat di
  `close_position_with_retry()` (bukan di tiap caller bot), dibungkus
  try/except sendiri (kegagalan reset TIDAK menutupi exception asli).
  Terbukti AMAN — tidak menciptakan race baru (guard concurrency
  sebenarnya di in-memory, bukan kolom ini).
- **Temuan B → Opsi B2 minimal**: `_reconcile_phantom_positions_on_startup()`
  (futures, baru) reuse `find_untracked_positions()` ASLI, HANYA
  `phantom_candidates` (auto-close tanpa debounce, aman krn dipanggil
  sebelum task periodik dibuat — invarian `assert not self._tasks`,
  pola sama persis spot). `untracked`/`amount_mismatches` SENGAJA di
  luar scope (tetap jalur periodik existing). Koreksi diri penting saat
  implementasi: `fetch_binance_futures_positions()` RAISE saat fetch
  gagal, TAPI `find_untracked_positions()` (pembungkusnya) MENANGKAP
  exception itu & return `fetch_failed=True` — bukan re-raise. Fix:
  cek flag eksplisit, bukan cuma try/except.
- **Temuan C → Opsi C3 (kombinasi C1+C2)**: **C2** (verify-before-send)
  — `_do_close_position()` cek `_verify_position_exists_at_exchange()`
  SEBELUM kirim order; kalau exchange sudah flat, skip order sama
  sekali, langsung `_sync_db_close_without_order()` (harga terakhir
  diketahui, bukan fetch ulang). Fail-safe ke `True` kalau fetch GAGAL
  (bukan `False`) — mencegah kelas bug baru yang sama seriusnya
  (auto-close DB padahal posisi asli mungkin masih ada). **C1**
  (reduce-only backstop) — `reduce_only=True` diteruskan ke SEMUA jalur
  order (`_execute_market`/`_execute_limit`/`_execute_iceberg`,
  `engine/execution_base.py`) via `signal.metadata`; paper
  (`_simulate_order_fill()`) menolak (`ReduceOnlyRejected`) kalau order
  BUKAN genuinely mengurangi posisi existing berlawanan arah (termasuk
  kasus "ada tapi searah = nambah"); live memakai `params={"reduceOnly":
  True}` native (Binance/ccxt menolak sendiri). **Asimetri desain
  disengaja**: paper — rejection auto-sync DB langsung (kondisi pasti
  diketahui, deterministik); live — rejection JATUH ke jalur
  "CLOSE ORDER GAGAL" existing (retry counter + notify manual), TIDAK
  auto-sync, krn mem-parsing kode error exchange spesifik utk
  memastikan penyebabnya PASTI "sudah closed duluan" berisiko salah
  klasifikasi di uang sungguhan — proteksi utama live cukup "order
  ditolak, tidak pernah buka posisi salah arah".

### Bukti test (6 file baru, 63 test)

- `test_item15_paper_position_race_simulation.py` — reproduksi awal
  ketiga bug (paper connector langsung), diperbarui bertahap jadi bukti
  fix (Temuan A, lalu C2) seiring tiap tahap selesai.
- `test_item15_reconcile_phantom_startup.py` — 11 test Temuan B,
  termasuk 3 test fetch-gagal eksplisit (diminta khusus).
- `test_item15_verify_before_send.py` — 15 test C2 (unit
  `_verify_position_exists_at_exchange`/`_sync_db_close_without_order` +
  integrasi routing `_do_close_position()`).
- `test_item15_reduce_only_backstop.py` — 15 test C1, termasuk bukti
  TOCTOU spesifik (`assertLogs` konfirmasi C1 — bukan C2 — yang menutup
  celah race sempit, dikoreksi sendiri dari desain awal yang keliru
  memicu race 2x via panggilan verify terpisah).
- `test_item15_final_integration.py` — **Tahap 3**, 1 test end-to-end
  merekonstruksi race asli lewat pipeline produksi SUNGGUHAN (bukan
  stub) dgn Temuan A + C2 + C1 aktif bersamaan dalam satu alur 2 siklus
  SL/TP — hasil akhir: DB sinkron, exchange genuinely NOL posisi (bukan
  cuma "tidak long lagi"), nol order baru di siklus ke-2.
- 2 file test lama (`test_main_future_close_position_retry.py`,
  `test_main_future_reduce_position_amount_retry.py`) disesuaikan
  (exchange fake ditambah) supaya tetap menguji jalur normal (posisi
  ada, order dikirim) tanpa diam-diam berubah makna oleh C2/C1.

**Regresi penuh (final, semua tahap A+B+C1+C2+C3)**: engine 511/511,
spot 107/107, future 254/254 — **872/872 total PASS, 0 gagal, 0 error**.
Import sweep (`main_future`, `exchange_future`, `execution_future`,
`exchange_base`, `execution_base`, `spot/exchange_spot`,
`spot/main_spot`) — semua OK.

**Status akhir #15: SELESAI (Temuan A + B + C1 + C2, semua
diimplementasikan + diuji + regresi lolos).** **Bot BELUM direstart —
keputusan restart TERPISAH, menunggu pemilik proyek. Belum di-push ke
GitHub.**

**📌 Temuan sampingan DICATAT, SENGAJA belum diperbaiki (butuh keputusan
terpisah)**: `reduce_position_amount_with_retry()` (partial-close, item
#28) punya pola gap IDENTIK dgn Temuan A SEBELUM fix — `is_closing`
cuma direset di jalur sukses `reduce_position_amount()`, retry-exhausted
TIDAK menyentuhnya. Gap yang sama utk amount-mismatch detector (item
#36, filter `is_closing` sama persis spt phantom detector). Keputusan
eksplisit Temuan A HANYA menyebut `close_position_with_retry()` (full
close) — `reduce_position_amount_with_retry()` (partial close) BELUM
disentuh, didokumentasikan langsung di komentar kode
(`engine/database.py::close_position_with_retry()`) & di sini supaya
tidak hilang. Perlu keputusan terpisah kalau mau diperbaiki (kemungkinan
Opsi sama, A2-style, tapi butuh sign-off eksplisit sendiri).

---

## 📌 TEMUAN TERPISAH (audit fungsional item #16): `market_structure_score`/`donchian_score` belum masuk `LEVEL2_WEIGHTS`

Keputusan lama (2026-07-09, sebelum proyek MTF Composite Side-Aware ada):
kedua skor ini (`engine/indicators/structure.py`, bobot 0.20+0.15 di
`structure.composite_score`) dipakai gerbang MTF tapi SENGAJA tidak
dimasukkan ke `LEVEL2_WEIGHTS` (`engine/profiles/weights.py`, key
`"structure"` di keenam profil, cuma berisi ichimoku/sar/pivot/fibonacci)
— ditunda karena belum ada backtest.

**Diverifikasi ulang 2026-07-19 setelah proyek MTF Composite Side-Aware
(Sub-Batch A-E) selesai — keputusan DITUNDA masih berlaku, TAPI
konteksnya berubah:**
- Kedua skor tetap mencapai keputusan trading lewat jalur terpisah:
  `structure.composite_score` → `_compute_tf_score()` (`observer.py`,
  bobot kategori `structure` 0.07) → `primary_tf_score`/
  `confirmation_tf_score` → gerbang MTF (`strategy_base.py`).
- **Risiko paling berbahaya dari penundaan ini — bias arah (long-only) di
  jalur MTF tersebut — sudah TERTUTUP OTOMATIS** oleh Sub-Batch A-E:
  `composite_score_short` (mencakup `market_structure_score_short`/
  `donchian_score_short`, ditambahkan Sub-Batch A) sekarang genuinely
  dipakai untuk sinyal short sejak Sub-Batch D men-thread `side` sampai
  ke gate. Sebelum proyek ini selesai, sinyal short digerbang pakai
  `composite_score` versi long — bias yang sekarang sudah tidak ada.
- **Yang tersisa murni pertanyaan optimisasi** ("apakah kedua skor ini
  juga layak dapat bobot eksplisit di L1/`LEVEL2_WEIGHTS`, bukan cuma
  kontribusi implisit 0.07× ke MTF gate?"), BUKAN bug/risiko produksi —
  butuh backtest dulu, prinsip sama persis dengan `_calc_atr_percentile()`
  di atas.

Komentar lengkap juga ada langsung di kode, di
`engine/profiles/weights.py` tepat sebelum definisi `LEVEL2_WEIGHTS`.

**Status:** DITUNDA (tidak berubah), tapi alasan penundaan sekarang murni
optimisasi butuh-backtest, bukan lagi menyembunyikan risiko bias arah.
JANGAN tambahkan `market_structure_score`/`donchian_score` ke
`LEVEL2_WEIGHTS` manapun tanpa backtest & sign-off eksplisit terpisah.

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
