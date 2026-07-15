"""
future/capital_allocator.py — Alokasi saldo long vs short saat kapasitas
modal terbatas (Binance USDT-M Futures).

Latar belakang: sejak 7 titik bias long-only diperbaiki (lihat catatan di
engine/intelligence/{commander,scorer,validator,trade_guardian}.py dan
future/{strategy_future,execution_future,position_sync_futures}.py),
future/main_future.py::run_gate3_worker() sudah genuinely bidirectional --
tiap simbol dicek DUA arah independen (_check_gate3_direction), dan skor
long/short dihitung lewat pipeline yang sama-sama side-aware. TAPI: begitu
kapasitas modal (slot max_open_positions ATAU margin) habis, kandidat yang
gagal cuma dibuang -- run_gate3_worker's `for cand_side in candidate_sides`
loop `break` begitu SATU kandidat sampai ke _handle_entry(), apapun
hasilnya, jadi kandidat arah lain di siklus yang sama tidak pernah dicoba
kalau kandidat pertama (selalu long duluan, krn urutan candidate_sides)
gagal di real risk-check. Modul ini menambal celah itu SETELAH modal
sungguhan sudah dicek gagal -- BUKAN mengubah urutan/logic gate3 itu sendiri.

Desain inti (didiskusikan & disetujui sebelum implementasi):
1. Registry di-key per SYMBOL (bukan symbol+side) -- terbukti valid karena
   _check_gate3_direction() secara matematis mutual exclusive per simbol
   per siklus (ema9>ema21 vs ema9<ema21 pada bar yang SAMA tidak mungkin
   sama-sama True). Lihat _check_gate3_direction() di main_future.py.
2. TTL kandidat tertunda HYBRID (OR, siapa duluan menang):
   a. Candle baru sudah closed di profile.timeframe kandidat itu sendiri
      (deteksi via TradingBot._last_candle_ts yang SUDAH ADA, tidak fetch
      OHLCV baru cuma utk cek TTL).
   b. Harga bergerak > 1.5x ATR baseline (ATRr_14 dari gate3 -- BUKAN
      scored.observation....volatility.atr -- konsisten & murah dihitung
      ulang tanpa re-run observer/scorer).
   c. Wall-clock cap 2x durasi timeframe -- jaring pengaman terakhir kalau
      (a) gagal terdeteksi (mis. simbol berhenti ter-scan/keluar universe).
3. Baseline TTL (registered_at/candle_ts_at_registration/price_at_registration/
   atr_at_registration) TIDAK PERNAH direset selama side tetap sama antar-
   refresh -- expiry clock jalan dari kali PERTAMA kandidat terlihat, bukan
   diperpanjang tiap kali gate3 re-detect arah yang sama. Kalau side
   berbalik (long<->short), itu REPLACE penuh (setup baru, baseline lama
   tidak relevan).
4. Rekonsiliasi dipicu event-driven (dari _do_close_position(), setelah
   _refresh_portfolio()) DAN fallback polling (dari run_portfolio_monitor()
   yang sudah ada, tiap SNAPSHOT_INTERVAL) sbg safety-net -- bukan
   mekanisme utama.
5. Re-score SELALU fresh (fetch OHLCV + confirmation TF + orderbook baru,
   panggil get_scored_signal()+commander.decide() lagi) -- TIDAK PERNAH
   pakai skor lama utk keputusan eksekusi. last_score yang tersimpan di
   registry HANYA dipakai utk urutan prioritas siapa di-re-score duluan
   (pick_best_pair), bukan keputusan akhir.
6. Tie-break: skor sama persis -> long menang (lihat _select_winner()).
7. Per pemanggilan reconcile_pending(), MAKSIMAL 1 long + 1 short yang
   di-re-score (bukan seluruh registry) -- biaya komputasi per event tetap
   kecil & predictable. Kandidat yang kalah di satu ronde TIDAK disentuh,
   nunggu giliran jadi top-pick di reconcile berikutnya.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from engine.constants import TIMEFRAME_SECONDS
from engine.core.models import SignalEvent, SignalType

log = logging.getLogger("future.capital_allocator")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Struktur data ────────────────────────────────────────────────────────


@dataclass
class PendingCandidate:
    symbol: str
    side: str  # "long" | "short"

    # Baseline -- DIPATOK sekali saat pertama kali terlihat, TIDAK direset
    # oleh register_or_refresh() selama side sama. Dasar dari is_expired().
    registered_at: datetime
    candle_ts_at_registration: int
    price_at_registration: float
    atr_at_registration: float
    profile_timeframe: str
    profile_name: str

    # Bookkeeping -- BOLEH berubah tiap refresh, TIDAK memengaruhi TTL.
    last_score: float = 0.0
    last_checked_at: Optional[datetime] = None
    reason_deferred: str = ""
    defer_count: int = 1


# ─── Registry: register / refresh ──────────────────────────────────────────


def register_or_refresh(
    registry: Dict[str, PendingCandidate],
    symbol: str,
    side: str,
    profile_name: str,
    profile_timeframe: str,
    candle_ts: int,
    price: float,
    atr: float,
    score: float,
    reason: str,
    now: Optional[datetime] = None,
) -> PendingCandidate:
    """Simpan kandidat baru, atau refresh kandidat existing untuk symbol
    yang sama. Kalau side BERBEDA dari yang tersimpan (arah pasar berbalik)
    -> REPLACE penuh, baseline di-set ulang. Kalau side SAMA -> HANYA update
    bookkeeping (last_score/last_checked_at/reason_deferred/defer_count),
    baseline TTL tidak disentuh sama sekali."""
    _now = now or _utcnow()
    existing = registry.get(symbol)

    if existing is None or existing.side != side:
        candidate = PendingCandidate(
            symbol=symbol, side=side,
            registered_at=_now, candle_ts_at_registration=candle_ts,
            price_at_registration=price, atr_at_registration=atr,
            profile_timeframe=profile_timeframe, profile_name=profile_name,
            last_score=score, last_checked_at=_now,
            reason_deferred=reason, defer_count=1,
        )
        registry[symbol] = candidate
        log.info(
            "[CapitalAllocator] REGISTER %s (%s) | score=%.1f | %s",
            symbol, side, score, reason,
        )
        return candidate

    existing.last_score = score
    existing.last_checked_at = _now
    existing.reason_deferred = reason
    existing.defer_count += 1
    log.debug(
        "[CapitalAllocator] REFRESH %s (%s) | score=%.1f | defer_count=%d | %s",
        symbol, side, score, existing.defer_count, reason,
    )
    return existing


# ─── TTL / expiry ───────────────────────────────────────────────────────────


def is_expired(
    candidate: PendingCandidate,
    now: datetime,
    latest_candle_ts: Optional[int],
    current_price: Optional[float],
) -> Tuple[bool, str]:
    """3 aturan, OR -- siapa duluan terpenuhi yang jadi alasan. Pure
    function, tidak melakukan I/O apapun -- semua input sudah harus
    disediakan caller."""

    # Aturan 1: candle baru closed di timeframe kandidat itu sendiri.
    if (
        latest_candle_ts is not None
        and latest_candle_ts > candidate.candle_ts_at_registration
    ):
        return True, f"candle_closed(tf={candidate.profile_timeframe})"

    # Aturan 2: harga bergerak > 1.5x ATR baseline.
    if (
        current_price is not None
        and candidate.atr_at_registration
        and candidate.atr_at_registration > 0
    ):
        moved = abs(current_price - candidate.price_at_registration)
        threshold = 1.5 * candidate.atr_at_registration
        if moved > threshold:
            return True, f"atr_move({moved:.6f}>{threshold:.6f})"

    # Aturan 3: wall-clock cap 2x durasi timeframe -- jaring pengaman
    # terakhir kalau aturan 1 gagal terdeteksi (simbol berhenti ter-scan).
    tf_secs = TIMEFRAME_SECONDS.get(candidate.profile_timeframe, 900)
    elapsed = (now - candidate.registered_at).total_seconds()
    cap = 2 * tf_secs
    if elapsed > cap:
        return True, f"wall_clock_cap({elapsed:.0f}s>{cap}s)"

    return False, ""


def purge_expired(
    registry: Dict[str, PendingCandidate],
    now: datetime,
    last_candle_ts_map: Dict[Tuple[str, str], int],
    current_prices: Dict[str, float],
) -> List[Tuple[str, PendingCandidate, str]]:
    """Hapus kandidat basi dari registry (mutasi in-place). Return daftar
    (symbol, candidate, alasan) yang dibuang, untuk logging caller. Pure
    thd I/O -- last_candle_ts_map & current_prices harus sudah disiapkan
    caller (biasanya dari TradingBot._last_candle_ts & ws_feed.live_tickers)."""
    purged: List[Tuple[str, PendingCandidate, str]] = []
    for symbol in list(registry.keys()):
        candidate = registry[symbol]
        latest_ts = last_candle_ts_map.get((symbol, candidate.profile_timeframe))
        current_price = current_prices.get(symbol)
        expired, reason = is_expired(candidate, now, latest_ts, current_price)
        if expired:
            purged.append((symbol, registry.pop(symbol), reason))
            log.info(
                "[CapitalAllocator] EXPIRE %s (%s) | %s | defer_count=%d",
                symbol, candidate.side, reason, candidate.defer_count,
            )
    return purged


# ─── Perbandingan & tie-break ───────────────────────────────────────────────


def pick_best_pair(
    registry: Dict[str, PendingCandidate],
) -> Tuple[Optional[PendingCandidate], Optional[PendingCandidate]]:
    """Ambil kandidat dgn last_score TERTINGGI per side (skor LAMA/stale --
    HANYA menentukan siapa di-re-score DULUAN, bukan keputusan eksekusi)."""
    longs = [c for c in registry.values() if c.side == "long"]
    shorts = [c for c in registry.values() if c.side == "short"]
    best_long = max(longs, key=lambda c: c.last_score) if longs else None
    best_short = max(shorts, key=lambda c: c.last_score) if shorts else None
    return best_long, best_short


def _select_winner(
    long_result: Optional[Tuple[PendingCandidate, float]],
    short_result: Optional[Tuple[PendingCandidate, float]],
) -> Optional[str]:
    """Pure function -- input HANYA kandidat yang sudah dikonfirmasi
    executable pasca re-score fresh (None kalau sisi itu tidak/tidak lagi
    actionable). Tie-break: skor sama -> long menang (>=, bukan >)."""
    if long_result is None and short_result is None:
        return None
    if long_result is None:
        return "short"
    if short_result is None:
        return "long"
    _, long_score = long_result
    _, short_score = short_result
    return "long" if long_score >= short_score else "short"


# ─── Re-scoring fresh (I/O, dipakai reconcile_pending) ─────────────────────


async def _rescore_candidate(
    bot, candidate: PendingCandidate,
) -> Tuple[str, Optional[float], Optional[dict]]:
    """Re-score SATU kandidat dari nol (fetch OHLCV+confirmation TF+
    orderbook baru, get_scored_signal, commander.decide) -- fidelity sama
    dengan run_gate3_worker._process_one(), sengaja DIDUPLIKASI (bukan
    refactor shared helper) supaya jalur gate3 normal yang sudah teruji
    tidak ikut disentuh oleh perubahan ini. Kandidat untuk refactor jadi
    helper bersama nanti kalau modul ini sudah stabil.

    Return: (status, fresh_score, payload)
      status: "executable" | "still_pending" | "stale"
      payload (hanya diisi kalau status=="executable"): dict berisi
        {df, close, atr, scored, decision} -- siap dipakai _build_entry_signal.
    """
    from engine.profiles.registry import get_coin_profile
    from engine.profiles.thresholds import get_dynamic_threshold
    from engine.intelligence.commander import decide as _cmd_decide

    symbol = candidate.symbol
    side = candidate.side

    # [BIAS-FIX] key (symbol, side) -- sebelumnya (symbol,) saja, sinyal
    # whale utk sisi LAWAN candidate.side ini bisa salah ikut memblokir.
    inv = bot._invalidation_signals.get((symbol, side))
    if inv and inv.get("action") in ("skip_all", "skip_gate3_only"):
        return "stale", None, None
    threshold_mult = 1.2 if (inv and inv.get("action") == "monitor") else 1.0

    try:
        profile = get_coin_profile(symbol)
        tf = profile.timeframe
    except Exception:
        profile = None
        tf = candidate.profile_timeframe

    try:
        bars = await bot.exchange.fetch_ohlcv(
            symbol, tf, limit=bot.config["lookback_candles"]
        )
    except Exception as e:
        log.debug("[CapitalAllocator] fetch OHLCV gagal %s: %s", symbol, e)
        return "still_pending", None, None
    if not bars or len(bars) < 60:
        return "still_pending", None, None

    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(bars, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)

    try:
        import engine.ta_compat  # noqa
        df.ta.ema(length=9, append=True)
        df.ta.ema(length=21, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df.ta.vwap(anchor="D", append=True)
        df = df.dropna()
    except Exception as e:
        log.debug("[CapitalAllocator] indikator gagal %s: %s", symbol, e)
        return "still_pending", None, None
    if len(df) < 5:
        return "still_pending", None, None

    # Gate3 direction WAJIB masih valid untuk side ini -- kalau arah pasar
    # sudah berbalik sejak kandidat pertama diregistrasi, setup lama sudah
    # tidak relevan, ini "sudah tidak bagus" (poin #4 spec), bukan "masih
    # nunggu modal".
    still_valid_direction = await bot._check_gate3_direction(symbol, df, tf, side, profile)
    if not still_valid_direction:
        return "stale", None, None

    bar = df.iloc[-2]
    close = float(bar["close"])
    atr = float(bar.get("ATRr_14", 0))

    confirmation_df = None
    confirmation_tf = None
    if bot.config.get("confirmation_tf_enabled", True):
        try:
            confirmation_tf = getattr(profile, "effective_confirmation_tf", None)
            if confirmation_tf and confirmation_tf != tf:
                conf_bars = await bot.exchange.fetch_ohlcv(
                    symbol, confirmation_tf, limit=bot.config["lookback_candles"]
                )
                if conf_bars and len(conf_bars) >= 20:
                    cdf = pd.DataFrame(conf_bars, columns=cols)
                    cdf["timestamp"] = pd.to_datetime(cdf["timestamp"], unit="ms", utc=True)
                    cdf.set_index("timestamp", inplace=True)
                    confirmation_df = cdf
        except Exception as e:
            log.debug("[CapitalAllocator] confirmation TF gagal %s: %s", symbol, e)

    ob = bot.ws_feed.live_orderbooks.get(symbol, {})
    ticker = bot.ws_feed.live_tickers.get(symbol, {})
    qv = ticker.get("quote_volume")
    if qv and float(qv) > 0 and close > 0:
        df["quote_volume"] = df["volume"] * df["close"]
        df.loc[df.index[-1], "quote_volume"] = float(qv)

    try:
        scored = await bot.strategy.get_scored_signal(
            symbol=symbol, df=df, confirmation_df=confirmation_df,
            confirmation_timeframe=confirmation_tf, ob_data=ob,
            side=side,
        )
    except Exception as e:
        log.debug("[CapitalAllocator] scored signal error %s (%s): %s", symbol, side, e)
        return "still_pending", None, None
    if scored is None:
        return "stale", None, None

    total_score = float(getattr(scored, "total_score", 0) or 0)
    try:
        regime_val = scored.regime.value if scored.regime else "undefined"
        # [BIAS-FIX] side=side -- sebelumnya selalu matrix long apapun side
        # kandidat yg sedang di-rescore.
        base_threshold = get_dynamic_threshold(profile.profile.value, regime_val, side=side)
    except Exception:
        base_threshold = float(getattr(scored, "threshold_used", 65) or 65)
    effective_threshold = base_threshold * threshold_mult
    if total_score < effective_threshold:
        return "stale", total_score, None

    try:
        open_syms = [p.symbol for p in await bot.db.get_open_positions()]
    except Exception:
        open_syms = []

    try:
        decision = await _cmd_decide(
            signal=scored, open_positions=open_syms,
            portfolio_value=bot.portfolio_state.get("total_equity", 0.0),
            base_risk_pct=bot.config.get("risk_per_trade_pct", 1.0),
            exchange_connector=bot.ws_feed, risk_manager=bot.risk_manager,
            db_manager=bot.db, side=side,
        )
    except Exception as e:
        log.warning("[CapitalAllocator] commander.decide error %s (%s): %s", symbol, side, e)
        return "still_pending", total_score, None

    if decision.is_executable:
        return "executable", total_score, {
            "df": df, "close": close, "atr": atr,
            "scored": scored, "decision": decision, "profile": profile, "tf": tf,
        }
    if decision.capital_constrained:
        return "still_pending", total_score, None

    # REJECT/WAIT non-capital -- sinyal genuinely sudah tidak bagus lagi.
    return "stale", total_score, None


def _build_entry_signal(
    bot, symbol: str, side: str, payload: dict,
) -> SignalEvent:
    """Bangun SignalEvent dari hasil _rescore_candidate(status=='executable'),
    pola konstruksi IDENTIK dengan run_gate3_worker (sengaja duplikat kecil,
    lihat catatan di _rescore_candidate)."""
    scored = payload["scored"]
    decision = payload["decision"]
    profile = payload["profile"]
    close = payload["close"]
    atr = payload["atr"]

    live_ticker = bot.ws_feed.live_tickers.get(symbol, {})
    live_price = float(live_ticker.get("last") or 0)
    exec_price = live_price if live_price > 0 else close

    signal_type = SignalType.BUY if side == "long" else SignalType.OPEN_SHORT
    return SignalEvent(
        symbol=symbol, signal_type=signal_type, price=exec_price,
        timestamp=_utcnow(), strategy="capital_allocator_reconcile",
        confidence=float(getattr(scored, "confidence", 0.5) or 0.5),
        stop_loss=getattr(scored, "suggested_sl", None),
        take_profit=getattr(scored, "suggested_tp", None),
        metadata={
            "atr": atr, "coin_profile": getattr(profile, "profile", "universal"),
            "pipeline_mode": "capital_allocator_reconcile",
            "total_score": scored.total_score,
            "kelly_size_pct": decision.position_size_pct, "side": side,
            "profile_timeframe": payload["tf"],
            # candle_ts sengaja None -- signal ini dari reconcile, bukan
            # siklus gate3 normal. AMAN: symbol ini sudah pasti ada di
            # registry (reconcile cuma re-attempt kandidat existing), jadi
            # kalau _handle_entry gagal lagi krn kapasitas, register_or_refresh()
            # akan lewat jalur REFRESH (side sama) yang mengabaikan candle_ts
            # baru ini -- baseline TTL asli tetap terjaga, tidak diganti 0.
            "candle_ts": None,
        },
        total_score=scored.total_score, regime=getattr(scored, "regime", "undefined"),
        score_breakdown=getattr(scored, "score_breakdown", {}),
        scoring_narrative=getattr(scored, "scoring_narrative", ""),
    )


# ─── Orkestrasi utama ───────────────────────────────────────────────────────


async def reconcile_pending(bot) -> Dict:
    """Dipanggil dari _do_close_position() (event-driven, primer) dan dari
    run_portfolio_monitor() (polling, fallback/safety-net). Aman dipanggil
    berkali-kali -- no-op murah kalau registry kosong atau tidak ada
    kandidat yang layak dieksekusi.

    bot: instance future.main_future.TradingBot -- butuh atribut
    _pending_candidates, _last_candle_ts, _invalidation_signals, ws_feed,
    exchange, strategy, risk_manager, db, config, portfolio_state,
    _check_gate3_direction(), _handle_entry().
    """
    registry = bot._pending_candidates
    now = _utcnow()
    summary = {
        "purged": 0, "attempted": None, "opened": False,
    }

    if not registry:
        return summary

    last_candle_ts_map = bot._last_candle_ts
    current_prices = {
        sym: float((bot.ws_feed.live_tickers.get(sym, {}) or {}).get("last") or 0) or None
        for sym in registry.keys()
    }
    purged = purge_expired(registry, now, last_candle_ts_map, current_prices)
    summary["purged"] = len(purged)

    if not registry:
        return summary

    # Optimisasi murah: kalau margin bebas masih jauh di bawah order
    # minimum, re-score fresh (mahal, fetch OHLCV+confirmation TF) pasti
    # sia-sia -- skip dulu, tunggu trigger berikutnya.
    free_balance = getattr(bot.risk_manager, "_free_balance", None)
    min_order = getattr(bot.risk_manager, "_min_order_value_usdt", 0.0)
    if free_balance is not None and free_balance < min_order:
        log.debug(
            "[CapitalAllocator] reconcile skip -- free_balance=%.2f < min_order=%.2f",
            free_balance, min_order,
        )
        return summary

    best_long, best_short = pick_best_pair(registry)

    long_result: Optional[Tuple[PendingCandidate, float]] = None
    short_result: Optional[Tuple[PendingCandidate, float]] = None
    long_payload: Optional[dict] = None
    short_payload: Optional[dict] = None

    if best_long is not None:
        status, score, payload = await _rescore_candidate(bot, best_long)
        if status == "executable":
            long_result = (best_long, score)
            long_payload = payload
        elif status == "still_pending":
            register_or_refresh(
                registry, best_long.symbol, "long",
                best_long.profile_name, best_long.profile_timeframe,
                best_long.candle_ts_at_registration, best_long.price_at_registration,
                best_long.atr_at_registration, score if score is not None else best_long.last_score,
                best_long.reason_deferred, now=now,
            )
        else:  # "stale"
            registry.pop(best_long.symbol, None)
            log.info(
                "[CapitalAllocator] DROP %s (long) -- tidak lagi actionable setelah re-score",
                best_long.symbol,
            )

    if best_short is not None:
        status, score, payload = await _rescore_candidate(bot, best_short)
        if status == "executable":
            short_result = (best_short, score)
            short_payload = payload
        elif status == "still_pending":
            register_or_refresh(
                registry, best_short.symbol, "short",
                best_short.profile_name, best_short.profile_timeframe,
                best_short.candle_ts_at_registration, best_short.price_at_registration,
                best_short.atr_at_registration, score if score is not None else best_short.last_score,
                best_short.reason_deferred, now=now,
            )
        else:  # "stale"
            registry.pop(best_short.symbol, None)
            log.info(
                "[CapitalAllocator] DROP %s (short) -- tidak lagi actionable setelah re-score",
                best_short.symbol,
            )

    winner_side = _select_winner(long_result, short_result)
    if winner_side is None:
        return summary

    winner_candidate, winner_score = long_result if winner_side == "long" else short_result
    winner_payload = long_payload if winner_side == "long" else short_payload
    symbol = winner_candidate.symbol

    log.info(
        "[CapitalAllocator] WINNER %s (%s) score=%.1f vs %s -- mencoba entry",
        symbol, winner_side, winner_score,
        (f"short={short_result[1]:.1f}" if winner_side == "long" and short_result
         else f"long={long_result[1]:.1f}" if winner_side == "short" and long_result
         else "tidak ada lawan"),
    )
    summary["attempted"] = (symbol, winner_side)

    signal = _build_entry_signal(bot, symbol, winner_side, winner_payload)
    await bot._handle_entry(signal, winner_side)

    opened = await bot.db.get_open_position_by_symbol(symbol)
    if opened is not None:
        registry.pop(symbol, None)
        summary["opened"] = True
        log.info("[CapitalAllocator] %s (%s) berhasil dibuka via reconcile", symbol, winner_side)
    else:
        log.info(
            "[CapitalAllocator] %s (%s) masih gagal setelah re-attempt (kemungkinan "
            "besar sudah diregistrasi ulang oleh _handle_entry kalau capital-constrained)",
            symbol, winner_side,
        )

    return summary
