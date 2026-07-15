#!/data/data/com.termux/files/usr/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# AlgoTrader Pro v7.0 — Termux Installer (WITH VENV — NO COMPILE STRATEGY)
#
# CARA PAKAI (IKUTI URUTAN INI):
#   1)  termux-setup-storage
#   2)  mkdir -p ~/algotrader && cd ~/algotrader
#   3)  cp -r /storage/emulated/0/algotrader/. ~/algotrader/
#   4)  ls -la ~/algotrader          ← verifikasi file sudah ada
#   5)  pkg update && pkg upgrade -y && pkg install python -y
#   6)  cd ~/algotrader && chmod +x install_termux.sh && bash install_termux.sh
#
# FILOSOFI NO-COMPILE:
#   1. pkg install DULU untuk semua binary berat (numpy, pandas, cryptography)
#   2. pkg update DIULANG setelah tur-repo dipasang agar index fresh
#   3. venv dibuat dengan --system-site-packages agar bisa pakai pkg packages
#   4. pip install hanya untuk pure-Python packages (tidak ada compile)
#   5. Fallback cerdas — tidak buang waktu coba compile dulu baru gagal
# ══════════════════════════════════════════════════════════════════════════════

# ── Warna ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

LOGFILE="install_log.txt"
PYTHON_MIN_MINOR=10
VENV_DIR="venv"

# ── Pastikan di direktori script ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[ERR] Gagal cd ke $SCRIPT_DIR"; exit 1; }
echo "" > "$LOGFILE"

# ── Fungsi log ─────────────────────────────────────────────────────────────────
log()  { echo -e "${GREEN}  ✔  ${NC}${BOLD}$1${NC}" | tee -a "$LOGFILE"; }
warn() { echo -e "${YELLOW}  ⚠  ${NC}$1" | tee -a "$LOGFILE"; }
err()  { echo -e "${RED}  ✘  ${NC}${BOLD}$1${NC}" | tee -a "$LOGFILE"; }
info() { echo -e "${DIM}${BLUE}  ›  ${NC}${DIM}$1${NC}" | tee -a "$LOGFILE"; }
ok()   { echo -e "${GREEN}  ✔  ${NC}$1" | tee -a "$LOGFILE"; }

step() {
    local msg="$1"
    local width=54
    local line; line=$(printf '─%.0s' $(seq 1 $width))
    echo "" | tee -a "$LOGFILE"
    echo -e "${MAGENTA}┌${line}┐${NC}" | tee -a "$LOGFILE"
    printf "${MAGENTA}│${NC}  ${BOLD}${WHITE}%-${width}s${NC}${MAGENTA}│${NC}\n" "$msg" | tee -a "$LOGFILE"
    echo -e "${MAGENTA}└${line}┘${NC}" | tee -a "$LOGFILE"
}

# ── Spinner ────────────────────────────────────────────────────────────────────
_cursor_hide() { printf "\033[?25l"; }
_cursor_show() { printf "\033[?25h"; }

spinner() {
    local pid=$1 msg="${2:-Loading...}"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0
    _cursor_hide
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${CYAN}  ${frames[$i]}  ${NC}${DIM}%-55s${NC}" "$msg"
        i=$(( (i+1) % ${#frames[@]} ))
        sleep 0.08
    done
    _cursor_show
    printf "\r\033[K"
}

run_bg() {
    local msg="$1"; shift
    "$@" >> "$LOGFILE" 2>&1 &
    spinner $! "$msg"
    wait $!
    return $?
}

# ── Banner ─────────────────────────────────────────────────────────────────────
clear
echo -e "${MAGENTA}"
echo "  ╔═══════════════════════════════════════════════════════╗"
echo "  ║                                                       ║"
echo "  ║    █████╗ ██╗      ██████╗  ██████╗                  ║"
echo "  ║   ██╔══██╗██║     ██╔════╝ ██╔═══██╗                 ║"
echo "  ║   ███████║██║     ██║  ███╗██║   ██║                 ║"
echo "  ║   ██╔══██║██║     ██║   ██║██║   ██║                 ║"
echo "  ║   ██║  ██║███████╗╚██████╔╝╚██████╔╝                 ║"
echo "  ║   ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝                  ║"
echo "  ║                                                       ║"
echo "  ║       TRADER PRO v7.0  ·  TERMUX EDITION             ║"
echo "  ║       VENV + NO-COMPILE INSTALLER  (v6.1-fixed)      ║"
echo "  ║                                                       ║"
echo "  ╚═══════════════════════════════════════════════════════╝"
echo -e "${NC}"
sleep 0.3

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 1 · Cek File Project & Storage"
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "  ${BOLD}Panduan setup awal (jalankan SEBELUM script ini):${NC}"
echo ""
echo -e "  ${CYAN}[1]${NC} Izinkan akses storage:"
echo -e "      ${DIM}termux-setup-storage${NC}"
echo ""
echo -e "  ${CYAN}[2]${NC} Buat & masuk folder project:"
echo -e "      ${DIM}mkdir -p ~/algotrader && cd ~/algotrader${NC}"
echo ""
echo -e "  ${CYAN}[3]${NC} Salin file dari storage Android:"
echo -e "      ${DIM}cp -r /storage/emulated/0/algotrader/. ~/algotrader/${NC}"
echo ""
echo -e "  ${CYAN}[4]${NC} Verifikasi file sudah ada:"
echo -e "      ${DIM}ls -la ~/algotrader${NC}"
echo ""
echo -e "  ${CYAN}[5]${NC} Install Python (satu kali):"
echo -e "      ${DIM}pkg update && pkg upgrade -y && pkg install python -y${NC}"
echo ""
echo -e "  ${CYAN}[6]${NC} Jalankan installer ini:"
echo -e "      ${DIM}cd ~/algotrader && chmod +x install_termux.sh && bash install_termux.sh${NC}"
echo ""

MISSING_FILES=()
for f in spot/main_spot.py; do
    [ ! -f "$SCRIPT_DIR/$f" ] && MISSING_FILES+=("$f")
done

if [ ${#MISSING_FILES[@]} -gt 0 ]; then
    warn "File berikut tidak ditemukan di $SCRIPT_DIR:"
    for f in "${MISSING_FILES[@]}"; do
        err "  → $f"
    done
    echo ""
    warn "Pastikan sudah salin semua file dari /storage/emulated/0/algotrader/ ke ~/algotrader/"
    warn "Jalankan: cp -r /storage/emulated/0/algotrader/. ~/algotrader/"
    echo ""
    read -r -p "  Lanjutkan tetap? (y/N): " CONFIRM
    [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]] && { echo "Installer dibatalkan."; exit 1; }
else
    log "Semua file project ditemukan ✔"
fi

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 2 · Deteksi Python & Versi"
# ══════════════════════════════════════════════════════════════════════════════

# Pastikan SYS_PY terdeteksi, exit jelas jika tidak ada
if command -v python3 &>/dev/null; then
    SYS_PY="python3"
elif command -v python &>/dev/null; then
    SYS_PY="python"
else
    err "Python tidak ditemukan! Jalankan: pkg install python"
    exit 1
fi

_PY_RAW=$($SYS_PY --version 2>&1)
PY_FULL=$(echo "$_PY_RAW" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
[ -z "$PY_FULL" ] && PY_FULL=$(echo "$_PY_RAW" | grep -oE '[0-9]+\.[0-9]+' | head -1)

PY_MAJOR=$(echo "$PY_FULL" | cut -d. -f1)
PY_MINOR=$(echo "$PY_FULL" | cut -d. -f2)
PY_VER="$PY_MAJOR.$PY_MINOR"

if [ -z "$PY_MAJOR" ] || [ -z "$PY_MINOR" ]; then
    err "Versi Python tidak terdeteksi! Jalankan: pkg install python"
    exit 1
fi

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt "$PYTHON_MIN_MINOR" ]; }; then
    err "Python $PY_VER terlalu lama (butuh >= 3.$PYTHON_MIN_MINOR)"
    err "Jalankan: pkg install python"
    exit 1
fi

ARCH=$(uname -m)  # aarch64 | armv7l | x86_64 | i686

case "$PY_MINOR" in
    10) PYTAG="cp310" ;;
    11) PYTAG="cp311" ;;
    12) PYTAG="cp312" ;;
    13) PYTAG="cp313" ;;
    *)  PYTAG="cp${PY_MAJOR}${PY_MINOR}" ;;
