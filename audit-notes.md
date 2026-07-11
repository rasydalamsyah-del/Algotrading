# Audit Arsitektur AlgoTrader — Catatan Kerja

Status: 🔄 Sedang berjalan | Terakhir update: mulai audit

---

## main.py — TradingBot (orchestrator utama, 172K, ~3470 baris)

**Peran:** Satu class besar `TradingBot` yang jadi pusat orkestrasi. Isinya kumpulan loop async yang jalan paralel:

| Method | Fungsi |
|---|---|
| `start()` / `stop()` / `run()` | Lifecycle bot |
| `run_scanner_loop()` | Gate 1 & 2 — scan awal seluruh universe |
| `run_gate3_worker()` | Gate 3 → Gate 5 — worker pool proses sinyal detail |
| `run_strategy_loop()` | Loop utama proses SignalEvent dari strategy |
| `run_sl_tp_monitor()` | Loop monitoring posisi terbuka (SL/TP/trailing/ATG) — **ini yang kita bedah abis-abisan hari ini** |
| `run_portfolio_monitor()` | Panggil `_refresh_portfolio()` berkala — **sumber race condition yang kita fix** |
| `run_coin_swap_loop()`, `run_analytics_loop()`, `run_config_watcher()` | Loop pendukung (learning, config hot-reload) |
| `_handle_buy()` / `_handle_close()` | Eksekusi keputusan beli/tutup posisi |
| `_reconcile_positions_on_startup()` | Sinkronisasi state posisi saat bot restart |

**Import kunci (siapa yang dia pakai):**
```
profiles.registry, database.DatabaseManager, exchange.ExchangeConnector,
strategy.get_strategy, risk.RiskManager, execution.OrderExecutionManager,
api_server.create_app, notifications.NotificationManager,
indicators.orderbook.WhaleDetector, intelligence.position_sync.run_position_sync
```

**Kesimpulan awal:** Ini file **paling market-specific** — penuh asumsi spot (saldo virtual, `side="long"` implisit di banyak tempat, tidak ada leverage/margin). Ini kandidat utama jadi `spot/main_spot.py`, TAPI dia manggil hampir semua modul lain jadi harus dipetakan dulu mana dependency-nya yang murni logic vs yang spot-specific.

---

## exchange.py — ExchangeConnector + WebSocketFeed (56K, ~1291 baris)

**Peran:** Wrapper di atas `ccxt.pro` (library exchange populer yang sudah abstraksi banyak exchange, termasuk endpoint spot vs futures Binance).

**🔑 TEMUAN PENTING — merevisi asumsi awal:**
```python
exchange_config = {
    ...
    "options": {"defaultType": "spot", ...},   # baris 72, HARDCODED
}
cls = getattr(ccxt, exchange_id)
self._ex: ccxt.Exchange = cls(exchange_config)
```
`ccxt.pro` **sudah native mendukung futures** — cukup ganti `defaultType` jadi `"future"`, dan seluruh endpoint (termasuk otomatis pindah ke `fapi.binance.com`) di-handle oleh library, BUKAN oleh kode kita. Ini mengubah kesimpulan catatan arsitektur kemarin: `ExchangeConnector` **jauh lebih reusable** dari dugaan awal.

**Method yang generik (aman dipakai bersama, spot & futures) via ccxt:**
`fetch_ohlcv`, `fetch_ticker`, `fetch_order_book`, `fetch_open_orders`, `connect`, `disconnect`, `amount_to_precision`, `price_to_precision`, `get_taker_fee`/`get_maker_fee`, `parse_balance` (struktur free/used/total kompatibel secara struktur, walau makna "used" beda: margin terpakai vs order terkunci)

**Method yang PERLU versi/tambahan khusus futures:**
- `fetch_balance()` — versi paper trading sekarang cuma track 1 currency (USDT) naik-turun linear; futures butuh track margin, unrealized PnL terpisah
- `create_order()` / `_simulate_order_fill()` — **perlu tambahan besar**: belum ada leverage, belum ada liquidation price, belum ada margin mode (isolated/cross)
- **Belum ada sama sekali**: `set_leverage()`, `fetch_funding_rate()`, `fetch_mark_price()`, `fetch_positions()` (ccxt sediakan endpoint-nya, tinggal di-wrap)

