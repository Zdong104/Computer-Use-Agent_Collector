/**
 * @file benchmark.cpp
 * @brief Standalone C++ benchmark for the V2 capture engine.
 *
 * Tests:
 *   1. Ring buffer write throughput at 3840×2400 RGB
 *   2. Pre-frame / post-frame lookup latency
 *   3. 400-click stress test (200 actions at 0.5s intervals)
 *   4. Memory usage reporting
 */

#include "ring_buffer.h"
#include "action_engine.h"
#include "input_monitor.h"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <thread>

using namespace cua;
using Clock = std::chrono::steady_clock;

static double to_ms(Clock::duration d) {
    return std::chrono::duration<double, std::milli>(d).count();
}

static double to_us(Clock::duration d) {
    return std::chrono::duration<double, std::micro>(d).count();
}

// ─── Get process RSS in MB ────────────────────────────────────
static double get_rss_mb() {
    FILE* f = fopen("/proc/self/statm", "r");
    if (!f) return 0;
    long pages = 0;
    [[maybe_unused]] int n = fscanf(f, "%*ld %ld", &pages);
    fclose(f);
    return pages * 4096.0 / (1024 * 1024);
}

// ═══════════════════════════════════════════════════════════════
// Test 1: Ring Buffer Write Throughput
// ═══════════════════════════════════════════════════════════════

static void test_ring_buffer_write() {
    std::cout << "\n══════════════════════════════════════════════\n"
              << "  Test 1: Ring Buffer Write Throughput\n"
              << "  Resolution: 3840 × 2400, RGB, 10 slots\n"
              << "══════════════════════════════════════════════\n";

    const int W = 3840, H = 2400;
    const size_t frame_bytes = static_cast<size_t>(W) * H * 3;

    double rss_before = get_rss_mb();
    RingBuffer buffer(10, W, H);
    double rss_after = get_rss_mb();

    std::cout << "  Buffer allocated: " << std::fixed << std::setprecision(1)
              << (rss_after - rss_before) << " MB RSS increase\n";
    std::cout << "  Per-frame: " << (frame_bytes / (1024.0 * 1024.0))
              << " MB × 10 = " << (frame_bytes * 10.0 / (1024.0 * 1024.0))
              << " MB\n";

    // Write 100 frames and measure
    const int N = 100;
    std::vector<uint8_t> fake_bgrx(static_cast<size_t>(W) * H * 4, 128);

    auto start = Clock::now();

    for (int i = 0; i < N; i++) {
        auto& slot = buffer.begin_write();
        slot.width = W;
        slot.height = H;
        slot.timestamp_sec = i * 0.1;

        // Simulate BGRx → RGB conversion (optimized: 4 pixels at a time)
        const uint8_t* __restrict__ src = fake_bgrx.data();
        uint8_t* __restrict__ dst = slot.rgb_data.data();
        const size_t pixels = static_cast<size_t>(W) * H;
        const size_t pixels4 = pixels & ~3ULL;
        size_t si = 0, di = 0;
        for (size_t p = 0; p < pixels4; p += 4) {
            dst[di+0]=src[si+2]; dst[di+1]=src[si+1]; dst[di+2]=src[si+0];
            dst[di+3]=src[si+6]; dst[di+4]=src[si+5]; dst[di+5]=src[si+4];
            dst[di+6]=src[si+10]; dst[di+7]=src[si+9]; dst[di+8]=src[si+8];
            dst[di+9]=src[si+14]; dst[di+10]=src[si+13]; dst[di+11]=src[si+12];
            si += 16; di += 12;
        }
        for (size_t p = pixels4; p < pixels; p++) {
            dst[di+0]=src[si+2]; dst[di+1]=src[si+1]; dst[di+2]=src[si+0];
            si += 4; di += 3;
        }
        buffer.commit_write();
    }

    auto elapsed = Clock::now() - start;
    double per_frame_ms = to_ms(elapsed) / N;

    std::cout << "\n  Results:\n"
              << "    Total: " << to_ms(elapsed) << " ms for " << N << " frames\n"
              << "    Per frame: " << std::setprecision(2) << per_frame_ms << " ms\n"
              << "    Max FPS: " << std::setprecision(0) << (1000.0 / per_frame_ms) << "\n"
              << "    " << (per_frame_ms < 100 ? "✅ PASS" : "❌ FAIL")
              << " (target: < 100 ms/frame for 10 FPS)\n";
}