esac

echo ""
echo -e "  ${DIM}Python  : $PY_VER  ($SYS_PY)${NC}"
echo -e "  ${DIM}Arch    : $ARCH${NC}"
echo -e "  ${DIM}PyTag   : $PYTAG${NC}"
echo -e "  ${DIM}Dir     : $SCRIPT_DIR${NC}"
echo -e "  ${DIM}Venv    : $SCRIPT_DIR/$VENV_DIR${NC}"
echo -e "  ${DIM}Date    : $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo ""
log "Deteksi environment selesai"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 3 · Update & Repo Setup"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #1 & #12:
#   - pkg update PERTAMA kali (index awal)
#   - pasang tur-repo
#   - pkg update KEDUA kali setelah tur-repo terpasang → index fresh
#   - fallback TIDAK pakai pkg upgrade -y (terlalu lama dan tidak perlu)

info "Update package index (pertama)..."
run_bg "Mengupdate package index Termux..." pkg update -y \
    && log "Package index updated (pertama)" \
    || warn "Update ada warning, lanjut..."

info "Tambah TUR-Repo (binary Python packages tambahan)..."
run_bg "Memasang tur-repo..." pkg install -y tur-repo \
    && {
        log "tur-repo terpasang"
        # FIX BUG #1: refresh index SETELAH tur-repo terpasang
        info "Refresh package index setelah tur-repo aktif..."
        run_bg "pkg update (refresh index tur-repo)..." pkg update -y \
            && log "Package index refreshed — tur-repo packages sekarang tersedia ✔" \
            || warn "Refresh index ada warning, lanjut..."
    } \
    || {
        warn "tur-repo tidak tersedia di mirror ini"
        warn "Coba ganti mirror: termux-change-repo"
        warn "Lanjut tanpa tur-repo — numpy/pandas mungkin tidak tersedia via pkg"
    }

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 4 · Install System Packages (pkg)"
# ══════════════════════════════════════════════════════════════════════════════

SYS_PKGS=(
    git curl wget make clang binutils patchelf
    libxml2 libxslt libjpeg-turbo freetype
    openssl libffi
)

for p in "${SYS_PKGS[@]}"; do
    run_bg "Memasang system package: $p" pkg install -y "$p" \
        && log "$p OK" \
        || warn "$p skip (mungkin sudah ada)"
done

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 5 · Install Binary Python Packages via pkg (TANPA COMPILE)"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #2: pkg install dijalankan SETELAH pkg update kedua (index fresh)
# sehingga python-pandas dari tur-repo bisa ditemukan.
# cryptography diprioritaskan via pkg.
# Jika pkg gagal, fallback pip dilakukan di dalam venv pada STEP 12.

info "Install numpy via pkg (pre-compiled untuk Android)..."
run_bg "pkg install python-numpy..." pkg install -y python-numpy \
    && log "numpy via pkg OK" \
    || warn "numpy via pkg gagal — akan fallback ke pip di dalam venv"

info "Install pandas via pkg (pre-compiled untuk Android)..."
run_bg "pkg install python-pandas..." pkg install -y python-pandas \
    && log "pandas via pkg OK" \
    || warn "pandas via pkg gagal — akan fallback ke pip di dalam venv"

info "Install cryptography via pkg (tanpa compile C/Rust)..."
run_bg "pkg install python-cryptography..." pkg install -y python-cryptography \
    && log "cryptography via pkg OK" \
    || warn "cryptography via pkg gagal — akan fallback via pip di dalam venv (STEP 12)"

# Cek apakah berhasil masuk ke sistem Python
NUMPY_SYS=false
PANDAS_SYS=false
CRYPTO_SYS=false

$SYS_PY -c "import numpy" 2>/dev/null       && NUMPY_SYS=true
$SYS_PY -c "import pandas" 2>/dev/null      && PANDAS_SYS=true
$SYS_PY -c "import cryptography" 2>/dev/null && CRYPTO_SYS=true

echo ""
[ "$NUMPY_SYS"  = true ] && ok "numpy  (system) : tersedia ✔" || warn "numpy  (system) : belum tersedia, fallback ke pip"
[ "$PANDAS_SYS" = true ] && ok "pandas (system) : tersedia ✔" || warn "pandas (system) : belum tersedia, fallback ke pip"
[ "$CRYPTO_SYS" = true ] && ok "crypto (system) : tersedia ✔" || warn "crypto (system) : belum tersedia"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 6 · Buat Virtual Environment (venv)"
# ══════════════════════════════════════════════════════════════════════════════
#
# KUNCI: --system-site-packages
#   Venv bisa mengakses package sistem (numpy, pandas, cryptography dari pkg)
#   tanpa perlu compile ulang.
#
# FIX BUG #10: SYS_PY sudah divalidasi di STEP 2, tidak perlu cek ulang.
# Tapi VENV_PATH didefinisikan di sini (bukan global) untuk kejelasan.

VENV_PATH="$SCRIPT_DIR/$VENV_DIR"

if [ -d "$VENV_PATH" ]; then
    warn "venv sudah ada di: $VENV_PATH"
    read -r -p "  Hapus & buat ulang venv? (y/N): " REBUILD_VENV
    if [[ "$REBUILD_VENV" == "y" || "$REBUILD_VENV" == "Y" ]]; then
        info "Menghapus venv lama..."
        rm -rf "$VENV_PATH"
        run_bg "Membuat venv baru dengan --system-site-packages..." \
            $SYS_PY -m venv "$VENV_PATH" --system-site-packages \
            && log "venv dibuat ulang: $VENV_PATH" \
            || { err "Gagal membuat venv!"; exit 1; }
    else
        info "Menggunakan venv yang sudah ada"
    fi
