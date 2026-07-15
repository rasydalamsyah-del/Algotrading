# AlgoTrader — Dokumentasi Handoff Sesi (13 Juli 2026)

> **Cara pakai file ini:** Copy-paste seluruh isi file ini sebagai pesan pertama ke sesi Claude Code baru. Ini berisi semua konteks yang dibutuhkan supaya sesi baru bisa langsung lanjut kerja tanpa perlu investigasi ulang dari nol.

---

## 1. Info Akses & Lingkungan

- **VPS**: idCloudHost, Singapore (sgp01), Ubuntu 24.04 LTS, 2 vCPU / 2GB RAM
- **IP**: `103.187.146.208`
- **Path project**: `/root/algotrader`
- **Bot spot**: port 8000, dashboard di `http://103.187.146.208:8000/dashboard/`
- **Bot futures**: port 8001
- **Mode**: **Paper trading di kedua bot** (modal virtual $1000 masing-masing, order tidak pernah benar-benar dikirim ke exchange, TIDAK ada risiko finansial nyata)
- **Struktur kode**: `engine/` (shared), `spot/`, `future/`, `shared_service/`, `dashboard/`

**Cara masuk & kontrol:**
```bash
ssh root@103.187.146.208
cd /root/algotrader
bash status.sh          # cek status kedua bot
bash start.sh / stop.sh # spot
bash start_future.sh / stop_future.sh  # futures
claude                  # masuk Claude Code
```

---

## 2. Ringkasan Apa yang Sudah Dikerjakan (13 Juli 2026)

### A. Restrukturisasi & Setup Dasar
- Migrasi dari `main.py` monolitik lama ke struktur baru `engine/` + `spot/` + `future/` + `shared_service/`
- Setup Claude Code langsung di VPS
- Aktivasi akun Binance Futures + API key baru dengan permission Futures (key lama tidak bisa dipakai futures karena dibuat sebelum akun futures aktif)

### B. Perbaikan Dashboard — Prioritas 1 & 2 (selesai, murni frontend)
Field-field yang salah nama/format antara dashboard (`.dc.html`) dan backend API, diperbaiki di 6+ halaman: Dashboard, Positions, Risk Monitor, Settings, AI Learner. Termasuk perbaikan bug UX field API Base/Key yang tidak auto-save.

### C. Bug BESAR: Bot Futures Salah Ambil Data Pasar (root cause, sudah diperbaiki)
**Ini temuan paling penting hari ini.** Kronologi:
1. Auto-scan universe futures diaktifkan (dari 14 koin manual → 200 koin otomatis dari Binance)
2. Setelah restart, ditemukan `watch_tickers_all` masuk **infinite retry loop** karena banyak simbol hasil scan (mis. `EVAA/USDT`, `1000SHIB/USDT`) tidak dikenali oleh koneksi trading
3. **Diagnosis awal (SALAH):** dikira `ccxt` tidak me-refresh market list setelah scan → sudah dibuktikan salah, dibatalkan
4. **Root cause sebenarnya:** class `WebSocketFeed` (dipakai bersama oleh spot & futures, didefinisikan di `spot/exchange_spot.py`) **hardcode `defaultType: "spot"`**. Akibatnya, bot futures selama ini mengambil harga lewat konteks market **SPOT**, bukan futures — bukan crash, tapi **salah data secara diam-diam** selama 9+ jam bot berjalan.
5. **Fix**: tambah parameter `default_type: str = "spot"` ke constructor `WebSocketFeed` (default tetap sama, tidak breaking untuk spot), lalu `future/main_future.py` kirim `default_type="future"` secara eksplisit.
6. **Diverifikasi aman**: diaudit baris-per-baris bahwa `WebSocketFeed` 100% instance-based (tidak ada shared state antar instance spot/futures) sebelum fix diterapkan.

### D. Fix Tambahan Terkait (semua sudah diverifikasi & aktif)
| # | Fix | File |
|---|---|---|
| 1 | `defaultType` WebSocketFeed (root cause di atas) | `spot/exchange_spot.py`, `future/main_future.py` |
| 2 | Isolasi kegagalan per-simbol di `_watch_tickers_all` — 1 simbol gagal (`ccxt.BadSymbol`) tidak lagi menghentikan seluruh batch 200 koin, cukup di-exclude & di-log warning | `spot/exchange_spot.py` |
| 3 | Validasi `is_symbol_supported()` — simbol hasil scan divalidasi terhadap `ccxt.market()` (bukan raw dict lookup yang terbukti salah) sebelum ditulis ke `universe_futures.json` | `engine/exchange_base.py`, `future/exchange_future.py`, `future/main_future.py` |
| B | `SNAPSHOT_INTERVAL` futures diperbaiki dari hardcode 30 detik → `self.SNAPSHOT_INTERVAL` (900 detik/15 menit), sebelumnya DB futures menulis ~13x lebih cepat dari seharusnya | `future/main_future.py` |
| C | Endpoint `/config/risk` di dashboard diarahkan ke `/config/update` yang sudah ada; field `atr_mult_sl/tp` diganti ke nama asli `atr_multiplier_sl/tp` (sebelumnya kolom ini SELALU tampil kosong di UI); ditambah cek `r.success` supaya tidak salah tampil toast sukses saat backend menolak | `dashboard/Settings.dc.html` |
| D | `max_position_size_pct` ditambahkan permanen ke response `/system_health` (spot & futures), workaround double-fetch di Risk Monitor dashboard dihapus | `spot/api_server_spot.py`, `future/api_server_future.py`, `dashboard/Risk Monitor.dc.html` |
| — | Endpoint baru `GET /api/analytics/attribution_by_profile` — expose fungsi `compute_all_profiles()` yang sudah ada di `engine/learning/analytics.py` tapi belum pernah di-expose lewat API manapun | `spot/api_server_spot.py` |

