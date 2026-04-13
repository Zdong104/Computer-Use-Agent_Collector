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
import sys
import json
import time
import uuid
import threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor
from PIL import Image as PILImage
import io

# Add build dir to path for cua_capture module
build_dir = Path(__file__).parent / 'build'
if build_dir.exists():
    sys.path.insert(0, str(build_dir))

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
        print("   Build it first: cmake -S . -B build && cmake --build build -j$(nproc)")
    else:
        print(f"❌ Failed to import cua_capture: {e}")
    sys.exit(1)


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

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        time.sleep(0.8)

    def _run_tk(self):
        import tkinter as tk
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

        self._poll()
        self._root.mainloop()

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
    system = plat.system().lower()
    if system == 'linux':
        session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        if session_type == 'wayland':
            if 'gnome' in desktop:
                return 'linux', 'wayland-gnome'
            return 'linux', 'wayland'
        return 'linux', 'x11'
    return system, 'unknown'

OS_NAME, SESSION_TYPE = detect_platform()


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
                 target_fps: int = 10):
        self.state = 'IDLE'
        self.data_store = DataStore(data_dir)
        self.overlay = StatusOverlay()

        # Resolution (will be updated from actual frames)
        self.resolution = (max_width, max_height)

        # C++ capture engine
        self.engine = cua_capture.CaptureEngine(
            buffer_capacity=buffer_capacity,
            max_width=max_width,
            max_height=max_height,
            target_fps=target_fps,
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
        hdr = (
            f"\n{'='*60}\n"
            f"  CUA_Collector V2 – Automated UI Action Data Collector\n"
            f"{'='*60}\n"
            f"  OS: {OS_NAME}  |  Session: {SESSION_TYPE}\n"
            f"  Max Resolution: {self.resolution[0]}×{self.resolution[1]}\n"
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

        # Initialize PipeWire portal (may show share dialog)
        print("  🖥️  Initializing PipeWire screen capture...")
        if not self.engine.init_portal():
            print("  ❌ Failed to initialize screen capture!")
            print("  Make sure you're on Wayland/GNOME and approved the share dialog.")
            return

        # Start capture + input monitoring
        self.engine.start()
        print("✅ Capture engine running. Press Ctrl+F8 to start a task.\n")

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

    def _handle_completed_action(self, action):
        """Process a completed action from the C++ engine."""
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
            # Update resolution from actual frame size
            self.resolution = (action.pre_w, action.pre_h)

        if action.post_frame_rgb and action.post_w > 0:
            self._io_pool.submit(
                self._save_rgb_as_png,
                action.post_frame_rgb, action.post_w, action.post_h, post_path
            )

        # V1 compatibility: Keys are a list of dicts.
        keys_list = []
        if hasattr(action, 'key_actions') and action.key_actions:
            for key_action in action.key_actions:
                keys_list.append({
                    'key': key_action.key_name,
                    'press_time': self._mono_to_iso(key_action.press_ts),
                    'release_time': self._mono_to_iso(key_action.release_ts),
                    'delta_time': self._duration_ms(
                        key_action.press_ts, key_action.release_ts
                    ),
                })
        else:
            for key_name in action.keys_pressed:
                keys_list.append({
                    'key': key_name,
                    'press_time': self._mono_to_iso(action.event_ts),
                    'release_time': self._mono_to_iso(action.event_ts),
                    'delta_time': 0.0,
                })

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
            elif action.type == 'drag':
                mouse_label = f"{mouse_label} drag"
            else:
                mouse_label = f"{mouse_label} click"
            summary_parts.append(mouse_label)
        operation_summary = " + ".join(summary_parts) if summary_parts else action.type

        status = "⚠️ degraded" if action.pre_degraded else "✓"
        print(f"   ✅ Action #{seq}: {operation_summary} @ ({action.x},{action.y}) [{status}]")

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
        self.engine.stop()
        self._io_pool.shutdown(wait=True)
        self.overlay.stop()
        self.data_store.save_master_index(self._all_tasks)
        print("👋 Done.")
        os._exit(0)


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
    args = parser.parse_args()

    collector = CollectorV2(
        data_dir=args.data_dir,
        buffer_capacity=args.buffer_capacity,
        max_width=args.max_width,
        max_height=args.max_height,
        target_fps=args.fps,
    )
    collector.run()


if __name__ == '__main__':
    main()