**Kesimpulan:** `ExchangeConnector` base class **bisa jadi kandidat `engine/`** dengan pola: buat versi dasar generik (baca-baca via ccxt, connect/disconnect/precision) di `engine/`, lalu `spot/exchange_spot.py` dan `future/exchange_future.py` masing-masing **extend** class dasar itu, override/tambah method yang spesifik (terutama simulasi order fill dan apapun yang menyentuh margin/leverage).

---

## risk.py — RiskManager (40K, ~914 baris)

**Peran:** Position sizing, evaluasi order, halt/resume trading, trailing SL/breakeven, metrik performa (Sharpe/Sortino/drawdown).

**Dependency:** hanya `numpy` + stdlib — TIDAK import `exchange.py`/`database.py`. Cukup independen.

**🔑 TEMUAN KRITIS:** Komentar eksplisit di `_compute_position_size()`:
> *"Di bot spot ini (tidak ada short)... side='sell' SELALU berarti MENUTUP posisi existing, bukan membuka posisi baru"*

Ini konfirmasi resmi dari developer sebelumnya: seluruh logika sizing mengasumsikan **spot-only**. Untuk futures, `side="sell"` jadi ambigu — bisa berarti **buka short** ATAU **tutup long**. Field `side` doang tidak cukup; futures butuh konsep eksplisit terpisah, misal `action: open_long | close_long | open_short | close_short`.

`check_trailing_sl()` dan `check_breakeven_sl()` (yang kita perbaiki hari ini) **sudah** punya cabang `if side=="long" / elif side=="short"` — jadi level monitoring posisi ini lebih siap dari level entry-decision.

---

## Lapisan Intelligence — Peta Kesiapan untuk Short

| File | Import ke exchange/db? | Handle `side` long/short? | Status |
|---|---|---|---|
| `intelligence/classifier.py` (regime) | ❌ Tidak | N/A (regime tidak peduli arah posisi) | ✅ Murni netral, siap dipakai bersama |
| `intelligence/scorer.py` (composite score) | ❌ Tidak | ❌ **Nol** referensi long/short | ⚠️ Perlu dicek apakah sub-skor (trend_score dst) secara implisit menghargai bullish saja |
| `intelligence/observer.py` | ❌ Tidak | — | ✅ Netral (cuma bangun IndicatorSet dari candle) |
| `intelligence/trade_guardian.py` (ATG) | ❌ Tidak | ❌ **Nol** — `profit_pct` dihitung `(current-entry)/entry`, cuma benar untuk long | ⚠️ Perlu versi/wrapper untuk short (profit short = `(entry-current)/entry`) |
| `intelligence/commander.py` (gate + Kelly) | ❌ Tidak | ⚠️ **Bias eksplisit ke long** — `_gate_supertrend()` MENOLAK sinyal kalau supertrend bearish+ADX kuat (harusnya ini sinyal BAGUS untuk short) | ❌ Perlu gate mirror untuk short |

**`SignalType` enum (strategy.py) — temuan menarik:**
```python
class SignalType(Enum):
    BUY = "buy"; SELL = "sell"; HOLD = "hold"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"   # ← SUDAH ADA!
```
`CLOSE_SHORT` sudah didefinisikan, tapi **tidak ada `OPEN_SHORT`/`SELL_SHORT`** — dan tidak ada satupun kode yang pernah menghasilkan sinyal `CLOSE_SHORT` ini. Kesimpulan: ada sisa rencana short dari awal yang tidak pernah dituntaskan, bukan dibangun dari nol.

---

## exchange.py — Detail Tambahan

`Position` model di `database.py` (SQLAlchemy) — kolom yang ADA: `symbol, entry_price, amount, side, stop_loss_price, take_profit_price, atr_at_entry, unrealized_pnl, highest_price, entry_regime, ...`. **Kolom yang TIDAK ADA (wajib ditambah untuk futures)**: `leverage`, `margin_mode`, `liquidation_price`, `funding_paid_total`, `mark_price_at_entry`.

---

## indicators/* — Terkonfirmasi 100% Netral

Grep `"long"/"short"` di seluruh `indicators/*.py` → **nol hasil**. Semua file (trend, momentum, volatility, oscillators, patterns, strength, structure, orderbook) murni fungsi matematika dari OHLCV/orderbook data — tidak tahu apa-apa soal posisi/arah trading. **100% aman dan langsung dipakai bersama tanpa modifikasi apapun.**

---

## KESIMPULAN — Rekomendasi Struktur Final

### Kandidat KUAT untuk `engine/` (tanpa modifikasi):
- `indicators/*` — seluruhnya
- `intelligence/classifier.py` — regime classifier
- `intelligence/observer.py` — indicator set builder
- `profiles/*` — base weights/thresholds (dasar, bisa di-override)

