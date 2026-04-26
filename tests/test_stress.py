#!/usr/bin/env python3
"""
Stress Test — Validates the C++ capture engine under synthetic load.

Tests:
  1. Ring buffer + action engine with 200 synthetic actions at 0.5s intervals
  2. Verifies pre-frame timestamps are ≤ event_ts - 0.2s
  3. Verifies post-frame timestamps are ≥ event_ts + 0.2s
  4. Reports timing statistics
"""

import sys
import time
import os
from pathlib import Path

# Add build dir to path
build_dir = Path(__file__).parent.parent / 'build'
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))
if build_dir.exists():
    sys.path.insert(0, str(build_dir))
    for config_name in ('Release', 'RelWithDebInfo', 'Debug', 'MinSizeRel'):
        config_dir = build_dir / config_name
        if config_dir.exists():
            sys.path.insert(0, str(config_dir))

try:
    import cua_capture
except ImportError:
    print("❌ cua_capture module not found!")
    print("   Build it first: cmake -S . -B build && cmake --build build -j$(nproc)")
    sys.exit(1)

import cross_platform_capture


def _prime_frames(engine, width=320, height=240, frame_count=100):
    for f in range(frame_count):
        engine.inject_frame(f * 0.1, width, height)


def _drain_native_actions(engine, expected, timeout=5.0):
    completed = []
    deadline = time.time() + timeout
    while len(completed) < expected and time.time() < deadline:
        action = engine.pop_action()
        if action is not None:
            completed.append(action)
        else:
            time.sleep(0.01)
    return completed


def _complete_python_actions(engine, expected):
    completed = []
    deadline = time.time() + 2.0
    while len(completed) < expected and time.time() < deadline:
        engine._check_pending_completions()
        action = engine.pop_action()
        if action is not None:
            completed.append(action)
        else:
            time.sleep(0.01)
    return completed


def _keys(action):
    return list(getattr(action, "keys_pressed", []) or [])


def _assert_no_standalone_ctrl(actions):
    return all(
        not (action.type == "hotkey" and _keys(action) == ["ctrl_l"])
        for action in actions
    )