### E. Restart Final (selesai, terverifikasi sukses total)
Kedua bot (spot PID baru, futures PID baru) sudah di-restart membawa semua fix di atas. Hasil verifikasi pasca-restart:
- Scan futures: 707 ticker → 573 kontrak valid → 200 koin lolos filter, **0 simbol dibuang** validasi
- **0 error/critical**, **0 baris infinite loop** (dibanding sebelumnya yang macet total sejak detik-detik awal)
- `max_position_size_pct` muncul benar di kedua bot (25.0)
- `attribution_by_profile` sekarang 200 OK (sebelumnya 404)
- Integritas kedua database (`PRAGMA integrity_check`) tetap OK

**Kesimpulan: kedua bot sekarang sehat, futures benar-benar menggunakan data market futures asli (bukan spot lagi), dan siap dipantau beberapa hari di paper trading sebelum dipertimbangkan live.**

---

## 3. Daftar Pekerjaan Berikutnya (belum dikerjakan, urutan prioritas)

### Prioritas Tinggi

**1. Verifikasi status 7 titik bias long-only**
Dari `audit-notes.md` (dokumentasi audit sebelum sesi ini), ada 7 titik kode yang membuat bot futures cenderung hanya bisa LONG, belum benar-benar bisa SHORT:
- `trade_guardian.py` (`check_atg`) — rumus profit belum sadar arah long/short
- `strategy.py` (`PositionTracker`) — field `side` belum diteruskan ke semua fungsi
- `commander.py` (`_gate_supertrend`) — belum ada cabang untuk terima sinyal bearish sebagai short-worthy
- `execution.py` (`execute_signal`) — logic biner (BUY/else=SELL), belum eksplisit `open_long/close_long/open_short/close_short`
- `validator.py` (26 fungsi `_check_*`) — sebagian besar butuh versi mirror untuk arah short
- `scorer.py` (`_check_primary_trigger`) — belum ada cabang terima skor rendah (bearish) sebagai trigger valid untuk short
- `position_sync.py` — perlu versi baru untuk futures (konsep leverage beda dari baca saldo koin spot)

**Langkah pertama di sesi baru: minta Claude Code cek ulang apakah 7 titik ini masih persis seperti didokumentasikan, atau ada yang kebetulan sudah tersentuh selama sesi restrukturisasi/fix hari ini.** Ini murni permintaan status, belum untuk diperbaiki dulu sampai jelas cakupannya.

**2. Mekanisme alokasi saldo long vs short (skenario baru dari pemilik project — lihat detail di bagian 4 di bawah)**
Ini terkait erat dengan poin 1 di atas — perbaikan bias long-only itu prasyarat sebelum mekanisme alokasi saldo long/short bisa benar-benar berfungsi adil.

### Prioritas Sedang

**3. 14 endpoint futures yang belum ada (dulu disebut "Prioritas 3")**
`future/api_server_future.py` belum punya endpoint yang dipakai 6 halaman dashboard berikut, sehingga mereka selalu fallback ke data contoh saat dipakai untuk futures:
- Dashboard.dc.html → `/dashboard_snapshot`
- Diagnosa.dc.html → `/diagnosa`
- Gate Scanner.dc.html, Coin Detail.dc.html → `/forecast`
- Watchlist.dc.html → `/universe/detail`, `/tickers`, `/intelligence/scores`
- AI Learner.dc.html → `/meta_learner/*`, `/analytics/*`, `/executor/stats`, `/shadow_trades`
- Analytics.dc.html → `/metrics`
- Coin Detail.dc.html → `/candles/{symbol}`

**4. Push/WebSocket (SSE) untuk dashboard**
Saat ini dashboard polling (nanya bot tiap 15-20 detik). Rencana: bikin endpoint `/ws/live` atau `/api/stream` supaya bot push data begitu ada perubahan (posisi baru, harga berubah, sinyal masuk), tanpa dashboard perlu polling berulang. Butuh library `websockets` atau `EventSourceResponse` (FastAPI) di backend, dan ubah 12 halaman dashboard dari `setInterval+fetch` ke `WebSocket`/`EventSource` listener.