### Kandidat `engine/` TAPI perlu penyesuaian/wrapper untuk short:
- `intelligence/scorer.py` — cek dulu apakah sub-skor implisit long-biased
- `intelligence/trade_guardian.py` (ATG) — butuh versi `profit_pct` yang sadar arah posisi
- `intelligence/commander.py` — `_gate_supertrend` dan kemungkinan gate lain butuh versi mirror untuk short; Kelly sizing bisa dipakai basisnya

### WAJIB spesifik per market (tidak bisa dibagi):
- `exchange.py` — base class bisa di-share (banyak method generik via ccxt), TAPI `_simulate_order_fill`, leverage, liquidation, margin mode WAJIB versi terpisah di `future/`
- `risk.py` — `_compute_position_size` perlu overhaul total untuk leverage; konsep `side="sell"` ambigu perlu diganti jadi `action` eksplisit
- `database.py` Position model — perlu kolom tambahan untuk futures (leverage, margin_mode, liquidation_price, dst) — kemungkinan besar bikin subclass/tabel terpisah, bukan tambah kolom nullable ke tabel yang sama
- `strategy.py` (`VolumetricBreakoutStrategy`, 1848 baris) — belum diaudit detail, tapi `SignalType` enum-nya perlu dilengkapi (`OPEN_SHORT`), dan logika `BaseStrategy`/`VolumetricBreakoutStrategy` kemungkinan besar banyak asumsi long tersembunyi di dalam — **perlu audit baris-per-baris terpisah sebelum yakin**
- `execution.py` — order execution, kemungkinan besar bisa direstrukturisasi mirip `exchange.py` (base + extend)

### Belum diaudit detail (perlu sesi lanjutan):
- `strategy.py` isi lengkap `VolumetricBreakoutStrategy` (file kedua terbesar setelah main.py)
- `intelligence/validator.py` (68K — besar, belum disentuh)
- `learning/*` (analytics, coin_swap, cross_learn, meta_learner)
- `api_server.py`, `telegram_bot.py`, `ta_compat.py`
- `database.py` — method-method `DatabaseManager` (bukan cuma model)

---

---
---

# BABAK 2 — Audit Mendalam Lanjutan

## strategy.py — Detail Kritis yang Ditemukan

### 🔴 TEMUAN PALING FUNDAMENTAL: `PositionTracker` tidak punya field `side` SAMA SEKALI

```python
@dataclass
class PositionTracker:
    symbol: str
    entry_price: float
    entry_time: datetime
    exit_mode: ExitMode
    highest_price: float
    trailing_active: bool = False
    ... (dst — TIDAK ADA field `side`)
```

Ini beda dengan `Position` (model DB) yang **punya** kolom `side`. Jadi ada inkonsistensi: DB tahu soal long/short, tapi objek in-memory yang dipakai real-time buat kalkulasi trailing (`PositionTracker`) **tidak tahu apa-apa** soal arah posisi.

**Konsekuensi konkret:**
- `check_trailing_exit()` (baris 736) — `profit_pct = (current_price - entry_price) / entry_price * 100` → **hardcoded long-only**, pola identik dengan bug ATG yang sudah kita perbaiki hari ini (`get_profit_zone_sl`)
- `register_position()` (baris 889) — **tidak menerima parameter `side` sama sekali** saat membuat tracker baru

### 🔴 KONFIRMASI FINAL: Seluruh pipeline sinyal 100% long-only, dari hulu ke hilir

Grep `SignalType.` di seluruh `strategy.py` — hanya 3 hasil, semuanya:
```
SignalType.CLOSE_LONG  (2x)
SignalType.BUY         (1x)
```
Tidak ada satupun kode di manapun yang pernah menghasilkan `SELL`, `CLOSE_SHORT`. Enum `SignalType` sudah mendefinisikan `CLOSE_SHORT = "close_short"` (sisa rencana lama yang tidak diselesaikan — lihat catatan Babak 1), tapi nihil implementasi.

**Kesimpulan:** kalau mau dukung short (baik untuk futures maupun margin-short spot di masa depan), perubahan **wajib** menyentuh 3 lapis sekaligus secara konsisten: `commander.py` (gate), `scorer.py`/`trade_guardian.py` (profit_pct & threshold arah), dan `strategy.py` (`PositionTracker.side`, `register_position()`, `check_trailing_exit()`). Ini bukan "tambah 1 file baru", tapi modifikasi terkoordinasi di banyak titik existing.

