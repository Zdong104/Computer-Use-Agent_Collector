/**
 * @file action_engine.cpp
 * @brief Event-to-action correlation engine.
 *
 * Consumes raw input events, groups them (click, double-click, drag, scroll),
 * looks up pre-frames from the ring buffer, waits for post-frames, and
 * produces completed action records.
 */

#include "action_engine.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <iostream>
#include <thread>

namespace cua {

ActionEngine::ActionEngine(RingBuffer& buffer, InputMonitor& input)
    : buffer_(buffer), input_(input) {}

ActionEngine::~ActionEngine() {
    stop();
}

double ActionEngine::monotonic_now() const {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

// ─── Mouse Button State ───────────────────────────────────────

ActionEngine::MouseButtonState& ActionEngine::get_button_state(const std::string& name) {
    if (name == "left") return mouse_left_;
    if (name == "right") return mouse_right_;
    return mouse_middle_;
}

// ─── Event Handlers ───────────────────────────────────────────

void ActionEngine::handle_mouse_down(const RawInputEvent& ev) {
    auto& state = get_button_state(ev.button_name);
    state.pressed = true;
    state.down_ts = ev.timestamp_sec;
    state.down_x = ev.x;
    state.down_y = ev.y;
}

void ActionEngine::handle_mouse_up(const RawInputEvent& ev) {
    auto& state = get_button_state(ev.button_name);
    if (!state.pressed) return;  // Spurious up without down

    state.pressed = false;

    double hold_time = ev.timestamp_sec - state.down_ts;
    double dx = std::abs(ev.x - state.down_x);
    double dy = std::abs(ev.y - state.down_y);
    double distance = std::sqrt(dx * dx + dy * dy);

    ActionType type;
    int action_x, action_y;

    if (distance > DRAG_MIN_DISTANCE || hold_time > DRAG_MIN_HOLD_TIME) {
        // Drag
        type = ActionType::DRAG;
        action_x = state.down_x;
        action_y = state.down_y;
    } else {
        // Click — check for double-click
        double since_last = ev.timestamp_sec - last_click_ts_;
        double click_dist = std::sqrt(
            std::pow(ev.x - last_click_x_, 2) +
            std::pow(ev.y - last_click_y_, 2));

        if (since_last < DOUBLE_CLICK_MAX_INTERVAL &&
            click_dist < DOUBLE_CLICK_MAX_DISTANCE &&
            ev.button_name == last_click_button_) {
            type = ActionType::DOUBLE_CLICK;
        } else {
            type = ActionType::CLICK;
        }
        action_x = ev.x;
        action_y = ev.y;

        last_click_ts_ = ev.timestamp_sec;
        last_click_x_ = ev.x;
        last_click_y_ = ev.y;
        last_click_button_ = ev.button_name;
    }

    // Use the mouse-down timestamp as the event time for pre-frame lookup
    double event_ts = state.down_ts;

    auto pending = create_pending(type, event_ts, action_x, action_y,
                                   ev.button_name);

    // For drag, store press/release coords
    if (type == ActionType::DRAG) {
        pending.press_x = state.down_x;
        pending.press_y = state.down_y;
        pending.release_x = ev.x;
        pending.release_y = ev.y;
    }
    pending.press_ts = state.down_ts;
    pending.release_ts = ev.timestamp_sec;
    pending.required_post_ts = ev.timestamp_sec + POST_FRAME_OFFSET;
    pending.last_event_ts = ev.timestamp_sec;

    // Store both down and up events
    RawInputEvent down_ev;
    down_ev.type = RawEventType::MOUSE_BTN_DOWN;
    down_ev.timestamp_sec = state.down_ts;
    down_ev.x = state.down_x;
    down_ev.y = state.down_y;
    down_ev.button_name = ev.button_name;
    pending.raw_events.push_back(down_ev);
    pending.raw_events.push_back(ev);

    std::lock_guard lock(pending_mu_);
    pending_.push_back(std::move(pending));
}

void ActionEngine::handle_scroll(const RawInputEvent& ev) {
    std::lock_guard lock(pending_mu_);

    // Check if we can merge with an existing scroll pending action
    for (auto& p : pending_) {
        if (p.type == ActionType::SCROLL &&
            (ev.timestamp_sec - p.last_event_ts) < SCROLL_MERGE_WINDOW) {
            // Merge into existing scroll action
            p.scroll_dx += ev.scroll_dx;
            p.scroll_dy += ev.scroll_dy;
            p.last_event_ts = ev.timestamp_sec;
            // Update required post time to after the latest scroll
            p.required_post_ts = ev.timestamp_sec + POST_FRAME_OFFSET;
            p.raw_events.push_back(ev);
            return;
        }
    }

    // New scroll action
    auto pending = create_pending(ActionType::SCROLL, ev.timestamp_sec,
                                   ev.x, ev.y, "",
                                   ev.scroll_dx, ev.scroll_dy);
    pending.raw_events.push_back(ev);
    pending_.push_back(std::move(pending));
}

void ActionEngine::handle_key(const RawInputEvent& ev) {
    std::lock_guard lock(pending_mu_);

    auto& state = key_states_[ev.key_name];

    if (ev.type == RawEventType::KEYBOARD_DOWN) {
        if (!state.pressed) {
            state.pressed = true;
            state.down_ts = ev.timestamp_sec;
            state.down_x = ev.x;
            state.down_y = ev.y;
        }
        return;
    }

    if (ev.type != RawEventType::KEYBOARD_UP || !state.pressed) return;

    state.pressed = false;

    auto pending = create_pending(ActionType::HOTKEY, state.down_ts,
                                  state.down_x, state.down_y, "", 0, 0);
    pending.press_ts = state.down_ts;
    pending.release_ts = ev.timestamp_sec;
    pending.required_post_ts = ev.timestamp_sec + POST_FRAME_OFFSET;
    pending.last_event_ts = ev.timestamp_sec;

    RawInputEvent down_ev;
    down_ev.type = RawEventType::KEYBOARD_DOWN;
    down_ev.timestamp_sec = state.down_ts;
    down_ev.x = state.down_x;
    down_ev.y = state.down_y;
    down_ev.key_code = ev.key_code;
    down_ev.key_name = ev.key_name;

    pending.raw_events.push_back(std::move(down_ev));
    pending.raw_events.push_back(ev);
    pending.keys_pressed.push_back(ev.key_name);
    pending.key_actions.push_back({ev.key_name, state.down_ts, ev.timestamp_sec});

    pending_.push_back(std::move(pending));
}

// ─── Pending Action Creation ──────────────────────────────────

ActionEngine::PendingAction ActionEngine::create_pending(
    ActionType type, double event_ts,
    int x, int y, const std::string& button_name,
    int scroll_dx, int scroll_dy) {

    PendingAction p;
    p.action_id = next_action_id_++;
    p.type = type;
    p.event_ts = event_ts;
    p.required_post_ts = event_ts + POST_FRAME_OFFSET;
    p.x = x;
    p.y = y;
    p.button_name = button_name;
    p.scroll_dx = scroll_dx;
    p.scroll_dy = scroll_dy;
    p.last_event_ts = event_ts;
    p.creation_ts = monotonic_now();

    // Look up pre-frame: latest frame with ts <= event_ts - PRE_FRAME_OFFSET
    double pre_target = event_ts - PRE_FRAME_OFFSET;
    FrameSlot pre_frame;
    if (buffer_.find_pre_frame(pre_target, pre_frame)) {
        p.pre_frame_id = pre_frame.frame_id;
        p.pre_frame_ts = pre_frame.timestamp_sec;
        p.pre_frame_rgb = std::move(pre_frame.rgb_data);
        p.pre_w = pre_frame.width;
        p.pre_h = pre_frame.height;
        p.pre_degraded = false;
    } else {
        // Fallback: try to get any frame before the event
        if (buffer_.find_pre_frame(event_ts, pre_frame)) {
            p.pre_frame_id = pre_frame.frame_id;
            p.pre_frame_ts = pre_frame.timestamp_sec;
            p.pre_frame_rgb = std::move(pre_frame.rgb_data);
            p.pre_w = pre_frame.width;
            p.pre_h = pre_frame.height;
            p.pre_degraded = true;  // Not truly 0.2s before
        } else {
            p.pre_degraded = true;
            std::cerr << "[ActionEngine] WARNING: No pre-frame available for action "
                      << p.action_id << std::endl;
        }
    }

    return p;
}

// ─── Action Finalization ──────────────────────────────────────

CompletedAction ActionEngine::finalize_action(PendingAction& pending,
                                                const FrameSlot& post_frame) {
    CompletedAction c;
    c.action_id = pending.action_id;
    c.type = pending.type;
    c.event_ts = pending.event_ts;
    c.x = pending.x;
    c.y = pending.y;
    c.button_name = pending.button_name;
    c.scroll_dx = pending.scroll_dx;
    c.scroll_dy = pending.scroll_dy;

    // Pre-frame
    c.pre_frame_id = pending.pre_frame_id;
    c.pre_frame_ts = pending.pre_frame_ts;
    c.pre_frame_rgb = std::move(pending.pre_frame_rgb);
    c.pre_w = pending.pre_w;
    c.pre_h = pending.pre_h;
    c.pre_degraded = pending.pre_degraded;

    // Post-frame
    c.post_frame_id = post_frame.frame_id;
    c.post_frame_ts = post_frame.timestamp_sec;
    c.post_frame_rgb.resize(
        static_cast<size_t>(post_frame.width) * post_frame.height * 3);
    std::memcpy(c.post_frame_rgb.data(), post_frame.rgb_data.data(),
                c.post_frame_rgb.size());
    c.post_w = post_frame.width;
    c.post_h = post_frame.height;

    // Raw events
    c.raw_events = std::move(pending.raw_events);

    // Drag coords
    c.press_x = pending.press_x;
    c.press_y = pending.press_y;
    c.release_x = pending.release_x;
    c.release_y = pending.release_y;
    c.press_ts = pending.press_ts;
    c.release_ts = pending.release_ts;

    // Keys
    c.keys_pressed = std::move(pending.keys_pressed);
    c.key_actions = std::move(pending.key_actions);

    return c;
}

// ─── Pending Completion Check ─────────────────────────────────

void ActionEngine::check_pending_completions() {
    std::lock_guard lock(pending_mu_);

    double now = monotonic_now();
    auto it = pending_.begin();

    while (it != pending_.end()) {
        // Check for timeout
        if (now - it->creation_ts > POST_FRAME_TIMEOUT) {
            std::cerr << "[ActionEngine] Action " << it->action_id
                      << " timed out waiting for post-frame" << std::endl;
            it = pending_.erase(it);
            continue;
        }

        // Try to find post-frame
        FrameSlot post_frame;
        if (buffer_.find_post_frame(it->required_post_ts, post_frame)) {
            auto completed = finalize_action(*it, post_frame);

            {
                std::lock_guard olock(output_mu_);
                completed_.push_back(std::move(completed));
            }

            it = pending_.erase(it);
        } else {
            ++it;
        }
    }
}

// ─── Worker Loop ──────────────────────────────────────────────

void ActionEngine::worker_loop() {
    while (running_) {
        // 1. Drain events from input monitor
        RawInputEvent ev;
        while (input_.pop_event(ev)) {
            process_event(ev);
        }

        // Also drain injected events (for testing)
        {
            std::lock_guard lock(inject_mu_);
            while (!injected_.empty()) {
                process_event(injected_.front());
                injected_.pop_front();
            }
        }

        // 2. Check pending actions for post-frame availability
        check_pending_completions();

        // 3. Sleep to avoid busy-wait (10ms = 100Hz poll rate)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

void ActionEngine::process_event(const RawInputEvent& ev) {
    switch (ev.type) {
        case RawEventType::MOUSE_BTN_DOWN:
            handle_mouse_down(ev);
            break;
        case RawEventType::MOUSE_BTN_UP:
            handle_mouse_up(ev);
            break;
        case RawEventType::SCROLL_EVENT:
            handle_scroll(ev);
            break;
        case RawEventType::KEYBOARD_DOWN:
        case RawEventType::KEYBOARD_UP:
            handle_key(ev);
            break;
    }
}

// ─── Start / Stop ─────────────────────────────────────────────

void ActionEngine::start() {
    if (running_.exchange(true)) return;

    worker_thread_ = std::thread([this]() {
        worker_loop();
    });

    std::cerr << "[ActionEngine] Action worker started" << std::endl;
}

void ActionEngine::stop() {
    if (!running_.exchange(false)) return;

    if (worker_thread_.joinable()) {
        worker_thread_.join();
    }

    std::cerr << "[ActionEngine] Action worker stopped. "
              << "Pending: " << pending_.size()
              << ", Completed: " << completed_.size() << std::endl;
}

// ─── Output Queue ─────────────────────────────────────────────

bool ActionEngine::pop_completed(CompletedAction& out) {
    std::lock_guard lock(output_mu_);
    if (completed_.empty()) return false;
    out = std::move(completed_.front());
    completed_.pop_front();
    return true;
}

size_t ActionEngine::completed_count() const {
    std::lock_guard lock(output_mu_);
    return completed_.size();
}

size_t ActionEngine::pending_count() const {
    std::lock_guard lock(pending_mu_);
    return pending_.size();
}

// ─── Testing Interface ────────────────────────────────────────

void ActionEngine::inject_event(RawInputEvent ev) {
    std::lock_guard lock(inject_mu_);
    injected_.push_back(std::move(ev));
}

}  // namespace cua