### Ditunda (Jangan Dikerjakan Kecuali Diminta Eksplisit)

**5. `OrderExecutionManager.get_stats()` belum pernah diimplementasikan**
Endpoint `/executor/stats` di spot selalu fallback ke dict kosong karena method backend-nya memang belum pernah ditulis. Ini bukan sekadar tambah endpoint — perlu desain tracking baru dari nol (fill rate, slippage, dst). Sengaja ditunda karena scope-nya besar dan butuh sesi terpisah.

### Gap Frontend (opsional, bisa dikerjakan di Claude Design kapan saja)
- Force Analyze 1 Coin di halaman Diagnosa
- Coin Profiles table di halaman Settings
- Compare & Bookmark di Gate Scanner
- Orderbook panel di Coin Detail

---

## 4. Skenario Baru: Alokasi Saldo Long vs Short (dari pemilik project, belum didesain/diimplementasi)

Deskripsi mekanisme yang diinginkan untuk bot futures (long dan short berjalan berdampingan, bukan saling eksklusif):

**Prinsip dasar:**
- Bot melakukan scan & scoring koin seperti biasa, menghasilkan kandidat dengan potensi naik (untuk **long**) dan kandidat dengan potensi turun (untuk **short**) — keduanya diproses lewat mekanisme leverage yang sudah ada.
- **Long dan short boleh terbuka bersamaan** (tidak masalah kalau ada posisi long dan short aktif di waktu yang sama, untuk koin yang berbeda).

**Logika alokasi saldo saat saldo terbatas (tidak cukup untuk buka semua sinyal valid sekaligus):**
1. Bandingkan skor terbaik dari kandidat long vs skor terbaik dari kandidat short.
2. Jika kandidat **long** punya skor lebih baik → prioritaskan buka long dulu dengan saldo yang ada.
3. Kandidat short yang tadi belum sempat dibeli **tidak hilang begitu saja** — setelah salah satu trade (apapun itu) selesai dan saldo kembali tersedia, sistem **cek ulang** perbandingan skor terkini antara sisa kandidat long vs kandidat short tadi (bukan pakai skor lama, karena kondisi pasar bisa berubah).
4. Kalau ternyata sudah tidak ada kandidat long yang layak entry lagi, baru sistem cek apakah kandidat short sebelumnya **masih bagus** (skor masih valid) — kalau masih bagus, baru dibeli; kalau sudah tidak bagus lagi, ulangi proses scan dari awal.
5. **Aturan tie-breaker**: jika skor kandidat long dan short **sama persis**, **utamakan long** (short dianggap kurang diprioritaskan pada kondisi seri).

**Catatan penting untuk implementasi nanti:**
- Ini butuh **7 titik bias long-only** (bagian 3, poin 1) diperbaiki dulu — tidak masuk akal membandingkan skor "short" secara adil kalau mesin scoring-nya belum benar-benar bisa menilai sinyal bearish.
- Perlu desain ulang bagian alokasi risk/position sizing supaya bisa mempertimbangkan "kandidat yang ditunda", bukan cuma "kandidat yang lolos gate saat itu juga".
- Ini murni deskripsi kebutuhan dari pemilik project — **belum ada analisis teknis, belum ada rencana file/kode yang akan disentuh**. Sesi berikutnya perlu mulai dari menganalisis di mana logika ini paling pas diletakkan (kemungkinan besar di `commander.py` atau modul baru khusus alokasi saldo).

---

## 5. Prinsip Kerja yang Disepakati Sepanjang Sesi Ini (penting untuk sesi baru)

1. **Jangan restart bot tanpa rencana matang** — kumpulkan beberapa fix jadi satu batch, baru restart sekali, bukan restart berkali-kali untuk tiap fix kecil.
2. **Verifikasi dulu sebelum percaya** — kalau ada asumsi/diagnosis, buktikan dengan command nyata (curl, query DB, baca kode) sebelum menulis fix. Sesi ini beberapa kali menemukan diagnosis awal ternyata salah setelah digali lebih dalam — itu wajar dan lebih baik ketahuan sebelum kode ditulis.
3. **Perubahan aditif, bukan modifikasi** — kalau menyentuh file yang dipakai bersama spot & futures, prioritaskan pendekatan yang membuat perilaku lama (spot) tetap 100% sama (parameter opsional dengan default lama, method baru, bukan mengubah logic yang sudah ada).
4. **Testing menyeluruh, bukan sampel** — sebelum anggap fix selesai, uji ke SEMUA kasus yang relevan (bukan cuma beberapa contoh), termasuk uji negatif (pastikan yang seharusnya ditolak memang ditolak).
5. **Jangan restart spot tanpa alasan jelas** — bot spot yang sedang jalan itu prioritas stabilitas; kalau fix hanya menyangkut futures, restart futures saja.

---

*Dokumen ini dibuat otomatis di akhir sesi 13 Juli 2026 untuk keperluan handoff ke sesi kerja berikutnya.*
