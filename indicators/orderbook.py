"""
indicators/orderbook.py
AlgoTrader Pro — Orderbook / Whale Detector  v3

Changelog v1:
  [MSL-2] WhaleDetector dipindah dari main.py ke sini — separation of concerns.
  [BUG-1] absorbed_bid/ask sekarang benar-benar dideteksi (sebelumnya selalu False).
  [MSL-1] State per-symbol (_SnapshotState) untuk absorption + wall age tracking.
  [BUG-3] _weighted_volume() — level 1-5 bobot 1.0, 6-10 bobot 0.7, 11-20 bobot 0.4.
  [MSL-3] _find_cluster_wall() — deteksi cluster wall (beberapa level berdekatan).
  [MSL-5] Price context: wall distance dari mid-price menentukan relevansi wall.
  [MSL-4] Spread penalty dikontekstualisasi: relatif terhadap median spread coin.
  [MSL-6] liquidity_score — total depth dalam USDT, normalized 0-100.
  [MSL-7] OrderbookIndicators di models.py diperluas dengan sub-scores baru.
  [BUG-2] simulate_test.py import alias dan signature mismatch diperbaiki.

Changelog v3.1:
  [BUG-FIX] imbalance_score — diskontinuitas 10.2 poin di kedua boundary:
            bull branch (imb>=0.62) mulai dari 65.0, tapi neutral branch berakhir
            di 54.8 → lompatan 10.2 yang tidak wajar. Begitu pula di sisi bear.
            Fix: anchor bull/bear branch ke nilai neutral branch di boundary-nya
            (54.8 dan 45.2) sehingga scoring kontinu di seluruh range [0,1].
            Range baru: 10→45.2→50→54.8→90 (monoton, tidak ada lompatan).

Changelog v3:
  [BUG-A FIX] Spoofing penalty sebelumnya membandingkan current vs current karena
              state.prev_bid_walls sudah di-overwrite sebelum _spoofing_penalty
              dipanggil → selalu return 1.0, spoof tidak pernah terdeteksi.
              Fix: simpan prev_bid/ask_snapshot SEBELUM state.prev di-update.
  [BUG-B FIX] _walls_to_dict menyimpan semua 20 level termasuk micro-order kecil.
              Micro-order yang menghilang bisa trigger absorbed=True secara salah.
              Fix: tambah filter level >= WHALE_WALL_PCT * total_usdt (whale-only).
  [MSL-A FIX] _state_registry tumbuh selamanya jika coin dirotasi keluar universe.
              Fix: tambah reset_state(symbol) + cleanup_stale_states(ttl_secs=3600).
              _SnapshotState.last_active di-update setiap akses via _get_state().
  [MSL-B FIX] _SnapshotState.wall_first_seen adalah dead field — didefinisikan tapi
              tidak pernah dipakai (WhaleDetector punya self._wall_first_seen sendiri).
              Field dihapus agar tidak menyesatkan.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from core.models import OrderbookIndicators

# ── Constants ─────────────────────────────────────────────────────────────────
WHALE_WALL_PCT      = 0.08   # level > 8% total volume sisi = whale wall
IMBALANCE_BULL      = 0.62   # bid ratio > 0.62 = tekanan beli
IMBALANCE_BEAR      = 0.38   # bid ratio < 0.38 = tekanan jual
ABSORPTION_DROP_PCT = 0.40   # wall menyusut >40% dalam 1 tick = diserap
CLUSTER_GAP_PCT     = 0.003  # level dalam 0.3% dari level tertinggi = satu cluster
WALL_CLOSE_PCT      = 0.005  # wall < 0.5% dari mid = sangat relevan
WALL_FAR_PCT        = 0.030  # wall > 3.0% dari mid = kurang relevan
LIQUIDITY_SOFT      = 50_000  # USDT — depth di bawah ini = likuiditas rendah
LIQUIDITY_FULL      = 500_000 # USDT — depth di atas ini = likuiditas penuh

# Bobot depth per level — identik dengan WhaleDetector sebelumnya di main.py
_DEPTH_WEIGHTS: List[float] = [
    *([1.0] * 5),   # level 1-5  bobot penuh
    *([0.7] * 5),   # level 6-10 bobot sedang
    *([0.4] * 10),  # level 11-20 bobot rendah
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def clamp_score(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _weighted_volume(levels: list) -> float:
    """Total volume USDT dengan bobot per kedalaman level (BUG-3 fix)."""
    total = 0.0
    for i, (p, q) in enumerate(levels[:20]):
        p, q = float(p), float(q)
        if p <= 0 or q <= 0:
            continue
        w = _DEPTH_WEIGHTS[i] if i < len(_DEPTH_WEIGHTS) else 0.4
        total += p * q * w
    return total


def _raw_volume(levels: list) -> float:
    """Total volume USDT tanpa bobot — untuk spread normalisasi & liquidity."""
    return sum(float(p) * float(q) for p, q in levels if float(p) > 0 and float(q) > 0)


def _filter_min_size(levels: list) -> list:
    """Buang order < 50% median qty — filter noise & micro-order."""
    if not levels:
        return levels
    qtys = [float(v) for _, v in levels if float(v) > 0]
    if not qtys:
        return levels
    med = statistics.median(qtys)
    return [(p, v) for p, v in levels if float(v) >= med * 0.5]


def _find_whale_wall(levels: list, weighted_vol: float) -> Tuple[Optional[float], Optional[float]]:
    """Return (price, strength_pct) dari single level terbesar (weighted)."""
    if weighted_vol <= 0 or not levels:
        return None, None
    best_price, best_strength = None, 0.0
    for i, (p, q) in enumerate(levels[:20]):
        p, q = float(p), float(q)
        if p <= 0 or q <= 0:
            continue
        w = _DEPTH_WEIGHTS[i] if i < len(_DEPTH_WEIGHTS) else 0.4
        strength = (p * q * w) / weighted_vol
        if strength > best_strength:
            best_strength = strength
            best_price    = p
    if best_strength >= WHALE_WALL_PCT:
        return best_price, round(best_strength * 100, 2)
    return None, None


def _find_cluster_wall(
    levels: list,
    weighted_vol: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Deteksi cluster wall — beberapa level berdekatan (dalam CLUSTER_GAP_PCT)
    yang secara kolektif membentuk resistance/support kuat (MSL-3).
    Return (price_center, cluster_strength_pct).
    """
    if weighted_vol <= 0 or not levels:
        return None, None

    # Buat list (price, weighted_usdt) untuk 20 level teratas
    pts: List[Tuple[float, float]] = []
    for i, (p, q) in enumerate(levels[:20]):
        p, q = float(p), float(q)
        if p <= 0 or q <= 0:
            continue
        w = _DEPTH_WEIGHTS[i] if i < len(_DEPTH_WEIGHTS) else 0.4
        pts.append((p, p * q * w))

    if not pts:
        return None, None

    # Kelompokkan level yang harganya dalam CLUSTER_GAP_PCT satu sama lain
    pts.sort(key=lambda x: x[0], reverse=True)
    best_cluster_strength = 0.0
    best_cluster_price    = None

    i = 0
    while i < len(pts):
        anchor_price  = pts[i][0]
        cluster_usdt  = pts[i][1]
        j = i + 1
        while j < len(pts):
            gap = abs(pts[j][0] - anchor_price) / anchor_price
            if gap <= CLUSTER_GAP_PCT:
                cluster_usdt += pts[j][1]
                j += 1
            else:
                break
        strength = cluster_usdt / weighted_vol
        if strength > best_cluster_strength:
            best_cluster_strength = strength
            best_cluster_price    = anchor_price
        i = j if j > i else i + 1

    if best_cluster_strength >= WHALE_WALL_PCT:
        return best_cluster_price, round(best_cluster_strength * 100, 2)
    return None, None