def test_synthetic_stress():
    """
    Test the engine with synthetic frames and clicks.
    No PipeWire or evdev needed — uses inject_frame() and inject_mouse_click().
    """
    print("=" * 60)
    print("  Stress Test — 200 Actions @ 0.5s Interval")
    print("  Resolution: 3840 × 2400")
    print("=" * 60)

    # Create engine with small resolution for speed
    # (we're not testing actual screenshot quality here)
    W, H = 320, 240  # Small for synthetic test speed
    engine = cua_capture.CaptureEngine(
        buffer_capacity=20,
        max_width=W,
        max_height=H,
        target_fps=10,
    )
    # Don't init_portal or start real capture — we'll inject frames manually

    # Start only the action engine (it will process injected events)
    engine.start()
    time.sleep(0.1)  # Let threads start

    NUM_ACTIONS = 200
    ACTION_INTERVAL = 0.5
    PRE_OFFSET = 0.2
    POST_OFFSET = 0.2
    EPS = 1e-9

    completed = []
    start_time = time.time()

    # Simulate the entire timeline
    total_sim_time = (NUM_ACTIONS + 1) * ACTION_INTERVAL + 1.0
    sim_fps = 10
    total_frames = int(total_sim_time * sim_fps) + 10

    print(f"\n  Simulating {total_frames} frames over {total_sim_time:.1f}s...")
    print(f"  Injecting {NUM_ACTIONS} clicks at {ACTION_INTERVAL}s intervals...")

    action_injected = 0

    for f in range(total_frames):
        sim_t = f * (1.0 / sim_fps)

        # Inject frame
        engine.inject_frame(sim_t, W, H)

        # Inject click if it's time
        next_action_t = action_injected * ACTION_INTERVAL
        if action_injected < NUM_ACTIONS and sim_t >= next_action_t + 0.05:
            engine.inject_mouse_click(
                next_action_t,
                100 + action_injected,
                200 + action_injected,
                "left"
            )
            action_injected += 1

        # Poll for completed actions
        while True:
            action = engine.pop_action()
            if action is None:
                break
            completed.append(action)

        # Small delay to let worker thread process
        time.sleep(0.001)

    # Wait for remaining completions
    print("  Waiting for remaining actions to complete...")
    deadline = time.time() + 5.0
    while len(completed) < NUM_ACTIONS and time.time() < deadline:
        action = engine.pop_action()
        if action is not None:
            completed.append(action)
        else:
            time.sleep(0.01)

    engine.stop()
    elapsed = time.time() - start_time

    # ── Analysis ──────────────────────────────────────────────

    print(f"\n  Results:")
    print(f"    Actions injected:  {action_injected}")
    print(f"    Actions completed: {len(completed)}")
    print(f"    Wall time:         {elapsed:.1f}s")
    print(f"    Frames generated:  {total_frames}")

    if not completed:
        print("\n  ❌ FAIL: No actions completed!")
        return False

    # Timing analysis
    pre_ok = 0
    post_ok = 0
    pre_deltas = []
    post_deltas = []
    degraded = 0

    for action in completed:
        event_ts = action.event_ts

        # Pre-frame check: should be ≤ event_ts - 0.2
        if action.pre_frame_ts > 0:
            pre_delta = event_ts - action.pre_frame_ts
            pre_deltas.append(pre_delta)
            if pre_delta + EPS >= PRE_OFFSET:
                pre_ok += 1

        if action.pre_degraded:
            degraded += 1

        # Post-frame check: should be ≥ event_ts + 0.2
        if action.post_frame_ts > 0:
            post_delta = action.post_frame_ts - event_ts
            post_deltas.append(post_delta)
            if post_delta + EPS >= POST_OFFSET:
                post_ok += 1

    print(f"\n  Timing Analysis:")
    if pre_deltas:
        print(f"    Pre-frame delta (event - pre):")
        print(f"      Min:  {min(pre_deltas):.3f}s")
        print(f"      Max:  {max(pre_deltas):.3f}s")
        print(f"      Mean: {sum(pre_deltas)/len(pre_deltas):.3f}s")
        print(f"      OK:   {pre_ok}/{len(pre_deltas)} (≥ 0.2s)")

    if post_deltas:
        print(f"    Post-frame delta (post - event):")
        print(f"      Min:  {min(post_deltas):.3f}s")
        print(f"      Max:  {max(post_deltas):.3f}s")
        print(f"      Mean: {sum(post_deltas)/len(post_deltas):.3f}s")
        print(f"      OK:   {post_ok}/{len(post_deltas)} (≥ 0.2s)")

    print(f"    Degraded pre-frames: {degraded}/{len(completed)}")

    # ── Pass/Fail ─────────────────────────────────────────────
    completion_rate = len(completed) / NUM_ACTIONS
    pre_rate = pre_ok / len(completed) if completed else 0
    post_rate = post_ok / len(completed) if completed else 0

    passed = True
    print(f"\n  Checks:")

    if completion_rate >= 0.95:
        print(f"    ✅ Completion rate: {completion_rate*100:.1f}% (≥ 95%)")
    else:
        print(f"    ❌ Completion rate: {completion_rate*100:.1f}% (< 95%)")
        passed = False

    if pre_rate >= 0.90:
        print(f"    ✅ Pre-frame accuracy: {pre_rate*100:.1f}% (≥ 90%)")
    else:
        print(f"    ❌ Pre-frame accuracy: {pre_rate*100:.1f}% (< 90%)")
        passed = False

    if post_rate >= 0.90:
        print(f"    ✅ Post-frame accuracy: {post_rate*100:.1f}% (≥ 90%)")
    else:
        print(f"    ❌ Post-frame accuracy: {post_rate*100:.1f}% (< 90%)")
        passed = False

    print(f"\n  {'✅ ALL TESTS PASSED' if passed else '❌ SOME TESTS FAILED'}")
    return passed


