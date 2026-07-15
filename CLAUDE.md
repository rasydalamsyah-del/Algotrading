# Rutinitas Kerja Standar — Proyek Algotrader (Side-Aware Scoring)

Instruksi ini merangkum pola kerja yang konsisten dipakai di sesi-sesi sebelumnya
(Batch 0–7, side-aware scoring untuk long/short). Ikuti alur ini setiap kali
mengerjakan fungsi/batch baru, kecuali diarahkan lain.

---

## ⚠️ WAJIB DIBACA PERTAMA — CHECKPOINT SESI TERAKHIR (belum di-commit)

Sesi sebelumnya terputus karena kuota mingguan habis, BUKAN karena selesai.
Sebelum melakukan apa pun yang lain, lakukan langkah verifikasi di bawah ini
secara nyata (jalankan betulan di sandbox, jangan diasumsikan dari catatan
ini) — catatan ini adalah state yang DIKLAIM terjadi, bukan bukti final.

### Konteks: sedang mengerjakan Batch 7, fungsi TERAKHIR (composite `orderbook_score`)

Tujuan fungsi ini: wiring `score_orderbook()` di `engine/indicators/orderbook.py`
supaya side-aware (punya versi `_short`), menyusul 3 sub-score orderbook lain
(`imbalance_score`, `whale_score`, `absorption_score`) yang sudah tuntas
side-aware di Batch 7 sebelumnya (262/262 test PASS).

### Tahap 0 (investigasi) — DIKLAIM SELESAI, verifikasi ulang kesimpulan berikut:
1. `composite_score` (alias `orderbook_score`, di luar file orderbook.py)
   dipakai di 2 tempat: `simulate_test.py:287` (cuma logging, bukan
   decision-making) dan `observer.py:307` fungsi `_compute_tf_score()`
   (mekanisme composite 7-kategori terpisah total, TIDAK punya parameter
   `side`, di luar cakupan proyek "24 sub-score, 8 batch" ini).
   → **Kesimpulan: `composite_score_short` TIDAK diperlukan.** Cukup
   `orderbook_score_short` — field generik yang dibaca `_pick_side_score`
   di `scorer.py`.
2. `spoofing_confidence` dihitung `round((pen_b + pen_a) / 2, 3)` — rata-rata
   komutatif dari `_spoofing_penalty(bids)` dan `_spoofing_penalty(asks)`,
   jadi arah-agnostic (dibuktikan matematis + empiris 2000 fuzz swap-test,
   0 mismatch). Sama seperti `spread_score`/`liquidity_score` — tidak perlu
   versi `_short`.
3. `_pick_side_score(obj, base_field, side)` di `scorer.py` generic: kalau
   `side="short"` dan `getattr(obj, base_field+"_short")` tidak None, pakai
   itu; kalau tidak, fallback ke `base_field`. Field `"ob_score"` sudah
   wired ke `_pick_side_score(iset.orderbook, "orderbook_score", side)` —
   TIDAK perlu ubah `scorer.py` sama sekali begitu `orderbook_score_short`
   terisi.

### Tahap 1 (implementasi) — progres yang DIKLAIM sudah dikerjakan, cek satu per satu apakah benar ada di kode:

1. **`engine/indicators/orderbook.py`** — `score_orderbook(data, side="long")`:
   - Signature diubah dari `score_orderbook(data: dict) -> float` jadi
     `score_orderbook(data: dict, side: str = "long") -> float`
   - Ditambah `suffix = "_short" if side == "short" else ""` lalu baca
     `imb_score = data.get(f"imbalance_score{suffix}", 50.0)`, sama untuk
     `whl_score` dan `abs_score`
   - `spr_score`, `liq_score`, `spoof_conf` TETAP dibaca tanpa suffix
     (identik di kedua sisi, sesuai kesimpulan Tahap 0)
   - Bobot (imbalance 40% / whale 25% / absorption 20% / spread 10% /
     liquidity 5%) dan formula penalti spoofing TIDAK berubah

