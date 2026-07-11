"""
future/api_server_future.py — REST API + Dashboard server untuk bot futures

Diadaptasi dari spot/api_server_spot.py (2351 baris, ~45 endpoint). BUKAN
1:1 penuh -- endpoint ESENSIAL (status, balance/margin, positions dengan
leverage/liquidation, trades, bot control, config) dibangun dan diadaptasi
dengan benar untuk data model futures. Endpoint yang DIHILANGKAN dengan
sengaja (bukan lupa):

- /api/crosslearn/*, /api/shadow_trades: sistem deprecated permanen
  (dikonfirmasi user), tidak relevan sama sekali di futures.
- /api/meta_learner/*, /api/analytics/*, /api/forecast, /api/universe/*,
  /api/stream (SSE), /api/diagnosa, /api/intelligence/*,
  /api/candles/{symbol}/indicators: BELUM dibangun di pass ini -- fitur
  "nice to have" untuk monitoring mendalam, bukan esensial untuk operasi/
  keamanan dasar. Bisa ditambahkan di sesi terpisah kalau diperlukan.

Perbedaan MENDASAR dari spot (bukan sekadar rename):
- /api/balance: tampilkan free_margin/used_margin/unrealized_pnl (BUKAN
  free_balance/locked_balance/open_pnl seperti spot -- field portfolio_state
  di main_future.py memang berbeda nama & makna).
- _pos_dict(): tambah leverage, margin_mode, liquidation_price,
  mark_price_at_entry, funding_paid_total -- field yang tidak ada di spot.
- /api/bot/panic: pesan & log disesuaikan konteks futures (leverage aktif).
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Depends, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from engine.risk_base import HaltReason

if TYPE_CHECKING:
    from future.main_future import TradingBot

log = logging.getLogger("api_future")
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


class HaltRequest(BaseModel):
    reason: str = "Manual halt via API"


class BotConfigPatchRequest(BaseModel):
    key:   str
    value: Any


class SafeJSONResponse(JSONResponse):
    """[REUSE] JSON response yang aman untuk NaN/Inf."""
    def render(self, content) -> bytes:
        def sanitize(obj):
            if isinstance(obj, float):
                return None if (math.isinf(obj) or math.isnan(obj)) else obj
            if isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sanitize(i) for i in obj]
            return obj
        return json.dumps(sanitize(content), ensure_ascii=False).encode("utf-8")


class _RateLimiter:
    """[REUSE VERBATIM] Token bucket sederhana per IP."""
    def __init__(self, max_calls: int = 120, window_secs: float = 60.0):
        self._max    = max_calls
        self._window = window_secs
        self._hits: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, ip: str) -> bool:
        now    = time.monotonic()
        cutoff = now - self._window
        hits   = self._hits[ip]
        self._hits[ip] = [t for t in hits if t > cutoff]
        if len(self._hits[ip]) >= self._max:
            return False
        self._hits[ip].append(now)
        return True


_rate_limiter = _RateLimiter(max_calls=120, window_secs=60.0)


def _check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.is_allowed(ip):
        raise HTTPException(status_code=429, detail="Rate limit terlampaui. Maksimal 120 request/menit.")


def _get_api_key_from_env() -> str:
    # [FUTURES-SPECIFIC] Key TERPISAH dari spot -- DASHBOARD_API_KEY_FUTURES
    # dengan fallback ke DASHBOARD_API_KEY kalau belum diset terpisah.
    key = os.getenv("DASHBOARD_API_KEY_FUTURES", os.getenv("DASHBOARD_API_KEY", ""))
    if not key or len(key) < 16:
        raise RuntimeError(
            "DASHBOARD_API_KEY_FUTURES (atau DASHBOARD_API_KEY) tidak diset atau "
            "terlalu pendek (min 16 karakter)."
        )
    return key


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    try:
        valid_key = _get_api_key_from_env()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not api_key or not secrets.compare_digest(api_key, valid_key):
        raise HTTPException(status_code=401, detail="API key tidak valid.")
    return api_key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dur(entry_time: Optional[datetime]) -> str:
    if entry_time is None:
        return "00:00:00"
    delta  = _utcnow() - entry_time
    total  = max(int(delta.total_seconds()), 0)
    h, rem = divmod(total, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _pos_dict(pos) -> dict:
    """
    [FUTURES-SPECIFIC] Tambah leverage, margin_mode, liquidation_price,
    mark_price_at_entry, funding_paid_total -- field yang tidak ada sama
    sekali di versi spot.
    """
    return {
        "id":                  pos.id,
        "symbol":              pos.symbol,
        "side":                pos.side,
        "entry_time":          _iso(pos.entry_time),
        "entry_price":         pos.entry_price,
        "current_price":       pos.current_price,
        "amount":              pos.amount,
        "unrealized_pnl":      pos.unrealized_pnl,
        "unrealized_pnl_pct":  pos.unrealized_pnl_pct,
        "realized_pnl":        pos.realized_pnl,
        "realized_pnl_pct":    pos.realized_pnl_pct,
        "stop_loss_price":     pos.stop_loss_price,
        "take_profit_price":   pos.take_profit_price,
        "atr_at_entry":        pos.atr_at_entry,
        "strategy":            pos.strategy_name,
        "profile":             pos.strategy_profile or "",
        "entry_order_id":      pos.entry_order_id,
        "duration_secs":       int((_utcnow() - pos.entry_time).total_seconds() if pos.entry_time else 0),
        "duration_display":    _dur(pos.entry_time),
        "is_open":             pos.is_open,
        "is_closing":          getattr(pos, "is_closing", False),
        "entry_score":         getattr(pos, "entry_score", None),
        "entry_regime":        getattr(pos, "entry_regime", None),
        "highest_price":       getattr(pos, "highest_price", None),
        "exit_time":           _iso(getattr(pos, "exit_time", None)),
        # ── Field futures-specific ──
        "market_type":         getattr(pos, "market_type", "futures"),
        "leverage":            getattr(pos, "leverage", None),
        "margin_mode":         getattr(pos, "margin_mode", None),
        "liquidation_price":   getattr(pos, "liquidation_price", None),
        "mark_price_at_entry": getattr(pos, "mark_price_at_entry", None),
        "funding_paid_total":  getattr(pos, "funding_paid_total", 0.0),
        # Jarak ke liquidation, dihitung on-the-fly kalau data tersedia
        # (berguna utk dashboard tampilkan warning visual)
        "liquidation_distance_pct": (
            round(abs(pos.current_price - pos.liquidation_price) / pos.liquidation_price * 100, 2)
            if pos.current_price and getattr(pos, "liquidation_price", None)
            else None
        ),
    }


def _trade_dict(t) -> dict:
    """[FUTURES-SPECIFIC] Tambah market_type, leverage, margin_mode, realized_funding."""
    return {
        "id":                t.id,
        "timestamp":         _iso(t.timestamp),
        "symbol":            t.symbol,
        "side":              t.side,
        "order_type":        t.order_type,
        "order_id":          getattr(t, "order_id", None),
        "status":            t.status,
        "requested_price":   t.requested_price,
        "executed_price":    t.executed_price,
        "amount":            t.amount,
        "filled":            t.filled,
        "cost":              t.cost,
        "fee_cost":          t.fee_cost,
        "fee_currency":      t.fee_currency,
        "fee_rate":          t.fee_rate,
        "slippage_pct":      t.slippage_pct,
        "stop_loss_price":   t.stop_loss_price,
        "take_profit_price": t.take_profit_price,
        "realized_pnl":      t.realized_pnl,
        "realized_pnl_pct":  t.realized_pnl_pct,
        "strategy":          t.strategy_name,
        "strategy_profile":  getattr(t, "strategy_profile", None),
        "signal_origin":     t.signal_origin,
        "notes":             t.notes,
        # ── Field futures-specific ──
        "market_type":       getattr(t, "market_type", "futures"),
        "leverage":          getattr(t, "leverage", None),
        "margin_mode":       getattr(t, "margin_mode", None),
        "realized_funding":  getattr(t, "realized_funding", None),
    }


def create_app(bot_getter) -> FastAPI:
    app = FastAPI(
        title="AlgoTrader Pro Futures API",
        version="1.0.0",
        description="Real-time dashboard API untuk AlgoTrader Pro -- Binance USDT-M Futures",
        default_response_class=SafeJSONResponse,
        dependencies=[Depends(_check_rate_limit)],
    )

    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        t0       = time.perf_counter()
        response = await call_next(request)
        elapsed  = (time.perf_counter() - t0) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.2f}"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv(
            "ALLOWED_ORIGINS_FUTURES",
            "http://localhost:3000,http://localhost:8001,http://127.0.0.1:8001,http://127.0.0.1:3000",
        ).split(","),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    def bot() -> "TradingBot":
        b = bot_getter()
        if b is None:
            raise HTTPException(status_code=503, detail="Bot futures not initialised")
        return b

    @app.get("/", response_class=HTMLResponse)
    async def landing_page():
        return """
        <html><head><title>AlgoTrader Pro Futures API</title></head>
        <body style="font-family:monospace;background:#0a0a0a;color:#e0e0e0;padding:2rem;">
        <h1>🔮 AlgoTrader Pro — Futures API</h1>
        <p>Binance USDT-M Futures dashboard backend. Endpoint utama:</p>
        <ul>
          <li>GET /health</li>
          <li>GET /api/status</li>
          <li>GET /api/balance 🔑</li>
          <li>GET /api/positions 🔑 (leverage, margin_mode, liquidation_price, liquidation_distance_pct)</li>
          <li>GET /api/positions/{symbol} 🔑</li>
          <li>GET /api/trades 🔑</li>
          <li>GET /api/trades/{symbol} 🔑</li>
          <li>GET /api/equity_curve 🔑</li>
          <li>GET /api/system_health 🔑</li>
          <li>GET /api/config/current 🔑</li>
          <li>POST /api/config/update 🔑</li>
          <li>POST /api/bot/halt 🔑</li>
          <li>POST /api/bot/resume 🔑</li>
          <li>POST /api/bot/pause_strategy 🔑</li>
          <li>POST /api/bot/resume_strategy 🔑</li>
          <li>POST /api/bot/panic 🔑 (tutup semua posisi darurat)</li>
          <li>POST /api/positions/{symbol}/close 🔑</li>
          <li>GET /api/logs 🔑</li>
        </ul>
        <p>🔑 = butuh header X-API-Key</p>
        <p style="color:#ff9800;">⚠️ Fitur belum tersedia di versi ini: meta_learner, analytics,
        forecast, universe management, SSE stream, crosslearn/shadow_trades (deprecated).</p>
        </body></html>
        """

    @app.get("/health")
    async def health():
        return {"status": "ok", "time": _iso(_utcnow()), "version": "1.0.0", "market": "futures"}

    @app.get("/api/status")
    async def get_status():
        b      = bot()
        uptime = int((_utcnow() - b.start_time).total_seconds()) if b.start_time else 0
        halted = b.risk_manager.is_halted if b.risk_manager else False
        halt_reason = b.risk_manager.halt_reason if b.risk_manager else ""
        return {
            "status":             "running" if b.is_running else "stopped",
            "halted":             halted,
            "halt_reason":        halt_reason,
            "exchange":           b.config.get("exchange_id"),
            "market":             "futures",
            "testnet":            b.config.get("testnet"),
            "connected":          b.exchange.is_connected if b.exchange else False,
            "strategy":           b.strategy.name if b.strategy else None,
            "strategy_active":    b.strategy.is_active if b.strategy else False,
            "universe_watchlist": b.config.get("universe_watchlist", []),
            "timeframe":          b.config.get("timeframe"),
            "default_leverage":   b.config.get("default_leverage"),
            "max_leverage":       b.config.get("max_leverage"),
            "margin_mode":        b.config.get("margin_mode"),
            "enable_short":       b.config.get("enable_short"),
            "uptime_secs":        uptime,
            "uptime_display":     str(timedelta(seconds=uptime)),
            "timestamp":          _iso(_utcnow()),
        }

    @app.get("/api/balance")
    async def get_balance(_: str = Depends(verify_api_key)):
        """[FUTURES-SPECIFIC] free_margin/used_margin/unrealized_pnl, BUKAN
        free_balance/locked_balance/open_pnl seperti spot."""
        b  = bot()
        ps = b.portfolio_state
        drawdown_pct = b.risk_manager.current_drawdown_pct if b.risk_manager else 0.0
        return {
            "total_equity":   ps.get("total_equity", 0),
            "free_margin":    ps.get("free_margin", 0),
            "used_margin":    ps.get("used_margin", 0),
            "unrealized_pnl": ps.get("unrealized_pnl", 0),
            "daily_pnl":      ps.get("daily_pnl", 0),
            "daily_pnl_pct":  ps.get("daily_pnl_pct", 0),
            "drawdown_pct":   drawdown_pct,
            "currency":       b.config.get("quote_currency", "USDT"),
            "timestamp":      _iso(_utcnow()),
        }

    @app.get("/api/positions")
    async def get_positions(_: str = Depends(verify_api_key)):
        b         = bot()
        positions = await b.db.get_open_positions()
        return {"positions": [_pos_dict(p) for p in positions], "count": len(positions)}

    @app.get("/api/positions/{symbol:path}")
    async def get_position_by_symbol(symbol: str, _: str = Depends(verify_api_key)):
        b         = bot()
        positions = await b.db.get_open_positions()
        sym       = urllib.parse.unquote(symbol).upper()
        matched   = [p for p in positions if p.symbol == sym]
        if not matched:
            raise HTTPException(status_code=404, detail=f"Posisi {sym} tidak ditemukan")
        return {"position": _pos_dict(matched[0]), "timestamp": _iso(_utcnow())}

    @app.get("/api/trades")
    async def get_trades(limit: int = 50, offset: int = 0, _: str = Depends(verify_api_key)):
        b      = bot()
        trades = await b.db.get_recent_trades(limit=min(limit + offset, 500))
        page   = trades[offset: offset + limit]
        return {"trades": [_trade_dict(t) for t in page], "count": len(page), "total": len(trades), "offset": offset}

    @app.get("/api/trades/{symbol:path}")
    async def get_trades_by_symbol(symbol: str, limit: int = 50, _: str = Depends(verify_api_key)):
        b      = bot()
        sym    = urllib.parse.unquote(symbol).upper()
        trades = await b.db.get_recent_trades(limit=200)
        matched = [t for t in trades if t.symbol == sym][:limit]
        return {"trades": [_trade_dict(t) for t in matched], "count": len(matched)}

    @app.get("/api/equity_curve")
    async def get_equity_curve(limit: int = 500, _: str = Depends(verify_api_key)):
        b = bot()
        snapshots = await b.db.get_equity_curve(limit=limit)
        return {
            "points": [
                {
                    "timestamp": _iso(s.timestamp), "equity": s.total_equity,
                    "drawdown_pct": s.drawdown_pct,
                    "free_balance": s.free_balance, "locked_balance": s.locked_balance,
                }
                for s in snapshots
            ],
            "count": len(snapshots),
        }

    @app.get("/api/system_health")
    async def system_health(_: str = Depends(verify_api_key)):
        b = bot()
        health = b.risk_manager.get_system_health() if b.risk_manager else {}
        health["market"] = "futures"
        health["default_leverage"] = b.config.get("default_leverage")
        health["margin_mode"] = b.config.get("margin_mode")
        return health

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, level: Optional[str] = None, _: str = Depends(verify_api_key)):
        b = bot()
        logs = await b.db.get_recent_logs(limit=limit if not level else min(limit * 3, 500))
        if level:
            logs = [l for l in logs if l.level.upper() == level.upper()][:limit]
        return {"logs": [
            {"timestamp": _iso(l.timestamp), "level": l.level, "source": l.source, "message": l.message}
            for l in logs
        ], "count": len(logs)}

    @app.get("/api/config/current")
    async def get_current_config(_: str = Depends(verify_api_key)):
        b = bot()
        safe_config = {
            k: v for k, v in b.config.items()
            if k not in ("api_key", "api_secret", "telegram_bot_token", "smtp_password")
        }
        return {"config": safe_config, "timestamp": _iso(_utcnow())}

    @app.post("/api/config/update")
    async def update_config(payload: dict, _: str = Depends(verify_api_key)):
        """[REUSE pola spot] set_bot_state('config_update', json) -- dibaca
        & diterapkan oleh run_config_watcher() di main_future.py tiap 30 detik."""
        import json as _json
        b = bot()
        try:
            allowed = [
                "universe_watchlist", "max_open_positions", "max_drawdown_pct",
                "risk_per_trade_pct", "daily_loss_limit_pct", "max_position_size_pct",
                "stop_loss_pct", "take_profit_pct", "atr_multiplier_sl", "atr_multiplier_tp",
                "trailing_atr_mult", "use_trailing_stop", "telegram_enabled",
                "min_order_value_usdt", "max_slippage_pct", "rsi_min", "rsi_max",
                "lookback_candles", "paper_trading_mode",
                # [FUTURES-SPECIFIC] key tambahan yang tidak ada di spot
                "default_leverage", "max_leverage", "margin_mode",
                "maintenance_margin_rate", "min_liquidation_safety_pct", "enable_short",
            ]
            updates = {k: v for k, v in payload.items() if k in allowed}
            if not updates:
                return {"success": False, "message": "Tidak ada field valid untuk diupdate"}
            await b.db.set_bot_state("config_update_futures", _json.dumps(updates))
            return {"success": True, "message": "Config akan diupdate dalam 30 detik", "fields": list(updates.keys())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/bot/halt")
    async def halt_bot(req: HaltRequest, _: str = Depends(verify_api_key)):
        bot().risk_manager.halt_trading(HaltReason.MANUAL, req.reason or "Manual halt via API (futures)")
        return {"status": "halted", "reason": req.reason}

    @app.post("/api/bot/resume")
    async def resume_bot(_: str = Depends(verify_api_key)):
        bot().risk_manager.resume_trading()
        return {"status": "running"}

    @app.post("/api/bot/pause_strategy")
    async def pause_strategy(_: str = Depends(verify_api_key)):
        b = bot()
        if b.strategy:
            b.strategy.pause()
        return {"status": "strategy_paused"}

    @app.post("/api/bot/resume_strategy")
    async def resume_strategy(_: str = Depends(verify_api_key)):
        b = bot()
        if b.strategy:
            b.strategy.resume()
        return {"status": "strategy_running"}

    @app.post("/api/bot/panic")
    async def panic_close_all(_: str = Depends(verify_api_key)):
        """
        [FUTURES-SPECIFIC] Sama seperti spot secara struktur, tapi pesan &
        konteks disesuaikan (leverage aktif, risiko lebih tinggi krn margin-based).
        """
        b = bot()
        log.critical("PANIC BUTTON ACTIVATED (FUTURES, leverage aktif) — closing all positions!")
        await b.db.save_log("CRITICAL", "api_future", "PANIC BUTTON: closing all open futures positions")

        positions    = await b.db.get_open_positions()
        closed_count = 0
        failed: list = []

        for pos in positions:
            try:
                price = await b._get_current_price(pos.symbol)
                if not price:
                    ticker = await b.exchange.fetch_ticker(pos.symbol)
                    price  = ticker.get("last") or pos.current_price or pos.entry_price
                await b._close_position_market(pos, float(price), "PANIC BUTTON")
                closed_count += 1
            except Exception as e:
                log.error("Panic close FAILED for %s: %s", pos.symbol, e)
                failed.append(pos.symbol)

        b.risk_manager.halt_trading(HaltReason.PANIC_BUTTON, "Manual emergency close from dashboard (futures)")

        try:
            await b.notifier.notify_panic(
                positions_found=len(positions), closed_count=closed_count, failed_symbols=failed,
            )
        except Exception as _np_err:
            log.warning("notify_panic gagal terkirim (non-fatal): %s", _np_err)

        return {
            "status": "panic_executed", "positions_found": len(positions),
            "closed_count": closed_count, "failed_symbols": failed,
            "halted": True, "timestamp": _iso(_utcnow()),
        }

    @app.post("/api/positions/{symbol}/close")
    async def close_position(symbol: str, _: str = Depends(verify_api_key)):
        symbol = urllib.parse.unquote(symbol)
        b = bot()
        try:
            pos = await b.db.get_open_position_by_symbol(symbol)
            if not pos:
                return {"success": False, "message": f"Posisi {symbol} tidak ditemukan atau sudah closed"}
            price = await b._get_current_price(symbol)
            if not price:
                ticker = await b.exchange.fetch_ticker(symbol)
                price = ticker.get("last") or pos.current_price or pos.entry_price
            await b._close_position_market(pos, float(price), "MANUAL_CLOSE_DASHBOARD")
            return {"success": True, "message": f"Posisi {symbol} berhasil ditutup @ {price}"}
        except Exception as e:
            log.error("Manual close error [%s]: %s", symbol, e)
            return {"success": False, "error": str(e)}

    return app
