"""
notifications.py
AlgoTrader Pro v7.0

"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Tuple

import aiohttp

from constants import APP_VERSION

log = logging.getLogger("notifications")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)



class WhaleNotifier:
    """
    Notifikasi whale dengan auto-delete dinamis.
    Banyak whale → hapus cepat (1 menit)
    Sedikit whale → hapus lambat (5 menit)
    """
    def __init__(self, tg_token: str, tg_chat_id: str):
        self._token   = tg_token
        self._chat_id = tg_chat_id
        self._whale_events: List[float] = []  # timestamp whale events
        self._pending_deletes: List[Tuple[int, float]] = []  # (message_id, delete_at)
        self._lock = asyncio.Lock()

    def _count_recent_whales(self) -> int:
        now = _utcnow().timestamp()
        self._whale_events = [t for t in self._whale_events if now - t <= 300]
        return len(self._whale_events)

    def _get_delete_delay(self, whale_count: int) -> float:
        if whale_count > 3:
            return 60.0   # 1 menit kalau banyak whale
        return 300.0      # 5 menit kalau sedikit whale

    async def _send_tg(self, text: str) -> Optional[int]:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data.get("result", {}).get("message_id")
        except Exception as e:
            log.debug("WhaleNotifier send error: %s", e)
        return None

    async def _delete_tg(self, message_id: int) -> None:
        url = f"https://api.telegram.org/bot{self._token}/deleteMessage"
        payload = {"chat_id": self._chat_id, "message_id": message_id}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.debug("WhaleNotifier delete error: %s", await r.text())
        except Exception as e:
            log.debug("WhaleNotifier delete error: %s", e)

    async def _process_pending_deletes(self) -> None:
        now = _utcnow().timestamp()
        async with self._lock:
            still_pending = []
            for msg_id, delete_at in self._pending_deletes:
                if now >= delete_at:
                    asyncio.ensure_future(self._delete_tg(msg_id))
                else:
                    still_pending.append((msg_id, delete_at))
            self._pending_deletes = still_pending

    async def start_delete_loop(self) -> None:
        """Background loop yang cek dan hapus pesan expired setiap 10 detik."""
        while True:
            try:
                await self._process_pending_deletes()
            except Exception as e:
                log.debug("WhaleNotifier delete loop error: %s", e)
            await asyncio.sleep(10)

    async def notify_whale(
        self,
        symbol:     str,
        direction:  str,  # "SELL" atau "BUY"
        ratio:      float,
        confidence: float,
        mode:       str = "LIVE",
    ) -> None:
        if direction != "BUY":
            return  # hanya notif whale BUY

        async with self._lock:
            now = _utcnow().timestamp()
            self._whale_events.append(now)
            whale_count = self._count_recent_whales()
            delay = self._get_delete_delay(whale_count)

            # Percepat semua pending deletes kalau banyak whale
            if whale_count > 3:
                accelerated = []
                for msg_id, delete_at in self._pending_deletes:
                    new_delete_at = min(delete_at, now + 60.0)
                    accelerated.append((msg_id, new_delete_at))
                self._pending_deletes = accelerated

        emoji = "🔴" if mode == "LIVE" else "🟡"
        text = (
            f"🐋 *Whale BUY* | `{symbol}`\n"
            f"ratio: `{ratio:.4f}` | conf: `{confidence:.0%}`\n"
            f"_{emoji} {mode} | "
            f"{'⚠️ Banyak whale aktif!' if whale_count > 3 else 'Normal'}_"
        )

        msg_id = await self._send_tg(text)
        if msg_id:
            delete_at = _utcnow().timestamp() + delay
            async with self._lock:
                self._pending_deletes.append((msg_id, delete_at))

        # Proses pending deletes
        await self._process_pending_deletes()

class NotificationManager:
    def __init__(self, config: dict):
        self._tg_enabled = bool(config.get("telegram_enabled", False))
        self._tg_token   = config.get("telegram_bot_token", "")
        self._tg_chat_id = config.get("telegram_chat_id", "")
        
        if self._tg_enabled:
            if not self._tg_token or len(self._tg_token) < 10:
                log.error(
                    "TELEGRAM_BOT_TOKEN tidak valid atau kosong! "
                    "Notifikasi Telegram dinonaktifkan. "
                    "Set TELEGRAM_BOT_TOKEN=<token> di .env"
                )
                self._tg_enabled = False
            elif not self._tg_chat_id:
                log.error(
                    "TELEGRAM_CHAT_ID tidak diset! "
                    "Notifikasi Telegram dinonaktifkan."
                )
                self._tg_enabled = False

        self._email_enabled = bool(config.get("email_enabled", False))
        self._smtp_host     = config.get("smtp_host", "smtp.gmail.com")
        self._smtp_port     = int(config.get("smtp_port", 587))
        self._smtp_user     = config.get("smtp_user", "")
        self._smtp_password = config.get("smtp_password", "")
        self._email_from    = config.get("email_from", self._smtp_user)
        self._email_to      = config.get("email_to", "")

        self._mode     = "🟡 TESTNET" if config.get("testnet", True) else "🔴 LIVE"
        self._exchange = config.get("exchange_id", "binance").upper()
        self._whale_notifier = WhaleNotifier(self._tg_token, self._tg_chat_id)

    def _update_config(self, config: dict) -> None:
        """Hot-reload konfigurasi Telegram dari config terbaru."""
        self._tg_enabled = bool(config.get("telegram_enabled", False))
        self._tg_token   = config.get("telegram_bot_token", self._tg_token)
        self._tg_chat_id = config.get("telegram_chat_id", self._tg_chat_id)
        self._whale_notifier = WhaleNotifier(self._tg_token, self._tg_chat_id)
        log.info(
            "NotificationManager config updated | Telegram: %s chat_id=%s",
            "ENABLED" if self._tg_enabled else "DISABLED",
            self._tg_chat_id,
        )

        if self._tg_enabled:
            log.info(
                "Telegram notifications: ENABLED (chat_id=%s)", self._tg_chat_id
            )
        else:
            log.info("Telegram notifications: DISABLED")

        if self._email_enabled and self._smtp_user and self._email_to:
            log.info(
                "Email notifications: ENABLED (%s → %s)",
                self._smtp_user, self._email_to,
            )
        else:
            log.info("Email notifications: DISABLED")

    async def notify_trade_opened(
        self,
        symbol:        str,
        side:          str,
        entry_price:   float,
        amount:        float,
        stop_loss:     Optional[float],
        take_profit:   Optional[float],
        atr:           Optional[float],
        confidence:    float = 0.0,
        coin_profile:  str   = "",
        exit_mode:     str   = "",
        adaptive_mode: str   = "",
    ) -> None:
        sl_str   = f"${stop_loss:.6f}"   if stop_loss   else "—"
        tp_str   = f"${take_profit:.6f}" if take_profit  else "—"

        if (
            stop_loss is not None and take_profit is not None
            and stop_loss > 0 and entry_price > 0
            and stop_loss != entry_price
        ):
            risk_pct = abs(entry_price - stop_loss) / entry_price * 100
            rr       = abs(take_profit - entry_price) / abs(entry_price - stop_loss)
        else:
            risk_pct = 0.0
            rr       = 0.0

        profile_line  = f"🧬 Profile : `{coin_profile}`\n"   if coin_profile else ""
        exit_line     = f"🚦 ExitMode: `{exit_mode}`\n"      if exit_mode    else ""
        adaptive_line = (
            f"⚙️ Adaptive: `{adaptive_mode}`\n"
            if adaptive_mode and adaptive_mode not in ("N/A", "NORMAL", "")
            else ""
        )

        tg_msg = (
            f"📈 *TRADE OPENED* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Symbol : `{symbol}`\n"
            f"📊 Side   : `{side.upper()}`\n"
            f"💰 Entry  : `${entry_price:.6f}`\n"
            f"📦 Amount : `{amount:.6f}`\n"
            f"💵 Value  : `${entry_price * amount:.2f}`\n"
            f"🛑 SL     : `{sl_str}`\n"
            f"🎯 TP     : `{tp_str}`\n"
            f"⚖️ Risk   : `{risk_pct:.2f}%` | R/R: `{rr:.2f}`\n"
            + (f"🤖 ATR    : `{atr:.6f}`\n" if atr else "")
            + f"🎲 Conf   : `{confidence:.1%}`\n"
            + profile_line + exit_line + adaptive_line
            + f"🕐 Time   : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )

        email_subject = (
            f"[AlgoTrader] Trade Opened: {symbol} {side.upper()} @ ${entry_price:.4f}"
            + (f" [{coin_profile}]" if coin_profile else "")
        )
        email_html = self._html_trade_opened(
            symbol, side, entry_price, amount, stop_loss, take_profit,
            atr, confidence, coin_profile, exit_mode, adaptive_mode,
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_trade_closed(
        self,
        symbol:       str,
        side:         str,
        entry_price:  float,
        exit_price:   float,
        amount:       float,
        realized_pnl: float,
        reason:       str,
    ) -> None:
        # [BUG-FIX] Sebelumnya: pnl_pct dihitung dari (exit-entry)/entry*100 —
        # yaitu persentase pergerakan HARGA mentah, tanpa fee. Ini tidak
        # konsisten dengan realized_pnl yang sudah memperhitungkan fee (entry
        # fee + exit fee). User bisa melihat pnl_pct positif tapi realized_pnl
        # negatif (karena fee) atau sebaliknya — membingungkan dan menyesatkan.
        # Sekarang: hitung pnl_pct dari realized_pnl dibagi position value
        # (entry_price * amount), konsisten dengan angka PnL yang ditampilkan.
        # Fallback ke 0.0 kalau position_value = 0 (entry_price atau amount = 0).
        position_value = entry_price * amount
        pnl_pct = (realized_pnl / position_value * 100) if position_value > 0 else 0.0

        emoji    = "✅" if realized_pnl >= 0 else "❌"
        pnl_sign = "+" if realized_pnl >= 0 else ""

        tg_msg = (
            f"{emoji} *TRADE CLOSED* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Symbol: `{symbol}`\n"
            f"📊 Side  : `{side.upper()}`\n"
            f"📥 Entry : `${entry_price:.6f}`\n"
            f"📤 Exit  : `${exit_price:.6f}`\n"
            f"📦 Amount: `{amount:.6f}`\n"
            f"💰 PnL   : `{pnl_sign}${realized_pnl:.4f}` "
            f"(`{pnl_sign}{pnl_pct:.2f}%`)\n"
            f"📝 Reason: `{reason}`\n"
            f"🕐 Time  : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )

        email_subject = (
            f"[AlgoTrader] Trade Closed: {symbol} "
            f"{pnl_sign}${realized_pnl:.4f} ({pnl_sign}{pnl_pct:.2f}%)"
        )
        email_html = self._html_trade_closed(
            symbol, side, entry_price, exit_price, amount,
            realized_pnl, pnl_pct, reason,
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_sl_tp_hit(
        self,
        symbol:      str,
        trigger:     str,
        price:       float,
        entry_price: float,
        pnl:         float,
    ) -> None:
        emoji    = "🛑" if trigger == "stop_loss" else "🎯"
        label    = "STOP LOSS HIT" if trigger == "stop_loss" else "TAKE PROFIT HIT"
        pnl_sign = "+" if pnl >= 0 else ""

        tg_msg = (
            f"{emoji} *{label}* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Symbol : `{symbol}`\n"
            f"💵 Price  : `${price:.6f}`\n"
            f"📥 Entry  : `${entry_price:.6f}`\n"
            f"💰 Est PnL: `{pnl_sign}${pnl:.4f}`\n"
            f"🕐 Time   : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        email_subject = f"[AlgoTrader] {label}: {symbol} @ ${price:.4f}"
        email_html    = (
            f"<p>{emoji} <b>{label}</b><br>"
            f"Symbol: {symbol}<br>"
            f"Price: ${price:.6f}<br>"
            f"Entry: ${entry_price:.6f}<br>"
            f"Est. PnL: {pnl_sign}${pnl:.4f}</p>"
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_bot_halted(self, reason: str, detail: str = "") -> None:
        tg_msg = (
            f"⚠️ *BOT HALTED* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📛 Reason: `{reason}`\n"
            f"📝 Detail: `{detail or '—'}`\n"
            f"🕐 Time  : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`\n\n"
            f"_Login ke dashboard untuk review dan resume._"
        )
        email_subject = f"[AlgoTrader] ⚠️ BOT HALTED: {reason}"
        email_html    = (
            f"<p>⚠️ <b>BOT HALTED</b><br>"
            f"Reason: {reason}<br>"
            f"Detail: {detail or '—'}<br>"
            f"Time: {_utcnow()} UTC</p>"
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_bot_resumed(self) -> None:
        tg_msg = (
            f"▶️ *BOT RESUMED* [{self._mode}]\n"
            f"Trading telah dilanjutkan.\n"
            f"🕐 Time: `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        email_subject = "[AlgoTrader] ▶️ Bot Resumed"
        email_html    = (
            f"<p>▶️ <b>BOT RESUMED</b><br>"
            f"Trading dilanjutkan pada {_utcnow()} UTC</p>"
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_panic(
        self,
        positions_found: int,
        closed_count:    int,
        failed_symbols:  List[str],
    ) -> None:
        failed_str = ", ".join(failed_symbols) if failed_symbols else "tidak ada"
        tg_msg = (
            f"🆘 *PANIC BUTTON TRIGGERED* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Posisi ditemukan : `{positions_found}`\n"
            f"✅ Berhasil ditutup : `{closed_count}`\n"
            f"❌ Gagal            : `{failed_str}`\n"
            f"🔒 Bot              : `HALTED`\n"
            f"🕐 Time             : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        email_subject = "[AlgoTrader] 🆘 PANIC BUTTON TRIGGERED"
        email_html    = (
            f"<p>🆘 <b>PANIC BUTTON TRIGGERED</b><br>"
            f"Posisi ditemukan: {positions_found}<br>"
            f"Berhasil ditutup: {closed_count}<br>"
            f"Gagal: {failed_str}<br>"
            f"Bot: HALTED<br>"
            f"Time: {_utcnow()} UTC</p>"
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_daily_summary(
        self,
        total_equity:  float,
        daily_pnl:     float,
        daily_pnl_pct: float,
        total_trades:  int,
        win_rate:      float,
        drawdown_pct:  float,
    ) -> None:
        emoji = "📈" if daily_pnl >= 0 else "📉"
        sign  = "+" if daily_pnl >= 0 else ""

        tg_msg = (
            f"{emoji} *DAILY SUMMARY* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Equity       : `${total_equity:.2f}`\n"
            f"📊 Daily PnL    : `{sign}${daily_pnl:.4f}` "
            f"(`{sign}{daily_pnl_pct:.2f}%`)\n"
            f"🏆 Win Rate     : `{win_rate:.1f}%`\n"
            f"📝 Trades Today : `{total_trades}`\n"
            f"📉 Drawdown     : `{drawdown_pct:.2f}%`\n"
            f"🕐 `{_utcnow().strftime('%Y-%m-%d')} UTC`"
        )
        email_subject = (
            f"[AlgoTrader] Daily Summary: "
            f"{sign}${daily_pnl:.4f} ({sign}{daily_pnl_pct:.2f}%)"
        )
        email_html = self._html_daily_summary(
            total_equity, daily_pnl, daily_pnl_pct,
            total_trades, win_rate, drawdown_pct,
        )
        await self._send(tg_msg, email_subject, email_html)

    async def notify_error(self, context: str, error: str) -> None:
        tg_msg = (
            f"🔴 *CRITICAL ERROR* [{self._mode}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Context: `{context}`\n"
            f"💬 Error  : `{error[:300]}`\n"
            f"🕐 Time   : `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        email_subject = f"[AlgoTrader] 🔴 Critical Error: {context}"
        email_html    = (
            f"<p>🔴 <b>CRITICAL ERROR</b><br>"
            f"Context: {context}<br>"
            f"Error: {error}<br>"
            f"Time: {_utcnow()} UTC</p>"
        )
        await self._send(tg_msg, email_subject, email_html)


    async def notify_info(self, msg: str) -> None:
        tg_msg        = f"ℹ️ *Info* [{self._mode}]\n{msg}"
        email_subject = f"[AlgoTrader] ℹ️ Info"
        email_html    = f"<p>ℹ️ <b>Info</b><br>{msg}</p>"
        await self._send(tg_msg, email_subject, email_html)

    async def notify_whale(
        self,
        symbol:     str,
        direction:  str,
        ratio:      float,
        confidence: float = 0.0,
        mode:       str   = "LIVE",
    ) -> None:
        """Wrapper publik untuk WhaleNotifier.notify_whale()."""
        # [BUG-FIX] Sebelumnya: ada guard 'if self._whale_notifier is None: return'
        # yang dead code — self._whale_notifier SELALU diinisialisasi di __init__
        # dan _update_config, tidak pernah None. Guard ini hanya memberi kesan
        # palsu bahwa ada kondisi di mana _whale_notifier bisa None, padahal tidak.
        # Dihapus untuk kejelasan kode. Kalau telegram tidak enabled, WhaleNotifier
        # tetap dibuat tapi _send_tg-nya akan gagal gracefully (silent).
        await self._whale_notifier.notify_whale(
            symbol     = symbol,
            direction  = direction,
            ratio      = ratio,
            confidence = confidence,
            mode       = mode,
        )

    async def notify_regime_change(
        self,
        symbol:     str,
        old_regime: str,
        new_regime: str,
        confidence: float = 0.0,
    ) -> None:
        regime_emoji = {
            "trending_bull":      "📈",
            "trending_bear":      "📉",
            "ranging":            "↔️",
            "volatile_expansion": "⚡",
            "undefined":          "❓",
        }
        old_emoji = regime_emoji.get(old_regime, "❓")
        new_emoji = regime_emoji.get(new_regime, "❓")
        text = (
            f"🔄 *Regime Change* | `{symbol}`\n"
            f"{old_emoji} `{old_regime}` \u2192 {new_emoji} `{new_regime}`\n"
            f"Confidence: `{confidence:.0%}`\n"
            f"_{self._mode} | {self._exchange}_"
        )
        await self._send(tg_msg=text, email_subject=f"Regime Change {symbol}", email_html=f"<pre>{text}</pre>")


    async def notify_projection(
        self,
        symbol:        str,
        side:          str,
        entry_price:   float,
        amount:        float,
        stop_loss:     Optional[float],
        take_profit:   Optional[float],
        atr:           Optional[float],
        confidence:    float = 0.0,
        total_score:   float = 0.0,
        score_breakdown: Optional[dict] = None,
        regime:        str = "",
        narrative:     str = "",
    ) -> None:
        """
        Proyeksi trade berbasis gabungan semua indikator.
        Dikirim tepat setelah notify_trade_opened.
        """
        if not self._tg_enabled:
            return

        # ── Kalkulasi proyeksi ─────────────────────────────────────────────────
        sl    = stop_loss   or 0.0
        tp    = take_profit or 0.0
        atr_v = atr         or 0.0

        # Price movement projection
        tp_pct = ((tp - entry_price) / entry_price * 100) if tp > 0 and entry_price > 0 else 0.0
        sl_pct = ((entry_price - sl) / entry_price * 100) if sl > 0 and entry_price > 0 else 0.0
        rr     = (tp_pct / sl_pct) if sl_pct > 0 else 0.0

        # Profit simulation dari nilai posisi aktual
        position_value = entry_price * amount
        est_profit = position_value * (tp_pct / 100) if tp_pct > 0 else 0.0
        est_loss   = position_value * (sl_pct / 100) if sl_pct > 0 else 0.0

        # Time window — dari ATR: makin besar ATR relatif ke harga, makin cepat bergerak
        atr_pct = (atr_v / entry_price * 100) if atr_v > 0 and entry_price > 0 else 0.0
        if atr_pct >= 2.0:
            time_min, time_max, time_unit = 1, 4, "jam"
        elif atr_pct >= 1.0:
            time_min, time_max, time_unit = 2, 8, "jam"
        elif atr_pct >= 0.5:
            time_min, time_max, time_unit = 4, 16, "jam"
        else:
            time_min, time_max, time_unit = 8, 48, "jam"

        # Confidence bar visual
        conf_pct  = confidence * 100
        bar_fill  = int(conf_pct / 10)
        conf_bar  = "█" * bar_fill + "░" * (10 - bar_fill)

        # Score bar visual
        score_fill = int(total_score / 10)
        score_bar  = "█" * score_fill + "░" * (10 - score_fill)

        # Signal strength label
        if total_score >= 85:
            strength_label = "🔥 SANGAT KUAT"
        elif total_score >= 75:
            strength_label = "💪 KUAT"
        elif total_score >= 65:
            strength_label = "✅ MODERAT"
        else:
            strength_label = "⚠️ LEMAH"

        # Regime label
        regime_map = {
            "trending_bull":  "📈 Trending Bull",
            "trending_bear":  "📉 Trending Bear",
            "ranging":        "↔️ Ranging",
            "volatile":       "⚡ Volatile",
            "breakout":       "🚀 Breakout",
            "undefined":      "❓ Undefined",
        }
        regime_label = regime_map.get(regime.lower(), f"📊 {regime}") if regime else "❓ Undefined"

        # ── Score breakdown ────────────────────────────────────────────────────
        breakdown_lines = ""
        if score_breakdown:
            cat_emoji = {
                "trend":      "📐",
                "momentum":   "⚡",
                "strength":   "💪",
                "volatility": "🌊",
                "pattern":    "🕯️",
                "oscillator": "🔄",
                "structure":  "🏗️",
                "orderbook":  "📖",
            }
            lines = []
            for cat, data in score_breakdown.items():
                if cat == "regime_modifier" or cat == "total":
                    continue
                if not isinstance(data, dict):
                    continue
                raw  = data.get("raw", 0.0)
                w    = data.get("weight", 0.0)
                if w <= 0:
                    continue
                bar_len  = int(raw / 10)
                mini_bar = "▓" * bar_len + "░" * (10 - bar_len)
                emoji    = cat_emoji.get(cat, "•")
                lines.append(
                    f"  {emoji} `{cat:<11}` `{mini_bar}` `{raw:5.1f}` ×`{w:.2f}`"
                )
            breakdown_lines = "\n".join(lines) if lines else ""

        # ── Narrative ringkas ──────────────────────────────────────────────────
        narr_short = ""
        if narrative:
            # Ambil max 2 kalimat pertama
            sentences = [s.strip() for s in narrative.replace("\n", ". ").split(".") if s.strip()]
            narr_short = ". ".join(sentences[:2]) + "." if sentences else ""
            if len(narr_short) > 200:
                narr_short = narr_short[:197] + "..."

        # ── Build pesan ────────────────────────────────────────────────────────
        lines_msg = [
            f"🔮 *PROYEKSI TRADE* — `{symbol}`",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📍 *Entry*   : `${entry_price:.6f}`",
            f"🎯 *Target*  : `${tp:.6f}` (`+{tp_pct:.2f}%`)" if tp > 0 else f"🎯 *Target*  : `—`",
            f"🛑 *Stop Loss*: `${sl:.6f}` (`-{sl_pct:.2f}%`)" if sl > 0 else f"🛑 *Stop Loss*: `—`",
            f"⚖️ *R/R*     : `1 : {rr:.2f}`" if rr > 0 else "",
            f"",
            f"💰 *Simulasi Posisi* (`${position_value:.2f}`)",
            f"  Est. Profit : `+${est_profit:.4f}` (`+{tp_pct:.2f}%`)" if est_profit > 0 else "",
            f"  Est. Loss   : `-${est_loss:.4f}` (`-{sl_pct:.2f}%`)"   if est_loss  > 0 else "",
            f"",
            f"⏱️ *Estimasi Waktu*",
            f"  Window : `{time_min}–{time_max} {time_unit}`",
            f"  Basis  : ATR `{atr_pct:.2f}%` dari harga",
            f"",
            f"📊 *Analisis Signal*",
            f"  Score    : `{score_bar}` `{total_score:.1f}/100` {strength_label}",
            f"  Conf     : `{conf_bar}` `{conf_pct:.1f}%`",
            f"  Regime   : {regime_label}",
            f"",
        ]

        if breakdown_lines:
            lines_msg += [
                f"🧩 *Breakdown Indikator*",
                breakdown_lines,
                f"",
            ]

        if narr_short:
            lines_msg += [
                f"📝 *Narasi*",
                f"_{narr_short}_",
                f"",
            ]

        lines_msg += [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"⚠️ _Proyeksi berbasis data & indikator, bukan jaminan._",
            f"🕐 `{_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`",
        ]

        # Bersihkan baris kosong di ujung, gabungkan
        tg_msg = "\n".join(line for line in lines_msg if line is not None)

        await self._send_telegram(tg_msg)

    async def _send(
        self, tg_msg: str, email_subject: str, email_html: str
    ) -> None:
        tasks = []
        if self._tg_enabled and self._tg_token and self._tg_chat_id:
            tasks.append(self._send_telegram(tg_msg))
        if self._email_enabled and self._smtp_user and self._email_to:
            tasks.append(self._send_email(email_subject, email_html))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {
            "chat_id":    self._tg_chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning(
                            "Telegram send gagal [%d]: %s",
                            resp.status, body[:200],
                        )
                    else:
                        log.debug("Telegram notifikasi terkirim.")
        except Exception as e:
            log.warning("Telegram send error: %s", e)

    async def _send_email(self, subject: str, html_body: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self._send_email_sync, subject, html_body
            )
        except Exception as e:
            log.warning("Email send error: %s", e)

    def _send_email_sync(self, subject: str, html_body: str) -> None:
        full_html = f"""
