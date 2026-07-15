#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

[ -f "$SCRIPT_DIR/venv/bin/activate" ] && source "$SCRIPT_DIR/venv/bin/activate"

BOT_PID=$(pgrep -f "python3 -m spot.main_spot" | head -1)
TG_PID=$(pgrep -f "python3 -m shared_service.telegram_bot" | head -1)
BOT_FUT_PID=$(pgrep -f "python3 -m future.main_future" | head -1)

echo "=== System Process ==="
[ -n "$BOT_PID" ] && echo "✅ Core Bot (spot)   : RUNNING (PID: $BOT_PID)" || echo "❌ Core Bot (spot)   : OFFLINE"
[ -n "$TG_PID" ]  && echo "✅ Telegram          : RUNNING (PID: $TG_PID)" || echo "❌ Telegram          : OFFLINE"
[ -n "$BOT_FUT_PID" ] && echo "✅ Bot Futures       : RUNNING (PID: $BOT_FUT_PID)" || echo "⬜ Bot Futures       : OFFLINE (jalankan: bash start_future.sh)"

if [ -n "$BOT_PID" ]; then
    echo ""
    echo "=== Status API Spot (FastAPI, port 8000) ==="
    _KEY=$(grep -oP '(?<=^DASHBOARD_API_KEY=).*' "$SCRIPT_DIR/.env"); curl -s -H "X-API-Key: $_KEY" http://127.0.0.1:8000/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"Status  : {d.get('status','?')}\")
    print(f\"Halted  : {d.get('halted','?')}\")
    print(f\"Uptime  : {d.get('uptime_display','?')}\")
    print(f\"Strategy: {d.get('strategy','?')}\")
    print(f\"Mode    : {'TESTNET' if d.get('testnet') else 'LIVE'}\")
except:
    print('API belum siap/merespon...')
" 2>/dev/null || echo "API tidak terjangkau."
    echo ""
    echo "=== Portfolio Summary (Spot) ==="
    curl -s -H "X-API-Key: $_KEY" http://127.0.0.1:8000/api/balance 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"Equity  : \${d.get('total_equity',0):.2f}\")
    print(f\"Free    : \${d.get('free_balance',0):.2f}\")
    print(f\"Daily   : {d.get('daily_pnl_pct',0):+.2f}%\")
    print(f\"Drawdown: {d.get('drawdown_pct',0):.2f}%\")
except:
    print('Gagal mengambil data balance.')
" 2>/dev/null || echo "Data balance tidak tersedia."
fi

if [ -n "$BOT_FUT_PID" ]; then
    echo ""
    echo "=== Status API Futures (FastAPI, port 8001) ==="
    _FUT_API_KEY=$(grep -oP '(?<=^DASHBOARD_API_KEY_FUTURES=).*' "$SCRIPT_DIR/.env" 2>/dev/null || grep -oP '(?<=^DASHBOARD_API_KEY=).*' "$SCRIPT_DIR/.env" 2>/dev/null)
    curl -s http://127.0.0.1:8001/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"Status  : {d.get('status','?')}\")
    print(f\"Halted  : {d.get('halted','?')}\")
    print(f\"Uptime  : {d.get('uptime_display','?')}\")
    print(f\"Leverage: {d.get('default_leverage','?')}x  Margin: {d.get('margin_mode','?')}\")
    print(f\"Mode    : {'TESTNET' if d.get('testnet') else 'LIVE'}\")
except:
    print('API belum siap/merespon...')
" 2>/dev/null || echo "API tidak terjangkau."
    echo ""
    echo "=== Portfolio Summary (Futures) ==="
    curl -s -H "X-API-Key: $_FUT_API_KEY" http://127.0.0.1:8001/api/balance 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"Equity      : \${d.get('total_equity',0):.2f}\")
    print(f\"Free Margin : \${d.get('free_margin',0):.2f}\")
    print(f\"Used Margin : \${d.get('used_margin',0):.2f}\")
    print(f\"Unrealized  : \${d.get('unrealized_pnl',0):+.2f}\")
    print(f\"Daily       : {d.get('daily_pnl_pct',0):+.2f}%\")
    print(f\"Drawdown    : {d.get('drawdown_pct',0):.2f}%\")
except:
    print('Gagal mengambil data balance (cek DASHBOARD_API_KEY_FUTURES di .env).')
" 2>/dev/null || echo "Data balance tidak tersedia."
fi