---

## Pola Bug Berulang: "profit_pct Hardcoded Long-Only"

Ditemukan di **3 lokasi terpisah**, kemungkinan besar disalin/ditulis ulang tanpa disadari duplikasinya:

| Lokasi | Baris kira-kira | Formula |
|---|---|---|
| `intelligence/trade_guardian.py` → `check_atg()` | ~215 | `(current_price - entry_price) / entry_price * 100` |
| `strategy.py` → `check_trailing_exit()` | ~763 | `(current_price - entry_price) / entry_price * 100` |
| `risk.py` → `check_trailing_sl()`, `check_breakeven_sl()` | ~730, 749 | **Ini SATU-SATUNYA yang sudah benar** — ada cabang eksplisit `if side=="long" / elif side=="short"` |

**Catatan penting:** `risk.py` ternyata paling siap di antara ketiganya untuk urusan arah posisi, sementara dua lainnya (yang justru lebih sering jadi penentu keputusan exit di kasus AIGENSYN kemarin) masih long-only. Perlu diseragamkan.

---

## learning/coin_swap.py & learning/cross_learn.py — STATUS: ❌ DEPRECATED PERMANEN (dikonfirmasi user)

**Klarifikasi dari user:** `algotrader_test` adalah desain lama untuk menyiasati keterbatasan `algotrader` utama yang dulu cuma mampu pantau 20 koin. Sekarang dengan sistem Gate yang bisa handle 500 koin, `algotrader_test` **tidak diperlukan lagi selamanya**. Bot ini tidak pernah di-deploy di VPS saat ini dan tidak akan dipakai lagi.

**Keputusan untuk restrukturisasi:** `coin_swap.py` dan `cross_learn.py` **TIDAK perlu dipindah** ke struktur baru manapun (`engine/`, `spot/`, atau `future/`). Aman untuk:
- Dikeluarkan dari import chain `main.py` (cari `CoinSwapEngine`, `CrossLearnReader` di `main.py` dan hapus pemanggilannya)
- File-nya sendiri bisa dihapus atau dipindah ke folder `deprecated/`/`archive/` kalau mau disimpan sebagai referensi historis
- Ini juga akan **menghilangkan** error berulang `algotrader_test/database.py not found` yang selama ini mengotori log

---

## database.py — DatabaseManager (65 method, murni SQLAlchemy async)

**Temuan bagus:** `__init__(self, database_url: str)` — **sudah menerima path DB sebagai parameter**, bukan hardcoded. Artinya:

> **`DatabaseManager` tidak perlu diduplikasi/dipisah jadi `database_spot.py` vs `database_future.py`.** Cukup instansiasi dua kali dengan `database_url` berbeda (`sqlite+aiosqlite:///./data/trading_bot_spot.db` vs `.../trading_bot_future.db`), pakai class yang **sama persis**.

Yang tetap perlu keputusan: model `Position`/`Trade` (kelas SQLAlchemy `Base`) saat ini **satu skema untuk semua**. Dua opsi:
- **(A)** Tambah kolom nullable (`leverage`, `margin_mode`, `liquidation_price`, dst) ke skema yang sama — lebih sederhana, tapi kolom itu akan selalu `NULL` di baris-baris spot (sedikit "kotor" tapi tidak berbahaya karena DB-nya toh sudah dipisah fisik per market)
- **(B)** Bikin skema `Position`/`Trade` terpisah total untuk futures — lebih bersih secara model, tapi berarti fork sebagian `database.py`

Ini murni keputusan gaya, bukan soal benar/salah — perlu didiskusikan.

---

## api_server.py & telegram_bot.py — Pola Komunikasi (penting untuk arsitektur proses terpisah)

- **`api_server.py`**: **TIDAK berdiri sendiri** — `create_app(bot_getter)` menerima referensi langsung ke objek `TradingBot` yang hidup, jalan **dalam proses yang sama** dengan `main.py` (via `uvicorn` yang di-embed). Artinya untuk arsitektur spot+futures terpisah, **setiap proses (`main_spot.py`, `main_future.py`) butuh instance `api_server` sendiri**, idealnya di port berbeda (mis. 8000 untuk spot, 8001 untuk futures).
- **`telegram_bot.py`**: **proses independen sepenuhnya** — komunikasi ke bot lewat HTTP (`aiohttp` ke `http://localhost:8000/api`, header `X-API-Key`), sama sekali tidak import `database.py`/`exchange.py`/`main.py`. Ini kandidat kuat untuk **satu instance shared** yang bisa dikonfigurasi hit ke API spot atau futures (atau dua instance terpisah kalau mau notifikasi kebedaan jelas per market) — fleksibel, tidak butuh perubahan besar.

