# Status Audit Lengkap Tier 1 → 7 — AlgoTrader

_Dokumen ini digabungkan dari 2 tabel ringkasan (jumlah fungsi per file +
status verifikasi) yang diminta user disimpan ke repo. Terakhir diupdate:
2026-07-09, setelah re-verifikasi penuh Tier 1._

## Legenda
- **Dibaca Tuntas**: file dibaca baris-per-baris, checklist manual diterapkan
  (None-safety, caller/callee, dsb).
- **Diverifikasi Eksperimen**: jumlah fungsi yang BENAR-BENAR diuji dengan
  skrip eksperimen tertulis independen (level bukti paling kuat — ini yang
  berhasil menemukan bug seperti deadlock `strategy.py` atau selisih ADX
  218% di `ta_compat.py`, yang TIDAK akan ketemu hanya dengan membaca).
- ✅ FIXED = bug ditemukan & diperbaiki | ✅ CLEAN = diverifikasi, tidak ada
  bug | ⚪ SKIP = tidak relevan (non-matematis) | ❌ PENDING = belum dikerjakan.

## Tabel 1 — Jumlah Fungsi & Kedalaman Verifikasi

| Tier | File | Total Fungsi | Dibaca Tuntas | Diverifikasi Eksperimen |
|---|---|---|---|---|
| 1 | execution.py | 12 | 12 | 6 |
| 1 | risk.py | 38 | 38 | 5 |
| 1 | exchange.py | 50 | 50 | 6 |
| 2 | ta_compat.py | 60 | ~35 | ~10 (RSI/MACD/ADX/ATR/BB/Supertrend/StochRSI) |
| 2 | indicators/trend.py | 8 | 8 | ~6 |
| 2 | indicators/momentum.py | 18 | 18 | ~5 |
| 2 | indicators/oscillators.py | 15 | 15 | ~7 |
| 2 | indicators/volatility.py | 16 | 16 | ~5 |
| 2 | indicators/strength.py | 18 | 18 | ~4 |
| 2 | indicators/structure.py | 21 | 21 | ~9 |
| 2 | indicators/patterns.py | 16 | 16 | ~4 |
| 2 | indicators/orderbook.py | 23 | 23 | ~5 |
| 3 | intelligence/scorer.py | 12 | 12 | ~2 |
| 3 | intelligence/classifier.py | 19 | 19 | ~10 |
| 3 | intelligence/validator.py | 36 | 36 | ~4 |
| 3 | intelligence/observer.py | 12 | 12 | ~4 |
| 3 | intelligence/trade_guardian.py | 6 | 6 | ~6 |
| 3 | intelligence/position_sync.py | 5 | 5 | ~2 |
| 3 | strategy.py | 43 | 43 | ~10 |
| 3 | intelligence/commander.py | 27 | 27 | ~6 |
| 4 | profiles/base_profile.py | 12 | 12 | ~3 |
| 4 | profiles/weights.py | 8 | 8 | ~2 |
| 4 | profiles/thresholds.py | 12 | 12 | ~4 |
| 4 | profiles/registry.py | 21 | 21 | ~4 |
| 5 | learning/coin_swap.py | 13 | 13 | ~2 |
| 5 | learning/cross_learn.py | 15 | 15 | ~1 |
| 5 | learning/analytics.py | 37 | 37 | ~5 |
| 5 | learning/meta_learner.py | 38 | 38 | ~3 |
| 6 | database.py | 66 | ❌ 0 | ❌ 0 |
| 6 | core/models.py | 43 | ❌ 0 | ❌ 0 |
| 6 | notifications.py | 31 | ❌ 0 | ❌ 0 |
| 6 | api_server.py | 70 | ❌ 0 | ❌ 0 |
| 6 | telegram_bot.py | 55 | ❌ 0 | ❌ 0 |
| 6 | smoke_api.py | 1 | ❌ 0 | ❌ 0 |
| 7 | main.py | 42 | ~15 (cuma bagian wiring yg disentuh) | ~15 |

**Total fungsi proyek: ~1010 | Dibaca tuntas: ~577 (~57%) | Diverifikasi eksperimen: ~148 (~15%)**

## Tabel 2 — Status per Kategori Checklist

