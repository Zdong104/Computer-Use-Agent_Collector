#!/bin/bash
# ============================================================
# CUA_Collector — Full System Setup
# ============================================================
# Run this script on a fresh system to set up everything needed
# for the CUA Collector to work properly.
#
# Usage:
#   bash setup.sh          # interactive (prompts for sudo)
#   sudo bash setup.sh     # run as root (skips sudo prompts)
#
# What it does:
#   1. Installs system dependencies (gjs, gstreamer, etc.)
#   2. Installs Python dependencies
#   3. Sets up evdev permissions (input group)
#   4. Installs GNOME cursor-tracker extension (Wayland only)
#   5. Verifies everything works
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR]${NC}   $*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(eval echo "~$REAL_USER")

echo ""
echo "============================================================"
echo "  CUA_Collector — System Setup"
echo "============================================================"
echo "  User:    $REAL_USER"
echo "  Home:    $REAL_HOME"
echo "  Repo:    $SCRIPT_DIR"
echo "  OS:      $(lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | head -1)"
echo "  Session: ${XDG_SESSION_TYPE:-unknown}"
echo "  Desktop: ${XDG_CURRENT_DESKTOP:-unknown}"
echo "============================================================"
echo ""

# ---- Step 1: System dependencies ----
info "Step 1/5: Installing system dependencies..."

if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        python3 python3-pip python3-venv \
        gjs \
        gstreamer1.0-pipewire \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        libgstreamer1.0-dev \
        pipewire \
        xdg-desktop-portal \
        xdg-desktop-portal-gnome \
        2>/dev/null
    ok "APT packages installed"
elif command -v dnf &>/dev/null; then
    sudo dnf install -y -q \
        python3 python3-pip \
        gjs \
        gstreamer1-pipewire \
        gstreamer1-plugins-base \
        gstreamer1-plugins-good \
        pipewire \
        xdg-desktop-portal \
        xdg-desktop-portal-gnome \
        2>/dev/null
    ok "DNF packages installed"
else
    warn "Unknown package manager. Install manually: python3, gjs, gstreamer, pipewire"
fi

# ---- Step 2: Python dependencies ----
info "Step 2/5: Installing Python dependencies..."

cd "$SCRIPT_DIR"

if [ -d ".venv" ]; then
    info "Existing venv found, using it"
else
    python3 -m venv .venv
    ok "Created virtual environment (.venv)"
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Python packages installed"

# ---- Step 3: evdev permissions ----
info "Step 3/5: Setting up input device permissions..."

if groups "$REAL_USER" | grep -q '\binput\b'; then
    ok "User '$REAL_USER' already in 'input' group"
else
    sudo usermod -aG input "$REAL_USER"
    ok "Added '$REAL_USER' to 'input' group"
    warn "You must LOG OUT and LOG BACK IN for group change to take effect"
fi

# Verify /dev/input is accessible
if [ -r /dev/input/event0 ] 2>/dev/null; then
    ok "Input devices are readable"
else
    warn "Input devices not yet readable (log out/in after group change)"
fi

# ---- Step 4: GNOME cursor-tracker extension (Wayland only) ----
info "Step 4/5: Setting up GNOME cursor-tracker extension..."

SESSION_TYPE="${XDG_SESSION_TYPE:-x11}"
DESKTOP="${XDG_CURRENT_DESKTOP:-unknown}"

if [[ "$SESSION_TYPE" == "wayland" ]] && echo "$DESKTOP" | grep -qi "gnome"; then
    EXT_UUID="cursor-tracker@cua"
    EXT_DIR="$REAL_HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

    mkdir -p "$EXT_DIR"

    # metadata.json
    cat > "$EXT_DIR/metadata.json" << 'METAEOF'
{
  "uuid": "cursor-tracker@cua",
  "name": "CUA Cursor Tracker",
  "description": "Exposes cursor position over D-Bus for CUA Collector",
  "shell-version": ["45", "46", "47", "48", "49"],
  "version": 1
}
METAEOF

    # extension.js
    cat > "$EXT_DIR/extension.js" << 'EXTEOF'
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const DBUS_IFACE = `
<node>
  <interface name="org.cua.CursorTracker">
    <method name="GetPosition">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
    </method>
  </interface>
</node>`;

export default class CursorTrackerExtension {
    _dbusId = null;
    _ownerId = null;

