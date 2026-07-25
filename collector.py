"""
CUA Collector — Automated UI Action Data Collector
==================================================
Fully automated collector using the C++ capture engine (`cua_capture`).

Instead of manual Ctrl+F9 for each action, the collector continuously captures
screenshots into a ring buffer and automatically records grouped mouse/keyboard
actions with their pre/post screenshots.

Workflow:
  Ctrl+F8  → Start new task (popup for description)
  [actions are captured automatically while task is active]
  Ctrl+F12 → End current task
  Ctrl+C   → Quit

Architecture:
  C++ Engine (cua_capture.so):
    - Capture thread: 10 FPS PipeWire screenshots → ring buffer
    - Input thread: libevdev mouse/keyboard events
    - Action worker: correlates events with pre/post frames
  Python Layer (this file):
    - Task management (start/end, descriptions)
    - Data persistence (PNG screenshots, JSON metadata)
    - Status overlay UI

Requirements:
  Build the C++ module first:
    cmake -S . -B build
    cmake --build build -j$(nproc)
"""

import os
import re
import sys
import json
import time
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor
from PIL import Image as PILImage
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
import tkinter as tk
try:
    import pyautogui
    pyautogui.PAUSE = 0.05
    pyautogui.FAILSAFE = True
except Exception:
    pyautogui = None

try:
    from wayland_input import WaylandInputController
except ImportError:
    WaylandInputController = None


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(errors='replace')
        except Exception:
            pass

# Add build dir to path for native cua_capture module
build_dir = Path(__file__).parent / 'build'
if build_dir.exists():
    sys.path.insert(0, str(build_dir))
    for config_name in ('Release', 'RelWithDebInfo', 'Debug', 'MinSizeRel'):
        config_dir = build_dir / config_name
        if config_dir.exists():
            sys.path.insert(0, str(config_dir))

CAPTURE_BACKEND = os.environ.get('CUA_CAPTURE_BACKEND', 'native').strip().lower()
if CAPTURE_BACKEND in ('python', 'mss', 'pynput', 'cross-platform', 'cross_platform'):
    import cross_platform_capture as cua_capture
else:
    try:
        import cua_capture
    except ImportError as e:
        err = str(e)
        if 'GLIBCXX' in err or 'libstdc++' in err:
            print("❌ libstdc++ version mismatch (miniconda vs system GCC)!")
            print(f"   Error: {err}")
            print()
            print("   Fix: Use the launcher script instead:")
            print("     ./run.sh")
            print()
            print("   Or run directly with:")
            print("     LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 python collector.py")
        elif 'No module named' in err or 'No such file' in err:
            print("❌ cua_capture module not found!")
            if sys.platform.startswith('win'):
                print("   Build it first:")
                print("     cmake -S . -B build -DPython3_EXECUTABLE=%CD%\\.venv\\Scripts\\python.exe")
                print("     cmake --build build --config Release")
            else:
                print("   Build it first: cmake -S . -B build && cmake --build build -j$(nproc)")
            print("   For X11/Windows/macOS, use ./run_x11.sh, ./run_win.sh, or ./run_mac.sh")
        else:
            print(f"❌ Failed to import cua_capture: {e}")
        sys.exit(1)

CAPTURE_BACKEND_NAME = getattr(cua_capture, 'BACKEND_NAME', 'pipewire+libevdev')


# ============================================================
# Data Models (compatible with V1)
# ============================================================

@dataclass
class ActionRecord:
    id: str
    task_id: str
    task_description: str
    sequence_number: int
    timestamp_before: str
    timestamp_action: str
    timestamp_after: str
    elapsed_since_task_start: float
    pre_screenshot: str
    post_screenshot: str
    action_type: str
    action_coords: Tuple[int, int]
    action_details: dict
    os_name: str
    session_type: str
    screen_resolution: Tuple[int, int]
    pre_degraded: bool = False
    # The capture frame the coords above live in, and the same coords
    # normalized to [0,1] — replayable on any screen size by multiplying
    # with the live frame (norm_x * live_w, norm_y * live_h).
    frame_size: Tuple[int, int] = (0, 0)
    norm_coords: Tuple[float, float] = (0.0, 0.0)


@dataclass
class TaskRecord:
    task_id: str
    description: str
    start_time: str
    end_time: Optional[str]
    os_name: str
    session_type: str
    screen_resolution: Tuple[int, int]
    actions: List[dict] = field(default_factory=list)
    # The capture frame the task's screenshots/coords are in (may differ from
    # screen_resolution on scaled displays, e.g. 4K monitor -> 1920x1080 frame).
    capture_frame: Tuple[int, int] = (0, 0)


@dataclass
class ScreenRegion:
    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    physical_left: int
    physical_top: int
    physical_width: int
    physical_height: int
    primary: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


# ============================================================
# Data Store (same as V1)
# ============================================================

class DataStore:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_task_dir(self, task_id: str) -> Path:
        task_dir = self.base_dir / task_id
        (task_dir / 'screenshots').mkdir(parents=True, exist_ok=True)
        return task_dir

    def screenshot_path(self, task_id: str, name: str) -> str:
        return str(self.base_dir / task_id / 'screenshots' / f'{name}.png')

    def save_task(self, task: TaskRecord):
        task_dir = self.base_dir / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(task_dir / 'task.json', 'w') as f:
            json.dump(asdict(task), f, indent=2, default=str)

    def save_master_index(self, tasks: List[TaskRecord]):
        index_path = self.base_dir / 'index.json'
        records = []
        if index_path.exists():
            try:
                with open(index_path, 'r') as f:
                    records = json.load(f)
            except Exception:
                pass

        existing_ids = {r['task_id']: i for i, r in enumerate(records)}

        for t in tasks:
            rec = {
                'task_id': t.task_id,
                'description': t.description,
                'start_time': t.start_time,
                'end_time': t.end_time,
                'num_actions': len(t.actions),
                'os': t.os_name,
                'session_type': t.session_type,
            }
            if t.task_id in existing_ids:
                records[existing_ids[t.task_id]] = rec
            else:
                records.append(rec)

        with open(index_path, 'w') as f:
            json.dump(records, f, indent=2)


# ============================================================
# Lock Overlay (Tkinter) — for Agent GUI Control
# ============================================================

class LockOverlayWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Agent Control Active")
        self.withdraw()
        
        self.user_aborted = False
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        
        try:
            self.attributes('-alpha', 0.88)
        except Exception:
            pass
            
        self.configure(bg='#0b0f19')  # slate-950
        
        # Center container
        container = tk.Frame(self, bg='#0b0f19')
        container.place(relx=0.5, rely=0.5, anchor='center')
        
        # Beautiful icon/emoji
        self.loader_label = tk.Label(
            container, text="🤖", font=("Helvetica", 72),
            bg='#0b0f19', fg='#3b82f6'
        )
        self.loader_label.pack(pady=20)
        
        # Pulsing status
        self.status_label = tk.Label(
            container, text="Agent Executing Action...",
            font=("Helvetica", 26, "bold"), bg='#0b0f19', fg='#f8fafc'
        )
        self.status_label.pack(pady=10)
        
        self.detail_label = tk.Label(
            container, text="Please do not move mouse or press keys.",
            font=("Helvetica", 14), bg='#0b0f19', fg='#94a3b8'
        )
        self.detail_label.pack(pady=5)
        
        # Cancel info
        self.cancel_label = tk.Label(
            container, text="Press ESC to force reclaim control",
            font=("Helvetica", 12, "italic"), bg='#0b0f19', fg='#ef4444'
        )
        self.cancel_label.pack(pady=25)
        
        self.bind('<Escape>', self.force_release)
        
        # Start the pulse animation
        self.start_pulse()
        
    def start_pulse(self):
        self._pulse_val = 0
        self._pulse_dir = 1
        self._animate_pulse()
        
    def _animate_pulse(self):
        if not self.winfo_exists():
            return
        self._pulse_val += self._pulse_dir * 4
        if self._pulse_val >= 100:
            self._pulse_val = 100
            self._pulse_dir = -1
        elif self._pulse_val <= 0:
            self._pulse_val = 0
            self._pulse_dir = 1
            
        r = int(59 + (168 - 59) * (self._pulse_val / 100.0))
        g = int(130 + (85 - 130) * (self._pulse_val / 100.0))
        b = int(246 + (247 - 246) * (self._pulse_val / 100.0))
        color_hex = f"#{r:02x}{g:02x}{b:02x}"
        
        self.status_label.config(fg=color_hex)
        self.loader_label.config(fg=color_hex)
        
        self.after(40, self._animate_pulse)
        
    def update_geometry(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        
    def show_lock(self, message="Agent Executing Action..."):
        self.update_geometry()
        self.status_label.config(text=message)
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()
        self.update()
        
    def hide_lock(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.withdraw()
        self.update()
        
    def force_release(self, event=None):
        print("🚨 Force release triggered by user!")
        self.user_aborted = True
        self.hide_lock()


# ============================================================
# Status Overlay (Tkinter) — reused from V1
# ============================================================

class StatusOverlay:
    """Always-on-top floating status indicator."""

    COLORS = {
        'IDLE': '#555555',
        'TASK_ACTIVE': '#1565C0',
        'CAPTURING': '#2E7D32',
        'SAVING': '#E65100',
    }
    LABELS = {
        'IDLE': '⏹  Idle',
        'TASK_ACTIVE': '🟢 Task Active',
        'CAPTURING': '🔄 Auto-Capturing',
        'SAVING': '💾 Saving…',
    }

    def __init__(self):
        self._root = None
        self._label = None
        self._thread = None
        self._running = False
        self._pending_state = 'IDLE'
        self._pending_text = None
        self._dialog = None
        self._lock_window = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        time.sleep(0.8)

    def _run_tk(self):
        self._root = tk.Tk()
        self._root.title("CUA Collector")
        self._root.attributes('-topmost', True)
        self._root.overrideredirect(True)

        sw = self._root.winfo_screenwidth()
        self._root.geometry(f'300x36+{sw - 320}+10')
        self._root.configure(bg='#222')

        self._label = tk.Label(
            self._root, text='⏹  Idle',
            font=('monospace', 11, 'bold'),
            fg='white', bg=self.COLORS['IDLE'],
            padx=8, pady=4,
        )
        self._label.pack(fill='both', expand=True)

        try:
            self._root.attributes('-alpha', 0.88)
        except Exception:
            pass

        # Create Lock Window as child of self._root
        self._lock_window = LockOverlayWindow(self._root)

        self._poll()
        self._root.mainloop()

    def show_lock(self, message="Agent Executing Action..."):
        if self._root and self._lock_window:
            self._lock_window.show_lock(message)
            
    def hide_lock(self):
        if self._root and self._lock_window:
            self._lock_window.hide_lock()
            
    @property
    def user_aborted(self):
        if self._lock_window:
            return self._lock_window.user_aborted
        return False
        
    @user_aborted.setter
    def user_aborted(self, value):
        if self._lock_window:
            self._lock_window.user_aborted = value


    def _poll(self):
        if not self._running:
            try:
                self._root.destroy()
            except Exception:
                pass
            return
        if self._pending_text is not None and self._label:
            self._label.config(
                text=self._pending_text,
                bg=self.COLORS.get(self._pending_state, '#555'),
            )
            self._pending_text = None
        if self._root:
            self._root.after(80, self._poll)

    def update_state(self, state: str, extra: str = ''):
        label = self.LABELS.get(state, state)
        if extra:
            label = f"{label} | {extra}"
        self._pending_state = state
        self._pending_text = label

    def stop(self):
        self._running = False

    def ask_description(self) -> Optional[str]:
        self._dialog_result = None
        self._dialog_done = threading.Event()

        if self._root:
            self._root.after(0, self._create_dialog)

        while not self._dialog_done.is_set():
            time.sleep(0.1)

        return self._dialog_result

    def _create_dialog(self):
        import tkinter as tk

        if self._dialog is not None and self._dialog.winfo_exists():
            self._dialog.lift()
            self._dialog.focus_force()
            return

        dialog = tk.Toplevel(self._root)
        self._dialog = dialog
        dialog.title("New Task")
        dialog.attributes('-topmost', True)
        dialog.transient(self._root)

        w, h = 600, 260
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        dialog.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        tk.Label(dialog, text="Enter task description:", font=("Helvetica", 18)).pack(pady=(20, 5))
        tk.Label(dialog, text="(Enter = submit, Shift+Enter = new line)",
                 font=("Helvetica", 10), fg='#666').pack()

        text = tk.Text(dialog, font=("Helvetica", 18), height=3, wrap='word')
        text.pack(pady=10, padx=40, fill='x')

        def close_dialog():
            if not dialog.winfo_exists():
                return
            try:
                dialog.grab_release()
            except Exception:
                pass
            self._dialog = None
            dialog.destroy()

        def submit(e=None):
            content = text.get('1.0', 'end-1c').strip()
            self._dialog_result = content if content else None
            close_dialog()
            self._dialog_done.set()
            return 'break'

        def cancel(e=None):
            self._dialog_result = None
            close_dialog()
            self._dialog_done.set()
            return 'break'

        def handle_text_return(e=None):
            # Match the V1 intent explicitly: Enter submits, Shift+Enter inserts
            # a newline in the description box.
            if e is not None and (e.state & 0x1):
                text.insert('insert', '\n')
                return 'break'
            return submit()

        text.bind('<Return>', handle_text_return)
        text.bind('<KP_Enter>', handle_text_return)
        text.bind('<Shift-Return>', handle_text_return)
        text.bind('<Shift-KP_Enter>', handle_text_return)
        text.bind('<Escape>', cancel)
        dialog.bind('<Escape>', cancel)

        bf = tk.Frame(dialog)
        bf.pack(pady=10)
        tk.Button(bf, text="OK", command=submit, font=("Helvetica", 14), width=10).pack(side='left', padx=10)
        tk.Button(bf, text="Cancel", command=cancel, font=("Helvetica", 14), width=10).pack(side='left', padx=10)

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        dialog.after(0, lambda: (dialog.lift(), text.focus_force(), text.mark_set('insert', 'end')))


# ============================================================
# Platform Detection
# ============================================================

import platform as plat

def detect_platform():
    forced_session = os.environ.get('CUA_SESSION_TYPE', '').strip().lower()
    system = plat.system().lower()
    if system == 'linux':
        session_type = forced_session or os.environ.get('XDG_SESSION_TYPE', '').lower()
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        if session_type == 'wayland':
            if 'gnome' in desktop:
                return 'linux', 'wayland-gnome'
            return 'linux', 'wayland'
        return 'linux', 'x11'
    if system == 'windows':
        return 'windows', forced_session or 'windows'
    if system == 'darwin':
        return 'macos', forced_session or 'macos'
    return system, 'unknown'

OS_NAME, SESSION_TYPE = detect_platform()


# ============================================================
# Screen Selection
# ============================================================

def enumerate_windows_screens() -> List[ScreenRegion]:
    if OS_NAME != 'windows':
        return []

    import ctypes
    import re
    from ctypes import wintypes

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    class RECT(ctypes.Structure):
        _fields_ = [
            ('left', wintypes.LONG),
            ('top', wintypes.LONG),
            ('right', wintypes.LONG),
            ('bottom', wintypes.LONG),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD),
            ('rcMonitor', RECT),
            ('rcWork', RECT),
            ('dwFlags', wintypes.DWORD),
            ('szDevice', wintypes.WCHAR * 32),
        ]

    monitors = []
    get_dpi_for_monitor = None
    try:
        shcore = ctypes.windll.shcore
        get_dpi_for_monitor = shcore.GetDpiForMonitor
        get_dpi_for_monitor.argtypes = [
            wintypes.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
    except Exception:
        get_dpi_for_monitor = None

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rect = info.rcMonitor
            dpi_x = wintypes.UINT(96)
            dpi_y = wintypes.UINT(96)
            if get_dpi_for_monitor is not None:
                try:
                    # MDT_EFFECTIVE_DPI gives the desktop coordinate scale.
                    get_dpi_for_monitor(hmonitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
                except Exception:
                    dpi_x = wintypes.UINT(96)
                    dpi_y = wintypes.UINT(96)
            physical_width = int(rect.right - rect.left)
            physical_height = int(rect.bottom - rect.top)
            logical_width = max(1, round(physical_width * 96 / max(1, int(dpi_x.value))))
            logical_height = max(1, round(physical_height * 96 / max(1, int(dpi_y.value))))
            match = re.search(r'DISPLAY(\d+)$', info.szDevice, re.IGNORECASE)
            display_index = int(match.group(1)) if match else len(monitors) + 1
            monitors.append({
                'index': display_index,
                'name': info.szDevice,
                'left': round(int(rect.left) * 96 / max(1, int(dpi_x.value))),
                'top': round(int(rect.top) * 96 / max(1, int(dpi_y.value))),
                'width': logical_width,
                'height': logical_height,
                'physical_left': int(rect.left),
                'physical_top': int(rect.top),
                'physical_width': physical_width,
                'physical_height': physical_height,
                'primary': bool(info.dwFlags & 1),
            })
        return True

    ctypes.windll.user32.EnumDisplayMonitors(
        None, None, monitor_enum_proc(callback), 0
    )

    monitors.sort(key=lambda item: item['index'])
    return [
        ScreenRegion(**monitor)
        for monitor in monitors
    ]


def choose_capture_screen_gui(monitors: List[ScreenRegion]) -> Optional[ScreenRegion]:
    # Removed: the Tkinter screen picker was redundant with the GNOME portal's
    # own "Share Screen" dialog (which is authoritative on Wayland). Screen
    # selection now comes from the portal via resolve_screen_from_portal(), and
    # X11/Windows fall through to the console prompt in choose_capture_screen().
    return None





def _enumerate_linux_monitors() -> List['ScreenRegion']:
    """Enumerate physical monitors via mss (same ordering the collector uses)."""
    monitors: List[ScreenRegion] = []
    try:
        import mss
        with mss.mss() as sct:
            for idx, m in enumerate(sct.monitors[1:], 1):
                monitors.append(ScreenRegion(
                    index=idx,
                    name=f"Monitor {idx}",
                    left=m['left'],
                    top=m['top'],
                    width=m['width'],
                    height=m['height'],
                    physical_left=m['left'],
                    physical_top=m['top'],
                    physical_width=m['width'],
                    physical_height=m['height'],
                    primary=(idx == 1),
                ))
    except Exception:
        pass
    return monitors


def _mutter_logical_monitors():
    """Return [(logical_x, logical_y, scale), ...] from Mutter, or [] if absent.

    The XDG ScreenCast portal reports the selected monitor's position/size in
    *logical* coordinates, which match Mutter's logical monitor layout exactly.
    """
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        addr = DBusAddress(
            object_path="/org/gnome/Mutter/DisplayConfig",
            bus_name="org.gnome.Mutter.DisplayConfig",
            interface="org.gnome.Mutter.DisplayConfig",
        )
        conn = open_dbus_connection(bus="SESSION")
        try:
            reply = conn.send_and_get_reply(
                new_method_call(addr, "GetCurrentState", "", ())
            )
        finally:
            conn.close()
        _serial, _monitors, logical_monitors, _props = reply.body
        out = []
        for lm in logical_monitors:
            x, y, scale = lm[0], lm[1], lm[2]
            out.append((int(x), int(y), float(scale)))
        return out
    except Exception:
        return None


def resolve_screen_from_portal(engine, monitors: Optional[List['ScreenRegion']] = None
                               ) -> Optional['ScreenRegion']:
    """Build the capture ScreenRegion from the portal-selected monitor.

    Reads the portal geometry the engine learned during init_portal() (logical
    position/size), maps it to a physical mss monitor, and returns that
    ScreenRegion — so capture and uinput injection refer to the same screen the
    user picked in the GNOME dialog. Returns None if geometry is unavailable.
    """
    px = getattr(engine, 'portal_position_x', lambda: -1)
    # engine exposes read-only properties, not methods
    try:
        px = engine.portal_position_x
        py = engine.portal_position_y
        pw = engine.portal_size_w
        ph = engine.portal_size_h
    except Exception:
        return None
    if px < 0 or py < 0 or pw <= 0 or ph <= 0:
        return None

    if monitors is None:
        monitors = _enumerate_linux_monitors()
    if not monitors:
        return None

    # Map the portal's logical position → a physical mss monitor.
    # Strategy 1: use Mutter logical layout to find which logical monitor sits at
    # the portal position, then match its physical rect against mss by scaling.
    logical = _mutter_logical_monitors()
    if logical:
        # Find the logical monitor whose origin matches the portal position.
        match = None
        for (lx, ly, scale) in logical:
            if abs(lx - px) <= 2 and abs(ly - py) <= 2:
                match = (lx, ly, scale)
                break
        if match is not None:
            lx, ly, scale = match
            # Physical origin = logical origin * scale (mss reports physical px).
            phys_x = round(lx * scale)
            phys_y = round(ly * scale)
            for m in monitors:
                if abs(m.physical_left - phys_x) <= 2 and abs(m.physical_top - phys_y) <= 2:
                    print(f"  🎯 Portal-selected screen matched: #{m.index} "
                          f"{m.width}x{m.height} at ({m.left},{m.top}) "
                          f"[portal logical ({px},{py}) {pw}x{ph}, scale {scale}]")
                    return m

    # Strategy 2 (fallback): match by scaled position directly against mss,
    # trying common integer scales. Position disambiguates identical resolutions.
    for scale in (1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
        phys_x = round(px * scale)
        phys_y = round(py * scale)
        for m in monitors:
            if abs(m.physical_left - phys_x) <= 2 and abs(m.physical_top - phys_y) <= 2:
                print(f"  🎯 Portal-selected screen matched (scale {scale}): "
                      f"#{m.index} {m.width}x{m.height} at ({m.left},{m.top})")
                return m

    print(f"  ⚠️ Could not match portal geometry (logical ({px},{py}) {pw}x{ph}) "
          f"to any monitor; leaving capture screen unset.")
    return None


def choose_capture_screen(screen_index: Optional[int] = None) -> Optional[ScreenRegion]:
    if OS_NAME == 'windows':
        monitors = enumerate_windows_screens()
    elif OS_NAME == 'linux':
        try:
            import mss
            with mss.mss() as sct:
                monitors = []
                for idx, m in enumerate(sct.monitors[1:], 1):
                    monitors.append(ScreenRegion(
                        index=idx,
                        name=f"Monitor {idx}",
                        left=m['left'],
                        top=m['top'],
                        width=m['width'],
                        height=m['height'],
                        physical_left=m['left'],
                        physical_top=m['top'],
                        physical_width=m['width'],
                        physical_height=m['height'],
                        primary=(idx == 1)
                    ))
        except Exception:
            return None
    else:
        return None

    if not monitors:
        print("  [WARN] Could not enumerate Windows monitors; falling back to virtual desktop capture.")
        return None

    env_index = os.environ.get('CUA_CAPTURE_MONITOR', '').strip()
    if screen_index is None and env_index:
        try:
            screen_index = int(env_index)
        except ValueError:
            print(f"  [WARN] Ignoring invalid CUA_CAPTURE_MONITOR={env_index!r}")

    def find_monitor(index: Optional[int]) -> Optional[ScreenRegion]:
        if index is None:
            return None
        for monitor in monitors:
            if monitor.index == index:
                return monitor
        return None

    selected = find_monitor(screen_index)
    if selected is None and screen_index is not None:
        print(f"  [WARN] Screen #{screen_index} was not found; please choose from the list.")

    if selected is None and len(monitors) == 1:
        selected = monitors[0]

    if selected is None:
        print("\nSelect screen to record:")
        for monitor in monitors:
            primary = " primary" if monitor.primary else ""
            print(
                f"  [{monitor.index}] {monitor.name}{primary}: "
                f"{monitor.width}x{monitor.height} at "
                f"({monitor.left},{monitor.top})"
            )

        default = next((m for m in monitors if m.primary), monitors[0])
        if sys.stdin.isatty():
            choice = input(f"Screen number [{default.index}]: ").strip()
            if choice:
                selected = find_monitor(int(choice)) if choice.isdigit() else None
                if selected is None:
                    print(f"  [WARN] Invalid screen {choice!r}; using primary screen.")
            if selected is None:
                selected = default
        else:
            print(f"  Non-interactive terminal; using primary screen #{default.index}.")
            selected = default

    print(
        f"  Recording screen #{selected.index}: {selected.name} "
        f"{selected.width}x{selected.height} at ({selected.left},{selected.top})"
    )
    return selected


# ============================================================
# V2 Collector
# ============================================================

class CollectorV2:
    """
    Automated collector using the C++ capture engine.

    States:
      IDLE        – no task active
      CAPTURING   – task running, auto-capturing actions
    """
    START_CAPTURE_SETTLE_SEC = 0.35

    def __init__(self, data_dir: str = './data',
                 buffer_capacity: int = 10,
                 max_width: int = 3840,
                 max_height: int = 2400,
                 target_fps: int = 10,
                 screen_index: Optional[int] = None,
                 agent_port: int = 8321):
        self.state = 'IDLE'
        self.data_store = DataStore(data_dir)
        self.overlay = StatusOverlay()
        # On Wayland-GNOME the capture screen is derived from the portal
        # selection in run() (after init_portal), so we don't prompt here.
        # On X11/Windows, select up front as before.
        self._screen_index = screen_index
        if SESSION_TYPE == 'wayland-gnome':
            self.capture_screen = None
        else:
            self.capture_screen = choose_capture_screen(screen_index)
        self.agent_port = agent_port

        if self.capture_screen is not None:
            max_width = max(max_width, self.capture_screen.width)
            max_height = max(max_height, self.capture_screen.height)

        # Resolution (will be updated from actual frames)
        if self.capture_screen is not None:
            self.resolution = (self.capture_screen.width, self.capture_screen.height)
        else:
            self.resolution = (max_width, max_height)
        # The CAPTURE FRAME: the (w, h) every saved screenshot has and the
        # coordinate space /api/action reads pixel coordinates in. It can be
        # smaller than the monitor (e.g. PipeWire streams a 3840x2160 monitor
        # at its 1920x1080 logical size). Starts as the best guess and is
        # corrected from the portal's logical size and from real frames.
        self.frame_size = self.resolution
        # The selected screen's rect in GLOBAL LOGICAL desktop coordinates
        # (portal geometry on Wayland; mss logical rect elsewhere). Used to
        # decide whether the pointer is on the captured screen at all.
        self.portal_rect: Optional[Tuple[int, int, int, int]] = None

        # C++ capture engine
        self.engine = cua_capture.CaptureEngine(
            buffer_capacity=buffer_capacity,
            max_width=max_width,
            max_height=max_height,
            target_fps=target_fps,
        )
        if self.capture_screen is not None and hasattr(self.engine, 'set_capture_region'):
            self.engine.set_capture_region(
                self.capture_screen.physical_left,
                self.capture_screen.physical_top,
                self.capture_screen.physical_width,
                self.capture_screen.physical_height,
                self.capture_screen.width,
                self.capture_screen.height,
                self.capture_screen.left,
                self.capture_screen.top,
            )

        # Task state
        self.current_task: Optional[TaskRecord] = None
        self.seq = 0
        self.task_start_mono = 0.0
        self._epoch_minus_monotonic = (
            time.time_ns() / 1_000_000_000 - time.monotonic_ns() / 1_000_000_000
        )

        # Thread pool for async disk I/O
        self._io_pool = ThreadPoolExecutor(max_workers=4)
        self._lock = threading.Lock()
        self._all_tasks: List[TaskRecord] = []

    def _mono_to_unix_ts(self, mono_ts: float) -> float:
        if mono_ts <= 0:
            return 0.0
        return mono_ts + self._epoch_minus_monotonic

    def _mono_to_iso(self, mono_ts: float) -> str:
        if mono_ts <= 0:
            return 'N/A'
        return datetime.fromtimestamp(
            self._mono_to_unix_ts(mono_ts), tz=timezone.utc
        ).isoformat()

    @staticmethod
    def _duration_ms(start_ts: float, end_ts: float) -> float:
        if start_ts <= 0 or end_ts <= 0 or end_ts < start_ts:
            return 0.0
        return round((end_ts - start_ts) * 1000.0, 3)

    def run(self):
        screen_line = ""
        if self.capture_screen is not None:
            screen_line = (
                f"  Capture Screen: #{self.capture_screen.index} "
                f"{self.capture_screen.width}x{self.capture_screen.height} "
                f"@ ({self.capture_screen.left},{self.capture_screen.top})\n"
            )
        hdr = (
            f"\n{'='*60}\n"
            f"  CUA_Collector V2 – Automated UI Action Data Collector\n"
            f"{'='*60}\n"
            f"  OS: {OS_NAME}  |  Session: {SESSION_TYPE}\n"
            f"  Backend: {CAPTURE_BACKEND_NAME}\n"
            f"{screen_line}"
            f"  Target Resolution: {self.resolution[0]}x{self.resolution[1]}\n"
            f"  Data dir: {self.data_store.base_dir.resolve()}\n\n"
            f"  Hotkeys:\n"
            f"    Ctrl+F8   → Start new task\n"
            f"    Ctrl+F12  → End current task\n"
            f"    Ctrl+C    → Quit\n"
            f"\n"
            f"  V2 Features:\n"
            f"    • Continuous 10 FPS ring buffer capture\n"
            f"    • Automatic pre/post screenshot matching\n"
            f"    • No manual Ctrl+F9 needed!\n"
            f"{'='*60}\n"
        )
        print(hdr)

        self.overlay.start()
        self.overlay.update_state('IDLE')

        # On Wayland, initialize the portal FIRST (this shows the single GNOME
        # "Share Screen" dialog) so we can derive the selected monitor's geometry
        # before building the uinput controller. The portal is the sole source of
        # truth for which screen is captured and controlled.
        portal_first = (SESSION_TYPE == 'wayland-gnome')
        if portal_first:
            print(f"  🖥️  Initializing {CAPTURE_BACKEND_NAME} screen capture...")
            if not self.engine.init_portal():
                print("  ❌ Failed to initialize screen capture!")
                print("  Make sure you're on Wayland/GNOME and approved the share dialog.")
                return
            # Derive the capture screen from the portal selection.
            resolved = resolve_screen_from_portal(self.engine)
            if resolved is not None:
                self.capture_screen = resolved
                self.resolution = (resolved.width, resolved.height)
            else:
                # Fallback: honor an explicit --screen-index if the match failed.
                self.capture_screen = choose_capture_screen(self._screen_index)
                if self.capture_screen is not None:
                    self.resolution = (self.capture_screen.width, self.capture_screen.height)
            # PipeWire negotiates the stream at the portal's LOGICAL size (a
            # scale-2 4K monitor streams at 1920x1080), so that logical size is
            # the capture frame — for any monitor/scale, not just 3840x2160.
            # The full logical rect (position + size) also identifies WHICH
            # screen was selected: it is the containment test used to reject
            # input that happens on a different monitor.
            try:
                px, py = int(self.engine.portal_position_x), int(self.engine.portal_position_y)
                pw, ph = int(self.engine.portal_size_w), int(self.engine.portal_size_h)
                if pw > 0 and ph > 0:
                    self.frame_size = (pw, ph)
                    if px >= 0 and py >= 0:
                        self.portal_rect = (px, py, pw, ph)
            except Exception:
                pass

        # Setup WaylandInputController now that the selected screen is known, so
        # the uinput virtual devices exist when the C++ engine scans /dev/input
        # inside self.engine.start().
        self.wayland_ctrl = None
        if SESSION_TYPE == 'wayland-gnome' and WaylandInputController is not None and self.capture_screen is not None:
            monitor = self.capture_screen
            # frame_size comes from the portal's logical size above, so the
            # injection frame matches the capture frame on any monitor/scale
            # (previously a hardcoded 3840x2160 -> 1920x1080 special case).
            frame_w, frame_h = self.frame_size
            print("🎮 Initializing Wayland Input Controller at startup...")
            try:
                self.wayland_ctrl = WaylandInputController(
                    monitor_physical_left=monitor.physical_left,
                    monitor_physical_top=monitor.physical_top,
                    monitor_physical_width=monitor.physical_width,
                    monitor_physical_height=monitor.physical_height,
                    frame_width=frame_w,
                    frame_height=frame_h,
                )
            except Exception as e:
                print(f"⚠️ Failed to create Wayland uinput virtual devices at startup: {e}")

        # Initialize capture backend for non-Wayland paths (portal already done above).
        if not portal_first:
            print(f"  🖥️  Initializing {CAPTURE_BACKEND_NAME} screen capture...")
            if not self.engine.init_portal():
                print("  ❌ Failed to initialize screen capture!")
                print("  Make sure mss/pynput dependencies are installed and OS permissions are granted.")
                return

        # Start capture + input monitoring
        self.engine.start()
        print("✅ Capture engine running. Press Ctrl+F8 to start a task.\n")

        # Start agent server
        self.agent_server = start_agent_server(self, start_port=self.agent_port)

        try:
            while True:
                self._poll_loop()
                time.sleep(0.05)  # 20 Hz poll
        except KeyboardInterrupt:
            print("\n🛑 Shutting down…")
            self._cleanup()

    def _poll_loop(self):
        """Main poll: check hotkeys and completed actions."""

        # 1. Check hotkeys
        hotkey = self.engine.pop_hotkey()
        if hotkey is not None:
            if hotkey == cua_capture.HotkeyType.START_TASK:
                self._on_start_task()
            elif hotkey == cua_capture.HotkeyType.END_TASK:
                self._on_end_task()
            # SCREENSHOT and DROP_ACTION are V1 compat, ignored in V2

        # 2. Check completed actions
        while True:
            action = self.engine.pop_action()
            if action is None:
                break
            
            if self.state == 'CAPTURING':
                # Only record actions that strictly happened after the task started
                # action.event_ts and task_start_mono both use time.monotonic() clock
                if action.event_ts >= self.task_start_mono:
                    self._handle_completed_action(action)

            # Update overlay with stats
            pending = self.engine.pending_count
            completed = self.engine.completed_count
            frames = self.engine.total_frames
            if frames > 0:
                self.overlay.update_state(
                    'CAPTURING',
                    f'#{self.seq} | ⏳{pending} | 📷{frames}'
                )

    def _on_start_task(self):
        with self._lock:
            if self.state != 'IDLE':
                print("⚠️  Task already active. End it first (Ctrl+F12).")
                return
            # Prevent duplicate Ctrl+F8 handling while the description dialog is open.
            self.state = 'TASK_ACTIVE'

        print("\n📋 Starting new task…")
        self.overlay.update_state('TASK_ACTIVE', 'Enter description…')

        desc = self.overlay.ask_description()
        if not desc:
            print("❌ Cancelled.")
            with self._lock:
                self.state = 'IDLE'
            self.overlay.update_state('IDLE')
            return

        with self._lock:
            tid = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
            self.current_task = TaskRecord(
                task_id=tid, description=desc,
                start_time=datetime.now(timezone.utc).isoformat(),
                end_time=None,
                os_name=OS_NAME, session_type=SESSION_TYPE,
                screen_resolution=self.resolution,
                capture_frame=self.frame_size,
            )
            self.data_store.create_task_dir(tid)
            self.seq = 0

            # Drain any stale actions accumulated during IDLE state
            while self.engine.pop_action() is not None:
                pass
            while self.engine.pop_hotkey() is not None:
                pass
            
            # Match V1 semantics more closely: do not record the Ctrl+F8
            # sequence or popup submit/cancel keystrokes as the first action.
            self.task_start_mono = time.monotonic() + self.START_CAPTURE_SETTLE_SEC
            self.state = 'CAPTURING'

            # Snapshot capture instrumentation so we can report frame supply
            # and the action miss rate when the task ends.
            self._task_start_wall = time.monotonic()
            try:
                cap, keep = self.engine.capture_stats()
            except Exception:
                cap, keep = 0, 0
            self._task_start_frames_captured = cap
            self._task_start_frames_keepalive = keep
            self._task_timeouts = 0

        self.overlay.update_state('CAPTURING', desc[:30])
        print(f'✅ Task "{desc}" started  (id: {tid})')
        print("   Actions are being captured automatically!")

    def _on_end_task(self):
        with self._lock:
            if self.state == 'IDLE':
                print("⚠️  No task to end.")
                return
        self._finalize_task()

    def _finalize_task(self):
        with self._lock:
            if self.current_task:
                self.current_task.end_time = datetime.now(timezone.utc).isoformat()
                self.data_store.save_task(self.current_task)
                self._all_tasks.append(self.current_task)
                self.data_store.save_master_index(self._all_tasks)
                n = len(self.current_task.actions)
                desc = self.current_task.description
                self.current_task = None
            else:
                n, desc = 0, ''
            self.state = 'IDLE'

        self.overlay.update_state('IDLE')
        print(f'\n🏁 Task "{desc}" ended. {n} actions recorded.\n')

        # Report capture instrumentation: effective frame supply and how many
        # of it came from the keepalive re-emit path (static-screen frames).
        try:
            cap, keep = self.engine.capture_stats()
        except Exception:
            cap = keep = None
        if cap is not None:
            elapsed = max(1e-3, time.monotonic() - getattr(self, '_task_start_wall', time.monotonic()))
            d_cap = cap - getattr(self, '_task_start_frames_captured', 0)
            d_keep = keep - getattr(self, '_task_start_frames_keepalive', 0)
            total = d_cap + d_keep
            fps = total / elapsed
            print(
                f"   📊 Capture: {total} frames in {elapsed:.1f}s "
                f"({fps:.1f} fps) — {d_cap} live, {d_keep} keepalive"
            )
            if d_keep > 0:
                pct = 100.0 * d_keep / max(1, total)
                print(
                    f"      ℹ️  {pct:.0f}% of frames were keepalive re-emits "
                    f"(screen was static; these previously caused timeouts)."
                )

    def _handle_completed_action(self, action):
        """Process a completed action from the C++ engine."""
        if self._off_capture_screen(action):
            # The pointer was on a DIFFERENT monitor: the user was interacting
            # with a screen we are not capturing, so neither the mouse action
            # nor the keys typed there belong in this recording. Recording
            # them would poison replay with out-of-frame coordinates.
            print(f"   🚫 Ignored {action.type} @ ({action.x},{action.y}): "
                  f"pointer is outside the captured screen "
                  f"(frame {self.frame_size[0]}x{self.frame_size[1]})")
            return

        with self._lock:
            if not self.current_task:
                return
            self.seq += 1
            seq = self.seq
            tid = self.current_task.task_id

        # Save screenshots async
        pre_name = f"action_{seq:04d}_before.png"
        post_name = f"action_{seq:04d}_after.png"
        pre_path = self.data_store.screenshot_path(tid, f"action_{seq:04d}_before")
        post_path = self.data_store.screenshot_path(tid, f"action_{seq:04d}_after")

        # Convert RGB bytes to PNG in thread pool
        if action.pre_frame_rgb and action.pre_w > 0:
            self._io_pool.submit(
                self._save_rgb_as_png,
                action.pre_frame_rgb, action.pre_w, action.pre_h, pre_path
            )
            # Update resolution + capture frame from the actual frame size:
            # a really-captured frame is the ground truth for both.
            self.resolution = (action.pre_w, action.pre_h)
            self.frame_size = (action.pre_w, action.pre_h)

        if action.post_frame_rgb and action.post_w > 0:
            self._io_pool.submit(
                self._save_rgb_as_png,
                action.post_frame_rgb, action.post_w, action.post_h, post_path
            )

        # V1 compatibility: Keys are a list of dicts.
        keys_list = []
        key_actions = list(getattr(action, 'key_actions', []) or [])
        key_records = {record.key_name: record for record in key_actions}
        ordered_keys = list(getattr(action, 'keys_pressed', []) or [])
        if not ordered_keys:
            ordered_keys = [record.key_name for record in key_actions]

        for key_name in ordered_keys:
            key_action = key_records.get(key_name)
            if key_action is not None:
                press_ts = key_action.press_ts
                release_ts = key_action.release_ts
            else:
                press_ts = action.event_ts
                release_ts = action.event_ts
            keys_list.append({
                'key': key_name,
                'press_time': self._mono_to_iso(press_ts),
                'release_time': self._mono_to_iso(release_ts),
                'delta_time': self._duration_ms(press_ts, release_ts),
            })

        # Normalize against the frame this action's screenshots actually have;
        # fall back to the tracked capture frame before the first saved frame.
        if action.pre_frame_rgb and action.pre_w > 0:
            fw, fh = action.pre_w, action.pre_h
        else:
            fw, fh = self.frame_size
        fw, fh = max(1, int(fw)), max(1, int(fh))

        def _norm(x, y):
            return (round(x / fw, 6), round(y / fh, 6))

        mouse_list = []
        if action.button_name:
            press_ts = action.press_ts if getattr(action, 'press_ts', 0) > 0 else action.event_ts
            release_ts = action.release_ts if getattr(action, 'release_ts', 0) > 0 else action.event_ts
            is_drag = action.type == 'drag'
            mouse_list.append({
                'button': action.button_name,
                'press_coords': (
                    action.press_x, action.press_y
                ) if is_drag else (action.x, action.y),
                'release_coords': (
                    action.release_x, action.release_y
                ) if is_drag else (action.x, action.y),
                'press_coords_norm': _norm(
                    action.press_x, action.press_y
                ) if is_drag else _norm(action.x, action.y),
                'release_coords_norm': _norm(
                    action.release_x, action.release_y
                ) if is_drag else _norm(action.x, action.y),
                'press_time': self._mono_to_iso(press_ts),
                'release_time': self._mono_to_iso(release_ts),
                'delta_time': self._duration_ms(press_ts, release_ts),
            })

        # Build action details
        action_details = {
            'mouse': mouse_list,
            'keys': keys_list,
            'scroll': {
                'dx_total': action.scroll_dx,
                'dy_total': action.scroll_dy,
                'direction': 'down' if action.scroll_dy < 0 else 'up' if action.scroll_dy > 0 else 'horizontal' if action.scroll_dx != 0 else 'none'
            },
            'pre_degraded': action.pre_degraded,
            'pre_frame_ts': self._mono_to_unix_ts(action.pre_frame_ts),
            'post_frame_ts': self._mono_to_unix_ts(action.post_frame_ts),
            'event_ts': self._mono_to_unix_ts(action.event_ts),
        }

        # Create action record
        with self._lock:
            if not self.current_task:
                return

            rec = ActionRecord(
                id=uuid.uuid4().hex,
                task_id=tid,
                task_description=self.current_task.description,
                sequence_number=seq,
                timestamp_before=self._mono_to_iso(action.pre_frame_ts),
                timestamp_action=self._mono_to_iso(action.event_ts),
                timestamp_after=self._mono_to_iso(action.post_frame_ts),
                elapsed_since_task_start=time.monotonic() - self.task_start_mono,
                pre_screenshot=pre_name,
                post_screenshot=post_name,
                action_type=action.type,
                action_coords=(action.x, action.y),
                action_details=action_details,
                os_name=OS_NAME,
                session_type=SESSION_TYPE,
                screen_resolution=self.resolution,
                pre_degraded=action.pre_degraded,
                frame_size=(fw, fh),
                norm_coords=_norm(action.x, action.y),
            )
            self.current_task.actions.append(asdict(rec))

            # Save task JSON after each action
            self._io_pool.submit(
                self.data_store.save_task, self.current_task
            )

        key_names = [k['key'] for k in action_details['keys']]
        mouse_names = [m['button'] for m in action_details['mouse']]
        summary_parts = []
        if key_names:
            summary_parts.append("+".join(key_names))
        if action.type == 'scroll':
            summary_parts.append(f"scroll:{action_details['scroll']['direction']}")
        elif mouse_names:
            mouse_label = mouse_names[0]
            if action.type == 'double_click':
                mouse_label = f"{mouse_label} double_click"
            elif action.type == 'triple_click':
                mouse_label = f"{mouse_label} triple_click"
            elif action.type == 'drag':
                mouse_label = f"{mouse_label} drag"
            else:
                mouse_label = f"{mouse_label} click"
            summary_parts.append(mouse_label)
        operation_summary = " + ".join(summary_parts) if summary_parts else action.type

        status = "⚠️ degraded" if action.pre_degraded else "✓"
        print(f"   ✅ Action #{seq}: {operation_summary} @ ({action.x},{action.y}) [{status}]")

    # Edge clicks legitimately land ON the boundary; only clearly-outside
    # coordinates mean "the pointer was on another monitor".
    _OFFSCREEN_SLACK_PX = 2

    def _off_capture_screen(self, action) -> bool:
        """True when this action happened on a monitor we are NOT capturing.

        Primary test — SCREEN IDENTITY, not coordinate range: the cursor
        tracker reports coordinates relative to whichever monitor the pointer
        is on (display.get_current_monitor()), so a click on another screen
        can look perfectly in-bounds. The only reliable signal is the global
        pointer position checked for containment in the SELECTED screen's
        global logical rect (the portal geometry).

        Fallback — when the global pointer cannot be read: out-of-frame
        coordinates still prove the pointer was elsewhere (they catch clicks
        on monitors larger than the frame, and nothing else).
        """
        on_screen = self._pointer_on_selected_screen()
        if on_screen is not None:
            return not on_screen

        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            return False  # frame unknown: never reject on a guess

        s = self._OFFSCREEN_SLACK_PX

        def outside(x, y):
            return x < -s or y < -s or x >= fw + s or y >= fh + s

        pts = [(action.x, action.y)]
        if action.type == 'drag':
            pts += [(action.press_x, action.press_y),
                    (action.release_x, action.release_y)]
        return any(outside(x, y) for x, y in pts)

    def _pointer_on_selected_screen(self) -> Optional[bool]:
        """Is the global pointer inside the selected screen's logical rect?

        Returns None when either side of the comparison is unavailable, so the
        caller can fall back instead of rejecting on a guess. Sampled at
        action-completion time (~the post-frame), which is close enough to the
        event: crossing monitors within that window is a corner case.
        """
        rect = self.portal_rect
        if rect is None and self.capture_screen is not None:
            cs = self.capture_screen
            rect = (cs.left, cs.top, cs.width, cs.height)  # logical, like mss
        if rect is None:
            return None
        pos = self._global_pointer_logical()
        if pos is None:
            return None
        x, y = pos
        rx, ry, rw, rh = rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def _global_pointer_logical(self) -> Optional[Tuple[int, int]]:
        """The pointer in GLOBAL LOGICAL desktop coordinates, or None.

        Wayland: the CUA extension's legacy GetPosition method — unlike
        GetPositionPixel it does NOT rebase onto the current monitor, which is
        exactly what makes it usable for telling monitors apart.
        """
        if SESSION_TYPE == 'wayland-gnome':
            try:
                out = subprocess.run(
                    ['gdbus', 'call', '--session',
                     '--dest', 'org.cua.CursorTracker',
                     '--object-path', '/org/cua/CursorTracker',
                     '--method', 'org.cua.CursorTracker.GetPosition'],
                    capture_output=True, text=True, timeout=1.0)
                m = re.search(r'\((-?\d+),\s*(-?\d+)\)', out.stdout)
                if m:
                    return (int(m.group(1)), int(m.group(2)))
            except Exception:
                pass
            return None
        if pyautogui is not None:
            try:
                p = pyautogui.position()
                return (int(p[0]), int(p[1]))
            except Exception:
                pass
        return None

    @staticmethod
    def _save_rgb_as_png(rgb_bytes: bytes, width: int, height: int, path: str):
        """Convert raw RGB bytes to PNG file."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img = PILImage.frombytes("RGB", (width, height), rgb_bytes)
            img.save(path, "PNG")
        except Exception as e:
            print(f"   ❌ Failed to save screenshot {path}: {e}")

    def _cleanup(self):
        if self.current_task:
            self._finalize_task()
        if hasattr(self, 'agent_server') and self.agent_server:
            try:
                self.agent_server.shutdown()
            except Exception:
                pass
        self.engine.stop()
        self._io_pool.shutdown(wait=True)
        self.overlay.stop()
        self.data_store.save_master_index(self._all_tasks)
        print("👋 Done.")
        os._exit(0)


# ============================================================
# Agent API Server & Control Helper
# ============================================================

def run_on_gui_thread(collector, func):
    result = []
    exception = []
    event = threading.Event()
    
    def wrapped():
        try:
            res = func()
            result.append(res)
        except Exception as e:
            exception.append(e)
        finally:
            event.set()
            
    if collector.overlay._root:
        collector.overlay._root.after(0, wrapped)
        event.wait()
    else:
        wrapped()
        
    if exception:
        raise exception[0]
    return result[0] if result else None


class AgentAPIHandler(BaseHTTPRequestHandler):
    collector = None

    def log_message(self, format, *args):
        # Silence logs
        pass

    def _send_json(self, data, status_code=200):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_GET(self):
        if self.path == '/api/status':
            status = {
                "state": self.collector.state,
                "task_active": self.collector.current_task is not None,
                "task_id": self.collector.current_task.task_id if self.collector.current_task else None,
                "num_actions": len(self.collector.current_task.actions) if self.collector.current_task else 0,
            }
            # Expose the screen selected at startup so agent clients can capture
            # the exact monitor the collector controls (no manual re-selection).
            cs = self.collector.capture_screen
            status["capture_screen"] = None if cs is None else {
                "index": cs.index, "name": cs.name,
                "left": cs.left, "top": cs.top,
                "width": cs.width, "height": cs.height,
            }
            # The CAPTURE FRAME: the coordinate space screenshots are saved in
            # and /api/action reads pixel coords in. NOT the monitor size --
            # a scale-2 4K monitor has a 1920x1080 frame. Its presence also
            # signals that /api/action accepts normalized norm_x/norm_y coords.
            fw, fh = self.collector.frame_size
            status["frame"] = {"width": int(fw), "height": int(fh)}
            self._send_json(status)
        else:
            self._send_json({"error": "Not Found"}, 404)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if self.path == '/api/task/start':
            desc = data.get('description', 'Agent Automated Task')

            def run_start():
                if self.collector.state != 'IDLE':
                    return {"error": "Task already active"}, 400
                tid = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
                self.collector.current_task = TaskRecord(
                    task_id=tid, description=desc,
                    start_time=datetime.now(timezone.utc).isoformat(),
                    end_time=None,
                    os_name=OS_NAME, session_type=SESSION_TYPE,
                    screen_resolution=self.collector.resolution,
                    capture_frame=self.collector.frame_size,
                )
                self.collector.data_store.create_task_dir(tid)
                self.collector.seq = 0
                while self.collector.engine.pop_action() is not None:
                    pass
                while self.collector.engine.pop_hotkey() is not None:
                    pass
                self.collector.task_start_mono = time.monotonic() + self.collector.START_CAPTURE_SETTLE_SEC
                self.collector.state = 'CAPTURING'
                self.collector.overlay.update_state('CAPTURING', desc[:30])
                print(f'🤖 Agent Task "{desc}" started (id: {tid})')
                return {"status": "success", "task_id": tid}

            try:
                res = run_on_gui_thread(self.collector, run_start)
                if isinstance(res, dict) and "error" in res:
                    self._send_json(res, 400)
                else:
                    self._send_json(res, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == '/api/task/end':
            def run_end():
                if self.collector.state == 'IDLE':
                    return {"error": "No task active"}
                self.collector._finalize_task()
                return {"status": "success"}

            try:
                res = run_on_gui_thread(self.collector, run_end)
                if isinstance(res, dict) and "error" in res:
                    self._send_json(res, 400)
                else:
                    self._send_json(res, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == '/api/action':
            if self.collector.state != 'CAPTURING':
                self._send_json({"error": "No active task. Start a task first."}, 400)
                return

            action_type = data.get('type')
            if not action_type:
                self._send_json({"error": "Missing action type"}, 400)
                return

            if not pyautogui and not WaylandInputController:
                self._send_json({"error": "No input backend available (pyautogui or wayland_input)"}, 500)
                return

            def run_action():
                if self.collector.overlay.user_aborted:
                    self.collector.overlay.user_aborted = False
                    return {"error": "Aborted by user"}, 499

                # On Wayland, skip the Tkinter overlay — it runs on XWayland
                # and can't cover native Wayland windows, but DOES steal keyboard
                # focus from them, breaking hotkey/keyboard input.
                skip_overlay = (SESSION_TYPE == 'wayland-gnome')
                if not skip_overlay:
                    self.collector.overlay.show_lock("🤖 Agent Executing Action...")
                try:
                    monitor = self.collector.capture_screen
                    # The tracked capture frame: portal logical size at startup,
                    # corrected from real frames as they arrive. This replaces
                    # the old hardcoded 3840x2160 -> 1920x1080 special case.
                    frame_w, frame_h = self.collector.frame_size

                    # Normalized coordinates: norm_* fields are fractions of the
                    # frame in [0,1], projected here onto the LIVE frame -- so a
                    # client never needs to know the frame size, and coords stay
                    # correct across any screen size or scale.
                    for xk, yk, nxk, nyk in (
                        ('x', 'y', 'norm_x', 'norm_y'),
                        ('press_x', 'press_y', 'norm_press_x', 'norm_press_y'),
                        ('release_x', 'release_y', 'norm_release_x', 'norm_release_y'),
                    ):
                        if data.get(nxk) is not None and data.get(nyk) is not None:
                            data[xk] = int(round(float(data[nxk]) * frame_w))
                            data[yk] = int(round(float(data[nyk]) * frame_h))

                    if monitor:
                        scale_x = monitor.physical_width / frame_w if frame_w > 0 else 1.0
                        scale_y = monitor.physical_height / frame_h if frame_h > 0 else 1.0
                        offset_x = monitor.physical_left
                        offset_y = monitor.physical_top
                    else:
                        scale_x, scale_y = 1.0, 1.0
                        offset_x, offset_y = 0, 0

                    # On Wayland, use kernel-level input via evdev/uinput
                    # so we can control native Wayland windows (not just X11)
                    use_wayland_input = (
                        SESSION_TYPE == 'wayland-gnome'
                        and WaylandInputController is not None
                        and monitor is not None
                    )

                    if use_wayland_input:
                        # Use pre-initialized WaylandInputController from collector
                        if not hasattr(self.collector, 'wayland_ctrl') or self.collector.wayland_ctrl is None:
                            # Lazy fallback
                            self.collector.wayland_ctrl = WaylandInputController(
                                monitor_physical_left=monitor.physical_left,
                                monitor_physical_top=monitor.physical_top,
                                monitor_physical_width=monitor.physical_width,
                                monitor_physical_height=monitor.physical_height,
                                frame_width=frame_w,
                                frame_height=frame_h,
                            )
                        wctrl = self.collector.wayland_ctrl
                        # The controller caches its frame->physical scale at
                        # build time; the tracked frame can be corrected from
                        # real frames afterwards. Keep the scale in sync.
                        if frame_w > 0 and frame_h > 0:
                            wctrl.scale_x = monitor.physical_width / frame_w
                            wctrl.scale_y = monitor.physical_height / frame_h

                    def input_block():
                        if not skip_overlay:
                            self.collector.overlay.hide_lock()
                            # Short sleep to let the overlay withdraw
                            time.sleep(0.1)
                        ts_start = time.monotonic()

                        if action_type == 'click':
                            x, y = data['x'], data['y']
                            button = data.get('button', 'left')
                            if use_wayland_input:
                                wctrl.click(x, y, button)
                            else:
                                pyautogui.click(x * scale_x + offset_x, y * scale_y + offset_y, button=button)
                            self.collector.engine.inject_mouse_click(ts_start, x, y, button)

                        elif action_type == 'double_click':
                            x, y = data['x'], data['y']
                            button = data.get('button', 'left')
                            if use_wayland_input:
                                wctrl.double_click(x, y, button)
                            else:
                                pyautogui.doubleClick(x * scale_x + offset_x, y * scale_y + offset_y, button=button)
                            if hasattr(self.collector.engine, 'inject_mouse_double_click'):
                                self.collector.engine.inject_mouse_double_click(ts_start, x, y, button)
                            else:
                                self.collector.engine.inject_mouse_click(ts_start, x, y, button)
                                self.collector.engine.inject_mouse_click(ts_start + 0.1, x, y, button)

                        elif action_type == 'triple_click':
                            x, y = data['x'], data['y']
                            button = data.get('button', 'left')
                            if use_wayland_input:
                                wctrl.click(x, y, button)
                                wctrl.click(x, y, button)
                                wctrl.click(x, y, button)
                            else:
                                pyautogui.click(x * scale_x + offset_x, y * scale_y + offset_y,
                                                button=button, clicks=3, interval=0.05)
                            self.collector.engine.inject_mouse_click(ts_start, x, y, button)
                            self.collector.engine.inject_mouse_click(ts_start + 0.1, x, y, button)
                            self.collector.engine.inject_mouse_click(ts_start + 0.2, x, y, button)

                        elif action_type == 'drag':
                            px, py = data['press_x'], data['press_y']
                            rx, ry = data['release_x'], data['release_y']
                            button = data.get('button', 'left')
                            dur = data.get('duration', 0.5)
                            if use_wayland_input:
                                wctrl.drag(px, py, rx, ry, button, dur)
                            else:
                                pyautogui.moveTo(px * scale_x + offset_x, py * scale_y + offset_y)
                                pyautogui.dragTo(rx * scale_x + offset_x, ry * scale_y + offset_y, button=button, duration=dur)
                            self.collector.engine.inject_mouse_drag(ts_start, px, py, rx, ry, button, dur)

                        elif action_type == 'write':
                            text = data['text']
                            if use_wayland_input:
                                wctrl.write(text)
                            else:
                                pyautogui.write(text)
                            for i, char in enumerate(text):
                                ts_char = ts_start + i * 0.05
                                self.collector.engine.inject_key_event(ts_char, char, True)
                                self.collector.engine.inject_key_event(ts_char + 0.02, char, False)

                        elif action_type == 'press_key':
                            key = data['key']
                            if use_wayland_input:
                                wctrl.press_key(key)
                            else:
                                pyautogui.press(key)
                            self.collector.engine.inject_key_event(ts_start, key, True)
                            self.collector.engine.inject_key_event(ts_start + 0.05, key, False)

                        elif action_type == 'hotkey':
                            keys = data['keys']
                            if use_wayland_input:
                                wctrl.hotkey(*keys)
                            else:
                                pyautogui.hotkey(*keys)
                            # Inject key events for the combo
                            for i, key in enumerate(keys):
                                self.collector.engine.inject_key_event(ts_start + i * 0.02, key, True)
                            for i, key in enumerate(reversed(keys)):
                                self.collector.engine.inject_key_event(ts_start + 0.1 + i * 0.02, key, False)

                        elif action_type == 'scroll':
                            dx = data.get('dx', 0)
                            dy = data.get('dy', 0)
                            x = data.get('x')
                            y = data.get('y')
                            if use_wayland_input:
                                wctrl.scroll(x, y, dx, dy)
                            else:
                                if x is not None and y is not None:
                                    pyautogui.moveTo(x * scale_x + offset_x, y * scale_y + offset_y)
                                else:
                                    ax, ay = pyautogui.position()
                                    x = (ax - offset_x) / scale_x if scale_x > 0 else ax
                                    y = (ay - offset_y) / scale_y if scale_y > 0 else ay
                                pyautogui.scroll(dy)
                                if dx != 0:
                                    pyautogui.hscroll(dx)
                            self.collector.engine.inject_scroll(ts_start, x, y, dx, dy)

                        elif action_type == 'move':
                            # Pointer move only; not a recorded action on its own
                            # (matches human capture, where a bare move is not an action).
                            x, y = data['x'], data['y']
                            if use_wayland_input:
                                wctrl.move_to(x, y)
                            else:
                                pyautogui.moveTo(x * scale_x + offset_x, y * scale_y + offset_y)

                        elif action_type == 'right_click':
                            x, y = data['x'], data['y']
                            if use_wayland_input:
                                wctrl.click(x, y, 'right')
                            else:
                                pyautogui.click(x * scale_x + offset_x, y * scale_y + offset_y, button='right')
                            self.collector.engine.inject_mouse_click(ts_start, x, y, 'right')

                        elif action_type == 'mouse_down':
                            x = data.get('x')
                            y = data.get('y')
                            button = data.get('button', 'left')
                            if use_wayland_input:
                                wctrl.mouse_down(x, y, button)
                            else:
                                if x is not None and y is not None:
                                    pyautogui.moveTo(x * scale_x + offset_x, y * scale_y + offset_y)
                                pyautogui.mouseDown(button=button)

                        elif action_type == 'mouse_up':
                            x = data.get('x')
                            y = data.get('y')
                            button = data.get('button', 'left')
                            if use_wayland_input:
                                wctrl.mouse_up(x, y, button)
                            else:
                                if x is not None and y is not None:
                                    pyautogui.moveTo(x * scale_x + offset_x, y * scale_y + offset_y)
                                pyautogui.mouseUp(button=button)

                        elif action_type == 'key_down':
                            key = data['key']
                            if use_wayland_input:
                                wctrl.key_down(key)
                            else:
                                pyautogui.keyDown(key)
                            self.collector.engine.inject_key_event(ts_start, key, True)

                        elif action_type == 'key_up':
                            key = data['key']
                            if use_wayland_input:
                                wctrl.key_up(key)
                            else:
                                pyautogui.keyUp(key)
                            self.collector.engine.inject_key_event(ts_start, key, False)
                        else:
                            raise ValueError(f"Unknown action type: {action_type}")

                        time.sleep(0.35)

                    input_block()
                finally:
                    if not skip_overlay:
                        self.collector.overlay.show_lock("🤖 Agent Executing Action...")

                if not skip_overlay:
                    self.collector.overlay.hide_lock()
                return {"status": "success"}, 200

            try:
                res, code = run_on_gui_thread(self.collector, run_action)
                self._send_json(res, code)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "Not Found"}, 404)


def start_agent_server(collector, start_port=8321):
    AgentAPIHandler.collector = collector
    port = start_port
    while port < start_port + 100:
        try:
            server = HTTPServer(('127.0.0.1', port), AgentAPIHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            print(f"🤖 Agent API server started on http://127.0.0.1:{port}")
            collector.agent_port = port
            return server
        except OSError:
            port += 1
    print("❌ Failed to start Agent API server: all ports in range 8321-8421 in use.")
    return None


# ============================================================
# Entry Point
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='CUA_Collector - Automated UI Action Data Collector')
    parser.add_argument('--data-dir', default='./data', help='Directory to store collected data')
    parser.add_argument('--buffer-capacity', type=int, default=10, help='Ring buffer capacity (frames)')
    parser.add_argument('--max-width', type=int, default=3840, help='Max frame width')
    parser.add_argument('--max-height', type=int, default=2400, help='Max frame height')
    parser.add_argument('--fps', type=int, default=10, help='Target capture FPS')
    parser.add_argument(
        '--screen-index',
        type=int,
        default=None,
        help='1-based Windows screen index to capture (left-to-right order)',
    )
    parser.add_argument(
        '--agent-port',
        type=int,
        default=8321,
        help='Start port for local agent control API server (default: 8321)',
    )
    args = parser.parse_args()

    collector = CollectorV2(
        data_dir=args.data_dir,
        buffer_capacity=args.buffer_capacity,
        max_width=args.max_width,
        max_height=args.max_height,
        target_fps=args.fps,
        screen_index=args.screen_index,
        agent_port=args.agent_port,
    )
    collector.run()


if __name__ == '__main__':
    main()