def _wall_distance_factor(wall_price: Optional[float], mid: float) -> float:
    """
    MSL-5: Factor relevansi wall berdasarkan jarak dari mid-price.
    Wall dekat (< 0.5%) = relevansi 1.0. Wall jauh (> 3%) = relevansi 0.2.
    """
    if wall_price is None or mid <= 0:
        return 0.5
    dist = abs(wall_price - mid) / mid
    if dist <= WALL_CLOSE_PCT:
        return 1.0
    if dist >= WALL_FAR_PCT:
        return 0.2
    # Interpolasi linear antara WALL_CLOSE_PCT dan WALL_FAR_PCT
    t = (dist - WALL_CLOSE_PCT) / (WALL_FAR_PCT - WALL_CLOSE_PCT)
    return round(1.0 - 0.8 * t, 4)


def _spoofing_penalty(
    levels: list,
    prev: Dict[float, float],
) -> float:
    """
    Deteksi spoofing: wall besar yang menghilang dalam 1 tick = spoof.
    Return 0.3-1.0, makin rendah makin banyak spoof.
    """
    if not prev:
        return 1.0
    curr_map = {float(p): float(v) for p, v in levels[:20]}
    prev_vals = list(prev.values()) or [0.0]
    med_prev  = statistics.median(prev_vals)
    disappeared = sum(
        v for p, v in prev.items()
        if p not in curr_map and v > med_prev * 2
    )
    total_prev = sum(prev.values()) or 1
    spoof_ratio = min(disappeared / total_prev, 1.0)
    return max(0.3, 1.0 - spoof_ratio)


