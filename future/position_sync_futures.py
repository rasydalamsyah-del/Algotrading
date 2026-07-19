"""
future/position_sync_futures.py — Binance USDT-M Futures Position Sync & Guardian

Diadaptasi dari spot/position_sync_spot.py. Perbedaan MENDASAR (bukan
sekadar rename):
- fetch_binance_futures_positions(): pakai exchange.fetch_positions()
  (endpoint khusus futures, tersedia di FutureExchangeConnector), BUKAN
  fetch_balance() -- konsep "punya saldo koin" spot tidak berlaku sama
  sekali di futures (posisi = leveraged exposure, bukan kepemilikan aset).
- side POSISI SEBENARNYA (long/short dari data exchange) diteruskan ke
  analyze_position()/score_signal(), BUKAN hardcode "long" seperti spot.
- adopt_position() menyimpan leverage/margin_mode/liquidation_price (field
  yang tidak ada sama sekali di spot), diambil langsung dari data posisi
  exchange (utk exchange asli, ccxt fetch_positions() biasanya sudah
  menyediakan liquidationPrice dari exchange itu sendiri -- lebih akurat
  dari estimasi kita di future/liquidation.py. Untuk paper trading, data
  ini datang dari _paper_positions internal FutureExchangeConnector).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("future.position_sync")

MIN_USDT_VALUE  = 1.0
MIN_ADOPT_SCORE = 45.0
MIN_CANDLE_BARS = 50
# [#36 -- audit fungsional] Toleransi deteksi amount mismatch (posisi ADA
# di kedua sisi tapi amount beda) -- reuse ambang PERSIS sama dgn
# _reconcile_positions_on_startup() (main_future.py/main_spot.py, 5%
# relatif) supaya konsisten satu ambang di seluruh repo utk kelas masalah
# yang sama, BUKAN nilai baru yang ditebak. Floor absolut (MIN_USDT_VALUE)
# dipakai bersamaan -- symbol dust/receh yang % relatifnya kebetulan besar
# tapi nilai $ selisihnya kecil TIDAK memicu alert (noise).
AMOUNT_MISMATCH_TOLERANCE_PCT = 5.0


async def fetch_binance_futures_positions(exchange) -> List[Dict]:
    """
    Fetch semua posisi terbuka di Binance Futures via exchange.fetch_positions()
    -- endpoint khusus futures, BEDA dari fetch_balance() yang dipakai spot.
    Return list of {symbol, side, amount, entry_price, price, leverage,
    margin_mode, liquidation_price, usdt_value}.

    [#18 -- audit fungsional] Docstring sebelumnya menjanjikan key
    `unrealized_pnl` di hasil, tapi TIDAK PERNAH genuinely ditulis ke
    results.append({...}) di bawah -- dikonfirmasi lewat baca kode
    langsung, field ini juga tidak pernah dikonsumsi di manapun (file ini
    maupun caller-nya). Diperbaiki di sini (docstring saja) supaya akurat
    -- bukan gap camelCase (entry_price/margin_mode/liquidation_price
    semua sudah punya fallback camelCase yang benar), murni dead
    docstring claim.

    [ITEM #4 -- audit fungsional] Sebelumnya fungsi ini menelan SEMUA
    exception (network/rate-limit/auth/apapun) dan mengembalikan [] --
    SAMA PERSIS dengan hasil "genuinely tidak ada posisi". Aman untuk arah
    untracked (fail-safe: fetch gagal -> tidak ada yang di-adopt), TAPI
    berbahaya untuk deteksi phantom (arah baru, lihat find_untracked_
    positions()) -- [] yang ambigu itu akan membuat SEMUA posisi DB tampak
    "phantom" serentak dari satu hiccup API. Sekarang exception di-RAISE
    apa adanya -- pemanggil (find_untracked_positions(), satu-satunya
    caller di repo ini, diverifikasi via grep) yang memutuskan cara
    menangani kegagalan fetch berbeda per arah perbandingan.
    """
    positions = await exchange.fetch_positions()
    results = []

    for pos in positions:
        symbol = pos.get("symbol")
        if not symbol:
            continue
        amount = float(pos.get("amount") or pos.get("contracts") or 0)
        if amount <= 0:
            continue

        side = pos.get("side", "long")
        entry_price = float(pos.get("entry_price") or pos.get("entryPrice") or 0)
        # [BUG-FIX] Sebelumnya cuma entry_price yang punya fallback camelCase.
        # ccxt unified fetch_positions() pakai camelCase (marginMode,
        # liquidationPrice) utk exchange ASLI -- paper trading kita sendiri
        # pakai snake_case. Tanpa fallback ini, margin_mode & liquidation_price
        # akan selalu None kalau dijalankan terhadap exchange Binance asli
        # (bukan paper trading), meski amount/side/entry_price tetap benar.
        margin_mode = pos.get("margin_mode") or pos.get("marginMode")
        liquidation_price_raw = pos.get("liquidation_price") or pos.get("liquidationPrice")
        leverage = pos.get("leverage")
        price = entry_price  # fallback awal, akan di-update di analyze_position via ticker fresh
        usdt_value = amount * price if price > 0 else 0.0
        if usdt_value < MIN_USDT_VALUE:
            continue

        results.append({
            "symbol":            symbol,
            "side":              side,
            "amount":            amount,
            "entry_price":       entry_price,
            "price":             price,
            "usdt_value":        usdt_value,
            "leverage":          leverage,
            "margin_mode":       margin_mode,
            "liquidation_price": liquidation_price_raw,
        })

    log.info("Binance futures: %d posisi aktif ditemukan", len(results))
    return results


async def find_untracked_positions(exchange, db_manager) -> Dict:
    """
    Bandingkan posisi Binance Futures vs DB bot -- DUA ARAH.

    [ITEM #4 -- audit fungsional] Sebelumnya fungsi ini HANYA mendeteksi
    1 dari 3 kemungkinan mismatch: posisi ADA di exchange, TIDAK ADA di DB
    ("untracked"). Sekarang JUGA mendeteksi arah sebaliknya: posisi ADA di
    DB (is_open=True), TIDAK ADA LAGI di exchange ("phantom candidate") --
    root cause konkret: _paper_positions dihapus SEBELUM db.close_position()
    (lihat _do_close_position() di main_future.py), kalau db.close_position()
    gagal setelahnya, DB nyangkut is_open=True permanen tanpa ada yang
    pernah tahu.

    [#36 -- audit fungsional] Mismatch tipe ke-3 (posisi ADA di KEDUA sisi
    tapi `amount` beda > AMOUNT_MISMATCH_TOLERANCE_PCT & > MIN_USDT_VALUE)
    SEKARANG JUGA dideteksi (sebelumnya "dicatat sbg item backlog terpisah"
    -- item ini). Root cause konkret: reduce_position_amount_with_retry()
    (#28) exhausted SEMUA retry setelah partial-close order sukses
    tereksekusi di exchange -- DB nyangkut amount LAMA (lebih besar).
    Symbol dgn is_closing=True DIKECUALIKAN (sama alasan dgn
    phantom_candidates -- posisi yang genuinely sedang proses close normal
    bisa tampak "mismatch" sesaat, bukan masalah nyata).

    Return dict:
        untracked:          List[Dict] -- posisi exchange tanpa row DB.
        phantom_candidates: List[str]  -- simbol DB is_open=True TANPA posisi
                             exchange, SUDAH difilter is_closing=True (posisi
                             yang genuinely sedang proses close normal --
                             mark_position_closing() sudah dipanggil --
                             BUKAN phantom, cuma race sesaat yg akan resolve
                             sendiri lewat jalur close biasa).
        amount_mismatches:  List[Dict] -- {symbol, db_amount, exchange_amount,
                             diff_pct} utk symbol yang ADA di kedua sisi tapi
                             amount beda signifikan, SUDAH difilter
                             is_closing=True (alasan sama dgn phantom_candidates).
        fetch_failed:        bool -- True kalau fetch_binance_futures_positions()
                             gagal (network/rate-limit/auth/dst). Kalau True,
                             `untracked` SELALU [] (fail-safe, sama seperti
                             perilaku lama -- fetch gagal = tidak ada yang
                             di-adopt, aman) TAPI `phantom_candidates` &
                             `amount_mismatches` JUGA SELALU [] -- caller
                             WAJIB tidak memperlakukan itu sebagai "genuinely
                             tidak ada phantom/mismatch", cukup skip
                             pemrosesan siklus ini (coba lagi siklus
                             berikutnya). Lihat run_position_sync().
    """
    try:
        futures_positions = await fetch_binance_futures_positions(exchange)
    except Exception as e:
        log.error(
            "Fetch posisi Binance Futures gagal -- skip perbandingan phantom "
            "siklus ini (arah untracked tetap fail-safe kosong, coba lagi "
            "siklus berikutnya): %s", e,
        )
        return {"untracked": [], "phantom_candidates": [], "amount_mismatches": [], "fetch_failed": True}

    db_open = await db_manager.get_open_positions()
    db_symbols       = {p.symbol for p in db_open}
    db_by_symbol     = {p.symbol: p for p in db_open}
    exchange_by_symbol = {pos["symbol"]: pos for pos in futures_positions}
    exchange_symbols = set(exchange_by_symbol.keys())

    untracked = []
    for pos in futures_positions:
        if pos["symbol"] not in db_symbols:
            untracked.append(pos)
            log.warning(
                "⚠️  Posisi futures tidak tertracking: %s (%s) | amount=%.4f | ~$%.2f USDT | leverage=%s",
                pos["symbol"], pos["side"], pos["amount"], pos["usdt_value"], pos.get("leverage"),
            )

    phantom_candidates = [
        p.symbol for p in db_open
        if p.symbol not in exchange_symbols and not p.is_closing
    ]

    # [#36] Amount mismatch -- symbol ADA di kedua sisi (bukan untracked,
    # bukan phantom), is_closing=False, tapi amount beda signifikan.
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
            "⚠️  Amount mismatch futures: %s | DB=%.8f vs exchange=%.8f (%.1f%%)",
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
    """
    [ITEM #4] Debounce 2 siklus run_position_sync() berturut-turut (~5-10
    menit, interval loop 300 detik) sebelum kandidat dianggap phantom
    TERKONFIRMASI -- lapis pertahanan tambahan terhadap false-positive dari
    race close normal yang belum sempat mengeset is_closing=True (window
    SANGAT sempit, filter is_closing di find_untracked_positions() sudah
    menutup mayoritas kasus, debounce ini menutup sisanya).

    TIDAK PERNAH memanggil db.close_position() otomatis -- risiko false-
    positive (walau sudah didebounce) menghancurkan data posisi ASLI kalau
    ternyata salah, ASIMETRIS lebih berbahaya drpd status quo (phantom yang
    tidak terdeteksi setidaknya tidak merusak apapun lebih jauh). Aksi
    HANYA: log.critical + db.save_log("CRITICAL", ...) + notifier.notify_error()
    -- reuse pola yang SUDAH jadi konvensi di _do_close_position() utk
    kelas masalah "butuh intervensi manusia", bukan channel baru.

    `phantom_suspects` dimutasi in-place -- WAJIB dimiliki caller (TradingBot
    instance, field self._phantom_suspects) supaya persist antar-siklus.
    Simbol yang tidak lagi jadi kandidat siklus ini (mismatch resolve
    sendiri) dibersihkan dari counter.
    """
    candidate_set = set(candidates)
    for sym in list(phantom_suspects.keys()):
        if sym not in candidate_set:
            del phantom_suspects[sym]

    for symbol in candidates:
        phantom_suspects[symbol] = phantom_suspects.get(symbol, 0) + 1
        count = phantom_suspects[symbol]

        if count < 2:
            log.warning(
                "Kandidat phantom position (futures): %s TIDAK ada di exchange "
                "tapi is_open=True di DB (siklus ke-%d/2 -- belum dikonfirmasi).",
                symbol, count,
            )
            continue

        result["phantom_confirmed"] = result.get("phantom_confirmed", 0) + 1
        msg = (
            f"PHANTOM POSITION terdeteksi (futures): {symbol} — is_open=True di "
            f"DB TAPI tidak ada di exchange selama >= 2 siklus sync berturut-turut. "
            f"Kemungkinan db.close_position() gagal setelah order close sukses "
            f"tereksekusi (lihat _do_close_position()). TIDAK di-auto-close -- "
            f"perlu review manual: cek riwayat order {symbol} di exchange, lalu "
            f"close manual di DB kalau genuinely sudah tidak ada posisi."
        )
        log.critical(msg)
        try:
            await db_manager.save_log("CRITICAL", "position_sync_futures", msg)
        except Exception as e:
            log.error("save_log phantom position gagal: %s", e)
        if notifier is not None:
            try:
                await notifier.notify_error(f"phantom_position_futures", msg)
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
    [#36 -- audit fungsional] Debounce 2 siklus (pola IDENTIK dgn
    _process_phantom_candidates() di atas) sebelum amount mismatch
    dianggap TERKONFIRMASI -- melindungi dari race legitimate: snapshot
    diambil PERSIS di antara "order partial-close sukses di exchange" dan
    "DB commit" akan tampak mismatch sesaat padahal itu proses closing
    normal yang akan self-resolve di siklus berikutnya (kelas race sama
    persis dgn phantom, filter is_closing di find_untracked_positions()
    sudah menutup mayoritas, debounce ini menutup sisanya).

    [Keputusan #36, konsisten dgn _process_phantom_candidates()] TIDAK
    PERNAH auto-correct DB amount -- notify-only. Auto-correct SETELAH
    debounce mengurangi risiko race tapi tidak menghilangkannya, dan
    auto-correct blind bisa menutupi masalah lebih dalam (order duplikat,
    aktivitas eksternal tak terduga) yang butuh dilihat manusia dulu.
    Reuse channel notifikasi yang sudah konvensi (log.critical + save_log +
    notify_error), BUKAN channel baru.

    `mismatch_suspects` TERPISAH dari `phantom_suspects` (dict counter
    sendiri) -- symbol bisa punya kedua masalah independen (mis. posisi
    genuinely open+tracked TAPI amount-nya beda, BUKAN phantom sama
    sekali) -- mencampur counter akan membuat reset logic salah.
    Dimutasi in-place -- WAJIB dimiliki caller (TradingBot instance, field
    self._amount_mismatch_suspects) supaya persist antar-siklus.
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
                "Kandidat amount mismatch (futures): %s DB=%.8f vs exchange=%.8f "
                "(%.1f%%) (siklus ke-%d/2 -- belum dikonfirmasi).",
                symbol, c["db_amount"], c["exchange_amount"], c["diff_pct"], count,
            )
            continue

        result["amount_mismatch_confirmed"] = result.get("amount_mismatch_confirmed", 0) + 1
        msg = (
            f"AMOUNT MISMATCH terdeteksi (futures): {symbol} — DB amount="
            f"{c['db_amount']:.8f} vs exchange amount={c['exchange_amount']:.8f} "
            f"({c['diff_pct']:.1f}%) selama >= 2 siklus sync berturut-turut. "
            f"Kemungkinan reduce_position_amount_with_retry() gagal setelah "
            f"partial-close order sukses tereksekusi (lihat _do_close_position()). "
            f"TIDAK di-auto-correct -- perlu review manual: cek riwayat order "
            f"{symbol} di exchange, lalu koreksi manual amount di DB kalau "
            f"genuinely sudah berbeda."
        )
        log.critical(msg)
        try:
            await db_manager.save_log("CRITICAL", "position_sync_futures", msg)
        except Exception as e:
            log.error("save_log amount mismatch gagal: %s", e)
        if notifier is not None:
            try:
                await notifier.notify_error("amount_mismatch_futures", msg)
            except Exception as e:
                log.error("notify_error amount mismatch gagal: %s", e)


async def analyze_position(
    symbol:  str,
    side:    str,
    amount:  float,
    price:   float,
    exchange,
    db_manager,
) -> Tuple[bool, float, Optional[float], Optional[float], str, str, str]:
    """
    [FUTURES-SPECIFIC] Analisis Gate3-5, side diteruskan APA ADANYA dari
    posisi sebenarnya (bukan hardcode "long" spt spot) ke score_signal().
    Fallback SL/TP juga di-mirror sesuai side.
    """
    from engine.intelligence.observer  import observe
    from engine.intelligence.scorer    import score_signal
    from engine.intelligence.classifier import classify_regime
    from engine.profiles.registry      import get_coin_profile

    profile_name = "unknown"
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe="15m", limit=200)
        if not bars or len(bars) < MIN_CANDLE_BARS:
            return (False, 0.0, None, None,
                    f"Data candle tidak cukup ({len(bars) if bars else 0} bars)",
                    "unknown", profile_name)

        import pandas as pd
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        profile = get_coin_profile(symbol)
        profile_name = profile.profile.value if hasattr(profile.profile, "value") else str(profile.profile)

        observation = observe(
            symbol=symbol, strategy_profile=profile_name,
            primary_df=df, primary_timeframe=profile.timeframe,
        )
        if not observation.primary_tf_valid:
            return (False, 0.0, None, None, "Indikator primary TF tidak valid", "unknown", profile_name)

        regime, regime_conf = classify_regime(symbol, observation.primary_tf_indicators)
        regime_value = regime.value if hasattr(regime, "value") else str(regime)

        # [FUTURES-SPECIFIC] side diteruskan APA ADANYA -- scorer sudah
        # side-aware sejak perbaikan bias long-only sebelumnya.
        scored = score_signal(observation, regime, regime_conf, db_manager, side=side)

        score = scored.total_score
        sl    = scored.suggested_sl
        tp    = scored.suggested_tp

        # Fallback SL/TP, di-mirror sesuai side
        if sl is None:
            sl = round(price * 0.985, 8) if side == "long" else round(price * 1.015, 8)
        if tp is None:
            tp = round(price * 1.025, 8) if side == "long" else round(price * 0.975, 8)

        if score >= MIN_ADOPT_SCORE:
            alasan = f"Score {score:.1f} >= {MIN_ADOPT_SCORE} | regime={regime_value} | conf={regime_conf:.2f}"
            return True, score, sl, tp, alasan, regime_value, profile_name
        else:
            alasan = f"Score {score:.1f} < {MIN_ADOPT_SCORE} (terlalu lemah) | regime={regime_value}"
            return False, score, sl, tp, alasan, regime_value, profile_name

    except Exception as e:
        log.error("analyze_position error [%s]: %s", symbol, e)
        return False, 0.0, None, None, f"Error analisis: {e}", "unknown", profile_name


async def adopt_position(
    symbol:            str,
    side:              str,
    amount:            float,
    price:             float,
    score:             float,
    sl:                float,
    tp:                float,
    regime:            str,
    profile_name:      str,
    leverage:          Optional[int],
    margin_mode:       Optional[str],
    liquidation_price: Optional[float],
    db_manager,
) -> bool:
    """
    [FUTURES-SPECIFIC] side APA ADANYA (bukan hardcode "long"), plus
    leverage/margin_mode/liquidation_price -- field yang tidak ada di
    spot::adopt_position(). liquidation_price di sini berasal dari data
    exchange asli (fetch_positions()) kalau tersedia -- LEBIH AKURAT dari
    estimasi future/liquidation.py yang APPROXIMATE, karena datang langsung
    dari exchange (yang tahu tier margin bracket sebenarnya).
    """
    try:
        entry_time = datetime.now(timezone.utc).replace(tzinfo=None)

        await db_manager.upsert_position(symbol, {
            "entry_time":          entry_time,
            "entry_price":         round(price, 8),
            "current_price":       round(price, 8),
            "amount":              round(amount, 8),
            "side":                side,
            "is_open":             True,
            "is_closing":          False,
            "stop_loss_price":     sl,
            "take_profit_price":   tp,
            "strategy_name":       "manual_adopt_futures",
            "strategy_profile":    profile_name,
            "entry_score":         score,
            "entry_regime":        regime,
            "highest_price":       price,
            "market_type":         "futures",
            "leverage":            leverage,
            "margin_mode":         margin_mode,
            "liquidation_price":   liquidation_price,
            "mark_price_at_entry": price,
        })

        log.info(
            "✅ ADOPT (futures) %s %s | amount=%.4f | entry=%.6f | SL=%.6f | TP=%.6f | "
            "score=%.1f | profile=%s | regime=%s | leverage=%s | liq_price=%s",
            side.upper(), symbol, amount, price, sl, tp, score, profile_name, regime,
            leverage, liquidation_price,
        )
        return True

    except Exception as e:
        log.error("adopt_position (futures) error [%s]: %s", symbol, e)
        return False


async def run_position_sync(
    exchange, db_manager, notifier=None, phantom_suspects: Optional[Dict[str, int]] = None,
    amount_mismatch_suspects: Optional[Dict[str, int]] = None,
) -> Dict:
    """
    Main entry point -- dipanggil periodik dari main_future.py loop.

    [ITEM #4] `notifier` opsional (default None -- notify_error() di-skip
    kalau tidak diisi, TIDAK crash) & `phantom_suspects` opsional (default
    dict lokal baru per panggilan -- TANPA debounce lintas-siklus kalau
    caller tidak mengoper punya sendiri yang persist, mis. self._phantom_
    suspects milik TradingBot). Backward-compatible: caller lama yang cuma
    oper (exchange, db_manager) tetap jalan, cuma tanpa notifikasi phantom
    & tanpa debounce antar-panggilan.

    [#36] `amount_mismatch_suspects` opsional -- pola identik phantom_
    suspects (dict TERPISAH, lihat _process_amount_mismatch_candidates()),
    default dict lokal baru per panggilan kalau caller tidak mengoper
    punya sendiri yang persist (mis. self._amount_mismatch_suspects milik
    TradingBot).
    """
    result = {
        "untracked_found": 0, "adopted": 0, "rejected": 0, "errors": 0,
        "phantom_candidates": 0, "phantom_confirmed": 0,
        "amount_mismatch_candidates": 0, "amount_mismatch_confirmed": 0,
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
        # fetch_failed=True -> phantom_suspects/amount_mismatch_suspects
        # SENGAJA tidak disentuh sama sekali (bukan direset/dianggap
        # resolve) -- counter membeku, coba lagi siklus berikutnya begitu
        # fetch sukses lagi.

        if not untracked:
            log.info("✅ Semua posisi Binance Futures sudah tertracking di DB")
            return result

        for pos in untracked:
            symbol = pos["symbol"]
            side   = pos["side"]
            amount = pos["amount"]

            # [FUTURES-SPECIFIC] Ambil harga fresh (bukan cuma entry_price
            # lama) supaya analisis pakai kondisi pasar terkini.
            try:
                ticker = await exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or pos["entry_price"] or 0)
            except Exception:
                price = pos["entry_price"]

            log.info("🔍 Analisis posisi futures tidak tertracking: %s (%s)", symbol, side)

            layak, score, sl, tp, alasan, regime, profile_name = await analyze_position(
                symbol, side, amount, price, exchange, db_manager
            )

            if layak:
                adopted = await adopt_position(
                    symbol, side, amount, price, score, sl, tp, regime, profile_name,
                    pos.get("leverage"), pos.get("margin_mode"), pos.get("liquidation_price"),
                    db_manager,
                )
                if adopted:
                    result["adopted"] += 1
                    log.info("✅ %s (%s) diadopsi | %s", symbol, side, alasan)
                else:
                    result["errors"] += 1
            else:
                result["rejected"] += 1
                log.warning(
                    "⚠️  %s (%s) TIDAK diadopsi | %s | Pertimbangkan tutup manual di Binance Futures!",
                    symbol, side, alasan,
                )

    except Exception as e:
        log.error("run_position_sync (futures) error: %s", e)
        result["errors"] += 1

    return result
