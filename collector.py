"""
CUA_Collector - UI Action Data Collector
========================================
Collects (State_A, Action, State_B) data for UI agent training.

Workflow:
  Ctrl+F8  → Start new task (popup for description)
  Ctrl+F9  → Take pre-screenshot, then listen for mouse action
  [mouse click/scroll] → debounce 0.5s → auto post-screenshot
  Ctrl+F12 → End current task

Records: timestamps, screenshots, action type/coords, OS info, and more.
"""
import os
from PIL import Image as PILImage
import sys
import json
import time
import uuid
import threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List

from platform_backends import (
    OS_NAME, SESSION_TYPE,
    detect_platform, get_screen_resolution,
    Screenshotter, CursorTracker,
    WaylandInputMonitor, PynputInputMonitor,
)


# ============================================================
# Data Models
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
    action_type: str          # "click" or "scroll"
    action_coords: Tuple[int, int]
    action_details: dict      # button info or scroll delta
    os_name: str
    session_type: str
    screen_resolution: Tuple[int, int]


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
# Data Store
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
# Status Overlay (Tkinter)
# ============================================================

class StatusOverlay:
    """Always-on-top floating status indicator."""

    COLORS = {
        'IDLE': '#555555',
        'TASK_ACTIVE': '#1565C0',
        'WAITING_ACTION': '#E65100',
        'WAITING_TIMEOUT': '#B71C1C',
    }
    LABELS = {
        'IDLE': '⏹  Idle',
        'TASK_ACTIVE': '🟢 Task Active',
        'WAITING_ACTION': '🟡 Waiting for Action',
        'WAITING_TIMEOUT': '🔴 Recording…',
    }

    def __init__(self):
        self._root = None
        self._label = None
        self._thread = None
        self._running = False
        self._pending_state = 'IDLE'
        self._pending_text = None

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
        self._root.geometry(f'250x36+{sw - 270}+10')
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
        """Blocking popup to get task description from user.
        Shift+Enter inserts a newline. Plain Enter submits.
        Uses the overlay's existing Tk root to avoid duplicate windows."""
        self._dialog_result = None
        self._dialog_done = threading.Event()

        # Schedule dialog creation on the overlay's Tk event loop
        if self._root:
            self._root.after(0, self._create_dialog)

        # Block calling thread until dialog is dismissed
        while not self._dialog_done.is_set():
            time.sleep(0.1)

        return self._dialog_result

    def _create_dialog(self):
        """Create the task description dialog as a Toplevel of the overlay root."""
        import tkinter as tk

        dialog = tk.Toplevel(self._root)
        dialog.title("New Task")
        dialog.attributes('-topmost', True)

        w, h = 600, 260
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        dialog.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        tk.Label(dialog, text="Enter task description:", font=("Helvetica", 18)).pack(pady=(20, 5))
        tk.Label(dialog, text="(Enter = submit, Shift+Enter = new line)",
                 font=("Helvetica", 10), fg='#666').pack()

        text = tk.Text(dialog, font=("Helvetica", 18), height=3, wrap='word')
        text.pack(pady=10, padx=40, fill='x')
        text.focus_set()

        def submit(e=None):
            content = text.get('1.0', 'end-1c').strip()
            self._dialog_result = content if content else None
            dialog.destroy()
            self._dialog_done.set()
            return 'break'

        def cancel(e=None):
            self._dialog_result = None
            dialog.destroy()
            self._dialog_done.set()

        text.bind('<Return>', submit)
        text.bind('<Shift-Return>', lambda e: None)  # allow default newline
        text.bind('<Escape>', cancel)

        bf = tk.Frame(dialog)
        bf.pack(pady=10)
        tk.Button(bf, text="OK", command=submit, font=("Helvetica", 14), width=10).pack(side='left', padx=10)
        tk.Button(bf, text="Cancel", command=cancel, font=("Helvetica", 14), width=10).pack(side='left', padx=10)

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.grab_set()  # make modal


# ============================================================
# Main Collector (State Machine)
# ============================================================