else
    info "Membuat venv di: $VENV_PATH"
    info "Menggunakan --system-site-packages agar numpy/pandas/crypto dari pkg bisa diakses"
    run_bg "Membuat virtual environment..." \
        $SYS_PY -m venv "$VENV_PATH" --system-site-packages \
        && log "venv berhasil dibuat: $VENV_PATH" \
        || { err "Gagal membuat venv! Coba: pkg install python"; exit 1; }
fi

# ── Aktifkan venv ──────────────────────────────────────────────────────────────
source "$VENV_PATH/bin/activate" 2>/dev/null || {
    err "Gagal aktifkan venv!"
    exit 1
}

# Alias PY & PIP ke binary di dalam venv (eksplisit, tidak ambigu)
PY="$VENV_PATH/bin/python"
PIP="$VENV_PATH/bin/pip"

echo ""
echo -e "  ${DIM}Venv Python : $($PY --version 2>&1)${NC}"
echo -e "  ${DIM}Venv pip    : $($PIP --version 2>&1 | cut -d' ' -f1-2)${NC}"
echo -e "  ${DIM}Site-pkgs   : --system-site-packages (numpy/pandas/crypto dari pkg ✔)${NC}"
echo ""

# Verifikasi package sistem terlihat dari venv
NUMPY_OK=false
PANDAS_OK=false
CRYPTO_OK=false

$PY -c "import numpy" 2>/dev/null       && { NUMPY_OK=true;  ok "numpy  terlihat dari venv ✔ (via system-site-packages)"; } \
    || warn "numpy belum terlihat dari venv, akan diinstall via pip"
$PY -c "import pandas" 2>/dev/null      && { PANDAS_OK=true; ok "pandas terlihat dari venv ✔ (via system-site-packages)"; } \
    || warn "pandas belum terlihat dari venv, akan diinstall via pip"
$PY -c "import cryptography" 2>/dev/null && { CRYPTO_OK=true; ok "crypto  terlihat dari venv ✔ (via system-site-packages)"; } \
    || warn "crypto belum terlihat dari venv"

log "venv aktif: $VENV_PATH"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 7 · Upgrade pip & Tools di dalam venv"
# ══════════════════════════════════════════════════════════════════════════════

run_bg "Upgrade pip, setuptools, wheel (di dalam venv)..." \
    $PIP install --upgrade pip setuptools wheel \
    && log "pip tools upgraded di venv" \
    || warn "pip upgrade ada warning, lanjut..."

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 8 · Fallback numpy via pip (jika pkg gagal)"
# ══════════════════════════════════════════════════════════════════════════════

if [ "$NUMPY_OK" = false ]; then
    info "numpy belum tersedia di venv — install via pip --prefer-binary..."
    run_bg "pip install numpy --prefer-binary..." \
        $PIP install "numpy>=1.24.0,<2.0.0" --prefer-binary \
        && {
            $PY -c "import numpy" 2>/dev/null && { NUMPY_OK=true; log "numpy pip OK"; } \
            || warn "numpy pip install tapi import gagal"
        } || {
            warn "numpy pip gagal, coba versi 1.26.4..."
            run_bg "pip install numpy==1.26.4 --prefer-binary..." \
                $PIP install "numpy==1.26.4" --prefer-binary \
                && {
                    $PY -c "import numpy" 2>/dev/null && { NUMPY_OK=true; log "numpy 1.26.4 OK"; }
                } || err "numpy GAGAL di semua metode!"
        }
else
    info "numpy sudah OK dari pkg (via system-site-packages), skip pip"
fi

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 9 · Fallback pandas via pip (jika pkg gagal)"
# ══════════════════════════════════════════════════════════════════════════════

if [ "$PANDAS_OK" = false ]; then
    info "pandas belum tersedia di venv — install via pip --prefer-binary..."
    run_bg "pip install pandas --prefer-binary..." \
        $PIP install "pandas>=1.5.0" --prefer-binary \
        && {
            $PY -c "import pandas" 2>/dev/null && { PANDAS_OK=true; log "pandas pip OK"; } \
            || warn "pandas pip install tapi import gagal"
        } || {
            warn "pandas pip gagal, coba 1.5.3..."
            run_bg "pip install pandas==1.5.3 --prefer-binary..." \
                $PIP install "pandas==1.5.3" --prefer-binary \
                && {
                    $PY -c "import pandas" 2>/dev/null && { PANDAS_OK=true; log "pandas 1.5.3 OK"; }
                } || err "pandas GAGAL di semua metode!"
        }
else
    info "pandas sudah OK dari pkg (via system-site-packages), skip pip"
fi

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 10 · Install pydantic-core (Pre-compiled Android Wheel)"
# ══════════════════════════════════════════════════════════════════════════════
#
# pydantic-core membutuhkan Rust untuk compile dari source.
# Di Android/Termux, gunakan pre-compiled wheel dari:
#   https://github.com/Eutalix/android-pydantic-core
#
# FIX BUG #3: PYDANTIC_V2 diset dengan benar di semua jalur (M1-M7).
# Setiap jalur yang berhasil WAJIB set PYDANTIC_OK=true dan PYDANTIC_V2
# berdasarkan versi yang benar-benar terdeteksi dari $PY (venv), bukan $SYS_PY.

PYDANTIC_OK=false
PYDANTIC_V2=false

# Helper: cek versi pydantic dari venv dan set flag
_check_pydantic_version() {
    local VER VMAJ
    VER=$($PY -c "import pydantic; print(pydantic.__version__)" 2>/dev/null)
    VMAJ=$(echo "$VER" | cut -d. -f1)
    if [ -n "$VER" ]; then
        PYDANTIC_OK=true
        if [ "${VMAJ:-0}" -ge 2 ] 2>/dev/null; then
            PYDANTIC_V2=true
        else
            PYDANTIC_V2=false
        fi
        return 0
    fi
    return 1
}

# ── M1: Android pre-compiled index ────────────────────────────────────────────
info "[M1] pip install pydantic-core via Android pre-compiled index..."
run_bg "pydantic-core via Android index (eutalix)..." \
    $PIP install pydantic-core \
        --extra-index-url https://eutalix.github.io/android-pydantic-core/ \
        --prefer-binary \
    && {
        _check_pydantic_version \
            && log "pydantic-core OK (Android index)" \
            || warn "M1: install sukses tapi import gagal"
    } || warn "M1: Android index gagal, lanjut M2..."

