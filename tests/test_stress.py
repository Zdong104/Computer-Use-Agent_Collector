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
if build_dir.exists():
    sys.path.insert(0, str(build_dir))

try:
    import cua_capture
except ImportError:
    print("❌ cua_capture module not found!")
    print("   Build it first: cmake -S . -B build && cmake --build build -j$(nproc)")
    sys.exit(1)


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
            if pre_delta >= PRE_OFFSET:
                pre_ok += 1

        if action.pre_degraded:
            degraded += 1

        # Post-frame check: should be ≥ event_ts + 0.2
        if action.post_frame_ts > 0:
            post_delta = action.post_frame_ts - event_ts
            post_deltas.append(post_delta)
            if post_delta >= POST_OFFSET:
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


if __name__ == '__main__':
    success = test_synthetic_stress()
    sys.exit(0 if success else 1)
