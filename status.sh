#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

[ -f "$SCRIPT_DIR/venv/bin/activate" ] && source "$SCRIPT_DIR/venv/bin/activate"

BOT_PID=$(pgrep -f "python.*spot/main_spot.py" | head -1)
TG_PID=$(pgrep -f "python.*shared_service/telegram_bot.py" | head -1)

echo "=== System Process ==="
[ -n "$BOT_PID" ] && echo "✅ Core Bot : RUNNING (PID: $BOT_PID)" || echo "❌ Core Bot : OFFLINE"
[ -n "$TG_PID" ]  && echo "✅ Telegram : RUNNING (PID: $TG_PID)" || echo "❌ Telegram : OFFLINE"

if [ -n "$BOT_PID" ]; then
    echo ""
    echo "=== Status API (FastAPI) ==="
    curl -s http://127.0.0.1:8000/api/status 2>/dev/null | python -c "
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
    echo "=== Portfolio Summary ==="
    curl -s http://127.0.0.1:8000/api/balance 2>/dev/null | python -c "
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
