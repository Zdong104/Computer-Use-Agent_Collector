"""
CUA Collector V2 cross-platform capture backend.

This module mirrors the small Python-facing surface of the native
``cua_capture`` pybind11 module, but uses mss for screenshots and pynput for
input. It is intended for X11, Windows, and macOS launchers while keeping the
root V2 collector workflow and output schema unchanged.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import math
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple

BACKEND_NAME = "mss+pynput"


class HotkeyType(Enum):
    START_TASK = 1
    SCREENSHOT = 2
    END_TASK = 3
    DROP_ACTION = 4


class ActionType(Enum):
    CLICK = 1
    DOUBLE_CLICK = 2
    DRAG = 3
    SCROLL = 4
    HOTKEY = 5
    UNKNOWN = 6


class RawEventType(Enum):
    MOUSE_BTN_DOWN = 1
    MOUSE_BTN_UP = 2
    KEYBOARD_DOWN = 3
    KEYBOARD_UP = 4
    SCROLL_EVENT = 5


@dataclass
class KeyActionRecord:
    key_name: str
    press_ts: float
    release_ts: float


@dataclass
class CompletedAction:
    action_id: int
    type: str
    event_ts: float
    x: int = 0
    y: int = 0
    button_name: str = ""
    scroll_dx: int = 0
    scroll_dy: int = 0
    pre_frame_id: int = 0
    pre_frame_ts: float = 0.0
    pre_frame_rgb: bytes = b""
    pre_w: int = 0
    pre_h: int = 0
    pre_degraded: bool = False
    post_frame_id: int = 0
    post_frame_ts: float = 0.0
    post_frame_rgb: bytes = b""
    post_w: int = 0
    post_h: int = 0
    press_x: int = 0
    press_y: int = 0
    release_x: int = 0
    release_y: int = 0
    press_ts: float = 0.0
    release_ts: float = 0.0
    keys_pressed: List[str] = field(default_factory=list)
    key_actions: List[KeyActionRecord] = field(default_factory=list)


@dataclass
class _Frame:
    frame_id: int
    timestamp_sec: float
    width: int
    height: int
    rgb: bytes


@dataclass
class _PendingAction:
    action_id: int
    type: str
    event_ts: float
    required_post_ts: float
    x: int = 0
    y: int = 0
    button_name: str = ""
    scroll_dx: int = 0
    scroll_dy: int = 0
    pre_frame: Optional[_Frame] = None
    pre_degraded: bool = False
    creation_ts: float = 0.0
    last_event_ts: float = 0.0
    press_x: int = 0
    press_y: int = 0
    release_x: int = 0
    release_y: int = 0
    press_ts: float = 0.0
    release_ts: float = 0.0
    keys_pressed: List[str] = field(default_factory=list)
    key_actions: List[KeyActionRecord] = field(default_factory=list)


class CaptureEngine:
    PRE_FRAME_OFFSET = 0.200
    POST_FRAME_OFFSET = 0.200
    POST_FRAME_TIMEOUT = 5.0
    DOUBLE_CLICK_MAX_INTERVAL = 0.250
    DOUBLE_CLICK_MAX_DISTANCE = 8.0
    SCROLL_MERGE_WINDOW = 0.200
    DRAG_MIN_DISTANCE = 3.0
    DRAG_MIN_HOLD_TIME = 0.300
    MODIFIER_DEBOUNCE_MS = 200.0

    def __init__(
        self,
        buffer_capacity: int = 10,
        max_width: int = 3840,
        max_height: int = 2400,
        target_fps: int = 10,
    ):
        self.buffer_capacity = max(2, int(buffer_capacity))
        self.max_width = max_width
        self.max_height = max_height
        self.target_fps = max(1, int(target_fps))

        self._running = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._keyboard_listener = None
        self._mouse_listener = None

        self._frames: Deque[_Frame] = deque(maxlen=self.buffer_capacity)
        self._frame_mu = threading.Lock()
        self._next_frame_id = 1
        self._total_frames = 0

        self._pending: List[_PendingAction] = []
        self._pending_mu = threading.Lock()
        self._completed: Deque[CompletedAction] = deque()
        self._completed_mu = threading.Lock()
        self._next_action_id = 1

        self._hotkeys: Deque[HotkeyType] = deque()
        self._hotkey_mu = threading.Lock()

        self._ctrl_pressed = False
        self._active_keys: Dict[str, float] = {}
        self._consumed_modifiers = set()
        self._consumed_keys = set()
        self._active_buttons: Dict[str, Tuple[float, int, int, List[str]]] = {}
        self._last_click_ts = 0.0
        self._last_click_x = 0
        self._last_click_y = 0
        self._last_click_button = ""
        self._cursor_pos = (0, 0)
        self._capture_origin = (0, 0)

    def init_portal(self, gjs_script_path: str = "") -> bool:
        try:
            import mss  # noqa: F401
            import pynput  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except Exception as exc:
            print(f"  Failed to initialize {BACKEND_NAME} backend: {exc}")
            print("  Install Python dependencies with: pip install -r requirements.txt")
            return False

    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._capture_thread.start()
        self._start_input_listeners()
        self._worker_thread.start()
        print(f"[CaptureEngine] Cross-platform backend started ({BACKEND_NAME})")

    def stop(self):
        self._running.clear()
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        for thread in (self._capture_thread, self._worker_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        print("[CaptureEngine] Cross-platform backend stopped")

    def pop_action(self):
        with self._completed_mu:
            if not self._completed:
                return None
            return self._completed.popleft()

    def pop_hotkey(self):
        with self._hotkey_mu:
            if not self._hotkeys:
                return None
            return self._hotkeys.popleft()

    @property
    def completed_count(self) -> int:
        with self._completed_mu:
            return len(self._completed)

    @property
    def pending_count(self) -> int:
        with self._pending_mu:
            return len(self._pending)

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def latest_frame_ts(self) -> float:
        with self._frame_mu:
            return self._frames[-1].timestamp_sec if self._frames else 0.0

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def get_cursor_position(self) -> Tuple[int, int]:
        return self._cursor_pos

    def inject_mouse_click(
        self, ts: float, x: int, y: int, button: str = "left"
    ):
        self._enqueue_mouse_action(
            "click", ts, x, y, button, x, y, x, y, ts, ts + 0.05,
            self._active_modifiers()
        )

    def inject_mouse_drag(
        self,
        ts: float,
        press_x: int,
        press_y: int,
        release_x: int,
        release_y: int,
        button: str = "left",
        duration: float = 0.5,
    ):
        self._enqueue_mouse_action(
            "drag",
            ts,
            press_x,
            press_y,
            button,
            press_x,
            press_y,
            release_x,
            release_y,
            ts,
            ts + duration,
            self._active_modifiers(),
        )

    def inject_mouse_double_click(
        self, ts: float, x: int, y: int, button: str = "left"
    ):
        self._enqueue_mouse_action(
            "double_click", ts, x, y, button, x, y, x, y, ts, ts + 0.15,
            self._active_modifiers()
        )

    def inject_key_event(
        self,
        ts: float,
        key_name: str,
        pressed: bool,
        x: int = 0,
        y: int = 0,
    ):
        self._cursor_pos = (int(x), int(y))
        self._handle_key_event(key_name, pressed, ts)

    def inject_scroll(self, ts: float, x: int, y: int, dx: int, dy: int):
        self._cursor_pos = (int(x), int(y))
        self._enqueue_scroll_action(ts, int(x), int(y), int(dx), int(dy))

    def inject_frame(self, ts: float, width: int, height: int):
        rgb = bytes([128]) * (width * height * 3)
        self._commit_frame(ts, width, height, rgb)

    def _capture_loop(self):
        import mss
        from PIL import Image

        frame_interval = 1.0 / self.target_fps
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            self._capture_origin = (int(monitor.get("left", 0)), int(monitor.get("top", 0)))
            while self._running.is_set():
                start = time.monotonic()
                try:
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    self._commit_frame(start, img.width, img.height, img.tobytes())
                except Exception as exc:
                    print(f"  Screenshot capture error ({BACKEND_NAME}): {exc}")
                    time.sleep(1.0)
                    continue

                elapsed = time.monotonic() - start
                time.sleep(max(0.0, frame_interval - elapsed))

    def _commit_frame(self, ts: float, width: int, height: int, rgb: bytes):
        with self._frame_mu:
            frame = _Frame(self._next_frame_id, ts, width, height, rgb)
            self._next_frame_id += 1
            self._total_frames += 1
            self._frames.append(frame)

    def _start_input_listeners(self):
        from pynput import keyboard, mouse

        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll,
            on_move=self._on_move,
        )
        self._keyboard_listener.start()
        self._mouse_listener.start()

    def _on_move(self, x: int, y: int):
        self._cursor_pos = self._to_capture_coords(x, y)

    def _on_click(self, x: int, y: int, button, pressed: bool):
        now = time.monotonic()
        button_name = self._button_name(button)
        x, y = self._to_capture_coords(x, y)
        self._cursor_pos = (x, y)

        if pressed:
            self._active_buttons[button_name] = (
                now,
                x,
                y,
                self._active_modifiers(),
            )
            return

        down_ts, down_x, down_y, down_modifiers = self._active_buttons.pop(
            button_name, (now, x, y, [])
        )
        action_modifiers = self._merge_modifier_lists(
            down_modifiers, self._active_modifiers()
        )
        hold_time = max(0.0, now - down_ts)
        distance = math.hypot(x - down_x, y - down_y)
        if distance > self.DRAG_MIN_DISTANCE or hold_time > self.DRAG_MIN_HOLD_TIME:
            action_type = "drag"
            action_x, action_y = down_x, down_y
        else:
            since_last = now - self._last_click_ts
            click_distance = math.hypot(x - self._last_click_x, y - self._last_click_y)
            if (
                since_last < self.DOUBLE_CLICK_MAX_INTERVAL
                and click_distance < self.DOUBLE_CLICK_MAX_DISTANCE
                and button_name == self._last_click_button
            ):
                action_type = "double_click"
            else:
                action_type = "click"
            action_x, action_y = x, y
            self._last_click_ts = now
            self._last_click_x = x
            self._last_click_y = y
            self._last_click_button = button_name

        self._enqueue_mouse_action(
            action_type,
            down_ts,
            action_x,
            action_y,
            button_name,
            down_x,
            down_y,
            x,
            y,
            down_ts,
            now,
            action_modifiers,
        )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int):
        now = time.monotonic()
        x, y = self._to_capture_coords(x, y)
        self._cursor_pos = (x, y)
        self._enqueue_scroll_action(now, x, y, int(dx), int(dy))

    def _on_key_press(self, key):
        from pynput import keyboard

        now = time.monotonic()
        key_name = self._key_name(key)
        if not key_name:
            return

        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = True

        if self._ctrl_pressed:
            if key == keyboard.Key.f8:
                self._push_hotkey(HotkeyType.START_TASK)
            elif key == keyboard.Key.f9:
                self._push_hotkey(HotkeyType.SCREENSHOT)
            elif key == keyboard.Key.f12:
                self._push_hotkey(HotkeyType.END_TASK)

        if key == keyboard.Key.esc:
            self._push_hotkey(HotkeyType.DROP_ACTION)

        self._handle_key_event(key_name, True, now)

    def _on_key_release(self, key):
        from pynput import keyboard

        now = time.monotonic()
        key_name = self._key_name(key)
        if not key_name:
            return

        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = False

        self._handle_key_event(key_name, False, now)

    def _push_hotkey(self, hotkey: HotkeyType):
        with self._hotkey_mu:
            self._hotkeys.append(hotkey)

    def _enqueue_mouse_action(
        self,
        action_type: str,
        event_ts: float,
        x: int,
        y: int,
        button_name: str,
        press_x: int,
        press_y: int,
        release_x: int,
        release_y: int,
        press_ts: float,
        release_ts: float,
        modifiers: Optional[List[str]] = None,
    ):
        pending = self._create_pending(action_type, event_ts, x, y)
        pending.button_name = button_name
        pending.press_x = press_x
        pending.press_y = press_y
        pending.release_x = release_x
        pending.release_y = release_y
        pending.press_ts = press_ts
        pending.release_ts = release_ts
        pending.last_event_ts = release_ts
        pending.required_post_ts = release_ts + self.POST_FRAME_OFFSET
        self._attach_keys_to_pending(pending, modifiers or [], release_ts)
        self._add_pending(pending)

    def _enqueue_scroll_action(self, event_ts: float, x: int, y: int, dx: int, dy: int):
        modifiers = self._active_modifiers()
        with self._pending_mu:
            if self._pending:
                last = self._pending[-1]
                if (
                    last.type == "scroll"
                    and event_ts - last.last_event_ts <= self.SCROLL_MERGE_WINDOW
                ):
                    last.scroll_dx += dx
                    last.scroll_dy += dy
                    last.last_event_ts = event_ts
                    last.required_post_ts = event_ts + self.POST_FRAME_OFFSET
                    self._attach_keys_to_pending(last, modifiers, event_ts)
                    return

        pending = self._create_pending("scroll", event_ts, x, y)
        pending.scroll_dx = dx
        pending.scroll_dy = dy
        pending.last_event_ts = event_ts
        self._attach_keys_to_pending(pending, modifiers, event_ts)
        self._add_pending(pending)

    def _enqueue_key_action(
        self,
        press_ts: float,
        release_ts: float,
        combo: List[str],
        key_name: str,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ):
        if x is None or y is None:
            x, y = self._cursor_pos
        pending = self._create_pending("hotkey", press_ts, x, y)
        pending.release_ts = release_ts
        pending.last_event_ts = release_ts
        pending.required_post_ts = release_ts + self.POST_FRAME_OFFSET
        self._attach_keys_to_pending(
            pending, combo, release_ts, key_name, press_ts, release_ts
        )
        self._add_pending(pending)

    def _handle_key_event(self, key_name: str, pressed: bool, ts: float):
        if pressed:
            if key_name not in self._active_keys:
                self._active_keys[key_name] = ts
                if key_name in self._modifier_names():
                    self._consumed_modifiers.discard(key_name)
                else:
                    self._consumed_keys.discard(key_name)
            return

        if key_name not in self._active_keys:
            return

        press_ts = self._active_keys[key_name]
        if key_name in self._modifier_names():
            modifiers = self._active_modifiers()
            active_non_modifiers = [
                key for key in self._active_keys if key not in self._modifier_names()
            ]
            held_ms = (ts - press_ts) * 1000.0
            if held_ms >= self.MODIFIER_DEBOUNCE_MS:
                for trigger_key in active_non_modifiers:
                    if trigger_key in self._consumed_keys:
                        continue
                    combo = self._append_unique(list(modifiers), trigger_key)
                    self._enqueue_key_action(
                        self._active_keys[trigger_key],
                        ts,
                        combo,
                        trigger_key,
                        *self._cursor_pos,
                    )
                    self._consumed_keys.add(trigger_key)
            self._active_keys.pop(key_name, None)
            self._consumed_modifiers.discard(key_name)
            return

        modifiers = self._active_modifiers()
        combo = self._append_unique(list(modifiers), key_name)
        self._active_keys.pop(key_name, None)

        if key_name in self._consumed_keys:
            self._consumed_keys.discard(key_name)
            return
        if self._is_plain_key_noise(combo):
            return

        self._enqueue_key_action(press_ts, ts, combo, key_name)

    def _active_modifiers(self) -> List[str]:
        modifier_names = self._modifier_names()
        return [key for key in self._active_keys if key in modifier_names]

    @staticmethod
    def _append_unique(keys: List[str], key_name: str) -> List[str]:
        if key_name not in keys:
            keys.append(key_name)
        return keys

    @staticmethod
    def _merge_modifier_lists(first: List[str], second: List[str]) -> List[str]:
        merged: List[str] = []
        for key in first + second:
            if key not in merged:
                merged.append(key)
        return merged

    def _attach_keys_to_pending(
        self,
        pending: _PendingAction,
        keys: List[str],
        action_release_ts: float,
        trigger_key: str = "",
        trigger_press_ts: float = 0.0,
        trigger_release_ts: float = 0.0,
    ):
        for key in keys:
            if key not in pending.keys_pressed:
                pending.keys_pressed.append(key)

            if key == trigger_key:
                press_ts = trigger_press_ts
                release_ts = trigger_release_ts
            else:
                press_ts = self._active_keys.get(key, pending.event_ts)
                release_ts = action_release_ts

            existing = next(
                (record for record in pending.key_actions if record.key_name == key),
                None,
            )
            if existing is None:
                pending.key_actions.append(KeyActionRecord(key, press_ts, release_ts))
            elif key != trigger_key:
                existing.release_ts = max(existing.release_ts, release_ts)

        for key in keys:
            if key in self._modifier_names():
                self._consumed_modifiers.add(key)

    def _create_pending(self, action_type: str, event_ts: float, x: int, y: int):
        target_ts = event_ts - self.PRE_FRAME_OFFSET
        pre_frame, degraded = self._find_frame_at_or_before(target_ts)
        pending = _PendingAction(
            action_id=self._next_action_id,
            type=action_type,
            event_ts=event_ts,
            required_post_ts=event_ts + self.POST_FRAME_OFFSET,
            x=x,
            y=y,
            pre_frame=pre_frame,
            pre_degraded=degraded,
            creation_ts=time.monotonic(),
            last_event_ts=event_ts,
        )
        self._next_action_id += 1
        return pending

    def _add_pending(self, pending: _PendingAction):
        with self._pending_mu:
            self._pending.append(pending)

    def _find_frame_at_or_before(self, target_ts: float) -> Tuple[Optional[_Frame], bool]:
        with self._frame_mu:
            if not self._frames:
                return None, True
            candidate = None
            for frame in reversed(self._frames):
                if frame.timestamp_sec <= target_ts:
                    candidate = frame
                    break
            if candidate is not None:
                return candidate, False
            return self._frames[0], True

    def _find_post_frame(self, target_ts: float) -> Optional[_Frame]:
        with self._frame_mu:
            for frame in self._frames:
                if frame.timestamp_sec >= target_ts:
                    return frame
            return None

    def _worker_loop(self):
        while self._running.is_set():
            self._check_pending_completions()
            time.sleep(0.02)

    def _check_pending_completions(self):
        completed: List[CompletedAction] = []
        now = time.monotonic()
        with self._pending_mu:
            keep: List[_PendingAction] = []
            for pending in self._pending:
                post = self._find_post_frame(pending.required_post_ts)
                timed_out = now - pending.creation_ts > self.POST_FRAME_TIMEOUT
                if post is None and not timed_out:
                    keep.append(pending)
                    continue
                if post is None:
                    post = self._latest_frame()
                completed.append(self._complete_pending(pending, post))
            self._pending = keep

        if completed:
            with self._completed_mu:
                self._completed.extend(completed)

    def _latest_frame(self) -> Optional[_Frame]:
        with self._frame_mu:
            return self._frames[-1] if self._frames else None

    @staticmethod
    def _complete_pending(
        pending: _PendingAction, post: Optional[_Frame]
    ) -> CompletedAction:
        pre = pending.pre_frame
        return CompletedAction(
            action_id=pending.action_id,
            type=pending.type,
            event_ts=pending.event_ts,
            x=pending.x,
            y=pending.y,
            button_name=pending.button_name,
            scroll_dx=pending.scroll_dx,
            scroll_dy=pending.scroll_dy,
            pre_frame_id=pre.frame_id if pre else 0,
            pre_frame_ts=pre.timestamp_sec if pre else 0.0,
            pre_frame_rgb=pre.rgb if pre else b"",
            pre_w=pre.width if pre else 0,
            pre_h=pre.height if pre else 0,
            pre_degraded=pending.pre_degraded,
            post_frame_id=post.frame_id if post else 0,
            post_frame_ts=post.timestamp_sec if post else 0.0,
            post_frame_rgb=post.rgb if post else b"",
            post_w=post.width if post else 0,
            post_h=post.height if post else 0,
            press_x=pending.press_x,
            press_y=pending.press_y,
            release_x=pending.release_x,
            release_y=pending.release_y,
            press_ts=pending.press_ts,
            release_ts=pending.release_ts,
            keys_pressed=pending.keys_pressed,
            key_actions=pending.key_actions,
        )

    @staticmethod
    def _button_name(button) -> str:
        name = getattr(button, "name", str(button)).lower()
        if "left" in name:
            return "left"
        if "right" in name:
            return "right"
        if "middle" in name:
            return "middle"
        return name.replace("button.", "")

    @staticmethod
    def _modifier_names() -> set:
        return {
            "ctrl_l",
            "ctrl_r",
            "shift_l",
            "shift_r",
            "alt_l",
            "alt_r",
            "super_l",
            "super_r",
            "fn",
        }

    @staticmethod
    def _is_plain_key_noise(combo: List[str]) -> bool:
        return bool(combo) and all(len(key) == 1 and key.isalnum() for key in combo)

    def _to_capture_coords(self, x: int, y: int) -> Tuple[int, int]:
        left, top = self._capture_origin
        return (int(x) - left, int(y) - top)

    @staticmethod
    def _key_name(key) -> Optional[str]:
        from pynput import keyboard

        def key_attr(name: str):
            return getattr(keyboard.Key, name, None)

        modifiers = {
            key_attr("ctrl_l"): "ctrl_l",
            key_attr("ctrl_r"): "ctrl_r",
            key_attr("shift_l"): "shift_l",
            key_attr("shift_r"): "shift_r",
            key_attr("alt_l"): "alt_l",
            key_attr("alt_r"): "alt_r",
            key_attr("cmd_l"): "super_l",
            key_attr("cmd_r"): "super_r",
        }
        specials = {
            key_attr("esc"): "esc",
            key_attr("backspace"): "backspace",
            key_attr("enter"): "enter",
            key_attr("tab"): "tab",
            key_attr("space"): "space",
            key_attr("caps_lock"): "capslock",
            key_attr("delete"): "delete",
            key_attr("insert"): "insert",
            key_attr("home"): "home",
            key_attr("end"): "end",
            key_attr("page_up"): "pageup",
            key_attr("page_down"): "pagedown",
            key_attr("up"): "up",
            key_attr("down"): "down",
            key_attr("left"): "left",
            key_attr("right"): "right",
            key_attr("num_lock"): "numlock",
            key_attr("print_screen"): "print",
            key_attr("scroll_lock"): "scrolllock",
            key_attr("pause"): "pause",
            key_attr("menu"): "menu",
            key_attr("f1"): "f1",
            key_attr("f2"): "f2",
            key_attr("f3"): "f3",
            key_attr("f4"): "f4",
            key_attr("f5"): "f5",
            key_attr("f6"): "f6",
            key_attr("f7"): "f7",
            key_attr("f8"): "f8",
            key_attr("f9"): "f9",
            key_attr("f10"): "f10",
            key_attr("f11"): "f11",
            key_attr("f12"): "f12",
        }
        modifiers.pop(None, None)
        specials.pop(None, None)
        if key in modifiers:
            return modifiers[key]
        if key in specials:
            return specials[key]
        char = getattr(key, "char", None)
        if char:
            return char
        return None
