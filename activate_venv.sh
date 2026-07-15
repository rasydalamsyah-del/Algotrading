#!/data/data/com.termux/files/usr/bin/bash
# Gunakan: source activate_venv.sh  (bukan bash activate_venv.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"
echo "✔ venv aktif: $VIRTUAL_ENV"
echo "  Python : $(python --version)"
echo "  pip    : $(pip --version | cut -d' ' -f1-2)"
echo ""
echo "Untuk keluar dari venv: deactivate"
