#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tail -f "$SCRIPT_DIR/logs/trading_bot.log"
