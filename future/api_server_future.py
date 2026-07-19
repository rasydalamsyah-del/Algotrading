"""
future/api_server_future.py — REST API + Dashboard server untuk bot futures

Diadaptasi dari spot/api_server_spot.py (2385 baris, ~45 endpoint). BUKAN
1:1 penuh -- endpoint ESENSIAL (status, balance/margin, positions dengan
leverage/liquidation, trades, bot control, config) dibangun dan diadaptasi
dengan benar untuk data model futures. Endpoint yang DIHILANGKAN dengan
sengaja (bukan lupa):

- /api/crosslearn/*, /api/shadow_trades: sistem deprecated permanen
  (dikonfirmasi user), tidak relevan sama sekali di futures. shadow_trades
  KHUSUS: dikonfirmasi dead code juga di spot (getattr(b,"_shadow_positions",{})
  -- atribut itu tidak pernah di-assign di manapun di main_spot.py, endpoint
  selalu return kosong), jadi sengaja TIDAK di-port sama sekali, bukan lupa.
[AUDIT ITEM #8 -- SELESAI] /api/stream (SSE) sekarang ADA, genuinely
event-driven (bukan polling berbalut SSE spt versi awal spot) -- subscribe
ke b.event_bus (EventBus in-process, engine/event_bus.py), dipublish dari
titik Tier 1 (engine/database.py: save_trade/upsert_position/
close_position/mark_position_closing/upsert_universe_override/
deactivate_universe_override/save_parameter_change), transisi halt/resume
(engine/risk_base.py), Tier 2 teragregasi (positions_snapshot per cycle,
snapshot equity), dan ticker (WebSocketFeed.on_ticker, throttled 1.5s/
symbol -- hook itu SUDAH ADA sejak awal tapi TIDAK PERNAH disambungkan ke
apa pun sebelum ini). 2 bus (spot & futures) genuinely terisolasi -- 2
proses OS terpisah, klien buka 2 EventSource terpisah (tidak ada gateway/
proxy dibangun).

[AUDIT ITEM #7 -- SELESAI, 4 langkah] Endpoint yang tadinya "belum
dibangun" sekarang genuinely ada:
- Langkah 1: /api/candles/{symbol}/indicators, /api/orderbook/{symbol},
  /api/market_info/{symbol} -- straight port + 1 bug ditemukan&dihindari
  (_get_ob_danger_level dipanggil salah argumen di versi spot, endpoint
  itu SELALU 502 di sana -- lihat test_api_server_future_new_endpoints.py).
- Langkah 2: POST /api/universe/add (+validasi is_symbol_supported(),
  TIDAK ADA di spot) & /api/universe/remove (sengaja tanpa validasi).
- Langkah 3: /api/analytics/*, /api/meta_learner/* -- prasyarat
  PerformanceAnalytics/MetaLearner di-wire di main_future.py::
  _initialize_intelligence_pipeline() (lihat
  test_main_future_analytics_meta_learner.py). MetaLearner futures
  diinstansiasi dgn market_type="futures" -- suggestion tipe weight_*
  DIBLOKIR teknis (bukan cuma dokumentasi) krn engine/profiles/weights.py
  adalah file SHARED dgn spot, approve suggestion weight dari futures bisa
  diam-diam mengubah scoring live spot saat spot restart.
- Langkah 4: /api/forecast, /api/diagnosa -- GENUINELY BIDIRECTIONAL
  (keputusan desain dikonfirmasi user, BUKAN long-only apa adanya). Versi
  spot memanggil get_latest_signal_score(symbol)/get_cached_observation(
  symbol, tf) TANPA side -- untuk symbol yg di-scoring long DAN short tiap
  siklus, itu mengambil row/cache APA PUN yg kebetulan terakhir, lalu
  diinterpretasi 100% sbg sinyal long (ema_bullish, probability_up) --
  berpotensi aktif menyesatkan. Di sini fetch side="long" & side="short"
  terpisah, 1 entri per side. Prasyarat yg juga diperbaiki:
  engine/intelligence/scorer.py::_suggest_sl_tp() sebelumnya hardcode
  formula long (SL selalu di bawah harga) regardless side -- sekarang
  side-aware (lihat test_scorer_suggest_sl_tp_side_aware.py).

[BATCH 1 -- PORTING] /api/tickers, /api/intelligence/regime,
/api/intelligence/scores(+{symbol}), /api/candles/{symbol} -- di-porting
verbatim dari spot/api_server_spot.py, murni baca DB/ws_feed/exchange yang
sudah shared (DatabaseManager, WebSocketFeed, BaseExchangeConnector), tidak
ada konsep spot-only yang perlu disesuaikan.

Perbedaan MENDASAR dari spot (bukan sekadar rename):
- /api/balance: tampilkan free_margin/used_margin/unrealized_pnl (BUKAN
  free_balance/locked_balance/open_pnl seperti spot -- field portfolio_state
  di main_future.py memang berbeda nama & makna).
- _pos_dict(): tambah leverage, margin_mode, liquidation_price,
  mark_price_at_entry, funding_paid_total -- field yang tidak ada di spot.
- /api/bot/panic: pesan & log disesuaikan konteks futures (leverage aktif).
"""

from __future__ import annotations

import asyncio
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

try:
    import engine.ta_compat  # noqa: F401 -- registrasi accessor df.ta
    from engine.ta_compat import lookup_col
except ImportError:
    def lookup_col(bar, *cols, default=0.0):  # type: ignore[misc]
        return default

import pandas as pd
from fastapi import FastAPI, HTTPException, Depends, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from engine.risk_base import HaltReason
from engine.profiles.thresholds import get_dynamic_threshold, DYNAMIC_THRESHOLD_MATRIX, ENTRY_THRESHOLDS
from engine.profiles.registry import select_profile_from_indicators, get_coin_profile
from engine.profiles.weights import LEVEL1_WEIGHTS
from engine.profiles.base_profile import PROFILE_EMOJI
from engine.constants import COL_EMA9, COL_EMA21, COL_EMA50, COL_RSI, COL_ATR
from engine.indicators.orderbook import WhaleDetector
from engine.event_bus import serialize_event

if TYPE_CHECKING:
    from future.main_future import TradingBot

log = logging.getLogger("api_future")
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

# [PORT #7 langkah 4/4 -- audit item #19] Sama persis dgn spot/api_server_spot.py
# (nilai identik, dipindah ke sini sengaja bukan diekstrak ke engine/ --
# keduanya table kecil berbasis nama profile/timeframe, genuinely
# market-agnostic, duplikasi kecil ini lebih murah drpd refactor lintas modul).
FORECAST_HOLD_MINUTES_MATRIX: Dict[str, Dict[str, int]] = {
    "scalp_volatile":   {"trending_bull": 30, "volatile_expansion": 20, "ranging": 45, "undefined": 35},
    "extreme_momentum": {"trending_bull": 25, "volatile_expansion": 15, "ranging": 60, "undefined": 30},
    "breakout_swift":   {"trending_bull": 120, "volatile_expansion": 90, "ranging": 180, "undefined": 150},
    "trend_follow":     {"trending_bull": 480, "volatile_expansion": 300, "ranging": 600, "undefined": 360},
    "mean_revert":      {"trending_bull": 240, "volatile_expansion": 300, "ranging": 120, "undefined": 180},
    "hodl_accumulate":  {"trending_bull": 2880, "volatile_expansion": 4320, "ranging": 1440, "undefined": 2160},
}

FORECAST_TF_CONFIRM: Dict[str, str] = {
    "15m": "1h", "30m": "2h", "1h": "4h", "5m": "15m",
}

DIAGNOSA_TF_FALLBACK: Dict[str, List[str]] = {
    "1d":  ["4h", "1h"],
    "4h":  ["1h"],
    "1h":  ["15m"],
    "15m": [],
}


class HaltRequest(BaseModel):
    reason: str = "Manual halt via API"


class BotConfigPatchRequest(BaseModel):
    key:   str
    value: Any


class UniverseAddRequest(BaseModel):
    symbol: str
    notes:  Optional[str] = None


class UniverseRemoveRequest(BaseModel):
    symbol: str


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


class _MetricCache:
    """[REUSE VERBATIM dari spot] Cache hasil get_metrics selama TTL detik.
    Cegah kalkulasi Sharpe/Sortino/Calmar tiap request saat dashboard polling."""
    def __init__(self, ttl: float = 10.0):
        self._ttl    = ttl
        self._ts:    float = 0.0
        self._value: Optional[Dict] = None

    def get(self) -> Optional[Dict]:
        if self._value and (time.monotonic() - self._ts) < self._ttl:
            return self._value
        return None

    def set(self, value: Dict) -> None:
        self._value = value
        self._ts    = time.monotonic()


_metrics_cache = _MetricCache(ttl=10.0)


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


