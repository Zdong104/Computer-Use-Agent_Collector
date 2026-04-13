"""
Platform-specific backends for screenshot, cursor tracking, and input monitoring.
Supports: Windows, macOS, Linux (X11), Linux (Wayland/GNOME)
"""
import os
import sys
import re
import time
import platform
import subprocess
import shutil
import threading
from typing import Tuple, Optional, Dict, Callable

# ============================================================
# Platform Detection
# ============================================================

def detect_platform() -> Tuple[str, str]:
    """Returns (os_name, session_type)."""
    system = platform.system().lower()
    if system == 'windows':
        return 'windows', 'windows'
    elif system == 'darwin':
        return 'macos', 'macos'
    elif system == 'linux':
        session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if session_type == 'wayland':
            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
            if 'gnome' in desktop:
                return 'linux', 'wayland-gnome'
            elif 'kde' in desktop:
                return 'linux', 'wayland-kde'
            elif 'sway' in desktop:
                return 'linux', 'wayland-sway'
            return 'linux', 'wayland'
        return 'linux', 'x11'
    return system, 'unknown'


OS_NAME, SESSION_TYPE = detect_platform()


def get_screen_resolution() -> Tuple[int, int]:
    """Get the resolution of the current/primary monitor (not the combined virtual desktop).

    On multi-monitor setups the combined virtual desktop (e.g. 9840x3840) differs
    from the individual monitor resolution (e.g. 3840x2400).  Since screenshots
    and cursor coordinates are relative to a single monitor, we need the latter.
    """
    try:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            import mss
            with mss.mss() as sct:
                # monitors[0] is the combined virtual screen; monitors[1] is the primary
                if len(sct.monitors) > 1:
                    m = sct.monitors[1]  # primary monitor
                else:
                    m = sct.monitors[0]
                return (m['width'], m['height'])
    except Exception:
        pass

    # Wayland: try multiple methods to get the primary monitor resolution
    # Method 1: Parse individual monitor outputs from xrandr
    try:
        r = subprocess.run(['xrandr', '--current'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            best_res = None
            in_connected_output = False
            is_primary = False
            for line in r.stdout.splitlines():
                if ' connected' in line:
                    in_connected_output = True
                    is_primary = 'primary' in line
                    header_match = re.search(r'(\d{3,5})x(\d{3,5})\+', line)
                    if header_match:
                        res = (int(header_match.group(1)), int(header_match.group(2)))
                        if is_primary or best_res is None:
                            best_res = res
                            if is_primary:
                                break
                elif ' disconnected' in line:
                    in_connected_output = False
                    is_primary = False
                elif in_connected_output and '*' in line:
                    mode_match = re.match(r'\s+(\d{3,5})x(\d{3,5})', line)
                    if mode_match:
                        res = (int(mode_match.group(1)), int(mode_match.group(2)))
                        if is_primary or best_res is None:
                            best_res = res
                            if is_primary:
                                break
            if best_res:
                return best_res
    except Exception:
        pass

    # Method 2: Try gnome-randr (available on some GNOME Wayland setups)
    try:
        r = subprocess.run(['gnome-randr'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            # Look for active mode line, e.g. "  3840x2400@60.000  *"
            for line in r.stdout.splitlines():
                if '*' in line:
                    m = re.search(r'(\d{3,5})x(\d{3,5})', line)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass

    # Method 3: Try wlr-randr (wlroots compositors like Sway)
    try:
        r = subprocess.run(['wlr-randr'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if 'current' in line.lower():
                    m = re.search(r'(\d{3,5})x(\d{3,5})', line)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass

    return (1920, 1080)


# ============================================================
# Screenshot Backend
# ============================================================

class Screenshotter:
    """Cross-platform screenshot utility with automatic backend selection."""

    def __init__(self):
        self._wayland_backend = None
        self.method = self._detect_method()
        print(f"  📷 Screenshot backend: {self.method}")

    def _detect_method(self) -> str:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            return 'mss'
        # Wayland: use PipeWire screencast (one-time approval, then free capture)
        if SESSION_TYPE.startswith('wayland'):
            return 'pipewire'
        # Fallback
        if shutil.which('grim'):
            return 'grim'
        return 'mss'

    def init_wayland(self):
        """Initialize Wayland screenshot backend. Call after startup banner."""
        if self.method == 'pipewire':
            from screenshot_wayland import create_wayland_screenshotter
            self._wayland_backend = create_wayland_screenshotter()
            self.method = 'pipewire' if self._wayland_backend else 'mss'

    def capture(self, output_path: str) -> bool:
        """Take a screenshot and save to output_path. Returns True on success."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        try:
            if self.method == 'pipewire' and self._wayland_backend:
                return self._wayland_backend.capture(output_path)
            elif self.method == 'mss':
                return self._capture_mss(output_path)
            elif self.method == 'grim':
                return subprocess.run(['grim', output_path], capture_output=True, timeout=10).returncode == 0
            elif self.method == 'gnome-screenshot-cli':
                return subprocess.run(['gnome-screenshot', '-f', output_path], capture_output=True, timeout=10).returncode == 0
        except Exception as e:
            print(f"  ❌ Screenshot error ({self.method}): {e}")
        return False

    def stop(self):
        """Clean up screenshot resources."""
        if self._wayland_backend:
            self._wayland_backend.stop()

    def _capture_mss(self, output_path: str) -> bool:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(output_path, "PNG")
        return True


# ============================================================
# Cursor Position Backend
# ============================================================

class CursorTracker:
    """Cross-platform cursor position tracking.

    On Wayland GNOME, cursor coordinates from global.get_pointer() are in the
    global *logical* coordinate space spanning all monitors.  Screenshots are
    captured at the monitor's *native pixel* resolution.

    With the v2 CUA extension (GetPositionPixel), coordinates are already
    converted to monitor-relative pixel space.  For fallback methods (gnome-eval,
    v1 extension), we read the primary monitor's offset and scale from
    monitors.xml and transform coordinates in Python.

    Detection order on Wayland:
      1. cursor-tracker@cua extension v2 (GetPositionPixel – best)
      2. cursor-tracker@cua extension v1 (GetPosition + Python transform)
      3. org.gnome.Shell.Eval            (+ Python transform)
      4. pynput                          (broken on Wayland, last resort)
    """

    def __init__(self):
        self._pixel_method = False  # True if using GetPositionPixel
        self._monitor_native_res = None  # (w, h) in native pixels
        # Monitor geometry for coordinate transform (v1 fallback)
        self._monitor_offset = (0, 0)  # (x, y) logical offset in compositor
        self._monitor_logical_size = None  # (w, h) logical size
        self._monitor_scale = 1.0  # scale factor (native / logical)
        self._load_monitor_geometry()

        self.method = self._detect_method()
        print(f"  🖱️  Cursor tracking: {self.method}"
              + (" (pixel coords)" if self._pixel_method else ""))
        if not self._pixel_method and self._monitor_scale != 1.0:
            print(f"  📐 Monitor offset: {self._monitor_offset}, "
                  f"scale: {self._monitor_scale}x "
                  f"(native: {self._monitor_native_res})")
        if self.method == 'pynput' and SESSION_TYPE.startswith('wayland'):
            print("  ⚠️  pynput cursor tracking is unreliable on Wayland!")
            print("     Install the CUA extension: see README or run setup_extension.sh")

    def _load_monitor_geometry(self):
        """Detect primary monitor geometry from the running GNOME compositor.

        Uses org.gnome.Mutter.DisplayConfig.GetCurrentState D-Bus API which
        returns the live monitor layout, including:
         - logical offset (x, y) of each monitor in compositor space
         - scale factor (e.g. 2.0 for HiDPI)
         - primary flag
         - connected output names

        Falls back to parsing monitors.xml if D-Bus is unavailable.
        """
        if not SESSION_TYPE.startswith('wayland'):
            return

        # Try live D-Bus query first (most reliable)
        if self._load_from_mutter_dbus():
            return

        # Fallback: parse monitors.xml
        self._load_from_monitors_xml()

    def _load_from_mutter_dbus(self) -> bool:
        """Load monitor geometry from org.gnome.Mutter.DisplayConfig.GetCurrentState."""
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Mutter.DisplayConfig',
                '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', 'org.gnome.Mutter.DisplayConfig.GetCurrentState',
            ], capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return False

            text = r.stdout

            # Parse logical monitors section: [(x, y, scale, transform, primary, [(connector, vendor, product, serial)], props), ...]
            # We look for the primary monitor (has 'true' after the transform uint32)
            # Pattern: (x, y, scale, uint32 N, true, [('connector', ...
            primary_match = re.search(
                r'\((\d+),\s*(\d+),\s*([\d.]+),\s*(?:uint32\s+)?\d+,\s*true,\s*\[\(\'([^\']+)\'',
                text
            )
            if not primary_match:
                return False

            offset_x = int(primary_match.group(1))
            offset_y = int(primary_match.group(2))
            scale = float(primary_match.group(3))
            connector = primary_match.group(4)

            self._monitor_offset = (offset_x, offset_y)
            self._monitor_scale = scale

            # Now find the monitor mode resolution for this connector
            # Monitors section has: ('connector', 'vendor', 'product', 'serial'), [modes...], properties
            # Find the active mode for this connector by looking for a mode with 'is-current' property
            # For simplicity, parse the connector's mode width/height from the output
            # The mode entries look like: ('WxH@rate', W, H, rate, preferred_scale, ...)
            # Find the connector section and its first (native) mode
            connector_pattern = rf"'{re.escape(connector)}'"
            conn_idx = text.find(connector_pattern)
            if conn_idx > 0:
                # Find modes after this connector - look for WxH patterns
                # The first mode after 'is-builtin' property is usually the list
                # Find active mode width/height - look for resolution modes
                modes_text = text[conn_idx:]
                # Find the first mode entry: ('3840x2400@60.002', 3840, 2400, ...)
                mode_match = re.search(r"\('(\d+)x(\d+)@[\d.]+',\s*\d+,\s*\d+", modes_text)
                if mode_match:
                    native_w = int(mode_match.group(1))
                    native_h = int(mode_match.group(2))
                    self._monitor_native_res = (native_w, native_h)

            # If we couldn't parse native resolution from modes, compute it from scale
            if not self._monitor_native_res and scale > 1.0:
                # We need the logical size - try to compute from scale
                # Look at xrandr for the mode if available
                native = self._get_native_resolution_xrandr(connector)
                if native:
                    self._monitor_native_res = native
                    # Recalculate scale based on native vs logical
                    # logical_width = native_width / scale
                else:
                    # Can't determine native, but we have offset and scale
                    pass

            print(f"  📐 Primary monitor ({connector}): "
                  f"offset=({offset_x},{offset_y}), scale={scale}x"
                  + (f", native={self._monitor_native_res[0]}×{self._monitor_native_res[1]}"
                     if self._monitor_native_res else ""))
            return True

        except Exception as e:
            print(f"  ⚠️  Mutter D-Bus query failed: {e}")
            return False

    def _load_from_monitors_xml(self):
        """Fallback: parse monitors.xml to get primary monitor geometry."""
        import xml.etree.ElementTree as ET
        monitors_xml = os.path.expanduser('~/.config/monitors.xml')
        if not os.path.isfile(monitors_xml):
            return

        try:
            tree = ET.parse(monitors_xml)
            root = tree.getroot()

            # Get currently connected monitors from sysfs
            connected = set()
            drm_dir = '/sys/class/drm'
            if os.path.isdir(drm_dir):
                for entry in os.listdir(drm_dir):
                    status_file = os.path.join(drm_dir, entry, 'status')
                    if os.path.isfile(status_file):
                        try:
                            with open(status_file) as f:
                                if 'connected' == f.read().strip():
                                    # Extract connector name: card0-DP-1 -> DP-1
                                    parts = entry.split('-', 1)
                                    if len(parts) > 1:
                                        connected.add(parts[1])
                        except Exception:
                            pass

            # Find the configuration matching currently connected monitors
            best_config = None
            for config in root.findall('configuration'):
                config_connectors = set()
                for lmon in config.findall('logicalmonitor'):
                    conn_el = lmon.find('monitor/monitorspec/connector')
                    if conn_el is not None:
                        config_connectors.add(conn_el.text)
                # Check for disabled monitors too
                for disabled in config.findall('disabled'):
                    conn_el = disabled.find('monitorspec/connector')
                    if conn_el is not None:
                        config_connectors.add(conn_el.text)

                if config_connectors == connected or (connected and config_connectors.issuperset(connected)):
                    best_config = config
                    break

            if not best_config:
                # Fall back to last config
                configs = root.findall('configuration')
                if configs:
                    best_config = configs[-1]

            if not best_config:
                return

            # Find the primary monitor
            for lmon in best_config.findall('logicalmonitor'):
                primary_el = lmon.find('primary')
                if primary_el is None or primary_el.text != 'yes':
                    continue

                x = int(lmon.find('x').text)
                y = int(lmon.find('y').text)
                self._monitor_offset = (x, y)

                scale_el = lmon.find('scale')
                config_scale = float(scale_el.text) if scale_el is not None else 1.0

                mode = lmon.find('monitor/mode')
                if mode is not None:
                    logical_w = int(mode.find('width').text)
                    logical_h = int(mode.find('height').text)
                    self._monitor_logical_size = (logical_w, logical_h)

                    connector = lmon.find('monitor/monitorspec/connector')
                    if connector is not None:
                        native = self._get_native_resolution_xrandr(connector.text)
                        if native:
                            self._monitor_native_res = native
                            self._monitor_scale = native[0] / logical_w
                        elif config_scale != 1.0:
                            self._monitor_scale = config_scale
                            self._monitor_native_res = (
                                int(logical_w * config_scale),
                                int(logical_h * config_scale),
                            )
                        else:
                            self._monitor_native_res = (logical_w, logical_h)
                break
        except Exception as e:
            print(f"  ⚠️  Could not parse monitors.xml: {e}")

    def _get_native_resolution_xrandr(self, connector: str) -> Optional[Tuple[int, int]]:
        """Try to get the native (max) resolution for a display connector via xrandr."""
        try:
            r = subprocess.run(['xrandr', '--current'], capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                return None
            in_connector = False
            for line in r.stdout.splitlines():
                if line.startswith(connector + ' '):
                    in_connector = True
                    continue
                elif not line.startswith(' ') and in_connector:
                    break
                elif in_connector:
                    m = re.match(r'\s+(\d+)x(\d+)', line)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        return None

    def _detect_method(self) -> str:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            return 'pynput'
        # Wayland: try extension first, then gnome-eval, then pynput
        if 'gnome' in SESSION_TYPE:
            if self._test_cua_pixel():
                self._pixel_method = True
                return 'cua-extension'
            if self._test_cua_extension():
                return 'cua-extension'
            if self._test_gnome_eval():
                return 'gnome-eval'
        return 'pynput'

    def _test_cua_pixel(self) -> bool:
        """Test if the v2 extension with GetPositionPixel is available."""
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.cua.CursorTracker',
                '--object-path', '/org/cua/CursorTracker',
                '--method', 'org.cua.CursorTracker.GetPositionPixel',
            ], capture_output=True, text=True, timeout=2)
            if r.returncode == 0 and '(' in r.stdout:
                # Parse the native monitor resolution from the response
                nums = re.findall(r'-?\d+', r.stdout)
                if len(nums) >= 4:
                    self._monitor_native_res = (int(nums[2]), int(nums[3]))
                    print(f"  📐 Monitor native resolution from extension: "
                          f"{self._monitor_native_res[0]}×{self._monitor_native_res[1]}")
                return True
        except Exception:
            pass
        return False

    def _test_cua_extension(self) -> bool:
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.cua.CursorTracker',
                '--object-path', '/org/cua/CursorTracker',
                '--method', 'org.cua.CursorTracker.GetPosition',
            ], capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and '(' in r.stdout
        except Exception:
            return False

    def _test_gnome_eval(self) -> bool:
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Shell',
                '--object-path', '/org/gnome/Shell',
                '--method', 'org.gnome.Shell.Eval',
                'let [x,y]=global.get_pointer(); x+","+y'
            ], capture_output=True, text=True, timeout=3)
            # Eval returns (true, 'x,y') on success, (false, '') on failure
            return r.returncode == 0 and "(true," in r.stdout
        except Exception:
            return False

    def get_monitor_native_resolution(self) -> Optional[Tuple[int, int]]:
        """Return the native pixel resolution detected, or None."""
        return self._monitor_native_res

    def _transform_to_pixel(self, global_x: int, global_y: int) -> Tuple[int, int]:
        """Transform global logical coordinates to monitor-relative pixel coordinates."""
        # Subtract monitor offset to get monitor-relative logical coords
        local_x = global_x - self._monitor_offset[0]
        local_y = global_y - self._monitor_offset[1]
        # Scale to native pixel coordinates
        pixel_x = int(round(local_x * self._monitor_scale))
        pixel_y = int(round(local_y * self._monitor_scale))
        return (pixel_x, pixel_y)

    def get_position(self) -> Tuple[int, int]:
        if self.method == 'cua-extension':
            if self._pixel_method:
                return self._get_cua_pixel()
            return self._get_cua_extension_transformed()
        if self.method == 'gnome-eval':
            return self._get_gnome_eval_transformed()
        return self._get_pynput()

    def _get_cua_pixel(self) -> Tuple[int, int]:
        """Get monitor-relative pixel coordinates from v2 extension."""
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.cua.CursorTracker',
                '--object-path', '/org/cua/CursorTracker',
                '--method', 'org.cua.CursorTracker.GetPositionPixel',
            ], capture_output=True, text=True, timeout=1)
            if r.returncode == 0:
                nums = re.findall(r'-?\d+', r.stdout)
                if len(nums) >= 4:
                    # Update cached native resolution
                    self._monitor_native_res = (int(nums[2]), int(nums[3]))
                    return (int(nums[0]), int(nums[1]))
        except Exception:
            pass
        return (0, 0)

    def _get_cua_extension_transformed(self) -> Tuple[int, int]:
        """Get global coords from v1 extension, then transform to pixel coords."""
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.cua.CursorTracker',
                '--object-path', '/org/cua/CursorTracker',
                '--method', 'org.cua.CursorTracker.GetPosition',
            ], capture_output=True, text=True, timeout=1)
            if r.returncode == 0:
                nums = re.findall(r'-?\d+', r.stdout)
                if len(nums) >= 2:
                    return self._transform_to_pixel(int(nums[0]), int(nums[1]))
        except Exception:
            pass
        return (0, 0)

    def _get_gnome_eval_transformed(self) -> Tuple[int, int]:
        """Get global coords from Shell.Eval, then transform to pixel coords."""
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Shell',
                '--object-path', '/org/gnome/Shell',
                '--method', 'org.gnome.Shell.Eval',
                'let [x,y]=global.get_pointer(); x+","+y'
            ], capture_output=True, text=True, timeout=2)
            if r.returncode == 0 and "(true," in r.stdout:
                # Output: (true, '1234,567')
                match = re.search(r"'(\d+),(\d+)'", r.stdout)
                if match:
                    return self._transform_to_pixel(
                        int(match.group(1)), int(match.group(2)))
        except Exception:
            pass
        return (0, 0)

    def _get_pynput(self) -> Tuple[int, int]:
        try:
            from pynput.mouse import Controller
            return Controller().position
        except Exception:
            return (0, 0)


# ============================================================
# Input Monitor - Wayland (evdev)
# ============================================================

class WaylandInputMonitor:
    """Monitor keyboard and mouse input via evdev on Wayland."""

    def __init__(self, callbacks: Dict[str, Callable]):
        self.callbacks = callbacks
        self._running = False
        self._threads = []
        self._ctrl_pressed = False
        self._devices = []

    def start(self):
        import evdev
        self._running = True
        all_devices = [evdev.InputDevice(path) for path in evdev.list_devices()]

        for dev in all_devices:
            caps = dev.capabilities(verbose=True)
            is_keyboard = False
            is_mouse = False

            for key, events in caps.items():
                key_name = key[0] if isinstance(key, tuple) else key
                event_strs = [str(e) for e in events]
                if key_name == 'EV_KEY':
                    if any('KEY_A' in s for s in event_strs):
                        is_keyboard = True
                    if any('BTN_LEFT' in s for s in event_strs):
                        is_mouse = True
                if key_name == 'EV_REL':
                    if any('REL_WHEEL' in s for s in event_strs):
                        is_mouse = True

            if is_keyboard or is_mouse:
                self._devices.append((dev, is_keyboard, is_mouse))
                kind = []
                if is_keyboard:
                    kind.append('kbd')
                if is_mouse:
                    kind.append('mouse')
                print(f"  📡 Monitoring: {dev.name} ({'+'.join(kind)})")

        for dev, is_kbd, is_mouse in self._devices:
            t = threading.Thread(target=self._monitor_device, args=(dev, is_kbd, is_mouse), daemon=True)
            t.start()
            self._threads.append(t)

    def _monitor_device(self, device, is_keyboard: bool, is_mouse: bool):
        import evdev
        from evdev import ecodes

        try:
            for event in device.read_loop():
                if not self._running:
                    break

                if event.type == ecodes.EV_KEY:
                    key_event = evdev.categorize(event)

                    # Track Ctrl
                    if key_event.scancode in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL):
                        self._ctrl_pressed = key_event.keystate != 0

                    # Hotkeys on key-down
                    if key_event.keystate == 1 and self._ctrl_pressed:
                        if key_event.scancode == ecodes.KEY_F8:
                            threading.Thread(target=self.callbacks['on_hotkey_start_task'], daemon=True).start()
                        elif key_event.scancode == ecodes.KEY_F9:
                            threading.Thread(target=self.callbacks['on_hotkey_screenshot'], daemon=True).start()
                        elif key_event.scancode == ecodes.KEY_F12:
                            threading.Thread(target=self.callbacks['on_hotkey_end_task'], daemon=True).start()

                    # ESC (no Ctrl needed) to drop current action
                    if key_event.keystate == 1 and key_event.scancode == ecodes.KEY_ESC:
                        threading.Thread(target=self.callbacks['on_hotkey_drop_action'], daemon=True).start()

                    # Modifiers / Special keys tracking
                    special_keys = {
                        ecodes.KEY_LEFTCTRL: 'ctrl_l',
                        ecodes.KEY_RIGHTCTRL: 'ctrl_r',
                        ecodes.KEY_LEFTSHIFT: 'shift_l',
                        ecodes.KEY_RIGHTSHIFT: 'shift_r',
                        ecodes.KEY_ESC: 'esc',
                        ecodes.KEY_BACKSPACE: 'backspace',
                        ecodes.KEY_ENTER: 'enter',
                        ecodes.KEY_FN: 'fn',
                    }
                    if key_event.scancode in special_keys and key_event.keystate in (0, 1): # 0 is up, 1 is down
                        if 'on_key_event' in self.callbacks:
                            threading.Thread(target=self.callbacks['on_key_event'], args=(special_keys[key_event.scancode], key_event.keystate == 1), daemon=True).start()

                    # Mouse button press/release
                    if is_mouse:
                        btn_map = {
                            ecodes.BTN_LEFT: 'left',
                            ecodes.BTN_RIGHT: 'right',
                            ecodes.BTN_MIDDLE: 'middle',
                        }
                        if key_event.scancode in btn_map and key_event.keystate in (0, 1):
                            if 'on_mouse_button' in self.callbacks:
                                threading.Thread(target=self.callbacks['on_mouse_button'], args=(btn_map[key_event.scancode], key_event.keystate == 1), daemon=True).start()

                elif event.type == ecodes.EV_REL and is_mouse:
                    if event.code in (ecodes.REL_WHEEL, getattr(ecodes, 'REL_WHEEL_HI_RES', 11)):
                        self.callbacks['on_mouse_scroll'](0, event.value)
                    elif event.code in (ecodes.REL_HWHEEL, getattr(ecodes, 'REL_HWHEEL_HI_RES', 12)):
                        self.callbacks['on_mouse_scroll'](event.value, 0)

        except Exception as e:
            if self._running:
                print(f"  ⚠️  Device error ({device.name}): {e}")

    def stop(self):
        self._running = False


# ============================================================
# Input Monitor - pynput (X11 / Windows / macOS)
# ============================================================

class PynputInputMonitor:
    """Monitor keyboard and mouse input via pynput."""

    def __init__(self, callbacks: Dict[str, Callable]):
        self.callbacks = callbacks
        self._ctrl_pressed = False
        self._keyboard_listener = None
        self._mouse_listener = None

    def start(self):
        from pynput import mouse, keyboard

        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll
        )
        self._keyboard_listener.start()
        self._mouse_listener.start()
        print("  📡 Monitoring via pynput (keyboard + mouse)")

    def _on_key_press(self, key):
        from pynput import keyboard
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = True
        if self._ctrl_pressed:
            if key == keyboard.Key.f8:
                threading.Thread(target=self.callbacks['on_hotkey_start_task'], daemon=True).start()
            elif key == keyboard.Key.f9:
                threading.Thread(target=self.callbacks['on_hotkey_screenshot'], daemon=True).start()
            elif key == keyboard.Key.f12:
                threading.Thread(target=self.callbacks['on_hotkey_end_task'], daemon=True).start()
        # ESC (no Ctrl needed) to drop current action
        if getattr(key, 'name', '') == 'esc':
            threading.Thread(target=self.callbacks['on_hotkey_drop_action'], daemon=True).start()

        key_name = self._map_pynput_key(key)
        if key_name and 'on_key_event' in self.callbacks:
            threading.Thread(target=self.callbacks['on_key_event'], args=(key_name, True), daemon=True).start()

    def _on_key_release(self, key):
        from pynput import keyboard
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = False
            
        key_name = self._map_pynput_key(key)
        if key_name and 'on_key_event' in self.callbacks:
            threading.Thread(target=self.callbacks['on_key_event'], args=(key_name, False), daemon=True).start()

    def _map_pynput_key(self, key):
        from pynput import keyboard
        mapping = {
            keyboard.Key.ctrl_l: 'ctrl_l',
            keyboard.Key.ctrl_r: 'ctrl_r',
            keyboard.Key.shift_l: 'shift_l',
            keyboard.Key.shift_r: 'shift_r',
            keyboard.Key.esc: 'esc',
            keyboard.Key.backspace: 'backspace',
            keyboard.Key.enter: 'enter',
        }
        if key in mapping:
            return mapping[key]
        return None

    def _on_click(self, x, y, button, pressed):
        btn_name = button.name if hasattr(button, 'name') else str(button)
        if 'on_mouse_button' in self.callbacks:
            threading.Thread(target=self.callbacks['on_mouse_button'], args=(btn_name, pressed), daemon=True).start()

    def _on_scroll(self, x, y, dx, dy):
        self.callbacks['on_mouse_scroll'](dx, dy)

    def stop(self):
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