# ── M2: pkg install python-pydantic ───────────────────────────────────────────
if [ "$PYDANTIC_OK" = false ]; then
    info "[M2] pkg install python-pydantic (akan terlihat via system-site-packages)..."
    run_bg "pkg install python-pydantic..." pkg install -y python-pydantic \
        && {
            # FIX BUG #3: cek dari $PY (venv) bukan $SYS_PY
            # karena venv pakai --system-site-packages, pkg package terlihat dari venv
            _check_pydantic_version \
                && log "pydantic OK via pkg (terlihat dari venv)" \
                || {
                    warn "M2: pkg install berhasil tapi import dari venv gagal"
                    warn "    Pastikan venv dibuat dengan --system-site-packages"
                }
        } || warn "M2: pkg python-pydantic gagal"
fi

# ── M3: pip --prefer-binary di dalam venv ─────────────────────────────────────
if [ "$PYDANTIC_OK" = false ]; then
    info "[M3] pip install pydantic>=2.5.0 --prefer-binary (di venv)..."
    run_bg "pip pydantic --prefer-binary..." \
        $PIP install "pydantic>=2.5.0" --prefer-binary \
        && {
            _check_pydantic_version \
                && log "pydantic OK (pip prefer-binary di venv)" \
                || warn "M3: install sukses tapi import gagal"
        } || warn "M3: gagal"
fi

# ── M4: Download wheel manual dari GitHub Releases ────────────────────────────
# FIX BUG #9: pakai curl -L (follow redirect) bukan wget -q
if [ "$PYDANTIC_OK" = false ]; then
    info "[M4] Download wheel pydantic-core dari GitHub Releases..."

    case "$ARCH" in
        aarch64)      WHEEL_ARCH="aarch64" ;;
        armv7l|armv7) WHEEL_ARCH="armv7l" ;;
        x86_64)       WHEEL_ARCH="x86_64" ;;
        i686|i386)    WHEEL_ARCH="i686" ;;
        *)             WHEEL_ARCH="aarch64" ;;
    esac

    info "    Fetching asset list dari GitHub API..."
    RELEASE_JSON=$(curl -s --max-time 15 \
        "https://api.github.com/repos/Eutalix/android-pydantic-core/releases/latest" \
        2>/dev/null)

    LATEST_TAG=$(echo "$RELEASE_JSON" | grep '"tag_name"' | head -1 \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)

    if [ -n "$LATEST_TAG" ]; then
        ASSET_NAMES=$(echo "$RELEASE_JSON" \
            | grep '"browser_download_url"' \
            | grep -oE '[^"]+\.whl' \
            | sed 's|.*/||')

        MATCHED_WHEEL=""
        while IFS= read -r whl; do
            if echo "$whl" | grep -q "${PYTAG}" && echo "$whl" | grep -q "${WHEEL_ARCH}"; then
                MATCHED_WHEEL="$whl"
                break
            fi
        done <<< "$ASSET_NAMES"

        if [ -n "$MATCHED_WHEEL" ]; then
            WHEEL_URL="https://github.com/Eutalix/android-pydantic-core/releases/download/v${LATEST_TAG}/${MATCHED_WHEEL}"
            info "    Wheel cocok: $MATCHED_WHEEL"
            # FIX BUG #9: curl -L untuk follow GitHub CDN redirect
            run_bg "Download wheel pydantic-core v$LATEST_TAG (curl -L)..." \
                curl -L --max-time 120 --silent --output "/tmp/${MATCHED_WHEEL}" "$WHEEL_URL" \
                && {
                    # Validasi file bukan HTML error page
                    if file "/tmp/${MATCHED_WHEEL}" 2>/dev/null | grep -q "Zip"; then
                        run_bg "Install wheel pydantic-core..." \
                            $PIP install "/tmp/${MATCHED_WHEEL}" --force-reinstall \
                            && _check_pydantic_version \
                            && log "pydantic-core OK (manual wheel)"
                    else
                        warn "M4: file download bukan wheel yang valid (mungkin redirect HTML)"
                    fi
                    rm -f "/tmp/${MATCHED_WHEEL}"
                } || warn "M4: download gagal"
        else
            warn "M4: tidak ada wheel cocok untuk $PYTAG-linux_$WHEEL_ARCH"
            warn "    Asset tersedia: $(echo "$ASSET_NAMES" | tr '\n' ' ')"
        fi
    else
        warn "M4: tidak bisa fetch release info dari GitHub API"
    fi
fi

# ── M5: Coba versi pydantic-core tertentu dari Android index ──────────────────
if [ "$PYDANTIC_OK" = false ]; then
    info "[M5] Coba versi pydantic-core yang diketahui dari Android index..."
    for VER_TRY in "2.27.2" "2.23.4" "2.20.1" "2.14.6"; do
        info "    Mencoba pydantic-core==$VER_TRY..."
        run_bg "pip pydantic-core==$VER_TRY via Android index..." \
            $PIP install "pydantic-core==$VER_TRY" \
                --extra-index-url https://eutalix.github.io/android-pydantic-core/ \
                --prefer-binary \
            && {
                _check_pydantic_version && { log "pydantic-core==$VER_TRY OK (M5)"; break; }
            } || true
    done
fi

# ── M6: Build dari source jika cargo tersedia ─────────────────────────────────
if [ "$PYDANTIC_OK" = false ]; then
    info "[M6] Build pydantic-core dari source (butuh cargo+maturin, LAMA)..."
    if command -v cargo &>/dev/null; then
        if ! command -v maturin &>/dev/null; then
            run_bg "cargo install maturin (bisa 10-30 menit)..." \
                cargo install maturin
        fi
        if command -v maturin &>/dev/null; then
            cd /tmp && rm -rf pydantic-core-src
            run_bg "git clone pydantic-core..." \
                git clone --depth 1 --branch v2.27.1 \
                    https://github.com/pydantic/pydantic-core.git pydantic-core-src \
                && {
                    cd pydantic-core-src
                    export CARGO_HOME="$HOME/.cargo"
                    export PATH="$HOME/.cargo/bin:$PATH"
                    run_bg "maturin build --release (sabar, ini lama)..." \
                        maturin build --release \
                        && {
                            WHEEL=$(find target/wheels -name "*.whl" 2>/dev/null | head -1)
                            [ -n "$WHEEL" ] && {
                                $PIP install "$WHEEL" \
                                    && _check_pydantic_version \
                                    && log "pydantic-core OK (source build)"
                            }
                        } || warn "M6: maturin build gagal"
                    cd "$SCRIPT_DIR"
                } || { warn "M6: clone gagal"; cd "$SCRIPT_DIR"; }
        else
            warn "M6: maturin tidak tersedia, skip"
        fi
    else
        warn "M6: cargo tidak ditemukan. Jalankan: pkg install rust (lalu ulangi installer)"
    fi
fi

# ── M7: pydantic v1 — last resort ─────────────────────────────────────────────
if [ "$PYDANTIC_OK" = false ]; then
    warn "[M7/LAST RESORT] Semua metode pydantic v2 gagal!"
    warn "Install pydantic v1 sebagai fallback sementara..."
    run_bg "pip install pydantic v1 (last resort)..." \
        $PIP install "pydantic>=1.10.0,<2.0.0" \
        && {
            _check_pydantic_version && log "pydantic v1 OK — FastAPI akan dibatasi ke versi lama"
        } || err "pydantic GAGAL TOTAL di semua metode!"