// ═══════════════════════════════════════════════════════════════
// Test 2: Frame Lookup Latency
// ═══════════════════════════════════════════════════════════════

static void test_frame_lookup() {
    std::cout << "\n══════════════════════════════════════════════\n"
              << "  Test 2: Frame Lookup Latency\n"
              << "══════════════════════════════════════════════\n";

    const int W = 3840, H = 2400;
    RingBuffer buffer(10, W, H);

    // Fill buffer with 10 frames at 10 FPS
    for (int i = 0; i < 10; i++) {
        auto& slot = buffer.begin_write();
        slot.width = W;
        slot.height = H;
        slot.timestamp_sec = 100.0 + i * 0.1;
        std::memset(slot.rgb_data.data(), i, slot.rgb_data.size());
        buffer.commit_write();
    }

    // Benchmark pre-frame lookup
    const int LOOKUPS = 1000;
    FrameSlot result;

    auto start = Clock::now();
    for (int i = 0; i < LOOKUPS; i++) {
        buffer.find_pre_frame(100.5, result);  // Should find frame at t=100.5
    }
    auto pre_elapsed = Clock::now() - start;

    // Benchmark post-frame lookup
    start = Clock::now();
    for (int i = 0; i < LOOKUPS; i++) {
        buffer.find_post_frame(100.5, result);
    }
    auto post_elapsed = Clock::now() - start;

    double pre_us = to_us(pre_elapsed) / LOOKUPS;
    double post_us = to_us(post_elapsed) / LOOKUPS;

    std::cout << "  Pre-frame lookup: " << std::fixed << std::setprecision(1)
              << pre_us << " µs/lookup (includes " << (W * H * 3.0 / 1024 / 1024)
              << " MB memcpy)\n"
              << "  Post-frame lookup: " << post_us << " µs/lookup\n"
              << "  " << (pre_us < 50000 ? "✅ PASS" : "❌ FAIL")
              << " (target: < 50 ms)\n";
}

// ═══════════════════════════════════════════════════════════════
// Test 3: 400-Click Stress Test
// ═══════════════════════════════════════════════════════════════