def test_mouse_drag_buttons():
    """
    Regression test for button-specific drag handling.
    Verifies left, middle, and right drags survive the full Python binding path.
    """
    print("\n" + "=" * 60)
    print("  Mouse Drag Button Test — left / middle / right")
    print("=" * 60)

    W, H = 320, 240
    engine = cua_capture.CaptureEngine(
        buffer_capacity=80,
        max_width=W,
        max_height=H,
        target_fps=10,
    )

    # Preload frames covering all pre/post lookups. The action engine accepts
    # injected frames even when live PipeWire capture is not running.
    for f in range(70):
        engine.inject_frame(f * 0.1, W, H)

    engine.start()
    time.sleep(0.1)

    cases = [
        ("left", 1.0, (20, 30), (80, 90)),
        ("middle", 2.0, (40, 50), (110, 130)),
        ("right", 3.0, (60, 70), (140, 160)),
    ]
    for button, ts, press, release in cases:
        engine.inject_mouse_drag(
            ts,
            press[0], press[1],
            release[0], release[1],
            button,
            0.5,
        )

    completed = []
    deadline = time.time() + 5.0
    while len(completed) < len(cases) and time.time() < deadline:
        action = engine.pop_action()
        if action is not None:
            completed.append(action)
        else:
            time.sleep(0.01)

    engine.stop()

    if len(completed) != len(cases):
        print(f"    ❌ Completed {len(completed)}/{len(cases)} drags")
        return False

    completed.sort(key=lambda action: action.event_ts)
    passed = True
    for action, (button, _ts, press, release) in zip(completed, cases):
        checks = [
            action.type == "drag",
            action.button_name == button,
            (action.press_x, action.press_y) == press,
            (action.release_x, action.release_y) == release,
        ]
        if all(checks):
            print(
                f"    ✅ {button} drag: "
                f"{press[0]},{press[1]} -> {release[0]},{release[1]}"
            )
        else:
            print(
                f"    ❌ {button} drag mismatch: "
                f"type={action.type}, button={action.button_name}, "
                f"press=({action.press_x},{action.press_y}), "
                f"release=({action.release_x},{action.release_y})"
            )
            passed = False

    return passed


def test_native_modifier_keyboard_combos():
    print("\n" + "=" * 60)
    print("  Native Modifier Keyboard Combo Test")
    print("=" * 60)

    engine = cua_capture.CaptureEngine(120, 320, 240, 10)
    _prime_frames(engine)
    engine.inject_key_event(1.0, "ctrl_l", True, 10, 10)
    engine.inject_key_event(1.1, "c", True, 10, 10)
    engine.inject_key_event(1.15, "c", False, 10, 10)
    engine.inject_key_event(1.3, "s", True, 10, 10)
    engine.inject_key_event(1.35, "s", False, 10, 10)
    engine.inject_key_event(1.5, "ctrl_l", False, 10, 10)
    engine.inject_key_event(2.0, "super_l", True, 20, 20)
    engine.inject_key_event(2.1, "o", True, 20, 20)
    engine.inject_key_event(2.15, "o", False, 20, 20)
    engine.inject_key_event(2.3, "super_l", False, 20, 20)
    engine.inject_key_event(3.0, "a", True, 30, 30)
    engine.inject_key_event(3.05, "a", False, 30, 30)

    engine.start()
    completed = _drain_native_actions(engine, 3)
    engine.stop()
    completed.sort(key=lambda action: action.event_ts)
    combos = [_keys(action) for action in completed]
    expected = [["ctrl_l", "c"], ["ctrl_l", "s"], ["super_l", "o"]]
    passed = combos == expected and _assert_no_standalone_ctrl(completed)
    print(f"    {'✅' if passed else '❌'} combos: {combos}")
    return passed


