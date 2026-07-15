#!/bin/bash
# AlgoTrader Pro v7.0 — Start Script (system-wide python, no venv)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERR: gagal cd ke $SCRIPT_DIR"; exit 1; }
echo "🚀 Starting AlgoTrader Pro v7.0..."

if grep -q "ISI_API_KEY_KAMU_DISINI" "$SCRIPT_DIR/.env" 2>/dev/null; then
    echo ""
    echo "⚠️  Peringatan: API_KEY belum diisi di .env!"
    read -r -p "   Lanjutkan tetap? (y/N): " _CONFIRM
    [[ "$_CONFIRM" != "y" && "$_CONFIRM" != "Y" ]] && { echo "Dibatalkan."; exit 1; }
fi

mkdir -p "$SCRIPT_DIR/logs"

nohup python3 -m spot.main_spot >> "$SCRIPT_DIR/logs/trading_bot.log" 2>&1 &
BOT_PID=$!
echo $BOT_PID > "$SCRIPT_DIR/.bot_pid"

nohup python3 -m shared_service.telegram_bot >> "$SCRIPT_DIR/logs/telegram_bot.log" 2>&1 &
TG_PID=$!
echo $TG_PID > "$SCRIPT_DIR/.tg_pid"

echo "✅ Telegram Bot started (nohup)! PID: $TG_PID"
echo "✅ Bot started (nohup)! PID: $BOT_PID"
echo "📋 Log    : bash $SCRIPT_DIR/view_log.sh"
echo "📊 Status : bash $SCRIPT_DIR/status.sh"
echo "🌐 Dashboard: http://127.0.0.1:8000"
