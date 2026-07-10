"""
telegram_bot.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import json
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp
from dotenv import load_dotenv

_bot_src = os.path.dirname(os.path.abspath(__file__))
if _bot_src not in sys.path:
    sys.path.insert(0, _bot_src)

_env_path = os.path.join(_bot_src, ".env")
load_dotenv(_env_path)

try:
    import ta_compat
except ImportError:
    pass

from constants import APP_VERSION
from profiles.base_profile import PROFILE_EMOJI, PROFILE_TIMEFRAME
from profiles.registry import get_coin_profile, get_profile_summary

log = logging.getLogger("telegram_ctrl")

TOKEN             = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID           = os.getenv("TELEGRAM_CHAT_ID", "")
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")
API_URL           = "https://api.telegram.org/bot" + TOKEN
BOT_DIR           = os.getenv("BOT_DIR", "/root/algotrader")
START_SCRIPT      = os.path.join(BOT_DIR, "start.sh")
STOP_SCRIPT       = os.path.join(BOT_DIR, "stop.sh")
OFFSET_FILE       = os.path.join(BOT_DIR, ".tg_offset")
BOT_API           = os.getenv("BOT_API", "http://localhost:8000/api")
BOT_API_URL       = BOT_API   # alias untuk fungsi-fungsi baru
BOT_API_KEY       = DASHBOARD_API_KEY  # alias untuk fungsi-fungsi baru
_last_msg: dict    = {}  # menyimpan msg object terakhir dari handle_update

_API_RETRY_COUNT   = 1
_API_RETRY_BACKOFF = 2.0
_TG_MAX_LEN        = 3800

_REGIME_EMOJI = {
    "trending_bull":      "🟢",
    "trending_bear":      "🔴",
    "ranging":            "🟡",
    "volatile_expansion": "🟠",
    "undefined":          "⚪",
}

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _pct(v) -> str:
    if v is None:
        return "—"
    return ("+" if float(v) >= 0 else "") + f"{float(v):.2f}%"

def _usd(v) -> str:
    if v is None:
        return "—"
    val = float(v)
    if val == 0:
        return "$0.00"
    if val >= 1000:
        return f"${val:,.2f}"
    if val >= 1:
        return f"${val:.4f}"
    if val >= 0.01:
        return f"${val:.5f}"
    if val >= 0.0001:
        return f"${val:.6f}"
    return f"${val:.8f}"

def _sign(v) -> str:
    if v is None:
        return "—"
    val = float(v)
    return f"+${val:,.4f}" if val >= 0 else f"-${abs(val):,.4f}"

def _score_bar(score: float, width: int = 8) -> str:
    filled = int(round(score / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)

def _profile_from_signal_origin(origin: str) -> str:
    if not origin:
        return ""
    try:
        if "Profile(" in origin:
            start = origin.index("Profile(") + len("Profile(")
            end   = origin.index(")", start)
            return origin[start:end]
    except (ValueError, IndexError):
        log.debug("_profile_from_signal_origin: tidak bisa parse '%s...'", origin[:60])
    return ""

def _profile_label(prof_name: str) -> str:
    if not prof_name:
        return "⚙️ VolumetricBreakout"
    emoji = PROFILE_EMOJI.get(prof_name, "⚙️")
    tf    = PROFILE_TIMEFRAME.get(prof_name, "")
    if prof_name in PROFILE_EMOJI:
        return f"{emoji} {prof_name} ({tf})" if tf else f"{emoji} {prof_name}"
    return f"⚙️ {prof_name}"

def _load_offset() -> int:
    try:
        if os.path.exists(OFFSET_FILE):
            with open(OFFSET_FILE, "r") as f:
                return int(json.load(f).get("offset", 0))
    except Exception:
        pass
    return 0

def _save_offset(offset: int) -> None:
    try:
        with open(OFFSET_FILE, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception as e:
        log.debug("Gagal simpan offset: %s", e)

async def tg_send(
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict | None = None,
) -> Optional[int]:
    url     = API_URL + "/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.error("Telegram error %d: %s", r.status, body[:200])
                    return None
                data = await r.json()
                return data.get("result", {}).get("message_id")
    except Exception as e:
        log.error("tg_send error: %s", e)
        return None

async def tg_delete(message_id: int) -> None:
    if not message_id:
        return
    url     = API_URL + "/deleteMessage"
    payload = {"chat_id": CHAT_ID, "message_id": message_id}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.debug("tg_delete error %d: %s", r.status, body[:100])
    except Exception as e:
        log.debug("tg_delete error: %s", e)

async def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    if not callback_query_id:
        return
    url = API_URL + "/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.debug("answerCallbackQuery error %d: %s", r.status, body[:200])
    except Exception as e:
        log.debug("answerCallbackQuery exception: %s", e)

def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": d} for (t, d) in row]
            for row in rows
        ]
    }

async def cmd_menu() -> None:
    await tg_send(
        "🧭 *Menu AlgoTrader*\nPilih aksi:",
        reply_markup=_kb([
            [("📊 Status", "status"), ("💰 Balance", "balance")],
            [("📌 Positions", "positions"), ("🧾 Trades", "history")],
            [("🧠 Scores", "scores"), ("🗺️ Regime", "regime")],
            [("📈 Attribution", "attribution"), ("🧩 Suggestions", "suggestions")],
            [("⏸ Pause", "pause"), ("▶️ Resume", "resume")],
            [("🆘 PANIC (confirm)", "panic_prompt")],
        ]),
    )

async def tg_send_long(parts: List[str], header: str = "") -> None:
    chunks: List[List[str]] = []
    cur:    List[str]       = []
    cur_len = len(header)

    for part in parts:
        part_len = len(part) + 1
        if cur_len + part_len > _TG_MAX_LEN and cur:
            chunks.append(cur.copy())
            cur     = []
            cur_len = 0
        cur.append(part)
        cur_len += part_len

    if cur:
        chunks.append(cur)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = header if i == 1 else f"_(lanjutan {i}/{total})_"
        text   = (prefix + "\n" + "\n".join(chunk)).strip()
        await tg_send(text)
        if i < total:
            await asyncio.sleep(0.5)

async def api_get(path: str):
    headers = {"X-API-Key": DASHBOARD_API_KEY} if DASHBOARD_API_KEY else {}
    for attempt in range(_API_RETRY_COUNT + 1):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    BOT_API + path,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    log.warning("api_get %s → HTTP %d (attempt %d)", path, r.status, attempt + 1)
        except Exception as e:
            log.warning("api_get %s gagal (attempt %d): %s", path, attempt + 1, e)
        if attempt < _API_RETRY_COUNT:
            await asyncio.sleep(_API_RETRY_BACKOFF)
    return None

async def api_post(path: str, body: dict | None = None):
    headers = {"X-API-Key": DASHBOARD_API_KEY} if DASHBOARD_API_KEY else {}
    for attempt in range(_API_RETRY_COUNT + 1):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    BOT_API + path,
                    headers=headers,
                    json=body or {},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status == 200:
                        return await r.json()
                    log.warning("api_post %s → HTTP %d", path, r.status)
        except Exception as e:
            log.warning("api_post %s gagal: %s", path, e)
        if attempt < _API_RETRY_COUNT:
            await asyncio.sleep(_API_RETRY_BACKOFF)
    return None

async def cmd_start() -> None:
    lines = [
        f"🤖 *AlgoTrader Pro v{APP_VERSION} — Telegram Control*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "Selamat datang! Bot trading kamu aktif.",
        f"Engine: Intelligence Pipeline v{APP_VERSION}",
        "",
        "Ketik /help untuk daftar lengkap command.",
        "",
        f"🕐 `{_utcnow()} UTC`",
    ]
    await tg_send("\n".join(lines))

async def cmd_help() -> None:
    lines = [
        "📋 *DAFTAR COMMAND*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 *Monitor:*",
        "  /status    — Status bot lengkap",
        "  /balance   — Equity & balance",
        "  /positions — Posisi terbuka",
        "  /metrics   — Statistik trading",
        f"  /diagnosa  — Analisis sinyal coin (v{APP_VERSION})",
        f"  /strategy  — Profil per coin (v{APP_VERSION})",
        "  /history   — Riwayat trade",
        "  /log       — Log terakhir",
        "",
        "🧠 *Intelligence (v7.0):*",
        "  /scores        — Score board semua coin",
        "  /score SYMBOL  — Detail skor satu coin",
        "  /regime        — Market regime semua coin",
        "  /attribution   — Insight performa 7 hari",
        "  /suggestions   — Saran meta-learner pending",
        "  /crosslearn    — Status cross-learning",
        "  /swaphistory   — Riwayat coin swap",
        "  /setconfig key val — Update config tanpa restart",
        "  /getconfig       — Lihat config bot saat ini",
        "  /forecast        — Prediksi & analisis koin untuk trader manual",
        "  /universe        — Lihat daftar universe koin",
        "  /addcoin SYMBOL  — Tambah koin ke universe",
        "  /removecoin SYM  — Hapus koin dari universe",
        "",
        "⚙️ *Kontrol:*",
        "  /run       — Jalankan bot",
        "  /stop      — Hentikan bot",
        "  /pause     — Pause strategi",
        "  /resume    — Resume strategi",
        "",
        "🆘 *Emergency:*",
        "  /panic     — Close SEMUA posisi!",
        "",
        f"🕐 `{_utcnow()} UTC`",
    ]
    await tg_send("\n".join(lines))

async def cmd_status() -> None:
    data = await api_get("/status")
    bal  = await api_get("/balance")

    if not data:
        await tg_send("❌ Bot API tidak merespons. Bot mungkin mati.")
        return

    halted = data.get("halted", False)
    status = "🔴 HALTED" if halted else "🟢 RUNNING"
    mode   = "🟡 TESTNET" if data.get("testnet") else "🔴 LIVE"
    conn   = "✅" if data.get("connected") else "❌"
    strat  = "✅ AKTIF" if data.get("strategy_active") else "⏸ PAUSED"
    uptime = data.get("uptime_display", "—")
    symbols = ", ".join(data.get("universe_watchlist", []))
    equity = _usd(bal.get("total_equity")) if bal else "—"
    free   = _usd(bal.get("free_balance")) if bal else "—"
    dpnl   = _sign(bal.get("daily_pnl"))   if bal else "—"
    dd     = f"{bal.get('drawdown_pct', 0):.2f}%" if bal else "—"

    lines = [
        "📊 *STATUS BOT*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🤖 Status    : `{status}`",
        f"🌐 Mode      : `{mode}`",
        f"🔌 Exchange  : `{conn} {data.get('exchange', '').upper()}`",
        f"📈 Strategi  : `{strat}`",
        f"⏱ Uptime    : `{uptime}`",
        "",
        "💰 *PORTFOLIO:*",
        f"  Equity     : `{equity}`",
        f"  Free       : `{free}`",
        f"  Daily PnL  : `{dpnl}`",
        f"  Drawdown   : `{dd}`",
        "",
        f"👀 Watchlist : `{symbols}`",
        f"⚙️ Engine    : `Intelligence Pipeline v{APP_VERSION}`",
    ]

    regime_data = await api_get("/intelligence/regime")
    if regime_data and regime_data.get("regimes"):
        regimes = regime_data["regimes"]  # list[dict]
        from collections import Counter
        regime_count: Counter = Counter()
        for r in regimes:
            regime_name = r.get("regime", "undefined")
            regime_count[regime_name] += 1

        regime_parts = []
        for reg, cnt in sorted(regime_count.items(), key=lambda x: -x[1]):
            emoji = _REGIME_EMOJI.get(reg, "⚪")
            regime_parts.append(f"{emoji} {reg.replace('_', ' ').title()}: {cnt}")

        if regime_parts:
            lines += ["", "🌐 *Regime saat ini:*", "  " + " | ".join(regime_parts)]

    # Tambah info RAM/CPU VPS
    try:
        import psutil
        cpu_pct   = psutil.cpu_percent(interval=0.5)
        ram       = psutil.virtual_memory()
        swap      = psutil.swap_memory()
        disk      = psutil.disk_usage('/')
        ram_emoji = "🟢" if ram.percent < 70 else "🟡" if ram.percent < 85 else "🔴"
        cpu_emoji = "🟢" if cpu_pct < 50 else "🟡" if cpu_pct < 80 else "🔴"
        dsk_emoji = "🟢" if disk.percent < 70 else "🟡" if disk.percent < 85 else "🔴"
        lines += [
            "",
            "🖥 *SERVER VPS:*",
            f"  {cpu_emoji} CPU   : `{cpu_pct:.1f}%`",
            f"  {ram_emoji} RAM   : `{ram.used/1024**3:.1f}GB / {ram.total/1024**3:.1f}GB ({ram.percent:.0f}%)`",
            f"  💾 Swap  : `{swap.used/1024**3:.2f}GB`",
            f"  {dsk_emoji} Disk  : `{disk.percent:.0f}% terpakai`",
        ]
    except Exception as _e:
        lines += ["", f"🖥 Server: tidak tersedia"]

    lines += ["", f"🕐 `{_utcnow()} UTC`"]
    if halted:
        lines += ["", f"⚠️ *Halt reason:* `{data.get('halt_reason', '—')}`"]

    await tg_send("\n".join(lines))

async def cmd_balance() -> None:
    bal = await api_get("/balance")
    if not bal:
        await tg_send("❌ Tidak bisa ambil data balance.")
        return

    lines = [
        "💰 *BALANCE & EQUITY*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Total Equity  : `{_usd(bal.get('total_equity'))}`",
        f"  Free Balance  : `{_usd(bal.get('free_balance'))}`",
        f"  Locked        : `{_usd(bal.get('locked_balance'))}`",
        f"  Open PnL      : `{_sign(bal.get('open_pnl'))}`",
        f"  Daily PnL     : `{_sign(bal.get('daily_pnl'))}` "
        f"(`{_pct(bal.get('daily_pnl_pct'))}`)",
        f"  Drawdown      : `{bal.get('drawdown_pct', 0):.2f}%`",
        f"  Currency      : `{bal.get('currency', 'USDT')}`",
        "",
        f"🕐 `{_utcnow()} UTC`",
    ]
    await tg_send("\n".join(lines))

async def cmd_positions() -> None:
    data = await api_get("/positions")
    if not data:
        await tg_send("❌ Tidak bisa ambil data posisi.")
        return

    if data.get("count", 0) == 0:
        await tg_send("📭 *Tidak ada posisi terbuka saat ini.*")
        return

    header = [
        f"📌 *POSISI TERBUKA ({data['count']})*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    parts = []
    for p in data["positions"]:
        pnl     = p.get("unrealized_pnl", 0)
        pnl_pct = p.get("unrealized_pnl_pct", 0)
        emoji   = "📈" if pnl >= 0 else "📉"
        sl_ok   = p.get("stop_loss_price") and p.get("entry_price")
        be      = "✅ BE" if (sl_ok and p["stop_loss_price"] >= p["entry_price"]) else ""
        closing_tag = " *(closing)*" if p.get("is_closing") else ""

        # Positions API doesn't include signal_origin; fall back to strategy name.
        prof_lbl = _profile_label(p.get("profile") or p.get("strategy", "VolumetricBreakout"))

        parts.append("\n".join([
            f"{emoji} *{p['symbol']}* `{p['side'].upper()}` {be}{closing_tag}",
            f"  Profile  : `{prof_lbl}`",
            f"  Entry    : `{_usd(p.get('entry_price'))}`",
            f"  Current  : `{_usd(p.get('current_price'))}`",
            f"  Amount   : `{p.get('amount', 0):.6f}`",
            f"  PnL      : `{_sign(pnl)}` (`{_pct(pnl_pct)}`)",
            f"  SL       : `{_usd(p.get('stop_loss_price'))}`",
            f"  TP       : `{_usd(p.get('take_profit_price'))}`",
            f"  Durasi   : `{p.get('duration_display', '—')}`",
        ]))

    parts.append(f"\n🕐 `{_utcnow()} UTC`")
    await tg_send_long(parts, header="\n".join(header))

async def cmd_metrics() -> None:
    m = await api_get("/metrics")
    if not m:
        await tg_send("❌ Tidak bisa ambil metrics.")
        return

    pf     = m.get("profit_factor", 0)
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"

    lines = [
        "📊 *STATISTIK TRADING*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Total Trade   : `{m.get('total_trades', 0)}`",
        f"  Win Rate      : `{m.get('win_rate_pct', 0):.1f}%`",
        f"  Total PnL     : `{_sign(m.get('total_pnl'))}`",
        f"  Avg PnL/Trade : `{_sign(m.get('avg_pnl_per_trade'))}`",
        f"  Profit Factor : `{pf_str}`",
        f"  Expectancy    : `{_sign(m.get('expectancy'))}`",
        f"  Sharpe        : `{m.get('sharpe_ratio', 0):.4f}`",
        f"  Sortino       : `{m.get('sortino_ratio', 0):.4f}`",
        f"  Max Drawdown  : `{m.get('max_drawdown_pct', 0):.2f}%`",
        f"  Cur Drawdown  : `{m.get('current_drawdown_pct', 0):.2f}%`",
        f"  Daily Loss    : `{m.get('daily_loss_pct', 0):.2f}%`",
        f"  Total Fees    : `{_usd(m.get('total_fees'))}`",
        f"  Open Pos      : `{m.get('open_positions', 0)}`",
        "",
        f"🕐 `{_utcnow()} UTC`",
    ]
    await tg_send("\n".join(lines))

async def cmd_diagnosa() -> None:
    await tg_send(
        f"🔍 *Menganalisis semua coin (v{APP_VERSION})... tunggu sebentar*"
    )

    data = await api_get("/diagnosa")
    if data and data.get("results"):
        await _render_diagnosa_from_api(data)
        return

    await tg_send(
        "⚠️ _Endpoint /api/diagnosa belum tersedia — fallback ke analisis langsung._\n"
        "_Ini membuat koneksi exchange baru._"
    )
    await _diagnosa_direct()

async def _render_diagnosa_from_api(data: dict) -> None:
    is_testnet  = data.get("testnet", True)
    mode_label  = "🟡 TESTNET" if is_testnet else "🔴 LIVE"
    results     = data.get("results", [])
    universe_n = data.get("universe_count", len(results))

    header_lines = [
        f"🔍 *DIAGNOSA SINYAL v{APP_VERSION} — {universe_n} COIN* [{mode_label}]",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if is_testnet:
        header_lines.append(
            "_⚠️ Testnet: volume & data mungkin tidak mencerminkan pasar real_"
        )

    parts: List[str] = []
    for item in results:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            parts.append(f"⚠️ Data diagnosa tidak dikenal: `{str(item)[:80]}`")
            continue

        symbol = item.get("symbol", "?")
        if item.get("error"):
            parts.append(f"⚠️ *{symbol}*: {item.get('error')}")
            continue

        total_score = item.get("total_score")
        trigger_met = bool(item.get("trigger_met"))
        regime = item.get("regime", "undefined")
        tf_used = item.get("tf_used", "?")
        profile = item.get("profile", "unknown")
        source = item.get("source", "?")
        threshold = item.get("threshold")
        score_txt = f"{float(total_score):.1f}" if total_score is not None else "N/A"
        threshold_txt = (
            f"{float(threshold):.1f}" if threshold is not None else "N/A"
        )
        trig = "✅ READY" if trigger_met else "⏳ WAIT"
        reg_emoji = _REGIME_EMOJI.get(regime, "⚪")
        price = item.get("price")
        price_txt = _usd(price) if price is not None else "—"
        vol_ratio = item.get("vol_ratio")
        vol_txt = f"{float(vol_ratio):.1f}x" if vol_ratio is not None else "—"
        atr_pct = item.get("atr_pct")
        atr_txt = f"{float(atr_pct):.3f}%" if atr_pct is not None else "—"

        parts.append("\n".join([
            f"🔎 *{symbol}* {trig}",
            f"  📊 Score   : `{score_txt}` / `{threshold_txt}`",
            f"  🌐 Regime  : {reg_emoji} `{regime}`",
            f"  🧬 Profile : `{profile}` | TF `{tf_used}`",
            f"  💵 Price   : `{price_txt}` | Vol `{vol_txt}` | ATR `{atr_txt}`",
            f"  🧠 Source  : `{source}`",
        ]))

    parts.append(f"\n🕐 `{_utcnow()} UTC`")
    await tg_send_long(parts, header="\n".join(header_lines))

async def _diagnosa_direct() -> None:
    try:
        from constants import COL_EMA9, COL_EMA21, COL_EMA50, COL_RSI, COL_ATR
    except ImportError:
        COL_EMA9, COL_EMA21, COL_EMA50, COL_RSI, COL_ATR = (
            "EMA_9", "EMA_21", "EMA_50", "RSI_14", "ATRr_14"
        )

    _TF_FALLBACK = {"1d": ["4h", "1h"], "4h": ["1h"], "1h": ["15m"], "15m": []}

    # [BUG-FIX] Sebelumnya `exchange` hanya di-assign DI DALAM try block
    # (setelah import ccxt & konstruksi ccxt.binance(...)). Kalau exception
    # terjadi SEBELUM assignment itu (mis. ccxt belum terinstall, atau
    # ccxt.binance(...) sendiri gagal karena kredensial/konfigurasi), variabel
    # `exchange` tidak pernah terikat — lalu blok `finally` di bawah yang
    # mengecek `if exchange is not None:` akan melempar NameError baru,
    # menutupi pesan error asli dan berpotensi membuat exception itu
    # menjalar keluar dari fungsi (tidak tertangkap oleh except di atasnya).
    # Sekarang: inisialisasi exchange=None di luar try supaya selalu terikat.
    exchange = None

    try:
        import ccxt.async_support as ccxt
        import pandas as pd

        is_testnet = os.getenv("TESTNET", "true").lower() == "true"
        exchange   = ccxt.binance({
            "apiKey": os.getenv("API_KEY"), "secret": os.getenv("API_SECRET"),
            "enableRateLimit": True,
        })
        exchange.set_sandbox_mode(is_testnet)

        # [CLEANUP] Sebelumnya os.getenv("UNIVERSE_WATCHLIST", os.getenv("UNIVERSE_WATCHLIST", ...))
        # — panggilan os.getenv bersarang dengan key yang sama persis, tidak
        # berefek apa pun selain membingungkan pembaca. Disederhanakan.
        universe = [s.strip() for s in os.getenv("UNIVERSE_WATCHLIST", "BTC/USDT,ETH/USDT").split(",")]
        results: List[str] = []

        for symbol in universe:
            try:
                prof    = get_coin_profile(symbol)
                bars    = None
                tf_used = prof.timeframe
                tf_note = ""

                for tf_try in [prof.timeframe] + _TF_FALLBACK.get(prof.timeframe, []):
                    try:
                        candidate = await exchange.fetch_ohlcv(symbol, tf_try, limit=250)
                        if candidate and len(candidate) >= 60:
                            bars    = candidate
                            tf_used = tf_try
                            if tf_try != prof.timeframe:
                                tf_note = f" ⚠️fallback:{tf_try}"
                            break
                    except Exception:
                        continue

                if not bars or len(bars) < 60:
                    note = " (testnet — data terbatas)" if is_testnet else ""
                    results.append(
                        f"⚠️ *{symbol}*: Data tidak cukup ({len(bars) if bars else 0} bar){note}"
                    )
                    continue

                cols = ["timestamp", "open", "high", "low", "close", "volume"]
                df   = pd.DataFrame(bars, columns=cols)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("timestamp", inplace=True)
                df["quote_volume"] = df["volume"] * df["close"]

                df.ta.ema(length=9,  append=True)
                df.ta.ema(length=21, append=True)
                df.ta.ema(length=50, append=True)
                df.ta.rsi(length=14, append=True)
                df.ta.atr(length=14, append=True)
                df = df.dropna()

                if len(df) < 5:
                    results.append(f"⚠️ *{symbol}*: Indikator tidak cukup ({len(df)} bar)")
                    continue

                df["_resistance"] = df["close"].shift(1).rolling(20).max()
                df["_vol_ma"]     = df["quote_volume"].rolling(20).mean()
                df = df.dropna()

                bar  = df.iloc[-2]
                prev = df.iloc[-3]

                close = float(bar["close"])
                ema9  = float(bar[COL_EMA9])
                ema21 = float(bar[COL_EMA21])
                ema50 = float(bar[COL_EMA50])
                rsi   = float(bar[COL_RSI])
                atr   = float(bar[COL_ATR])

                resist    = float(bar["_resistance"]) if pd.notna(bar.get("_resistance")) else close
                vol_ma_v  = float(bar["_vol_ma"]) if (pd.notna(bar.get("_vol_ma")) and float(bar["_vol_ma"]) > 0) else float(df["quote_volume"].mean())
                vol       = float(bar["quote_volume"]) if pd.notna(bar.get("quote_volume")) else float(bar["volume"])
                vol_ratio = vol / vol_ma_v if vol_ma_v > 0 else 0.0
                atr_pct   = (atr / close * 100) if close > 0 else 0
                vol_warn  = " ⚠️sandbox" if (is_testnet and vol_ma_v < 1.0) else ""

                prev_ema9  = float(prev[COL_EMA9])
                prev_ema21 = float(prev[COL_EMA21])

                min_dist     = close * (prof.min_breakout_pct / 100)
                brk_dist     = (close - resist) if resist > 0 else 0.0
                trigger_a    = (brk_dist >= min_dist) and (vol_ratio >= prof.volume_mult)
                golden_cross = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
                trigger_b    = golden_cross and (rsi > prof.rsi_gc_min)
                cond_trend   = ema9 > ema21 > ema50
                cond_momentum = prof.rsi_min <= rsi <= prof.rsi_max

                cond_vwap = True
                if tf_used not in ("1d", "3d", "1w"):
                    for vwap_col in ("VWAP_D", "VWAP", "vwap"):
                        if vwap_col in bar.index and pd.notna(bar[vwap_col]):
                            vwap_val = float(bar[vwap_col])
                            if vwap_val > 0:
                                cond_vwap = close > vwap_val
                                break

                entry_ok  = (trigger_a or trigger_b) and cond_trend and cond_momentum and cond_vwap
                exit_mode = (
                    "RIDE_THE_WAVE"
                    if (vol_ratio >= prof.volume_spike or atr_pct >= prof.atr_pct_threshold)
                    else "QUICK_PROFIT"
                )

                if atr > 0:
                    sl_val = close - max(atr * prof.atr_sl_mult, close * (prof.quick_sl_pct / 100))
                    tp_val = close + max(atr * prof.atr_tp_mult, close * (prof.quick_tp_pct / 100))
                else:
                    sl_val = close * (1 - prof.quick_sl_pct / 100)
                    tp_val = close * (1 + prof.quick_tp_pct / 100)

                prof_emoji = PROFILE_EMOJI.get(prof.profile.value, "⚙️")
                score      = sum([brk_dist >= min_dist, vol_ratio >= prof.volume_mult, cond_trend, cond_momentum])

                def _fmt(v) -> str:
                    if v is None:
                        return "—"
                    v = float(v)
                    if v >= 1000:
                        return f"${v:,.2f}"
                    if v >= 1:
                        return f"${v:.4f}"
                    if v >= 0.01:
                        return f"${v:.5f}"
                    return f"${v:.8f}"

                if entry_ok:
                    trigger_lbl = "Breakout+GC" if (trigger_a and trigger_b) else ("Breakout" if trigger_a else "GoldenCross")
                    mode_emoji  = "🌊" if exit_mode == "RIDE_THE_WAVE" else "⚡"
                    txt = "\n".join([
                        f"🚀 *{symbol}* — `SINYAL BUY!` {mode_emoji} `{exit_mode}`",
                        f"  🧬 Profil : `{prof_emoji} {prof.profile.value}` (`{tf_used}{tf_note}`)",
                        f"  🎯 Trigger: `{trigger_lbl}`",
                        f"  💵 Entry  : `{_fmt(close)}`",
                        f"  🛑 SL     : `{_fmt(sl_val)}`",
                        f"  🎯 TP     : `{_fmt(tp_val)}`",
                        f"  📊 RSI:`{rsi:.1f}` Vol:`{vol_ratio:.1f}x{vol_warn}` ATR%:`{atr_pct:.3f}%`",
                    ])
                else:
                    failed = []
                    if not (trigger_a or trigger_b):
                        failed.append(f"NoTrig(vol={vol_ratio:.1f}x,gc={'✅' if golden_cross else '❌'})")
                    if not cond_trend:
                        failed.append("EMAStack")
                    if not cond_momentum:
                        failed.append(f"RSI({rsi:.0f} not in [{prof.rsi_min},{prof.rsi_max}])")
                    if not cond_vwap:
                        failed.append("BelowVWAP")

                    bar_str = "█" * score + "░" * (4 - score)
                    txt = "\n".join([
                        f"⏳ *{symbol}* — `{score}/4` [{bar_str}] `{prof_emoji} {prof.profile.value}` (`{tf_used}{tf_note}`)",
                        f"  💵 `{_fmt(close)}` RSI:`{rsi:.0f}` Vol:`{vol_ratio:.1f}x{vol_warn}` ATR%:`{atr_pct:.3f}%`",
                        f"  ❌ Gagal: `{', '.join(failed) if failed else '—'}`",
                    ])

                results.append(txt)

            except Exception as e:
                results.append(f"❌ *{symbol}*: Error `{str(e)[:120]}`")

        mode_label   = "🟡 TESTNET" if is_testnet else "🔴 LIVE"
        header_lines = [
            f"🔍 *DIAGNOSA SINYAL v{APP_VERSION} — {len(universe)} COIN* [{mode_label}]",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if is_testnet:
            header_lines.append("_⚠️ Testnet: volume & data mungkin tidak mencerminkan pasar real_")

        parts = list(results) + [f"\n🕐 `{_utcnow()} UTC`"]
        await tg_send_long(parts, header="\n".join(header_lines))

    except Exception as e:
        await tg_send(f"❌ Diagnosa error: `{str(e)[:200]}`")
    finally:
        if exchange is not None:
            await exchange.close()

async def cmd_strategy() -> None:
    try:
        # Baca universe dari universe.json, fallback ke .env
        from exchange import load_universe_json
        universe = load_universe_json()
        if not universe:
            universe = [s.strip() for s in os.getenv("UNIVERSE_WATCHLIST", "BTC/USDT,ETH/USDT").split(",")]

        header = f"📊 *PROFIL STRATEGI v{APP_VERSION} — {len(universe)} COIN*\n━━━━━━━━━━━━━━━━━━━━━━━━"
        by_profile: dict = {}
        for sym in universe:
            try:
                prof = get_coin_profile(sym)
                by_profile.setdefault(prof.profile.value, []).append((sym, prof))
            except Exception:
                by_profile.setdefault("unknown", []).append((sym, None))

        parts = []
        for pname, items in sorted(by_profile.items()):
            emoji = PROFILE_EMOJI.get(pname, "⚙️")
            tf    = PROFILE_TIMEFRAME.get(pname, "?")
            parts.append(f"\n{emoji} *{pname.upper()}* (`{tf}`) — {len(items)} koin")
            for sym, prof in items:
                if prof:
                    parts.append(
                        f"  `{sym:<14}` SL:{prof.quick_sl_pct:.1f}% "
                        f"TP:{prof.quick_tp_pct:.1f}% "
                        f"RSI:{prof.rsi_min:.0f}-{prof.rsi_max:.0f}"
                    )
                else:
                    parts.append(f"  `{sym}` (gagal load profil)")
        parts.append(f"\n🕐 `{_utcnow()} UTC`")
        await tg_send_long(parts, header=header)

    except Exception as e:
        await tg_send(f"❌ Error strategy: `{str(e)}`")

async def cmd_log() -> None:
    data = await api_get("/logs?limit=10")
    if not data:
        await tg_send("❌ Tidak bisa ambil log.")
        return

    lines     = ["📋 *LOG TERAKHIR*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    emoji_map = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴", "CRITICAL": "🆘"}
    for entry in data["logs"][:10]:
        ts  = (entry.get("timestamp") or "")[:19] or "—"
        lv  = entry.get("level", "")
        mod = entry.get("module", "")
        txt = (entry.get("message") or "")[:80]
        emj = emoji_map.get(lv, "•")
        lines.append(f"{emj} `{ts}` *{mod}*")
        lines.append(f"  `{txt}`")

    lines += ["", f"🕐 `{_utcnow()} UTC`"]
    await tg_send("\n".join(lines))

async def cmd_history() -> None:
    data = await api_get("/trades?limit=20")
    if not data or not data.get("trades"):
        await tg_send("📭 *Belum ada riwayat trade.*")
        return

    trades       = data["trades"]
    total_pnl    = sum(float(t.get("realized_pnl") or 0) for t in trades)
    wins         = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
    total_closed = sum(1 for t in trades if t.get("realized_pnl") is not None)
    win_rate     = (wins / total_closed * 100) if total_closed > 0 else 0

    header_lines = [
        "📜 *RIWAYAT TRADE (20 terakhir)*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Total Trade  : `{total_closed}`",
        f"  Win Rate     : `{win_rate:.1f}%`",
        f"  Total PnL    : `{_sign(total_pnl)}`",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    parts: List[str] = []
    for t in trades[:15]:
        side   = (t.get("side") or "").upper()
        symbol = t.get("symbol", "")
        price  = t.get("executed_price")
        pnl    = t.get("realized_pnl")
        slip   = t.get("slippage_pct", 0)
        origin = str(t.get("signal_origin") or "—")
        ts     = (t.get("timestamp") or "")[:16]
        fee    = t.get("fee_cost", 0)

        prof     = _profile_from_signal_origin(origin)
        prof_lbl = _profile_label(prof) if prof else "⚙️ VB"
        origin_short = origin[:60]

        if pnl is not None:
            emoji   = "✅" if float(pnl) >= 0 else "❌"
            pnl_str = _sign(pnl)
        else:
            emoji   = "🔄"
            pnl_str = "open"

        side_emoji = "🟢" if side == "BUY" else "🔴"
        parts.append("\n".join([
            f"{emoji} {side_emoji} *{symbol}* `{side}` {prof_lbl}",
            f"  🕐 `{ts}`",
            f"  💵 Price  : `{_usd(price)}`",
            f"  💰 PnL    : `{pnl_str}`",
            f"  📊 Slip   : `{slip:+.4f}%`",
            f"  💸 Fee    : `{_usd(fee)}`",
            f"  🔍 Signal : `{origin_short}`",
        ]))

    parts.append(f"\n🕐 `{_utcnow()} UTC`")
    await tg_send_long(parts, header="\n".join(header_lines))

async def cmd_run() -> None:
    if not os.path.exists(START_SCRIPT):
        await tg_send(
            f"❌ *start.sh tidak ditemukan:* `{START_SCRIPT}`\n"
            f"Set `BOT_DIR` di .env atau periksa path."
        )
        return

    await tg_send("▶️ *Menjalankan bot...*")
    subprocess.Popen(
        ["bash", START_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(4)
    # [BUG-FIX] Sebelumnya api_get("/health") memanggil BOT_API + "/health"
    # = ".../api/health" — padahal api_server.py cuma mendaftarkan "/health"
    # di ROOT (tanpa prefix /api), bukan "/api/health". Akibatnya panggilan
    # ini SELALU gagal (404/None) walau bot berhasil start, sehingga /run
    # selalu menampilkan pesan "API belum merespons" meski sebenarnya sudah.
    # Sekarang: pakai endpoint "/status" yang memang ada di bawah /api/ dan
    # sudah dipakai sukses di cmd_status(), sebagai indikator API sudah hidup.
    data = await api_get("/status")
    if data:
        await tg_send("✅ *Bot berhasil dijalankan!*\nGunakan /status untuk cek kondisi.")
    else:
        await tg_send(
            "⚠️ *Bot dijalankan tapi API belum merespons.*\n"
            "Tunggu beberapa detik lalu ketik /status"
        )

async def cmd_stop() -> None:
    if not os.path.exists(STOP_SCRIPT):
        await tg_send(
            f"❌ *stop.sh tidak ditemukan:* `{STOP_SCRIPT}`\n"
            f"Set `BOT_DIR` di .env atau periksa path."
        )
        return

    await tg_send("🛑 *Menghentikan bot...*")
    subprocess.run(["bash", STOP_SCRIPT], check=False)
    await asyncio.sleep(2)
    await tg_send("✅ *Bot dihentikan.*\nGunakan /run untuk menjalankan kembali.")

async def cmd_pause() -> None:
    data = await api_post("/bot/pause_strategy")
    if data:
        await tg_send(
            "⏸ *Strategi di-pause.*\n"
            "Bot tidak akan buka posisi baru.\n"
            "Gunakan /resume untuk lanjut."
        )
    else:
        await tg_send("❌ Gagal pause. Cek apakah bot berjalan.")

async def cmd_resume() -> None:
    data = await api_post("/bot/resume_strategy")
    if data:
        await tg_send("▶️ *Strategi di-resume.*\nBot kembali analisis sinyal.")
    else:
        await tg_send("❌ Gagal resume. Cek apakah bot berjalan.")

async def cmd_panic() -> None:
    lines = [
        "🆘 *PANIC BUTTON*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Ini akan CLOSE SEMUA posisi terbuka!",
        "",
        "Ketik `/panic_confirm` untuk konfirmasi.",
        "Atau abaikan pesan ini untuk batal.",
    ]
    await tg_send("\n".join(lines))

async def cmd_panic_confirm() -> None:
    await tg_send("🆘 *Executing PANIC CLOSE...*")
    try:
        data = await api_post("/bot/panic")
        if not data:
            await tg_send("❌ Panic gagal: API tidak merespons atau API key salah.")
            return

        failed_str = ", ".join(data.get("failed_symbols", [])) or "tidak ada"
        lines = [
            "🆘 *PANIC EXECUTED*",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"  Posisi ditemukan : `{data.get('positions_found', 0)}`",
            f"  Berhasil ditutup : `{data.get('closed_count', 0)}`",
            f"  Gagal            : `{failed_str}`",
            "  Bot status       : `HALTED`",
            "",
            "Gunakan /resume untuk lanjut trading.",
            f"🕐 `{_utcnow()} UTC`",
        ]
        await tg_send("\n".join(lines))
    except Exception as e:
        await tg_send(f"❌ Panic error: `{str(e)}`\nClose posisi manual di Binance!")

async def cmd_scores() -> None:
    data = await api_get("/intelligence/scores")
    if not data or not data.get("scores"):
        await tg_send(
            "⚠️ *Score board belum tersedia.*\n"
            "_Intelligence pipeline mungkin belum aktif atau belum collect data._"
        )
        return

    scores: List[dict] = data["scores"]
    scores_sorted = sorted(
        scores,
        key=lambda sc: (sc.get("total_score") is not None, sc.get("total_score") or 0),
        reverse=True,
    )

    header = [
        f"🎯 *SCORE BOARD — {len(scores_sorted)} COIN*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "`COIN           SCORE  BAR       REGIME    TRG`",
    ]

    parts: List[str] = []
    for sc in scores_sorted:
        symbol = sc.get("symbol", "")
        score_raw = sc.get("total_score")
        score   = float(score_raw) if score_raw is not None else 0.0
        regime  = sc.get("regime", "undefined")
        trigger = "✅" if sc.get("trigger_met") else "❌"
        bar     = _score_bar(score, 6)
        reg_emj = _REGIME_EMOJI.get(regime, "⚪")
        thresh  = 70
        has_score = score_raw is not None
        star    = "🔥" if has_score and score >= thresh and sc.get("trigger_met") else ""
        score_txt = f"{score:5.1f}" if has_score else "  N/A"

        base = symbol.split("/")[0]
        parts.append(
            f"{star}`{base:<6}` `{score_txt}` `{bar}` {reg_emj} `{regime[:8]:<8}` {trigger}"
        )

    parts.append(f"\n🕐 `{_utcnow()} UTC`")
    await tg_send_long(parts, header="\n".join(header))

async def cmd_score_symbol(symbol: str) -> None:
    sym_upper = symbol.upper()
    if "/" not in sym_upper:
        sym_upper += "/USDT"

    data = await api_get(f"/intelligence/scores/{sym_upper}")
    if not data:
        await tg_send(
            f"⚠️ Tidak ada data skor untuk `{sym_upper}`.\n"
            "_Pastikan simbol ada di universe._"
        )
        return

    score    = float(data.get("total_score", 0))
    threshold = float(data.get("entry_threshold", 70))
    regime   = data.get("regime", "undefined")
    reg_emj  = _REGIME_EMOJI.get(regime, "⚪")
    trigger  = "✅ TERPENUHI" if data.get("above_threshold") else "❌ BELUM"
    action   = data.get("action_taken", "—")
    narrative = data.get("narrative", "")

    breakdown = data.get("breakdown", {})
    cat_lines = []
    for cat, cat_score in breakdown.items():
        bar = _score_bar(float(cat_score), 8)
        cat_lines.append(f"  `{cat:<12}` `{float(cat_score):5.1f}` `{bar}`")

    lines = [
        f"🔬 *SKOR DETAIL: {sym_upper}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Total Score : `{score:.1f} / 100` (threshold: `{threshold:.0f}`)",
        f"  Bar         : `{_score_bar(score, 12)}`",
        f"  Regime      : {reg_emj} `{regime}`",
        f"  Trigger     : {trigger}",
        f"  Keputusan   : `{action}`",
        "",
        "*Breakdown per kategori:*",
    ] + cat_lines

    if narrative:
        lines += ["", f"📝 `{narrative[:200]}`"]

    history = data.get("history_24h", [])
    if history:
        recent = [
            f"{float(h.get('total_score') or 0):.0f}"
            for h in history[-6:]
        ]
        lines += ["", f"📈 Trend (6 bar): `{'→'.join(recent)}`"]

    lines += ["", f"🕐 `{_utcnow()} UTC`"]
    await tg_send("\n".join(lines))

async def cmd_regime() -> None:
    data = await api_get("/intelligence/regime")
    if not data or not data.get("regimes"):
        await tg_send(
            "⚠️ *Data regime belum tersedia.*\n"
            "_Intelligence pipeline mungkin belum aktif._"
        )
        return

    regimes: List[dict] = data["regimes"]
    sorted_regimes = sorted(regimes, key=lambda x: x.get("symbol", ""))
    total = len(sorted_regimes)
    summary = data.get("summary", {})
    sum_lines = ["MARKET REGIME - " + str(total) + " koin"]
    for reg, cnt in sorted(summary.items(), key=lambda x: -x[1]):
        sum_lines.append(reg + ": " + str(cnt) + " koin")
    sum_lines.append("Detail dikirim per 30 koin...")
    await tg_send("\n".join(sum_lines))
    chunk_size = 30
    for i in range(0, total, chunk_size):
        chunk = sorted_regimes[i:i+chunk_size]
        msg_lines = ["Koin " + str(i+1) + "-" + str(min(i+chunk_size, total)) + ":"]
        for r in chunk:
            symbol = r.get("symbol", "")
            regime = r.get("regime", "undefined")
            conf   = float(r.get("confidence", 0))
            base   = symbol.split("/")[0]
            msg_lines.append(base + " - " + regime + " " + str(round(conf*100)) + "%")
        await tg_send("\n".join(msg_lines))

async def cmd_attribution() -> None:
    data = await api_get("/analytics/attribution?lookback_days=7")
    if not data:
        await tg_send(
            "⚠️ *Analytics belum tersedia.*\n"
            "_Pastikan ANALYTICS_ENABLED=true dan sudah ada cukup data trade._"
        )
        return

    report = data.get("attribution") or {}
    insights = report.get("insights", [])
    summary  = report

    lines = [
        "📊 *ATTRIBUTION REPORT — 7 Hari*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Total Trades : `{summary.get('total_trades', 0)}`",
        f"  Win Rate     : `{float(summary.get('overall_win_rate', 0) or 0):.1f}%`",
        f"  Profit Factor: `{float(summary.get('overall_profit_factor', 0) or 0):.2f}`",
        "",
        "*Top Insight:*",
    ]

    if insights:
        for ins in insights[:3]:
            lines.append(f"  ⚠️ {ins[:120]}")
    else:
        lines.append("  _Tidak ada insight signifikan untuk periode ini._")

    regime_perf = report.get("regime_performance", [])
    if regime_perf:
        lines += ["", "*Win Rate per Regime:*"]
        for rp in sorted(regime_perf, key=lambda x: -(x.get("win_rate") or 0)):
            reg = rp.get("regime", "undefined")
            emj = _REGIME_EMOJI.get(reg, "⚪")
            wr  = float(rp.get("win_rate") or 0)
            n   = int(rp.get("total_trades") or 0)
            if n >= 5:
                lines.append(f"  {emj} `{reg:<22}` WR: `{wr:.1f}%` ({n} trades)")

    lines += ["", f"🕐 `{_utcnow()} UTC`"]
    await tg_send("\n".join(lines))

async def cmd_suggestions() -> None:
    data = await api_get("/meta_learner/suggestions")
    if data is None:
        await tg_send(
            "⚠️ *Meta-learner endpoint tidak tersedia.*\n"
            "_Pastikan META_LEARNER_ENABLED=true._"
        )
        return

    suggestions = data.get("suggestions", [])
    if not suggestions:
        await tg_send("✅ *Tidak ada suggestion pending dari meta-learner.*")
        return

    lines = [
        f"🤖 *META-LEARNER SUGGESTIONS ({len(suggestions)})*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for sug in suggestions[:5]:
        sug_id    = sug.get("id", "?")
        symbol    = sug.get("symbol", "?")
        param     = sug.get("parameter_name", "?")
        old_v     = sug.get("old_value", "?")
        new_v     = sug.get("new_value", "?")
        reason    = sug.get("reason", "")[:80]
        conf      = float(sug.get("confidence", 0)) * 100

        lines += [
            f"\n📌 *ID #{sug_id}* — `{symbol}` / `{param}`",
            f"  Perubahan : `{old_v}` → `{new_v}`",
            f"  Confidence: `{conf:.0f}%`",
            f"  Alasan    : _{reason}_",
            f"  Approve   : `/approve_{sug_id}`",
            f"  Reject    : `/reject_{sug_id}`",
        ]

    lines += ["", f"🕐 `{_utcnow()} UTC`"]
    await tg_send("\n".join(lines))

async def cmd_approve_suggestion(suggestion_id: str) -> None:
    data = await api_post(f"/meta_learner/approve/{suggestion_id}")
    # [BUG-FIX] api_server.py /api/meta_learner/approve/{id} SELALU
    # mengembalikan "status": "approved" pada body 200 OK-nya, terlepas dari
    # apakah suggestion beneran berhasil di-apply atau tidak (mis. ditolak
    # guardrail: cooldown 1 jam, delta tidak safe, dsb). Hasil SEBENARNYA ada
    # di field "applied" (bool) + "message". Sebelumnya kode ini cek
    # `data.get("status") == "approved"` yang SELALU True, jadi Telegram
    # selalu menampilkan "✅ berhasil di-approve!" walau sebenarnya gagal.
    # api_post() sendiri sudah return None untuk HTTP non-200, jadi cabang
    # "detail" di bawah tidak pernah tercapai — diganti pakai "message".
    if data and data.get("applied"):
        await tg_send(
            f"✅ *Suggestion #{suggestion_id} di-approve!*\n"
            f"Parameter akan diperbarui. Pantau performa dalam 24 jam ke depan."
        )
    elif data:
        msg = data.get("message") or "Ditolak guardrail atau alasan lain."
        await tg_send(f"❌ Approve gagal: `{msg[:200]}`")
    else:
        await tg_send(f"❌ API tidak merespons. Pastikan bot berjalan.")

async def cmd_reject_suggestion(suggestion_id: str) -> None:
    data = await api_post(f"/meta_learner/reject/{suggestion_id}")
    # [BUG-FIX] Sama seperti cmd_approve_suggestion: "status" di response
    # SELALU "rejected", hasil sebenarnya ada di field "rejected" (bool).
    if data and data.get("rejected"):
        await tg_send(
            f"🚫 *Suggestion #{suggestion_id} di-reject.*\n"
            f"Meta-learner tidak akan membuat suggestion yang sama dalam waktu dekat."
        )
    elif data:
        msg = data.get("message") or "Unknown reason."
        await tg_send(f"❌ Reject gagal: `{msg[:200]}`")
    else:
        await tg_send(f"❌ Reject gagal atau API tidak merespons.")


async def cmd_crosslearn() -> None:
    data = await api_get("/crosslearn/status")
    if not data:
        await tg_send("❌ Tidak bisa mengambil status cross-learning.")
        return
    summary = data.get("summary", {})
    enabled = summary.get("enabled", data.get("enabled", False))
    status_ok = summary.get("status") == "OK"
    lines = ["📡 *Cross-Learning Status*", ""]
    lines.append(f"Status: {'✅ AKTIF' if (enabled and status_ok) else '❌ NONAKTIF'}")
    if summary:
        lines.append(f"Peer DB: {summary.get('peer_db','?')}")
        lines.append(f"Trades 30d: {summary.get('trades_30d', 0)}")
        lines.append(f"Scores 30d: {summary.get('scores_30d', 0)}")
        top = summary.get('top_coins', [])
        if top:
            t = top[0]
            lines.append(f"Top coin: {t.get('symbol','?')} | WR: {t.get('win_rate',0):.1f}% ({t.get('trades',0)} trades)")
    await tg_send("\n".join(lines))

async def cmd_swaphistory() -> None:
    data = await api_get("/crosslearn/swap_history")
    if not data:
        await tg_send("❌ Tidak bisa mengambil riwayat swap.")
        return
    swaps = data.get("swaps", [])
    if not swaps:
        await tg_send("📋 *Swap History*\n\nBelum ada swap yang terjadi.")
        return
    lines = [f"📋 *Swap History* ({len(swaps)} swap)", ""]
    for s in swaps[:10]:
        lines.append(
            f"• {s.get('timestamp','')[:10]} | "
            f"OUT: {s.get('coin_out','?')} → IN: {s.get('coin_in','?')} | "
            f"Alasan: {s.get('reason','?')}"
        )
    await tg_send("\n".join(lines))



async def handle_update(update: dict) -> None:
    cq = update.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            return

        data = (cq.get("data") or "").strip()
        await tg_answer_callback(cq.get("id", ""), "OK")

        mapping = {
            "menu": cmd_menu,
            "status": cmd_status,
            "balance": cmd_balance,
            "positions": cmd_positions,
            "metrics": cmd_metrics,
            "diagnosa": cmd_diagnosa,
            "strategy": cmd_strategy,
            "history": cmd_history,
            "log": cmd_log,
            "scores": cmd_scores,
            "regime": cmd_regime,
            "attribution": cmd_attribution,
            "suggestions": cmd_suggestions,
            "run": cmd_run,
            "stop": cmd_stop,
            "pause": cmd_pause,
            "resume": cmd_resume,
        }

        if data == "panic_prompt":
            await tg_send(
                "⚠️ *PANIC CLOSE*\nIni akan close semua posisi.\nLanjutkan?",
                reply_markup=_kb([
                    [("✅ YA, CLOSE ALL", "panic_confirm"), ("❌ Batal", "menu")],
                ]),
            )
            return

        if data == "panic_confirm":
            await cmd_panic_confirm()
            return

        handler = mapping.get(data)
        if handler:
            await handler()
        else:
            await tg_send(f"❓ Tombol `{data}` belum dikenali.\nKetik /menu untuk buka menu.")
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
        log.warning(
            "Unauthorized access dari chat_id: %s",
            msg.get("chat", {}).get("id"),
        )
        return

    global _last_msg
    _last_msg = msg
    raw_text = msg.get("text", "").strip()
    text     = raw_text.split("@")[0].lower()
    log.info("Command diterima: %s", text)

    handler = COMMANDS.get(text)
    if handler:
        await handler()
        return

    if text == "/score":
        await tg_send("ℹ️ Gunakan format: `/score BTC/USDT` atau `/score BTC`")
        return

    if text.startswith("/score "):
        parts = raw_text.split(maxsplit=1)
        if len(parts) == 2:
            await cmd_score_symbol(parts[1].strip())
            return

    if text.startswith("/approve_"):
        sug_id = text[len("/approve_"):]
        if sug_id:
            await cmd_approve_suggestion(sug_id)
            return

    if text.startswith("/reject_"):
        sug_id = text[len("/reject_"):]
        if sug_id:
            await cmd_reject_suggestion(sug_id)
            return

    if text.startswith("/addcoin "):
        await cmd_addcoin()
        return
    if text.startswith("/removecoin "):
        await cmd_removecoin()
        return
    if text.startswith("/setconfig "):
        await cmd_setconfig()
        return

    await tg_send(
        f"❓ Command `{text}` tidak dikenal.\n"
        "Ketik /help untuk daftar command."
    )

async def cmd_forecast() -> None:
    try:
        result = await api_get("/forecast")
        if result is None:
            await tg_send("❌ Gagal koneksi ke API bot.")
            return
        forecasts = result.get("forecasts", [])
        if not forecasts:
            await tg_send(
                "📭 *Belum ada data forecast.*\n"
                "Bot perlu jalan dulu agar data terkumpul."
            )
            return

        now = datetime.now(timezone.utc)

        # ── Freshness check ──
        first_updated = forecasts[0].get("last_updated", "") if forecasts else ""
        stale_warning = ""
        freshness = "Waktu update tidak diketahui"
        try:
            last_dt = datetime.fromisoformat(first_updated.replace("Z", "+00:00"))
            diff_minutes = int((now - last_dt).total_seconds() / 60)
            if diff_minutes <= 5:
                freshness = f"Fresh ({diff_minutes} mnt lalu) ✅"
            elif diff_minutes <= 15:
                freshness = f"Fresh ({diff_minutes} mnt lalu) ✅"
            elif diff_minutes <= 60:
                freshness = f"{diff_minutes} mnt lalu ⚠️"
            else:
                hours = diff_minutes // 60
                freshness = f"{hours} jam lalu — jalankan /forecast lagi ⚠️"
                stale_warning = "⚠️ *Data sudah lama! Bot mungkin tidak jalan.*"
        except Exception:
            pass

        ready   = [f for f in forecasts if f.get("trigger_met")]
        waiting = [f for f in forecasts if not f.get("trigger_met")]
        total   = len(forecasts)

        header_lines = [
            f"🔮 *FORECAST {total} KOIN*",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"🕐 {freshness}",
            "📈 Urut: probabilitas tertinggi",
        ]
        if stale_warning:
            header_lines.append(stale_warning)

        parts = []

        # ── Helper ──
        def _ind_icon(status: str) -> str:
            return "✅" if status == "pass" else "❌" if status == "fail" else "⚠️"

        def _htf_icon(r: str) -> str:
            return "🟢" if r == "bullish" else "🔴" if r == "bearish" else "🟡"

        def _trend_icon(t: str) -> str:
            if "kuat" in t:   return "🚀"
            if "lemah" in t:  return "📈"
            if "Bearish" in t: return "📉"
            return "➡️"

        # ════════════════════════════════════
        # READY SECTION
        # ════════════════════════════════════
        if ready:
            parts.append(f"\n✅ *SIAP ENTRY — {len(ready)} KOIN*")
            for i, f in enumerate(ready[:5], 1):
                sym       = f.get("symbol", "?")
                price     = f.get("current_price")
                tp        = f.get("suggested_tp")
                sl        = f.get("suggested_sl")
                prob      = f.get("probability_up_pct", 0)
                profit    = f.get("potential_profit_pct")
                loss      = f.get("potential_loss_pct")
                rr        = f.get("rr_ratio")
                quality   = f.get("signal_quality", "?")
                regime    = f.get("regime", "undefined")
                reg_conf  = f.get("regime_confidence", 0)
                score     = f.get("total_score", 0)
                thresh    = f.get("threshold_used", 65)
                gap       = f.get("threshold_gap", 0)
                sig_conf  = f.get("signal_confidence", 0)
                profile   = f.get("strategy_profile", "")
                tf        = f.get("timeframe", "15m")
                ctf       = f.get("confirm_tf", "1h")
                trend     = f.get("trend_summary", "")
                hold_disp = f.get("hold_display", "")
                tp_eta    = f.get("tp_eta_wib", "")
                tp_date   = f.get("tp_eta_date", "")
                htf_res   = f.get("confirm_tf_result")
                support   = f.get("nearest_support")
                resist    = f.get("nearest_resistance")
                fib_s     = f.get("fib_support")
                fib_r     = f.get("fib_resistance")
                updated   = f.get("last_updated", "")

                # Indikator
                ind       = f.get("indicators", {})
                ind_thr   = f.get("indicator_thresholds", {})
                ind_st    = f.get("indicator_status", {})
                rsi_v     = ind.get("rsi")
                ema9      = ind.get("ema9")
                ema21     = ind.get("ema21")
                ema50     = ind.get("ema50")
                ema_bull  = ind.get("ema_bullish", False)
                adx_v     = ind.get("adx")
                atr_pct   = ind.get("atr_pct")
                vol_ratio = ind.get("volume_ratio")
                atr_trend = ind.get("atr_trend", "")

                # Score breakdown top 3
                breakdown = f.get("score_breakdown", {})
                weights   = f.get("category_weights", {})
                bd_sorted = sorted(
                    [(k, v) for k, v in breakdown.items() if v],
                    key=lambda x: x[1], reverse=True
                )

                reg_emoji = _REGIME_EMOJI.get(regime, "⚪")
                q_emoji   = {"excellent": "🌟", "good": "✅", "fair": "🟡", "poor": "🔴"}.get(quality, "⚪")
                prob_bar  = _score_bar(float(prob), width=6)
                regime_str = regime.replace("_", " ").title()
                profile_str = profile.replace("_", " ").title()

                try:
                    kdt     = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    km      = int((now - kdt).total_seconds() / 60)
                    upd_str = f"{km} mnt lalu"
                except Exception:
                    upd_str = "—"

                lines = [f"\n{'━'*24}"]
                lines.append(f"{i}. {q_emoji} *{sym}*  `{quality.upper()}`  {prob_bar} `{prob}%`")
                lines.append(f"  💵 `{_usd(price)}`  |  🕐 {upd_str}")

                # Profile + TF
                lines.append(f"  📋 *{profile_str}* | TF: `{tf}` konfirmasi `{ctf}`")

                # Regime + tren
                lines.append(f"  {reg_emoji} Regime: `{regime_str}` (conf `{reg_conf:.0%}`)")
                if trend:
                    lines.append(f"  {_trend_icon(trend)} Tren: *{trend}*")

                # HTF konfirmasi
                if htf_res:
                    lines.append(f"  {_htf_icon(htf_res)} Konfirmasi {ctf}: *{htf_res.upper()}*")

                # Score + threshold
                gap_str = f"+{gap:.1f}" if gap >= 0 else f"{gap:.1f}"
                lines.append(f"  🎯 Score: `{score:.1f}` / threshold `{thresh:.0f}` ({gap_str})")
                lines.append(f"  🔒 Confidence: `{sig_conf:.0%}`")

                # TP / SL / RR
                if tp:
                    profit_str = f" `+{profit}%`" if profit else ""
                    lines.append(f"  🎯 TP : `{_usd(tp)}`{profit_str}")
                if sl:
                    loss_str = f" `-{loss}%`" if loss else ""
                    lines.append(f"  🛑 SL : `{_usd(sl)}`{loss_str}")
                if rr:
                    lines.append(f"  ⚖️ RR : `{rr}` : 1")

                # Indikator detail
                ind_lines = []
                if rsi_v is not None:
                    rsi_icon = _ind_icon(ind_st.get("rsi", ""))
                    ob_warn = " ⚠️OB" if ind_st.get("rsi_overbought") else ""
                    rsi_range = f"[{ind_thr.get('rsi_min','?')}–{ind_thr.get('rsi_max','?')}]"
                    ind_lines.append(f"RSI `{rsi_v:.1f}` {rsi_icon}{ob_warn} {rsi_range}")
                if ema9 and ema21:
                    ema_icon = "✅" if ema_bull else "❌"
                    ind_lines.append(f"EMA9>`{_usd(ema9)}`>{_usd(ema21)} {ema_icon}")
                if adx_v is not None:
                    adx_st = ind_st.get("adx", "")
                    adx_icon = "💪" if adx_st == "strong" else "➡️" if adx_st == "moderate" else "😴"
                    ind_lines.append(f"ADX `{adx_v:.1f}` {adx_icon}")
                if atr_pct is not None:
                    atr_icon = _ind_icon(ind_st.get("atr_pct", ""))
                    atr_trend_str = f" ({atr_trend})" if atr_trend else ""
                    ind_lines.append(f"ATR% `{atr_pct:.3f}%` {atr_icon}{atr_trend_str}")
                if vol_ratio is not None:
                    vol_icon = _ind_icon(ind_st.get("volume", ""))
                    ind_lines.append(f"Vol `{vol_ratio:.2f}x` {vol_icon}")
                if ind_lines:
                    lines.append(f"  📊 *Indikator:*")
                    for il in ind_lines:
                        lines.append(f"     • {il}")

                # Score breakdown top 3
                if bd_sorted:
                    bd_str = "  ".join([f"{k[:4].title()}`{v:.0f}`" for k, v in bd_sorted[:3]])
                    lines.append(f"  📈 Top skor: {bd_str}")

                # Support / Resistance
                if support or resist:
                    sr_parts = []
                    if support: sr_parts.append(f"S:`{_usd(support)}`")
                    if resist:  sr_parts.append(f"R:`{_usd(resist)}`")
                    lines.append(f"  📍 {' | '.join(sr_parts)}")
                if fib_s or fib_r:
                    fib_parts = []
                    if fib_s: fib_parts.append(f"🌀S:`{_usd(fib_s)}`")
                    if fib_r: fib_parts.append(f"🌀R:`{_usd(fib_r)}`")
                    lines.append(f"  {' | '.join(fib_parts)}")

                # Timing
                if hold_disp:
                    lines.append(f"  ⏱ Hold est.: *{hold_disp}*")
                if tp_eta:
                    lines.append(f"  🏁 TP est.  : *{tp_date if tp_date else tp_eta}*")

                parts.append("\n".join(lines))

        # ════════════════════════════════════
        # WAITING SECTION
        # ════════════════════════════════════
        if waiting:
            parts.append(f"\n⏳ *HAMPIR SIAP — {len(waiting)} koin belum trigger*")
            for i, f in enumerate(waiting[:5], 1):
                sym       = f.get("symbol", "?")
                price     = f.get("current_price")
                prob      = f.get("probability_up_pct", 0)
                score     = f.get("total_score", 0)
                regime    = f.get("regime", "undefined")
                thresh    = f.get("threshold_used", 65)
                gap       = f.get("threshold_gap", 0)
                trend     = f.get("trend_summary", "")
                profile   = f.get("strategy_profile", "")
                htf_res   = f.get("confirm_tf_result")
                reg_emoji = _REGIME_EMOJI.get(regime, "⚪")
                prob_bar  = _score_bar(float(prob), width=6)
                regime_str = regime.replace("_", " ").title()
                gap_str   = f"{gap:.1f}" if gap >= 0 else f"{gap:.1f}"
                need_str  = f"perlu +{abs(gap):.1f} poin" if gap < 0 else "sudah melewati threshold"
                profile_str = profile.replace("_", " ").title()
                parts.append("\n".join([
                    f"\n{i}. *{sym}*  `{_usd(price)}`",
                    f"  📋 {profile_str} | {reg_emoji} `{regime_str}`",
                    f"  🎯 Score: `{score:.1f}` / `{thresh:.0f}` — {need_str}",
                    f"  📈 Prob: `{prob}%` {prob_bar}" + (f"  {_trend_icon(trend)} {trend}" if trend else ""),
                ]))

        parts.append(f"\n🕐 `{_utcnow()} UTC`")
        await tg_send_long(parts, header="\n".join(header_lines))

    except Exception as e:
        await tg_send(f"❌ Forecast error: `{str(e)[:200]}`")

async def cmd_universe() -> None:
    try:
        result = await api_get("/config/current")
        if result is None:
            await tg_send("Gagal koneksi ke API bot.")
            return
        wl = result.get("config", {}).get("universe_watchlist", [])
        total = len(wl)
        await tg_send("Universe Watchlist: " + str(total) + " koin\nDikirim per 50 koin...")
        chunk_size = 50
        for i in range(0, total, chunk_size):
            chunk = wl[i:i+chunk_size]
            msg = "Koin " + str(i+1) + "-" + str(min(i+chunk_size, total)) + ":\n"
            for j, coin in enumerate(chunk, i+1):
                msg += str(j) + ". " + coin + "\n"
            await tg_send(msg)
        await tg_send("Gunakan /addcoin SYMBOL untuk tambah\nGunakan /removecoin SYMBOL untuk hapus")
    except Exception as e:
        await tg_send("Error: " + str(e))

async def cmd_addcoin() -> None:
    text = _last_msg.get("text", "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        await tg_send("Format: /addcoin SYMBOL\nContoh: /addcoin SOL/USDT")
        return
    symbol = parts[1].strip().upper()
    if "/" not in symbol:
        symbol = symbol + "/USDT"
    try:
        result = await api_get("/config/current")
        if result is None:
            await tg_send("Gagal koneksi ke API bot.")
            return
        wl = result.get("config", {}).get("universe_watchlist", [])
        if symbol in wl:
            await tg_send(symbol + " sudah ada di universe.")
            return
        wl.append(symbol)
        result = await api_post("/config/update", {"universe_watchlist": wl})
        if result is None:
            await tg_send("Gagal koneksi ke API bot saat update.")
            return
        if result.get("success"):
            await tg_send("[OK] " + symbol + " ditambahkan ke universe!\nTotal: " + str(len(wl)) + " koin.\nAktif dalam ~30 detik.")
        else:
            await tg_send("Gagal: " + str(result.get("message","Unknown error")))
    except Exception as e:
        await tg_send("Error: " + str(e))

async def cmd_removecoin() -> None:
    text = _last_msg.get("text", "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        await tg_send("Format: /removecoin SYMBOL\nContoh: /removecoin SOL/USDT")
        return
    symbol = parts[1].strip().upper()
    if "/" not in symbol:
        symbol = symbol + "/USDT"
    try:
        result = await api_get("/config/current")
        if result is None:
            await tg_send("Gagal koneksi ke API bot.")
            return
        wl = result.get("config", {}).get("universe_watchlist", [])
        if symbol not in wl:
            await tg_send(symbol + " tidak ada di universe.")
            return
        wl.remove(symbol)
        result = await api_post("/config/update", {"universe_watchlist": wl})
        if result is None:
            await tg_send("Gagal koneksi ke API bot saat update.")
            return
        if result.get("success"):
            await tg_send("[OK] " + symbol + " dihapus dari universe!\nSisa: " + str(len(wl)) + " koin.\nAktif dalam ~30 detik.")
        else:
            await tg_send("Gagal: " + str(result.get("message","Unknown error")))
    except Exception as e:
        await tg_send("Error: " + str(e))

async def cmd_getconfig() -> None:
    try:
        result = await api_get("/config/current")
        if result is None:
            await tg_send("Gagal koneksi ke API bot.")
            return
        cfg = result.get("config", {})
        msg = "Config Bot Saat Ini:\n\n"
        msg += "Exchange: " + str(cfg.get("exchange_id","?")) + "\n"
        msg += "Testnet: " + str(cfg.get("testnet","?")) + "\n"
        uw = cfg.get("universe_watchlist", [])
        msg += f"Universe: {len(uw)} koin (gunakan /universe untuk lihat daftar)\n"
        msg += "Max Posisi: " + str(cfg.get("max_open_positions","?")) + "\n"
        msg += "Modal: " + str(cfg.get("initial_capital","?")) + "\n"
        msg += "Risk/Trade: " + str(cfg.get("risk_per_trade_pct","?")) + "%\n"
        msg += "Max DD: " + str(cfg.get("max_drawdown_pct","?")) + "%\n"
        msg += "Daily Loss Limit: " + str(cfg.get("daily_loss_limit_pct","?")) + "%\n"
        msg += "SL: " + str(cfg.get("stop_loss_pct","?")) + "%\n"
        msg += "TP: " + str(cfg.get("take_profit_pct","?")) + "%\n"
        msg += "Trailing ATR: " + str(cfg.get("trailing_atr_mult","?")) + "\n"
        msg += "Trailing: " + str(cfg.get("use_trailing_stop","?")) + "\n"
        await tg_send(msg)
    except Exception as e:
        await tg_send("Error: " + str(e))

async def cmd_setconfig() -> None:
    text = _last_msg.get("text", "").strip()
    parts = text.split(None, 2)
    if len(parts) < 3:
        msg = "Update Config Bot\n\n"
        msg += "Format: /setconfig key value\n\n"
        msg += "Key tersedia:\n"
        msg += "max_open_positions - maks posisi\n"
        msg += "max_drawdown_pct - maks drawdown pct\n"
        msg += "risk_per_trade_pct - risk per trade pct\n"
        msg += "daily_loss_limit_pct - batas loss harian pct\n"
        msg += "stop_loss_pct - SL default pct\n"
        msg += "take_profit_pct - TP default pct\n"
        msg += "trailing_atr_mult - ATR multiplier trailing\n"
        msg += "universe_watchlist - daftar koin pantauan (pisah koma)\n"
        msg += "initial_capital - modal awal\n"
        msg += "testnet - true atau false\n"
        msg += "rsi_min / rsi_max - batas RSI global\n"
        msg += "lookback_candles - jumlah candle historis diambil\n"
        await tg_send(msg)
        return
    key = parts[1].strip()
    value = parts[2].strip()
    allowed_int   = ["max_open_positions","lookback_candles","rsi_min","rsi_max"]
    allowed_float = ["max_drawdown_pct","risk_per_trade_pct","daily_loss_limit_pct","stop_loss_pct","take_profit_pct","trailing_atr_mult","atr_multiplier_sl","atr_multiplier_tp","initial_capital","max_position_size_pct","max_slippage_pct","min_order_value_usdt"]
    allowed_bool  = ["testnet","use_trailing_stop","telegram_enabled","paper_trading_mode"]
    allowed_list  = ["universe_watchlist"]
    allowed_str   = ["exchange_id","api_key","api_secret","api_passphrase","telegram_bot_token","telegram_chat_id"]
    all_allowed   = allowed_int + allowed_float + allowed_bool + allowed_list + allowed_str
    if key not in all_allowed:
        await tg_send("Key " + key + " tidak dikenali.")
        return
    try:
        if key in allowed_int:
            typed_value = int(value)
        elif key in allowed_float:
            typed_value = float(value)
        elif key in allowed_bool:
            typed_value = value.lower() == "true"
        elif key in allowed_list:
            typed_value = [s.strip() for s in value.split(",")]
        else:
            typed_value = value
    except ValueError:
        await tg_send("Nilai " + value + " tidak valid untuk key " + key)
        return
    try:
        result = await api_post("/config/update", {key: typed_value})
        if result is None:
            await tg_send("Gagal koneksi ke API bot.")
            return
        if result.get("success"):
            await tg_send("[OK] Config diupdate! Key: " + str(key) + " Value: " + str(typed_value) + " Aktif dalam 30 detik.")
        else:
            await tg_send("Gagal: " + str(result.get("message","Unknown error")))
    except Exception as e:
        await tg_send("Error koneksi API: " + str(e))


COMMANDS = {
    "/start":         cmd_start,
    "/help":          cmd_help,
    "/menu":          cmd_menu,
    "/status":        cmd_status,
    "/balance":       cmd_balance,
    "/positions":     cmd_positions,
    "/metrics":       cmd_metrics,
    "/diagnosa":      cmd_diagnosa,
    "/strategy":      cmd_strategy,
    "/log":           cmd_log,
    "/history":       cmd_history,
    "/run":           cmd_run,
    "/stop":          cmd_stop,
    "/pause":         cmd_pause,
    "/resume":        cmd_resume,
    "/panic":         cmd_panic,
    "/panic_confirm": cmd_panic_confirm,
    "/scores":        cmd_scores,
    "/regime":        cmd_regime,
    "/attribution":   cmd_attribution,
    "/suggestions":   cmd_suggestions,
    "/crosslearn":    cmd_crosslearn,
    "/swaphistory":   cmd_swaphistory,
    "/setconfig":     cmd_setconfig,
    "/getconfig":     cmd_getconfig,
    "/forecast":      cmd_forecast,
    "/universe":      cmd_universe,
    "/addcoin":       cmd_addcoin,
    "/removecoin":    cmd_removecoin,
}

async def polling() -> None:
    offset = _load_offset()
    log.info("Telegram bot polling dimulai (offset=%d)...", offset)
    lines = [
        f"🤖 *AlgoTrader Intelligence Controller v{APP_VERSION} AKTIF!*",
        f"Engine: Intelligence Pipeline v{APP_VERSION}",
        "Ketik /help untuk daftar command.",
    ]
    await tg_send("\n".join(lines))

    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    API_URL + "/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as r:
                    data = await r.json()

                    if data.get("ok"):
                        for update in data.get("result", []):
                            new_offset = update["update_id"] + 1
                            if new_offset > offset:
                                offset = new_offset
                                _save_offset(offset)
                            await handle_update(update)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Polling error: %s", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    # Windows console can default to cp1252 and crash on emoji output.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # FIX: hanya setup logging kalau belum ada handler (hindari double log)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

    if not TOKEN or len(TOKEN) < 10 or TOKEN == "your_bot_token_here":
        print(
            "❌ TELEGRAM_BOT_TOKEN tidak valid atau belum diset!\n"
            "   Edit .env dan isi: TELEGRAM_BOT_TOKEN=<token_dari_botfather>\n"
            "   Dapatkan token dari @BotFather di Telegram."
        )
        sys.exit(1)

    if not CHAT_ID:
        print(
            "❌ TELEGRAM_CHAT_ID tidak diset!\n"
            "   Edit .env dan isi: TELEGRAM_CHAT_ID=<chat_id_kamu>"
        )
        sys.exit(1)

    print(f"✅ Telegram Intelligence Controller v{APP_VERSION} starting...")
    print(f"   Token  : {TOKEN[:10]}...{TOKEN[-4:]}")
    print(f"   Chat ID: {CHAT_ID}")
    print(f"   BOT_DIR: {BOT_DIR}")

    asyncio.run(polling())