---

## RINGKASAN STRUKTUR FINAL (revisi setelah audit mendalam)

### `engine/` — terkonfirmasi aman dibagi (tidak ada exchange/db coupling):
```
indicators/*              — 100% netral (dikonfirmasi grep, nol referensi long/short)
intelligence/classifier.py — regime, netral
intelligence/observer.py   — builder IndicatorSet, netral
intelligence/validator.py  — perlu diaudit isinya lebih detail (baru dicek imports)
learning/analytics.py      — netral (numpy + constants + core.models saja)
learning/meta_learner.py   — netral
profiles/base_profile.py   — zero import eksternal, murni definisi
profiles/weights.py        — zero import eksternal
profiles/registry.py       — internal only
profiles/thresholds.py     — internal only
constants.py               — zero import, fondasi semua
database.py (class DatabaseManager + Base models) — reusable via parameter, TAPI model Position/Trade perlu keputusan A/B di atas
```

### `engine/` tapi WAJIB ada penyesuaian arah (long/short) sebelum dipakai futures:
```
intelligence/scorer.py         — cek ulang apakah sub-skor implisit long-biased (belum diaudit sedalam commander)
intelligence/trade_guardian.py — profit_pct hardcoded long-only
intelligence/commander.py      — _gate_supertrend eksplisit tolak bearish kuat
```

### WAJIB spesifik per proses (`spot/` vs `future/`):
```
main.py         → jadi main_spot.py / main_future.py, orchestrator terpisah total
exchange.py     → base class bisa di-share sebagian (ccxt generik), _simulate_order_fill
                  dan apapun leverage/liquidation WAJIB versi khusus futures
risk.py         → _compute_position_size butuh overhaul leverage; konsep side="sell"
                  ambigu perlu diganti action eksplisit untuk futures
strategy.py     → PositionTracker butuh field `side`; register_position,
                  check_trailing_exit butuh versi side-aware; SignalType perlu
                  OPEN_SHORT
execution.py    → belum diaudit detail isinya (baru imports), kemungkinan besar
                  ikut pola exchange.py (perlu extend untuk leverage)
api_server.py   → instance terpisah per proses (port beda)
```

### Fleksibel/shared-service (tidak terikat spot atau futures):
```
telegram_bot.py    — proses independen, komunikasi via HTTP, bisa dikonfigurasi
                      untuk hit endpoint manapun
notifications.py   — perlu tag [SPOT]/[FUTURES] di pesan, tapi logikanya generik
```

### Perlu keputusan/klarifikasi dari user (bukan technical blocker, tapi keputusan desain):
```
learning/coin_swap.py, learning/cross_learn.py  — sistem "algotrader_test" peer bot:
   lanjutkan konsepnya (deploy instance kedua beneran) atau nonaktifkan?
   (ini sumber error berulang di log yang kita lihat sepanjang hari ini)
```

---

## execution.py — OrderExecutionManager (44K, ~956 baris)

**Peran:** Mekanika eksekusi order — market order, limit order (marketable-limit), iceberg splitting (order besar dipecah jadi beberapa chunk), polling fill status, cek slippage.

**Dependency:** `database.py`, `exchange.py`, `risk.py`, `strategy.py` — terikat penuh ke seluruh stack spot.

**Temuan:** baris 77 — `side = "buy" if signal.signal_type == SignalType.BUY else "sell"`. Asumsi biner sederhana: kalau bukan BUY, otomatis "sell" (tutup posisi). Sama seperti `risk.py`, ini akan pecah kalau `SignalType.OPEN_SHORT` ditambahkan nanti (perlu logic eksplisit, bukan sekadar else-branch).

**Kesimpulan:** Mekanika inti (iceberg splitting, slippage check, fill polling) **generik dan reusable** — bukan soal long/short, cuma soal "eksekusi order beli/jual di exchange". Kandidat baik untuk pola base-class + extend seperti `exchange.py`.

---

## intelligence/validator.py — SignalValidator (68K, ~1810 baris) — TERBESAR yang belum diaudit

