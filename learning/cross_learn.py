"""
learning/cross_learn.py
AlgoTrader Pro v7.0 — Cross-Bot Learning Reader

Membaca data signal_scores dan trades dari database algotrader_test,
menormalisasi nilainya agar sebanding dengan skala algotrader,
lalu menyediakan data tersebut untuk analytics dan meta_learner algotrader.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("learning.cross_learn")

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _get_peer_db_path() -> str:
    return os.getenv("CROSS_LEARN_DB", "")

def _is_enabled() -> bool:
    return os.getenv("CROSS_LEARN_ENABLED", "false").lower() == "true"

# ── Normalisasi ───────────────────────────────────────────────────────────────

# Threshold default per profil untuk algotrader (production)
_PROD_THRESHOLDS = {
    "hodl_accumulate":  66.0,
    "trend_follow":     68.0,
    "breakout_swift":   72.0,
    "scalp_volatile":   71.0,
    "mean_revert":      64.0,
    "extreme_momentum": 76.0,
}

# Threshold default per profil untuk algotrader_test
_TEST_THRESHOLDS = {
    "hodl_accumulate":  61.0,
    "trend_follow":     63.0,
    "breakout_swift":   65.0,
    "scalp_volatile":   64.0,
    "mean_revert":      59.0,
    "extreme_momentum": 71.0,
}

def _normalize_score(
    raw_score:  float,
    profile:    str,
) -> float:
    """
    Normalisasi score dari skala algotrader_test ke skala algotrader.
    Rumus: score_normalized = (raw / test_threshold) * prod_threshold
    Artinya: score yang "cukup" di test, diukur ulang apakah "cukup" di production.
    """
    test_thresh = _TEST_THRESHOLDS.get(profile, 65.0)
    prod_thresh = _PROD_THRESHOLDS.get(profile, 70.0)

    if test_thresh <= 0:
        return raw_score

    normalized = (raw_score / test_thresh) * prod_thresh
    return round(min(100.0, max(0.0, normalized)), 2)

def _normalize_trade_row(row: Dict, profile: str) -> Dict[str, Any]:
    """
    Normalisasi satu baris trade dari algotrader_test
    agar skornya sebanding dengan skala algotrader.
    """
    normalized = dict(row)

    for score_col in [
        "total_score", "trend_score", "momentum_score",
        "strength_score", "volatility_score", "pattern_score",
        "entry_score",
    ]:
        val = normalized.get(score_col)
        if val is not None:
            try:
                normalized[score_col] = _normalize_score(float(val), profile)
            except (TypeError, ValueError):
                pass

    # Tandai bahwa data ini berasal dari peer bot
    normalized["_source"] = "peer_algotrader_test"
    normalized["_normalized"] = True

    return normalized


# ── Reader ────────────────────────────────────────────────────────────────────

class CrossLearnReader:
    """
    Membaca data dari database algotrader_test dan menyediakan
    data yang sudah dinormalisasi untuk dipakai analytics algotrader.
    """

    def __init__(self):
        self._peer_db   = _get_peer_db_path()
        self._enabled   = _is_enabled()
        # [BUG-FIX] _cache/_cache_ts dulu dideklarasikan (dengan TTL 30 menit)
        # tapi TIDAK PERNAH dipakai di get_peer_trades/get_peer_signal_scores/
        # get_peer_regime_stats/get_summary — semua method itu selalu buka
        # koneksi sqlite baru dan query ulang peer DB setiap dipanggil, padahal
        # dalam satu siklus compute_attribution() (analytics.py) method-method
        # ini bisa terpanggil beberapa kali dengan lookback_days yang sama.
        # Ini juga berisiko lock contention karena peer DB sedang aktif ditulis
        # oleh proses algotrader_test lain.
        # Sekarang: _cache jadi dict per-key (key = nama method + params) yang
        # menyimpan (timestamp, value); _is_cache_fresh() menerima key spesifik.
        self._cache:    Dict[str, Any] = {}   # key -> (timestamp, value)
        self._cache_ttl_s = 1800  # cache 30 menit

        log.info(
            "CrossLearnReader init: enabled=%s peer_db=%s",
            self._enabled, self._peer_db or "(tidak ada)",
        )

    def _is_cache_fresh(self, key: str) -> bool:
        entry = self._cache.get(key)
        if entry is None:
            return False
        ts, _ = entry
        age = (_utcnow() - ts).total_seconds()
        return age < self._cache_ttl_s

    def _cache_get(self, key: str) -> Any:
        entry = self._cache.get(key)
        return entry[1] if entry is not None else None

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (_utcnow(), value)

    def _open_peer_db(self) -> Optional[sqlite3.Connection]:
        if not self._peer_db or not os.path.exists(self._peer_db):
            log.debug("CrossLearn: peer DB tidak ditemukan: %s", self._peer_db)
            return None
        try:
            conn = sqlite3.connect(self._peer_db, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            log.error("CrossLearn: gagal buka peer DB: %s", e)
            return None

    def get_peer_trades(
        self,
        lookback_days: int = 30,
        profile:       Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Ambil trades dari algotrader_test, normalisasi skornya,
        return sebagai list dict yang kompatibel dengan format analytics.
        """
        if not self._enabled:
            return []

        # [BUG-FIX] Pakai cache 30 menit yang sekarang benar-benar aktif —
        # sebelumnya method ini selalu query ulang peer DB tiap dipanggil.
        cache_key = f"trades:{lookback_days}:{profile or ''}"
        if self._is_cache_fresh(cache_key):
            return self._cache_get(cache_key)

        conn = self._open_peer_db()
        if conn is None:
            return []

        try:
            query = """
                SELECT
                    symbol,
                    timestamp,
                    strategy_profile,
                    realized_pnl,
                    realized_pnl_pct,
                    strategy_name,
                    notes
                FROM trades
                WHERE timestamp >= datetime('now', ?)
            """
            params: List[Any] = [f"-{lookback_days} days"]

            if profile:
                query += " AND strategy_profile = ?"
                params.append(profile)

            query += " ORDER BY timestamp DESC"

            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()

            result = []
            for row in rows:
                d = dict(row)
                prof = d.get("strategy_profile") or d.get("strategy_name") or ""

                # Tambah field yang dibutuhkan analytics
                d["regime"]       = d.get("regime", "undefined")
                d["entry_score"]  = None  # trades tidak punya score langsung
                d["total_score"]  = None

                d = _normalize_trade_row(d, prof)
                result.append(d)

            log.info(
                "CrossLearn: %d peer trades dimuat (lookback=%dd profile=%s)",
                len(result), lookback_days, profile or "all",
            )
            self._cache_set(cache_key, result)
            return result

        except Exception as e:
            log.error("CrossLearn get_peer_trades: %s", e)
            return []
        finally:
            conn.close()

    def get_peer_signal_scores(
        self,
        lookback_days: int = 30,
        profile:       Optional[str] = None,
        only_triggered: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Ambil signal_scores dari algotrader_test, normalisasi semua skor,
        return sebagai list dict untuk analytics.
        """
        if not self._enabled:
            return []

        # [BUG-FIX] Sama seperti get_peer_trades — pakai cache 30 menit yang
        # sekarang aktif, key mencakup semua parameter query.
        cache_key = f"scores:{lookback_days}:{profile or ''}:{only_triggered}"
        if self._is_cache_fresh(cache_key):
            return self._cache_get(cache_key)

        conn = self._open_peer_db()
        if conn is None:
            return []

        try:
            query = """
                SELECT
                    symbol,
                    timestamp,
                    strategy_profile,
                    total_score,
                    trend_score,
                    momentum_score,
                    strength_score,
                    volatility_score,
                    pattern_score,
                    threshold_used,
                    regime,
                    regime_confidence,
                    trigger_met,
                    signal_type,
                    action_taken,
                    related_trade_id
                FROM signal_scores
                WHERE timestamp >= datetime('now', ?)
            """
            params: List[Any] = [f"-{lookback_days} days"]

            if only_triggered:
                query += " AND trigger_met = 1"
            if profile:
                query += " AND strategy_profile = ?"
                params.append(profile)

            query += " ORDER BY timestamp DESC"

            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()

            result = []
            for row in rows:
                d = dict(row)
                prof = d.get("strategy_profile") or ""

                # Normalisasi semua score ke skala production
                for col in [
                    "total_score", "trend_score", "momentum_score",
                    "strength_score", "volatility_score", "pattern_score",
                ]:
                    val = d.get(col)
                    if val is not None:
                        try:
                            d[col] = _normalize_score(float(val), prof)
                        except (TypeError, ValueError):
                            pass

                # Normalisasi threshold_used juga
                # [BUG-FIX] Sebelumnya "if thresh_raw:" — threshold_used=0.0
                # (nilai valid tapi falsy) akan ke-skip dan raw 0.0 lolos
                # tanpa dinormalisasi. Sekarang cek eksplisit "is not None".
                thresh_raw = d.get("threshold_used")
                if thresh_raw is not None:
                    prod_thresh = _PROD_THRESHOLDS.get(prof, 70.0)
                    d["threshold_used"] = prod_thresh

                # Field tambahan untuk kompatibilitas analytics
                d["entry_score"]      = d.get("total_score")
                d["realized_pnl"]     = 0.0
                d["realized_pnl_pct"] = 0.0
                d["is_win"]           = False  # signal saja, belum tentu trade
                d["_source"]          = "peer_algotrader_test"

                result.append(d)

            log.info(
                "CrossLearn: %d peer signal_scores dimuat (lookback=%dd profile=%s triggered=%s)",
                len(result), lookback_days, profile or "all", only_triggered,
            )
            self._cache_set(cache_key, result)
            return result

        except Exception as e:
            log.error("CrossLearn get_peer_signal_scores: %s", e)
            return []
        finally:
            conn.close()

    def get_peer_regime_stats(
        self,
        lookback_days: int = 30,
    ) -> Dict[str, Dict]:
        """
        Rangkuman performa per regime dari algotrader_test.
        Berguna untuk meta_learner mengetahui kondisi market umum.
        """
        if not self._enabled:
            return {}

        # [BUG-FIX] Cache 30 menit yang sekarang aktif.
        cache_key = f"regime:{lookback_days}"
        if self._is_cache_fresh(cache_key):
            return self._cache_get(cache_key)

        conn = self._open_peer_db()
        if conn is None:
            return {}

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    regime,
                    COUNT(*) as total,
                    AVG(total_score) as avg_score,
                    SUM(CASE WHEN action_taken = 'BUY' THEN 1 ELSE 0 END) as buy_signals,
                    AVG(regime_confidence) as avg_confidence
                FROM signal_scores
                WHERE timestamp >= datetime('now', ?)
                  AND regime IS NOT NULL
                  AND regime != 'undefined'
                GROUP BY regime
                ORDER BY total DESC
            """, (f"-{lookback_days} days",))

            rows = cur.fetchall()
            result = {}
            for row in rows:
                regime = row["regime"]
                result[regime] = {
                    "regime":         regime,
                    "total_signals":  row["total"] or 0,
                    "avg_score":      round(float(row["avg_score"] or 0), 2),
                    "buy_signals":    row["buy_signals"] or 0,
                    "avg_confidence": round(float(row["avg_confidence"] or 0), 3),
                }

            log.info(
                "CrossLearn: %d regime stats dari peer (lookback=%dd)",
                len(result), lookback_days,
            )
            self._cache_set(cache_key, result)
            return result

        except Exception as e:
            log.error("CrossLearn get_peer_regime_stats: %s", e)
            return {}
        finally:
            conn.close()

    def get_summary(self) -> Dict[str, Any]:
        """Ringkasan status cross-learning untuk logging/Telegram."""
        if not self._enabled:
            return {"enabled": False}

        conn = self._open_peer_db()
        if conn is None:
            return {"enabled": True, "peer_db": self._peer_db, "status": "DB tidak ditemukan"}

        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as n FROM trades WHERE timestamp >= datetime('now', '-30 days')")
            trade_count = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) as n FROM signal_scores WHERE timestamp >= datetime('now', '-30 days')")
            score_count = cur.fetchone()["n"]

            cur.execute("""
                SELECT symbol, COUNT(*) as n,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM trades
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY symbol
                ORDER BY n DESC
                LIMIT 5
            """)
            top_coins = [
                {
                    "symbol":   r["symbol"],
                    "trades":   r["n"],
                    "win_rate": round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0.0,
                }
                for r in cur.fetchall()
            ]

            return {
                "enabled":     True,
                "peer_db":     self._peer_db,
                "status":      "OK",
                "trades_30d":  trade_count,
                "scores_30d":  score_count,
                "top_coins":   top_coins,
            }
        except Exception as e:
            return {"enabled": True, "peer_db": self._peer_db, "status": f"error: {e}"}
        finally:
            conn.close()


# ── Singleton ─────────────────────────────────────────────────────────────────

_reader: Optional[CrossLearnReader] = None

def get_cross_learn_reader() -> CrossLearnReader:
    global _reader
    if _reader is None:
        _reader = CrossLearnReader()
    return _reader