def _liquidity_score(total_raw_usdt: float) -> float:
    """
    MSL-6: Likuiditas pasar dalam USDT → skor 0-100.
    < LIQUIDITY_SOFT  = skor rendah (risiko slippage tinggi).
    > LIQUIDITY_FULL  = skor 100 (aman untuk position sizing apapun).
    """
    if total_raw_usdt <= 0:
        return 0.0
    if total_raw_usdt >= LIQUIDITY_FULL:
        return 100.0
    if total_raw_usdt <= LIQUIDITY_SOFT:
        t = total_raw_usdt / LIQUIDITY_SOFT
        return round(t * 40.0, 2)   # 0-40 untuk thin market
    t = (total_raw_usdt - LIQUIDITY_SOFT) / (LIQUIDITY_FULL - LIQUIDITY_SOFT)
    return round(40.0 + t * 60.0, 2)  # 40-100


# ── Per-symbol state untuk absorption & wall age tracking (MSL-1 fix) ────────
@dataclass
class _SnapshotState:
    """State per-symbol untuk perbandingan antar tick."""
    prev_bid_walls: Dict[float, float] = field(default_factory=dict)  # price→usdt_weighted
    prev_ask_walls: Dict[float, float] = field(default_factory=dict)
    prev_ts:        float              = 0.0
    # [MSL-B FIX] wall_first_seen dihapus — field ini tidak pernah dipakai di
    # calculate_orderbook(). WhaleDetector mengelola wall age via self._wall_first_seen
    # internal sendiri. Field di sini adalah dead code yang menyesatkan.
    spread_history:  List[float]       = field(default_factory=list)   # rolling spread
    last_active:     float             = field(default_factory=time.time)  # untuk TTL cleanup


_state_registry: Dict[str, _SnapshotState] = {}

_STATE_TTL_SECS = 3600.0  # state coin yang tidak aktif > 1 jam dihapus otomatis


def _get_state(symbol: str) -> _SnapshotState:
    """Ambil state per-symbol. Buat baru jika belum ada. Update last_active."""
    if symbol not in _state_registry:
        _state_registry[symbol] = _SnapshotState()
    state = _state_registry[symbol]
    state.last_active = time.time()
    return state


def reset_state(symbol: str) -> None:
    """[MSL-A FIX] Hapus state coin tertentu — panggil saat coin dikeluarkan dari universe."""
    _state_registry.pop(symbol, None)
    log.debug("orderbook: state '%s' direset", symbol)


def cleanup_stale_states(ttl_secs: float = _STATE_TTL_SECS) -> int:
    """[MSL-A FIX] Hapus state coin yang tidak aktif lebih dari ttl_secs.

    Panggil dari main loop periodik (misal setiap jam) agar _state_registry
    tidak tumbuh selamanya ketika coin dirotasi keluar dari universe.

    Returns:
        Jumlah state yang dihapus.
    """
    now   = time.time()
    stale = [sym for sym, st in _state_registry.items()
             if (now - st.last_active) > ttl_secs]
    for sym in stale:
        del _state_registry[sym]
    if stale:
        log.info("orderbook: cleanup %d stale state(s): %s", len(stale), stale)
    return len(stale)


def _detect_absorption(
    current_walls: Dict[float, float],
    prev_walls:    Dict[float, float],
) -> bool:
    """
    BUG-1 + MSL-1 fix: Cek apakah whale wall yang ada di tick sebelumnya
    sudah menyusut > ABSORPTION_DROP_PCT (diserap market).
    """
    if not prev_walls:
        return False
    for price, prev_usdt in prev_walls.items():
        curr_usdt = current_walls.get(price, 0.0)
        if prev_usdt > 0 and (prev_usdt - curr_usdt) / prev_usdt >= ABSORPTION_DROP_PCT:
            return True
    return False