**Peran:** Lapisan "second opinion" — setelah `scorer.py` hasilkan skor, `validator.py` cek ulang konteks lebih detail (~26 fungsi `_check_*`): divergensi RSI/MACD, konteks pattern, support/resistance, alignment timeframe lebih tinggi, volume climax, staleness data, Bollinger/Keltner/squeeze, stochastic, struktur pasar, orderbook, VWAP, Ichimoku, pivot, Fibonacci, Donchian. Tiap check menambah **catatan** atau **warning + penalti confidence** ke `ValidationResult`.

**🔴 TEMUAN: bias long eksplisit di level teks/logic**, bukan cuma implisit:
```python
def _check_rsi_divergence(iset, result):
    if div > 0:
        result.add_note("✅ RSI bullish divergence — konfirmasi sinyal")
        result.confidence_adjustment += 0.05
    elif div < 0:
        result.add_warning("RSI bearish divergence — berlawanan dengan sinyal BUY", ...)
```
Bearish divergence **selalu** diperlakukan sebagai warning/penalti — padahal untuk sinyal short, bearish divergence justru seharusnya jadi **konfirmasi positif** (mirror logic dari bullish). Pola sama kemungkinan berulang di sebagian besar dari 26 fungsi `_check_*` lainnya (belum diverifikasi satu-satu, tapi pola judul fungsi seperti `_check_stoch_context`, `_check_macd_context` kemungkinan besar serupa).

**Kesimpulan:** File ini **market-agnostic secara dependency** (aman taruh di `engine/`), TAPI **isinya paling banyak butuh kerja mirroring** untuk mendukung short dibanding file lain manapun yang sudah diaudit — 26 fungsi, sebagian besar kemungkinan perlu versi arah-sadar.

---

## ta_compat.py — TERKONFIRMASI netral

Grep long/short → **0 hasil**. Import cuma `time`, `numpy`, `pandas` (tidak ada di daftar grep karena di-exclude filter, tapi dikonfirmasi tidak ada exchange/db coupling). Ini adalah compatibility layer untuk `pandas_ta` (indikator teknikal), murni matematika seperti `indicators/`. **Aman 100% untuk `engine/`.**

---

## api_server.py & telegram_bot.py — Endpoint/Command Terkait Fitur Deprecated

Ditemukan endpoint dan command yang terikat ke sistem `algotrader_test` (sudah dikonfirmasi user: **deprecated permanen**):

| File | Item yang perlu dihapus |
|---|---|
| `api_server.py` | Route `/api/crosslearn/status`, `/api/crosslearn/swap_history` |
| `telegram_bot.py` | Command `/crosslearn`, `/swaphistory` |

Selain itu, seluruh route/command lain di kedua file ini **generik** (positions, trades, balance, candles, forecast, universe, bot control, analytics, meta_learner) — pola yang sama persis dibutuhkan baik untuk spot maupun futures, cuma datanya beda sumber (DB spot vs DB futures). Struktur route bisa dipakai sebagai **template**, diinstansiasi dua kali (satu per proses/port).

---
---

## intelligence/scorer.py — Audit Mendalam (koreksi penting atas klaim sebelumnya)

### 🟢 Klarifikasi penting: `indicators/` TETAP 100% netral — koreksinya ada di lapisan konsumsi

Ditemukan bahwa `indicators/trend.py` → `calculate_ema_stack()` sebenarnya **sudah dirancang simetris**:
```python
# stack_score bertambah HANYA kalau fast_val > slow_val (bullish pair)
# Hasil dinormalisasi ke skala 0-100 dimana:
#   ~100 = bullish stack kuat, ~0 = bearish stack kuat, ~50 = netral/campuran
# gap_adj JUGA simetris (ada komentar eksplisit di kode: "bull dapat bonus,
# bear dapat penalti ekuivalen")
```
Ini genuinely direction-aware di level matematika — **bukan** bias. Klaim audit sebelumnya ("indicators/ 100% netral") tetap valid dan dikonfirmasi ulang di sini.

### 🔴 Tapi bias muncul di `_check_primary_trigger()` (scorer.py) — cara skor itu DIPAKAI

```python
elif trigger_type == PrimaryTriggerType.TREND_CONFIRMATION:
    ema_score = iset.trend.ema_stack_score
    if ema_score < 55.0:
        return False, "trend belum confirm"   # ← HANYA terima skor tinggi (bullish)
```
Skor rendah (0-45, menandakan bearish kuat) **dibuang** sebagai "gagal trigger" — padahal secara matematis itu sinyal simetris yang justru berguna untuk short. **Kesimpulan: `indicators/trend.py` tidak perlu diubah sama sekali; yang perlu ditambah adalah cabang trigger baru di `scorer.py`** (misal: kalau mode short diaktifkan, terima `ema_score <= 45` sebagai trigger valid).