fi

# ── Cek final pydantic ─────────────────────────────────────────────────────────
if [ "$PYDANTIC_OK" = true ]; then
    VER=$($PY -c "import pydantic; print(pydantic.__version__)" 2>/dev/null)
    ok "pydantic v${VER:-?} $([ "$PYDANTIC_V2" = true ] && echo '(v2 ✔)' || echo '(v1 ⚠)')"
fi

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 11 · Install pydantic & FastAPI di dalam venv"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #3: PYDANTIC_V2 sekarang selalu ter-set dengan benar dari _check_pydantic_version
# FIX BUG #6: install starlette secara eksplisit sebelum uvicorn

if [ "$PYDANTIC_V2" = true ]; then
    info "Pydantic v2 tersedia — install FastAPI versi terbaru..."
    run_bg "pip install pydantic fastapi starlette (di venv)..." \
        $PIP install "pydantic>=2.5.0" "starlette>=0.35.0" "fastapi>=0.109.0" \
        && log "pydantic + starlette + FastAPI terbaru OK" \
        || {
            warn "Versi terbaru gagal, coba tanpa constraint versi..."
            run_bg "pip install pydantic fastapi starlette..." \
                $PIP install pydantic starlette fastapi \
                && log "pydantic + FastAPI OK" \
                || err "pydantic/FastAPI gagal"
        }
else
    info "Pydantic v1 mode — install FastAPI 0.99.x (last version yang support pydantic v1)..."
    run_bg "pip install fastapi==0.99.1 starlette (pydantic v1 compat)..." \
        $PIP install "fastapi==0.99.1" "starlette>=0.27.0,<0.28.0" \
        && log "FastAPI 0.99.1 + starlette OK (pydantic v1 mode)" \
        || {
            warn "fastapi 0.99.1 gagal, coba tanpa constraint..."
            run_bg "pip install fastapi starlette..." \
                $PIP install fastapi starlette \
                && log "FastAPI OK" \
                || err "FastAPI gagal"
        }
fi

$PY -c "import fastapi; print('fastapi:', fastapi.__version__)" >> "$LOGFILE" 2>&1 \
    && ok "FastAPI terverifikasi ✔" \
    || err "FastAPI import GAGAL!"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 12 · Install CCXT (di dalam venv)"
# ══════════════════════════════════════════════════════════════════════════════
#
# STRATEGI:
#   1. aiohttp>=3.9.0 diinstall dulu (versi baru, tidak butuh compile C)
#   2. cryptography: sudah ada dari STEP 5 (pkg atau pip sistem)
#      jika belum ada di venv, install pip sekarang
#   3. ccxt --no-deps (pure-Python wheel, tidak tarik dep lama)
#   4. dep ccxt lainnya (requests, certifi) diinstall manual
#
# FIX BUG #4: aiohttp HANYA diinstall di sini, TIDAK diulang di STEP 14

CCXT_OK=false

# Pastikan cryptography tersedia di venv (dep wajib ccxt)
# FIX BUG #5: fallback pip cryptography jika pkg gagal
if ! $PY -c "import cryptography" 2>/dev/null; then
    info "cryptography belum ada di venv — install pip sekarang..."
    run_bg "pip install cryptography --prefer-binary (di venv)..." \
        $PIP install "cryptography>=41.0.0" --prefer-binary \
        && {
            $PY -c "import cryptography" 2>/dev/null \
                && { CRYPTO_OK=true; log "cryptography pip OK (venv)"; } \
                || warn "cryptography pip install tapi import gagal"
        } || warn "cryptography pip gagal — ccxt mungkin tidak bisa koneksi HTTPS"
fi

# aiohttp>=3.9.0 (satu kali di sini, tidak diulang di STEP 14)
run_bg "pip install aiohttp>=3.9.0 --prefer-binary..." \
    $PIP install "aiohttp>=3.9.0" --prefer-binary \
    && log "aiohttp>=3.9.0 OK" \
    || warn "aiohttp gagal, ccxt mungkin terbatas ke sync saja"

# requests & certifi
run_bg "pip install requests certifi..." \
    $PIP install requests certifi \
    && log "requests certifi OK" || warn "requests/certifi ada warning"

# ── FASE 2: Install ccxt --no-deps ─────────────────────────────────────────────
info "[CCXT-M1] pip install ccxt --no-deps (strategi utama)..."
run_bg "pip install ccxt --no-deps..." \
    $PIP install "ccxt>=4.2.0" --no-deps \
    && {
        $PY -c "import ccxt; print('ccxt:', ccxt.__version__)" >> "$LOGFILE" 2>&1 \
            && { log "ccxt OK (M1 no-deps)"; CCXT_OK=true; } \
            || warn "M1: install sukses tapi import gagal"
    } || warn "M1: no-deps gagal, coba dengan deps..."

# ── M2: Install normal ─────────────────────────────────────────────────────────
if [ "$CCXT_OK" = false ]; then
    info "[CCXT-M2] pip install ccxt normal (aiohttp baru sudah terpasang)..."
    run_bg "pip install ccxt..." \
        $PIP install "ccxt>=4.2.0" \
        && {
            $PY -c "import ccxt" 2>/dev/null && { log "ccxt OK (M2 normal)"; CCXT_OK=true; } \
                || warn "M2: import gagal"
        } || warn "M2: gagal"
fi

# ── M3: Versi ccxt lebih lama ──────────────────────────────────────────────────
if [ "$CCXT_OK" = false ]; then
    info "[CCXT-M3] Coba ccxt==4.1.98 --no-deps..."
    run_bg "pip ccxt==4.1.98 --no-deps..." \
        $PIP install "ccxt==4.1.98" --no-deps \
        && {
            $PY -c "import ccxt" 2>/dev/null && { log "ccxt 4.1.98 OK (M3)"; CCXT_OK=true; } \
                || warn "M3: import gagal"
        } || warn "M3: gagal"
fi

# ── M4: Install dari GitHub ────────────────────────────────────────────────────
if [ "$CCXT_OK" = false ]; then
    info "[CCXT-M4] pip install ccxt dari GitHub master..."
    run_bg "pip ccxt dari GitHub..." \
        $PIP install "git+https://github.com/ccxt/ccxt.git" --no-deps \
        && {
            $PY -c "import ccxt" 2>/dev/null && { log "ccxt OK (M4 GitHub)"; CCXT_OK=true; } \
                || warn "M4: import gagal"
        } || warn "M4: gagal"
fi

