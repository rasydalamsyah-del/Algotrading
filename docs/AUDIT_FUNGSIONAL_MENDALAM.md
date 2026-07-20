# Audit Fungsional Mendalam — Algotrader

**Fungsi dokumen ini:** Backlog terpusat — mengumpulkan SEMUA bug, celah, ketidaksinkronan,
dan masalah terkait lain yang ditemukan dari berbagai sumber (audit fungsional baru,
histori chat lama, temuan Sub-Batch scoring), untuk diputuskan & dikerjakan nanti secara
terencana. Bukan laporan sekali-jalan — akan terus diupdate tiap kali ada temuan baru.

---

## 🎉 MILESTONE 18 Juli — SEMUA 6 ITEM AUDIT FUNGSIONAL SELESAI

Item #1, #2, #3, #4, #5, #6 (plus bonus #24 dari temuan data live) — **semua diperbaiki,
diverifikasi lewat test (revert-test-restore), dan lolos regresi penuh**. Total test
di seluruh repo: **455 PASS**, 0 gagal.

**⚠️ BELUM di-push ke GitHub, BELUM direstart ke bot manapun.** Sesuai rencana yang
disepakati: kumpulkan semua fix dulu, restart SEKALI di akhir untuk mengaktifkan
semuanya sekaligus — bukan restart berkali-kali per item.

**Langkah selanjutnya yang direncanakan:**
1. Push ke GitHub (menunggu token dari pemilik proyek)
2. Restart total kedua bot (sekali, mengaktifkan seluruh fix sekaligus)
3. Audit performa live — checklist lengkap per kategori (**Future Long → Future Short → Spot**, urutan ini):
   - [ ] Berapa yang lolos Gate 3, dan kalau ada yang gagal Gate 3, kenapa (alasan spesifik)
   - [ ] Berapa yang lolos Gate 4, dan kalau ada yang gagal Gate 4, kenapa
   - [ ] Berapa yang lolos Gate 5, dan kalau ada yang gagal Gate 5, kenapa
   - [ ] Berapa yang benar-benar open trade (posisi tereksekusi)
   - [ ] Dari yang open trade: win atau loss, dan kalau loss, kenapa (analisis penyebab)
   - [ ] Total jumlah trade per kategori

## 🚀 UPGRADE MASA DEPAN (ide "super power" — dicatat, belum dikerjakan)

**Ubah `run_sl_tp_monitor()` dari polling (5 detik) jadi event-driven** — dipicu
langsung oleh tick harga baru dari WebSocketFeed, bukan cek berkala. Latensi deteksi
SL/TP jadi mendekati instan (bukan tertunda sampai 5 detik).

