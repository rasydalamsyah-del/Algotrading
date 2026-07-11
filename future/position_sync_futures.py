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


async def fetch_binance_futures_positions(exchange) -> List[Dict]:
    """
    Fetch semua posisi terbuka di Binance Futures via exchange.fetch_positions()
    -- endpoint khusus futures, BEDA dari fetch_balance() yang dipakai spot.
    Return list of {symbol, side, amount, entry_price, leverage, margin_mode,
    liquidation_price, unrealized_pnl, usdt_value}.
    """
    try:
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
                "leverage":          pos.get("leverage"),
                "margin_mode":       pos.get("margin_mode"),
                "liquidation_price": pos.get("liquidation_price"),
            })

        log.info("Binance futures: %d posisi aktif ditemukan", len(results))
        return results

    except Exception as e:
        log.error("fetch_binance_futures_positions error: %s", e)
        return []


async def find_untracked_positions(exchange, db_manager) -> List[Dict]:
    """Bandingkan posisi Binance Futures vs DB bot."""
    futures_positions = await fetch_binance_futures_positions(exchange)
    if not futures_positions:
        return []

    db_open = await db_manager.get_open_positions()
    db_symbols = {p.symbol for p in db_open}

    untracked = []
    for pos in futures_positions:
        if pos["symbol"] not in db_symbols:
            untracked.append(pos)
            log.warning(
                "⚠️  Posisi futures tidak tertracking: %s (%s) | amount=%.4f | ~$%.2f USDT | leverage=%s",
                pos["symbol"], pos["side"], pos["amount"], pos["usdt_value"], pos.get("leverage"),
            )
    return untracked


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


async def run_position_sync(exchange, db_manager) -> Dict:
    """Main entry point -- dipanggil periodik dari main_future.py loop."""
    result = {"untracked_found": 0, "adopted": 0, "rejected": 0, "errors": 0}

    try:
        untracked = await find_untracked_positions(exchange, db_manager)
        result["untracked_found"] = len(untracked)

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
