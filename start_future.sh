#!/bin/bash
# AlgoTrader Pro Futures — Start Script
#
# [CATATAN PENTING] Script ini TERPISAH dari start.sh (spot) dengan SENGAJA.
# future/main_future.py belum sematang spot/main_spot.py -- belum pernah
# terhubung ke exchange sungguhan, cross margin/hedge mode belum didukung,
# formula liquidation masih approximate. Jangan jalankan bareng start.sh
# tanpa paham risikonya kalau live/testnet mode diaktifkan (bukan paper
# trading).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERR: gagal cd ke $SCRIPT_DIR"; exit 1; }

echo "🔮 Starting AlgoTrader Pro Futures..."
echo "⚠️  Pastikan kamu paham: future/ belum sematang spot/ -- lihat audit-notes.md"

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

if [ "$(grep -oP '(?<=^PAPER_TRADING_MODE=).*' "$SCRIPT_DIR/.env" 2>/dev/null | tr -d '[:space:]')" != "true" ]; then
    echo ""
    echo "⚠️⚠️⚠️  PAPER_TRADING_MODE bukan 'true' di .env! ⚠️⚠️⚠️"
    echo "   future/ belum pernah diverifikasi dengan exchange sungguhan."
    echo "   SANGAT disarankan pastikan PAPER_TRADING_MODE=true dulu."
    echo ""
    read -r -p "   Lanjutkan TANPA paper trading? (y/N): " _CONFIRM2
    [[ "$_CONFIRM2" != "y" && "$_CONFIRM2" != "Y" ]] && { echo "Dibatalkan (aman)."; exit 1; }
fi

mkdir -p "$SCRIPT_DIR/logs"

nohup python "$SCRIPT_DIR/future/main_future.py" >> "$SCRIPT_DIR/logs/trading_bot_futures.log" 2>&1 &
BOT_FUT_PID=$!
echo $BOT_FUT_PID > "$SCRIPT_DIR/.bot_future_pid"

echo "✅ Bot Futures started (nohup)! PID: $BOT_FUT_PID"
echo "📋 Log      : tail -f $SCRIPT_DIR/logs/trading_bot_futures.log"
echo "📊 Status   : bash $SCRIPT_DIR/status.sh"
echo "🌐 Dashboard: http://127.0.0.1:8001"