static void test_stress_400_clicks() {
    std::cout << "\n══════════════════════════════════════════════\n"
              << "  Test 3: 400-Click Stress Test\n"
              << "  200 actions @ 0.5s interval, 3840×2400\n"
              << "══════════════════════════════════════════════\n";

    const int W = 3840, H = 2400;
    const int NUM_ACTIONS = 200;
    const double ACTION_INTERVAL = 0.5;

    // Use larger buffer for stress test
    RingBuffer buffer(20, W, H);  // 2 seconds at 10 FPS
    InputMonitor input;  // Won't actually start monitoring
    ActionEngine engine(buffer, input);

    engine.start();

    // Simulate: for each action, we need frames before and after
    // Timeline: action[i] at t = i * 0.5
    // Frames at 10 FPS: t = 0, 0.1, 0.2, ...
    // Pre-frame for action[i]: needs frame at t <= i*0.5 - 0.2
    // Post-frame for action[i]: needs frame at t >= i*0.5 + 0.2

    double sim_time = 0.0;
    int frame_count = 0;
    int action_count = 0;
    int next_action_frame = 0;  // Which frame number triggers next action

    auto start = Clock::now();

    // Simulate 10 FPS frames over 100+ seconds
    double total_time = (NUM_ACTIONS + 1) * ACTION_INTERVAL + 1.0;
    int total_frames = static_cast<int>(total_time * 10) + 10;

    for (int f = 0; f < total_frames; f++) {
        sim_time = f * 0.1;

        // Write frame to buffer
        auto& slot = buffer.begin_write();
        slot.width = W;
        slot.height = H;
        slot.timestamp_sec = sim_time;
        // Don't actually fill pixel data for speed
        buffer.commit_write();
        frame_count++;

        // Check if it's time to inject an action
        double next_action_time = action_count * ACTION_INTERVAL;
        if (action_count < NUM_ACTIONS && sim_time >= next_action_time + 0.05) {
            // Inject click at this time
            RawInputEvent down;
            down.type = RawEventType::MOUSE_BTN_DOWN;
            down.timestamp_sec = next_action_time;
            down.x = 100 + action_count;
            down.y = 200 + action_count;
            down.button_name = "left";
            engine.inject_event(down);

            RawInputEvent up;
            up.type = RawEventType::MOUSE_BTN_UP;
            up.timestamp_sec = next_action_time + 0.05;
            up.x = 100 + action_count;
            up.y = 200 + action_count;
            up.button_name = "left";
            engine.inject_event(up);

            action_count++;
        }

        // Let engine process (small sleep to yield)
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }

    // Wait for all pending to complete
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    auto elapsed = Clock::now() - start;

    engine.stop();

    size_t completed = 0;
    size_t degraded = 0;
    CompletedAction action;
    while (engine.pop_completed(action)) {
        completed++;
        if (action.pre_degraded) degraded++;
    }

    // Add already-popped count
    // (engine.completed_count() is 0 now since we popped them all)

    std::cout << "  Frames written: " << frame_count << "\n"
              << "  Actions injected: " << action_count << "\n"
              << "  Actions completed: " << completed << "\n"
              << "  Pre-frame degraded: " << degraded << "\n"
              << "  Actions pending (timeout): " << engine.pending_count() << "\n"
              << "  Wall time: " << std::fixed << std::setprecision(1)
              << to_ms(elapsed) / 1000.0 << " s\n"
              << "  " << (completed >= static_cast<size_t>(NUM_ACTIONS * 0.95)
                          ? "✅ PASS" : "❌ FAIL")
              << " (target: >= 95% of " << NUM_ACTIONS << " actions completed)\n";
}

// ═══════════════════════════════════════════════════════════════
// Test 4: Memory Usage
// ═══════════════════════════════════════════════════════════════

static void test_memory() {
    std::cout << "\n══════════════════════════════════════════════\n"
              << "  Test 4: Memory Usage\n"
              << "══════════════════════════════════════════════\n";

    double rss_base = get_rss_mb();

    {
        RingBuffer buffer(10, 3840, 2400);
        double rss_buffer = get_rss_mb();
        std::cout << "  Ring buffer (10 × 3840×2400 RGB): "
                  << std::fixed << std::setprecision(1)
                  << (rss_buffer - rss_base) << " MB\n";

        // Expected: 10 × 3840 × 2400 × 3 = 276.5 MB
        double expected = 10.0 * 3840 * 2400 * 3 / (1024 * 1024);
        std::cout << "  Expected: ~" << expected << " MB\n"
                  << "  " << (rss_buffer - rss_base < expected * 1.5
                              ? "✅ PASS" : "⚠️ OVER BUDGET")
                  << " (budget: < " << expected * 1.5 << " MB)\n";
    }

    double rss_freed = get_rss_mb();
    std::cout << "  After dealloc: " << (rss_freed - rss_base) << " MB residual\n";
}

// ═══════════════════════════════════════════════════════════════
// Main
// ═══════════════════════════════════════════════════════════════

int main() {
    std::cout << "╔══════════════════════════════════════════════╗\n"
              << "║  CUA Capture V2 — Performance Benchmark      ║\n"
              << "╚══════════════════════════════════════════════╝\n";

    double rss_start = get_rss_mb();
    std::cout << "  Baseline RSS: " << std::fixed << std::setprecision(1)
              << rss_start << " MB\n";

    test_ring_buffer_write();
    test_frame_lookup();
    test_stress_400_clicks();
    test_memory();

    std::cout << "\n══════════════════════════════════════════════\n"
              << "  All benchmarks complete.\n"
              << "══════════════════════════════════════════════\n\n";

    return 0;
}
