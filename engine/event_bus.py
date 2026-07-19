"""
engine/event_bus.py -- In-process pub/sub broadcaster untuk push/SSE
(audit item #8, langkah 1/4).

[LATAR BELAKANG -- investigasi #8] Spot & futures adalah 2 PROSES OS
terpisah (port 8000/8001), tidak share memori sama sekali -- bus ini
genuinely in-process PER BOT, BUKAN cross-process. Konfirmasi user:
klien (browser) nanti buka 2 EventSource terpisah, tidak ada gateway/
proxy yang menggabungkan dibangun sekarang.

Pola injeksi opsional SAMA PERSIS dengan WebSocketFeed.on_ticker
(spot/exchange_spot.py) -- default None, `if bus: bus.publish(...)`,
tidak memaksa dependency ke context yang tidak serve API (test, script,
migrasi DB).

[Desain non-blocking -- KRUSIAL] publish() SENGAJA fungsi SINKRON (bukan
`async def`), dipanggil TANPA await dari caller manapun (mis.
engine/database.py sesudah commit tulis DB). Tidak pernah ada titik
suspend di publish() -- pakai put_nowait() ke tiap queue subscriber, kalau
penuh (subscriber lambat/macet di sisi client) event TERLAMA di queue itu
dibuang supaya event TERBARU tetap masuk (drop-oldest), TIDAK PERNAH
memblokir publisher menunggu client lambat. Ini prasyarat wajib -- kalau
publish() bisa nge-block, dia bisa menunda commit DB (caller Tier 1 di
database.py) hanya karena satu browser tab lupa ditutup.

Thread-safety: asyncio.Queue TIDAK thread-safe lintas OS thread. Asumsi
desain (diverifikasi thd caller Tier 1 di database.py): publish() SELALU
dipanggil dari event loop thread yang sama tempat EventBus dibuat (semua
write DB Tier 1 adalah async method biasa, bukan dijalankan lewat
run_in_executor()) -- kalau nanti ada caller dari worker thread lain,
perlu jembatan `loop.call_soon_threadsafe()`, BUKAN panggil publish()
langsung dari thread itu.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("event_bus")

DEFAULT_QUEUE_MAXSIZE = 200


@dataclass
class Event:
    """Amplop generik seragam -- payload `data` SENGAJA tidak diseragamkan
    (lihat investigasi #8 poin 4): spot & futures publish objek ORM mentah
    apa adanya (Position/Trade dari engine/database.py), serialisasi
    (_pos_dict()/_trade_dict(), beda field per market_type) tetap di lapis
    API server, bukan di sini."""
    type: str
    data: Any
    market_type: Optional[str] = None
    ts: float = field(default_factory=time.time)


class EventBus:
    """Broadcaster in-process, 1 instance per proses bot (spot ATAU
    futures, tidak dishare)."""

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE):
        self._queue_maxsize = queue_maxsize
        self._subscribers: Dict[int, "asyncio.Queue[Event]"] = {}
        self._next_id = 0

    def subscribe(self) -> "Subscription":
        sub_id = self._next_id
        self._next_id += 1
        q: "asyncio.Queue[Event]" = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers[sub_id] = q
        return Subscription(bus=self, sub_id=sub_id, queue=q)

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def publish(self, event_type: str, data: Any, market_type: Optional[str] = None) -> None:
        """SINKRON, TIDAK PERNAH di-await -- lihat catatan desain di
        docstring modul. Aman dipanggil walau nol subscriber (no-op)."""
        if not self._subscribers:
            return
        event = Event(type=event_type, data=data, market_type=market_type)
        for sub_id, q in list(self._subscribers.items()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.debug(
                        "EventBus: subscriber %d queue penuh & gagal drop-oldest, event %s dibuang.",
                        sub_id, event_type,
                    )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class Subscription:
    """Handle per-client. Dukung `async with` (auto-unsubscribe) atau
    unsubscribe() manual -- dipanggil dari endpoint SSE saat client
    disconnect (lihat request.is_disconnected() di layer API)."""

    def __init__(self, bus: EventBus, sub_id: int, queue: "asyncio.Queue[Event]"):
        self._bus = bus
        self._id = sub_id
        self.queue = queue

    def unsubscribe(self) -> None:
        self._bus.unsubscribe(self._id)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.unsubscribe()


DEFAULT_TICKER_THROTTLE_SECS = 1.5


class ThrottledTickerPublisher:
    """[AUDIT ITEM #8, langkah 3/4] Wrapper untuk WebSocketFeed.on_ticker
    (spot/exchange_spot.py) -- hook itu SUDAH ADA sejak awal (dipanggil di
    _watch_tickers_all(), _watch_ticker(), _poll_one_ticker()) tapi TIDAK
    PERNAH disambungkan ke apa pun (dikonfirmasi: on_ticker= tidak pernah
    diisi di instansiasi WebSocketFeed manapun). Binance WS bisa kirim tick
    beberapa kali per detik per symbol -- publish mentah per-tick akan
    membanjiri subscriber SSE. Throttle PER SYMBOL (bukan global) supaya
    symbol yang jarang bergerak tidak menunggu symbol lain yang ramai.

    Dipanggil sbg `on_ticker=` -- signature HARUS cocok dgn
    `await self.on_ticker(symbol, ticker_dict)` (lihat exchange_spot.py)."""

    def __init__(
        self,
        bus: EventBus,
        market_type: str,
        throttle_interval_secs: float = DEFAULT_TICKER_THROTTLE_SECS,
    ):
        self._bus = bus
        self._market_type = market_type
        self._interval = throttle_interval_secs
        self._last_published_at: Dict[str, float] = {}

    async def __call__(self, symbol: str, ticker: Dict[str, Any]) -> None:
        now = time.monotonic()
        last = self._last_published_at.get(symbol, 0.0)
        if now - last < self._interval:
            return
        self._last_published_at[symbol] = now
        self._bus.publish("ticker", {"symbol": symbol, **ticker}, market_type=self._market_type)


class KeyedLogThrottle:
    """[#23 -- audit fungsional] Rate-limiter generik per-key -- pola SAMA
    persis dgn ThrottledTickerPublisher di atas (item #8): dict timestamp
    terakhir per key, izinkan lewat kalau sudah >= interval sejak terakhir
    kali key itu lolos. BEDA konteks (keputusan boolean SINKRON utk gating
    satu baris log, bukan publish event async ke EventBus) makanya
    primitive terpisah -- tapi bentuk/gaya throttle-nya SENGAJA disamakan
    (bukan reinvent gaya lain) supaya cuma ada SATU pola throttle-per-key
    di seluruh codebase ini, dipakai reuse di mana pun butuh rate-limit
    per-key (ticker publish, log gate, dst -- generik, TIDAK spesifik
    logging walau nama kelasnya "LogThrottle").

    `key` FLEKSIBEL -- boleh str (symbol saja, spt spot yang tidak kenal
    `side`) ATAU tuple (mis. (symbol, side) utk futures, supaya throttle
    long & short independen -- BUKAN saling menahan, pola sama dgn
    `self._invalidation_signals.get((symbol, cand_side))` yang sudah
    established di main_future.py).
    """

    def __init__(self, interval_secs: float):
        self._interval = interval_secs
        self._last_at: Dict[Any, float] = {}

    def allow(self, key: Any) -> bool:
        now = time.monotonic()
        last = self._last_at.get(key, 0.0)
        if now - last < self._interval:
            return False
        self._last_at[key] = now
        return True


def serialize_event(
    event: Event,
    pos_dict_fn: Callable[[Any], Dict[str, Any]],
    trade_dict_fn: Callable[[Any], Dict[str, Any]],
    iso_fn: Callable[[Optional[Any]], Optional[str]],
) -> Dict[str, Any]:
    """[AUDIT ITEM #8, langkah 4/4] Dispatch serialisasi event.data (objek
    ORM mentah/dict minimal, lihat engine/database.py Tier 1/Tier 2 &
    engine/risk_base.py halt_changed) -> dict siap-JSON, dipanggil dari
    endpoint /api/stream KEDUA bot sebelum yield ke client.

    [Prinsip desain #8 poin 4 -- payload tidak dipaksa 1 skema] pos_dict_fn/
    trade_dict_fn DISUNTIK dari lapis API (spot/api_server_spot.py atau
    future/api_server_future.py, masing2 _pos_dict()/_trade_dict() sendiri
    -- futures nambah leverage/margin_mode/dst). engine/ (modul ini)
    SENGAJA TIDAK tahu bentuk field spot vs futures -- cuma tahu KAPAN
    memanggil serializer mana per event.type. Event tipe lain (universe
    override, parameter_changed, snapshot) field-nya genuinely sama utk
    kedua bot (tidak ada konsep leverage di situ), jadi di-serialize
    langsung di sini tanpa perlu injeksi tambahan -- mirror field yang
    SUDAH dipakai endpoint GET terkait (/api/universe/overrides,
    /api/meta_learner/history, /api/equity_curve) supaya konsisten dgn
    REST API yang sudah ada.
    """
    t = event.type
    data = event.data

    if t == "trade":
        payload = trade_dict_fn(data)
    elif t in ("position_upserted", "position_closed"):
        payload = pos_dict_fn(data)
    elif t == "positions_snapshot":
        payload = [pos_dict_fn(p) for p in data]
    elif t == "position_closing":
        payload = data  # sudah dict minimal {"symbol": ...}, lihat database.py
    elif t == "universe_override_added":
        payload = {
            "symbol":    data.symbol,
            "source":    data.source,
            "is_active": data.is_active,
            "added_at":  iso_fn(data.added_at),
            "notes":     data.notes,
        }
    elif t == "universe_override_removed":
        payload = data  # sudah dict minimal {"symbol": ...}
    elif t == "parameter_changed":
        payload = {
            "id":             data.id,
            "timestamp":      iso_fn(data.timestamp),
            "symbol":         data.symbol,
            "profile":        data.profile,
            "parameter_name": data.parameter_name,
            "old_value":      data.old_value,
            "new_value":      data.new_value,
            "reason":         data.reason,
            "approved_by":    data.approved_by,
            "outcome":        data.outcome,
        }
    elif t == "snapshot":
        payload = {
            "timestamp":     iso_fn(data.timestamp),
            "equity":        data.total_equity,
            "drawdown":      data.drawdown_pct,
            "daily_pnl":     data.daily_pnl,
            "daily_pnl_pct": data.daily_pnl_pct,
        }
    elif t in ("ticker", "halt_changed"):
        payload = data  # sudah dict siap pakai, lihat ThrottledTickerPublisher/risk_base.py
    else:
        payload = data if isinstance(data, dict) else str(data)

    return {"type": t, "market_type": event.market_type, "ts": event.ts, "data": payload}