<html><body style="font-family:monospace;background:#0b0d12;color:#e2e6f0;padding:20px">
<div style="max-width:520px;margin:auto;background:#10141c;padding:20px;
            border-radius:8px;border:1px solid #1e2433">
<h3 style="color:#00d68f;margin-top:0">
  AlgoTrader Pro v{APP_VERSION} — {self._exchange} {self._mode}
</h3>
<hr style="border-color:#1e2433">
{html_body}
<hr style="border-color:#1e2433">
<p style="color:#3a4060;font-size:11px">
  AlgoTrader Pro v{APP_VERSION} · {_utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
</p>
</div></body></html>
"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self._email_from
        msg["To"]      = self._email_to
        msg.attach(MIMEText(full_html, "html"))
        
        SMTP_TIMEOUT = 15
        try:
            with smtplib.SMTP(
                self._smtp_host,
                self._smtp_port,
                timeout=SMTP_TIMEOUT,
            ) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(self._smtp_user, self._smtp_password)
                server.sendmail(self._email_from, self._email_to, msg.as_string())
            log.debug("Email notifikasi terkirim ke %s", self._email_to)
        except smtplib.SMTPAuthenticationError as e:
            log.error("SMTP authentication gagal: %s — periksa smtp_user/password", e)
        except smtplib.SMTPException as e:
            log.warning("SMTP error: %s", e)
        except TimeoutError:
            log.warning("SMTP timeout setelah %ds — server tidak merespons", SMTP_TIMEOUT)

    def _html_trade_opened(
        self,
        symbol:        str,
        side:          str,
        entry:         float,
        amount:        float,
        sl:            Optional[float],
        tp:            Optional[float],
        atr:           Optional[float],
        conf:          float,
        coin_profile:  str = "",
        exit_mode:     str = "",
        adaptive_mode: str = "",
    ) -> str:
        sl_str = f"${sl:.6f}" if sl else "—"
        tp_str = f"${tp:.6f}" if tp else "—"
        if (
            sl is not None and tp is not None
            and sl > 0 and entry > 0
            and sl != entry
        ):
            rr = abs(tp - entry) / abs(entry - sl)
        else:
            rr = 0.0

        atr_row = (
            f'<tr><td style="color:#7b84a0;padding:4px 0">ATR</td>'
            f'<td style="color:#e2e6f0">{atr:.6f}</td></tr>'
        ) if atr else ""

        profile_rows = ""
        if coin_profile:
            profile_rows += (
                f'<tr><td style="color:#7b84a0;padding:4px 0">Profile</td>'
                f'<td style="color:#a78bfa">{coin_profile}</td></tr>'
            )
        if exit_mode:
            em_color = "#00d68f" if "RIDE" in exit_mode.upper() else "#f0b429"
            profile_rows += (
                f'<tr><td style="color:#7b84a0;padding:4px 0">Exit Mode</td>'
                f'<td style="color:{em_color}">{exit_mode}</td></tr>'
            )
        if adaptive_mode and adaptive_mode not in ("N/A", "NORMAL", ""):
            profile_rows += (
                f'<tr><td style="color:#7b84a0;padding:4px 0">Adaptive</td>'
                f'<td style="color:#f0b429">{adaptive_mode}</td></tr>'
            )

        return f"""
<table style="width:100%;border-collapse:collapse">
<tr><td style="color:#7b84a0;padding:4px 0">Symbol</td>
    <td style="color:#e2e6f0"><b>{symbol}</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Side</td>
    <td style="color:#4589ff"><b>{side.upper()}</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Entry Price</td>
    <td style="color:#e2e6f0">${entry:.6f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Amount</td>
    <td style="color:#e2e6f0">{amount:.6f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Value</td>
    <td style="color:#e2e6f0">${entry * amount:.2f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Stop Loss</td>
    <td style="color:#f0455a">{sl_str}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Take Profit</td>
    <td style="color:#00d68f">{tp_str}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">R/R Ratio</td>
    <td style="color:#e2e6f0">{rr:.2f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Confidence</td>
    <td style="color:#e2e6f0">{conf:.1%}</td></tr>
{atr_row}
{profile_rows}
</table>"""

    def _html_trade_closed(
        self,
        symbol:     str,
        side:       str,
        entry:      float,
        exit_price: float,
        amount:     float,
        pnl:        float,
        pnl_pct:    float,
        reason:     str,
    ) -> str:
        color = "#00d68f" if pnl >= 0 else "#f0455a"
        sign  = "+" if pnl >= 0 else ""
        return f"""
<table style="width:100%;border-collapse:collapse">
<tr><td style="color:#7b84a0;padding:4px 0">Symbol</td>
    <td style="color:#e2e6f0"><b>{symbol}</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Side</td>
    <td style="color:#e2e6f0">{side.upper()}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Entry</td>
    <td style="color:#e2e6f0">${entry:.6f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Exit</td>
    <td style="color:#e2e6f0">${exit_price:.6f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Amount</td>
    <td style="color:#e2e6f0">{amount:.6f}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Realized PnL</td>
    <td style="color:{color}"><b>{sign}${pnl:.4f} ({sign}{pnl_pct:.2f}%)</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Reason</td>
    <td style="color:#f0b429">{reason}</td></tr>
</table>"""

    def _html_daily_summary(
        self,
        equity:   float,
        pnl:      float,
        pnl_pct:  float,
        trades:   int,
        win_rate: float,
        drawdown: float,
    ) -> str:
        color = "#00d68f" if pnl >= 0 else "#f0455a"
        sign  = "+" if pnl >= 0 else ""
        return f"""
<table style="width:100%;border-collapse:collapse">
<tr><td style="color:#7b84a0;padding:4px 0">Total Equity</td>
    <td style="color:#e2e6f0"><b>${equity:.2f}</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Daily PnL</td>
    <td style="color:{color}">
      <b>{sign}${pnl:.4f} ({sign}{pnl_pct:.2f}%)</b></td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Trades Today</td>
    <td style="color:#e2e6f0">{trades}</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Win Rate</td>
    <td style="color:#e2e6f0">{win_rate:.1f}%</td></tr>
<tr><td style="color:#7b84a0;padding:4px 0">Drawdown</td>
    <td style="color:#f0b429">{drawdown:.2f}%</td></tr>
</table>"""