def _build_forecast_entry(row, side: str, tf_primary: str, indicators: dict, conf_tf_data: dict) -> Optional[dict]:
    """[PORT #7 langkah 4/4 -- audit item #19] Satu entri forecast utk SATU
    side (dipanggil 2x per symbol dari endpoint, side="long" & "short").

    [Keputusan desain, dikonfirmasi user] Genuinely bidirectional -- BUKAN
    long-only apa adanya. `row` HARUS sudah hasil
    `db.get_latest_signal_score(symbol, side=side)` (row TERPISAH per side,
    bukan "row terakhir apapun sisinya" spt versi spot). Field skor
    (row.trend_score dkk) SUDAH side-aware dari sono -- score_signal()
    menghitungnya lewat _pick_side_score() per kategori (dikonfirmasi baca
    kode engine/intelligence/scorer.py::_calc_weighted_breakdown), jadi
    TIDAK perlu baca composite_score_short terpisah di sini utk breakdown.
    Yang genuinely perlu penyesuaian arah: SL/TP di atas/bawah harga
    (side-aware sejak bug-fix _suggest_sl_tp()), interpretasi EMA stack
    (bullish vs bearish), dan pelabelan trend_summary/probability."""
    if not row or not row.current_price:
        return None

    price = row.current_price
    sl    = row.suggested_sl
    tp    = row.suggested_tp
    regime  = row.regime or "undefined"
    profile = row.strategy_profile or "scalp_volatile"
    conf    = row.signal_confidence or 0.5
    score   = row.total_score or 0.0

    if side == "short":
        potential_profit_pct = round((price - tp) / price * 100, 2) if tp and price else None
        potential_loss_pct   = round((sl - price) / price * 100, 2) if sl and price else None
    else:
        potential_profit_pct = round((tp - price) / price * 100, 2) if tp and price else None
        potential_loss_pct   = round((price - sl) / price * 100, 2) if sl and price else None
    rr_ratio = round(potential_profit_pct / potential_loss_pct, 2) if potential_profit_pct and potential_loss_pct and potential_loss_pct > 0 else None

    score_pct      = min(100, max(0, score))
    threshold_used = row.threshold_used or DYNAMIC_THRESHOLD_MATRIX.get(profile, {}).get(regime, ENTRY_THRESHOLDS.get(profile, 65.0))
    probability_favorable_pct = round((score_pct / 100 * 0.6 + conf * 0.25 + min(1.0, score_pct / max(threshold_used, 1)) * 0.15) * 100, 1)
    signal_quality = "excellent" if score_pct >= 85 else "good" if score_pct >= 70 else "fair" if score_pct >= 50 else "poor"

    score_breakdown = {
        "trend":      row.trend_score,
        "momentum":   row.momentum_score,
        "strength":   row.strength_score,
        "volatility": row.volatility_score,
        "pattern":    row.pattern_score,
        "oscillator": row.oscillator_score,
        "structure":  row.structure_score,
        "orderbook":  row.orderbook_score,
    }
    weights = LEVEL1_WEIGHTS.get(profile, {})
    dyn_threshold = DYNAMIC_THRESHOLD_MATRIX.get(profile, {}).get(regime, threshold_used)
    threshold_gap = round(score - dyn_threshold, 2)

    ema9  = indicators.get("ema9")
    ema21 = indicators.get("ema21")
    ema50 = indicators.get("ema50")
    ema_stack_aligned = (
        (ema9 or 0) > (ema21 or 0) > (ema50 or 0) if side == "long"
        else (ema9 or 0) < (ema21 or 0) < (ema50 or 0)
    )
    rsi_val   = indicators.get("rsi")
    rsi_slope = indicators.get("rsi_slope", 0) or 0
    if side == "short":
        trend_summary = (
            "Bearish kuat" if ema_stack_aligned and (rsi_val or 100) < 45 else
            "Bearish lemah" if ema_stack_aligned else
            "Sideways" if abs(rsi_slope) < 0.5 else
            "Bullish"
        )
    else:
        trend_summary = (
            "Bullish kuat" if ema_stack_aligned and (rsi_val or 0) > 55 else
            "Bullish lemah" if ema_stack_aligned else
            "Sideways" if abs(rsi_slope) < 0.5 else
            "Bearish"
        )

    htf_label = FORECAST_TF_CONFIRM.get(tf_primary, "1h")
    htf_direction = None
    if conf_tf_data:
        htf_rsi  = conf_tf_data.get("rsi")
        htf_bull = conf_tf_data.get("ema_bullish")
        if htf_rsi is not None and htf_bull is not None:
            htf_direction = "bullish" if htf_bull and htf_rsi > 50 else "bearish" if not htf_bull else "neutral"
    confirm_tf_result = None
    if htf_direction is not None:
        wants = "bullish" if side == "long" else "bearish"
        confirm_tf_result = "confirms" if htf_direction == wants else (
            "neutral" if htf_direction == "neutral" else "conflicts"
        )

    now_utc    = _utcnow()
    hold_map   = FORECAST_HOLD_MINUTES_MATRIX.get(profile, {})
    hold_mins  = hold_map.get(regime, 60)
    hold_mins  = int(hold_mins * (0.7 + conf * 0.6))
    hold_mins  = max(10, hold_mins)
    tp_eta_utc = now_utc + timedelta(minutes=hold_mins)
    tp_eta_wib = tp_eta_utc + timedelta(hours=7)
    hold_display = (
        f"{hold_mins} menit" if hold_mins < 60
        else f"{hold_mins // 60}j {hold_mins % 60}m" if hold_mins % 60
        else f"{hold_mins // 60} jam"
    )

    return {
        "side":                 side,
        "strategy_profile":     profile,
        "timeframe":            tf_primary,
        "confirm_tf":           htf_label,
        "current_price":        price,
        "suggested_sl":         sl,
        "suggested_tp":         tp,
        "nearest_support":      row.nearest_support,
        "nearest_resistance":   row.nearest_resistance,
        "fib_support":          row.fib_support,
        "fib_resistance":       row.fib_resistance,
        "potential_profit_pct": potential_profit_pct,
        "potential_loss_pct":   potential_loss_pct,
        "rr_ratio":             rr_ratio,
        "total_score":          round(score, 2),
        "threshold_used":       round(dyn_threshold, 1),
        "threshold_gap":        threshold_gap,
        "probability_favorable_pct": probability_favorable_pct,
        "signal_quality":       signal_quality,
        "signal_confidence":    round(conf, 3),
        "trigger_met":          row.trigger_met,
        "score_breakdown":      {k: round(v, 1) if v else None for k, v in score_breakdown.items()},
        "category_weights":     {k: round(v * 100, 1) for k, v in weights.items()},
        "regime":               regime,
        "regime_confidence":    row.regime_confidence,
        "indicators":           indicators,
        "ema_stack_aligned":    ema_stack_aligned,
        "confirm_tf_data":      conf_tf_data,
        "confirm_tf_direction": htf_direction,
        "confirm_tf_result":    confirm_tf_result,
        "trend_summary":        trend_summary,
        "hold_minutes":         hold_mins,
        "hold_display":         hold_display,
        "tp_eta_wib":           tp_eta_wib.strftime("%H:%M WIB"),
        "tp_eta_date":          tp_eta_wib.strftime("%d/%m %H:%M WIB"),
        "last_updated":         _iso(row.timestamp),
        "probability_note":     "Composite: score(60%) + confidence(25%) + threshold_ratio(15%)",
    }


def _diagnosa_entry_from_row(row, side: str, open_position=None) -> dict:
    """[PORT #7 langkah 4/4 -- audit item #19] Jalur PRIMER /api/diagnosa,
    genuinely bidirectional. BEDA STRUKTURAL dari spot: spot pakai
    get_cached_observation(symbol, tf) + ind.trend.composite_score dkk --
    fungsi itu (engine/intelligence/observer.py::get_cached_observation)
    TIDAK PUNYA parameter side sama sekali, cuma ambil entry cache
    ter-update (bisa long ATAU short, tidak deterministik dari sisi
    caller) -- ambiguitas sisi yang SAMA PERSIS dgn root cause bug
    /api/forecast. Di sini SENGAJA TIDAK dipakai -- pakai
    get_latest_signal_score(symbol, side=side) sbg satu-satunya sumber
    (row terpisah per side, field skor SUDAH side-aware, sama dgn
    _build_forecast_entry()). Konsekuensi: field `narrative` &
    `calculation_errors` (ada di versi spot, sumbernya observation cache)
    TIDAK disertakan di sini -- bukan lupa, sengaja dihindari krn sumbernya
    ambigu sisi. Bisa ditambah nanti KALAU get_cached_observation()
    diperluas dgn parameter side (perubahan terpisah, di luar scope ini,
    menyentuh engine/intelligence/observer.py yang dipakai spot juga)."""
    breakdown = {
        "trend":      row.trend_score,
        "momentum":   row.momentum_score,
        "strength":   row.strength_score,
        "volatility": row.volatility_score,
        "pattern":    row.pattern_score,
        "oscillator": row.oscillator_score,
        "structure":  row.structure_score,
        "orderbook":  row.orderbook_score,
    }
    entry = {
        "side":              side,
        "profile":           row.strategy_profile or "scalp_volatile",
        "regime":            row.regime or "undefined",
        "regime_confidence": row.regime_confidence,
        "total_score":       row.total_score,
        "trigger_met":       row.trigger_met,
        "threshold":         row.threshold_used,
        "breakdown":         breakdown,
        "last_updated":      _iso(row.timestamp),
        "source":            "database",
    }
    if open_position is not None:
        entry["open_position"] = {
            "entry_score":       getattr(open_position, "entry_score", None),
            "current_score":     row.total_score,
            "score_delta": (
                round(row.total_score - open_position.entry_score, 2)
                if row.total_score is not None and getattr(open_position, "entry_score", None) is not None
                else None
            ),
            "entry_price":       open_position.entry_price,
            "unrealized_pnl_pct": open_position.unrealized_pnl_pct,
        }
    return entry