    enable() {
        this._dbusId = Gio.DBus.session.register_object(
            '/org/cua/CursorTracker',
            Gio.DBusNodeInfo.new_for_xml(DBUS_IFACE).interfaces[0],
            (connection, sender, path, iface, method, params, invocation) => {
                if (method === 'GetPosition') {
                    const [x, y] = global.get_pointer();
                    invocation.return_value(new GLib.Variant('(ii)', [x, y]));
                }
            },
            null,
            null
        );

        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            'org.cua.CursorTracker',
            Gio.BusNameOwnerFlags.NONE,
            null, null, null
        );

        console.log('[CUA] Cursor tracker extension enabled');
    }

    disable() {
        if (this._dbusId) {
            Gio.DBus.session.unregister_object(this._dbusId);
            this._dbusId = null;
        }
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = null;
        }
        console.log('[CUA] Cursor tracker extension disabled');
    }
}
EXTEOF

    # Fix ownership if running as sudo
    if [ -n "$SUDO_USER" ]; then
        chown -R "$REAL_USER:$(id -gn $REAL_USER)" "$EXT_DIR"
    fi

    # Add to enabled-extensions
    CURRENT=$(su - "$REAL_USER" -c "gsettings get org.gnome.shell enabled-extensions" 2>/dev/null || echo "@as []")
    if echo "$CURRENT" | grep -q "$EXT_UUID"; then
        ok "Extension already in enabled list"
    else
        if [ "$CURRENT" = "@as []" ]; then
            NEW="['$EXT_UUID']"
        else
            NEW=$(echo "$CURRENT" | sed "s/]/, '$EXT_UUID']/")
        fi
        su - "$REAL_USER" -c "gsettings set org.gnome.shell enabled-extensions \"$NEW\"" 2>/dev/null || \
            gsettings set org.gnome.shell enabled-extensions "$NEW" 2>/dev/null || \
            warn "Could not update gsettings. Manually enable: gnome-extensions enable $EXT_UUID"
        ok "Extension added to enabled list"
    fi

    ok "GNOME extension installed at: $EXT_DIR"
    warn "You must LOG OUT and LOG BACK IN for the extension to load"
else
    info "Not on Wayland GNOME — skipping extension install"
    ok "pynput or gnome-eval will be used for cursor tracking"
fi

# ---- Step 5: Verification ----
info "Step 5/5: Verifying setup..."

echo ""
echo "  Checking components:"

# Python
if source "$SCRIPT_DIR/.venv/bin/activate" 2>/dev/null && python3 -c "import evdev, mss, pynput, PIL" 2>/dev/null; then
    echo -e "    Python packages:     ${GREEN}✓${NC}"
else
    echo -e "    Python packages:     ${RED}✗${NC} (run: pip install -r requirements.txt)"
fi

# gjs
if command -v gjs &>/dev/null; then
    echo -e "    gjs:                 ${GREEN}✓${NC}"
else
    echo -e "    gjs:                 ${RED}✗${NC} (apt install gjs)"
fi

# PipeWire
if command -v pw-cli &>/dev/null; then
    echo -e "    PipeWire:            ${GREEN}✓${NC}"
else
    echo -e "    PipeWire:            ${RED}✗${NC} (apt install pipewire)"
fi

# input group
if groups "$REAL_USER" 2>/dev/null | grep -q '\binput\b'; then
    echo -e "    input group:         ${GREEN}✓${NC}"
else
    echo -e "    input group:         ${YELLOW}⟳${NC} (pending — log out/in)"
fi

# GNOME extension
if [[ "$SESSION_TYPE" == "wayland" ]]; then
    if gdbus call --session --dest org.cua.CursorTracker --object-path /org/cua/CursorTracker --method org.cua.CursorTracker.GetPosition &>/dev/null; then
        echo -e "    Cursor extension:    ${GREEN}✓${NC}"
    else
        echo -e "    Cursor extension:    ${YELLOW}⟳${NC} (pending — log out/in)"
    fi
fi

echo ""
echo "============================================================"
echo "  Setup complete!"
echo ""
if [[ "$SESSION_TYPE" == "wayland" ]]; then
    echo "  ⚠️  ACTION REQUIRED: Log out and log back in to activate:"
    echo "     • input group membership (for evdev)"
    echo "     • GNOME cursor-tracker extension"
    echo ""
fi
echo "  After logging back in, run:"
echo "    cd $SCRIPT_DIR"
echo "    source .venv/bin/activate"
echo "    python collector.py"
echo "============================================================"
echo ""
