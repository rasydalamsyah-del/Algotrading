#!/bin/bash
# AlgoTrader Pro v7.0 — Start Script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERR: gagal cd ke $SCRIPT_DIR"; exit 1; }

echo "🚀 Starting AlgoTrader Pro v7.0..."

if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    echo "❌ venv tidak ditemukan di $SCRIPT_DIR/venv"
    echo "   Jalankan ulang: bash install_termux.sh"
    exit 1
fi
source "$SCRIPT_DIR/venv/bin/activate"
echo "✔ venv aktif: $VIRTUAL_ENV"

if grep -q "ISI_API_KEY_KAMU_DISINI" "$SCRIPT_DIR/.env" 2>/dev/null; then
    echo ""
    echo "⚠️  Peringatan: API_KEY belum diisi di .env!"
    echo "   Edit dulu: nano $SCRIPT_DIR/.env"
    echo ""
    read -r -p "   Lanjutkan tetap? (y/N): " _CONFIRM
    [[ "$_CONFIRM" != "y" && "$_CONFIRM" != "Y" ]] && { echo "Dibatalkan."; exit 1; }
fi

mkdir -p "$SCRIPT_DIR/logs"

# Jalankan Core Bot dengan nohup biar nggak mati pas session ditutup
nohup python "$SCRIPT_DIR/spot/main_spot.py" >> "$SCRIPT_DIR/logs/trading_bot.log" 2>&1 &
BOT_PID=$!
echo $BOT_PID > "$SCRIPT_DIR/.bot_pid"

# Jalankan Telegram Bot dengan nohup
nohup python "$SCRIPT_DIR/shared_service/telegram_bot.py" >> "$SCRIPT_DIR/logs/telegram_bot.log" 2>&1 &
TG_PID=$!
echo $TG_PID > "$SCRIPT_DIR/.tg_pid"

echo "✅ Telegram Bot started (nohup)! PID: $TG_PID"
echo "✅ Bot started (nohup)! PID: $BOT_PID"
echo "📋 Log    : bash $SCRIPT_DIR/view_log.sh"
echo "📊 Status : bash $SCRIPT_DIR/status.sh"
echo "🌐 Dashboard: http://127.0.0.1:8000"