2. **`score_orderbook_data()`** di file yang sama — ditambah baris:
   `result.orderbook_score_short = score_orderbook(data, side="short")`
   (tanpa mengubah baris `result.orderbook_score = score_orderbook(data)`
   yang sudah ada)

3. **`engine/core/models.py`**, class `OrderbookIndicators` — ditambah field:
   `orderbook_score_short: Optional[float] = None`, dengan komentar bahwa
   `composite_score` TETAP alias long-only dari `orderbook_score` (tidak
   dapat versi `_short`, sesuai kesimpulan 0.1)

4. **Verifikasi yang DIKLAIM sudah dijalankan dengan hasil sukses** (JALANKAN
   ULANG dari nol untuk konfirmasi, jangan percaya begitu saja):

   a) Fuzz test 20.000 kasus acak, bandingkan `score_orderbook()` baru
      (side="long") vs `score_orderbook_old()` (reimplementasi formula
      SEBELUM ada parameter `side`) — diklaim hasil `mismatches: 0 of 20000`.

   b) Skenario terkontrol imbalance dominan (`bid_ask_imbalance=0.9`,
      `imbalance_score=90.0`/`imbalance_score_short=10.0`, sub-score lain
      netral 50.0, `spread_score=80.0`) — diklaim hasil:
      `score_orderbook(data, side='long')` = 69.0
      `score_orderbook(data, side='short')` = 37.0
      (cocok dengan hitungan manual 90×0.4+50×0.25+50×0.2+80×0.1+50×0.05=69.0
      dan 10×0.4+50×0.25+50×0.2+80×0.1+50×0.05=37.0)

   c) **BELUM ADA HASIL — INI YANG PALING PENTING DIJALANKAN ULANG.**
      Command terakhir yang dikirim sebelum sesi terputus (limit habis),
      TIDAK sempat menghasilkan output. Jalankan ulang persis kode ini:

      ```python
      import random
      from engine.indicators.orderbook import calculate_orderbook, score_orderbook, reset_state, score_orderbook_data

      def make_bullish_book(n=20, seed=1):
          rng = random.Random(seed)
          bids, asks = [], []
          for i in range(n):
              bp = 100.0 - i*0.05
              ap = 100.1 + i*0.05
              bq = rng.uniform(0.5,2.0)*3.0  # bid-heavy -> imbalance & whale bid condong
              aq = rng.uniform(0.5,2.0)
              if i==2:
                  bq *= 15.0  # whale bid wall
              bids.append((bp,bq)); asks.append((ap,aq))
          return {'bids':bids,'asks':asks}

      sym='COMPOSITE-END2END/USDT'
      reset_state(sym)
      ob = make_bullish_book()
      calculate_orderbook(ob, symbol=sym)  # tick pertama isi state
      ob['symbol']=sym
      ind = score_orderbook_data(ob)
      print('orderbook_score(long)=', ind.orderbook_score, 'orderbook_score_short=', ind.orderbook_score_short)
      print('imbalance:', ind.imbalance_score, ind.imbalance_score_short)
      print('whale:', ind.whale_score, ind.whale_score_short)
      print('absorption:', ind.absorption_score, ind.absorption_score_short)
      print('spread:', ind.spread_score, 'liquidity:', ind.liquidity_score, 'spoof:', ind.spoofing_confidence)

      # neutral/empty book
      reset_state('EMPTY-COMPOSITE/USDT')
      res_empty = calculate_orderbook({}, symbol='EMPTY-COMPOSITE/USDT')
      print('empty composite long=', score_orderbook(res_empty, side='long'), 'short=', score_orderbook(res_empty, side='short'))
      ```

      Yang perlu diperiksa dari hasilnya:
      - Book bid-heavy (whale wall di bid) → `orderbook_score` (long) harus
        LEBIH TINGGI dari `orderbook_score_short`, konsisten dengan
        `imbalance_score`/`whale_score` yang condong ke long.
      - `res_empty` (book kosong) → `score_orderbook(res_empty, side='long')`
        dan `side='short'` harus SAMA-SAMA netral (kemungkinan 50.0, tapi
        VERIFIKASI, jangan asumsi — cek juga apakah `calculate_orderbook({})`
        mengembalikan dict kosong yang bikin semua `.get()` fallback ke
        default 50.0/80.0/1.0).

   Setelah hasil (c) keluar dan masuk akal, baru Tahap 1 dianggap benar-benar
   tuntas.

