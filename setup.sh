#!/bin/bash
# ============================================================
# CUA_Collector — Root Setup (Automated Collector)
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(eval echo "~$REAL_USER")

echo ""
echo "============================================================"
echo "  CUA_Collector — Root Setup"
echo "============================================================"
echo "  User:    $REAL_USER"
echo "  Home:    $REAL_HOME"
echo "  Repo:    $SCRIPT_DIR"
echo "  Session: ${XDG_SESSION_TYPE:-unknown}"
echo "  Desktop: ${XDG_CURRENT_DESKTOP:-unknown}"
echo "============================================================"
echo ""

info "Step 1/5: Installing system dependencies..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        build-essential cmake pkg-config \
        python3 python3-pip python3-venv python3-dev \
        gjs \
        pipewire libpipewire-0.3-dev libspa-0.2-dev \
        libevdev-dev libturbojpeg0-dev \
        xdg-desktop-portal xdg-desktop-portal-gnome \
        2>/dev/null
    ok "APT packages installed"
elif command -v dnf &>/dev/null; then
    sudo dnf install -y -q \
        gcc-c++ cmake pkgconf-pkg-config \
        python3 python3-pip python3-devel \
        gjs \
        pipewire pipewire-devel libevdev-devel libjpeg-turbo-devel \
        xdg-desktop-portal xdg-desktop-portal-gnome \
        2>/dev/null
    ok "DNF packages installed"
else
    warn "Unknown package manager. Install build tools and PipeWire/libevdev/turbojpeg dev packages manually."
fi

info "Step 2/5: Installing Python dependencies..."
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ok "Created virtual environment (.venv)"
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt pybind11
ok "Python packages installed"

info "Step 3/5: Setting up input device permissions..."
if groups "$REAL_USER" | grep -q '\binput\b'; then
    ok "User '$REAL_USER' already in 'input' group"
else
    sudo usermod -aG input "$REAL_USER"
    ok "Added '$REAL_USER' to 'input' group"
    warn "You must LOG OUT and LOG BACK IN for the group change to take effect"
fi

info "Step 4/5: Installing GNOME cursor tracker extension..."
bash "$SCRIPT_DIR/setup_extension.sh"
ok "GNOME extension installed"

info "Step 5/5: Building native module..."
cmake -S "$SCRIPT_DIR" -B "$SCRIPT_DIR/build" -DPython3_EXECUTABLE="$SCRIPT_DIR/.venv/bin/python3"
cmake --build "$SCRIPT_DIR/build" -j"$(nproc)"
ok "Native module built"

echo ""
echo "Done."
echo "Next:"
echo "  1. Log out and log back in once"
echo "  2. Run: ./run.sh"
echo ""