[ "$CCXT_OK" = true ] && ok "ccxt terverifikasi ✔" || err "ccxt GAGAL di semua metode!"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 13 · Install Technical Analysis Library (di dalam venv)"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #11: pandas-ta punya bug kompatibilitas NumPy >= 2.0
# Jika numpy >= 2.0 terinstall, pandas-ta diinstall dengan --no-deps
# dan numpy di-pin ke <2.0 di dalam venv jika belum ada pandas-ta yang kompatibel.
# Prioritas tetap 'ta' (pure-Python, tidak ada masalah NumPy 2.x).

TA_OK=false
TA_LIB="none"

# ── TA-M1: 'ta' — pure-Python, tidak ada masalah NumPy 2.x ────────────────────
info "[TA-M1] Install 'ta' — pure-Python, ringan, aman di NumPy 2.x..."
run_bg "pip install ta (di venv)..." \
    $PIP install "ta>=0.11.0" \
    && {
        $PY -c "import ta" 2>/dev/null && { log "ta OK (M1)"; TA_OK=true; TA_LIB="ta"; }
    } || warn "TA-M1 gagal"

# ── TA-M2: pandas-ta — hanya jika 'ta' gagal ──────────────────────────────────
if [ "$TA_OK" = false ]; then
    info "[TA-M2] 'ta' gagal — coba pandas-ta..."

    # Deteksi versi numpy untuk cek kompatibilitas
    NUMPY_VER=$($PY -c "import numpy; print(numpy.__version__)" 2>/dev/null | cut -d. -f1)
    if [ "${NUMPY_VER:-1}" -ge 2 ] 2>/dev/null; then
        info "    NumPy >= 2.0 terdeteksi — pandas-ta mungkin tidak kompatibel"
        info "    Mencoba pandas-ta --no-deps untuk hindari konflik..."
        run_bg "pip install pandas-ta --no-deps..." \
            $PIP install "pandas-ta" --no-deps \
            && {
                $PY -c "import pandas_ta" 2>/dev/null && {
                    log "pandas-ta OK (M2, --no-deps, NumPy 2.x)"
                    TA_OK=true; TA_LIB="pandas_ta"
                }
            } || warn "TA-M2 --no-deps gagal juga"
    else
        run_bg "pip install pandas-ta (di venv)..." \
            $PIP install "pandas-ta" \
            && {
                $PY -c "import pandas_ta" 2>/dev/null && {
                    log "pandas-ta OK (M2)"
                    TA_OK=true; TA_LIB="pandas_ta"
                }
            } || warn "TA-M2 gagal"
    fi
fi

# ── TA-M3: pandas-ta dari GitHub development ──────────────────────────────────
if [ "$TA_OK" = false ]; then
    info "[TA-M3] pandas-ta dari GitHub development branch..."
    run_bg "pip pandas-ta dari GitHub..." \
        $PIP install "git+https://github.com/twopirllc/pandas-ta.git@development" \
        && {
            $PY -c "import pandas_ta" 2>/dev/null && {
                log "pandas-ta OK (M3 GitHub)"
                TA_OK=true; TA_LIB="pandas_ta"
            }
        } || warn "TA-M3 gagal"
fi

# ── TA-M4: ta-lib via pkg ──────────────────────────────────────────────────────
if [ "$TA_OK" = false ]; then
    info "[TA-M4] Coba ta-lib via pkg (opsional, jarang tersedia)..."
    run_bg "pkg install ta-lib..." pkg install -y ta-lib \
        && {
            run_bg "pip install TA-Lib wrapper..." $PIP install TA-Lib \
                && {
                    $PY -c "import talib" 2>/dev/null && {
                        log "ta-lib OK (M4)"
                        TA_OK=true; TA_LIB="talib"
                    }
                }
        } || warn "TA-M4: ta-lib tidak tersedia di pkg"
fi

[ "$TA_OK" = true ] && ok "TA library OK: $TA_LIB ✔" \
    || warn "TA library tidak ada — indikator teknikal tidak akan aktif"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 14 · Install Core Dependencies (Pure Python, di dalam venv)"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #4: aiohttp DIHAPUS dari sini (sudah diinstall di STEP 12)
# FIX BUG #6: starlette diinstall eksplisit sebelum uvicorn
#             greenlet diinstall untuk SQLAlchemy async support

pip_install() {
    local name="$1" spec="$2"
    run_bg "pip install $name (di venv)..." $PIP install "$spec" \
        && { log "$name OK"; return 0; } \
        || { warn "$name gagal, skip"; return 1; }
}

pip_install "python-dotenv"    "python-dotenv>=1.0.0"
pip_install "aiofiles"         "aiofiles>=23.2.1"
pip_install "certifi"          "certifi"
pip_install "python-dateutil"  "python-dateutil>=2.8.2"
pip_install "pytz"             "pytz"
pip_install "asyncio-throttle" "asyncio-throttle>=1.0.2"
# FIX BUG #6: starlette eksplisit sebelum uvicorn
pip_install "starlette"        "starlette>=0.35.0"
pip_install "uvicorn"          "uvicorn>=0.27.0"
pip_install "aiosqlite"        "aiosqlite>=0.19.0"
# FIX BUG #6: greenlet untuk SQLAlchemy async
pip_install "greenlet"         "greenlet>=3.0.0"
pip_install "sqlalchemy"       "sqlalchemy>=2.0.25"
pip_install "httpx"            "httpx>=0.26.0"
# aiohttp TIDAK diinstall ulang di sini (sudah di STEP 12)

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 15 · Buat Struktur Folder"
# ══════════════════════════════════════════════════════════════════════════════

mkdir -p data logs dashboard
log "Folder data/ logs/ dashboard/ dibuat"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 16 · Setup .env"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #7: generate API key dengan metode yang aman di Termux
# - Utama: $PY -c "import secrets..." (venv sudah aktif dan valid)
# - Fallback: $SYS_PY (Python sistem, pasti ada)
# - Fallback akhir: openssl rand (tersedia di Termux setelah STEP 4)
# - TIDAK pakai: cat /dev/urandom | tr | head (bisa hang di Termux)

BOT_DIR="$SCRIPT_DIR"

# Generate API key dengan urutan fallback yang aman
NEW_API_KEY=""
if [ -x "$PY" ]; then
    NEW_API_KEY=$($PY -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null)
fi
if [ -z "$NEW_API_KEY" ] && command -v "$SYS_PY" &>/dev/null; then
    NEW_API_KEY=$($SYS_PY -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null)
fi
if [ -z "$NEW_API_KEY" ] && command -v openssl &>/dev/null; then
    NEW_API_KEY=$(openssl rand -hex 32 2>/dev/null)
fi
if [ -z "$NEW_API_KEY" ]; then
    # Absolute last resort: timestamp + random dari $RANDOM (bash builtin)
    NEW_API_KEY=$(printf '%x%x%x%x' $RANDOM $RANDOM $RANDOM $RANDOM)
    warn "API key digenerate dengan metode darurat (kurang acak)"
fi

if [ ! -f ".env" ]; then
    info "Membuat .env dari template..."
    cat > .env << EOF