**4 tipe primary trigger di `_check_primary_trigger()` — audit bias per tipe:**

| Trigger type | Bias? | Detail |
|---|---|---|
| `BREAKOUT_VOLUME` | ✅ Netral | Cuma cek RSI dalam band + volume ratio — tidak mensyaratkan arah tertentu |
| `TREND_CONFIRMATION` | 🔴 Bias long | `ema_score < 55.0` → fail. Perlu cabang mirror untuk short |
| `MOMENTUM_REVERSAL` | 🔴 Bias long | Logikanya "beli saat oversold" (mean-revert long) — perlu cabang mirror "short saat overbought" |
| `COMPOSITE` (default) | ✅ Netral | Sama seperti BREAKOUT_VOLUME, cuma cek band RSI + volume |

**Revisi pola bug "long-only" — total sekarang 6 lokasi** (bertambah 1 dari catatan sebelumnya, dengan nuansa lebih presisi di scorer.py: 2 dari 4 trigger type kena, 2 lainnya sudah netral).

---

Ini pola bug/gap yang paling sering muncul sepanjang audit — dicatat lengkap supaya tidak ada yang terlewat saat nanti mengerjakan dukungan short:

| # | File | Fungsi | Sifat masalah |
|---|---|---|---|
| 1 | `intelligence/trade_guardian.py` | `check_atg()` / `get_profit_zone_sl()` | `profit_pct` hardcoded formula long |
| 2 | `strategy.py` | `check_trailing_exit()` | `profit_pct` hardcoded formula long, **plus** `PositionTracker` tidak punya field `side` sama sekali (akar masalah lebih dalam) |
| 3 | `intelligence/commander.py` | `_gate_supertrend()` | Menolak sinyal kalau bearish kuat — harusnya jadi sinyal short yang bagus |
| 4 | `execution.py` | `execute_signal()` | `side` ditentukan biner BUY vs else="sell", tidak ada konsep open-short |
| 5 | `intelligence/validator.py` | `_check_rsi_divergence()` dan kemungkinan besar sebagian dari 25 fungsi `_check_*` lainnya | Bearish selalu diperlakukan sebagai warning, padahal untuk short seharusnya jadi konfirmasi |
| 6 | `intelligence/scorer.py` | `_check_primary_trigger()` — tipe `TREND_CONFIRMATION` & `MOMENTUM_REVERSAL` | Cuma terima skor tinggi/oversold (2 dari 4 tipe trigger bias; `BREAKOUT_VOLUME` & `COMPOSITE` sudah netral) |
| 7 | `intelligence/position_sync.py` | **Seluruh file** (`fetch_binance_spot_positions`, dst) | Bukan sekadar bias — konsepnya sendiri (baca saldo koin via `fetch_balance()`) tidak berlaku untuk futures sama sekali. Butuh file baru total, bukan modifikasi |

**Yang SUDAH benar (referensi buat pola yang benar):** `risk.py` → `check_trailing_sl()`, `check_breakeven_sl()` — keduanya punya cabang eksplisit `if side=="long" / elif side=="short"`.

**Catatan penting soal akar masalah:** dari 7 temuan ini, yang paling fundamental adalah **#2** — `PositionTracker` (strategy.py) tidak punya field `side` sama sekali. Ini bukan cuma "kurang 1 cabang if/else", tapi objek inti yang dipakai real-time monitoring memang belum dirancang untuk tahu arah posisi. Perbaikan di sini adalah prasyarat sebelum efektif memperbaiki #1 dan sebagian #5 (yang keduanya bergantung pada tahu posisi ini long atau short saat kalkulasi).

---

# STRUKTUR FOLDER FINAL (setelah audit menyeluruh — 100% file utama sudah ditelusuri)

