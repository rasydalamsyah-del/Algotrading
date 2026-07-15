#!/data/data/com.termux/files/usr/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
echo "🔄 Menjalankan ulang installer..."
bash "$SCRIPT_DIR/install_termux.sh"
