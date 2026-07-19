"""
intelligence/position_sync.py
AlgoTrader Pro v7.0 — Binance Position Sync & Guardian
Fungsi: Deteksi posisi aktif di Binance yang tidak ada di DB bot,
        analisis Gate3-5, lalu adopt & kawal otomatis.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("intelligence.position_sync")

# ─── Threshold minimum untuk adopt posisi ────────────────────────────────────
MIN_USDT_VALUE     = 1.0    # Abaikan dust < $1
MIN_ADOPT_SCORE    = 45.0   # Score minimum agar posisi layak dikawal
MIN_CANDLE_BARS    = 50     # Minimum candle untuk analisis
# [#36 -- audit fungsional] Toleransi deteksi amount mismatch -- reuse
# ambang PERSIS sama dgn _reconcile_positions_on_startup() (main_spot.py,
# 5% relatif) supaya konsisten satu ambang di seluruh repo. Floor absolut
# (MIN_USDT_VALUE) dipakai bersamaan -- lihat future/position_sync_futures.py
# utk latar belakang lengkap (pola identik).
AMOUNT_MISMATCH_TOLERANCE_PCT = 5.0

async def fetch_binance_spot_positions(exchange) -> List[Dict]:
    """
    Fetch semua coin yang dipegang di Binance (balance > 0, bukan USDT).
    Return list of {symbol, amount, approx_usdt_value}

    [ITEM #4 -- audit fungsional] Sebelumnya fungsi ini menelan SEMUA
    exception dari fetch_balance() dan mengembalikan [] -- SAMA PERSIS
    dengan hasil "genuinely tidak ada posisi". Aman untuk arah untracked
    (fail-safe), TAPI berbahaya untuk deteksi phantom (arah baru, lihat
    find_untracked_positions()) -- [] yang ambigu itu akan membuat SEMUA
    posisi DB tampak "phantom" serentak dari satu hiccup API. Sekarang
    exception dari fetch_balance() di-RAISE apa adanya -- pemanggil
    (find_untracked_positions(), satu-satunya caller di repo ini,
    diverifikasi via grep) yang memutuskan cara menangani kegagalan fetch
    berbeda per arah perbandingan. Kegagalan fetch_ticker() PER-COIN di
    bawah TETAP ditelan (try/except lokal tidak diubah) -- itu cuma
    estimasi nilai USDT, bukan sinyal "posisi ada/tidak ada".
    """
    balance = await exchange.fetch_balance()
    total   = balance.get("total", {})
    results = []

    for coin, amount in total.items():
        if coin in ("USDT", "BUSD", "USDC", "TUSD", "DAI"):
            continue
        if not isinstance(amount, (int, float)) or amount <= 0:
            continue

        symbol = f"{coin}/USDT"
        # Estimasi nilai USDT
        # [BUG-FIX] Sebelumnya: exchange._ex.fetch_ticker(symbol) — akses
        # langsung ke raw ccxt object (private attribute _ex), bypass
        # wrapper publik ExchangeConnector.fetch_ticker() yang menyediakan
        # throttling, retry/backoff, dan latency logging. Ini satu-satunya
        # tempat di seluruh repo yang bypass wrapper (dicek via grep).
        # Sekarang: pakai exchange.fetch_ticker(symbol) — return format
        # sama persis (ccxt ticker dict), tapi dapat proteksi rate-limit.
        try:
            ticker = await exchange.fetch_ticker(symbol)
            price  = ticker.get("last") or ticker.get("close") or 0.0
            usdt_value = amount * price
        except Exception:
            usdt_value = 0.0
            price      = 0.0

        if usdt_value < MIN_USDT_VALUE:
            continue

        results.append({
            "symbol":      symbol,
            "coin":        coin,
            "amount":      amount,
            "price":       price,
            "usdt_value":  usdt_value,
        })

    log.info("Binance spot: %d posisi aktif ditemukan", len(results))
    return results


async def find_untracked_positions(exchange, db_manager) -> Dict:
    """
    Bandingkan posisi Binance vs DB bot -- DUA ARAH.

    [ITEM #4 -- audit fungsional] Sebelumnya fungsi ini HANYA mendeteksi
    1 dari 3 kemungkinan mismatch: posisi ADA di Binance, TIDAK ADA di DB
    ("untracked"). Sekarang JUGA mendeteksi arah sebaliknya: posisi ADA di
    DB (is_open=True), TIDAK ADA LAGI di Binance ("phantom candidate") --
    root cause konkret di spot: _paper_balance berkurang SEBELUM db.close_
    position() (lihat _do_close_position() di main_spot.py), kalau db.close_
    position() gagal setelahnya, DB nyangkut is_open=True permanen tanpa
    ada yang pernah tahu.

    [#36 -- audit fungsional] Mismatch tipe ke-3 (posisi ADA di KEDUA sisi
    tapi `amount` beda > AMOUNT_MISMATCH_TOLERANCE_PCT & > MIN_USDT_VALUE)
    SEKARANG JUGA dideteksi (sebelumnya "dicatat sbg item backlog terpisah"
    -- item ini). Spot TIDAK punya partial-close (dikonfirmasi tidak ada
    pemanggilan reduce_position_amount() di main_spot.py) -- penyebab
    plausible di sini beda dari futures: fee/dust deduction (Binance
    kadang potong fee dari coin yang sama) atau aktivitas manual eksternal
    di akun, BUKAN kegagalan retry partial-close (itu murni futures/#28).
    Symbol dgn is_closing=True DIKECUALIKAN (alasan sama dgn
    phantom_candidates).

    Return dict: lihat docstring future/position_sync_futures.py::
    find_untracked_positions() (struktur & semantik IDENTIK -- untracked,
    phantom_candidates, amount_mismatches, fetch_failed).
    """
    try:
        binance_positions = await fetch_binance_spot_positions(exchange)
    except Exception as e:
        log.error(
            "Fetch posisi Binance spot gagal -- skip perbandingan phantom "
            "siklus ini (arah untracked tetap fail-safe kosong, coba lagi "
            "siklus berikutnya): %s", e,
        )
        return {"untracked": [], "phantom_candidates": [], "amount_mismatches": [], "fetch_failed": True}

    # Ambil posisi terbuka di DB
    db_open = await db_manager.get_open_positions()
    db_symbols       = {p.symbol for p in db_open}
    db_by_symbol     = {p.symbol: p for p in db_open}
    exchange_by_symbol = {pos["symbol"]: pos for pos in binance_positions}
    exchange_symbols = set(exchange_by_symbol.keys())

    untracked = []
    for pos in binance_positions:
        if pos["symbol"] not in db_symbols:
            untracked.append(pos)
            log.warning(
                "⚠️  Posisi tidak tertracking: %s | amount=%.4f | ~$%.2f USDT",
                pos["symbol"], pos["amount"], pos["usdt_value"],
            )

    phantom_candidates = [
        p.symbol for p in db_open
        if p.symbol not in exchange_symbols and not p.is_closing
    ]

    # [#36] Amount mismatch -- symbol ADA di kedua sisi, is_closing=False,
    # tapi amount beda signifikan.
    amount_mismatches = []
    for symbol in (db_symbols & exchange_symbols):
        db_pos = db_by_symbol[symbol]
        if db_pos.is_closing:
            continue
        db_amount = float(db_pos.amount or 0)
        ex_amount = float(exchange_by_symbol[symbol]["amount"])
        if db_amount <= 0:
            continue
        diff_pct = abs(db_amount - ex_amount) / db_amount * 100.0
        if diff_pct <= AMOUNT_MISMATCH_TOLERANCE_PCT:
            continue
        price = float(exchange_by_symbol[symbol].get("price") or 0)
        diff_usdt = abs(db_amount - ex_amount) * price
        if price > 0 and diff_usdt < MIN_USDT_VALUE:
            continue
        amount_mismatches.append({
            "symbol": symbol, "db_amount": db_amount,
            "exchange_amount": ex_amount, "diff_pct": diff_pct,
        })
        log.warning(
            "⚠️  Amount mismatch spot: %s | DB=%.8f vs exchange=%.8f (%.1f%%)",
            symbol, db_amount, ex_amount, diff_pct,
        )

    return {
        "untracked": untracked,
        "phantom_candidates": phantom_candidates,
        "amount_mismatches": amount_mismatches,
        "fetch_failed": False,
    }


async def _process_phantom_candidates(
    candidates: List[str],
    phantom_suspects: Dict[str, int],
    db_manager,
    notifier,
    result: Dict,
) -> None:
    """[ITEM #4] Mirror persis future/position_sync_futures.py::
    _process_phantom_candidates() -- lihat docstring di sana untuk latar
    belakang lengkap (debounce 2 siklus, filter is_closing di caller,
    TIDAK PERNAH auto-close, reuse channel notifikasi yang sudah konvensi)."""
    candidate_set = set(candidates)
    for sym in list(phantom_suspects.keys()):
        if sym not in candidate_set:
            del phantom_suspects[sym]

    for symbol in candidates:
        phantom_suspects[symbol] = phantom_suspects.get(symbol, 0) + 1
        count = phantom_suspects[symbol]

        if count < 2:
            log.warning(
                "Kandidat phantom position (spot): %s TIDAK ada di Binance "
                "tapi is_open=True di DB (siklus ke-%d/2 -- belum dikonfirmasi).",
                symbol, count,
            )
            continue

        result["phantom_confirmed"] = result.get("phantom_confirmed", 0) + 1
        msg = (
            f"PHANTOM POSITION terdeteksi (spot): {symbol} — is_open=True di "
            f"DB TAPI tidak ada di Binance selama >= 2 siklus sync berturut-turut. "
            f"Kemungkinan db.close_position() gagal setelah order close sukses "
            f"tereksekusi (lihat _do_close_position()). TIDAK di-auto-close -- "
            f"perlu review manual: cek riwayat order {symbol} di Binance, lalu "
            f"close manual di DB kalau genuinely sudah tidak ada posisi."
        )
        log.critical(msg)
        try:
            await db_manager.save_log("CRITICAL", "position_sync_spot", msg)
        except Exception as e:
            log.error("save_log phantom position gagal: %s", e)
        if notifier is not None:
            try:
                await notifier.notify_error("phantom_position_spot", msg)
            except Exception as e:
                log.error("notify_error phantom position gagal: %s", e)


async def _process_amount_mismatch_candidates(
    candidates: List[Dict],
    mismatch_suspects: Dict[str, int],
    db_manager,
    notifier,
    result: Dict,
) -> None:
    """
    [#36 -- audit fungsional] Mirror persis future/position_sync_futures.py::
    _process_amount_mismatch_candidates() -- debounce 2 siklus, TIDAK
    PERNAH auto-correct DB (notify-only), counter TERPISAH dari
    phantom_suspects. Lihat docstring di sana utk latar belakang lengkap.
    """
    candidate_symbols = {c["symbol"] for c in candidates}
    for sym in list(mismatch_suspects.keys()):
        if sym not in candidate_symbols:
            del mismatch_suspects[sym]

    for c in candidates:
        symbol = c["symbol"]
        mismatch_suspects[symbol] = mismatch_suspects.get(symbol, 0) + 1
        count = mismatch_suspects[symbol]

        if count < 2:
            log.warning(
                "Kandidat amount mismatch (spot): %s DB=%.8f vs exchange=%.8f "
                "(%.1f%%) (siklus ke-%d/2 -- belum dikonfirmasi).",
                symbol, c["db_amount"], c["exchange_amount"], c["diff_pct"], count,
            )
            continue

        result["amount_mismatch_confirmed"] = result.get("amount_mismatch_confirmed", 0) + 1
        msg = (
            f"AMOUNT MISMATCH terdeteksi (spot): {symbol} — DB amount="
            f"{c['db_amount']:.8f} vs exchange amount={c['exchange_amount']:.8f} "
            f"({c['diff_pct']:.1f}%) selama >= 2 siklus sync berturut-turut. "
            f"Kemungkinan fee/dust deduction atau aktivitas manual eksternal "
            f"di akun. TIDAK di-auto-correct -- perlu review manual: cek "
            f"riwayat transaksi {symbol} di Binance, lalu koreksi manual "
            f"amount di DB kalau genuinely sudah berbeda."
        )
        log.critical(msg)
        try:
            await db_manager.save_log("CRITICAL", "position_sync_spot", msg)
        except Exception as e:
            log.error("save_log amount mismatch gagal: %s", e)
        if notifier is not None:
            try:
                await notifier.notify_error("amount_mismatch_spot", msg)
            except Exception as e:
                log.error("notify_error amount mismatch gagal: %s", e)


async def analyze_position(
    symbol:   str,
    amount:   float,
    price:    float,
    exchange,
    db_manager,
) -> Tuple[bool, float, Optional[float], Optional[float], str, str, str]:
    """
    Analisis Gate3-5 untuk posisi yang sudah terbeli.
    Return: (layak_dikawal, score, sl, tp, alasan, regime_value, profile_name)

    # [BUG-FIX] Sebelumnya return tuple tidak menyertakan regime dan
    # profile_name hasil analisis nyata — caller (run_position_sync) terpaksa
    # hardcode regime="undefined" saat memanggil adopt_position, dan
    # adopt_position sendiri hardcode strategy_profile="scalp_volatile"
    # untuk SEMUA posisi apapun hasil klasifikasi sebenarnya. Sekarang
    # regime_value dan profile_name asli dikembalikan agar tersimpan benar
    # di kolom entry_regime & strategy_profile saat posisi diadopsi.
    """
    from engine.intelligence.observer  import observe
    from engine.intelligence.scorer    import score_signal
    from engine.intelligence.classifier import classify_regime
    from engine.profiles.registry      import get_coin_profile

    profile_name = "unknown"
    try:
        # ── Gate 3: Fetch OHLCV & hitung indikator ──
        bars = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=200)
        if not bars or len(bars) < MIN_CANDLE_BARS:
            return (False, 0.0, None, None,
                    f"Data candle tidak cukup ({len(bars) if bars else 0} bars)",
                    "unknown", profile_name)

        import pandas as pd
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        # ── Gate 4: Observe & Score ──
        profile      = get_coin_profile(symbol)
        profile_name = (
            profile.profile.value if hasattr(profile.profile, "value")
            else str(profile.profile)
        )
        # [BUG-FIX] Sebelumnya: `observation = await observe(symbol, df, profile)`.
        # DUA bug sekaligus: (1) observe() adalah fungsi SYNC (bukan async def),
        # tapi dipakai dgn `await` -- observe() mengembalikan ObservationReport
        # langsung, bukan coroutine, jadi `await` di objek itu SELALU
        # TypeError; (2) signature mismatch -- observe() aslinya butuh
        # (symbol, strategy_profile, primary_df, primary_timeframe, ...), tapi
        # dipanggil positional (symbol, df, profile) -- df ke-mapping jadi
        # strategy_profile (harusnya string, malah DataFrame), profile
        # ke-mapping jadi primary_df (harusnya DataFrame, malah objek profile),
        # dan primary_timeframe (wajib, tanpa default) tidak diisi sama
        # sekali. Kombinasi keduanya membuat pemanggilan ini SELALU melempar
        # TypeError "missing 1 required positional argument: primary_timeframe"
        # -- ditangkap oleh except Exception di akhir analyze_position, jadi
        # SETIAP posisi Binance yang terdeteksi untracked SELALU dianggap
        # "tidak layak dikawal" (return False, score=0.0) walau datanya
        # sebenarnya valid. Dibuktikan via eksperimen: pola pemanggilan persis
        # ini menghasilkan TypeError nyata. Fix: panggil observe() dgn
        # parameter benar (bukan positional ambigu) & tanpa await (sync).
        observation = observe(
            symbol=symbol,
            strategy_profile=profile_name,
            primary_df=df,
            primary_timeframe=profile.timeframe,
        )

        if not observation.primary_tf_valid:
            return (False, 0.0, None, None, "Indikator primary TF tidak valid",
                    "unknown", profile_name)

        regime, regime_conf = classify_regime(symbol, observation.primary_tf_indicators)
        regime_value = regime.value if hasattr(regime, "value") else str(regime)
        scored = score_signal(observation, regime, regime_conf, db_manager)

        score = scored.total_score
        sl    = scored.suggested_sl
        tp    = scored.suggested_tp

        # Fallback SL/TP jika scorer tidak menghasilkan
        if sl is None:
            sl = round(price * 0.985, 8)   # SL 1.5%
        if tp is None:
            tp = round(price * 1.025, 8)   # TP 2.5%

        # ── Gate 5: Layak dikawal? ──
        if score >= MIN_ADOPT_SCORE:
            alasan = (
                f"Score {score:.1f} >= {MIN_ADOPT_SCORE} | "
                f"regime={regime_value} | conf={regime_conf:.2f}"
            )
            return True, score, sl, tp, alasan, regime_value, profile_name
        else:
            alasan = (
                f"Score {score:.1f} < {MIN_ADOPT_SCORE} (terlalu lemah) | "
                f"regime={regime_value}"
            )
            return False, score, sl, tp, alasan, regime_value, profile_name

    except Exception as e:
        log.error("analyze_position error [%s]: %s", symbol, e)
        return False, 0.0, None, None, f"Error analisis: {e}", "unknown", profile_name


async def adopt_position(
    symbol:       str,
    amount:       float,
    price:        float,
    score:        float,
    sl:           float,
    tp:           float,
    regime:       str,
    profile_name: str,
    db_manager,
) -> bool:
    """
    Inject posisi ke DB bot agar Trade Guardian bisa mengawal.

    # [BUG-FIX] Sebelumnya fungsi ini melakukan raw sqlite3.connect() ke path
    # ABSOLUT hardcoded "/root/algotrader/data/trading_bot.db" — sama sekali
    # tidak memakai parameter db_manager yang sudah di-pass (dead parameter).
    # Path ini TIDAK cocok dengan konvensi path DB yang dipakai di seluruh
    # codebase lain (main.py, simulate_test.py, learning/coin_swap.py semua
    # pakai "sqlite+aiosqlite:///./data/trading_bot.db", relatif & lewat
    # DATABASE_URL, via SQLAlchemy async ORM). Proyek ini jalan di
    # Termux/Linux (bukan selalu /root) — path lama nyaris pasti salah,
    # membuat sqlite3.connect() diam-diam bikin file DB baru yang terpisah/
    # orphan, atau gagal buka (caught oleh except -> log.error saja).
    # Juga ditemukan 2 bug tambahan di data yang ditulis:
    #   - side="buy" -- SALAH. Konvensi Position.side di seluruh main.py
    #     (trailing stop, cek SL/TP) HANYA membandingkan == "long" / "short".
    #     Posisi dgn side="buy" tidak akan pernah kena SL/TP oleh logika
    #     normal -> risk management posisi hasil adopt jadi tidak aktif.
    #   - strategy_profile="scalp_volatile" hardcoded utk SEMUA symbol,
    #     padahal analyze_position sudah menghitung profile asli via
    #     get_coin_profile(). entry_regime juga selalu "undefined" (caller
    #     tidak pernah mengirim regime asli — lihat fix di analyze_position).
    #   - entry_time berupa STRING (strftime), padahal Position.entry_time
    #     adalah kolom DateTime dan seluruh call site upsert_position lain
    #     (main.py) selalu mengirim objek datetime naive
    #     (datetime.now(timezone.utc).replace(tzinfo=None)).
    # Sekarang: pakai db_manager.upsert_position(symbol, {...}) — jalur yang
    # sama dgn cara bot membuka posisi normal, otomatis pakai DB/engine yang
    # benar (async, sesuai DATABASE_URL), side="long", strategy_profile &
    # entry_regime asli, entry_time sebagai objek datetime.
    """
    try:
        entry_time = datetime.now(timezone.utc).replace(tzinfo=None)

        await db_manager.upsert_position(symbol, {
            "entry_time":        entry_time,
            "entry_price":       round(price, 8),
            "current_price":     round(price, 8),
            "amount":            round(amount, 8),
            "side":              "long",
            "is_open":           True,
            "is_closing":        False,
            "stop_loss_price":   sl,
            "take_profit_price": tp,
            "strategy_name":     "manual_adopt",
            "strategy_profile":  profile_name,
            "entry_score":       score,
            "entry_regime":      regime,
            "highest_price":     price,
        })

        log.info(
            "✅ ADOPT %s | amount=%.4f | entry=%.6f | SL=%.6f | TP=%.6f | "
            "score=%.1f | profile=%s | regime=%s",
            symbol, amount, price, sl, tp, score, profile_name, regime,
        )
        return True

    except Exception as e:
        log.error("adopt_position error [%s]: %s", symbol, e)
        return False


async def run_position_sync(
    exchange, db_manager, notifier=None, phantom_suspects: Optional[Dict[str, int]] = None,
    amount_mismatch_suspects: Optional[Dict[str, int]] = None,
) -> Dict:
    """
    Main entry point — dipanggil periodik dari main loop.
    Deteksi → Analisis → Adopt posisi yang tidak tertracking.

    [ITEM #4] `notifier` opsional (default None -- notify_error() di-skip
    kalau tidak diisi) & `phantom_suspects` opsional (default dict lokal
    baru per panggilan -- TANPA debounce lintas-siklus kalau caller tidak
    oper punya sendiri yang persist, mis. self._phantom_suspects milik
    TradingBot). Backward-compatible dgn caller lama.

    [#36] `amount_mismatch_suspects` opsional -- pola identik phantom_
    suspects (dict TERPISAH), lihat future/position_sync_futures.py::
    run_position_sync() utk latar belakang lengkap (pola identik).
    """
    result = {
        "untracked_found": 0,
        "adopted":         0,
        "rejected":        0,
        "errors":          0,
        "phantom_candidates": 0,
        "phantom_confirmed":  0,
        "amount_mismatch_candidates": 0,
        "amount_mismatch_confirmed":  0,
    }
    if phantom_suspects is None:
        phantom_suspects = {}
    if amount_mismatch_suspects is None:
        amount_mismatch_suspects = {}

    try:
        sync_result = await find_untracked_positions(exchange, db_manager)
        untracked = sync_result["untracked"]
        result["untracked_found"] = len(untracked)

        if not sync_result["fetch_failed"]:
            candidates = sync_result["phantom_candidates"]
            result["phantom_candidates"] = len(candidates)
            await _process_phantom_candidates(candidates, phantom_suspects, db_manager, notifier, result)

            mismatch_candidates = sync_result["amount_mismatches"]
            result["amount_mismatch_candidates"] = len(mismatch_candidates)
            await _process_amount_mismatch_candidates(
                mismatch_candidates, amount_mismatch_suspects, db_manager, notifier, result,
            )

        if not untracked:
            log.info("✅ Semua posisi Binance sudah tertracking di DB")
            return result

        for pos in untracked:
            symbol = pos["symbol"]
            amount = pos["amount"]
            price  = pos["price"]

            log.info("🔍 Analisis posisi tidak tertracking: %s", symbol)

            layak, score, sl, tp, alasan, regime, profile_name = await analyze_position(
                symbol, amount, price, exchange, db_manager
            )

            if layak:
                adopted = await adopt_position(
                    symbol, amount, price, score,
                    sl, tp, regime, profile_name, db_manager,
                )
                if adopted:
                    result["adopted"] += 1
                    log.info("✅ %s diadopsi | %s", symbol, alasan)
                else:
                    result["errors"] += 1
            else:
                result["rejected"] += 1
                log.warning(
                    "⚠️  %s TIDAK diadopsi | %s | "
                    "Pertimbangkan jual manual!", symbol, alasan
                )

    except Exception as e:
        log.error("run_position_sync error: %s", e)
        result["errors"] += 1

    return result