EXCHANGE_ID=binance
API_KEY=ISI_API_KEY_KAMU_DISINI
API_SECRET=ISI_API_SECRET_KAMU_DISINI
TESTNET=true

QUOTE_CURRENCY=USDT
INITIAL_CAPITAL=1000
MAX_OPEN_POSITIONS=3
WATCHLIST=BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,BNB/USDT,NEAR/USDT,PEPE/USDT
STRATEGY=volumetric_breakout
TIMEFRAME=15m
LOOKBACK_CANDLES=200

MAX_DRAWDOWN_PCT=15.0
MAX_POSITION_SIZE_PCT=10.0
STOP_LOSS_PCT=2.5
TAKE_PROFIT_PCT=5.0
ATR_MULTIPLIER_SL=2.0
ATR_MULTIPLIER_TP=3.5
DAILY_LOSS_LIMIT_PCT=10.0
RISK_PER_TRADE_PCT=1.0
MAX_SLIPPAGE_PCT=0.5
MIN_ORDER_VALUE_USDT=10.0

USE_TRAILING_STOP=true
TRAILING_ATR_MULT=1.5

SENTIMENT_ENABLED=true

DATABASE_URL=sqlite+aiosqlite:///./data/trading_bot.db

API_HOST=127.0.0.1
API_PORT=8000

LOG_LEVEL=INFO
LOG_FILE=logs/trading_bot.log

TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

EMAIL_ENABLED=false

BOT_DIR=${BOT_DIR}
VENV_DIR=${BOT_DIR}/venv

VOLUME_MULTIPLIER=2.0
VOLUME_SPIKE_THRESHOLD=2.5
RSI_MIN=48
RSI_MAX=72
RSI_GOLDEN_CROSS_MIN=48
ATR_PCT_THRESHOLD=0.5
QUICK_TP_PCT=2.5
QUICK_SL_PCT=1.8
TRAILING_ACTIVATION_PCT=2.0
TRAILING_GAP_PCT=0.8

DASHBOARD_API_KEY=${NEW_API_KEY}
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000

TA_LIBRARY=${TA_LIB}
EOF
    log ".env dibuat — EDIT API_KEY & API_SECRET sebelum jalankan bot!"
    info "DASHBOARD_API_KEY: ${NEW_API_KEY:0:16}..."
else
    warn ".env sudah ada, tidak ditimpa"
    _update_env() {
        local key="$1" val="$2"
        if grep -q "^${key}=" .env; then
            sed -i "s|^${key}=.*|${key}=${val}|" .env
        else
            echo "${key}=${val}" >> .env
        fi
    }
    _update_env "TA_LIBRARY"  "$TA_LIB"
    _update_env "BOT_DIR"     "$BOT_DIR"
    _update_env "VENV_DIR"    "${BOT_DIR}/venv"
    info "TA_LIBRARY, BOT_DIR, VENV_DIR diupdate di .env yang sudah ada"
fi

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 17 · Buat Helper Scripts"
# ══════════════════════════════════════════════════════════════════════════════
#
# FIX BUG #8: heredoc dengan quoting yang benar
# - STARTSCRIPT menggunakan 'STARTSCRIPT' (single-quote) agar semua $ di-escape
#   kecuali yang memang perlu expand saat script dibuat (tidak ada di sini)
# - Semua variable di dalam script yang dibuat menggunakan \$ untuk runtime

# start.sh — pakai quoted heredoc 'STARTSCRIPT' agar tidak ada expansion tak sengaja
cat > start.sh << 'STARTSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
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

python "$SCRIPT_DIR/spot/main_spot.py" >> "$SCRIPT_DIR/logs/trading_bot.log" 2>&1 &
BOT_PID=$!
echo $BOT_PID > "$SCRIPT_DIR/.bot_pid"
echo ""
python "$SCRIPT_DIR/shared_service/telegram_bot.py" >> "$SCRIPT_DIR/logs/telegram_bot.log" 2>&1 &
TG_PID=$!
echo $TG_PID > "$SCRIPT_DIR/.tg_pid"
echo "✅ Telegram Bot started! PID: $TG_PID"
echo "✅ Bot started! PID: $BOT_PID"
echo "📋 Log    : bash $SCRIPT_DIR/view_log.sh"
echo "📊 Status : bash $SCRIPT_DIR/status.sh"
echo "🌐 Dashboard: http://127.0.0.1:8000"
STARTSCRIPT

cat > stop.sh << 'STOPSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
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

echo "🛑 Stopping AlgoTrader Pro Components..."
kill_process "$SCRIPT_DIR/.bot_pid" "python.*spot/main_spot.py" "Core Bot"
kill_process "$SCRIPT_DIR/.tg_pid" "python.*shared_service/telegram_bot.py" "Telegram Bot"
echo "Done."
STOPSCRIPT

cat > status.sh << 'STATUSSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
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
STATUSSCRIPT

cat > view_log.sh << 'LOGSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tail -f "$SCRIPT_DIR/logs/trading_bot.log"
LOGSCRIPT

cat > activate_venv.sh << 'ACTIVATESCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Gunakan: source activate_venv.sh  (bukan bash activate_venv.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"
echo "✔ venv aktif: $VIRTUAL_ENV"
echo "  Python : $(python --version)"
echo "  pip    : $(pip --version | cut -d' ' -f1-2)"
echo ""
echo "Untuk keluar dari venv: deactivate"
ACTIVATESCRIPT

cat > reinstall.sh << 'REINSTALL'
#!/data/data/com.termux/files/usr/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
echo "🔄 Menjalankan ulang installer..."
bash "$SCRIPT_DIR/install_termux.sh"
REINSTALL

chmod +x start.sh stop.sh status.sh view_log.sh activate_venv.sh reinstall.sh
log "Helper scripts dibuat (start/stop/status/view_log/activate_venv/reinstall)"

# ══════════════════════════════════════════════════════════════════════════════
step "STEP 18 · Verifikasi Final (di dalam venv)"
# ══════════════════════════════════════════════════════════════════════════════

echo ""
info "Verifikasi semua import di dalam venv..."
echo ""

$PY << 'PYCHECK'
import sys

WAJIB = [
    ("numpy",              "numpy"),
    ("pandas",             "pandas"),
    ("pydantic",           "pydantic"),
    ("pydantic-core",      "pydantic_core"),
    ("fastapi",            "fastapi"),
    ("uvicorn",            "uvicorn"),
    ("ccxt",               "ccxt"),
    ("aiohttp",            "aiohttp"),
    ("sqlalchemy",         "sqlalchemy"),
    ("aiosqlite",          "aiosqlite"),
    ("python-dotenv",      "dotenv"),
    ("asyncio-throttle",   "asyncio_throttle"),
    ("aiofiles",           "aiofiles"),
    ("certifi",            "certifi"),
    ("cryptography",       "cryptography"),
    ("starlette",          "starlette"),
    ("greenlet",           "greenlet"),
]

