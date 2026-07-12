#!/bin/bash
# AlgoTrader Pro Futures — Stop Script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

function kill_process() {
    local pid_file=$1
    local pattern=$2
    local name=$3

    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" && echo "✅ $name stopped (PID: $PID)"
        fi
        rm -f "$pid_file"
    fi

    EXTRA_PID=$(pgrep -f "$pattern" | head -1)
    if [ -n "$EXTRA_PID" ]; then
        kill "$EXTRA_PID" && echo "✅ Cleaned up $name (PID: $EXTRA_PID)"
    fi
}

echo "🛑 Stopping AlgoTrader Pro Futures..."
kill_process "$SCRIPT_DIR/.bot_future_pid" "python.*/root/algotrader/future/main_future.py" "Bot Futures"
echo "Done."
