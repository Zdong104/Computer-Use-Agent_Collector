"""
Wayland-compatible input controller using evdev/uinput.

Creates virtual mouse (absolute pointer) and keyboard devices at the kernel level,
allowing input injection into native Wayland windows (unlike pyautogui which only
works with X11).

Coordinates are given in screenshot space (e.g. 1920x1080) and mapped to the
target monitor's physical region on the full virtual screen.
"""

import time
import subprocess
import re
import evdev
from evdev import UInput, ecodes, AbsInfo

# Character to (keycode, needs_shift) mapping
_CHAR_KEYMAP = {}

# Lowercase letters
for c in 'abcdefghijklmnopqrstuvwxyz':
    _CHAR_KEYMAP[c] = (getattr(ecodes, f"KEY_{c.upper()}"), False)

# Uppercase letters
for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    _CHAR_KEYMAP[c] = (getattr(ecodes, f"KEY_{c.upper()}"), True)

# Numbers
for c in '1234567890':
    _CHAR_KEYMAP[c] = (getattr(ecodes, f"KEY_{c}"), False)

# Shifted number row
_SHIFT_NUM = {'!': ecodes.KEY_1, '@': ecodes.KEY_2, '#': ecodes.KEY_3,
              '$': ecodes.KEY_4, '%': ecodes.KEY_5, '^': ecodes.KEY_6,
              '&': ecodes.KEY_7, '*': ecodes.KEY_8, '(': ecodes.KEY_9,
              ')': ecodes.KEY_0}
for c, kc in _SHIFT_NUM.items():
    _CHAR_KEYMAP[c] = (kc, True)

# Punctuation / symbols
_PUNCT = {
    ' ': (ecodes.KEY_SPACE, False),
    '\t': (ecodes.KEY_TAB, False),
    '\n': (ecodes.KEY_ENTER, False),
    '-': (ecodes.KEY_MINUS, False),
    '=': (ecodes.KEY_EQUAL, False),
    '[': (ecodes.KEY_LEFTBRACE, False),
    ']': (ecodes.KEY_RIGHTBRACE, False),
    '\\': (ecodes.KEY_BACKSLASH, False),
    ';': (ecodes.KEY_SEMICOLON, False),
    "'": (ecodes.KEY_APOSTROPHE, False),
    '`': (ecodes.KEY_GRAVE, False),
    ',': (ecodes.KEY_COMMA, False),
    '.': (ecodes.KEY_DOT, False),
    '/': (ecodes.KEY_SLASH, False),
    '_': (ecodes.KEY_MINUS, True),
    '+': (ecodes.KEY_EQUAL, True),
    '{': (ecodes.KEY_LEFTBRACE, True),
    '}': (ecodes.KEY_RIGHTBRACE, True),
    '|': (ecodes.KEY_BACKSLASH, True),
    ':': (ecodes.KEY_SEMICOLON, True),
    '"': (ecodes.KEY_APOSTROPHE, True),
    '~': (ecodes.KEY_GRAVE, True),
    '<': (ecodes.KEY_COMMA, True),
    '>': (ecodes.KEY_DOT, True),
    '?': (ecodes.KEY_SLASH, True),
}
_CHAR_KEYMAP.update(_PUNCT)

# Named key to keycode mapping (for press_key / hotkey)
_NAMED_KEYS = {
    'enter': ecodes.KEY_ENTER, 'return': ecodes.KEY_ENTER,
    'tab': ecodes.KEY_TAB, 'space': ecodes.KEY_SPACE,
    'backspace': ecodes.KEY_BACKSPACE, 'delete': ecodes.KEY_DELETE,
    'escape': ecodes.KEY_ESC, 'esc': ecodes.KEY_ESC,
    'up': ecodes.KEY_UP, 'down': ecodes.KEY_DOWN,
    'left': ecodes.KEY_LEFT, 'right': ecodes.KEY_RIGHT,
    'home': ecodes.KEY_HOME, 'end': ecodes.KEY_END,
    'pageup': ecodes.KEY_PAGEUP, 'pagedown': ecodes.KEY_PAGEDOWN,
    'shift': ecodes.KEY_LEFTSHIFT, 'ctrl': ecodes.KEY_LEFTCTRL,
    'control': ecodes.KEY_LEFTCTRL, 'alt': ecodes.KEY_LEFTALT,
    'super': ecodes.KEY_LEFTMETA, 'win': ecodes.KEY_LEFTMETA,
    'capslock': ecodes.KEY_CAPSLOCK,
    'f1': ecodes.KEY_F1, 'f2': ecodes.KEY_F2, 'f3': ecodes.KEY_F3,
    'f4': ecodes.KEY_F4, 'f5': ecodes.KEY_F5, 'f6': ecodes.KEY_F6,
    'f7': ecodes.KEY_F7, 'f8': ecodes.KEY_F8, 'f9': ecodes.KEY_F9,
    'f10': ecodes.KEY_F10, 'f11': ecodes.KEY_F11, 'f12': ecodes.KEY_F12,
}