OPSIONAL = [
    ("ta",        "ta"),
    ("pandas-ta", "pandas_ta"),
    ("ta-lib",    "talib"),
    ("httpx",     "httpx"),
]

GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
RED    = '\033[0;31m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
NC     = '\033[0m'

venv = sys.prefix
print(f"  {DIM}Python  : {sys.version.split()[0]}{NC}")
print(f"  {DIM}Venv    : {venv}{NC}")
print()

print(f"{BOLD}  Package Wajib:{NC}")
all_ok = True
for display, mod in WAJIB:
    try:
        m = __import__(mod)
        ver = getattr(m, '__version__', '?')
        note = ''
        if mod == 'pydantic':
            vmaj = int(str(ver).split('.')[0])
            note = f' {GREEN}(v2 ✔){NC}' if vmaj >= 2 else f' {YELLOW}(v1 ⚠){NC}'
        mod_file = getattr(m, '__file__', '') or ''
        is_sys = '/usr/lib/python' in mod_file and 'site-packages' not in mod_file
        src = f' {DIM}[sys-pkg]{NC}' if is_sys else ''
        print(f"  {GREEN}✔{NC}  {display:<22} {DIM}v{ver}{NC}{note}{src}")
    except ImportError as e:
        print(f"  {RED}✘{NC}  {display:<22} {RED}MISSING{NC}: {e}")
        all_ok = False

print(f"\n{BOLD}  Package Opsional:{NC}")
ta_found = []
for display, mod in OPSIONAL:
    try:
        m = __import__(mod)
        ver = getattr(m, 'version', getattr(m, '__version__', '?'))
        print(f"  {GREEN}✔{NC}  {display:<22} {DIM}v{ver}{NC}")
        if mod in ('ta', 'pandas_ta', 'talib'):
            ta_found.append(display)
    except ImportError:
        print(f"  {DIM}–{NC}  {display:<22} {DIM}tidak terinstall{NC}")

if ta_found:
    print(f"\n  TA Library aktif: {GREEN}{', '.join(ta_found)}{NC}")
else:
    print(f"\n  {YELLOW}⚠ Tidak ada TA library! Indikator teknikal tidak aktif.{NC}")

print()
if all_ok:
    print(f"  {GREEN}{BOLD}✔ Semua dependency wajib OK!{NC}")
    sys.exit(0)
else:
    print(f"  {YELLOW}⚠ Ada dependency yang belum terinstall.{NC}")
    sys.exit(1)
PYCHECK

VERIFY_STATUS=$?

# ══════════════════════════════════════════════════════════════════════════════
# RINGKASAN AKHIR
# ══════════════════════════════════════════════════════════════════════════════

echo ""

if [ $VERIFY_STATUS -eq 0 ]; then
    echo -e "${GREEN}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║                                                      ║"
    echo "  ║   ██████╗  ██████╗ ███╗  ██╗███████╗               ║"
    echo "  ║   ██╔══██╗██╔═══██╗████╗ ██║██╔════╝               ║"
    echo "  ║   ██║  ██║██║   ██║██╔██╗██║█████╗                 ║"
    echo "  ║   ██║  ██║██║   ██║██║╚████║██╔══╝                 ║"
    echo "  ║   ██████╔╝╚██████╔╝██║ ╚███║███████╗               ║"
    echo "  ║   ╚═════╝  ╚═════╝ ╚═╝  ╚══╝╚══════╝               ║"
    echo "  ║                                                      ║"
    echo "  ║        INSTALASI BERHASIL SEMPURNA!                  ║"
    echo "  ║                                                      ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo ""
    echo -e "  ${BOLD}Langkah selanjutnya:${NC}"
    echo ""
    echo -e "  ${CYAN}1.${NC}  ${BOLD}nano .env${NC}"
    echo -e "       Isi ${BOLD}API_KEY${NC} & ${BOLD}API_SECRET${NC} dari exchange kamu"
    echo ""
    echo -e "  ${CYAN}2.${NC}  ${BOLD}bash start.sh${NC}"
    echo -e "       Jalankan bot (venv aktif otomatis)"
    echo ""
    echo -e "  ${CYAN}3.${NC}  ${BOLD}bash status.sh${NC}     →  cek status bot"
    echo -e "  ${CYAN}4.${NC}  ${BOLD}bash view_log.sh${NC}   →  lihat log realtime"
    echo -e "  ${CYAN}5.${NC}  ${BOLD}http://127.0.0.1:8000${NC}  →  buka dashboard"
    echo ""
    echo -e "  ${BOLD}Untuk debugging / development manual:${NC}"
    echo -e "  ${CYAN}source activate_venv.sh${NC}  →  aktifkan venv di terminal"
    echo -e "  ${CYAN}deactivate${NC}               →  keluar dari venv"
    echo ""
    echo -e "  ${DIM}─────────────────────────────────────────────────────${NC}"
    echo -e "  ${DIM}TA Library   : $TA_LIB${NC}"
    echo -e "  ${DIM}Pydantic     : $([ "$PYDANTIC_V2" = true ] && echo 'v2' || echo 'v1')${NC}"
    echo -e "  ${DIM}Python       : $PY_VER${NC}"
    echo -e "  ${DIM}Arch         : $ARCH${NC}"
    echo -e "  ${DIM}Venv         : $VENV_PATH${NC}"
    echo -e "  ${DIM}Bot Dir      : $SCRIPT_DIR${NC}"
else
    echo -e "${YELLOW}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║                                                      ║"
    echo "  ║      ⚠   INSTALASI SELESAI DENGAN WARNING   ⚠       ║"
    echo "  ║                                                      ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo ""
    echo -e "  Ada package yang gagal. Lihat detail: ${BOLD}cat install_log.txt${NC}"
    echo ""
    echo -e "  ${BOLD}Solusi manual (aktifkan venv dulu):${NC}"
    echo -e "  ${CYAN}source venv/bin/activate${NC}"
    echo ""
    echo -e "  ${BOLD}Solusi pydantic-core:${NC}"
    echo -e "  ${CYAN}pip install pydantic-core \\${NC}"
    echo -e "  ${CYAN}    --extra-index-url https://eutalix.github.io/android-pydantic-core/${NC}"
    echo ""
    echo -e "  ${BOLD}Solusi numpy/pandas:${NC}"
    echo -e "  ${CYAN}pkg install tur-repo && pkg update${NC}"
    echo -e "  ${CYAN}pkg install python-numpy python-pandas${NC}"
    echo -e "  ${CYAN}rm -rf venv && bash install_termux.sh${NC}"
    echo ""
    echo -e "  ${BOLD}Atau jalankan ulang installer:${NC}"
    echo -e "  ${CYAN}bash reinstall.sh${NC}"
fi

echo ""
echo -e "${DIM}  ─────────────────────────────────────────────────────────${NC}"
echo -e "${DIM}  AlgoTrader Pro v7.0  ·  Log: $LOGFILE${NC}"
echo -e "${DIM}  ─────────────────────────────────────────────────────────${NC}"
echo ""