def _walls_to_dict(levels: list, total_usdt: float = 0.0) -> Dict[float, float]:
    """Konversi levels ke dict price→weighted_usdt untuk perbandingan absorption.

    [BUG-B FIX] Hanya simpan level yang kekuatannya >= WHALE_WALL_PCT * total_usdt.
    Sebelumnya semua 20 level disimpan sehingga micro-order kecil di level 18
    yang menghilang bisa trigger absorbed=True. Sekarang hanya whale-level wall
    yang masuk dict — absorption hanya aktif untuk wall yang benar-benar signifikan.
    """
    result: Dict[float, float] = {}
    whale_threshold = total_usdt * WHALE_WALL_PCT if total_usdt > 0 else 0.0
    for i, (p, q) in enumerate(levels[:20]):
        p, q = float(p), float(q)
        if p <= 0 or q <= 0:
            continue
        w    = _DEPTH_WEIGHTS[i] if i < len(_DEPTH_WEIGHTS) else 0.4
        usdt = p * q * w
        if whale_threshold <= 0 or usdt >= whale_threshold:
            result[p] = usdt
    return result


# ── WhaleDetector (dipindah dari main.py — MSL-2 fix) ────────────────────────
class WhaleDetector:
    """
    Weighted orderbook analyzer dengan:
    - Spoofing detection (wall menghilang dalam 1 tick)
    - Minimum size filter (buang micro-order)
    - Wall age tracking (fresh wall lebih dipercaya)
    - Dynamic threshold per coin (tipis vs tebal)
    - Absorption detection (wall diserap market)

    Sebelumnya ada di main.py (line 91). Dipindah ke sini (MSL-2).
    main.py import dari sini; tidak ada duplikasi logika.
    """

    def __init__(self) -> None:
        self._prev_bids: Dict[float, float] = {}
        self._prev_asks: Dict[float, float] = {}
        self._prev_ts:   float              = 0.0
        self._wall_first_seen: Dict[str, float] = {}

    def _weighted_wall(self, levels: list) -> float:
        return _weighted_volume(levels)

    def _filter_min_size(self, levels: list) -> list:
        return _filter_min_size(levels)

    def _spoofing_penalty(self, levels: list, prev: Dict[float, float]) -> float:
        return _spoofing_penalty(levels, prev)

    def _dynamic_threshold(self, bids: list, asks: list) -> Tuple[float, float]:
        """Threshold dinamis: coin tipis lebih longgar, coin tebal lebih ketat."""
        all_vals = [
            float(p) * float(v)
            for p, v in (bids + asks)[:40]
            if float(p) > 0 and float(v) > 0
        ]
        if not all_vals:
            return 0.65, 1.55
        depth      = sum(all_vals)
        med        = statistics.median(all_vals)
        depth_norm = min(depth / (med * 40 + 1e-9), 1.0)
        thr_sell   = round(0.50 + 0.20 * depth_norm, 3)  # 0.50–0.70
        thr_buy    = round(1.70 - 0.20 * depth_norm, 3)  # 1.50–1.70
        return thr_sell, thr_buy

    def analyze(
        self,
        symbol:          str,
        bids:            list,
        asks:            list,
        wall_first_seen: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """
        Analisis orderbook lengkap.
        wall_first_seen boleh None — WhaleDetector akan kelola state internalnya.
        """
        now = time.time()
        wfs = wall_first_seen if wall_first_seen is not None else self._wall_first_seen

        # Filter noise
        bids_f = self._filter_min_size(bids)
        asks_f = self._filter_min_size(asks)

        # Weighted wall
        bid_wall = self._weighted_wall(bids_f)
        ask_wall = self._weighted_wall(asks_f)
        ratio    = bid_wall / ask_wall if ask_wall > 0 else 1.0

        # Spoofing penalty
        penalty_b  = self._spoofing_penalty(bids_f, self._prev_bids)
        penalty_a  = self._spoofing_penalty(asks_f, self._prev_asks)
        confidence = round((penalty_b + penalty_a) / 2, 3)

        # Update snapshot
        self._prev_bids = {float(p): float(v) for p, v in bids_f[:20]}
        self._prev_asks = {float(p): float(v) for p, v in asks_f[:20]}
        self._prev_ts   = now

        # Wall age adjustment
        key = f"{symbol}_wall"
        if ratio < 0.80 or ratio > 1.25:
            if key not in wfs:
                wfs[key] = now
            age = now - wfs[key]
            if age < 30:
                confidence = min(confidence * 1.20, 1.0)   # fresh +20%
            elif age > 300:
                confidence = confidence * 0.80              # stale -20%
        else:
            wfs.pop(key, None)

        thr_sell, thr_buy = self._dynamic_threshold(bids, asks)

        return {
            "ratio":      round(ratio, 4),
            "confidence": confidence,
            "bid_wall":   round(bid_wall, 2),
            "ask_wall":   round(ask_wall, 2),
            "thr_sell":   thr_sell,
            "thr_buy":    thr_buy,
        }


# ── Main stateful calculator ──────────────────────────────────────────────────
def calculate_orderbook(ob: dict, symbol: str = "_default") -> dict:
    """
    Hitung semua metrik orderbook untuk satu snapshot.
    symbol dipakai untuk state tracking (absorption & wall age).
    """
    result: Dict = {
        "bid_ask_imbalance":  None,
        "whale_bid_wall":     None,
        "whale_ask_wall":     None,
        "bid_wall_strength":  None,
        "ask_wall_strength":  None,
        "cluster_bid_wall":   None,
        "cluster_bid_str":    None,
        "cluster_ask_wall":   None,
        "cluster_ask_str":    None,
        "spread_pct":         None,
        "absorbed_bid":       False,
        "absorbed_ask":       False,
        "bid_wall_dist":      None,   # jarak wall dari mid (0-1 relevance factor)
        "ask_wall_dist":      None,
        "liquidity_score":    50.0,
        "spoofing_confidence": 1.0,
        "imbalance_score":    50.0,
        "whale_score":        50.0,
        "spread_score":       50.0,
        "absorption_score":   50.0,
    }

    if not ob:
        return result

    bids: list = ob.get("bids", [])
    asks: list = ob.get("asks", [])
    if not bids or not asks:
        return result

    state = _get_state(symbol)

    # ── Spread ────────────────────────────────────────────────────────────────
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0

    if best_bid > 0 and best_ask > 0 and mid > 0:
        spread_pct = round((best_ask - best_bid) / mid * 100, 4)
        result["spread_pct"] = spread_pct
        # Rolling spread history untuk normalisasi kontekstual (MSL-4)
        state.spread_history.append(spread_pct)
        if len(state.spread_history) > 50:
            state.spread_history.pop(0)

    # ── Volume ────────────────────────────────────────────────────────────────
    bids_f = _filter_min_size(bids)
    asks_f = _filter_min_size(asks)

    bid_vol_w = _weighted_volume(bids_f)
    ask_vol_w = _weighted_volume(asks_f)
    total_w   = bid_vol_w + ask_vol_w

    bid_vol_raw = _raw_volume(bids)
    ask_vol_raw = _raw_volume(asks)
    total_raw   = bid_vol_raw + ask_vol_raw

    # ── Imbalance ─────────────────────────────────────────────────────────────
    if total_w > 0:
        imb = round(bid_vol_w / total_w, 4)
        result["bid_ask_imbalance"] = imb

        # [BUG-FIX v3.1] Diskontinuitas 10.2 poin di kedua boundary imbalance:
        # Neutral branch berakhir di 54.8 saat imb=0.62, tapi bull branch dimulai
        # dari 65.0 — lompatan yang tidak masuk akal. Begitu pula di sisi bear.
        # Fix: anchor bull/bear branch tepat di nilai yang dihasilkan neutral branch,
        # sehingga scoring kontinu di seluruh range [0, 1].
        # anchor_bull = 50 + (IMBALANCE_BULL - 0.5) * 40 = 54.8
        # anchor_bear = 50 + (IMBALANCE_BEAR - 0.5) * 40 = 45.2
        _anchor_bull = 50.0 + (IMBALANCE_BULL - 0.5) * 40.0  # ~54.8
        _anchor_bear = 50.0 + (IMBALANCE_BEAR - 0.5) * 40.0  # ~45.2
        if imb >= IMBALANCE_BULL:
            t = (imb - IMBALANCE_BULL) / (1.0 - IMBALANCE_BULL)
            result["imbalance_score"] = clamp_score(_anchor_bull + t * (90.0 - _anchor_bull))
        elif imb <= IMBALANCE_BEAR:
            t = (IMBALANCE_BEAR - imb) / IMBALANCE_BEAR
            result["imbalance_score"] = clamp_score(_anchor_bear - t * (_anchor_bear - 10.0))
        else:
            result["imbalance_score"] = clamp_score(50.0 + (imb - 0.5) * 40.0)

    # ── Whale walls (single level) ────────────────────────────────────────────
    wb_price, wb_str = _find_whale_wall(bids_f, bid_vol_w)
    wa_price, wa_str = _find_whale_wall(asks_f, ask_vol_w)
    result["whale_bid_wall"]    = wb_price
    result["bid_wall_strength"] = wb_str
    result["whale_ask_wall"]    = wa_price
    result["ask_wall_strength"] = wa_str

    # ── Cluster walls (MSL-3) ─────────────────────────────────────────────────
    cb_price, cb_str = _find_cluster_wall(bids_f, bid_vol_w)
    ca_price, ca_str = _find_cluster_wall(asks_f, ask_vol_w)
    result["cluster_bid_wall"] = cb_price
    result["cluster_bid_str"]  = cb_str
    result["cluster_ask_wall"] = ca_price
    result["cluster_ask_str"]  = ca_str

    # ── Wall distance relevance factor (MSL-5) ────────────────────────────────
    effective_bid_wall = wb_price or cb_price
    effective_ask_wall = wa_price or ca_price
    bid_dist_factor = _wall_distance_factor(effective_bid_wall, mid)
    ask_dist_factor = _wall_distance_factor(effective_ask_wall, mid)
    result["bid_wall_dist"] = bid_dist_factor
    result["ask_wall_dist"] = ask_dist_factor

    # ── Absorption detection (BUG-1 + MSL-1 fix) ─────────────────────────────
    curr_bid_dict = _walls_to_dict(bids_f, total_raw)
    curr_ask_dict = _walls_to_dict(asks_f, total_raw)

    if state.prev_ts > 0:  # Hanya cek jika ada snapshot sebelumnya
        result["absorbed_bid"] = _detect_absorption(curr_bid_dict, state.prev_bid_walls)
        result["absorbed_ask"] = _detect_absorption(curr_ask_dict, state.prev_ask_walls)

    # [BUG-A FIX] Simpan snapshot prev SEBELUM state di-update — dipakai spoofing di bawah.
    # Sebelumnya spoofing menggunakan state.prev_bid_walls SETELAH di-overwrite curr,
    # sehingga membandingkan current vs current → _spoofing_penalty selalu return 1.0.
    prev_bid_snapshot = dict(state.prev_bid_walls)
    prev_ask_snapshot = dict(state.prev_ask_walls)

    # Update state
    state.prev_bid_walls = curr_bid_dict
    state.prev_ask_walls = curr_ask_dict
    state.prev_ts        = time.time()

    # ── Spoofing confidence ───────────────────────────────────────────────────
    # [BUG-A FIX] Pakai prev_snapshot (sebelum update) bukan state.prev yang sudah = curr
    pen_b = _spoofing_penalty(bids_f, prev_bid_snapshot)
    pen_a = _spoofing_penalty(asks_f, prev_ask_snapshot)
    result["spoofing_confidence"] = round((pen_b + pen_a) / 2, 3)

    # ── Liquidity score (MSL-6) ───────────────────────────────────────────────
    result["liquidity_score"] = _liquidity_score(total_raw)

    # ── Whale sub-score (dengan distance factor — MSL-5) ─────────────────────
    whale_score = 50.0
    if wb_str:
        whale_score += min(wb_str * 0.3, 8.0) * bid_dist_factor
    if wa_str:
        whale_score -= min(wa_str * 0.3, 8.0) * ask_dist_factor
    if cb_str and cb_price != wb_price:   # cluster bonus jika beda dari single wall
        whale_score += min(cb_str * 0.2, 5.0) * bid_dist_factor
    if ca_str and ca_price != wa_price:
        whale_score -= min(ca_str * 0.2, 5.0) * ask_dist_factor
    result["whale_score"] = clamp_score(whale_score)

    # ── Spread sub-score (kontekstual — MSL-4) ────────────────────────────────
    spread_score = 80.0  # default bagus
    if result["spread_pct"] is not None:
        sp = result["spread_pct"]
        # Gunakan median historis sebagai baseline "normal" untuk coin ini
        baseline = statistics.median(state.spread_history) if len(state.spread_history) >= 3 else 0.1
        relative = sp / max(baseline, 0.01)
        if relative > 3.0:
            spread_score = 20.0   # spread 3x+ normal = sangat buruk
        elif relative > 2.0:
            spread_score = 40.0
        elif relative > 1.5:
            spread_score = 60.0
        elif relative > 1.0:
            spread_score = 70.0
        else:
            spread_score = 85.0
    result["spread_score"] = spread_score

    # ── Absorption sub-score ──────────────────────────────────────────────────
    absorption_score = 50.0
    if result["absorbed_ask"]:
        absorption_score += 15.0   # ask wall diserap = breakout signal
    if result["absorbed_bid"]:
        absorption_score -= 15.0   # bid wall diserap = breakdown signal
    result["absorption_score"] = clamp_score(absorption_score)

    return result


def score_orderbook(data: dict) -> float:
    """
    Hitung composite orderbook score 0-100 dari semua sub-komponen.
    Komponen: imbalance (40%) + whale (25%) + absorption (20%) + spread (10%) + liquidity (5%).
    """
    if data.get("bid_ask_imbalance") is None:
        return 50.0

    imb_score  = data.get("imbalance_score",   50.0)
    whl_score  = data.get("whale_score",        50.0)
    abs_score  = data.get("absorption_score",   50.0)
    spr_score  = data.get("spread_score",       80.0)
    liq_score  = data.get("liquidity_score",    50.0)
    spoof_conf = data.get("spoofing_confidence", 1.0)

    # Weighted composite
    raw = (
        imb_score * 0.40
        + whl_score  * 0.25
        + abs_score  * 0.20
        + spr_score  * 0.10
        + liq_score  * 0.05
    )

    # Spoofing confidence penalty: jika banyak spoof, skor menuju netral
    score = raw * spoof_conf + 50.0 * (1.0 - spoof_conf)

    return clamp_score(score)


# ── Public entry point ────────────────────────────────────────────────────────
def score_orderbook_data(ob: dict, errors: Optional[list] = None, symbol: str = "_default") -> OrderbookIndicators:
    """
    Entry point utama — dipanggil dari observer.py.
    Menerima ob dict dari live_orderbooks[symbol] dan mengembalikan OrderbookIndicators.

    Args:
        ob:     Dict {"bids": [...], "asks": [...], "_ts": float}
        errors: List untuk append pesan error (opsional)
        symbol: Nama simbol untuk state tracking per-coin
    """
    result = OrderbookIndicators()
    if not ob:
        return result
    try:
        sym  = ob.get("symbol", symbol)
        data = calculate_orderbook(ob, symbol=sym)

        result.bid_ask_imbalance    = data["bid_ask_imbalance"]
        result.whale_bid_wall       = data["whale_bid_wall"]
        result.whale_ask_wall       = data["whale_ask_wall"]
        result.bid_wall_strength    = data["bid_wall_strength"]
        result.ask_wall_strength    = data["ask_wall_strength"]
        result.cluster_bid_wall     = data["cluster_bid_wall"]
        result.cluster_bid_str      = data["cluster_bid_str"]
        result.cluster_ask_wall     = data["cluster_ask_wall"]
        result.cluster_ask_str      = data["cluster_ask_str"]
        result.spread_pct           = data["spread_pct"]
        result.absorbed_bid         = data["absorbed_bid"]
        result.absorbed_ask         = data["absorbed_ask"]
        result.bid_wall_dist        = data["bid_wall_dist"]
        result.ask_wall_dist        = data["ask_wall_dist"]
        result.liquidity_score      = data["liquidity_score"]
        result.spoofing_confidence  = data["spoofing_confidence"]
        result.imbalance_score      = data["imbalance_score"]
        result.whale_score          = data["whale_score"]
        result.spread_score         = data["spread_score"]
        result.absorption_score     = data["absorption_score"]
        result.orderbook_score      = score_orderbook(data)
        result.composite_score      = result.orderbook_score
    except Exception as exc:
        if errors is not None:
            errors.append(f"orderbook: {exc}")
        log.exception("Error kalkulasi orderbook [%s]: %s", symbol, exc)
    return result