def _get_virtual_screen_size():
    """Get total virtual screen size from xrandr."""
    try:
        result = subprocess.run(['xrandr', '--current'], capture_output=True, text=True, timeout=5)
        # Parse: "current 7680 x 4560"
        m = re.search(r'current\s+(\d+)\s+x\s+(\d+)', result.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    # Fallback: assume single 3840x2160
    return 3840, 2160


class WaylandInputController:
    """
    Kernel-level input injection for Wayland via evdev/uinput.

    Args:
        monitor_physical_left: X offset of target monitor in physical pixels
        monitor_physical_top: Y offset of target monitor in physical pixels
        monitor_physical_width: Width of target monitor in physical pixels
        monitor_physical_height: Height of target monitor in physical pixels
        frame_width: Width of the captured screenshot (e.g. 1920)
        frame_height: Height of the captured screenshot (e.g. 1080)
    """

    def __init__(self, monitor_physical_left, monitor_physical_top,
                 monitor_physical_width, monitor_physical_height,
                 frame_width=1920, frame_height=1080):
        self.mon_left = monitor_physical_left
        self.mon_top = monitor_physical_top
        self.mon_w = monitor_physical_width
        self.mon_h = monitor_physical_height
        self.frame_w = frame_width
        self.frame_h = frame_height

        # Scale factors: screenshot coords → physical coords on the monitor
        self.scale_x = monitor_physical_width / frame_width if frame_width > 0 else 1.0
        self.scale_y = monitor_physical_height / frame_height if frame_height > 0 else 1.0

        # Get total virtual screen size for absolute coordinate mapping
        self.total_w, self.total_h = _get_virtual_screen_size()

        # Create virtual mouse (absolute pointer)
        ABS_MAX = 32767
        cap_mouse = {
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_Y, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
            ],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
            ecodes.EV_REL: [ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
        }
        self.mouse = UInput(cap_mouse, name='cua-virtual-mouse', vendor=0x1234)

        # Create virtual keyboard
        cap_kbd = {
            ecodes.EV_KEY: list(range(1, 249)),  # All standard keycodes
        }
        self.kbd = UInput(cap_kbd, name='cua-virtual-keyboard', vendor=0x1234)

        self.ABS_MAX = ABS_MAX
        print(f"🎮 WaylandInputController initialized:")
        print(f"   Monitor: {self.mon_w}x{self.mon_h} at ({self.mon_left},{self.mon_top})")
        print(f"   Frame: {self.frame_w}x{self.frame_h}")
        print(f"   Scale: ({self.scale_x:.2f}, {self.scale_y:.2f})")
        print(f"   Virtual screen: {self.total_w}x{self.total_h}")

    def _screenshot_to_abs(self, x, y):
        """Convert screenshot coords to uinput absolute coords."""
        # Screenshot → physical
        phys_x = x * self.scale_x + self.mon_left
        phys_y = y * self.scale_y + self.mon_top
        # Physical → absolute [0, ABS_MAX]
        abs_x = int(phys_x / self.total_w * self.ABS_MAX)
        abs_y = int(phys_y / self.total_h * self.ABS_MAX)
        return abs_x, abs_y

    def move_to(self, x, y):
        """Move pointer to screenshot coordinates (x, y)."""
        abs_x, abs_y = self._screenshot_to_abs(x, y)
        self.mouse.write(ecodes.EV_ABS, ecodes.ABS_X, abs_x)
        self.mouse.write(ecodes.EV_ABS, ecodes.ABS_Y, abs_y)
        self.mouse.syn()

    def click(self, x, y, button='left'):
        """Click at screenshot coordinates."""
        btn = {'left': ecodes.BTN_LEFT, 'right': ecodes.BTN_RIGHT,
               'middle': ecodes.BTN_MIDDLE}.get(button, ecodes.BTN_LEFT)
        self.move_to(x, y)
        time.sleep(0.05)
        self.mouse.write(ecodes.EV_KEY, btn, 1)  # press
        self.mouse.syn()
        time.sleep(0.05)
        self.mouse.write(ecodes.EV_KEY, btn, 0)  # release
        self.mouse.syn()

    def double_click(self, x, y, button='left'):
        """Double-click at screenshot coordinates."""
        self.click(x, y, button)
        time.sleep(0.05)
        self.click(x, y, button)

    def drag(self, x1, y1, x2, y2, button='left', duration=0.5):
        """Drag from (x1,y1) to (x2,y2) in screenshot coordinates."""
        btn = {'left': ecodes.BTN_LEFT, 'right': ecodes.BTN_RIGHT}.get(button, ecodes.BTN_LEFT)
        self.move_to(x1, y1)
        time.sleep(0.05)
        self.mouse.write(ecodes.EV_KEY, btn, 1)
        self.mouse.syn()

        steps = max(int(duration / 0.02), 10)
        for i in range(1, steps + 1):
            t = i / steps
            ix = x1 + (x2 - x1) * t
            iy = y1 + (y2 - y1) * t
            self.move_to(ix, iy)
            time.sleep(duration / steps)

        self.mouse.write(ecodes.EV_KEY, btn, 0)
        self.mouse.syn()

    def scroll(self, x=None, y=None, dx=0, dy=0):
        """Scroll at optional position. dy>0 = scroll up, dy<0 = scroll down."""
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.05)
        if dy != 0:
            self.mouse.write(ecodes.EV_REL, ecodes.REL_WHEEL, dy)
        if dx != 0:
            self.mouse.write(ecodes.EV_REL, ecodes.REL_HWHEEL, dx)
        self.mouse.syn()

    def _resolve_key(self, key_name):
        """Resolve a key name to a keycode."""
        kn = key_name.lower().strip()
        if kn in _NAMED_KEYS:
            return _NAMED_KEYS[kn]
        if kn in _CHAR_KEYMAP:
            return _CHAR_KEYMAP[kn][0]
        # Try ecodes lookup
        attr = f'KEY_{kn.upper()}'
        if hasattr(ecodes, attr):
            return getattr(ecodes, attr)
        raise ValueError(f"Unknown key: {key_name}")

    def press_key(self, key_name):
        """Press and release a named key (e.g. 'enter', 'tab', 'f5')."""
        kc = self._resolve_key(key_name)
        self.kbd.write(ecodes.EV_KEY, kc, 1)
        self.kbd.syn()
        time.sleep(0.05)
        self.kbd.write(ecodes.EV_KEY, kc, 0)
        self.kbd.syn()

    def hotkey(self, *keys):
        """Press a key combination (e.g. hotkey('ctrl', 'l'))."""
        keycodes = [self._resolve_key(k) for k in keys]
        # Press all
        for kc in keycodes:
            self.kbd.write(ecodes.EV_KEY, kc, 1)
            self.kbd.syn()
            time.sleep(0.02)
        time.sleep(0.05)
        # Release all in reverse
        for kc in reversed(keycodes):
            self.kbd.write(ecodes.EV_KEY, kc, 0)
            self.kbd.syn()
            time.sleep(0.02)

    def write(self, text):
        """Type a string character by character."""
        for char in text:
            if char in _CHAR_KEYMAP:
                kc, needs_shift = _CHAR_KEYMAP[char]
                if needs_shift:
                    self.kbd.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                    self.kbd.syn()
                    time.sleep(0.01)
                self.kbd.write(ecodes.EV_KEY, kc, 1)
                self.kbd.syn()
                time.sleep(0.02)
                self.kbd.write(ecodes.EV_KEY, kc, 0)
                self.kbd.syn()
                if needs_shift:
                    time.sleep(0.01)
                    self.kbd.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                    self.kbd.syn()
                time.sleep(0.03)
            else:
                print(f"Warning: no keycode mapping for character '{char}'")

    def key_down(self, key_name):
        """Press (hold) a named key without releasing it."""
        kc = self._resolve_key(key_name)
        self.kbd.write(ecodes.EV_KEY, kc, 1)
        self.kbd.syn()

    def key_up(self, key_name):
        """Release a held named key."""
        kc = self._resolve_key(key_name)
        self.kbd.write(ecodes.EV_KEY, kc, 0)
        self.kbd.syn()

    def mouse_down(self, x=None, y=None, button='left'):
        """Press (hold) a mouse button, optionally moving to (x, y) first."""
        btn = {'left': ecodes.BTN_LEFT, 'right': ecodes.BTN_RIGHT,
               'middle': ecodes.BTN_MIDDLE}.get(button, ecodes.BTN_LEFT)
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.05)
        self.mouse.write(ecodes.EV_KEY, btn, 1)
        self.mouse.syn()

    def mouse_up(self, x=None, y=None, button='left'):
        """Release a held mouse button, optionally moving to (x, y) first."""
        btn = {'left': ecodes.BTN_LEFT, 'right': ecodes.BTN_RIGHT,
               'middle': ecodes.BTN_MIDDLE}.get(button, ecodes.BTN_LEFT)
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.05)
        self.mouse.write(ecodes.EV_KEY, btn, 0)
        self.mouse.syn()

    def close(self):
        """Close the virtual devices."""
        try:
            self.mouse.close()
        except Exception:
            pass
        try:
            self.kbd.close()
        except Exception:
            pass