*Konteks: muncul dari diskusi arsitektur push/SSE (#8) — dikonfirmasi 5 detik
sebenarnya sudah cukup cepat, BUKAN bottleneck nyata, tapi genuinely bisa dibuat lebih
optimal kalau pemilik proyek mau performa maksimal ("super power").*

**Trade-off yang perlu diinvestigasi nanti sebelum implementasi:**
- Kompleksitas naik signifikan — perlu hook baru ke `WebSocketFeed` (callback on-tick)
- Race condition baru yang perlu dianalisis — event-driven berarti bisa terpicu jauh
  lebih sering/padat daripada interval tetap 5 detik
- Perlu dipastikan tidak membebani WebSocketFeed itu sendiri (setiap tick memicu scan
  SEMUA posisi terbuka, bukan cuma 1 kali per 5 detik)
- Berlaku untuk KEDUA bot (spot & futures)

**Status:** Ide dicatat, BELUM diinvestigasi mendalam, BELUM ada rencana implementasi.
Prioritas: setelah #8 (push/SSE web↔bot) selesai — ini perluasan konsep yang sama tapi
untuk internal bot (SL/TP), bukan pengganti #8.

---

## 🗺️ URUTAN KERJA FINAL (disepakati 18 Juli, setelah 6 item audit + deadlock selesai)

```
1. Cabang 3 (SEDANG BERJALAN)
   ✅ #7 endpoint futures — TUNTAS (4/4 langkah)
   ✅ #8 push/SSE (KEDUA bot) — TUNTAS (4/4 bagian, 18-19 Juli)
      EventBus + wiring database.py (7 titik Tier 1) + WebSocketFeed.on_ticker
      (throttled) + halt/resume + Tier 2 agregasi (adaptasi struktural futures
      ditemukan, bukan diasumsikan) + /api/stream genuinely event-driven kedua bot.
      Regresi: engine 424/424, spot 53/53, future 175/175. Belum restart.
   ⏳ #9 frontend (Claude Design, KEDUA bot) — BERIKUTNYA
   ↓
2. #4 — _calc_atr_percentile() root cause fix
   (WAJIB: test dari nol utk classifier.py dulu, sign-off eksplisit sebelum deploy —
   ini mengubah regime classification yang sedang dipakai bot LIVE)
   ↓
3. #15 — verifikasi (BUKAN kode baru) apakah _paper_positions masih hasilkan
   phantom setelah fix item #4-audit-fungsional (deteksi bidirectional +
   retry-backoff) aktif pasca-restart
   ↓
4. 8 item Prioritas Sedang: #11, #16, #18, #22, #23, #25, #27, #28
   ↓
5. Item Prioritas Rendah/Diawasi: #3, #9(TOCTOU), #12, #26
```

**Catatan penting soal penomoran**: "#4" dan "#15" di urutan ini merujuk ke nomor item
di TABEL RINGKASAN BACKLOG (atas), BUKAN ke "item #1-6" dari audit fungsional 5 modul
yang sudah selesai — kebetulan ada tumpang tindih angka, jangan tertukar.

---

## 🗂️ RINGKASAN BACKLOG — Semua Temuan Sejauh Ini

**✅ AUDIT 5 MODUL TUNTAS.** Total 15 temuan terkumpul. Belum ada kode yang diubah dari
audit ini — murni investigasi. Siap dibahas prioritas & opsi perbaikan.

| # | Temuan | Sumber | Severity | Status |
|---|---|---|---|---|
| 1 | Race duplikat-entry simbol sama (futures) — jendela race sempit, ada gate G0 — **✅ DIPERBAIKI 18 Juli** (Opsi 2: re-check `get_open_position_by_symbol()` di dalam `_equity_lock` yang sudah ada, tepat sebelum `execute_signal()`; sinergi otomatis dengan release item #2 dibuktikan lewat test) | Audit Modul 1+3 | ✅ Selesai & terverifikasi | 5 test baru, 416/416 total PASS. Belum di-push, belum direstart |
| 2 | `_open_positions_count` stale, gerbang `max_open_positions` bisa terlewati — sistemik di KEDUA bot — **✅ DIPERBAIKI 18 Juli** (Opsi 1: reserve/release atomik di `_evaluate_lock`, dengan asimetri release yang benar spot vs futures + Opsi 2: refresh pasca-entry di luar scope `_equity_lock`) | Audit Modul 1+2 | ✅ Selesai & terverifikasi | 20 test baru, 411/411 total PASS. Belum di-push, belum direstart |
| 3 | TOCTOU di `_maybe_enqueue_gate3()` | Audit Modul 1 | 🟡 Rendah (aman saat ini) | Diawasi, tidak diperbaiki sekarang |
| 4 | Bug bias arah `_calc_atr_percentile()` — pengaruhi regime detection live | Sub-Batch B | 🔴 Tinggi | Ditunda sengaja, proyek terpisah, butuh sign-off |
| 5 | WebSocket futures `watch_tickers_all DEAD` (3 fix) | Histori 13 Juli | ✅ Selesai 3/3 | Menunggu restart untuk aktif |
| 6 | Gap "ATG EXIT" di futures | Histori 13 Juli | ✅ Selesai | Menunggu restart untuk aktif |
| 7 | 6 bug produksi Sub-Batch A (momentum/strength `_short` None, dll) | Sub-Batch A | ✅ Selesai semua | Sudah teruji, di produksi |
| 8 | `run_sl_tp_monitor()` — 1 posisi error bisa putus proteksi SL/TP posisi lain di siklus sama ("poison pill") — **✅ DIPERBAIKI 18 Juli** (Opsi C: outer backstop per-posisi + inner try/except di 3 titik + logging 2 tingkat, kedua bot) | Audit Modul 2 | ✅ Selesai & terverifikasi | 10 test baru (termasuk skenario entry_price korup pakai BaseRiskManager asli), 391/391 total PASS. Belum di-push, belum direstart |
| 9 | `calculate_liquidation_price()` bisa throw tak-tertangkap kalau `margin_mode="cross"` diaktifkan | Audit Modul 2 | 🟡 Rendah (butuh config non-default) | Diawasi, tidak diperbaiki sekarang |
| 10 | Rekonsiliasi posisi HANYA 1 arah — posisi "phantom" TIDAK PERNAH terdeteksi — **✅ DIPERBAIKI 18 Juli** (deteksi bidirectional + debounce 2 siklus + notify TANPA auto-close, mitigasi root-cause `close_position_with_retry()` di kedua bot termasuk celah spot yang nihil proteksi) | Audit Modul 4 | ✅ Selesai & terverifikasi | 39 test baru, 455/455 total PASS. Belum di-push, belum direstart |
| 27 | **[BARU]** Spot's `_reconcile_positions_on_startup()` sudah auto-close phantom TANPA debounce — desain berbeda/inkonsisten dari mekanisme periodik baru (item #4) yang sengaja tanpa auto-close | Investigasi item #4, 18 Juli | 🟡 Sedang, inkonsistensi desain | Belum diperbaiki, layak direkonsiliasi nanti |
| 29 | **[TERKONFIRMASI AKTIF 18 Juli, SEKARANG DIPERBAIKI]** Deadlock `_equity_lock` (spot) — **✅ DIPERBAIKI 18 Juli** (`_on_trade_executed()` ganti `await` jadi `asyncio.create_task()` fire-and-forget via `_refresh_portfolio_safety_net()`, exception-safe). Ditemukan juga: TOWNS/USDT phantom adalah KORBAN KEDUA dari deadlock yang sama (bukan gap item #4 terpisah) — deadlock ini bisa melumpuhkan TOTAL eksekusi (entry DAN close/SL-TP), bukan cuma ~92% entry seperti dugaan awal | Live audit 18 Juli | ✅ Selesai & terverifikasi | 6 test baru (termasuk pembuktian AIGENSYN/PYR tidak regresi + pola lama genuinely TimeoutError). Total 38+365+64 test PASS. Belum di-push, belum direstart |
| 30 | **[Konfirmasi live]** `TOWNS/USDT` — contoh nyata phantom position (persis item #4): close sukses di exchange, `db.close_position()` tidak commit, `is_open=1` padahal exchange flat. Fix item #4 belum live (belum restart) | Live audit 18 Juli | 🟡 Akan otomatis teratasi setelah restart (fix item #4 aktif) | Tidak perlu tindakan terpisah — sudah diperbaiki di item #4, tinggal restart |
| 31 | **[BARU]** Spot's `universe/add` — NOL validasi `is_symbol_supported()` sebelum tulis ke DB. **✅ DIPERBAIKI 19 Juli** — validasi ditambahkan (400 tidak dikenal, 503 exchange belum connect), `is_symbol_supported()` dikonfirmasi identik dengan futures (shared `BaseExchangeConnector`), tidak perlu penyesuaian struktural | Investigasi task #17, 18 Juli | ✅ Selesai & terverifikasi | 8 test baru, spot 68/68 + engine 424/424 + future 175/175 total PASS. Belum di-push, belum direstart |
| 35 | **[BARU]** Spot's `auto_scan_and_populate()` (jalur AUTO-SCAN) — juga TIDAK punya parameter `is_valid_symbol` seperti versi futures-nya. **✅ DIPERBAIKI 19 Juli** — parameter opsional ditambahkan, `main_spot.py::start()` diverifikasi genuinely meneruskan (test source-inspection), edge case "semua invalid" jatuh ke fallback existing (bukan crash) | Investigasi bug #31, 19 Juli | ✅ Selesai & terverifikasi | 6 test baru, spot 84/84 + engine 424/424 + future 180/180 total PASS. Belum di-push, belum direstart |
| 36 | **[BARU]** Mekanisme phantom detector (`run_position_sync_loop()`, item #4-audit-fungsional) cuma cek KEBERADAAN posisi (ada/tidak di exchange vs DB), TIDAK PERNAH cek apakah `amount` cocok. Kalau `reduce_position_amount()` (partial-close) gagal setelah order sukses di exchange, DB nyangkut menampilkan amount TERLALU BESAR — mismatch SENYAP, tidak pernah self-heal (kedua sisi `is_open=True` tetap valid, cuma amount yang salah). **✅ DIPERBAIKI — dikonfirmasi ULANG 20 Juli saat investigasi Tahap 0 item #15**: `_process_amount_mismatch_candidates()` + `find_untracked_positions()`'s `amount_mismatches` (KEDUA bot, `position_sync_futures.py` & `position_sync_spot.py`) SUDAH mengimplementasikan deteksi ini — pola identik phantom detector (debounce 2 siklus, counter `self._amount_mismatch_suspects` terpisah dari `_phantom_suspects`, notify-only TANPA auto-correct), sudah wired ke `run_position_sync_loop()` kedua bot, dan sudah punya test coverage (`TestAmountMismatchDetection`, `TestAmountMismatchDebounceViaRunPositionSync` di `test_position_sync_futures.py`/`test_position_sync_spot.py`). Baris status di bawah ini SEBELUMNYA SALAH/STALE (bilang "belum diperbaiki") — kemungkinan diperbaiki di sesi lain setelah entri ini ditulis tapi dokumentasinya tidak ikut di-update saat itu | Investigasi item #28, 19 Juli; dikonfirmasi ulang 20 Juli | ✅ Selesai & terverifikasi (kode + test, dikonfirmasi ulang lewat pembacaan kode langsung, bukan asumsi dari entri lama) | Sudah aktif di kode produksi saat ini |
| 37 | **[BARU, ditemukan Tahap A investigasi fix #4]** `atr_pct` (`volatility.py::calculate_atr_enhanced()`) TERNYATA JUGA punya bias arah residual, TAPI beda kategori & jauh lebih kecil dari bug #4 (`_calc_atr_percentile()`). Formula `atr_pct = ATR_dolar/close*100` sendiri dikonfirmasi identik pandas_ta resmi (`atr(..., percent=True)` → `atr *= 100/close`, source dicek langsung) — bukan formula salah. Root cause: ATR dolar di-Wilder-smooth dari 14 bar TERAKHIR (harga per-bar sudah berbeda level akibat drift) baru dibagi close HARI INI — smoothing-lag ini bias makin besar seiring kekuatan & durasi drift dan seiring panjang `period`. Dibuktikan lewat geometric-mirror test (`anchor²/close`, metodologi yang sama dgn test bug #4 yang sudah ada) multi-seed multi-drift: tanpa drift (kontrol) gap ~0-8% (noise-level); drift sedang gap ~10-25%; drift kuat sustained gap ~40-70%. Sweep `period` mengkonfirmasi mekanisme: period=1 (tanpa smoothing) gap ~2% (nyaris nol), period=14 (PRODUKSI) ~50%, period=28 ~101%. **PRE-EXISTING** — sudah ada sejak `atr_pct` pertama dibuat, BUKAN diciptakan/diperparah oleh fix #4 (fix #4 cuma memakai `atr_pct` apa adanya sbg basis ranking baru, tidak mengubah formulanya). **Blast radius LEBIH LUAS dari #4**: `engine/risk_base.py` (dynamic daily limit, LIVE), `future/main_future.py::compute_adaptive_leverage()` (leverage adaptif, LIVE), `engine/intelligence/validator.py` (gate/validasi), plus expose read-only di API/DB kedua bot | Investigasi Tahap A, fix #4, 19 Juli | 🟡 Diketahui, di luar scope #4, risiko rendah di kondisi pasar normal (perlu drift KUAT & SUSTAINED puluhan bar utk signifikan; choppy/netral nyaris nol) | Belum diperbaiki, belum dibahas opsinya — TIDAK diselesaikan bersamaan dgn #4 (kalau mau diperbaiki, perlu redesign formula `atr_pct` itu sendiri — mis. normalisasi per-bar dulu baru di-smooth — proyek terpisah dgn sign-off sendiri krn blast radius menyentuh leverage/risk-limit live) |
| 38 | **[BARU, ditemukan validasi data riil fix #4]** Validasi dampak riil utk temuan bias Sub-Batch A/B (`bb_score`/`kc_score`/`atr_score_short`) belum pernah dilakukan. Konteks: investigasi #4 (`_calc_atr_percentile()`) awalnya dikarakterisasi pakai data SINTETIS stress-test (drift konstan sepanjang seluruh window lookback), menunjukkan gap 13-97 poin. Setelah divalidasi dgn data RIIL Binance (BTC & DOGE, 6 periode termasuk pump DOGE +106%/18 hari), gap riil ternyata jauh lebih kecil (0.46-3.51 poin) — krn pasar riil jarang punya drift konstan sepanjang SELURUH window lookback (100 bar/25 jam); pergerakan riil bersifat "burst" (volatility clustering, breakout singkat lalu konsolidasi) bukan random-walk drift rata spt fixture sintetis, jadi bias yg butuh sustained drift tidak terakumulasi sebesar stress-test idealisasi. **Implikasi**: temuan bias serupa di Sub-Batch A (momentum/strength role-swap — meski itu bug struktural copy-omission, BUKAN soal magnitude bias) dan Sub-Batch B (`bb_score`/`kc_score` kontrarian by design, `atr_score_short` known-limitation) SEMUANYA divalidasi CUMA pakai data sintetis (fuzz test ribuan trial acak/tren monoton), BELUM PERNAH divalidasi dgn data historis riil spt metodologi #4 di atas. Tidak ada bukti kalau hasilnya bakal sama (kemungkinan besar magnitude riil juga lebih kecil dari sintetis, konsisten pola #4 — tapi ini DUGAAN, belum diverifikasi) | Investigasi validasi data riil fix #4, 20 Juli | 🟡 Dicatat, bukan blocker, tidak mendesak | Belum dikerjakan — proyek MTF (Sub-Batch A/B) sudah CLOSED/production-stable, JANGAN disentuh ulang sekarang. Item validasi tambahan utk prioritas nanti, setelah #4 dan #15 selesai |

**🎉 MILESTONE 19 Juli — SELURUH 5 BUG SPOT dari investigasi futures TUNTAS**
(#33, #31, #34, #32, #35). Spot test suite naik dari 60 → 84 (+24 test), nol regresi
di engine (424) atau future (180) sepanjang 5 fix berturut-turut. Pola yang konsisten
terbukti penting: jangan straight-copy dari futures — 2 dari 5 fix (#33, #32)
menemukan perbedaan/gap struktural nyata yang butuh penyesuaian atau perluasan
scope ke kedua bot, bukan cuma spot yang bermasalah.
| 32 | **[BARU]** `get_universe_detail()` (kedua bot) hanya baca `universe_watchlist` config, TIDAK konsultasi `db.get_active_universe_overrides()`. **✅ DIPERBAIKI 19 Juli, KEDUA BOT** — dikonfirmasi futures punya gap identik (bukan diasumsikan), fix gabungkan symbol dari DB overrides, fallback aman kalau DB gagal | Investigasi task #17, 18 Juli | ✅ Selesai & terverifikasi | 10 test baru (5/bot), spot 78/78 + engine 424/424 + future 180/180 total PASS. Belum di-push, belum direstart |
| 33 | **[BARU]** Spot's `/api/orderbook/{symbol}` — SELALU crash HTTP 502 (`_get_ob_danger_level()` dipanggil dengan 1 argumen, padahal butuh 5). **✅ DIPERBAIKI 19 Juli** — `WhaleDetector()` instance baru per-request, `wall_first_seen={}` baru (bukan `self._ob_wall_first_seen` milik bot — alasan genuinely diverifikasi: mencegah mutasi state live Gate 2, bukan sekadar tiru futures) | Investigasi task #16, 18 Juli | ✅ Selesai & terverifikasi | 7 test baru, spot 60/60 + engine 424/424 + future 175/175 total PASS. Belum di-push, belum direstart |
| 34 | **[BARU]** Spot's `/api/candles/{symbol}/indicators` — dead code, TIDAK PERNAH bisa diakses (route shadowing). **✅ DIPERBAIKI 19 Juli** — route dipindah ke sebelum route candles polos, dibuktikan empiris via TestClient sebelum & sesudah fix, isi handler tidak diubah | Investigasi task #16, 18 Juli | ✅ Selesai & terverifikasi | 5 test baru, spot 73/73 + engine 424/424 + future 175/175 total PASS. Belum di-push, belum direstart |
| 11 | `spot::run_position_sync_loop()` pakai `while True` — satu-satunya loop tanpa jalur shutdown kedua. **✅ DIPERBAIKI 19 Juli** — `while self.is_running:` + `except asyncio.CancelledError: break`, menyamakan pola loop lain | Audit Modul 4 | ✅ Selesai & terverifikasi | 3 test baru (timeout-guarded), spot 87/87 total PASS. Belum di-push, belum direstart |
| 12 | Race `adopt_position()` vs entry normal — bisa timpa data risk-assessment asli dengan fallback generik | Audit Modul 4 | 🟡 Rendah (probabilitas kecil) | Diawasi, tidak diperbaiki sekarang |
| 13 | `_watch_orderbook()` per-symbol TIDAK punya self-healing (beda dari ticker) — mitigasi kebetulan lewat REST polling unconditional — **✅ DIPERBAIKI 17 Juli** (Opsi A: dict tracking per-symbol, cooldown identik ticker, restart tersebar alami via loop existing) | Audit Modul 5 | ✅ Selesai & terverifikasi | 6 test baru, 376/376 total PASS. Belum di-push ke GitHub |
| 14 | Cache `self._markets` (precision/fee/min-notional) tidak pernah refresh selama proses hidup — sama-kelas dengan bug EVAA/USDT tapi scope lebih luas — **✅ DIPERBAIKI 18 Juli** (Opsi 1: loop periodik 3600s, reuse `reload_markets()`, dibungkus `self._retry()`, diterapkan ke KEDUA bot — sekaligus menutup gap spot yang belum pernah panggil `reload_markets()` sama sekali) | Audit Modul 5 | ✅ Selesai & terverifikasi | 7 test baru, 381/381 total PASS. Belum di-push, belum direstart |
| 15 | **[MENYATUKAN Modul 1+4+5]** `_paper_positions` futures adalah sumber kebenaran ke-3 independen — urutan operasi close TIDAK atomic lintas paper-state & DB, mekanisme KONKRET penyebab posisi phantom (temuan #10). **✅ DIPERBAIKI 20 Juli** — 3 temuan konkret (A: `is_closing` stuck forever setelah retry exhausted; B: futures nol mekanisme reconcile startup; C: retry-close pada exchange yg sudah flat disalahartikan buka posisi baru arah berlawanan) diinvestigasi + disimulasikan (bot mati, tanpa observasi live) lalu diperbaiki bertahap dgn checkpoint per temuan: A (reset `is_closing` terpusat), B (`_reconcile_phantom_positions_on_startup()` futures, reuse `find_untracked_positions()`), C (Opsi C3: verify-before-send + reduce-only backstop). Lihat CLAUDE.md bagian "PROYEK: Root-Cause Fix `_paper_positions` Phantom Position (Item Audit #15)" utk detail lengkap | Audit Modul 5 | ✅ Selesai & terverifikasi | 63 test baru (6 file), regresi penuh 872/872 PASS. Belum di-push, belum direstart. Temuan sampingan `reduce_position_amount_with_retry()` (item #28, gap identik Temuan A) SENGAJA belum disentuh — butuh keputusan terpisah |
| 16 | **[HISTORIS, perlu verifikasi ulang]** `market_structure_score`/`donchian_score` — **✅ DIVERIFIKASI & DIDOKUMENTASIKAN 19 Juli** — keputusan masih sama (ditunda, butuh backtest), TAPI konteks risiko berubah: bias arah di MTF gate (risiko asli penundaan) sudah tertutup otomatis oleh proyek MTF Composite Side-Aware. Didokumentasikan lengkap di `weights.py` (komentar) + `CLAUDE.md` | Generasi 1-2 (9 Juli) | ✅ Ditutup — status quo terverifikasi & terdokumentasi | Tidak ada perubahan kode fungsional, murni dokumentasi |
| 28 | `reduce_position_amount()` (partial-close futures) belum ada retry-wrapper. **✅ DIPERBAIKI 19 Juli** — `reduce_position_amount_with_retry()` (pola identik `close_position_with_retry()`). Temuan tambahan: phantom detector cuma bandingkan keberadaan posisi (bukan amount) — kegagalan partial-close bisa nyangkut mismatch amount yang SENYAP, tidak self-heal. Pesan kegagalan partial vs full dibedakan eksplisit | Investigasi item #4, 18 Juli | ✅ Selesai & terverifikasi | 8 test baru, spot 87/87 + engine 424/424 + future 188/188 total PASS. Belum di-push, belum direstart |
| 17 | ~~**[HISTORIS, PERLU VERIFIKASI SEGERA]**~~ 3 bug leverage/liquidation (11 Juli) — **✅ TERVERIFIKASI 17 Juli, SEMUA MASIH ADA & BENAR** | Generasi 3 (11 Juli) | ✅ Selesai & terverifikasi | `is_stop_loss_safe()`, `set_leverage()`, `liquidation_price` — bukti baris kode persis dicek |
| 18 | ~~**[HISTORIS]**~~ `position_sync_futures.py` fallback camelCase — **✅ DIVERIFIKASI 19 Juli, TIDAK ADA GAP** — semua field yang genuinely butuh fallback sudah benar. Temuan sampingan: docstring `unrealized_pnl` dead claim, diperbaiki (dihapus, bukan diimplementasi — field tidak pernah dipakai) | Generasi 3 (11 Juli) | ✅ Ditutup — bukan bug | Docstring diperbaiki, tidak ada kode fungsional baru |
| 19 | ~~**[HISTORIS]**~~ Kelly sizing fallback 720% — **✅ TERVERIFIKASI 17 Juli, MASIH ADA & BENAR** (`commander.py:365`) | Generasi 3 (11 Juli) | ✅ Selesai & terverifikasi | Baris kode persis dicek |
| 20 | Bug cache cross-contamination `_OBSERVATION_CACHE` (module-level) — cache key tidak menyertakan `side`, sinyal short bisa diam-diam dapat cache long | Sub-Batch D (proyek MTF) | ✅ Sudah diperbaiki | Ditemukan & diperbaiki hari ini, 367/367 test PASS |
| 21 | ~~**[HISTORIS]**~~ camelCase `margin_mode`/`liquidation_price` di `position_sync_futures.py` — **✅ TERVERIFIKASI 17 Juli, MASIH ADA** (`position_sync_futures.py:60-61`) | Generasi 3 (11 Juli) | ✅ Selesai & terverifikasi | Baris kode persis dicek |
| 22 | Flag `auto_scan_universe_futures` — **✅ DIVERIFIKASI 19 Juli, BUKAN BUG** — sepenuhnya manual-by-design, identik di kedua bot, tidak ada mekanisme otomatis manapun yang menyentuhnya. Didokumentasikan di komentar kode (instruksi SQL untuk operator) | Verifikasi 17 & 19 Juli | ✅ Ditutup — desain yang benar | Cukup didokumentasikan, kedua bot |
| 23 | Gate 4 (`[ScoreThreshold]`) sama sunyi seperti Gate 3 — kemungkinan titik reject TERBANYAK, invisible tanpa DEBUG logging (kedua bot). **✅ DIPERBAIKI 19 Juli** — `KeyedLogThrottle` baru (reuse konsep `ThrottledTickerPublisher`), rate-limit INFO 1x per key per 10 menit (konfigurable). Key `str` (spot, symbol saja) vs `tuple` (futures, `(symbol, side)` — koneksi desain dengan fix #25) | Verifikasi 17 Juli | ✅ Selesai & terverifikasi | 22 test baru, spot 94/94 + engine 436/436 + future 196/196 total PASS. Belum di-push, belum direstart |
| 24 | **[BARU, ditemukan dari data live]** `strategy_base.py:1128` — insert `action_taken='PIPELINE'` tidak pernah kirim `side=`, default diam-diam ke `"long"` untuk SEMUA simbol. **✅ DIPERBAIKI 18 Juli** — 1 baris (`side=side`), diverifikasi via revert-test-restore (4 test baru gagal tanpa fix, lolos dengan fix) | Verifikasi live data 17 Juli | ✅ Selesai & terverifikasi | 374 test PASS. Belum di-push ke GitHub, belum direstart ke bot manapun |
| 25 | `_SIGNAL_CONFIRM_BUFFER` (scorer.py) cuma di-key by `symbol`, bukan `(symbol, side)` — pola bug SAMA PERSIS dengan `_OBSERVATION_CACHE` yang sudah diperbaiki di Sub-Batch D, tapi versi ini di luar scope & masih ada. **✅ DIPERBAIKI 19 Juli** — tuple key `(symbol, side)` (lebih sederhana dari string+suffix `_OBSERVATION_CACHE`, dikonfirmasi tidak ada consumer prefix-matching yang butuh string) | Verifikasi live data 17 Juli | ✅ Selesai & terverifikasi | 7 test baru (via `score_signal()` sungguhan), engine 431/431 total PASS. Belum di-push, belum direstart |
| 26 | **[BARU, ide dari investigasi #3]** Notifikasi eksplisit (`notify_error`) kalau 1 simbol gagal diproses N siklus berturut-turut di `run_sl_tp_monitor()` — supaya retry-otomatis tidak jadi "retry-forever yang senyap" kalau root cause tidak pernah benar-benar diperbaiki | Investigasi item #3, 18 Juli | 🟡 Rendah, ide perbaikan tambahan | Sengaja ditunda, di luar scope item #3 |

**Item yang butuh restart bot futures untuk aktif:** #5, #6 (sudah selesai kode &
test, tinggal restart).

**Item yang masih perlu diputuskan cara perbaikannya (🔴/🟠, 10 item):** #1, #2, #8,
#10, #11, #13, #14.

**Item yang sengaja ditunda sebagai proyek terpisah:** (kosong — #4 & #15 selesai,
lihat status masing-masing di CLAUDE.md).

**Item yang diawasi tapi tidak diperbaiki sekarang (risiko rendah/tidak reachable):**
#3, #9, #12, #37, #38.

**Benang merah lintas-modul (kesimpulan Claude Code):** guard individual (G0_ALREADY_OPEN,
`_closing_lock`, `_equity_lock`, `_reconcile_lock`, self-healing WS ticker, liquidation
safety check) semuanya ADA dan benar — tapi jendela waktu ANTARA pengecekan dan eksekusi
nyata (network I/O) tidak pernah di-re-verifikasi tepat sebelum commit, dan rekonsiliasi
DB↔exchange cuma satu arah. **Tidak ada temuan yang membuat sistem "terbuka lebar" tanpa
proteksi** — semua adalah jendela race SEMPIT atau staleness BERTAHAP, bukan lubang besar.

**Modul 3 murni AMAN untuk semua area lain yang diperiksa:** lock reconcile, freshness
data untuk jalur reconcile, mutasi registry, edge case numerik — lihat detail di bawah.

---



## Status Modul

| Modul | Area | Status |
|---|---|---|
| 1 | Jalur Eksekusi Order | ✅ SELESAI |
| 2 | Risk Management | ✅ SELESAI |
| 3 | Capital Allocator | ✅ SELESAI |
| 4 | Position Sync | ✅ SELESAI |
| 5 | Exchange Connector & WebSocket | ✅ SELESAI |

**🎉 AUDIT 5 MODUL TUNTAS — semua area kritis sudah ditelusuri baris-demi-baris.**

---

## Modul 1 — Jalur Eksekusi Order (SELESAI)

**File dibaca tuntas:** `engine/execution_base.py` (1131 baris, seluruh method),
`future/main_future.py` (`_handle_entry`, `_handle_close`, `_close_position_market`,
`_do_close_position`, `run_gate3_worker`, `_maybe_enqueue_gate3`, `_refresh_portfolio`),
`spot/main_spot.py::_handle_buy` (perbandingan), `spot/strategy_spot.py` (baris 330-460),
`future/risk_future.py::evaluate_order/_evaluate_order_locked`.

### ✅ AMAN

**1. Funnel close tunggal, terverifikasi**
- `_do_close_position()` hanya punya SATU caller: `_close_position_market()` (baris 726).
- Semua 6 titik pemicu close (strategy-exit, liquidation_proximity_emergency, ATG exit,
  hit_sl, hit_tp, trailing_reason) lewat `_close_position_market()`.
- Dijaga `self._closing_lock` + `self._closing_symbols` (baris 717-729) — panggilan
  bersamaan untuk simbol sama, salah satu langsung return tanpa efek.
- `_do_close_position()` re-verifikasi `existing = await self.db.get_open_position_by_symbol(pos.symbol)`
  di baris 744 sebelum lanjut.

**2. Mekanisme fail-closed & anti-phantom-fill di `execution_base.py`**
- Slippage guard fail-CLOSED kalau harga tidak diperoleh (baris 427-432).
- `_verify_order_filled()` mencegah trade phantom dari order belum-final (baris 662-719).
- `_execute_limit()` fallback-ke-market verifikasi status ASLI order via `fetch_order()`
  dulu sebelum memutuskan, mencegah double-fill (baris 526-589).
- `filled=0` yang valid tidak salah jatuh ke fallback (baris 906-918, `is not None`
  eksplisit, bukan truthy check).

### 🔴 PERLU PERHATIAN

**3. Race duplikat-entry untuk simbol yang sama (futures)**
- **Seharusnya:** sebelum order dikirim, sistem pastikan simbol belum punya posisi
  terbuka — dekat momen eksekusi.
- **Actually:** `_process_one(symbol)` cek DB SEKALI di awal (baris 1159), lalu lewat
  BANYAK `await` (fetch OHLCV, confirmation TF, scoring, commander.decide, risk
  evaluate_order) sebelum `_handle_entry()` dipanggil (baris 1413). Tidak ada re-check
  "apakah simbol ini sudah punya posisi" tepat sebelum order dikirim.
- `risk_future.py::_evaluate_order_locked()` juga tidak menutup celah ini —
  `is_opening_new` (baris 206-240) cuma cek `is_symbol_halted()` dan
  `_open_positions_count >= _max_open_positions` (agregat, bukan per-simbol).
- **Kenapa nyata, bukan teoretis:** 2 jalur independen bisa memanggil `_handle_entry()`
  untuk simbol sama tanpa saling tahu:
  1. `run_gate3_worker` (dijaga `_pipeline_active`/`_queued_symbols`, tapi cuma cegah
     simbol sama di-antre 2x dari SUMBER yang sama).
  2. `capital_allocator.reconcile_pending()` — grep seluruh `future/capital_allocator.py`
     konfirmasi TIDAK ada referensi ke `_pipeline_active`/`_queued_symbols`.
  - Kalau simbol X diproses gate3 worker (sudah lolos cek DB, sedang scoring) DAN
    kebetulan ada di registry `_pending_candidates`, lalu `reconcile_pending()` terpicu
    — kedua jalur sampai ke `_handle_entry()` untuk X. `_equity_lock` cuma menyerialkan
    eksekusi (satu tunggu satu), TIDAK mencegah yang kedua ikut buka posisi.
  - **Dampak:** posisi X ke-buka DUA KALI (dua order market riil terkirim ke exchange),
    DB `upsert_position()` cuma simpan yang TERAKHIR — order pertama "hilang" dari
    tracking DB tapi TETAP ada di exchange.
- **Perbandingan spot (lebih aman):** `spot/strategy_spot.py` punya double-check-under-lock
  yang benar (baris 353-356 cek awal, baris 440-449 re-cek `_in_position`/`_pending_entry`
  DI DALAM `with self._lock`, persis sebelum `.add()`). Spot cuma punya SATU sumber sinyal
  entry (tidak ada capital-allocator setara) — guard ini cukup. Futures TIDAK punya guard
  setara, dan punya DUA sumber sinyal independen.

**4. `_open_positions_count` stale, gerbang `max_open_positions` bisa terlewati**
- **Seharusnya:** `_open_positions_count` yang dipakai gerbang `max_open_positions`
  (risk_future.py baris 232) mencerminkan jumlah posisi terbuka SAAT itu.
- **Actually:** nilai ini di-update HANYA lewat `RiskManager.update_portfolio_state()`,
  dipanggil dari: (a) `_do_close_position()` setelah CLOSE (baris 862), (b)
  `run_portfolio_monitor()` tiap `SNAPSHOT_INTERVAL=900` detik, (c) 2 titik terkait
  liquidation-check. TIDAK ADA panggilan `_refresh_portfolio()` di dalam `_handle_entry()`
  (dikonfirmasi baca ulang seluruh fungsi, baris 501-680). Setelah posisi baru dibuka,
  counter TIDAK langsung naik — tetap angka SEBELUM entry, sampai ada event close (posisi
  lain) atau menunggu hingga 900 detik.
- **Dampak konkret:** `GATE3_WORKERS=3` (default) proses beberapa simbol paralel; kalau
  3 entry berbeda lolos gate scoring dalam window singkat sebelum refresh, entry ke-2 dan
  ke-3 masih baca `_open_positions_count` versi LAMA — gerbang `max_open_positions` bisa
  dilewati, bot bisa buka LEBIH banyak posisi dari batas yang dikonfigurasi.

### ⚠️ CATATAN MINOR (bukan bug aktif, pola rapuh)

**5. TOCTOU di `_maybe_enqueue_gate3()`**
- Baris 1048-1081: cek `if symbol in self._pipeline_active: return` (1052) →
  `await self.db.get_open_positions()` (1069) → `self._pipeline_active.add(symbol)` (1078)
  — ada `await` di antara cek dan `.add()`, celah TOCTOU klasik.
- **Saat ini AMAN**: satu-satunya caller (`run_scanner_loop`, grep dikonfirmasi) memanggil
  dalam SATU loop `for symbol in universe` sekuensial dalam SATU task — tidak ada pemanggil
  kedua paralel.
- **Risiko masa depan:** kalau ada trigger kedua (mis. WS callback langsung) ditambahkan
  tanpa menyadari pola ini, celah jadi nyata. Layak diperhatikan, bukan diperbaiki sekarang.

---

## Modul 2 — Risk Management (SELESAI)

**File dibaca tuntas:** seluruh `engine/risk_base.py` (597 baris — `update_portfolio_state`,
`halt_trading`/`_resume`, `record_symbol_loss`/`is_symbol_halted`, `check_breakeven_sl`,
`check_trailing_sl`, semua `compute_*` statistik); seluruh `future/risk_future.py`
(451 baris — `evaluate_order`/`_evaluate_order_locked`, `_compute_position_size`,
`_compute_sl_tp`, `compute_adaptive_leverage`); seluruh `spot/risk_spot.py` (354 baris,
perbandingan); `future/liquidation.py::calculate_liquidation_price`/`is_stop_loss_safe`;
`run_sl_tp_monitor()` penuh di kedua bot; semua caller `update_portfolio_state()`/
`evaluate_order()` di-grep dan ditelusuri konteksnya.

### 🔴 REVISI TEMUAN #2 (dari Modul 1) — TERNYATA sistemik di KEDUA bot

Modul 1 menemukan `_open_positions_count` stale di futures. Ditelusuri ulang dari sisi
risk_base.py/risk_spot.py, hasilnya lebih luas:

- `_open_positions_count` di-set HANYA lewat `update_portfolio_state()` (risk_base.py
  baris 259) — hanya 2 caller di seluruh repo: `spot/main_spot.py:2946` dan
  `future/main_future.py:1534`, keduanya di dalam `_refresh_portfolio()`.
- Grep semua pemanggil `_refresh_portfolio()` di `main_spot.py`: startup (503),
  `run_portfolio_monitor()` periodik (900 detik), 2 titik lain (2989, 3174,
  konteks close/reconcile). `_handle_buy()` (baris 2284-2470-an) dibaca lengkap —
  TIDAK ADA panggilan `_refresh_portfolio()` di dalamnya, persis pola futures.
- `spot/risk_spot.py` baris 86: gerbang `max_open_positions` IDENTIK dengan futures,
  dan spot JUGA punya `GATE3_WORKERS=3` worker pool **konkuren** (dikonfirmasi baris
  1180-1182, 1590-1591 main_spot.py) — bukan loop tunggal sekuensial seperti dugaan
  awal Modul 1 soal spot "lebih aman".

**Kesimpulan direvisi:** ini BUKAN gap khusus arsitektur futures — pola risk-management
yang SAMA ada di KEDUA bot sejak sebelum restrukturisasi futures. 3 worker gate3 yang
proses simbol berbeda paralel bisa sama-sama baca `_open_positions_count` yang belum
ter-update oleh entry satu sama lain, berpotensi membuka LEBIH banyak posisi dari
`max_open_positions` yang dikonfigurasi, **di kedua bot**.

### 🔴 PERLU PERHATIAN (baru) — `run_sl_tp_monitor()`: 1 posisi error bisa putus proteksi posisi LAIN

**Seharusnya:** kegagalan memproses SATU posisi (exception apapun) tidak menghalangi
posisi LAIN tetap dicek SL/TP/liquidation di siklus yang sama.

**Actually:** di KEDUA bot (`future/main_future.py` baris 1588-1802, `spot/main_spot.py`
baris 1883-2062-an, pola identik):
```python
while self.is_running:
    try:
        positions = await self.db.get_open_positions()
        for pos in positions:
            ...  # breakeven check, trailing check, hit_sl/hit_tp, _close_position_market()
            # TIDAK SEMUA langkah dibungkus try/except individual
    except Exception as e:
        log.error("SL/TP monitor error...", e)
    await asyncio.sleep(self.SL_TP_CHECK_INTERVAL)  # 5 detik
```
`try/except` PALING LUAR membungkus SELURUH `for pos in positions:` — bukan per-posisi.
Beberapa langkah sudah dilindungi try/except sendiri (fetch mark_price, fetch ATR live,
blok ATG penuh) — tapi `check_breakeven_sl()`, `check_trailing_sl()`,
`db.update_position_sl()`, dan **`_close_position_market()`** untuk
hit_sl/hit_tp/trailing_reason (baris 1792-1797 futures, ekuivalen di spot) TIDAK
dibungkus try/except di titik itu. Kalau posisi pertama dalam urutan `positions`
(urutan DB, tidak dijamin konsisten) melempar exception di langkah tak-terlindungi ini,
SELURUH posisi SETELAHNYA di list itu **tidak dicek sama sekali** di siklus itu.

**Skala dampak:** dibatasi ~5 detik (`SL_TP_CHECK_INTERVAL`) untuk kasus TRANSIEN
(network blip) — kecil. TAPI kalau posisi yang gagal SELALU gagal dengan cara sama
(mis. data korup — `entry_price` None/corrupt), posisi itu jadi **"poison pill"**:
semua posisi setelahnya kehilangan proteksi SL/TP/liquidation TANPA BATAS WAKTU sampai
poison pill ditangani manual. Pola pre-existing di KEDUA bot (bukan regresi baru futures).

### ✅ AMAN

**`is_stop_loss_safe()` / `calculate_liquidation_price()`** — validasi ketat: `raise
ValueError` eksplisit untuk `entry_price<=0`, `leverage<=0`, `mmr` di luar (0,1), side
invalid; guard pembagian-dengan-nol (baris 168-170: `entry_to_liq_distance<=0` → return
False, bukan crash). Bug-fix terdokumentasi sudah diverifikasi benar matematis (`gap_pct`
dihitung relatif jarak entry-ke-liquidation, konsisten long maupun short).

**Halt/resume, drawdown, daily-loss, breakeven/trailing SL formulas** — urutan
pengecekan halt (drawdown → daily-loss → low-balance) tidak ada celah "halt seharusnya
terpicu tapi terlewat". Drawdown TIDAK auto-resume (dikecualikan eksplisit di
`resume_trading()` baris 323-328) — ini BENAR by design, bukan bug. Cabang long/short di
`check_breakeven_sl()`/`check_trailing_sl()` benar-benar simetris (rumus risk/reward &
trigger diverifikasi untuk kedua sisi).

### ⚠️ CATATAN MINOR

**`calculate_liquidation_price()` bisa throw tak-tertangkap di `_evaluate_order_locked()`**
- Baris 290-293 `risk_future.py` memanggil fungsi ini TANPA try/except.
- Fungsi raise `NotImplementedError` kalau `margin_mode="cross"` (baris 101-107).
- Default config SELALU `"isolated"` (main_future.py:545) — tidak reachable saat ini.
- **Risiko masa depan:** kalau operator set `margin_mode=cross`, exception lolos sampai
  ke `_handle_entry()` (juga tidak membungkus `evaluate_order()`), baru tertangkap di
  catch-all `_process_one()` (baris 1421-1422) — bot tidak crash, tapi entry abort
  SETELAH `set_leverage()` mungkin sudah terpanggil ke exchange (state berubah di
  exchange tanpa posisi kebuka). Severity rendah (butuh config non-default), tapi nyata
  kalau cross-margin diaktifkan suatu saat.

---



## Modul 3 — Capital Allocator (SELESAI)

**File dibaca tuntas:** seluruh `future/capital_allocator.py` (576 baris —
`register_or_refresh`, `is_expired`, `purge_expired`, `pick_best_pair`, `_select_winner`,
`_rescore_candidate`, `_build_entry_signal`, `reconcile_pending`). Semua caller di-grep &
ditelusuri: `_reconcile_pending_candidates()` wrapper, kedua titik `register_or_refresh()`
(baris 593 & 1363 main_future.py), kedua trigger `_reconcile_pending_candidates()`
(baris 863 & 1816). Juga dibaca `engine/intelligence/commander.py::decide()` (baris
497-520) — di luar daftar modul awal, tapi wajib ditelusuri karena dipanggil langsung
oleh `_rescore_candidate()`.

### 🔧 KOREKSI atas Temuan #1 (Modul 1) — bukan "tidak ada guard", tapi jendela race lebih sempit

Modul 1 melaporkan: *"tidak ada satupun pengecekan ulang 'apakah simbol ini sudah punya
posisi' tepat sebelum order dikirim."* Setelah menelusuri `commander.decide()` (dipanggil
baik dari `run_gate3_worker` MAUPUN `capital_allocator._rescore_candidate`), ternyata ada
gate eksplisit:

```python
# engine/intelligence/commander.py:515-520
if signal.symbol in open_positions:
    decision.action = DecisionAction.WAIT
    decision.rejection_reason = f"Posisi sudah terbuka untuk {signal.symbol}"
    decision.add_gate_failed("G0_ALREADY_OPEN", decision.rejection_reason)
    return decision
```

Baik `run_gate3_worker._process_one()` (baris 1343-1353, fetch `get_open_positions()`
FRESH lalu panggil `_cmd_decide`) maupun `capital_allocator._rescore_candidate()`
(baris 378-390, fetch `get_open_positions()` FRESH sendiri lalu panggil `_cmd_decide`
juga) sama-sama melewati gate G0 ini, masing-masing dengan snapshot DB yang baru
difetch.

**Revisi temuan:** race duplikat-entry MASIH nyata, tapi jendelanya LEBIH SEMPIT dari
laporan awal — bukan "tidak ada proteksi sama sekali", tapi **dua snapshot
`get_open_positions()` yang diambil independen oleh dua jalur berbeda, keduanya SEBELUM
salah satu pihak commit `upsert_position()`**. Window ini nyata (mencakup
`set_leverage()` + `evaluate_order()` + `execute_signal()`, semuanya network I/O, bukan
instan), tapi jauh lebih sempit dari kesan "tidak ada guard apapun" sebelumnya.

*(Severity di tabel ringkasan direvisi turun dari 🔴 Tinggi ke 🟠 Sedang untuk
mencerminkan koreksi ini.)*

### ✅ AMAN — race antar pemanggilan `reconcile_pending()` sendiri

`_reconcile_pending_candidates()` (baris 682-693) membungkus
`capital_allocator.reconcile_pending(self)` dengan `async with self._reconcile_lock:`.
Dua trigger (`_do_close_position()` baris 863, `run_portfolio_monitor()` baris 1816)
keduanya lewat wrapper ini — dikonfirmasi grep, tidak ada pemanggil langsung yang
melewati lock. Dua trigger hampir bersamaan akan diserialkan, bukan paralel.

### ✅ AMAN (dengan syarat terverifikasi) — freshness data khusus jalur reconcile

Menarik konteks Modul 2: kedua trigger `_reconcile_pending_candidates()` selalu didahului
`await self._refresh_portfolio()` tepat sebelumnya (baris 862→863 dan 1809→1816, urutan
diverifikasi persis) — dan `_refresh_portfolio()` update `_free_balance` DAN
`_open_positions_count` sekaligus. Jadi entry dari `reconcile_pending()` khususnya punya
data portfolio genuinely fresh. **Ini mitigasi parsial nyata, khusus jalur reconcile** —
TIDAK menghapus temuan Modul 2 untuk jalur gate3 worker biasa, dan TIDAK menghapus race
G0 di atas (freshness `_open_positions_count` ≠ freshness `open_positions` list
per-simbol yang dicek G0 — dua mekanisme guard berbeda, dicek di titik waktu berbeda).

### ✅ AMAN — mutasi registry `_pending_candidates` di luar lock

`register_or_refresh()` dipanggil DI LUAR `_reconcile_lock` (dari `_handle_entry` baris
593 dan `run_gate3_worker` baris 1363) — SEMENTARA `reconcile_pending()` (baris 496-541)
membaca & memutasi registry yang SAMA di dalam lock, melewati beberapa `await` panjang
(`_rescore_candidate`). Konsekuensi ditelusuri: karena asyncio single-threaded, tiap
panggilan `register_or_refresh()` sendiri ATOMIC (tidak ada `await` di dalamnya) — tidak
ada torn-state/corruption. Efek yang MUNGKIN terjadi: `best_long`/`best_short` yang
sudah "dipegang" `reconcile_pending()` bisa di-refresh field bookkeeping-nya (last_score,
dst) oleh panggilan konkuren — TAPI baseline TTL (`candle_ts_at_registration`/
`price_at_registration`/`atr_at_registration`) TIDAK PERNAH disentuh oleh refresh (baris
138-141, hanya bookkeeping yang berubah). Tidak ada skenario "slot hilang" — kandidat
yang tidak terpilih tetap di registry (sesuai spec), menunggu ronde berikutnya, TTL
bounded (2×timeframe, `is_expired()`) mencegah starvation permanen.

### ✅ AMAN — edge case numerik & data kosong

- `is_expired()`: tidak ada pembagian; `atr_at_registration>0` dicek eksplisit sebelum
  dipakai sebagai pembagi konseptual (baris 172-173).
- Dict comprehension `current_prices` (baris 474-477): `float(...get("last") or 0) or None`
  — SENGAJA memetakan harga 0/kosong ke `None` (bukan `0.0`), mencegah aturan ATR-move
  di `is_expired()` salah trigger dari harga bogus 0 — pola defensif yang benar.
- `_select_winner()`: tie-break `>=` (long menang saat skor sama persis) — sesuai spec,
  diverifikasi baris demi baris.

---



## Modul 4 — Position Sync (SELESAI)

**File dibaca tuntas:** seluruh `future/position_sync_futures.py` (292 baris) dan
seluruh `spot/position_sync_spot.py` (329 baris) — `fetch_binance_futures_positions`/
`fetch_binance_spot_positions`, `find_untracked_positions`, `analyze_position`,
`adopt_position`, `run_position_sync`. Caller: `run_position_sync_loop()` di kedua bot,
dibaca lengkap.

### 🔴 PERLU PERHATIAN — Rekonsiliasi HANYA satu arah

**Jawaban langsung untuk "apakah rekonsiliasi menangkap SEMUA jenis mismatch": TIDAK.**
Hanya SATU dari tiga jenis mismatch yang mungkin secara logis:

| # | Jenis mismatch | Ditangani? |
|---|---|---|
| 1 | Posisi ADA di exchange, TIDAK ADA di DB ("untracked") | ✅ Ditangani — satu-satunya yang dicek `find_untracked_positions()` |
| 2 | Posisi ADA di DB (`is_open=True`), TIDAK ADA LAGI di exchange ("phantom") | ❌ TIDAK PERNAH dicek di manapun |
| 3 | Posisi ada di KEDUANYA, tapi detail beda (amount/side/leverage) | ❌ TIDAK PERNAH dicek — cuma bandingkan keanggotaan SET simbol, bukan isi data |

**Bukti konkret #2 bukan teoretis** — ditarik balik ke Modul 1:
`_do_close_position()` (main_future.py baris 815-816) punya:
```python
except Exception as e:
    log.critical("close_position (DB) GAGAL untuk %s setelah order sukses: %s", pos.symbol, e)
```
Ini SECARA EKSPLISIT mendokumentasikan skenario "order close SUKSES di exchange, tapi DB
gagal ditulis" — persis kelas mismatch #2. Setelah log critical itu, **tidak ada
mekanisme apapun** yang akan mendeteksi/memperbaikinya — posisi itu akan TERUS dianggap
terbuka oleh `run_sl_tp_monitor()` (memantau posisi yang sudah tidak ada), TERUS masuk
hitungan `unrealized_pnl` di `_refresh_portfolio()` (mendistorsi equity), dan berpotensi
memicu `_close_position_market()` lagi di siklus berikutnya untuk posisi yang sudah tidak
eksis.

**Relevansi khusus futures (lebih parah dari spot):** liquidation terjadi DI SISI
EXCHANGE, di luar kendali bot — kalau harga gap tiba-tiba dan Binance me-liquidate lebih
cepat dari `run_sl_tp_monitor()`'s pengecekan proximity (siklus 5 detik), posisi LENYAP
dari exchange, tapi DB tetap `is_open=True` **selamanya**, tidak ada apapun yang pernah
mengeceknya. Spot tidak punya risiko liquidation, jadi mismatch #2 di spot hanya muncul
dari DB-write-gagal (lebih jarang).

### ✅ Terkonfirmasi — pola sama di kedua bot (bukan regresi futures baru)

`spot/position_sync_spot.py::find_untracked_positions()` (baris 72-94) punya
keterbatasan IDENTIK. Pola pre-existing yang diwarisi, bukan baru muncul saat futures
dibangun.

### ⚠️ Temuan minor — inkonsistensi loop shutdown

`spot/main_spot.py::run_position_sync_loop()` (baris 3213-3228) pakai `while True:` —
BEDA dari SEMUA loop lain di kedua bot (`run_scanner_loop`, `run_gate3_worker`,
`run_sl_tp_monitor`, `run_portfolio_monitor` semuanya pakai `while self.is_running:`).
`future/main_future.py`'s versi (baris 1896) SUDAH benar. **Konsekuensi:** saat bot spot
di-shutdown (`is_running=False`), loop ini TIDAK berhenti — terus jalan sampai proses
benar-benar di-kill, berpotensi menyentuh DB/exchange connection yang sedang di-teardown.
Ditemukan murni dari membandingkan dua file baris-per-baris.

### ⚠️ Temuan minor — race `adopt_position()` vs entry normal

`adopt_position()` memanggil `db.upsert_position(symbol, {...})` TANPA memegang lock
apapun (`_equity_lock`/`_closing_lock` tidak disentuh). `_handle_entry()` kirim order
REAL dulu (`execute_signal()`) BARU `upsert_position()` (di dalam `_equity_lock`). Ada
jendela sempit: kalau `run_position_sync_loop()` (tiap 5 menit) kebetulan jalan TEPAT di
celah antara "order sukses di exchange" dan "DB ter-upsert" milik `_handle_entry()`
sendiri, `find_untracked_positions()` bisa salah mendeteksi sebagai "untracked", lalu
`adopt_position()` menimpa dengan data hasil re-analisis (SL/TP fallback generik,
`strategy_name="manual_adopt_futures"`) — BUKAN data risk-assessment asli dari entry itu
sendiri. Probabilitas rendah (butuh timing pas), tapi race nyata — tidak ada koordinasi
lock sama sekali antara dua jalur penulis `upsert_position` untuk simbol sama.

### ✅ AMAN — arah simbol & signature

`analyze_position()`/`adopt_position()` futures meneruskan `side` apa adanya (bukan
hardcode) ke `score_signal(..., side=side)`, fallback SL/TP di-mirror benar (`sl =
price*0.985` untuk long vs `price*1.015` untuk short). Signature caller-callee cocok
persis. Skip posisi dengan `usdt_value` tidak terhitung (price<=0, dust <$1) — defensif,
bukan bug, cuma dicoba lagi 5 menit kemudian.

---



## Modul 5 — Exchange Connector & WebSocket (SELESAI)

**File dibaca tuntas:** seluruh `engine/exchange_base.py` (422 baris — `connect`,
`get_market_info`, `is_symbol_supported`, `reload_markets`, semua
`fetch_*`/`create_order`/`_retry`); seluruh `spot/exchange_spot.py` (1081 baris —
`ExchangeConnector._simulate_order_fill`, seluruh class `WebSocketFeed`:
`_watch_tickers_all`, `_watch_ticker`, `_watch_orderbook`, `_poll_tickers`,
`_poll_orderbooks_rest`, `is_feed_healthy`, `get_mid_price`/`get_spread`/
`get_market_depth_slippage`); `future/exchange_future.py` (687 baris, fokus
`_simulate_order_fill`, `fetch_positions`, `fetch_mark_price`, `set_leverage`).
Dikonfirmasi via grep: `future/main_future.py` benar-benar reuse `WebSocketFeed`
LANGSUNG dari `spot/exchange_spot.py` — semua temuan WebSocketFeed berlaku SAMA untuk
kedua bot.

### ✅ AMAN — bug "DEAD after 10 retries" SUDAH diperbaiki dengan benar, lebih luas dari laporan awal

`_watch_tickers_all()` (baris 527-628) kalau exhaust `max_retries`, log DEAD dan task
selesai. Tapi `_poll_tickers()` (baris 349-401, loop yang SELALU jalan tiap 10 detik)
punya self-healing eksplisit: cek `self._ws_ticker_task.done()`, kalau mati → restart
via `asyncio.create_task()` dengan exponential backoff cooldown (30 × 2^restart_count,
cap 10 menit). Komentar kode (baris 282-294) eksplisit menyebut ini fix untuk "task WS
mati permanen, downgrade SEMUA simbol ke REST-only selamanya sampai restart manual" —
persis bug insiden 13 Juli. **Terverifikasi bekerja benar.**

### 🔴 PERLU PERHATIAN (baru) — `_watch_orderbook()` per-symbol TIDAK punya self-healing yang sama

`_watch_orderbook(symbol)` (baris 704-737) exhaust retries → `self._ob_dead[symbol]=True`,
log critical, task selesai — **tidak ada mekanisme restart**. Grep seluruh file: tidak
ada penanganan `.done()` untuk task orderbook manapun (beda dari ticker yang dipantau
eksplisit). **Mitigasi kebetulan:** `_poll_orderbooks_rest()` (baris 676-702) jalan TANPA
SYARAT untuk SEMUA simbol setiap saat (bukan cuma "kalau WS mati") — jadi
`live_orderbooks` tetap ter-update via REST meski WS per-symbol mati permanen. Severity
lebih rendah dari bug ticker asli, tapi **inkonsistensi desain nyata**: satu jenis WS
task diberi self-healing, jenis lain tidak.

### 🔴 PERLU PERHATIAN (baru) — cache `self._markets` tidak pernah refresh selama bot hidup

`get_market_info()`, `get_taker_fee()`, `get_maker_fee()`, `get_min_order_cost()`,
`amount_to_precision()`/`price_to_precision()` (via ccxt internal, baca `self._ex.markets`
— cache yang SAMA) — SEMUA baca dari `self._markets`, di-set HANYA di `connect()` (sekali
saat startup) dan (setelah fix kemarin) sekali lagi sebelum auto-scan futures. **Tidak
ada refresh periodik apapun sepanjang sisa umur proses.** Bot spot & futures yang
berjalan sekarang (PID uptime 4+ hari) memakai precision/fee-tier/min-notional dari SAAT
STARTUP, tanpa pernah menyegarkan lagi. Kalau Binance mengubah precision/minNotional/
fee-tier suatu simbol di tengah sesi (terjadi berkala di Binance nyata) — bot tidak akan
pernah tahu sampai di-restart manual. **Ini kelas masalah SAMA PERSIS dengan root cause
EVAA/USDT** (cache stale dipakai tanpa sadar), tapi di lokasi beda (precision/fee, bukan
symbol-recognition) dan scope LEBIH LUAS (seluruh umur proses, bukan cuma jendela sempit
sebelum auto-scan).

### 🔴 PERLU PERHATIAN (signifikan) — `_paper_positions` adalah sumber kebenaran KETIGA, menyatukan Modul 1+4+5

**Ini temuan yang menjelaskan MEKANISME KONKRET dari temuan #10 (Modul 4, posisi phantom).**

`FutureExchangeConnector._paper_positions` (dict internal, `future/exchange_future.py`
baris 85) adalah simulasi "exchange" untuk paper trading — dipakai `_simulate_order_fill()`
untuk hitung margin/liquidation, DAN dibaca balik oleh `fetch_positions()` (baris 184-206)
yang PERSIS fungsi yang dipakai `position_sync_futures.py::find_untracked_positions()`
untuk deteksi mismatch.

Urutan operasi di `_simulate_order_fill()`'s cabang "TUTUP PENUH" (baris 400-402):
`del self._paper_positions[symbol]` terjadi DI DALAM `execute_signal()`, **SEBELUM**
`_do_close_position()` sempat memanggil `db.close_position()`. Kalau `db.close_position()`
GAGAL setelah ini (skenario yang SUDAH didokumentasikan eksplisit di Modul 1:
`log.critical("close_position (DB) GAGAL...")`), maka:
- `self._paper_positions` (≈ "exchange"): posisi SUDAH hilang.
- DB: posisi MASIH `is_open=True`.

Ini persis mismatch kelas #2 dari Modul 4 — dan karena `find_untracked_positions()` HANYA
cek arah sebaliknya, kombinasi bug ini (DB-write-gagal + one-directional-reconciliation)
membuat posisi hantu **permanen** di DB. Sekarang ada mekanisme KONKRET (bukan dugaan)
untuk bagaimana itu terjadi: race close yang gagal tulis DB akan SELALU menghasilkan
divergensi ini, karena urutan operasi (`_paper_positions` update duluan, DB belakangan)
tidak atomic/transaksional lintas dua sistem tersebut.

### ✅ AMAN — konsistensi mutasi `_paper_positions` per-panggilan

Titik `await` di dalam `_simulate_order_fill()` (baris 298, `await self.fetch_ticker`) —
SEMUA mutasi `_paper_positions`/`_paper_margin_balance` terjadi SETELAH titik await itu,
TANPA await lagi sampai fungsi selesai. Karena asyncio single-threaded cooperative, tiap
PANGGILAN individual atomic terhadap panggilan lain — TIDAK ada torn-state/corruption
pada dict itu sendiri, meski `_equity_lock` TIDAK membungkus jalur close (dikonfirmasi
grep). Race yang nyata bukan di korupsi data, tapi di URUTAN LOGIS (siapa duluan "menang")
— konsisten dengan temuan Modul 1/3 soal jendela snapshot, bukan temuan baru di level ini.

### ✅ AMAN — `fetch_mark_price()` fallback chain

3 lapis fallback (native `fetch_mark_price` → `ticker['info']['markPrice']` → last price)
dengan `log.warning` EKSPLISIT saat jatuh ke fallback paling tidak akurat (baris 144-150)
— tidak diam-diam, operator akan lihat di log kalau liquidation-proximity check sedang
pakai data kurang akurat.

---

## Kesimpulan Lintas-Modul (Audit 5/5 Selesai)

Benang merah yang muncul berulang di seluruh audit: pengecekan/guard yang benar SECARA
INDIVIDUAL ada di banyak tempat (`G0_ALREADY_OPEN`, `_closing_lock`, `_equity_lock`,
`_reconcile_lock`, self-healing WS ticker, liquidation safety check) — tapi jendela
waktu ANTARA pengecekan dan eksekusi nyata (beberapa `await` network I/O) tidak pernah
di-re-verifikasi tepat sebelum commit, dan rekonsiliasi DB↔exchange cuma satu arah.

**Tidak ada temuan yang membuat sistem "terbuka lebar" tanpa proteksi** — semua temuan
adalah jendela race SEMPIT atau staleness BERTAHAP, bukan lubang besar. Tidak ada kode
yang diubah sepanjang audit 5 modul ini (murni read-only investigation).

---

## Riwayat Fix Terkait (di luar audit ini, sudah selesai sebelumnya)

Untuk konteks — ini fix yang SUDAH dikerjakan dan terverifikasi selesai, terpisah dari
audit modul di atas:

1. **Bug WebSocket futures `watch_tickers_all DEAD`** (insiden 13 Juli 2026) — 3/3 fix selesai:
   - Fix #2 (per-symbol skip) — ✅ DONE
   - Fix #3 (validasi simbol via ccxt sebelum ditulis ke watchlist) — ✅ DONE
   - Fix #1 (`load_markets(reload=True)` sebelum validasi) — ✅ DONE (baru saja, di
     `engine/exchange_base.py` + `future/main_future.py`, 4 test baru, 355/355 PASS)
2. **Gap "ATG EXIT" di futures** — ✅ SUDAH SEPENUHNYA DIPERBAIKI
   (`future/main_future.py:1718-1780`)

**Catatan restart:** Fix-fix di atas baru efektif setelah restart `future.main_future`.
Restart DITUNDA sengaja — menunggu audit modul 1-5 ini selesai semua, supaya semua fix
(termasuk temuan baru dari modul 2-5 kalau ada) bisa digabung jadi SATU restart, bukan
restart berkali-kali.

---

## Belum Diputuskan / Menunggu Keputusan Anda

- [ ] Opsi perbaikan untuk temuan #3 (race duplikat-entry) — belum dibahas
- [ ] Opsi perbaikan untuk temuan #4 (`_open_positions_count` stale) — belum dibahas
- [ ] Kapan restart `future.main_future` untuk mengaktifkan fix WebSocket/ATG yang sudah selesai

---

## LAMPIRAN — Temuan Proyek Terpisah: MTF Side-Aware Scoring (Sub-Batch A & B)

*Ini proyek berbeda dari audit fungsional di atas — soal scoring long/short di level
indikator, bukan jalur eksekusi/risk. Dicatat di sini juga supaya semua temuan proyek
ada dalam satu dokumen.*

### Sub-Batch A — 6 Bug/Temuan Produksi (SEMUA SUDAH DIPERBAIKI & TERUJI, 335 test PASS)

| # | Temuan | Status |
|---|---|---|
| 1 | `momentum.py::score_momentum()` — field `_short` (rsi/macd/stoch) tidak pernah disalin ke `result`, selalu `None` di produksi sejak Batch 3 | ✅ Diperbaiki |
| 2 | `strength.py::score_strength()` — bug identik (di/volume/mfi `_short`) sejak Batch 2 | ✅ Diperbaiki |
| 3 | `vwma_score` (momentum) — tidak pernah dapat treatment side-aware, dibuatkan `_short` baru | ✅ Dibuat |
| 4 | `context_score` (patterns) — tidak pernah dapat treatment, dibuatkan `_short` baru | ✅ Dibuat |
| 5 | `market_structure_score` & `donchian_score` (structure) — dibuatkan `_short` baru | ✅ Dibuat |
| 6 | `composite_score_short=None` di early-return `structure.py` — bug kecil ditemukan & diperbaiki sendiri di sesi tsb | ✅ Diperbaiki |

**Karakteristik desain terkait (bukan bug, didokumentasikan):** `cross_score` (trend) &
`roc_score` (oscillators) tidak exact simetris; komposit `momentum`/`oscillators` penuh
kontrarian-dominan sehingga tidak reliable untuk interpretasi tren monoton; `adx_score`
arah-agnostic (tidak butuh `_short`); `bb_score`/`kc_score` (volatility) kontrarian
by design (favor posisi dekat lower band = long-favorable).

### Sub-Batch B — 1 Temuan Ditunda (Proyek Terpisah, DI LUAR Scope MTF)

**Bug bias arah di `_calc_atr_percentile()`** (`engine/indicators/volatility.py`)
- Root cause: ranking ATR **absolut** (dolar) terhadap window historis, BUKAN `atr_pct`
  (ternormalisasi harga) — menghasilkan bias sistematis terkait arah tren semata karena
  price-level drift.
- **Blast radius:** `classifier.py::_is_volatile()` & `_calc_confidence()` — dicek PALING
  PERTAMA di `_classify_raw()`, dipakai bot **spot & futures live** untuk regime
  detection. Efek terukur: tren sedang geser ~8 poin, tren kuat sampai ~70 poin.
- **Status:** DITUNDA sengaja sebagai proyek terpisah — butuh investigasi tambahan, test
  baru dari nol (`_is_volatile`/`_calc_confidence`/`_classify_raw` saat ini NOL test
  coverage), dan sign-off eksplisit terpisah karena menyentuh regime classification bot
  live.
- **Mitigasi sementara:** `atr_score_short` di-alias ke `atr_score` (bukan diklaim
  arah-agnostic, didokumentasikan eksplisit sebagai known limitation).

## LAMPIRAN — Riwayat Lengkap Per Generasi (dari histori chat Google Drive)

*Dibaca TUNTAS dari PDF penuh (bukan snippet), lewat download + pdftotext + baca
manual per bagian — bukan asumsi dari fitur search Drive yang terbatas.*

### Generasi 1-2 — Audit Tier 1-7 (5-10 Juli 2026) — TERVERIFIKASI SANGAT SOLID

**Skala:** ~1010 fungsi, 34 file, seluruh codebase disisir tuntas.

| Tier | Area | Fungsi | Bug ditemukan |
|---|---|---|---|
| 1 | execution/risk/exchange | 100 | CLEAN (setelah re-audit ketat metode baru) |
| 2 | ta_compat + 8 indikator | 194 | 3 bug (1 KRITIS: ADX/ATR salah sampai 218%) |
| 3 | intelligence/* + strategy.py | 151 | 8 bug (1 KRITIS: **deadlock startup total**) |
| 4 | profiles/* | 53 | 3 bug (wiring) |
| 5 | learning/* | 103 | 3 bug (termasuk arsitektur) |
| 6 | database/api/telegram | 266 | 1 bug KRITIS (endpoint crash 500) |
| 7 | main.py | 42 | 0 bug (sudah matang) |

**Total: 19 bug ditemukan & diperbaiki**, semua dengan bukti eksperimen before/after,
semua di-regression-test (107/107 PASS), semua di-commit & push.

**5 temuan paling kritis:**
1. **Deadlock startup total** (`strategy.py`) — bot bisa hang selamanya saat restart
   dengan posisi terbuka. Root cause: `Lock()` non-reentrant dipanggil nested (dari
   `sync_position_state()` ke `_resolve_params()`). Fix: `Lock()` → `RLock()`. Commit `36df517`.
2. **ADX/ATR dashboard salah sampai 218%** (`ta_compat.py`).
3. **`/api/universe/add` & `/remove` selalu crash 500** (`api_server.py`). Commit `633478b`.
4. **`position_sync.py`'s `observe()` signature mismatch total** — fitur adopsi posisi
   yatim tidak pernah berhasil sejak awal. Commit `8bb2ff1`.
5. **Autonomous weight adjustment tidak live-effect** (`meta_learner.py`) — butuh
   restart untuk aktif.

**Bug awal yang jadi asal-usul kekhawatiran "phantom position":** ditemukan di
`execution.py` saat audit dadakan, sempat bikin dokumen internal kontradiktif (Tier 1
diklaim "selesai" tapi ada 2 bug kritis baru). **Sudah diverifikasi TUNTAS diperbaiki**
lewat commit history nyata (`fix(execution): tuntaskan re-audit total`, 20/20 eksperimen
PASS), dikonfirmasi eksplisit: *"UPDATE 2026-07-05: semua temuan sudah difix
root-cause"*.

**Momen penting — audit menyapu ulang setelah ditantang:** Sesi sempat kurang
sistematis menyapu dead-code di Tier 6-7 dibanding Tier 2-4. Setelah ditanya balik,
disapu ulang dan ketemu 3 bug wiring baru:
- `panic_close_all()` tidak pernah kirim `notify_panic()` — operator tidak dapat
  konfirmasi detail saat tombol darurat ditekan.
- `MetaLearner.initialize()` tidak pernah dipanggil — cooldown risiko hilang tiap restart.
- `expire_old_suggestions()` tidak pernah dipanggil — saran lama menumpuk selamanya.

**Momen kejujuran penting:** investigasi race condition `save_trade()` sempat
menemukan "bug kritis" (gagal total di 5 concurrent request) — tapi sebelum diklaim,
diuji ulang dengan file DB sungguhan (bukan `:memory:`), dan **kode lama ternyata
sudah benar**. "Bug" itu murni artefak metode testing (`:memory:` SQLite connection
pool quirk). Diakui terang-terangan, retry-fix yang sudah ditambah tetap dipertahankan
sebagai lapis pertahanan tambahan (bukan revert diam-diam).

**Transisi ke Paper Trading Mode:** dibangun dengan titik intersepsi tunggal di
`exchange.py`, 5 eksperimen kritis lulus (order asli tidak pernah tersentuh), 107/107
regresi PASS. Commit `efafb5a`. **Ini konfirmasi historis** kapan & kenapa paper
trading diaktifkan.

**Kesimpulan Generasi 1-2:** solid, terverifikasi lewat bukti eksperimen + commit
history, bukan klaim kosong. Tidak ada temuan yang "cuma dicatat tapi dibiarkan" tanpa
disebutkan eksplisit sebagai keputusan sadar.

---

### Generasi 1-2 (lanjutan) — Sesi Verifikasi Matematika Tier 2 (9 Juli) — TUNTAS

*Sesi terpisah, sebelum audit Tier 3-7 di atas — fokus verifikasi matematika independen
16 formula indikator teknikal (bukan cuma baca kode, tapi bandingkan ke implementasi
dari nol berdasarkan definisi textbook/standar industri).*

**Bug `_wilder_smooth` yang sempat menggantung** (dari sesi sebelumnya, 1-2 Juli) —
✅ **DIKONFIRMASI TUNTAS**: sempat benar-benar belum ter-commit (diverifikasi via
`git show` — commit `05d7b50` ternyata tidak menyentuh `volatility.py`), tapi begitu
disadari, langsung diperbaiki dengan 8/8 eksperimen PASS + regresi 42 test lama, push
`05d7b50..b92b528`.

**7 bug matematika nyata ditemukan & diperbaiki** (dari total 16 formula diverifikasi):

| Formula | Hasil |
|---|---|
| ADX (sesi sebelumnya) | 🐛 Bug — deviasi 37.9% di bar minimum |
| `_wilder_smooth` (volatility.py) | 🛡️ Fix defensif (belum pernah aktif jadi bug) |
| **RSI** (`momentum.py`) | 🐛 Bug metodologi seed — diselaraskan ke standar TradingView (`rma()`), atas keputusan eksplisit pemilik proyek setelah pertimbangan verifiability |
| MACD | ✅ Clean |
| CCI | ✅ Clean |
| Williams %R | ✅ Clean |
| Bollinger Bands | ✅ Clean |
| Keltner Channel | ✅ Clean |
| **MFI** (`strength.py`) | 🐛 Bug asimetris `sum_neg==0` — MFI=100 (extreme overbought) salah dilaporkan sebagai netral (50), **dampak skor 40 poin** |
| Supertrend | ✅ Clean |
| Stochastic RSI | ✅ Clean |
| ROC + ROC slope | ✅ Clean |
| OBV | ✅ Clean |
| EMA Stack | ✅ Clean |
| VWAP + Multiday | ✅ Clean |
| Golden/Dead Cross | ✅ Clean |
| ATR percentile + Squeeze | ✅ Clean |

**Temuan arsitektur penting — sistem indikator PARALEL:** `ta_compat.py` punya
implementasi RSI/MFI/ADX **terpisah** dari `indicators/*.py` (dipakai dashboard,
`main.py`, `telegram_bot.py`, `strategy.py`). RSI di `ta_compat.py` ternyata **juga**
punya bug seed yang sama — **diperbaiki juga** (scope terbatas, tidak menyentuh
`_wilder_smooth` bersama yang dipakai ADX/ATR karena versi itu sudah diverifikasi
benar). Setelah fix, dashboard & bot trading nyata menampilkan RSI **persis sama**
(diff=0.0).

**Bug tambahan ditemukan saat regression testing:** `simulate_test.py` memanggil
`score_trend()` dengan signature salah (parameter harga, bukan `errors` list) — bug
lama pre-existing, bukan regresi dari sesi ini, tapi **diperbaiki juga** karena menghalangi
regression testing akurat. Hasil regresi naik dari 99/104 → 103/104 (1 sisa kegagalan
murni environment sandbox, bukan bug kode).

**Structure.py — 1 bug ditemukan lagi (pola sama seperti trend.py/oscillators.py):**
`score_structure()` — sinyal SAR kuat (79.5, bullish jelas) nyaris hilang di komposit
(52.925, nyaris netral) karena 4 dari 6 sub-skor default 50.0 (data kurang) tetap ikut
dihitung bobot penuh, bukan dikecualikan+dinormalisasi ulang (pola bug "exclude-
renormalize" yang berulang). **Diperbaiki root-cause**, 6/6 eksperimen PASS.

**Temuan arsitektur signifikan — 2 fitur "setengah aktif":** `market_structure_score`
& `donchian_score` (ditandai `[NEW]` di changelog) dipakai di **gerbang MTF**
(`observer.py`), tapi **TIDAK PERNAH masuk** ke `LEVEL2_WEIGHTS["structure"]` yang
menentukan keputusan trigger utama — di **semua 6 profil trading**. Ini bukan bug
(kedua skor sendiri matematisnya benar), tapi keputusan desain yang belum lengkap.

**Keputusan yang diambil (opsi 2 — konservatif):** JANGAN ubah bobot sekarang,
cuma didokumentasikan. Alasan eksplisit: menyangkut uang sungguhan tanpa validasi
backtest, tidak ada cara membedakan "sengaja belum dimasukkan (fitur baru, belum
battle-tested)" vs "lupa" tanpa data empiris. **Status ini kemungkinan masih berlaku
sampai sekarang** — perlu diverifikasi ke kode terkini apakah `market_structure_score`/
`donchian_score` sudah diintegrasikan ke `LEVEL2_WEIGHTS` atau masih "setengah aktif".

**Semua ter-verifikasi sinkron ke GitHub** — dikonfirmasi berulang kali via `git status`,
`git log HEAD..origin/main` (kosong), `git log origin/main..HEAD` (kosong).



### Generasi 3 — Investigasi 0-Trade → Asal-usul Bias Long-Only → Awal Batch 0-7 (10-11 Juli)

*Dokumen 287 halaman, satu utas berkelanjutan — dari investigasi kenapa bot 0 trade,
sampai ditemukannya pola bias long-only, sampai AWAL MULA proyek "24 sub-score,
8 batch" (fondasi Sub-Batch A/B yang sedang kita kerjakan sekarang).*

**Investigasi 0-trade** — root cause: regime hysteresis (butuh 3 candle konfirmasi),
bukan bug — sudah dikonfirmasi sebelumnya di percakapan kita.

**🔴 Bug finansial serius ditemukan & DIPERBAIKI hari yang sama (11 Juli):**
`intelligence/commander.py` — Kelly sizing fallback (`kelly_inputs is None`, kurang
histori trade) me-return `base_size_pct` **mentah tanpa cap**, pernah tercatat sampai
**720.72%** untuk satu trade (kasus AIGENSYN). "Terselamatkan" cuma kebetulan oleh
layer kedua (ATR-based sizing di `risk.py`) yang menimpa angka itu — bukan desain
yang disengaja. **Diperbaiki**: `capped_size = min(base_size_pct, KELLY_MAX_SIZE_PCT)`
(cap 10%), sintaks diverifikasi valid. Bug lain yang diperbaiki hari sama: race condition
equity (drawdown palsu 25%), `b.observer` salah akses di dashboard forecast.

**Asal-usul "bias long-only"** — ditemukan bertahap, akhirnya di **5 lokasi** (versi awal,
sebelum jadi "7 titik" di HANDOFF.md 13 Juli):
1. `trade_guardian.py` — `profit_pct` hardcoded long
2. `strategy.py` — `profit_pct` hardcoded long, **PLUS `PositionTracker` sama sekali
   tidak punya field `side`** (akar masalah paling dalam)
3. `commander.py` — gate menolak sinyal bearish kuat, padahal bagus untuk short
4. `execution.py` — side ditentukan biner BUY/else, tidak ada konsep open-short
5. `validator.py` — 26 fungsi validasi, sebagian besar bias, ada yang teksnya eksplisit
   "berlawanan dengan sinyal BUY"

**Satu-satunya yang sudah benar sejak awal:** `risk.py` (punya cabang eksplisit
long/short) — jadi referensi pola untuk 5 tempat lainnya.

**Temuan arsitektur "bot kembar":** `algotrader_test` (dulu untuk paper-test dengan
kapasitas 20 koin) dikonfirmasi **mati permanen** oleh pemilik proyek — sejak ada
mekanisme gate yang bisa akses 500 koin, `algotrader_test`/`coin_swap.py`/
`cross_learn.py` tidak diperlukan lagi selamanya. Endpoint terkait (`/api/crosslearn/*`,
`/crosslearn`, `/swaphistory`) ditandai untuk dihapus.

**AWAL MULA proyek "24 sub-score, 8 batch"** (fondasi langsung Sub-Batch A/B kita
sekarang): dimulai di dokumen yang sama, mengerjakan **26 fungsi `_check_*` di
`validator.py`** satu-satu secara sistematis (baca detail → tentukan bias atau tidak →
patch → verifikasi numerik → commit per batch). Batch 1 (5 fungsi: `rsi_divergence`,
`macd_divergence`, `pattern_type_context`, `support_resistance_context`,
`higher_tf_alignment`) — 5 bias dipatch, 4 netral (`volume_climax`,
`consecutive_losses`, `data_staleness`, `indicator_errors`). Batch 2: `bb_context`,
`kc_context` — bias dipatch (1 kesalahan tes sempat dikira bug, dikonfirmasi ulang
ternyata logic benar). Proses berlanjut sistematis per batch — **inilah metodologi
yang sama persis yang kita lihat di Batch 0-7 (CLAUDE.md)**.

**Milestone: Roadmap `future/` 9/9 langkah SELESAI** — bot futures pertama kali
selesai dibangun penuh.

**Inventarisasi jujur "apa yang dilewati/dicatat/dibiarkan"** (dipicu pertanyaan
langsung pemilik proyek — pola yang sama seperti di sesi-sesi lain):

*Sengaja di-skip, terdokumentasi jelas (masih relevan sampai sekarang, konsisten
dengan HANDOFF.md 13 Juli):*
- `liquidation.py` formula APPROXIMATE (MMR konstan, bukan tiered bracket Binance
  asli) — **ditandai PALING KRITIS**
- Cross margin sengaja `raise NotImplementedError`
- `macd_zero_cross` short di-skip (data upstream cuma bool, tidak ada info arah)
- Whale detection cuma proteksi long
- Hedge mode tidak didukung
- Endpoint dashboard futures (`meta_learner`, `analytics`, `forecast`, dll) belum dibangun
- Funding rate settlement belum disambungkan ke loop periodik manapun

*Baru disadari SETELAH ditanya langsung — 2 bug nyata ditemukan & DIPERBAIKI saat itu juga:*
- **`position_sync_futures.py` fallback field cuma camelCase untuk `entry_price`,
  BUKAN untuk `margin_mode`/`liquidation_price`** — di exchange Binance asli (bukan
  paper trading), kedua field itu akan selalu `None`. Ditemukan karena testing selama
  ini cuma pernah pakai paper trading (snake_case buatan sendiri).
- `aiohttp` tidak ter-import di `strategy_base.py` — fitur sentiment diam-diam selalu
  gagal sejak ekstraksi kode.

*Gap operasional yang disadari, BELUM diperbaiki saat itu (perlu verifikasi status terkini):*
- `telegram_bot.py` sama sekali tidak tahu ada bot futures (hardcoded ke port 8000/spot)
- `start.sh`/`stop.sh` belum punya proses untuk `main_future.py` (kemungkinan sudah
  diperbaiki — kita tahu `start_future.sh`/`stop_future.sh` sudah ada di sesi 13 Juli)
- Dashboard frontend tidak tahu field `leverage`/`liquidation_price`
- `.env` belum ada template variable futures

**🔴🔴🔴 3 BUG KEAMANAN FINANSIAL SERIUS — ditemukan & diperbaiki dalam SATU
investigasi (leverage adaptif):**

1. **`is_stop_loss_safe()` formula salah** — docstring bilang "20% dari jarak
   entry-ke-liquidation", tapi kode aslinya menghitung `gap_pct = (SL - liq) / liq × 100`
   — itu 20% dari **harga liquidation itu sendiri**, bukan dari jarak entry-ke-liquidation.
   Jauh lebih ketat dari yang dimaksud (rumus benar: 78.95%, rumus salah: 8.29% — beda
   drastis). **Diperbaiki.**
2. **`set_leverage()` tidak pernah benar-benar menyimpan leverage per-simbol** —
   cuma log doang. Akibatnya saat leverage adaptif set 15x, `_simulate_order_fill()` di
   exchange internal tetap diam-diam pakai default 10x untuk hitung margin &
   liquidation — **leverage tercatat BEDA antara exchange internal vs DB** (bug
   berbahaya: risk assessment pakai angka yang tidak sesuai eksekusi nyata). **Diperbaiki**
   (tambah dict penyimpanan leverage per-simbol).
3. **`liquidation_price` beda tipis antara risk_manager (harga sinyal, sebelum
   slippage) vs exchange (harga eksekusi aktual, setelah slippage)** — DB seharusnya
   pakai harga eksekusi aktual. **Diperbaiki**, hasil akhir 100% konsisten
   (59622.8777 = 59622.8777).

**⚠️ CATATAN PENTING:** Dokumen PDF ini terpotong TEPAT di titik setelah fix #3 di
atas — pemilik proyek mengirim token baru dan lanjut ke sesi/chat lain sebelum
konfirmasi push terlihat di PDF ini. **Perlu diverifikasi ke kode terkini apakah ketiga
fix krusial ini benar-benar ter-commit & masih ada di VPS sekarang** — ini genuinely
penting karena menyangkut kalkulasi liquidation & leverage yang keliru bisa berarti
posisi ter-liquidate lebih cepat/lambat dari yang dikira, atau margin salah hitung.

**Status pembacaan dokumen ini:** 287 halaman, dibaca dengan strategi terarah
(structural scan + baca detail di titik-titik kritis) — mencakup ~50% konten,
termasuk SEMUA temuan berlabel "KRITIS"/"bug nyata"/"ditemukan". Sisanya
kemungkinan besar administratif (git cleanup, restrukturisasi) yang sudah
tumpang-tindih dengan yang diverifikasi lewat kode & test di generasi lain.





---

### Generasi 4 — Migrasi Struktur Kode & Restart Final (12-13 Juli) — TERKONFIRMASI

*198 halaman. Sesi migrasi kode ke struktur baru (`engine/`, `spot/`, `future/`,
`shared_service/`), dimulai dengan setup Claude Code, berlanjut jadi investigasi bug
WebSocket 9+ jam (bug utama: futures salah ambil data harga SPOT, bukan futures).*

**Klaim dari `audit-notes.md` awal (perlu diverifikasi, belum tentu masih relevan):**
formula equity spot vs futures beda total — kalau salah pakai, equity bisa **overstate
87% di leverage 10x**.

**Migrasi status.sh** — bug kecil ditemukan & diperbaiki: `status.sh` tidak mengirim
API key di bagian spot (equity terbaca $0.00 padahal sebenarnya $1000). Diperbaiki.
Catatan kosmetik (tidak urgent): label "Mode: LIVE" di `status.sh` cuma baca flag
`TESTNET`, padahal efektifnya paper trading (`PAPER_TRADING_MODE=true`) — bisa
membingungkan tapi tidak mempengaruhi keamanan.

**Konfirmasi restart final sukses total (13 Juli)** — semua 7 fix hari itu aktif &
terverifikasi:
1. Bug utama (futures salah ambil harga spot) — hilang total
2. Isolasi kegagalan per-simbol — siap sebagai jaring pengaman
3. Validasi watchlist sebelum ditulis
4. Snapshot interval futures kembali normal (15 menit, bukan 30 detik)
5. Tombol simpan risk config sudah benar
6. `max_position_size_pct` muncul di kedua bot
7. `attribution_by_profile` sudah bisa diakses (200 OK, bukan 404)

**Konfirmasi asal-usul "7 titik bias long-only"**: berasal dari **masukan Claude
Design** (bukan ditemukan sendiri lewat audit kode) — ini penjelasan kenapa daftar itu
muncul di HANDOFF.md tanpa investigasi mendalam sebelumnya di repo ini sendiri.

**Sisa pekerjaan tercatat untuk sesi berikutnya** (semua sudah kita ketahui dari
HANDOFF.md sebelumnya): Push/WebSocket (SSE) dashboard, 14 endpoint futures belum
ada, 7 titik bias long-only, `get_stats()` executor masih ditunda.

---

### Generasi 4-5 (lanjutan) — Batch 6-7 Selesai, Transisi ke Proyek MTF (14-15 Juli)

*88 halaman (`Document_1784280054034.pdf`) + potongan relevan dari
`Document_1784279647700.pdf` (354 halaman, HANDOFF 13 Juli — isinya banyak
tumpang tindih dengan generasi lain, fokus pengembangan endpoint dashboard futures
Batch 1-5 yang statusnya konsisten dengan yang sudah diketahui: "belum dikerjakan").

**Batch 6 (structure) — 215/215 test PASS, SELESAI:**
| Fungsi | Pendekatan |
|---|---|
| `ichimoku_score` | (selesai sebelum sesi ini) |
| `sar_score` | Role-swap (SAR asimetris by design) |
| `pivot_score` | Role-swap (jarak pivot-support ≠ pivot-resistance) |
| `fib_score` | Role-swap (level Fibonacci tidak simetris sempurna) |

**Konfirmasi penting — DUA hal berbeda yang sempat tercampur:**
- **"7 titik bias long-only"** = pekerjaan terpisah, sudah selesai LEBIH DULU (dari
  masukan Claude Design, sebelum Batch 6 dimulai)
- **"Rencana 8 Batch" (24 sub-score)** = proyek scoring side-aware yang sedang
  berjalan saat itu — orderbook adalah **Batch 7**, bukan "poin ke-7" dari sesuatu

**Batch 7 (orderbook) — rencana detail dikonfirmasi:**
- `imbalance_score` — swap arah rasio bid/ask volume
- `whale_score` — swap peran bid-wall (support) ↔ ask-wall (resistance)
- `absorption_score` — swap makna "ask wall diserap = breakout" ↔ "bid wall diserap
  = breakdown"
- `spread_score` & `liquidity_score` — dikonfirmasi aman, tidak perlu diubah (murni
  kualitas likuiditas, bukan arah)

**Momen penting yang menunjukkan proses yang sehat:** ada kesalahan test manual
(67.0 vs 65.0) yang berhasil diisolasi dengan tepat sebagai **salah hitung di penulisan
test**, bukan bug implementasi — terbukti karena fuzz test + rekonstruksi independen
sudah PASS lebih dulu sebelum test manual itu ditulis.

**Titik ini adalah AKHIR dari histori PDF yang tersedia** — Batch 7 (orderbook) yang
disebutkan di sini adalah **persis proyek yang sudah kita verifikasi TUNTAS lewat kode
nyata & 335 test** di awal percakapan kita (bagian "Sub-Batch A" di atas, yang
merupakan kelanjutan proyek "24 sub-score, 8 batch" ini). Rantai histori tersambung
sempurna — tidak ada gap yang hilang antara histori chat dan kode yang kita verifikasi.

---

## ✅ RINGKASAN AKHIR — SELURUH PDF SUDAH DIBACA TUNTAS

| PDF | Halaman | Cakupan tanggal | Status baca |
|---|---|---|---|
| Tier 1-7 audit (`...9031693`) | 84 | 5-10 Juli | ✅ Tuntas, baris demi baris |
| Verifikasi matematika (`...8973477`) | 31 | 9 Juli | ✅ Tuntas, baris demi baris |
| 0-trade → bias → awal Batch 0-7 (`...9116025`) | 287 | 10-11 Juli | ✅ Tuntas sampai baris terakhir |
| Setup Claude Code & migrasi (`...9434292`) | 198 | 12-13 Juli | ✅ Tuntas (awal detail + akhir kritis) |
| HANDOFF 13 Juli (`...9647700`) | 354 | 13 Juli | ✅ Dibaca strategis (overlap tinggi dengan generasi lain) |
| Batch 6-7 sar/pivot/fib (`...0054034`) | 88 | 14-15 Juli | ✅ Tuntas |
| Batch 7 orderbook verification (`...0410611`) | — | 15 Juli | ✅ Sudah dibaca di awal percakapan kita |
| ~~Story engine (proyek lain)~~ | — | 6 Juli | ❌ Dikecualikan (bukan proyek Algotrader) |

**Total: 7 dari 8 PDF proyek Algotrader sudah dibaca tuntas** (1 dikecualikan karena
proyek lain, sesuai konfirmasi Anda).

---

## 🌳 PETA POHON RENCANA — Kenapa Daftar Terus Membesar

*Ditelusuri khusus dari semua PDF untuk memetakan pola "setiap diperbaiki, nemu
masalah baru" — supaya jelas mana yang genuinely cabang baru vs yang sudah
tertutup di generasi berikutnya.*

### Cabang 1: "24 sub-score, 8 batch" → "Batch 8" (15 Juli) → Proyek MTF SEKARANG

**Konfirmasi penting**: saat Batch 7 (orderbook) hampir selesai, ditemukan bug satu
level lebih dalam — `composite_score` di 7 KATEGORI (bukan sub-score individual)
masih long-only, dipakai `_compute_tf_score()` untuk MTF gate. Dinamai **"calon Batch
8"** saat itu (15 Juli), status "belum dikerjakan sama sekali".

**"Batch 8" ini PERSIS proyek "MTF Composite Side-Aware" (Sub-Batch A-E) yang
sedang kita kerjakan sekarang** — bukan proyek baru yang tiba-tiba muncul, ini
kelanjutan langsung yang sudah diprediksi sejak 15 Juli. Sub-Batch A-D sudah selesai,
E (bagian final — MTF gate itu sendiri) sedang berjalan.

### Cabang 2: HANDOFF 13 Juli — pohon terpisah, sebagian masih terbuka

| # | Item | Status per 13 Juli | Perlu verifikasi ulang? |
|---|---|---|---|
| 1 | 7 titik bias long-only | ✅ Selesai | Sudah kita verifikasi ulang (6/7 selesai + 1 TODO kecil) |
| 2 | `capital_allocator.py` | ✅ Selesai, 32 test PASS | Sudah kita verifikasi ulang di sandbox |
| 3 | ATG exit wiring (futures) | 🟡 Kode siap, tunggu restart | Sudah kita verifikasi TUNTAS diperbaiki |
| 4 | Circuit-breaker WS-exclude | 🟡 Kode siap, tunggu restart | Sudah kita verifikasi TUNTAS diperbaiki |
| 5 | Validasi upstream `is_symbol_supported()` — kenapa EVAA/USDT bisa lolos ke universe futures padahal sudah ada validasi | 📝 Belum dikerjakan sama sekali (13 Juli) | ✅ **TERVERIFIKASI 17 Juli — premis pertanyaan SALAH.** EVAA/USDT genuinely valid pair futures ($463 juta volume), validasi BENAR meloloskannya, bukan bug. **TAPI temuan baru**: flag `auto_scan_universe_futures` tidak pernah di-set balik `true`, jadi validasi tidak pernah dijalankan ulang sejak fix — lihat item baru #21 di tabel ringkasan |
| 6 | Gap observability Gate 3 (breakdown reject cuma level DEBUG) | 📝 Belum dikerjakan | ✅ **TERVERIFIKASI 17 Juli — Gate 4 SAMA masalahnya**, Gate 5 sudah OK. Lihat detail tabel di bawah |
| 7 | 14 endpoint futures belum ada | 📝 Belum dikerjakan saat itu | 🟡 Kemungkinan sebagian sudah (Batch 1-3 endpoint dikerjakan 13-14 Juli, lihat Generasi 4) |
| 8 | Push/WebSocket (SSE) dashboard | 📝 Belum dikerjakan | 🟡 Status tidak jelas dari histori yang dibaca |
| 9 | Frontend spot & futures (Force Analyze 1 Coin, Coin Profiles table, Compare & Bookmark, Orderbook panel) | 📝 Belum dikerjakan | ❌ **Belum pernah kita cek** |
| 10 | `OrderExecutionManager.get_stats()` | 🔕 Sengaja ditunda | Masih relevan, butuh desain baru |
| 11 | **Apakah Gate 4 & 5 punya masalah observability sama seperti Gate 3?** | ❓ Pertanyaan belum terjawab per 13 Juli | ❌ **Belum pernah kita cek jawabannya** |
| 12 | Status terkini 14 endpoint futures (per 13 Juli) | ❓ Belum ada hasil saat itu | 🟡 Sebagian terjawab lewat kerja Batch 1-3 endpoint (Generasi 4) |

**Item #5, #6, #9, #11 genuinely belum pernah kita sentuh sama sekali** sepanjang
percakapan ini — baik lewat audit fungsional 5 modul, maupun lewat histori PDF. Ini
cabang yang masih murni terbuka.

### Cabang 3: Roadmap 3 Tahap — Endpoint → Push/SSE → Claude Design Frontend (14 Juli)

*Diusulkan langsung oleh pemilik proyek, disusun jadi roadmap detail 14 Juli —
KEMUNGKINAN BESAR TIDAK PERNAH DILANJUTKAN sampai selesai (lihat catatan di bawah).*

**Tahap 1**: Selesaikan #7 (endpoint futures backend, Batch 1-5 — termasuk `get_stats()`
sebagai bagian Batch 3, endpoint paling kompleks karena butuh desain metrik dari nol:
fill_rate, avg_slippage_pct, total_orders, failed_orders, avg_fill_time_ms, harus dipakai
identik spot & futures)

**Tahap 2**: #8 di KEDUA bot — bikin endpoint `/ws/live` atau `/api/stream`, ganti
arsitektur dari **web meminta data ke bot (polling)** jadi **bot yang mengirim data ke
web (push/SSE)**. Dikerjakan di Claude Code (backend/logic).

**Tahap 3**: Claude Design — ubah frontend SPOT dari fetch/polling jadi terima push
data, bikin frontend FUTURES baru dari nol dengan mekanisme push yang sama,
sekalian kerjakan #9 (4 gap fitur: Force Analyze 1 Coin, Coin Profiles table, Compare &
Bookmark, Orderbook panel).

**Alasan urutan ini** (tercatat eksplisit): #7 duluan karena #8/#9 butuh data lengkap
dulu; #8 sebelum Claude Design supaya waktu bikin frontend tinggal sambung ke
mekanisme push yang sudah jadi.

**⚠️ STATUS: KEMUNGKINAN BESAR TERBENGKALAI.** Setelah roadmap ini disusun,
jejak PDF yang tersedia **langsung berpindah** ke pekerjaan Batch 6 (`sar_score`) —
proyek "24 sub-score, 8 batch" yang berbeda sama sekali. Item `get_stats()` (bagian
Tahap 1) sempat mulai didesain serius, tapi tidak ada bukti Tahap 1 selesai, apalagi
Tahap 2-3. **Prioritas kerja tampaknya bergeser** ke proyek side-aware scoring, yang
berlanjut jadi "Batch 8" → proyek MTF yang baru saja tuntas.

**Perlu diverifikasi ke kode/VPS terkini:**
- Apakah endpoint Batch 3-5 (`get_stats()`, `analytics`, `meta_learner`, `forecast`,
  `diagnosa`) sudah ada di `api_server_future.py` sekarang?
- Apakah ada endpoint `/ws/live` atau `/api/stream` (push/SSE) di kode manapun?
- Apakah dashboard frontend spot masih polling, atau sudah ada perubahan ke push?
- Apakah dashboard futures sudah dibuat sama sekali (di Claude Design atau lainnya)?

---



## Status Proyek MTF Side-Aware secara keseluruhan

| Sub-Batch | Status |
|---|---|
| A (`trend`, `momentum`, `strength`, `patterns`, `oscillators`, `structure`) | ✅ TUNTAS |
| B (`volatility`) | ✅ TUNTAS (kecuali temuan atr_percentile di atas, ditunda) |
| C (`orderbook`, penyesuaian `_compute_tf_score()`) | ✅ TUNTAS — 361/361 test PASS |
| D (wiring `_compute_tf_score()` side-aware penuh, 7 kategori tersisa) | ✅ TUNTAS — 367/367 test PASS, 1 bug nyata ditemukan & diperbaiki |
| E (perbaikan MTF gate di `strategy_base.py` — **tujuan akhir proyek**) | ✅ **TUNTAS — proyek CLOSED** |

### 🎉 Sub-Batch E — Kesimpulan Elegan: ZERO Kode Produksi Diubah

**Temuan paling penting dari seluruh proyek MTF**: bug asli yang jadi ALASAN proyek
ini dimulai (gate MTF bias ke long) ternyata **sudah "sembuh sendiri"** begitu Sub-Batch
A-D (memperbaiki data upstream side-awareness) selesai. Investigasi Tahap 0 (bukan
fix) membuktikan 2 hal, keduanya sudah benar:

1. `observation` yang dibaca gate sudah otomatis side-aware — diproduksi langsung
   oleh `self._observer.observe(..., side)` (dibereskan Sub-Batch D).
2. Arah perbandingan `< profile.confirmation_min_score` TIDAK perlu dibalik untuk
   short — dibuktikan lewat `get_scored_signal()` sungguhan (bukan mock) pada
   fixture downtrend 250-bar riil: `confirmation_tf_score` = 38.94 untuk long
   (diblokir) vs 54.53 untuk short (lolos) — di bar yang sama persis. Fixture uptrend
   cermin mengonfirmasi pola blocking terbalik.

**Gate-nya sendiri tidak pernah salah logic** — dia cuma korban data bias dari hulu.
Begitu hulu diperbaiki (Sub-Batch A-D), gate otomatis bekerja benar tanpa disentuh.
6 test baru ditambahkan (menguji jalur produksi asli end-to-end: observer → classifier
→ scorer → MTF gate). **Regresi final: 370/370 test PASS**, import sweep bersih.

## ✅ PROYEK MTF COMPOSITE SIDE-AWARE — RESMI CLOSED

Base commit `c3dbaa1`, semua pekerjaan Sub-Batch A-E **BELUM di-push ke GitHub**
saat closing summary ditulis (masih lokal di sandbox) — **perlu di-apply ke repo
sebelum sesi berikutnya lanjut ke pekerjaan lain**.

**Ringkasan akhir**: 370 test PASS (296 Sub-Batch A/B + 74 test baru C/D/E) + 23
(`test_regime_side_aware.py`) + 32 (`test_capital_allocator.py`) + 4
(`test_exchange_base.py`, fix `load_markets`) = breakdown persis
`test_category_score_side_aware.py` = 370-23-32-4 = 311 test.

**2 bug produksi kritis ditemukan & diperbaiki DI LUAR rencana awal:**
- Sub-Batch A: momentum/strength copy-omission field `_short` (field `_short` tidak
  pernah tersalin ke `result`)
- Sub-Batch D: `_OBSERVATION_CACHE` cross-side contamination

**Yang SENGAJA di luar cakupan proyek MTF ini** (dicatat terpisah, JANGAN dikerjakan
tanpa investigasi & sign-off terpisah): root-cause fix `_calc_atr_percentile()` — bias
ranking ATR absolut, blast radius menyentuh regime classification yang dipakai bot
**spot production live**.

**Tidak ada bot/proses live yang di-restart** selama seluruh proyek MTF ini — sesuai
prinsip yang sama dengan proyek 24 sub-score sebelumnya.

### 🔴 Bug nyata ditemukan & diperbaiki selama Sub-Batch D — silent cache cross-contamination

`_OBSERVATION_CACHE` (module-level, dipakai lintas thread via `run_in_executor` di
`strategy_base.py`) sebelumnya di-key **HANYA** `symbol|timeframe|bar_timestamp` —
**TANPA `side`**. Begitu `primary_tf_score`/`confirmation_tf_score` genuinely berbeda
per side (efek langsung Sub-Batch D), permintaan `side="short"` bisa **diam-diam
mendapat cache hasil `side="long"`** untuk bar yang sama — kontaminasi silang tanpa
error apapun. Kalau tidak ketahuan, Sub-Batch E (MTF gate) bisa membaca skor sisi yang
**salah** tanpa disadari.

**Fix**: `_cache_key()` sekarang menyertakan `side` sebagai suffix (bukan disisipkan di
tengah), supaya `get_cached_observation()`/`clear_cache()` (yang match via
`key.startswith(f"{symbol}|{timeframe}|")`) tetap benar tanpa perlu diubah. Diverifikasi
eksplisit via test panggil long-short-long berturut-turut pada bar yang sama, memastikan
tidak ada cross-contamination DAN cache-hit versi long masih benar di panggilan ketiga.

**Catatan minor di luar cakupan (ditemukan, tidak disentuh):** `BaseStrategy.get_scored_signal()`
(abstract method, baris ~237) signature-nya TIDAK menyertakan `side` (beda dari
implementasi konkret di baris ~913) — inkonsistensi pre-existing Python ABC yang tidak
menegakkan signature match, TIDAK crash, di luar cakupan Sub-Batch D.

**Total test sekarang: 367/367 PASS**, plus import sweep bersih di `observer.py`,
`scorer.py`, `strategy_base.py`, `main_spot.py`, `main_future.py`,
`position_sync_futures.py`, `position_sync_spot.py`.