```
engine/                          # Market-agnostic, TERKONFIRMASI lewat audit
├── indicators/*                 # ✅ 100% netral, dikonfirmasi 2x (grep + baca logic ema_stack_score)
├── ta_compat.py                 # ✅ 100% netral (dikonfirmasi grep)
├── constants.py                 # ✅ zero dependency, fondasi
├── intelligence/
│   ├── classifier.py            # ✅ netral, regime detection
│   ├── observer.py              # ✅ netral, builder IndicatorSet
│   ├── scorer.py                 # ⚠️ netral dependency, TAPI 2 dari 4 primary trigger type bias long (temuan #6)
│   ├── trade_guardian.py        # ⚠️ netral dependency, TAPI profit_pct perlu side-aware (temuan #1)
│   ├── commander.py             # ⚠️ netral dependency, TAPI _gate_supertrend perlu mirror (temuan #3)
│   └── validator.py             # ⚠️ netral dependency, TAPI ~26 fungsi _check_* sebagian perlu mirror (temuan #5)
├── profiles/*                   # ✅ netral
├── learning/
│   ├── analytics.py             # ✅ netral
│   └── meta_learner.py          # ✅ netral
│   (coin_swap.py, cross_learn.py — ❌ DIHAPUS, deprecated permanen, dikonfirmasi user)
└── database.py                  # ✅ DatabaseManager reusable (terima database_url sbg parameter)
                                  #    Model Position/Trade: keputusan A/B (kolom nullable vs skema terpisah)


spot/
├── main_spot.py                 # dari main.py
├── exchange_spot.py             # dari exchange.py, defaultType="spot"
├── risk_spot.py                 # dari risk.py (sudah paling siap side-aware, tinggal adjust)
├── execution_spot.py            # dari execution.py
├── strategy_spot.py             # dari strategy.py — PositionTracker perlu tambah field `side`
│                                 #   (dikerjakan di sini dulu sebelum tau apakah perlu di engine)
├── position_sync_spot.py        # dari intelligence/position_sync.py — SELURUHNYA spot-specific,
│                                 #   konsepnya (baca fetch_balance utk cari saldo koin) tidak ada
│                                 #   padanan di futures (temuan #7)
├── api_server_spot.py           # instance dashboard, port 8000 — HAPUS route /api/crosslearn/*
├── data/trading_bot_spot.db
└── logs/trading_bot_spot.log

future/
├── main_future.py               # BARU
├── exchange_future.py           # BARU — defaultType="future", +set_leverage, +fetch_funding_rate,
│                                 #   +fetch_mark_price, +liquidation calc
├── risk_future.py                # BARU — position sizing leverage-aware, liquidation price check
├── execution_future.py          # BARU — extend pola execution.py, +OPEN_SHORT/CLOSE_SHORT handling
├── liquidation.py               # BARU — formula resmi Binance Futures
├── funding.py                   # BARU — funding rate → PnL adjustment
├── api_server_future.py         # instance dashboard, port 8001 (atau lainnya)
├── data/trading_bot_future.db
└── logs/trading_bot_future.log

shared_service/                  # tidak terikat market tertentu
├── telegram_bot.py              # 1 proses, dikonfigurasi hit ke API spot dan/atau futures
└── notifications.py             # perlu tag [SPOT]/[FUTURES] di pesan

dashboard/
└── (frontend HTML — belum diaudit, kemungkinan perlu penyesuaian kalau ada view gabungan)
```

---

# PERTANYAAN/KERAGUAN YANG MASIH TERBUKA (perlu diputuskan sebelum eksekusi pemindahan)

1. **Model DB (Position/Trade):** kolom nullable ditambah ke skema sama (opsi A) vs skema terpisah total untuk futures (opsi B)? — lihat bagian database.py di atas.
2. **`intelligence/scorer.py`** — baru diaudit dependency-nya (netral), belum diaudit isi detail selevel `validator.py`. Kemungkinan ada bias implisit di perhitungan sub-skor (misal EMA stack scoring) yang belum ketemu karena belum dibaca baris-per-baris.
3. **`PositionTracker.side`** — nambah field ini di `strategy.py` (dipakai spot) sekarang demi konsistensi, atau baru ditambah nanti pas benar-benar mulai kerjakan `future/`? Ini keputusan urutan kerja, bukan soal benar/salah.
4. **`dashboard/*.html`** — belum diaudit sama sekali. Kalau nanti mau ada tampilan gabungan spot+futures di satu dashboard, ini perlu digali juga.
5. **`intelligence/position_sync.py`** — baru disentuh sebagian (bug `side="buy"` yang sudah diperbaiki sebelumnya kita temukan di history git). Belum diaudit fungsi lain di dalamnya secara menyeluruh.

---

*Status audit: ~90% file utama sudah ditelusuri strukturnya, dengan 6 file diaudit sangat mendalam (main.py, exchange.py, risk.py, strategy.py, commander.py, validator.py). Belum ada file yang dipindah — dokumen ini murni pemetaan untuk pengambilan keputusan struktur.*