| Tier | File | Caller/Callee | Matematika | Edge-case/Konkurensi | Status |
|---|---|---|---|---|---|
| 1 | execution.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai — re-verifikasi 2026-07-09 |
| 1 | risk.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai — re-verifikasi 2026-07-09 |
| 1 | exchange.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai — re-verifikasi 2026-07-09 |
| 2 | ta_compat.py | ✅ CLEAN | ✅ **FIXED** (ADX/ATR dashboard salah 218%) | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/trend.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/momentum.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/oscillators.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/volatility.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/strength.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai — putaran ulang |
| 2 | indicators/structure.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai — full re-audit |
| 2 | indicators/patterns.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai — full re-audit |
| 2 | indicators/orderbook.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED | Selesai — full re-audit |
| 3 | intelligence/scorer.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai |
| 3 | intelligence/classifier.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED | Selesai |
| 3 | intelligence/validator.py | ✅ FIXED | ⚪ SKIP (non-math) | ✅ CLEAN | Selesai |
| 3 | intelligence/observer.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED | Selesai |
| 3 | intelligence/trade_guardian.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai |
| 3 | intelligence/position_sync.py | ✅ FIXED | ⚪ SKIP (non-math) | ✅ CLEAN | Selesai |
| 3 | strategy.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED | Selesai (2 putaran, termasuk deadlock kritis) |
| 3 | intelligence/commander.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai |
| 4 | profiles/base_profile.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai |
| 4 | profiles/weights.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai |
| 4 | profiles/thresholds.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai |
| 4 | profiles/registry.py | ✅ FIXED | ⚪ SKIP (non-math) | ✅ CLEAN | Selesai |
| 5 | learning/coin_swap.py | ✅ FIXED | ✅ CLEAN | ✅ CLEAN | Selesai |
| 5 | learning/cross_learn.py | ✅ CLEAN | ✅ CLEAN | ✅ CLEAN | Selesai |
| 5 | learning/analytics.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED | Selesai |
| 5 | learning/meta_learner.py | ✅ FIXED | ✅ CLEAN | ✅ FIXED (arsitektur) | Selesai |
| 6 | database.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 6 | core/models.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 6 | notifications.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 6 | api_server.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 6 | telegram_bot.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 6 | smoke_api.py | ❌ PENDING | ❌ PENDING | ❌ PENDING | Belum disentuh |
| 7 | main.py | ⚠️ SEBAGIAN (wiring saja) | ⚠️ SEBAGIAN | ⚠️ SEBAGIAN | Belum full audit |

## Temuan Paling Kritis Sepanjang Audit Ini

1. **DEADLOCK STARTUP TOTAL** (`strategy.py`) — bot bisa hang selamanya saat
   restart dengan posisi terbuka. Fixed: `threading.Lock` → `threading.RLock`.
2. **ADX/ATR dashboard salah sampai 218%** (`ta_compat.py`) — `_wilder_smooth`
   pakai metode ewm lama yang sudah terbukti salah untuk RSI, tapi belum
   diperbaiki untuk ADX/ATR. Tidak memengaruhi trading nyata, hanya dashboard
   diagnostic `/api/diagnosa`.
3. **Sinyal `position_sync.py` tidak pernah berhasil sejak awal** — salah
   panggil `observe()` (signature mismatch total + await pada fungsi sync).
4. **`signal_scores` tidak pernah tersimpan di jalur produksi** (`scorer.py`)
   — `asyncio.get_event_loop()` gagal di worker thread.
5. **Autonomous weight adjustment tidak live-effect** (`meta_learner.py`) —
   menulis ke file tapi tidak update dict in-memory, perlu restart utk aktif.
6. Beberapa race condition (classifier.py `_REGIME_BUFFERS`, observer.py
   `_OBSERVATION_CACHE`, orderbook.py `_state_registry`) — diperbaiki dengan
   `threading.Lock`/`RLock`.

## Sisa Pekerjaan

- **Tier 6** (266 fungsi): database.py, core/models.py, notifications.py,
  api_server.py, telegram_bot.py, smoke_api.py — belum tersentuh sama sekali.
- **Tier 7** (main.py, 42 fungsi): baru disentuh sebagian (wiring fixes),
  belum full audit.
