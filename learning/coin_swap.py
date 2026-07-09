"""
learning/coin_swap.py
AlgoTrader Pro v7.0 — Cross-Bot Coin Swap Engine

Membaca performa coin di algotrader_test, membandingkan dengan coin algotrader,
lalu swap otomatis coin terbaik ke algotrader dan coin terlemah ke algotrader_test.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("learning.coin_swap")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _read_env_universe(env_path: str) -> List[str]:
    """Baca UNIVERSE_WATCHLIST dari file .env tanpa load dotenv."""
    # [BUG-FIX] Key salah: dulu cek "WATCHLIST=" padahal SEMUA file lain di
    # sistem (main.py, telegram_bot.py, dan bahkan _restart_bots() di file
    # ini sendiri) memakai key "UNIVERSE_WATCHLIST". Akibatnya fungsi ini
    # SELALU return [] karena baris "WATCHLIST=" tidak pernah ada di .env
    # manapun — membuat seluruh CoinSwapEngine (run_cycle & _triggered_swap)
    # selalu abort di pengecekan "universe kosong" dan tidak pernah benar-benar
    # jalan.
    # Sebelumnya: if line.startswith("WATCHLIST="):
    # Sekarang: cek key yang benar sesuai konvensi seluruh sistem.
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("UNIVERSE_WATCHLIST="):
                    val = line.split("=", 1)[1].strip()
                    return [s.strip() for s in val.split(",") if s.strip()]
    except Exception as e:
        log.error("_read_env_universe(%s): %s", env_path, e)
    return []

def _write_env_universe(env_path: str, new_universe: List[str]) -> bool:
    """Update UNIVERSE_WATCHLIST di file .env tanpa mengubah baris lain."""
    # [BUG-FIX] Sama seperti _read_env_universe: key lama "WATCHLIST=" tidak
    # pernah dibaca oleh main.py/telegram_bot.py. Kalau fungsi ini sempat
    # kepanggil, ia akan menambah baris "WATCHLIST=..." baru yang tidak
    # berefek apa pun ke bot (bot baca UNIVERSE_WATCHLIST), sehingga swap
    # terlihat "berhasil" di log tapi watchlist asli tidak pernah berubah.
    # Sebelumnya: cari/replace/append baris "WATCHLIST=..."
    # Sekarang: cari/replace/append baris "UNIVERSE_WATCHLIST=..." — konsisten
    # dengan key yang dipakai di seluruh sistem.
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()

        new_val = ",".join(new_universe)
        updated = False
        for i, line in enumerate(lines):
            if line.strip().startswith("UNIVERSE_WATCHLIST="):
                lines[i] = f"UNIVERSE_WATCHLIST={new_val}\n"
                updated = True
                break

        if not updated:
            lines.append(f"UNIVERSE_WATCHLIST={new_val}\n")

        with open(env_path, "w") as f:
            f.writelines(lines)

        log.info("WATCHLIST updated: %s → %s", env_path, new_val)
        return True
    except Exception as e:
        log.error("_write_env_universe(%s): %s", env_path, e)
        return False

def _query_coin_stats(db_path: str, min_trades: int) -> Dict[str, Dict]:
    """
    Baca performa per coin dari database (trades + signal_scores).
    Return dict: { "BNB/USDT": { win_rate, total_trades, avg_pnl, avg_score, profile } }
    """
    stats: Dict[str, Dict] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Stats dari trades
        cur.execute("""
            SELECT
                symbol,
                strategy_profile,
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                AVG(realized_pnl_pct) as avg_pnl_pct,
                AVG(executed_price) as avg_price
            FROM trades
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY symbol
            HAVING total_trades >= ?
        """, (min_trades,))
        rows = cur.fetchall()

        for row in rows:
            sym = row["symbol"]
            total = row["total_trades"] or 0
            wins  = row["wins"] or 0
            stats[sym] = {
                "symbol":       sym,
                "profile":      row["strategy_profile"] or "",
                "total_trades": total,
                "win_rate":     (wins / total * 100) if total > 0 else 0.0,
                "avg_pnl_pct":  float(row["avg_pnl_pct"] or 0.0),
                "avg_score":    0.0,
                "source":       "trades",
            }

        # Tambah avg_score dari signal_scores
        cur.execute("""
            SELECT
                symbol,
                strategy_profile,
                AVG(total_score) as avg_score,
                COUNT(*) as score_count
            FROM signal_scores
            WHERE timestamp >= datetime('now', '-30 days')
              AND trigger_met = 1
            GROUP BY symbol
        """)
        score_rows = cur.fetchall()
        for row in score_rows:
            sym = row["symbol"]
            if sym in stats:
                stats[sym]["avg_score"] = float(row["avg_score"] or 0.0)
                if not stats[sym]["profile"] and row["strategy_profile"]:
                    stats[sym]["profile"] = row["strategy_profile"]

        conn.close()
    except Exception as e:
        log.error("_query_coin_stats(%s): %s", db_path, e)

    return stats

def _score_coin(stat: Dict) -> float:
    """
    Hitung skor gabungan untuk ranking coin.
    Kombinasi win_rate (bobot 50%), avg_pnl_pct (30%), avg_score (20%).
    """
    wr    = stat.get("win_rate", 0.0)
    pnl   = stat.get("avg_pnl_pct", 0.0)
    score = stat.get("avg_score", 50.0)

    # Normalisasi masing-masing ke 0-100
    wr_norm    = min(100.0, max(0.0, wr))
    pnl_norm   = min(100.0, max(0.0, (pnl + 5.0) * 10))  # -5% → 0, +5% → 100
    score_norm = min(100.0, max(0.0, score))

    return (wr_norm * 0.50) + (pnl_norm * 0.30) + (score_norm * 0.20)


# ── Main Class ────────────────────────────────────────────────────────────────

class CoinSwapEngine:
    """
    Engine yang secara periodik membandingkan performa coin di algotrader
    dan algotrader_test, lalu swap otomatis jika ditemukan kandidat yang
    lebih baik.
    """

    def __init__(self, config: Dict, notifier=None):
        # [CATATAN] Param `config` sengaja tidak dipakai untuk override nilai
        # di bawah — semua setting CoinSwap diambil langsung dari env var,
        # konsisten dengan cara main.py & telegram_bot.py membaca
        # UNIVERSE_WATCHLIST/watchlist lain (selalu via os.getenv, bukan
        # dict config). Disimpan sebagai referensi/debug saja.
        self._config         = config
        self._enabled       = os.getenv("COIN_SWAP_ENABLED", "false").lower() == "true"
        self._peer_db       = os.getenv("CROSS_LEARN_DB", "")
        self._peer_env      = os.getenv("PEER_BOT_ENV", "/root/algotrader_test/.env")
        self._self_env      = os.getenv("BOT_ENV_PATH", "/root/algotrader/.env")
        self._interval_h    = int(os.getenv("COIN_SWAP_INTERVAL_H", "24"))
        self._min_trades    = int(os.getenv("COIN_SWAP_MIN_TRADES", "5"))
        self._min_win_rate  = float(os.getenv("COIN_SWAP_MIN_WIN_RATE", "60.0"))
        self._max_per_cycle = int(os.getenv("COIN_SWAP_MAX_PER_CYCLE", "2"))
        self._notifier      = notifier
        self._last_run:     Optional[datetime] = None
        self._swap_history: List[Dict] = []

        # Fallback: cari self .env dari lokasi standar
        if not os.path.exists(self._self_env):
            self._self_env = "/root/algotrader/.env"

        log.info(
            "CoinSwapEngine init: enabled=%s interval=%dh min_trades=%d "
            "min_wr=%.1f%% max_per_cycle=%d",
            self._enabled, self._interval_h, self._min_trades,
            self._min_win_rate, self._max_per_cycle,
        )

    def should_run(self) -> bool:
        if not self._enabled:
            return False
        if not self._peer_db or not os.path.exists(self._peer_db):
            log.debug("CoinSwap: peer DB tidak ditemukan: %s", self._peer_db)
            return False
        if self._last_run is None:
            return True
        elapsed_h = (_utcnow() - self._last_run).total_seconds() / 3600
        return elapsed_h >= self._interval_h

    async def run_cycle(self, bot_instance=None) -> List[Dict]:
        """
        Jalankan satu siklus swap:
        1. Baca performa coin di algotrader_test
        2. Baca performa coin di algotrader (dari DB sendiri)
        3. Temukan kandidat swap
        4. Eksekusi swap di .env kedua bot
        5. Kirim notifikasi
        """
        if not self.should_run():
            return []

        # --- Cek pintu algotrader ---
        # Kalau sedang hold posisi, swap ditahan agar tidak ganti koin
        # di saat algotrader sedang dalam posisi aktif.
        # _last_run tidak diupdate supaya siklus berikutnya tetap bisa jalan.
        if bot_instance is not None:
            try:
                open_positions = await bot_instance.db.get_open_positions()
                max_open = int(bot_instance.config.get("max_open_positions", 1))
                if len(open_positions) >= max_open:
                    log.info(
                        "CoinSwap: ditahan — algotrader sedang hold %d/%d posisi. "
                        "Swap akan dicoba di siklus berikutnya.",
                        len(open_positions), max_open,
                    )
                    if bot_instance.notifier:
                        try:
                            await bot_instance.notifier.notify_info(
                                "⏸ *CoinSwap Ditahan*\n"
                                f"algotrader sedang hold {len(open_positions)}/{max_open} posisi.\n"
                                "Swap akan dijalankan setelah posisi ditutup."
                            )
                        except Exception:
                            pass
                    return []
            except Exception as e:
                log.warning("CoinSwap: gagal cek posisi algotrader: %s", e)

        self._last_run = _utcnow()
        log.info("CoinSwap: memulai siklus evaluasi...")

        # Baca watchlist saat ini
        self_env  = self._self_env
        peer_env  = self._peer_env
        self_wl   = _read_env_universe(self_env)
        peer_wl   = _read_env_universe(peer_env)

        if not self_wl or not peer_wl:
            log.warning("CoinSwap: universe kosong, skip.")
            return []

        # Baca stats dari DB algotrader (self) — gunakan DB path dari env
        self_db = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./data/trading_bot.db"
        ).replace("sqlite+aiosqlite:///", "").replace("./", "/root/algotrader/")
        if not self_db.startswith("/"):
            self_db = f"/root/algotrader/{self_db.lstrip('./')}"

        self_stats = _query_coin_stats(self_db, self._min_trades)
        peer_stats = _query_coin_stats(self._peer_db, self._min_trades)

        log.info(
            "CoinSwap: self_stats=%d coin, peer_stats=%d coin",
            len(self_stats), len(peer_stats),
        )

        # Temukan kandidat dari algotrader_test yang layak masuk algotrader
        candidates_in  = []  # dari test → production
        candidates_out = []  # dari production → test (yang lemah)

        for sym, stat in peer_stats.items():
            if sym in self_wl:
                continue  # sudah ada di algotrader
            if stat["win_rate"] < self._min_win_rate:
                continue
            if stat["total_trades"] < self._min_trades:
                continue
            combined = _score_coin(stat)
            candidates_in.append((sym, combined, stat))

        # Urutkan dari yang paling bagus
        candidates_in.sort(key=lambda x: x[1], reverse=True)
        candidates_in = candidates_in[:self._max_per_cycle]

        if not candidates_in:
            log.info("CoinSwap: tidak ada kandidat masuk yang memenuhi syarat.")
            return []

        # Temukan coin algotrader yang paling lemah untuk diswap keluar
        for sym in self_wl:
            stat = self_stats.get(sym)
            if stat is None:
                # Belum ada trade — anggap lemah
                candidates_out.append((sym, 0.0, {}))
            else:
                combined = _score_coin(stat)
                candidates_out.append((sym, combined, stat))

        # Urutkan dari yang paling lemah
        candidates_out.sort(key=lambda x: x[1])
        candidates_out = candidates_out[:len(candidates_in)]

        if not candidates_out:
            log.info("CoinSwap: tidak ada kandidat keluar.")
            return []

        # Eksekusi swap
        swaps_done = []
        new_self_wl = list(self_wl)
        new_peer_wl = list(peer_wl)

        for i, (in_sym, in_score, in_stat) in enumerate(candidates_in):
            if i >= len(candidates_out):
                break

            out_sym, out_score, out_stat = candidates_out[i]

            # Swap di watchlist
            if out_sym in new_self_wl:
                new_self_wl.remove(out_sym)
            if in_sym not in new_self_wl:
                new_self_wl.append(in_sym)

            if in_sym in new_peer_wl:
                new_peer_wl.remove(in_sym)
            if out_sym not in new_peer_wl:
                new_peer_wl.append(out_sym)

            swap_record = {
                "timestamp":    _utcnow().isoformat(),
                "coin_in":      in_sym,
                "coin_in_wr":   round(in_stat.get("win_rate", 0.0), 1),
                "coin_in_score": round(in_score, 1),
                "coin_out":     out_sym,
                "coin_out_wr":  round(out_stat.get("win_rate", 0.0) if out_stat else 0.0, 1),
                "coin_out_score": round(out_score, 1),
            }
            swaps_done.append(swap_record)
            self._swap_history.append(swap_record)

            log.info(
                "CoinSwap: %s (score=%.1f, wr=%.1f%%) → algotrader | "
                "%s (score=%.1f) → algotrader_test",
                in_sym, in_score, in_stat.get("win_rate", 0.0),
                out_sym, out_score,
            )

        if not swaps_done:
            return []

        # Tulis .env baru
        ok_self = _write_env_universe(self_env, new_self_wl)
        ok_peer = _write_env_universe(peer_env, new_peer_wl)

        if not (ok_self and ok_peer):
            log.error("CoinSwap: gagal update .env, swap dibatalkan.")
            return []

        log.info(
            "CoinSwap: %d swap selesai | algotrader=%s | algotrader_test=%s",
            len(swaps_done), new_self_wl, new_peer_wl,
        )

        # Kirim notifikasi Telegram
        if self._notifier:
            try:
                msg = self._format_swap_notification(swaps_done, new_self_wl, new_peer_wl)
                await self._notifier.notify_info(msg)
            except Exception as e:
                log.warning("CoinSwap: gagal kirim notifikasi: %s", e)

        # Restart kedua bot agar baca watchlist baru
        await self._restart_bots()

        return swaps_done

    async def _restart_bots(self):
        """
        Inject universe_watchlist terbaru ke universe_overrides DB masing-masing bot.
        Kedua bot baca DB tiap cycle — tidak perlu restart sama sekali.
        """
        import importlib.util as _ilu
        import os

        jobs = [
            ("algotrader",      self._self_env),
            ("algotrader_test", self._peer_env),
        ]

        for bot_name, env_path in jobs:
            if not os.path.exists(env_path):
                log.debug("CoinSwap _restart_bots: skip %s (env tidak ada)", bot_name)
                continue
            try:
                env_data = {}
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            env_data[k.strip()] = v.strip()

                base_wl = [
                    s.strip()
                    for s in env_data.get("UNIVERSE_WATCHLIST", "").split(",")
                    if s.strip()
                ]
                db_url  = env_data.get("DATABASE_URL", "")
                if not db_url:
                    continue
                # [BUG-FIX -- ditemukan lewat verifikasi independen] Sebelumnya
                # pola construction db_path di sini BEDA & kurang robust dari
                # pola yang sudah dipakai run_cycle()/_triggered_swap() di file
                # yang sama: `.replace("sqlite+aiosqlite:///./", f"/root/{bot_name}/")
                # .replace("sqlite+aiosqlite:///", "")` -- kalau DATABASE_URL
                # TIDAK punya prefix "./" (mis. "sqlite+aiosqlite:///data/x.db",
                # format valid SQLAlchemy lain), replace pertama tidak match,
                # lalu replace kedua cuma membuang skema-nya saja, menyisakan
                # PATH RELATIF ("data/x.db") -- bukan absolute path. Dibuktikan
                # via eksperimen: 2 pola construction menghasilkan path BERBEDA
                # utk URL yang sama. DatabaseManager yang dikonstruksi dgn path
                # relatif ini bisa diam-diam membuat/akses file DB yang SALAH
                # (relatif thd cwd proses saat itu, bukan direktori bot), tanpa
                # error -- silent data-integrity risk utk universe_overrides.
                # Fix: pakai fallback robust yang sama (cek startswith("/"),
                # kalau bukan absolute, prefix dgn /root/{bot_name}/).
                db_path = (
                    db_url.replace("sqlite+aiosqlite:///", "")
                    .replace("./", "")
                )
                if not db_path.startswith("/"):
                    db_path = f"/root/{bot_name}/{db_path.lstrip('/')}"

                bot_dir = os.path.dirname(env_path)
                spec    = _ilu.spec_from_file_location(
                    f"_db_{bot_name}", os.path.join(bot_dir, "database.py")
                )
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                db  = mod.DatabaseManager(f"sqlite+aiosqlite:///{db_path}")
                await db.init_db()

                for sym in base_wl:
                    await db.upsert_universe_override(
                        symbol=sym,
                        source="coin_swap",
                        notes=f"CoinSwap sync {bot_name}",
                    )
                active = await db.get_active_universe_overrides()
                for sym in active:
                    if sym not in base_wl:
                        await db.deactivate_universe_override(sym)

                await db.close()
                log.info(
                    "CoinSwap: universe_overrides %s diperbarui (%d koin) tanpa restart.",
                    bot_name, len(base_wl),
                )
            except Exception as e:
                log.error("CoinSwap: gagal update universe_overrides %s: %s", bot_name, e)

    def _format_swap_notification(
        self,
        swaps:       List[Dict],
        new_self_wl: List[str],
        new_peer_wl: List[str],
    ) -> str:
        lines = [
            "🔄 COIN SWAP OTOMATIS",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for s in swaps:
            lines.append(
                f"✅ {s['coin_in']} (WR={s['coin_in_wr']}%) → algotrader (REAL)"
            )
            lines.append(
                f"↩️ {s['coin_out']} (WR={s['coin_out_wr']}%) → algotrader_test"
            )
        lines.append("")
        lines.append(f"📊 algotrader: {', '.join(new_self_wl)}")
        lines.append(f"🧪 algotrader_test: {', '.join(new_peer_wl)}")
        lines.append("")
        lines.append("🔁 Kedua bot sedang direstart...")
        return "\n".join(lines)

    def get_swap_history(self) -> List[Dict]:
        return list(reversed(self._swap_history[-20:]))

    async def check_triggered_swap(
        self,
        symbol: str,
        total_score: float,
        bot_instance=None,
    ) -> bool:
        """
        Dipanggil saat algotrader_test mendeteksi sinyal kuat.
        Cek apakah score melewati threshold algotrader — kalau ya, trigger swap permanen.
        Return True kalau swap berhasil dilakukan.
        """
        if not self._enabled:
            return False

        # algo_thresholds tidak diperlukan — get_dynamic_threshold sudah handle

        # Cek profile koin ini
        try:
            from profiles.registry import get_coin_profile
            prof = get_coin_profile(symbol)
            profile_name = prof.profile.value
        except Exception as e:
            log.warning("TriggeredSwap: gagal get profile %s: %s", symbol, e)
            return False

        # Ambil dynamic threshold berdasarkan profil
        try:
            from profiles.thresholds import get_dynamic_threshold
            algo_threshold = get_dynamic_threshold(profile_name, "undefined")
        except Exception:
            algo_threshold = 70.0

        # Cek apakah score melewati threshold algotrader
        if total_score < algo_threshold:
            log.debug(
                "TriggeredSwap: %s score=%.1f < algo_threshold=%.1f — skip.",
                symbol, total_score, algo_threshold,
            )
            return False

        log.info(
            "TriggeredSwap: %s score=%.1f >= algo_threshold=%.1f — trigger swap!",
            symbol, total_score, algo_threshold,
        )

        return await self._triggered_swap(symbol, total_score, bot_instance)

    async def _triggered_swap(
        self,
        symbol: str,
        total_score: float,
        bot_instance=None,
    ) -> bool:
        """
        Eksekusi swap permanen:
        - symbol masuk ke algotrader (disesuaikan profil algotrader)
        - koin terlemah algotrader keluar → masuk algotrader_test
        """
        self_wl = _read_env_universe(self._self_env)
        peer_wl = _read_env_universe(self._peer_env)

        if not self_wl or not peer_wl:
            log.warning("TriggeredSwap: universe kosong, abort.")
            return False

        # Cek: koin sudah ada di algotrader?
        if symbol in self_wl:
            log.info("TriggeredSwap: %s sudah ada di algotrader — skip.", symbol)
            return False

        # Cek: algotrader sedang open posisi di koin manapun?
        open_symbols = set()
        if bot_instance:
            try:
                open_pos = await bot_instance.db.get_open_positions()
                open_symbols = {p.symbol for p in open_pos}
            except Exception as e:
                log.warning("TriggeredSwap: gagal cek open positions: %s", e)

        # Cari koin terlemah algotrader yang tidak sedang open posisi
        self_db = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./data/trading_bot.db"
        ).replace("sqlite+aiosqlite:///", "").replace("./", "/root/algotrader/")
        if not self_db.startswith("/"):
            self_db = f"/root/algotrader/{self_db.lstrip('./')}"

        self_stats = _query_coin_stats(self_db, 0)

        candidates_out = []
        for sym in self_wl:
            if sym in open_symbols:
                continue  # skip yang sedang open posisi
            stat = self_stats.get(sym, {})
            score = _score_coin(stat) if stat else 0.0
            candidates_out.append((sym, score))

        if not candidates_out:
            log.warning("TriggeredSwap: tidak ada kandidat keluar yang aman.")
            return False

        # Pilih yang terlemah
        candidates_out.sort(key=lambda x: x[1])
        out_sym, out_score = candidates_out[0]

        # Eksekusi swap di .env
        new_self_wl = list(self_wl)
        new_peer_wl = list(peer_wl)

        new_self_wl.remove(out_sym)
        new_self_wl.append(symbol)

        if symbol in new_peer_wl:
            new_peer_wl.remove(symbol)
        if out_sym not in new_peer_wl:
            new_peer_wl.append(out_sym)

        ok_self = _write_env_universe(self._self_env, new_self_wl)
        ok_peer = _write_env_universe(self._peer_env, new_peer_wl)

        if not (ok_self and ok_peer):
            log.error("TriggeredSwap: gagal update .env, abort.")
            return False

        # Hot-reload via universe_overrides DB kedua bot
        await self._restart_bots()

        # Catat di history
        swap_record = {
            "timestamp":      _utcnow().isoformat(),
            "type":           "triggered",
            "coin_in":        symbol,
            "coin_in_score":  round(total_score, 1),
            "coin_out":       out_sym,
            "coin_out_score": round(out_score, 1),
        }
        self._swap_history.append(swap_record)

        log.info(
            "TriggeredSwap SELESAI: %s (score=%.1f) → algotrader | "
            "%s (score=%.1f) → algotrader_test",
            symbol, total_score, out_sym, out_score,
        )

        # Notifikasi Telegram
        if self._notifier:
            try:
                msg = (
                    "⚡ *TRIGGERED SWAP*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔥 *{symbol}* (score={total_score:.1f}) → algotrader (REAL)\n"
                    f"↩️ *{out_sym}* (score={out_score:.1f}) → algotrader_test\n\n"
                    f"📊 algotrader: {', '.join(new_self_wl)}\n"
                    f"🧪 algotrader_test: {', '.join(new_peer_wl)}"
                )
                await self._notifier.notify_info(msg)
            except Exception as e:
                log.warning("TriggeredSwap: gagal kirim notifikasi: %s", e)

        return True