class Collector:
    """
    States:
      IDLE            – no task active
      TASK_ACTIVE     – task running, waiting for Ctrl+F9
      WAITING_ACTION  – pre-screenshot taken, listening for click/scroll
      WAITING_TIMEOUT – action detected, 0.5s debounce running
    """

    DEBOUNCE_SEC = 0.5

    def __init__(self, data_dir: str = './data'):
        self.state = 'IDLE'
        self.data_store = DataStore(data_dir)
        self.screenshotter = Screenshotter()
        self.cursor = CursorTracker()
        self.overlay = StatusOverlay()
        self.resolution = get_screen_resolution()

        # If the CUA extension reports native pixel resolution, prefer that
        # (it matches the PipeWire screenshot dimensions exactly)
        native_res = self.cursor.get_monitor_native_resolution()
        if native_res:
            self.resolution = native_res

        # Task state
        self.current_task: Optional[TaskRecord] = None
        self.seq = 0
        self.task_start_mono = 0.0

        # Action capture state
        self._timer: Optional[threading.Timer] = None
        self._pre_ss_name: Optional[str] = None
        self._pre_ss_time: Optional[str] = None
        self._action_time: Optional[str] = None
        
        self._active_keys = {}
        self._active_mouse_buttons = {}
        self._completed_key_actions = []
        self._completed_mouse_actions = []
        self._scroll_acc = {'dx': 0, 'dy': 0}

        self._lock = threading.Lock()
        self._all_tasks: List[TaskRecord] = []

        # Input monitor
        cbs = {
            'on_hotkey_start_task': self._on_start_task,
            'on_hotkey_screenshot': self._on_screenshot,
            'on_hotkey_end_task': self._on_end_task,
            'on_hotkey_drop_action': self._on_drop_action,
            'on_mouse_button': self._on_mouse_button,
            'on_key_event': self._on_key_event,
            'on_mouse_scroll': self._on_scroll,
        }
        if SESSION_TYPE.startswith('wayland'):
            self.input = WaylandInputMonitor(cbs)
        else:
            self.input = PynputInputMonitor(cbs)

    # ---- public ----

    def run(self):
        hdr = (
            f"\n{'='*60}\n"
            f"  CUA_Collector – UI Action Data Collector\n"
            f"{'='*60}\n"
            f"  OS: {OS_NAME}  |  Session: {SESSION_TYPE}\n"
            f"  Resolution: {self.resolution[0]}×{self.resolution[1]}\n"
            f"  Data dir: {self.data_store.base_dir.resolve()}\n\n"
            f"  Hotkeys:\n"
            f"    Ctrl+F8   → Start new task\n"
            f"    Ctrl+F9   → Pre-screenshot (begin action capture)\n"
            f"    Ctrl+F12  → End current task\n"
            f"    Ctrl+C    → Quit\n"
            f"{'='*60}\n"
        )
        print(hdr)

        self.overlay.start()
        self.overlay.update_state('IDLE')

        # Initialize Wayland screenshot backend (shows share dialog)
        self.screenshotter.init_wayland()

        self.input.start()

        print("✅ Collector running. Press Ctrl+F8 to start a task.\n")

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down…")
            self._cleanup()

    def _cleanup(self):
        if self.current_task:
            self._finalize_task()
        self.input.stop()
        self.screenshotter.stop()
        self.overlay.stop()
        self.data_store.save_master_index(self._all_tasks)
        print("👋 Done.")
        os._exit(0)

    # ---- hotkey handlers ----

    def _on_start_task(self):
        with self._lock:
            if self.state != 'IDLE':
                print("⚠️  Task already active. End it first (Ctrl+F12).")
                return

        print("\n📋 Starting new task…")
        self.overlay.update_state('TASK_ACTIVE', 'Enter description…')

        desc = self.overlay.ask_description()
        if not desc:
            print("❌ Cancelled.")
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
            self.task_start_mono = time.monotonic()
            self.state = 'TASK_ACTIVE'

        self.overlay.update_state('TASK_ACTIVE', desc[:30])
        print(f'✅ Task "{desc}" started  (id: {tid})')
        print("   Press Ctrl+F9 to take the first pre-screenshot.")

    def _on_screenshot(self):
        with self._lock:
            if self.state == 'IDLE':
                print("⚠️  No active task. Press Ctrl+F8 first.")
                return
            if self.state in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                print("⚠️  Finish current action first.")
                return
            self.seq += 1
            seq = self.seq
            tid = self.current_task.task_id

        name = f"action_{seq:04d}_before"
        path = self.data_store.screenshot_path(tid, name)

        print(f"📸 Pre-screenshot #{seq}…")
        self.overlay.update_state('WAITING_ACTION', f'#{seq}')

        ok = self.screenshotter.capture(path)
        if not ok:
            print("❌ Screenshot failed!")
            with self._lock:
                self.seq -= 1
            self.overlay.update_state('TASK_ACTIVE')
            return

        # Dynamically read resolution from the actual captured screenshot
        # This ensures it matches the PipeWire-captured monitor, not the
        # monitor where the terminal is placed.
        try:
            img = PILImage.open(path)
            actual_res = img.size  # (width, height)
            img.close()
            if actual_res != self.resolution:
                print(f"   📐 Screen resolution updated: {self.resolution} → {actual_res}")
                self.resolution = actual_res
                if self.current_task:
                    self.current_task.screen_resolution = actual_res
        except Exception as e:
            print(f"   ⚠️  Could not read screenshot dimensions: {e}")

        with self._lock:
            self._pre_ss_name = name + '.png'
            self._pre_ss_time = datetime.now(timezone.utc).isoformat()
            self._action_time = None
            self._scroll_acc = {'dx': 0, 'dy': 0}
            self._completed_key_actions = []
            self._completed_mouse_actions = []
            self.state = 'WAITING_ACTION'

        print("   ✅ Saved. Now click, drag, or press special keys… (ESC to cancel)")

    def _on_drop_action(self):
        """ESC pressed: drop the current action capture and return to TASK_ACTIVE."""
        with self._lock:
            if self.state not in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                return
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self.seq -= 1  # rollback sequence number
            self.state = 'TASK_ACTIVE'

        self.overlay.update_state('TASK_ACTIVE', 'Action dropped')
        print("   ⏪ Action dropped. Press Ctrl+F9 for next, or Ctrl+F12 to end.\n")

    def _on_end_task(self):
        with self._lock:
            if self.state == 'IDLE':
                print("⚠️  No task to end.")
                return
        self._finalize_task()

    def _finalize_task(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
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

    # ---- mouse/key handlers ----

    def _on_key_event(self, key_name: str, pressed: bool):
        now = datetime.now(timezone.utc)
        with self._lock:
            if pressed:
                if key_name not in self._active_keys:
                    self._active_keys[key_name] = now
            else:
                if key_name in self._active_keys:
                    press_time = self._active_keys.pop(key_name)
                    if self.state in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                        delta_time = (now - press_time).total_seconds()
                        self._completed_key_actions.append({
                            'key': key_name,
                            'press_time': press_time.isoformat(),
                            'release_time': now.isoformat(),
                            'delta_time': delta_time
                        })
                        if not self._action_time:
                            self._action_time = press_time.isoformat()

            if self.state in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                self._reset_timer_if_idle()

    def _on_mouse_button(self, button: str, pressed: bool):
        now = datetime.now(timezone.utc)
        coords = self.cursor.get_position()
        with self._lock:
            if pressed:
                if button not in self._active_mouse_buttons:
                    self._active_mouse_buttons[button] = {'coords': coords, 'time': now}
            else:
                if button in self._active_mouse_buttons:
                    down_info = self._active_mouse_buttons.pop(button)
                    if self.state in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                        press_time = down_info['time']
                        delta_time = (now - press_time).total_seconds()
                        self._completed_mouse_actions.append({
                            'button': button,
                            'press_coords': down_info['coords'],
                            'release_coords': coords,
                            'press_time': press_time.isoformat(),
                            'release_time': now.isoformat(),
                            'delta_time': delta_time
                        })
                        if not self._action_time:
                            self._action_time = press_time.isoformat()
            
            if self.state in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                self._reset_timer_if_idle()

    def _on_scroll(self, dx: int, dy: int):
        with self._lock:
            if self.state not in ('WAITING_ACTION', 'WAITING_TIMEOUT'):
                return
            self._scroll_acc['dx'] += dx
            self._scroll_acc['dy'] += dy
            if not self._action_time:
                self._action_time = datetime.now(timezone.utc).isoformat()
            self._reset_timer_if_idle()

    # ---- debounce ----

    def _reset_timer_if_idle(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

        if self._active_mouse_buttons or self._active_keys:
            self.state = 'WAITING_TIMEOUT'
            self.overlay.update_state('WAITING_TIMEOUT', 'Holding…')
            return

        self.state = 'WAITING_TIMEOUT'
        self.overlay.update_state('WAITING_TIMEOUT', '⏳ 0.5s')
        self._timer = threading.Timer(self.DEBOUNCE_SEC, self._on_timer_done)
        self._timer.start()

    def _on_timer_done(self):
        """0.5s elapsed with no new events → take post-screenshot."""
        with self._lock:
            if self.state != 'WAITING_TIMEOUT':
                return
            seq = self.seq
            tid = self.current_task.task_id

            action_type = "unknown"
            action_coords = self.cursor.get_position()
            
            if self._completed_mouse_actions:
                act = self._completed_mouse_actions[0]
                dx = abs(act['release_coords'][0] - act['press_coords'][0])
                dy = abs(act['release_coords'][1] - act['press_coords'][1])
                if dx > 3 or dy > 3 or act['delta_time'] > 0.3:
                    action_type = "drag"
                else:
                    action_type = "click"
                action_coords = act['press_coords']
            elif self._scroll_acc['dx'] != 0 or self._scroll_acc['dy'] != 0:
                action_type = "scroll"
            elif self._completed_key_actions:
                action_type = "hotkey"

            action_details = {
                'mouse': self._completed_mouse_actions,
                'keys': self._completed_key_actions,
                'scroll': {
                    'dx_total': self._scroll_acc['dx'],
                    'dy_total': self._scroll_acc['dy'],
                    'direction': 'down' if self._scroll_acc['dy'] < 0 else 'up' if self._scroll_acc['dy'] > 0 else 'horizontal' if self._scroll_acc['dx'] != 0 else 'none'
                }
            }

        name = f"action_{seq:04d}_after"
        path = self.data_store.screenshot_path(tid, name)

        print(f"   📸 Post-screenshot #{seq}…")
        ok = self.screenshotter.capture(path)

        with self._lock:
            rec = ActionRecord(
                id=uuid.uuid4().hex,
                task_id=tid,
                task_description=self.current_task.description,
                sequence_number=seq,
                timestamp_before=self._pre_ss_time,
                timestamp_action=self._action_time,
                timestamp_after=datetime.now(timezone.utc).isoformat(),
                elapsed_since_task_start=time.monotonic() - self.task_start_mono,
                pre_screenshot=self._pre_ss_name,
                post_screenshot=(name + '.png') if ok else 'FAILED',
                action_type=action_type,
                action_coords=action_coords,
                action_details=action_details,
                os_name=OS_NAME,
                session_type=SESSION_TYPE,
                screen_resolution=self.resolution,
            )
            self.current_task.actions.append(asdict(rec))
            self.data_store.save_task(self.current_task)
            self.state = 'TASK_ACTIVE'

        self.overlay.update_state('TASK_ACTIVE', f'#{seq} ✓')
        print(f"   ✅ Action #{seq} recorded: {action_type} @ {action_coords}")
        print(f"      (Keys: {[k['key'] for k in action_details['keys']]}, Mouse: {[m['button'] for m in action_details['mouse']]})")
        print("   Press Ctrl+F9 for next action, or Ctrl+F12 to end task.\n")


# ============================================================
# Entry Point
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='CUA_Collector - UI Action Data Collector')
    parser.add_argument('--data-dir', default='./data', help='Directory to store collected data')
    parser.add_argument('--debounce', type=float, default=0.5, help='Debounce delay in seconds')
    args = parser.parse_args()

    collector = Collector(data_dir=args.data_dir)
    collector.DEBOUNCE_SEC = args.debounce
    collector.run()


if __name__ == '__main__':
    main()
