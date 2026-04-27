#!/bin/bash
# ============================================================
# Install the CUA Cursor Tracker GNOME Shell extension.
#
# This extension exposes cursor position over D-Bus on Wayland GNOME,
# where pynput and Shell.Eval cannot access the global pointer.
#
# Usage: bash setup_extension.sh
#
# After running, you MUST log out and log back in.
# ============================================================

set -e

EXT_UUID="cursor-tracker@cua"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

echo ""
echo "=== CUA Cursor Tracker Extension ==="
echo ""

# 1. Create extension files
mkdir -p "$EXT_DIR"

cat > "$EXT_DIR/metadata.json" << 'EOF'
{
  "uuid": "cursor-tracker@cua",
  "name": "CUA Cursor Tracker",
  "description": "Exposes cursor position over D-Bus for CUA Collector",
  "shell-version": ["45", "46", "47", "48", "49", "50"],
  "version": 3
}
EOF

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
    <method name="GetPositionPixel">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="i" direction="out" name="mon_width"/>
      <arg type="i" direction="out" name="mon_height"/>
    </method>
    <method name="GetMonitorInfo">
      <arg type="s" direction="out" name="json"/>
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
                    // Legacy: return raw global logical coordinates
                    const [x, y] = global.get_pointer();
                    invocation.return_value(new GLib.Variant('(ii)', [x, y]));
                } else if (method === 'GetPositionPixel') {
                    // Return monitor-relative PIXEL coordinates (matching PipeWire screenshot)
                    const [gx, gy] = global.get_pointer();
                    const display = global.display;
                    const monIdx = display.get_current_monitor();
                    const geom = display.get_monitor_geometry(monIdx);
                    const scale = display.get_monitor_scale(monIdx);

                    // Convert global logical -> monitor-relative -> pixel
                    const localX = gx - geom.x;
                    const localY = gy - geom.y;
                    const pixelX = Math.round(localX * scale);
                    const pixelY = Math.round(localY * scale);
                    const monW = Math.round(geom.width * scale);
                    const monH = Math.round(geom.height * scale);

                    invocation.return_value(new GLib.Variant('(iiii)', [pixelX, pixelY, monW, monH]));
                } else if (method === 'GetMonitorInfo') {
                    // Diagnostic: return JSON with all monitor info
                    const display = global.display;
                    const n = display.get_n_monitors();
                    const current = display.get_current_monitor();
                    const [gx, gy] = global.get_pointer();
                    let monitors = [];
                    for (let i = 0; i < n; i++) {
                        const g = display.get_monitor_geometry(i);
                        const s = display.get_monitor_scale(i);
                        monitors.push({
                            index: i,
                            x: g.x, y: g.y, width: g.width, height: g.height,
                            scale: s,
                            pixel_width: Math.round(g.width * s),
                            pixel_height: Math.round(g.height * s),
                        });
                    }
                    const info = JSON.stringify({
                        pointer: {x: gx, y: gy},
                        current_monitor: current,
                        monitors: monitors,
                    });
                    invocation.return_value(new GLib.Variant('(s)', [info]));
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

        console.log('[CUA] Cursor tracker extension v2 enabled');
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

echo "✅ Extension files written to: $EXT_DIR"

# 2. Add to enabled-extensions
CURRENT=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")
if echo "$CURRENT" | grep -q "$EXT_UUID"; then
    echo "✅ Already in enabled-extensions"
else
    if [ "$CURRENT" = "@as []" ]; then
        NEW="['$EXT_UUID']"
    else
        NEW=$(echo "$CURRENT" | sed "s/]/, '$EXT_UUID']/")
    fi
    gsettings set org.gnome.shell enabled-extensions "$NEW"
    echo "✅ Added to enabled-extensions"
fi

# GNOME Shell can keep old extension metadata cached in the running session.
# If the extension is reported as OUT OF DATE even after writing metadata.json,
# briefly disable version validation to force GNOME to re-read the extension,
# then restore the normal validation setting.
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
    EXT_INFO="$(gnome-extensions info "$EXT_UUID" 2>/dev/null || true)"
    if echo "$EXT_INFO" | grep -q "State: OUT OF DATE"; then
        echo "⚠️  GNOME still has old extension metadata cached; refreshing live session..."
        OLD_VALIDATION="$(gsettings get org.gnome.shell disable-extension-version-validation 2>/dev/null || echo false)"
        gsettings set org.gnome.shell disable-extension-version-validation true 2>/dev/null || true
        gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
        gsettings set org.gnome.shell disable-extension-version-validation "$OLD_VALIDATION" 2>/dev/null || true
    fi
fi

echo ""
echo "   Verify cursor tracking:"
echo "     gdbus call --session \\"
echo "       --dest org.cua.CursorTracker \\"
echo "       --object-path /org/cua/CursorTracker \\"
echo "       --method org.cua.CursorTracker.GetPositionPixel"
echo ""
echo "   If verification fails, log out and log back in once."
echo ""