def _diagnosa_fallback_entry(df: "pd.DataFrame", side: str, prof, is_testnet: bool, tf_used: str, tf_note: str) -> Optional[dict]:
    """[PORT #7 langkah 4/4 -- audit item #19] Jalur FALLBACK (dipakai HANYA
    saat belum ada SignalScore row sama sekali utk side ini -- symbol yang
    bot belum pernah scoring). Mirror manual dari versi spot (golden_cross/
    ema9>ema21>ema50/dst), side="short" MEMBALIK arah tiap kondisi.

    [CATATAN JUJUR -- BEDA dari sub-indikator pipeline utama] Formula long
    di sini adalah REPLIKASI LANGSUNG dari spot/api_server_spot.py (tidak
    diubah). Formula short adalah MIRROR MANUAL yang saya susun sendiri di
    langkah ini -- BUKAN hasil ekstraksi dari scoring pipeline side-aware
    yang sudah diverifikasi fuzz-test (beda dgn score_trend()/score_momentum()
    dkk di indicators/*.py yang sudah lewat proses verifikasi ketat proyek
    MTF Composite Side-Aware). Satu asumsi eksplisit yang TIDAK punya
    padanan field profile: rsi_gc_min (ambang RSI minimum utk konfirmasi
    golden-cross bullish) di-mirror sbg `100 - rsi_gc_min` utk konfirmasi
    death-cross bearish -- pilihan simetris di sekitar 50, BUKAN dikalibrasi
    empiris spt threshold lain di profile. Jalur ini HANYA dipakai sbg
    fallback langka (symbol belum pernah discoring sama sekali) -- kalau
    perilakunya perlu dipercaya utk keputusan trading, verifikasi/kalibrasi
    ulang terpisah direkomendasikan sebelum diandalkan."""
    if len(df) < 5:
        return None

    bar_row  = df.iloc[-2]
    prev_row = df.iloc[-3]

    close   = float(bar_row["close"])
    ema9    = float(bar_row[COL_EMA9])
    ema21   = float(bar_row[COL_EMA21])
    ema50   = float(bar_row[COL_EMA50])
    rsi     = float(bar_row[COL_RSI])
    atr     = float(bar_row[COL_ATR])
    atr_pct = (atr / close * 100) if close > 0 else 0

    prev_ema9  = float(prev_row[COL_EMA9])
    prev_ema21 = float(prev_row[COL_EMA21])

    resist = float(bar_row["_resistance"]) if pd.notna(bar_row.get("_resistance")) else close
    support = float(bar_row["_support"]) if pd.notna(bar_row.get("_support")) else close
    vol_ma_v = (
        float(bar_row["_vol_ma"])
        if pd.notna(bar_row.get("_vol_ma")) and float(bar_row["_vol_ma"]) > 0
        else float(df["quote_volume"].mean())
    )
    vol       = float(bar_row["quote_volume"]) if pd.notna(bar_row.get("quote_volume")) else float(bar_row["volume"])
    vol_ratio = vol / vol_ma_v if vol_ma_v > 0 else 0.0
    vol_warn  = " ⚠️sandbox" if (is_testnet and vol_ma_v < 1.0) else ""

    min_dist = close * (prof.min_breakout_pct / 100)
    cond_vwap = True
    if "VWAP_D" in bar_row.index or "VWAP" in bar_row.index or "vwap" in bar_row.index:
        for vwap_col in ("VWAP_D", "VWAP", "vwap"):
            if vwap_col in bar_row.index and pd.notna(bar_row[vwap_col]):
                vwap_val = float(bar_row[vwap_col])
                if vwap_val > 0:
                    cond_vwap = (close > vwap_val) if side == "long" else (close < vwap_val)
                break

    failed_conditions = []
    if side == "short":
        brk_dist     = (support - close) if support > 0 else 0.0
        trigger_a    = (brk_dist >= min_dist) and (vol_ratio >= prof.volume_mult)
        death_cross  = (prev_ema9 >= prev_ema21) and (ema9 < ema21)
        trigger_b    = death_cross and (rsi < (100 - prof.rsi_gc_min))
        cond_trend   = ema9 < ema21 < ema50
        cond_momentum = prof.rsi_min <= rsi <= prof.rsi_max
        entry_ok = (trigger_a or trigger_b) and cond_trend and cond_momentum and cond_vwap
        if not (trigger_a or trigger_b):
            failed_conditions.append(f"NoTrig(vol={vol_ratio:.1f}x,dc={'✅' if death_cross else '❌'})")
        if not cond_trend:
            failed_conditions.append("EMAStack")
        if not cond_momentum:
            failed_conditions.append(f"RSI({rsi:.0f} not in [{prof.rsi_min},{prof.rsi_max}])")
        if not cond_vwap:
            failed_conditions.append("AboveVWAP")
        cond_count = sum([bool(trigger_a or trigger_b), cond_trend, cond_momentum, cond_vwap])
        if atr > 0:
            sl_val = close + max(atr * prof.atr_sl_mult, close * (prof.quick_sl_pct / 100))
            tp_val = close - max(atr * prof.atr_tp_mult, close * (prof.quick_tp_pct / 100))
        else:
            sl_val = close * (1 + prof.quick_sl_pct / 100)
            tp_val = close * (1 - prof.quick_tp_pct / 100)
    else:
        brk_dist     = (close - resist) if resist > 0 else 0.0
        trigger_a    = (brk_dist >= min_dist) and (vol_ratio >= prof.volume_mult)
        golden_cross = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
        trigger_b    = golden_cross and (rsi > prof.rsi_gc_min)
        cond_trend   = ema9 > ema21 > ema50
        cond_momentum = prof.rsi_min <= rsi <= prof.rsi_max
        entry_ok = (trigger_a or trigger_b) and cond_trend and cond_momentum and cond_vwap
        if not (trigger_a or trigger_b):
            failed_conditions.append(f"NoTrig(vol={vol_ratio:.1f}x,gc={'✅' if golden_cross else '❌'})")
        if not cond_trend:
            failed_conditions.append("EMAStack")
        if not cond_momentum:
            failed_conditions.append(f"RSI({rsi:.0f} not in [{prof.rsi_min},{prof.rsi_max}])")
        if not cond_vwap:
            failed_conditions.append("BelowVWAP")
        cond_count = sum([bool(trigger_a or trigger_b), cond_trend, cond_momentum, cond_vwap])
        if atr > 0:
            sl_val = close - max(atr * prof.atr_sl_mult, close * (prof.quick_sl_pct / 100))
            tp_val = close + max(atr * prof.atr_tp_mult, close * (prof.quick_tp_pct / 100))
        else:
            sl_val = close * (1 - prof.quick_sl_pct / 100)
            tp_val = close * (1 + prof.quick_tp_pct / 100)

    exit_mode = (
        "RIDE_THE_WAVE"
        if (vol_ratio >= prof.volume_spike or atr_pct >= prof.atr_pct_threshold)
        else "QUICK_PROFIT"
    )
    prof_emoji = PROFILE_EMOJI.get(prof.profile.value, "⚙️")

    return {
        "side":             side,
        "profile":          f"{prof_emoji} {prof.profile.value}",
        "regime":           "undefined",
        "total_score":      None,
        "threshold":        getattr(prof, "entry_threshold", None) or getattr(prof, "min_score", None) or 70.0,
        "trigger_met":      entry_ok,
        "conditions_met":   cond_count,
        "conditions_total": 4,
        "failed_conditions": failed_conditions,
        "price":            close,
        "sl":               round(sl_val, 8),
        "tp":               round(tp_val, 8),
        "rsi":              round(rsi, 2),
        "vol_ratio":        round(vol_ratio, 2),
        "atr_pct":          round(atr_pct, 4),
        "vol_warn":         bool(vol_warn),
        "exit_mode":        exit_mode,
        "tf_used":          tf_used,
        "tf_note":          tf_note,
        "source":           "fallback_v6",
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
          <li>GET /api/tickers</li>
          <li>GET /api/intelligence/regime 🔑</li>
          <li>GET /api/intelligence/scores 🔑</li>
          <li>GET /api/intelligence/scores/{symbol} 🔑</li>
          <li>GET /api/candles/{symbol} 🔑</li>
          <li>GET /api/candles/{symbol}/indicators 🔑</li>
          <li>GET /api/orderbook/{symbol} 🔑</li>
          <li>GET /api/market_info/{symbol}</li>
          <li>GET /api/dashboard_snapshot 🔑</li>
          <li>GET /api/executor/stats 🔑</li>
          <li>GET /api/universe/detail 🔑</li>
          <li>POST /api/universe/add 🔑</li>
          <li>POST /api/universe/remove 🔑</li>
          <li>GET /api/forecast 🔑 (bidirectional: 1 entri per side long/short)</li>
          <li>GET /api/diagnosa 🔑 (bidirectional: 1 entri per side long/short)</li>
          <li>GET /api/analytics/attribution, indicator_effectiveness, regime_performance, attribution_by_profile 🔑</li>
          <li>POST /api/analytics/refresh 🔑</li>
          <li>GET /api/meta_learner/suggestions, history 🔑 (nonaktif kecuali META_LEARNER_ENABLED_FUTURES=true)</li>
          <li>POST /api/meta_learner/approve/{id}, reject/{id} 🔑</li>
          <li>GET /api/stream 🔑 (SSE, genuinely event-driven -- lihat docstring modul)</li>
        </ul>
        <p>🔑 = butuh header X-API-Key</p>
        <p style="color:#ff9800;">⚠️ Belum ada di versi ini: crosslearn/shadow_trades
        (deprecated permanen, tidak di-port).</p>
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
        # [FIX] Sebelumnya cuma ada di /config/current -- Risk Monitor.dc.html
        # terpaksa double-fetch. Sekarang disertakan langsung di sini juga.
        health["max_position_size_pct"] = b.config.get("max_position_size_pct", 10.0)
        return health

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, level: Optional[str] = None, _: str = Depends(verify_api_key)):
        b = bot()
        logs = await b.db.get_recent_logs(limit=limit if not level else min(limit * 3, 500))
        if level:
            logs = [l for l in logs if l.level.upper() == level.upper()][:limit]
        return {"logs": [
            # [BUG-FIX -- pre-existing, ditemukan lewat test dashboard_snapshot]
            # l.source TIDAK ADA di BotLog model (engine/database.py:175-182,
            # kolomnya "module") -- endpoint ini crash AttributeError setiap
            # kali ada baris log (selalu ada di produksi). Ganti ke l.module
            # (field asli), key JSON tetap "source" (kontrak existing, TIDAK
            # diubah -- beda dari spot yang key JSON-nya "module", tapi
            # mengubah nama key API di luar scope bug-fix ini).
            {"timestamp": _iso(l.timestamp), "level": l.level, "source": l.module, "message": l.message}
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

    # ══════════════════════════════════════════════════════════════════════
    # BATCH 1 -- di-porting verbatim dari spot/api_server_spot.py (murni
    # baca DB/ws_feed/exchange shared, tidak ada konsep spot-only).
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/api/metrics")
    async def get_metrics(_: str = Depends(verify_api_key)):
        """[v8 PERF, REUSE dari spot] Hasil di-cache 10 detik -- cegah
        kalkulasi berat tiap request. Blok attribution_summary/indicator_summary
        di-guard `hasattr(b, "analytics")` -- di futures ini SELALU None
        (PerformanceAnalytics belum di-wire ke main_future.py::start(), lihat
        catatan docstring atas file), jadi blok itu otomatis di-skip aman
        (bukan error) sampai wiring-nya ditambahkan di sesi terpisah."""
        cached = _metrics_cache.get()
        if cached:
            return {**cached, "_cached": True}

        b      = bot()
        rm     = b.risk_manager
        trades = await b.db.get_recent_trades(limit=500)
        closed = [t for t in trades if t.realized_pnl is not None]
        pnl_list = [float(t.realized_pnl) for t in closed]

        snaps    = await b.db.get_equity_curve(limit=500)
        eq_curve = [float(s.total_equity) for s in snaps]
        max_dd   = rm.compute_max_drawdown(eq_curve)

        initial       = b.config.get("initial_capital", 1.0)
        last_eq       = eq_curve[-1] if eq_curve else initial
        total_ret_pct = (last_eq - initial) / initial * 100

        annualized_ret_pct = total_ret_pct
        if len(snaps) >= 2 and initial > 0 and last_eq > 0:
            days_spanned = (snaps[-1].timestamp - snaps[0].timestamp).total_seconds() / 86400.0
            if days_spanned >= 1:
                growth_factor = last_eq / initial
                if growth_factor > 0:
                    annualized_ret_pct = (
                        (growth_factor ** (365.0 / days_spanned)) - 1
                    ) * 100

        attribution_summary = {}
        indicator_summary   = {}
        if getattr(b, "_analytics", None):
            try:
                snap = await b.db.get_latest_snapshot(scope="global", lookback_days=30)
                if snap:
                    attribution_summary = {
                        "best_regime":   snap.get("best_regime"),
                        "worst_regime":  snap.get("worst_regime"),
                        "lookback_days": snap.get("lookback_days"),
                        "computed_at":   _iso(snap.get("computed_at")),
                    }
                indicator_eff = await b.db.get_indicator_effectiveness(lookback_days=30)
                indicator_summary = indicator_eff or {}
            except Exception as e:
                log.warning("Tidak bisa ambil analytics summary: %s", e)

        pf_raw = rm.compute_profit_factor(pnl_list)
        result = {
            "total_trades":         len(closed),
            "win_rate_pct":         round(rm.compute_win_rate(pnl_list),              4),
            "total_pnl":            round(sum(pnl_list),                               6),
            "avg_pnl_per_trade":    round(rm.compute_expectancy(pnl_list),             6),
            "profit_factor":        9999.0 if math.isinf(pf_raw) else round(pf_raw,   4),
            "expectancy":           round(rm.compute_expectancy(pnl_list),             6),
            "avg_win_loss_ratio":   round(rm.compute_avg_win_loss_ratio(pnl_list),     4),
            "max_drawdown_pct":     round(max_dd,                                      4),
            "current_drawdown_pct": round(rm.current_drawdown_pct,                    4),
            "sharpe_ratio":         round(rm.compute_sharpe_ratio(pnl_list),           4),
            "sortino_ratio":        round(rm.compute_sortino_ratio(pnl_list),          4),
            "calmar_ratio":         round(rm.compute_calmar_ratio(annualized_ret_pct, max_dd), 4),
            "total_fees":           round(sum(t.fee_cost or 0 for t in trades),        6),
            "open_positions":       len(await b.db.get_open_positions()),
            "daily_loss_pct":       round(rm.daily_loss_pct,                           4),
            "daily_loss_limit_pct": rm.daily_loss_limit_pct,
            "halt_reason":          rm.halt_reason,
            "attribution_summary":  attribution_summary,
            "indicator_summary":    indicator_summary,
            "timestamp":            _iso(_utcnow()),
            "_cached":              False,
        }
        _metrics_cache.set(result)
        return result

    @app.get("/api/tickers")
    async def get_tickers():
        b = bot()
        return {"tickers": b.ws_feed.live_tickers if b.ws_feed else {}}

    @app.get("/api/intelligence/regime")
    async def get_intelligence_regime(_: str = Depends(verify_api_key)):
        b        = bot()
        universe = b.config.get("universe_watchlist", [])

        regimes: list = []
        for symbol in universe:
            try:
                row = await b.db.get_latest_regime(symbol)
                if row:
                    regimes.append({
                        "symbol":       symbol,
                        "regime":       row.regime,
                        "confidence":   round(row.regime_confidence, 4),
                        "adx":          row.adx_value,
                        "atr_pct":      row.atr_pct,
                        "bb_width":     row.bb_width,
                        "last_updated": _iso(row.timestamp),
                    })
                else:
                    regimes.append({
                        "symbol": symbol, "regime": "undefined",
                        "confidence": 0.0, "last_updated": None,
                    })
            except Exception as e:
                log.warning("get_intelligence_regime [%s]: %s", symbol, e)
                regimes.append({"symbol": symbol, "regime": "undefined", "error": str(e)})

        regime_counts: Dict[str, int] = {}
        for r in regimes:
            key = r.get("regime", "undefined")
            regime_counts[key] = regime_counts.get(key, 0) + 1

        return {
            "regimes":        regimes,
            "summary":        regime_counts,
            "universe_count": len(universe),
            "timestamp":      _iso(_utcnow()),
        }

    def _score_side_block(row) -> Dict[str, Any]:
        """[BIAS-FIX] Blok field lengkap utk satu row SignalScore (satu side).
        row=None -> default kosong (identik dgn perilaku lama saat belum ada
        skor sama sekali)."""
        if not row:
            return {
                "total_score": None, "breakdown": {}, "regime": "undefined",
                "trigger_met": False, "action_taken": None, "last_updated": None,
            }
        return {
            "total_score": row.total_score,
            "breakdown": {
                "trend":      row.trend_score,
                "momentum":   row.momentum_score,
                "strength":   row.strength_score,
                "volatility": row.volatility_score,
                "pattern":    row.pattern_score,
            },
            "regime":       row.regime,
            "trigger_met":  row.trigger_met,
            "action_taken": row.action_taken,
            "last_updated": _iso(row.timestamp),
        }

    def _pick_best_block(long_block: Dict[str, Any], short_block: Dict[str, Any]) -> Dict[str, Any]:
        """[BIAS-FIX] "Sisi terkuat" = total_score lebih tinggi (None
        dianggap paling rendah). Kalau dua-duanya None, keduanya identik
        (default kosong) jadi hasilnya sama saja -- pakai long_block."""
        ls = long_block["total_score"]
        ss = short_block["total_score"]
        if ls is None and ss is None:
            return long_block
        if ls is None:
            return short_block
        if ss is None:
            return long_block
        return long_block if ls >= ss else short_block

    @app.get("/api/intelligence/scores")
    async def get_intelligence_scores(_: str = Depends(verify_api_key)):
        """[BIAS-FIX] Sebelumnya get_latest_signal_score(symbol) tanpa side
        cuma menampilkan "sisi mana pun yang terakhir dihitung" -- sekarang
        panggil 2x (long & short), tampilkan sisi TERKUAT (skor tertinggi)
        di field top-level (backward-compat dgn dashboard/Watchlist.dc.html
        yang baca sc.total_score/sc.regime flat), plus breakdown lengkap
        per-side di "long"/"short"."""
        b        = bot()
        universe = b.config.get("universe_watchlist", [])

        scores: list = []
        for symbol in universe:
            try:
                row_long  = await b.db.get_latest_signal_score(symbol, side="long")
                row_short = await b.db.get_latest_signal_score(symbol, side="short")
                long_block  = _score_side_block(row_long)
                short_block = _score_side_block(row_short)
                best_block  = _pick_best_block(long_block, short_block)

                scores.append({
                    "symbol": symbol,
                    **best_block,
                    "long":  long_block,
                    "short": short_block,
                })
            except Exception as e:
                log.warning("get_intelligence_scores [%s]: %s", symbol, e)
                scores.append({"symbol": symbol, "error": str(e)})

        scores.sort(key=lambda x: (x.get("total_score") is not None, x.get("total_score") or 0), reverse=True)

        return {"scores": scores, "count": len(scores), "timestamp": _iso(_utcnow())}

    def _score_side_detail(row, side: str) -> Optional[Dict[str, Any]]:
        """[BIAS-FIX] Blok detail lengkap utk satu row SignalScore (satu
        side), termasuk entry_threshold/above_threshold yang sekarang
        dihitung side-aware (get_dynamic_threshold(..., side=side) -- bug
        family yang sama dgn get_latest_signal_score). row=None -> None
        (dibedakan dari _score_side_block's row=None -> dict kosong, krn di
        sini caller perlu tahu apakah side ini punya data sama sekali utk
        404 check)."""
        if not row:
            return None
        try:
            _regime  = row.regime if row.regime else "undefined"
            _profile = row.strategy_profile or "trend_follow"
            entry_threshold = get_dynamic_threshold(_profile, _regime, side=side)
        except Exception:
            entry_threshold = 70.0
        return {
            "total_score":     row.total_score,
            "entry_threshold": entry_threshold,
            "above_threshold": (
                row.total_score >= entry_threshold
                if row.total_score is not None else False
            ),
            "breakdown": {
                "trend":      row.trend_score,
                "momentum":   row.momentum_score,
                "strength":   row.strength_score,
                "volatility": row.volatility_score,
                "pattern":    row.pattern_score,
            },
            "regime":           row.regime,
            "trigger_met":      row.trigger_met,
            "action_taken":     row.action_taken,
            "rejection_reason": row.rejection_reason,
            "profile":          row.strategy_profile,
            "narrative":        getattr(row, "narrative", None),
            "last_updated":     _iso(row.timestamp),
        }

    def _pick_best_detail(long_detail, short_detail) -> Dict[str, Any]:
        """[BIAS-FIX] Sama prinsipnya dgn _pick_best_block: sisi dgn
        total_score lebih tinggi jadi field top-level (backward-compat).
        Dipanggil HANYA setelah dipastikan minimal satu sisi tidak None."""
        if long_detail is None:
            return short_detail
        if short_detail is None:
            return long_detail
        ls = long_detail["total_score"]
        ss = short_detail["total_score"]
        if ls is None and ss is None:
            return long_detail
        if ls is None:
            return short_detail
        if ss is None:
            return long_detail
        return long_detail if ls >= ss else short_detail

    @app.get("/api/intelligence/scores/{symbol:path}")
    async def get_intelligence_score_detail(symbol: str, _: str = Depends(verify_api_key)):
        """[BIAS-FIX] Sebelumnya get_latest_signal_score(symbol) tanpa side
        cuma menampilkan "sisi mana pun yang terakhir dihitung", dan
        entry_threshold dihitung tanpa side (bug family sama). Sekarang
        panggil 2x (long & short), tampilkan sisi TERKUAT di top-level
        (backward-compat, walau belum ada dashboard konsumen nyata utk
        endpoint ini), plus "long"/"short" nested berisi detail lengkap
        masing-masing sisi (termasuk entry_threshold side-aware sendiri-
        sendiri)."""
        b = bot()

        row_long  = await b.db.get_latest_signal_score(symbol, side="long")
        row_short = await b.db.get_latest_signal_score(symbol, side="short")
        if not row_long and not row_short:
            raise HTTPException(
                status_code=404,
                detail=f"Belum ada score untuk {symbol}. Bot mungkin belum menganalisis coin ini."
            )

        long_detail  = _score_side_detail(row_long, "long")
        short_detail = _score_side_detail(row_short, "short")
        best_detail  = _pick_best_detail(long_detail, short_detail)

        history_rows = await b.db.get_signal_scores(symbol=symbol, limit=96)
        history = [
            {
                "timestamp":   _iso(r["timestamp"]),
                "total_score": r["total_score"],
                "regime":      r["regime"],
                "trigger_met": r["trigger_met"],
                "action":      r["action_taken"],
                "side":        r.get("side"),
            }
            for r in history_rows
        ]

        return {
            "symbol": symbol,
            **best_detail,
            "long":         long_detail,
            "short":        short_detail,
            "history_24h":  history,
            "timestamp":    _iso(_utcnow()),
        }

    @app.get("/api/candles/{symbol:path}/indicators")
    async def get_candles_with_indicators(
        symbol:    str,
        timeframe: str = "15m",
        limit:     int = 100,
        _: str = Depends(verify_api_key),
    ):
        """[PORT #7 -- BUKAN straight copy, bug routing ditemukan &
        diperbaiki] Investigasi menemukan versi spot mendaftarkan route ini
        SETELAH `/api/candles/{symbol:path}` -- karena converter `:path`
        Starlette greedy (match termasuk slash lanjutan) dan resolusi rute
        pakai urutan registrasi pertama-menang, `/api/candles/X/indicators`
        di spot SELALU ketangkap handler get_candles() yang polos (return
        {candles, markers}), endpoint /indicators TIDAK PERNAH genuinely
        reachable sejak dibuat. Dibuktikan lewat TestClient langsung ke app
        spot sungguhan (bukan asumsi baca kode) -- response persis field
        get_candles(), bukan field endpoint ini. Dampak nyata NIHIL (nol
        pemakai di dashboard/*.dc.html, dicek grep). Fix di sini: route
        LEBIH SPESIFIK didaftarkan SEBELUM `/api/candles/{symbol:path}`,
        supaya benar-benar reachable. Isi handler sendiri straight port,
        tidak ada logic spot-only -- OHLCV + kolom enrich_production."""
        b   = bot()
        sym = urllib.parse.unquote(symbol).upper()
        if not b.exchange or not b.exchange.is_connected:
            raise HTTPException(status_code=503, detail="Exchange belum terhubung")
        try:
            raw  = await b.exchange.fetch_ohlcv(sym, timeframe, limit=limit + 50)
            cols = ["timestamp", "open", "high", "low", "close", "volume"]
            df   = pd.DataFrame(raw, columns=cols)
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            df.ta.enrich_production()
            df = df.iloc[-limit:]
            records = []
            for ts, row in df.iterrows():
                rec = {"timestamp": int(ts.timestamp() * 1000)}
                for col in df.columns:
                    v = row[col]
                    rec[col] = None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else (float(v) if hasattr(v, "item") else v)
                records.append(rec)
            return {
                "symbol":    sym,
                "timeframe": timeframe,
                "columns":   list(df.columns),
                "candles":   records,
                "count":     len(records),
                "timestamp": _iso(_utcnow()),
            }
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/api/candles/{symbol:path}")
    async def get_candles(symbol: str, timeframe: str = "15m", limit: int = 100, _: str = Depends(verify_api_key)):
        b = bot()

        if not b.exchange or not b.exchange.is_connected:
            raise HTTPException(status_code=503, detail="Exchange belum terhubung")

        try:
            raw     = await b.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            candles = [
                {
                    "timestamp":    bar[0],
                    "open":         bar[1],
                    "high":         bar[2],
                    "low":          bar[3],
                    "close":        bar[4],
                    "volume":       bar[5],
                    "quote_volume": bar[6] if len(bar) > 6 else None,
                }
                for bar in raw
            ]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"OHLCV error: {e}")

        trades  = await b.db.get_recent_trades(limit=200)
        markers = [
            {
                "timestamp": t.timestamp.timestamp() * 1000 if t.timestamp else None,
                "price":     t.executed_price,
                "side":      t.side,
                "origin":    t.signal_origin,
                "slippage":  t.slippage_pct,
                "fee":       t.fee_cost,
            }
            for t in trades
            if t.symbol == symbol and t.executed_price
        ]

        return {"candles": candles, "markers": markers}

    @app.get("/api/dashboard_snapshot")
    async def get_dashboard_snapshot(_: str = Depends(verify_api_key)):
        """[v8 PERF, REUSE dari spot] Semua data dashboard dalam satu call --
        parallel asyncio.gather. Panggil closure lokal get_status/get_balance/
        get_metrics/get_logs yang sudah didefinisikan di atas (bukan
        duplikasi logic).

        [PENYESUAIAN dari spot -- BUKAN copy-paste literal] get_logs() di
        futures signature-nya (limit, level, _) -- BEDA dari spot (limit, _)
        saja. Panggilan positional get_logs(20, _) ala spot akan salah
        mengikat `_` ke parameter `level`, bukan ke auth dependency -- di
        sini WAJIB pakai keyword get_logs(limit=20, _=_)."""
        b = bot()

        positions, trades, snaps = await asyncio.gather(
            b.db.get_open_positions(),
            b.db.get_recent_trades(limit=120),
            b.db.get_equity_curve(limit=300),
        )

        health = (
            b.risk_manager.get_system_health()
            if b.risk_manager
            else {"risk_status": "initializing", "halted": False,
                  "halt_reason": "", "drawdown_pct": 0.0}
        )
        feed_st = b.ws_feed.get_feed_status() if b.ws_feed else {}
        return {
            "status":  await get_status(),
            "balance": await get_balance(_),
            "metrics": await get_metrics(_),
            "system_health": {
                **health,
                "strategy_active": b.strategy.is_active if b.strategy else False,
                "strategy_name":   b.strategy.name if b.strategy else None,
                "ws_feed_status":  feed_st,
                "timestamp":       _iso(_utcnow()),
            },
            "positions": {
                "positions": [_pos_dict(p) for p in positions],
                "count":     len(positions),
            },
            "tickers": {"tickers": b.ws_feed.live_tickers if b.ws_feed else {}},
            "logs": await get_logs(limit=20, _=_),
            "equity_curve": {
                "curve": [
                    {
                        "timestamp":     _iso(s.timestamp),
                        "equity":        s.total_equity,
                        "drawdown":      s.drawdown_pct,
                        "daily_pnl":     s.daily_pnl,
                        "daily_pnl_pct": s.daily_pnl_pct,
                    }
                    for s in snaps
                ]
            },
            "trades": {"trades": [_trade_dict(t) for t in trades], "count": len(trades)},
        }

    @app.get("/api/executor/stats")
    async def get_executor_stats(_: str = Depends(verify_api_key)):
        """[EXECUTOR-STATS] Fill rate, slippage, fee, latency, leverage dari
        BaseOrderExecutionManager.get_stats() -- shared verbatim dgn spot
        lewat engine/execution_base.py, tidak ada implementasi terpisah di
        sini. avg_leverage_used relevan khusus utk futures (Trade.leverage
        selalu None di baris spot, otomatis diabaikan tanpa cek market_type)."""
        b = bot()
        if not b.executor:
            raise HTTPException(status_code=503, detail="Executor belum aktif")
        try:
            stats = await b.executor.get_stats()
            stats["timestamp"] = _iso(_utcnow())
            return stats
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/analytics/attribution")
    async def get_analytics_attribution(
        lookback_days: int = 30,
        profile: Optional[str] = None,
        symbol: Optional[str] = None,
        _: str = Depends(verify_api_key),
    ):
        """[PORT #7 langkah 3/4 -- straight port] Data source (engine/
        database.py get_trades_with_regime/get_score_vs_outcome) dikonfirmasi
        generik/market-agnostic (nol filter side), aman dipakai apa adanya."""
        b = bot()
        if not hasattr(b, "analytics") or not b.analytics:
            raise HTTPException(status_code=503, detail="Analytics engine belum diinisialisasi.")
        try:
            filters = {}
            if profile:
                filters["profile"] = profile
            if symbol:
                filters["symbol"] = symbol
            report = await b.analytics.compute_attribution(
                lookback_days=lookback_days,
                filters=filters,
            )
        except Exception as e:
            log.error("Attribution computation error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Attribution error: {e}")

        return {
            "attribution": report,
            "lookback_days": lookback_days,
            "filters":       {"profile": profile, "symbol": symbol},
            "timestamp":     _iso(_utcnow()),
        }

    @app.get("/api/analytics/indicator_effectiveness")
    async def get_indicator_effectiveness(
        lookback_days: int = 30,
        _: str = Depends(verify_api_key),
    ):
        b = bot()
        if not hasattr(b, "analytics") or not b.analytics:
            raise HTTPException(status_code=503, detail="Analytics engine belum diinisialisasi.")
        try:
            report = await b.analytics.compute_indicator_effectiveness(lookback_days=lookback_days)
        except Exception as e:
            log.error("Indicator effectiveness error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Analytics error: {e}")

        return {
            "indicator_effectiveness": report,
            "lookback_days": lookback_days,
            "timestamp":     _iso(_utcnow()),
        }

    @app.get("/api/analytics/regime_performance")
    async def get_regime_performance(
        lookback_days: int = 30,
        _: str = Depends(verify_api_key),
    ):
        b = bot()
        if not hasattr(b, "analytics") or not b.analytics:
            raise HTTPException(status_code=503, detail="Analytics engine belum diinisialisasi.")
        try:
            report = await b.analytics.compute_attribution(
                lookback_days=lookback_days,
                filters={},
                group_by="regime",
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Analytics error: {e}")

        return {
            "regime_performance": report,
            "lookback_days":     lookback_days,
            "timestamp":         _iso(_utcnow()),
        }

    @app.get("/api/analytics/attribution_by_profile")
    async def get_attribution_by_profile(
        lookback_days: int = 30,
        _: str = Depends(verify_api_key),
    ):
        b = bot()
        if not hasattr(b, "analytics") or not b.analytics:
            raise HTTPException(status_code=503, detail="Analytics engine belum diinisialisasi.")
        try:
            reports = await b.analytics.compute_all_profiles(lookback_days=lookback_days)
        except Exception as e:
            log.error("compute_all_profiles error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Attribution by profile error: {e}")

        return {
            "profiles":      reports,
            "lookback_days": lookback_days,
            "timestamp":     _iso(_utcnow()),
        }

    @app.post("/api/analytics/refresh")
    async def refresh_analytics(_: str = Depends(verify_api_key)):
        b = bot()
        if not hasattr(b, "analytics") or not b.analytics:
            raise HTTPException(status_code=503, detail="Analytics engine belum diinisialisasi.")
        try:
            await b.analytics.run_full_analysis()
            return {"status": "refreshed", "timestamp": _iso(_utcnow())}
        except Exception as e:
            log.error("Analytics refresh error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Refresh error: {e}")

    @app.get("/api/meta_learner/suggestions")
    async def get_meta_learner_suggestions(_: str = Depends(verify_api_key)):
        b = bot()
        try:
            rows = await b.db.get_pending_suggestions(limit=200)
        except Exception as e:
            log.error("get_suggestions error (futures): %s", e)
            rows = []

        suggestions = []
        for row in rows:
            suggestions.append({
                "id":                 row.get("id"),
                "created_at":         _iso(row.get("timestamp")),
                "symbol":             row.get("symbol"),
                "profile":            row.get("profile"),
                "parameter_name":     row.get("parameter_name"),
                "old_value":          row.get("old_value"),
                "new_value":          row.get("new_value"),
                "reason":             row.get("reason"),
                "confidence":         row.get("confidence"),
                "projected_improvement": row.get("projected_improvement"),
                "status":             row.get("status", "pending"),
            })

        return {
            "suggestions": suggestions,
            "count":       len(suggestions),
            "timestamp":   _iso(_utcnow()),
        }

    @app.post("/api/meta_learner/approve/{suggestion_id}")
    async def approve_suggestion(
        suggestion_id: str,
        _: str = Depends(verify_api_key),
    ):
        """[PORT #7 langkah 3/4] Suggestion tipe weight_* akan ditolak
        (ok=False, message jelas) oleh guard di
        engine/learning/meta_learner.py::_apply_suggestion() -- MetaLearner
        futures diinstansiasi dgn market_type="futures" (lihat
        main_future.py::_initialize_intelligence_pipeline()). Suggestion
        tipe threshold tetap ter-apply normal. Endpoint ini sendiri TIDAK
        perlu tahu bedanya -- cukup teruskan hasil approve_suggestion()
        apa adanya, persis pola spot."""
        b = bot()
        if not hasattr(b, "meta_learner") or not b.meta_learner:
            raise HTTPException(status_code=503, detail="Meta-learner belum diinisialisasi.")
        try:
            ok, msg = await b.meta_learner.approve_suggestion(
                suggestion_id=suggestion_id,
                approved_by="manual_api",
            )
            log.info("Suggestion %s approved via API (futures)", suggestion_id)
            return {
                "status":        "approved",
                "suggestion_id": suggestion_id,
                "applied":       ok,
                "message":       msg,
                "timestamp":     _iso(_utcnow()),
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            log.error("approve_suggestion error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Approve error: {e}")

    @app.post("/api/meta_learner/reject/{suggestion_id}")
    async def reject_suggestion(
        suggestion_id: str,
        _: str = Depends(verify_api_key),
    ):
        b = bot()
        if not hasattr(b, "meta_learner") or not b.meta_learner:
            raise HTTPException(status_code=503, detail="Meta-learner belum diinisialisasi.")
        try:
            ok, msg = await b.meta_learner.reject_suggestion(
                suggestion_id=suggestion_id,
                rejected_by="manual_api",
            )
            log.info("Suggestion %s rejected via API (futures)", suggestion_id)
            return {
                "status":        "rejected",
                "suggestion_id": suggestion_id,
                "rejected":      ok,
                "message":       msg,
                "timestamp":     _iso(_utcnow()),
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            log.error("reject_suggestion error (futures): %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Reject error: {e}")

    @app.get("/api/meta_learner/history")
    async def get_parameter_history(
        symbol: Optional[str] = None,
        profile: Optional[str] = None,
        limit: int = 50,
        _: str = Depends(verify_api_key),
    ):
        b = bot()
        try:
            rows = await b.db.get_parameter_history(
                symbol=symbol,
                profile=profile,
                limit=min(limit, 200),
            )
        except Exception as e:
            log.error("get_parameter_history error (futures): %s", e)
            rows = []

        history = []
        for row in rows:
            history.append({
                "id":                  row.get("id"),
                "timestamp":           _iso(row.get("timestamp")),
                "symbol":              row.get("symbol"),
                "profile":             row.get("profile"),
                "parameter_name":      row.get("parameter_name"),
                "old_value":           row.get("old_value"),
                "new_value":           row.get("new_value"),
                "reason":              row.get("reason"),
                "approved_by":         row.get("approved_by"),
                "performance_before":  None,
                "performance_after":   None,
                "outcome":             row.get("outcome"),
                "trades_after_apply":  row.get("trades_after_apply"),
            })

        return {
            "history":   history,
            "count":     len(history),
            "timestamp": _iso(_utcnow()),
        }

    @app.get("/api/forecast")
    async def get_forecast(_: str = Depends(verify_api_key)):
        """[PORT #7 langkah 4/4 -- audit item #19] BUKAN straight copy dari
        spot -- genuinely bidirectional (keputusan dikonfirmasi user
        setelah investigasi). Versi spot memanggil
        `get_latest_signal_score(symbol)` TANPA side -- utk symbol yang
        di-scoring long DAN short tiap siklus (futures), itu mengembalikan
        row APA PUN yang kebetulan terakhir di-scoring, lalu
        diinterpretasi 100% sbg sinyal long (ema_bullish, probability_up,
        dst) -- bisa aktif menyesatkan (tampilkan "Bullish 80%" utk row
        yang sebenarnya hasil scoring short). Di sini: fetch
        side="long" DAN side="short" terpisah (2 row DB independen, field
        skornya SUDAH side-aware dari scorer.py::_pick_side_score, lihat
        docstring _build_forecast_entry()), hasilkan 1 entri per side per
        symbol yang genuinely ada datanya."""
        b = bot()
        universe   = b.config.get("universe_watchlist", [])
        tf_primary = b.config.get("timeframe", "15m")
        forecasts  = []

        for symbol in universe:
            indicators   = {}
            conf_tf_data = {}
            try:
                strat = getattr(b, "strategy", None)
                obs = getattr(strat, "_observer", None) if strat else None
                if obs:
                    observation = await obs.get_cached_observation(symbol, tf_primary)
                    if observation and observation.primary_tf_indicators:
                        ind = observation.primary_tf_indicators
                        if ind.trend:
                            indicators["ema9"]  = round(ind.trend.ema9, 8) if ind.trend.ema9 else None
                            indicators["ema21"] = round(ind.trend.ema21, 8) if ind.trend.ema21 else None
                            indicators["ema50"] = round(ind.trend.ema50, 8) if ind.trend.ema50 else None
                        if ind.momentum:
                            indicators["rsi"]       = round(ind.momentum.rsi, 2) if ind.momentum.rsi else None
                            indicators["rsi_slope"] = round(ind.momentum.rsi_slope, 4) if ind.momentum.rsi_slope else None
                            indicators["rsi_zone"]  = ind.momentum.rsi_zone_exit
                        if ind.strength:
                            indicators["adx"]          = round(ind.strength.adx, 2) if ind.strength.adx else None
                            indicators["volume_ratio"] = round(ind.strength.volume_ratio, 3) if ind.strength.volume_ratio else None
                            indicators["volume_spike"] = ind.strength.volume_spike
                        if ind.volatility:
                            indicators["atr"]            = round(ind.volatility.atr, 8) if ind.volatility.atr else None
                            indicators["atr_pct"]        = round(ind.volatility.atr_pct, 4) if ind.volatility.atr_pct else None
                            indicators["atr_percentile"] = ind.volatility.atr_percentile
                            indicators["atr_trend"]      = ind.volatility.atr_trend
                        if observation.confirmation_tf_indicators:
                            c = observation.confirmation_tf_indicators
                            if c.momentum:
                                conf_tf_data["rsi"] = round(c.momentum.rsi, 2) if c.momentum.rsi else None
                            if c.trend:
                                conf_tf_data["ema_bullish"] = (c.trend.ema9 or 0) > (c.trend.ema21 or 0)
                                conf_tf_data["ema9"]        = round(c.trend.ema9, 8) if c.trend.ema9 else None
                                conf_tf_data["ema21"]       = round(c.trend.ema21, 8) if c.trend.ema21 else None
                            if c.strength:
                                conf_tf_data["adx"] = round(c.strength.adx, 2) if c.strength.adx else None
            except Exception:
                pass

            for side in ("long", "short"):
                try:
                    row = await b.db.get_latest_signal_score(symbol, side=side)
                    entry_indicators = dict(indicators)
                    if row and row.current_price:
                        entry = _build_forecast_entry(row, side, tf_primary, entry_indicators, dict(conf_tf_data))
                        if entry:
                            entry["symbol"] = symbol
                            forecasts.append(entry)
                except Exception as e:
                    log.warning("forecast [%s/%s]: %s", symbol, side, e)

        forecasts.sort(key=lambda x: x.get("probability_favorable_pct", 0), reverse=True)
        return {"forecasts": forecasts, "count": len(forecasts), "timestamp": _iso(_utcnow())}

    @app.get("/api/diagnosa")
    async def get_diagnosa(_: str = Depends(verify_api_key)):
        """[PORT #7 langkah 4/4 -- audit item #19] Genuinely bidirectional
        (keputusan dikonfirmasi user). BEDA STRUKTURAL dari spot -- lihat
        docstring _diagnosa_entry_from_row() utk alasan lengkap kenapa
        get_cached_observation() (dipakai spot) SENGAJA dihindari di sini
        (ambigu sisi, sama akar masalah dgn bug /api/forecast). Sumber
        utama: get_latest_signal_score(symbol, side=X) per sisi. Fallback
        manual (mirror spot, side="short" dibalik) HANYA dipakai kalau
        symbol itu genuinely belum pernah discoring sama sekali di sisi
        tsb -- lihat docstring _diagnosa_fallback_entry() utk batasan
        jujur mirror short (bukan hasil verifikasi fuzz-test spt sub-
        indikator pipeline utama)."""
        b = bot()
        universe   = b.config.get("universe_watchlist", [])
        is_testnet = b.config.get("testnet", True)
        tf_default = b.config.get("timeframe", "15m")
        results: List[dict] = []

        for symbol in universe:
            open_pos_by_side: Dict[str, Any] = {}
            try:
                for p in await b.db.get_open_positions():
                    if p.symbol == symbol:
                        open_pos_by_side[getattr(p, "side", "long")] = p
            except Exception:
                pass

            rows: Dict[str, Any] = {}
            for side in ("long", "short"):
                try:
                    rows[side] = await b.db.get_latest_signal_score(symbol, side=side)
                except Exception as e:
                    log.debug("Diagnosa: gagal baca signal_scores [%s/%s]: %s", symbol, side, e)
                    rows[side] = None

            df = None
            tf_used = tf_default
            tf_note = ""
            if rows["long"] is None or rows["short"] is None:
                bars = None
                for tf_try in [tf_default] + DIAGNOSA_TF_FALLBACK.get(tf_default, []):
                    try:
                        candidate = await b.exchange.fetch_ohlcv(symbol, tf_try, limit=250)
                        if candidate and len(candidate) >= 60:
                            bars = candidate
                            tf_used = tf_try
                            if tf_try != tf_default:
                                tf_note = f" ⚠️fallback:{tf_try}"
                            break
                    except Exception as tf_err:
                        log.debug("Diagnosa TF fallback %s [%s]: %s", symbol, tf_try, tf_err)
                        continue

                if bars and len(bars) >= 60:
                    try:
                        cols = ["timestamp", "open", "high", "low", "close", "volume"]
                        df_candidate = pd.DataFrame(bars, columns=cols)
                        df_candidate["timestamp"] = pd.to_datetime(df_candidate["timestamp"], unit="ms")
                        df_candidate.set_index("timestamp", inplace=True)

                        if len(bars[0]) > 6:
                            df_candidate["quote_volume"] = [
                                float(r[6]) if len(r) > 6 and r[6] is not None
                                else float(r[4]) * float(r[5])
                                for r in bars
                            ]
                        else:
                            df_candidate["quote_volume"] = df_candidate["volume"] * df_candidate["close"]

                        df_candidate.ta.enrich_production()
                        df_candidate = df_candidate.dropna(subset=[COL_EMA9, COL_RSI, COL_ATR])

                        df_candidate["_resistance"] = df_candidate["close"].shift(1).rolling(20).max()
                        df_candidate["_support"]    = df_candidate["close"].shift(1).rolling(20).min()
                        df_candidate["_vol_ma"]     = df_candidate["quote_volume"].rolling(20).mean()
                        df_candidate = df_candidate.dropna(subset=["_resistance", "_support", "_vol_ma"])

                        if len(df_candidate) >= 5:
                            df = df_candidate
                    except Exception as build_err:
                        log.debug("Diagnosa: gagal siapkan df fallback [%s]: %s", symbol, build_err)

            for side in ("long", "short"):
                entry: Dict[str, Any] = {"symbol": symbol, "side": side}
                try:
                    row = rows.get(side)
                    if row and row.current_price:
                        entry.update(_diagnosa_entry_from_row(row, side, open_position=open_pos_by_side.get(side)))
                    elif df is not None:
                        prof = get_coin_profile(symbol)
                        fb = _diagnosa_fallback_entry(df, side, prof, is_testnet, tf_used, tf_note)
                        if fb:
                            entry.update(fb)
                        else:
                            entry["error"] = "Indikator tidak cukup"
                    else:
                        note = " (testnet — data terbatas)" if is_testnet else ""
                        entry["error"] = f"Data tidak cukup{note}"
                except Exception as e:
                    log.error("Diagnosa error [%s/%s]: %s", symbol, side, e, exc_info=True)
                    entry["error"] = str(e)[:120]
                results.append(entry)

        return {
            "results":        results,
            "universe_count": len(universe),
            "testnet":        is_testnet,
            "timestamp":      _iso(_utcnow()),
        }

    @app.post("/api/universe/add")
    async def universe_add(
        req: UniverseAddRequest,
        _:   str = Depends(verify_api_key),
    ):
        """[PORT #7 -- BUKAN straight copy, penyesuaian konteks futures]
        Acuan: spot/api_server_spot.py universe_add(). Spot TIDAK PUNYA
        validasi apa pun sebelum menulis symbol ke universe_overrides --
        dicek langsung di kode spot (get_orderbook & routing bug sebelumnya
        sudah 2x membuktikan straight-copy tanpa cek itu berbahaya), dan
        ditelusuri lagi lewat seluruh pipeline hot-reload
        (main_spot.py run_scanner ~baris 892-915, WebSocketFeed.add_symbols)
        -- NOL validasi exchange di titik mana pun, symbol arbitrer bisa
        masuk DB lalu bikin ws_feed subscription yang permanen gagal
        (REST_FALLBACK loop error berulang) tanpa pernah ketahuan lewat API
        response.

        Futures PUNYA preseden nyata utk masalah persis ini:
        future/exchange_future.py::auto_scan_and_populate_futures() sudah
        memvalidasi tiap symbol hasil scan via `is_valid_symbol` callback
        (dipanggil dari main_future.py dengan
        self.exchange.is_symbol_supported -- BUKAN get_market_info, yang
        terbukti salah di 2 arah utk 61 symbol nyata, lihat docstring
        is_symbol_supported() di engine/exchange_base.py) SEBELUM ditulis
        ke universe_futures.json/universe_overrides -- didorong oleh
        insiden nyata EVAA/USDT. Endpoint manual ini pakai validator yang
        SAMA, prasyarat exchange terhubung (pola sama dgn endpoint lain yg
        butuh exchange, mis. candles/market_info)."""
        b   = bot()
        sym = req.symbol.upper().strip()
        if not b.exchange or not b.exchange.is_connected:
            raise HTTPException(status_code=503, detail="Exchange belum terhubung")
        if not b.exchange.is_symbol_supported(sym):
            raise HTTPException(
                status_code=400,
                detail=f"Symbol {sym} tidak dikenali/tidak tersedia di Binance Futures.",
            )
        try:
            await b.db.upsert_universe_override(
                symbol=sym, source="api", notes=req.notes or ""
            )
            log.info("Universe override ADD (futures): %s via API", sym)
            return {"status": "added", "symbol": sym, "timestamp": _iso(_utcnow())}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/universe/remove")
    async def universe_remove(
        req: UniverseRemoveRequest,
        _:   str = Depends(verify_api_key),
    ):
        """[PORT #7 -- straight port, tidak ada penyesuaian futures-spesifik
        diperlukan] Nonaktifkan symbol dari universe override. TIDAK
        divalidasi is_symbol_supported() secara sengaja -- symbol yang
        sudah di-delisted/tidak lagi didukung exchange HARUS tetap bisa
        dihapus dari watchlist (itu justru skenario paling umum utk
        endpoint remove), validasi di sini kontraproduktif."""
        b   = bot()
        sym = req.symbol.upper().strip()
        try:
            await b.db.deactivate_universe_override(symbol=sym)
            log.info("Universe override REMOVE (futures): %s via API", sym)
            return {"status": "removed", "symbol": sym, "timestamp": _iso(_utcnow())}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/universe/detail")
    async def get_universe_detail(_: str = Depends(verify_api_key)):
        """[FUTURES] Acuan: spot/api_server_spot.py get_universe_detail().
        Beda dari spot: baca universe_futures.json (bukan universe.json), dan
        side-aware -- symbol yang sama bisa punya skor long DAN short
        berbeda (lihat get_latest_signal_score(side=...)), jadi ditampilkan
        terpisah (total_score_long/short) bukan cuma "row terakhir" yang
        kebetulan side mana pun. projected_leverage: estimasi leverage yang
        akan dipakai RiskManager.compute_adaptive_leverage() kalau sisi
        terkuat (skor tertinggi antara long/short) entry sekarang -- data
        forward-looking, bukan dari trade historis."""
        b = bot()
        try:
            universe_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "universe_futures.json"
            )
            try:
                with open(universe_path, "r", encoding="utf-8") as f:
                    udata = json.load(f)
                coins      = udata.get("symbols", [])
                scanned_at = udata.get("scanned_at", "")
            except Exception:
                coins = [{"symbol": s, "volume_24h": 0}
                         for s in b.config.get("universe_watchlist", [])]
                scanned_at = ""

            # [BUG-FIX #32 -- dikonfirmasi futures punya gap SAMA PERSIS
            # dgn spot, bukan cuma spot] `coins` sebelumnya cuma dari
            # universe_futures.json (atau fallback config[
            # "universe_watchlist"]) -- db.get_active_universe_overrides()
            # TIDAK PERNAH dikonsultasi di sini. Symbol yang ditambah
            # manual lewat POST /api/universe/add genuinely ikut discan/
            # trading, tapi tidak pernah muncul di tampilan endpoint ini.
            # Fix: gabungkan symbol dari DB overrides yg belum ada di
            # `coins`, volume_24h default 0 (belum ter-scan volume-nya).
            try:
                db_overrides    = await b.db.get_active_universe_overrides()
                existing_symbols = {c["symbol"] for c in coins}
                for ov_sym in db_overrides:
                    if ov_sym not in existing_symbols:
                        coins.append({"symbol": ov_sym, "volume_24h": 0})
                        existing_symbols.add(ov_sym)
            except Exception as _ov_err:
                log.debug("universe/detail (futures): gagal baca DB overrides: %s", _ov_err)

            result = []
            for c in coins:
                symbol = c["symbol"]
                vol    = c.get("volume_24h", 0)
                try:
                    row        = await b.db.get_latest_regime(symbol)
                    regime     = row.regime if row else "undefined"
                    confidence = round(row.regime_confidence, 4) if row else 0.0
                    adx        = row.adx_value if row else 0.0
                    atr_pct    = row.atr_pct if row else 0.5
                    profile    = select_profile_from_indicators(
                        symbol=symbol, adx=adx or 20.0,
                        atr_pct=atr_pct or 0.5, regime=regime,
                    )

                    score_long  = await b.db.get_latest_signal_score(symbol, side="long")
                    score_short = await b.db.get_latest_signal_score(symbol, side="short")
                    total_score_long  = score_long.total_score  if score_long  else None
                    trigger_met_long  = score_long.trigger_met  if score_long  else False
                    total_score_short = score_short.total_score if score_short else None
                    trigger_met_short = score_short.trigger_met if score_short else False

                    projected_leverage = None
                    if b.risk_manager:
                        candidates = [s for s in (total_score_long, total_score_short) if s is not None]
                        projected_leverage = b.risk_manager.compute_adaptive_leverage(
                            base_leverage=b.config.get("default_leverage", 10),
                            atr_pct=atr_pct, regime=regime, profile_name=profile,
                            score=max(candidates) if candidates else None,
                        )
                except Exception:
                    regime = "undefined"; confidence = 0.0
                    profile = "scalp_volatile"
                    total_score_long = None;  trigger_met_long  = False
                    total_score_short = None; trigger_met_short = False
                    projected_leverage = None
                result.append({
                    "symbol":             symbol,
                    "volume_24h":         vol,
                    "volume_m":           round(vol / 1_000_000, 2),
                    "profile":            profile,
                    "regime":             regime,
                    "confidence":         confidence,
                    "total_score_long":   total_score_long,
                    "trigger_met_long":   trigger_met_long,
                    "total_score_short":  total_score_short,
                    "trigger_met_short":  trigger_met_short,
                    "projected_leverage": projected_leverage,
                })
            return {"universe": result, "total": len(result),
                    "scanned_at": scanned_at, "timestamp": _iso(_utcnow())}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/orderbook/{symbol:path}")
    async def get_orderbook(
        symbol: str,
        _: str = Depends(verify_api_key),
    ):
        """[PORT #7] Live orderbook + danger level dari ws_feed.

        [BEDA DARI SPOT -- BUKAN straight copy, bug ditemukan saat investigasi]
        Versi spot (api_server_spot.py get_orderbook) memanggil
        `_get_ob_danger_level(ob)` dengan HANYA 1 argumen (dict orderbook
        mentah) -- tapi _get_ob_danger_level() SUNGGUHAN (baik di spot
        maupun futures) butuh 5 argumen positional wajib:
        (symbol, bids, asks, ratio, confidence). ratio/confidence datang
        dari WhaleDetector.analyze(), BUKAN dari orderbook mentah. Akibatnya
        endpoint itu di spot SELALU TypeError -> HTTP 502 setiap dipanggil,
        dikonfirmasi lewat pembacaan kode langsung (bukan asumsi) -- endpoint
        itu tidak pernah genuinely berfungsi. TIDAK di-replikasi di sini.

        Fix di versi futures ini: hitung ratio/confidence sungguhan lewat
        WhaleDetector.analyze() persis seperti main loop (baris ~1041
        main_future.py), TAPI pakai instance WhaleDetector() BARU per
        request (bukan reuse b._whale_detectors[symbol] yang dipegang live
        scanner loop) -- supaya panggilan endpoint read-only ini TIDAK
        mengotori state internal (_prev_bids/_prev_asks utk spoofing
        detection) yang dipakai keputusan trading live. Konsekuensi: tanpa
        histori tick sebelumnya, spoofing-penalty pada panggilan ini selalu
        netral (confidence tidak dikurangi oleh spoofing) -- dapat diterima
        utk endpoint diagnostik/dashboard read-only, BUKAN utk gating."""
        b   = bot()
        sym = urllib.parse.unquote(symbol).upper()
        if not b.ws_feed:
            raise HTTPException(status_code=503, detail="WebSocket feed tidak aktif")
        try:
            ob   = b.ws_feed.get_orderbook(sym) or {}
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if bids and asks:
                wd  = WhaleDetector()
                res = wd.analyze(sym, bids, asks, {})
                danger = b._get_ob_danger_level(sym, bids, asks, res["ratio"], res["confidence"])
            else:
                danger = 10  # orderbook kosong -- persis _get_ob_danger_level(bids/asks kosong)
            return {
                "symbol":       sym,
                "bids":         bids[:20],
                "asks":         asks[:20],
                "spread_pct":   b.ws_feed.get_spread(sym),
                "mid_price":    b.ws_feed.get_mid_price(sym),
                "danger_level": round(danger, 4),
                "timestamp":    _iso(_utcnow()),
            }
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/api/market_info/{symbol:path}")
    async def get_market_info(symbol: str):
        """[PORT #7 -- straight port] get_market_info() ada di
        engine/exchange_base.py (shared), tidak ada logic spot-only.
        [Parity dgn spot] endpoint ini TIDAK di-guard verify_api_key sama
        sekali di versi spot (dicek langsung) -- dipertahankan identik di
        sini, bukan penyimpangan baru."""
        b      = bot()
        sym    = urllib.parse.unquote(symbol).upper()
        market = b.exchange.get_market_info(sym)
        price_data: dict = {}
        if b.ws_feed:
            ticker     = b.ws_feed.live_tickers.get(sym, {})
            price_data = {
                "last_price":       ticker.get("last"),
                "mid_price":        b.ws_feed.get_mid_price(sym),
                "bid":              ticker.get("bid"),
                "ask":              ticker.get("ask"),
                "spread_pct":       b.ws_feed.get_spread(sym),
                "spread_abs":       b.ws_feed.get_spread_absolute(sym),
                "volume_base_24h":  ticker.get("volume"),
                "volume_quote_24h": ticker.get("quote_volume"),
                "high_24h":         ticker.get("high_24h"),
                "low_24h":          ticker.get("low_24h"),
                "change_pct_24h":   ticker.get("change_pct"),
                "feed_healthy":     b.ws_feed.is_feed_healthy(sym),
            }
        return {**market, **price_data, "timestamp": _iso(_utcnow())}

    @app.get("/api/stream")
    async def stream_events(
        _: str = Depends(verify_api_key),
        request: Request = None,
    ):
        """[AUDIT ITEM #8 -- baru, futures sebelumnya TIDAK PUNYA endpoint
        ini sama sekali] Genuinely event-driven, pola sama persis dgn
        versi spot (lihat spot/api_server_spot.py::stream_events() utk
        latar belakang lengkap kenapa desainnya begini, bukan polling
        interval tetap). Subscribe ke b.event_bus (instance TERPISAH dari
        bus spot -- 2 proses OS, 2 bus in-process, dikonfirmasi user
        via investigasi #8).

        Client: const es = new EventSource('/api/stream', {headers: {'X-API-Key': key}})
        """
        b = bot()

        async def event_generator():
            async with b.event_bus.subscribe() as sub:
                try:
                    positions = await b.db.get_open_positions()
                    tickers   = b.ws_feed.live_tickers if b.ws_feed else {}
                    initial = {
                        "type": "initial_snapshot",
                        "market_type": "futures",
                        "ts": time.time(),
                        "data": {
                            "positions": [_pos_dict(p) for p in positions],
                            "tickers":   {k: {"last": v.get("last"), "change_pct": v.get("change_pct")}
                                          for k, v in tickers.items()},
                            "halted":    b.risk_manager.is_halted if b.risk_manager else False,
                        },
                    }
                    yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
                except Exception as exc:
                    yield f"data: {json.dumps({'type': 'error', 'data': str(exc)}, ensure_ascii=False)}\n\n"

                while True:
                    if request and await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    try:
                        payload = serialize_event(
                            event, pos_dict_fn=_pos_dict, trade_dict_fn=_trade_dict, iso_fn=_iso,
                        )
                        yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    except Exception as exc:
                        log.warning("SSE serialize error (futures) [%s]: %s", event.type, exc)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