### Yang BELUM dikerjakan sama sekali:

- **Tahap 2**: cari & perbaiki 3 assertion lama di
  `engine/intelligence/test_category_score_side_aware.py` yang masih
  `assertEqual` (menegaskan `ob_score`/`orderbook_score` fallback ke long)
  — ada di 3 test class Batch 7: `TestBatch7ImbalanceScoreShort`,
  `TestBatch7WhaleScoreShort`, `TestBatch7AbsorptionScoreShort`, masing-masing
  test bernama pola `..._still_fallback_composite_not_wired_yet`. Ganti jadi
  `assertNotEqual` dengan komentar yang jelaskan kenapa (pola "assert stale,
  diperbaiki" yang konsisten sejak Batch 6).

- **Tahap 3**: tulis test class baru `TestBatch7OrderbookScoreCompositeShort`
  di file yang sama, isinya:
  - Regression long (angka statis + fuzz vs formula lama)
  - Verifikasi bobot: skenario di mana salah satu sub-score dominan, cek
    composite bergerak sesuai bobot yang diharapkan
  - Verifikasi lewat data order book sintetis end-to-end (bukan cuma
    unit-level dict)
  - Kasus neutral/edge (order book kosong, semua sub-score netral)
  - Integrasi: buktikan `_extract_indicator_scores` sekarang mengembalikan
    `orderbook_score` yang BERBEDA antara long dan short

- Jalankan test class baru, lalu regresi gabungan penuh: seluruh file
  `test_category_score_side_aware.py` + `engine/` + `future/test_capital_allocator.py`

- Kalau semua bersih — ini menandai **selesainya seluruh rencana 8 batch
  (24 sub-score, semua side-aware)**. Buat ringkasan akhir lengkap: berapa
  total test yang ada, apa saja yang sudah dikerjakan dari Batch 0 sampai 7,
  dan konfirmasi tidak ada bug yang pernah di-restart tanpa rencana matang
  selama seluruh proses ini.

### Catatan tambahan yang harus ikut didokumentasikan di ringkasan akhir nanti:

Temuan dari investigasi 0.1: `_compute_tf_score()` di `observer.py` (dan MTF
gate di `strategy.py`) masih 100% long-biased untuk mekanisme composite_score
di 7 kategori lain (`trend`, `momentum`, `strength`, `volatility`, `patterns`,
`oscillators`, `structure`) — ini di LUAR cakupan proyek "24 sub-score, 8
batch" yang sedang dikerjakan, dan harus didokumentasikan eksplisit sebagai
pekerjaan besar yang masih tertunda, bukan untuk dikerjakan sekarang, tapi
jangan sampai terlupakan.

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
   - Sum-to-100 check kalau relevan.
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

> "Lanjutkan kerjaan dari sesi sebelumnya di [nama batch/fungsi]. Ikuti
> rutinitas kerja standar di CLAUDE.md yang saya upload. Repo ada di
> [link GitHub] — clone dulu ke sandbox, baca [file spesifik], lalu mulai
> dari Tahap 0 (investigasi)."

Kalau sandbox butuh clone dari GitHub, pastikan "Allow network egress" ke
package manager (termasuk GitHub) sudah aktif di Settings > Capabilities.