def test_native_modifier_mouse_combos():
    print("\n" + "=" * 60)
    print("  Native Modifier Mouse Combo Test")
    print("=" * 60)

    engine = cua_capture.CaptureEngine(160, 320, 240, 10)
    _prime_frames(engine, frame_count=140)
    engine.inject_key_event(1.0, "ctrl_l", True, 10, 10)
    engine.inject_mouse_click(1.1, 40, 50, "left")
    engine.inject_mouse_drag(2.0, 50, 60, 80, 90, "left", 0.5)
    engine.inject_mouse_click(3.0, 90, 100, "left")
    engine.inject_mouse_click(3.15, 90, 100, "left")
    engine.inject_scroll(4.0, 100, 110, 0, -1)
    engine.inject_key_event(4.3, "ctrl_l", False, 10, 10)

    engine.start()
    completed = _drain_native_actions(engine, 4)
    engine.stop()
    completed.sort(key=lambda action: action.event_ts)
    types = [action.type for action in completed]
    combos = [_keys(action) for action in completed]
    expected_types = ["click", "drag", "double_click", "scroll"]
    passed = (
        types == expected_types
        and combos == [["ctrl_l"], ["ctrl_l"], ["ctrl_l"], ["ctrl_l"]]
        and _assert_no_standalone_ctrl(completed)
    )
    print(f"    {'✅' if passed else '❌'} types={types}, keys={combos}")
    return passed


def test_python_fallback_modifier_combos():
    print("\n" + "=" * 60)
    print("  Python Fallback Modifier Combo Test")
    print("=" * 60)

    engine = cross_platform_capture.CaptureEngine(buffer_capacity=160)
    _prime_frames(engine, frame_count=140)
    engine.inject_key_event(1.0, "ctrl_l", True, 10, 10)
    engine.inject_key_event(1.1, "c", True, 10, 10)
    engine.inject_key_event(1.15, "c", False, 10, 10)
    engine.inject_key_event(1.3, "s", True, 10, 10)
    engine.inject_key_event(1.35, "s", False, 10, 10)
    engine.inject_mouse_click(2.0, 40, 50, "left")
    engine.inject_mouse_drag(3.0, 50, 60, 80, 90, "left", 0.5)
    engine.inject_mouse_double_click(4.0, 90, 100, "left")
    engine.inject_scroll(5.0, 100, 110, 0, -1)
    engine.inject_key_event(5.3, "ctrl_l", False, 10, 10)
    engine.inject_key_event(6.0, "super_l", True, 20, 20)
    engine.inject_key_event(6.1, "o", True, 20, 20)
    engine.inject_key_event(6.15, "o", False, 20, 20)
    engine.inject_key_event(6.3, "super_l", False, 20, 20)
    engine.inject_key_event(7.0, "a", True, 30, 30)
    engine.inject_key_event(7.05, "a", False, 30, 30)

    completed = _complete_python_actions(engine, 7)
    completed.sort(key=lambda action: action.event_ts)
    types = [action.type for action in completed]
    combos = [_keys(action) for action in completed]
    expected_types = [
        "hotkey",
        "hotkey",
        "click",
        "drag",
        "double_click",
        "scroll",
        "hotkey",
    ]
    expected_combos = [
        ["ctrl_l", "c"],
        ["ctrl_l", "s"],
        ["ctrl_l"],
        ["ctrl_l"],
        ["ctrl_l"],
        ["ctrl_l"],
        ["super_l", "o"],
    ]
    passed = types == expected_types and combos == expected_combos
    print(f"    {'✅' if passed else '❌'} types={types}, keys={combos}")
    return passed


if __name__ == '__main__':
    success = test_synthetic_stress()
    success = test_mouse_drag_buttons() and success
    success = test_native_modifier_keyboard_combos() and success
    success = test_native_modifier_mouse_combos() and success
    success = test_python_fallback_modifier_combos() and success
    sys.exit(0 if success else 1)
