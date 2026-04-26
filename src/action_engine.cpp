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

// Returns true for modifier key names.
// Must match the naming produced by InputMonitor::key_to_name().
bool ActionEngine::is_modifier_key(const std::string& name) {
    return name == "ctrl_l"  || name == "ctrl_r"  ||
           name == "shift_l" || name == "shift_r" ||
           name == "alt_l"   || name == "alt_r"   ||
           name == "super_l" || name == "super_r" ||
           name == "fn";
}

// Returns true if the key is a plain letter (a-z) that should NOT be recorded alone.
// Single letters typed without modifiers should be ignored (typing noise).
static bool is_plain_letter(const std::string& name) {
    return name.size() == 1 && ((name[0] >= 'a' && name[0] <= 'z') ||
                                (name[0] >= 'A' && name[0] <= 'Z'));
}

// Returns true if the key is a plain number key (0-9) that should NOT be recorded alone.
// Single number keys typed without modifiers should be ignored (typing noise).
static bool is_plain_number(const std::string& name) {
    return name.size() == 1 && name[0] >= '0' && name[0] <= '9';
}

static bool is_plain_key_noise_combo(const std::vector<std::string>& combo) {
    if (combo.empty()) return true;
    for (const auto& k : combo) {
        if (!is_plain_letter(k) && !is_plain_number(k)) {
            return false;
        }
    }
    return true;
}

static bool contains_key(const std::vector<std::string>& keys,
                         const std::string& key_name) {
    return std::find(keys.begin(), keys.end(), key_name) != keys.end();
}

ActionEngine::ActionEngine(RingBuffer& buffer, InputMonitor& input)
    : buffer_(buffer), input_(input) {
    // Pre-allocate capacity for stress test (200 actions + margin).
    // This prevents reallocation during the 400-click stress test.
    pending_.reserve(512);
}

ActionEngine::~ActionEngine() {
    stop();
}

double ActionEngine::monotonic_now() const {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

// ─── Mouse Button State ───────────────────────────────────────

ActionEngine::MouseButtonState& ActionEngine::get_button_state(const std::string& name) {
    if (name == "left") return mouse_left_;
    if (name == "right") return mouse_right_;
    return mouse_middle_;
}

ActionEngine::PendingAction* ActionEngine::find_merge_candidate_locked(double start_ts,
                                                                       double end_ts) {
    PendingAction* best = nullptr;
    double best_end = -1.0;

    for (auto& pending : pending_) {
        double pending_start = pending.event_ts;
        double pending_end = std::max({pending.event_ts, pending.last_event_ts,
                                       pending.release_ts});
        bool overlaps = start_ts <= pending_end && end_ts >= pending_start;
        if (overlaps && pending_end > best_end) {
            best = &pending;
            best_end = pending_end;
        }
    }
    return best;
}

bool ActionEngine::has_active_key_locked(const std::string& key_name) const {
    return active_keys_.find(key_name) != active_keys_.end();
}

void ActionEngine::erase_active_key_locked(const std::string& key_name) {
    active_keys_.erase(key_name);
    key_order_.erase(std::remove(key_order_.begin(), key_order_.end(), key_name),
                     key_order_.end());
}

std::vector<std::string> ActionEngine::active_modifiers_locked() const {
    std::vector<std::string> modifiers;
    for (const auto& key_name : key_order_) {
        if (has_active_key_locked(key_name) && is_modifier_key(key_name)) {
            modifiers.push_back(key_name);
        }
    }
    return modifiers;
}

std::vector<std::string> ActionEngine::active_non_modifiers_locked() const {
    std::vector<std::string> keys;
    for (const auto& key_name : key_order_) {
        if (has_active_key_locked(key_name) && !is_modifier_key(key_name)) {
            keys.push_back(key_name);
        }
    }
    return keys;
}

std::vector<std::string> ActionEngine::merge_modifier_sets_locked(
    const std::vector<std::string>& first,
    const std::vector<std::string>& second) const {
    std::vector<std::string> merged;
    for (const auto& key_name : key_order_) {
        if (!is_modifier_key(key_name)) continue;
        if ((contains_key(first, key_name) || contains_key(second, key_name)) &&
            !contains_key(merged, key_name)) {
            merged.push_back(key_name);
        }
    }
    for (const auto& key_name : first) {
        if (is_modifier_key(key_name) && !contains_key(merged, key_name)) {
            merged.push_back(key_name);
        }
    }
    for (const auto& key_name : second) {
        if (is_modifier_key(key_name) && !contains_key(merged, key_name)) {
            merged.push_back(key_name);
        }
    }
    return merged;
}

void ActionEngine::mark_modifiers_consumed_locked(const std::vector<std::string>& keys) {
    for (const auto& key_name : keys) {
        if (is_modifier_key(key_name)) {
            consumed_modifiers_.insert(key_name);
        }
    }
}

void ActionEngine::attach_keys_to_pending_locked(PendingAction& pending,
                                                 const std::vector<std::string>& keys,
                                                 double action_release_ts,
                                                 const std::string& trigger_key,
                                                 double trigger_press_ts,
                                                 double trigger_release_ts) {
    for (const auto& key_name : keys) {
        if (!contains_key(pending.keys_pressed, key_name)) {
            pending.keys_pressed.push_back(key_name);
        }

        double press_ts = trigger_press_ts;
        double release_ts = trigger_release_ts;
        if (key_name != trigger_key) {
            auto state_it = key_states_.find(key_name);
            press_ts = state_it != key_states_.end() ? state_it->second.down_ts
                                                     : pending.event_ts;
            release_ts = action_release_ts;
        }

        bool has_record = false;
        for (auto& record : pending.key_actions) {
            if (record.key_name == key_name) {
                has_record = true;
                if (key_name != trigger_key) {
                    record.release_ts = std::max(record.release_ts, release_ts);
                }
                break;
            }
        }
        if (!has_record) {
            pending.key_actions.push_back({key_name, press_ts, release_ts});
        }
    }
    mark_modifiers_consumed_locked(keys);
}

void ActionEngine::merge_key_into_pending_locked(PendingAction& pending,
                                                 const RawInputEvent& down_ev,
                                                 const RawInputEvent& up_ev,
                                                 double press_ts,
                                                 double release_ts,
                                                 const std::vector<std::string>& combo) {
    pending.raw_events.push_back(down_ev);
    pending.raw_events.push_back(up_ev);
    pending.event_ts = std::min(pending.event_ts, press_ts);
    pending.last_event_ts = std::max(pending.last_event_ts, release_ts);
    pending.required_post_ts = std::max(pending.required_post_ts,
                                        release_ts + POST_FRAME_OFFSET);
    attach_keys_to_pending_locked(pending, combo, release_ts,
                                  up_ev.key_name, press_ts, release_ts);
    pending.release_ts = std::max(pending.release_ts, release_ts);
}

void ActionEngine::merge_mouse_into_pending_locked(PendingAction& pending,
                                                   ActionType type,
                                                   const std::string& button_name,
                                                   const RawInputEvent& down_ev,
                                                   const RawInputEvent& up_ev,
                                                   int action_x,
                                                   int action_y,
                                                   double press_ts,
                                                   double release_ts,
                                                   int press_x,
                                                   int press_y,
                                                   int release_x,
                                                   int release_y) {
    pending.type = type;
    pending.button_name = button_name;
    pending.x = action_x;
    pending.y = action_y;
    pending.press_ts = press_ts;
    pending.release_ts = std::max(pending.release_ts, release_ts);
    pending.press_x = press_x;
    pending.press_y = press_y;
    pending.release_x = release_x;
    pending.release_y = release_y;
    pending.event_ts = std::min(pending.event_ts, press_ts);
    pending.last_event_ts = std::max(pending.last_event_ts, release_ts);
    pending.required_post_ts = std::max(pending.required_post_ts,
                                        release_ts + POST_FRAME_OFFSET);
    pending.raw_events.push_back(down_ev);
    pending.raw_events.push_back(up_ev);
}

// ─── Event Handlers ───────────────────────────────────────────

void ActionEngine::handle_mouse_down(const RawInputEvent& ev) {
    auto& state = get_button_state(ev.button_name);
    state.pressed = true;
    state.down_ts = ev.timestamp_sec;
    state.down_x = ev.x;
    state.down_y = ev.y;
    std::lock_guard lock(pending_mu_);
    state.down_modifiers = active_modifiers_locked();
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

    RawInputEvent down_ev;
    down_ev.type = RawEventType::MOUSE_BTN_DOWN;
    down_ev.timestamp_sec = state.down_ts;
    down_ev.x = state.down_x;
    down_ev.y = state.down_y;
    down_ev.button_name = ev.button_name;

    std::lock_guard lock(pending_mu_);
    const auto release_modifiers = active_modifiers_locked();
    const auto action_modifiers = merge_modifier_sets_locked(state.down_modifiers,
                                                             release_modifiers);

    if (auto* existing = find_merge_candidate_locked(state.down_ts, ev.timestamp_sec);
        existing && !existing->keys_pressed.empty() && existing->button_name.empty()) {
        merge_mouse_into_pending_locked(*existing, type, ev.button_name, down_ev, ev,
                                        action_x, action_y, state.down_ts, ev.timestamp_sec,
                                        state.down_x, state.down_y, ev.x, ev.y);
        attach_keys_to_pending_locked(*existing, action_modifiers, ev.timestamp_sec);
        return;
    }

    // Handle double-click detection: suppress first click if second click arrives
    if (type == ActionType::DOUBLE_CLICK) {
        // This is the second click - discard the pending first click if it exists
        if (pending_click_.active) {
            pending_click_.active = false;
        }
        // Record the double-click immediately
        auto pending = create_pending(type, event_ts, action_x, action_y,
                                       ev.button_name);
        pending.press_ts = state.down_ts;
        pending.release_ts = ev.timestamp_sec;
        pending.required_post_ts = ev.timestamp_sec + POST_FRAME_OFFSET;
        pending.last_event_ts = ev.timestamp_sec;
        pending.raw_events.push_back(down_ev);
        pending.raw_events.push_back(ev);
        attach_keys_to_pending_locked(pending, action_modifiers, ev.timestamp_sec);
        pending_.push_back(std::move(pending));
    } else if (type == ActionType::CLICK) {
        // This is a potential first click - delay recording it
        auto pending = create_pending(type, event_ts, action_x, action_y,
                                       ev.button_name);
        pending.press_ts = state.down_ts;
        pending.release_ts = ev.timestamp_sec;
        pending.required_post_ts = ev.timestamp_sec + POST_FRAME_OFFSET;
        pending.last_event_ts = ev.timestamp_sec;
        pending.raw_events.push_back(down_ev);
        pending.raw_events.push_back(ev);
        attach_keys_to_pending_locked(pending, action_modifiers, ev.timestamp_sec);

        // If another non-double-click arrives before the previous click timeout
        // has been processed, preserve the previous click instead of replacing it.
        if (pending_click_.active) {
            pending_.push_back(std::move(pending_click_.action));
        }

        // Store as pending click instead of adding to pending_ immediately
        pending_click_.active = true;
        pending_click_.action = std::move(pending);
    } else {
        // Drag or other action - record immediately
        auto pending = create_pending(type, event_ts, action_x, action_y,
                                       ev.button_name);
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
        pending.raw_events.push_back(down_ev);
        pending.raw_events.push_back(ev);
        attach_keys_to_pending_locked(pending, action_modifiers, ev.timestamp_sec);
        pending_.push_back(std::move(pending));
    }
}

void ActionEngine::handle_scroll(const RawInputEvent& ev) {
    std::lock_guard lock(pending_mu_);
    const auto modifiers = active_modifiers_locked();

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
            attach_keys_to_pending_locked(p, modifiers, ev.timestamp_sec);
            return;
        }
    }

    // New scroll action
    auto pending = create_pending(ActionType::SCROLL, ev.timestamp_sec,
                                   ev.x, ev.y, "",
                                   ev.scroll_dx, ev.scroll_dy);
    pending.raw_events.push_back(ev);
    attach_keys_to_pending_locked(pending, modifiers, ev.timestamp_sec);
    pending_.push_back(std::move(pending));
}

void ActionEngine::handle_key(const RawInputEvent& ev) {
    std::lock_guard lock(pending_mu_);

    const bool is_down = (ev.type == RawEventType::KEYBOARD_DOWN);

    if (is_down) {
        // ── Key press: add to active set ───────────────────────────
        auto& state = key_states_[ev.key_name];
        if (!state.pressed) {
            if (is_modifier_key(ev.key_name)) {
                modifier_press_ts_[ev.key_name] = ev.timestamp_sec;
                consumed_modifiers_.erase(ev.key_name);
            } else {
                consumed_keys_.erase(ev.key_name);
            }
            active_keys_.insert(ev.key_name);
            key_order_.push_back(ev.key_name);
            state.pressed = true;
            state.down_ts = ev.timestamp_sec;
            state.down_x = ev.x;
            state.down_y = ev.y;
        }
        return;
    }

    // ── Key release: record combination ──────────────────────────

    // Retrieve stored state for this key release.
    auto& state = key_states_[ev.key_name];
    const bool had_press = state.pressed;
    const double press_ts = state.down_ts;
    const double release_ts = ev.timestamp_sec;

    if (!had_press) return;  // Spurious up without down

    auto record_key_action = [&](const std::vector<std::string>& combo,
                                 const std::string& trigger_key,
                                 double trigger_press_ts,
                                 double trigger_release_ts,
                                 int trigger_x,
                                 int trigger_y,
                                 bool allow_merge) {
        RawInputEvent down_ev;
        down_ev.type = RawEventType::KEYBOARD_DOWN;
        down_ev.timestamp_sec = trigger_press_ts;
        down_ev.x = trigger_x;
        down_ev.y = trigger_y;
        down_ev.key_name = trigger_key;

        RawInputEvent up_ev;
        up_ev.type = RawEventType::KEYBOARD_UP;
        up_ev.timestamp_sec = trigger_release_ts;
        up_ev.x = ev.x;
        up_ev.y = ev.y;
        up_ev.key_name = trigger_key;

        if (allow_merge) {
            if (auto* existing = find_merge_candidate_locked(trigger_press_ts,
                                                             trigger_release_ts)) {
                merge_key_into_pending_locked(*existing, down_ev, up_ev,
                                              trigger_press_ts, trigger_release_ts,
                                              combo);
                return;
            }
        }

        auto pending = create_pending(ActionType::HOTKEY, trigger_press_ts,
                                      trigger_x, trigger_y, "", 0, 0);
        pending.press_ts = trigger_press_ts;
        pending.release_ts = trigger_release_ts;
        pending.required_post_ts = trigger_release_ts + POST_FRAME_OFFSET;
        pending.last_event_ts = trigger_release_ts;
        pending.raw_events.push_back(std::move(down_ev));
        pending.raw_events.push_back(std::move(up_ev));
        attach_keys_to_pending_locked(pending, combo, trigger_release_ts,
                                      trigger_key, trigger_press_ts,
                                      trigger_release_ts);
        pending_.push_back(std::move(pending));
    };

    if (is_modifier_key(ev.key_name)) {
        std::vector<std::string> modifiers = active_modifiers_locked();
        std::vector<std::string> active_non_modifiers = active_non_modifiers_locked();

        auto it = modifier_press_ts_.find(ev.key_name);
        if (it != modifier_press_ts_.end()) {
            const double elapsed_ms = (release_ts - it->second) * 1000.0;
            modifier_press_ts_.erase(it);
            if (elapsed_ms < MODIFIER_DEBOUNCE_MS) {
                erase_active_key_locked(ev.key_name);
                state.pressed = false;
                consumed_modifiers_.erase(ev.key_name);
                return;
            }
        }

        for (const auto& trigger_key : active_non_modifiers) {
            if (consumed_keys_.find(trigger_key) != consumed_keys_.end()) {
                continue;
            }
            auto trigger_it = key_states_.find(trigger_key);
            if (trigger_it == key_states_.end() || !trigger_it->second.pressed) {
                continue;
            }
            std::vector<std::string> combo = modifiers;
            if (!contains_key(combo, trigger_key)) {
                combo.push_back(trigger_key);
            }
            record_key_action(combo, trigger_key, trigger_it->second.down_ts,
                              release_ts, trigger_it->second.down_x,
                              trigger_it->second.down_y, false);
            consumed_keys_.insert(trigger_key);
        }

        erase_active_key_locked(ev.key_name);
        state.pressed = false;
        consumed_modifiers_.erase(ev.key_name);
        return;
    }

    std::vector<std::string> modifiers = active_modifiers_locked();
    std::vector<std::string> combo = modifiers;
    combo.push_back(ev.key_name);

    erase_active_key_locked(ev.key_name);
    state.pressed = false;

    if (consumed_keys_.erase(ev.key_name) > 0) {
        return;
    }

    if (is_plain_key_noise_combo(combo)) {
        return;
    }

    record_key_action(combo, ev.key_name, press_ts, release_ts,
                      state.down_x, state.down_y, true);
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
                                                FrameSlot post_frame) {
    CompletedAction c;
    c.action_id = pending.action_id;
    c.type = pending.type;
    c.event_ts = pending.event_ts;
    c.x = pending.x;
    c.y = pending.y;
    c.button_name = pending.button_name;
    c.scroll_dx = pending.scroll_dx;
    c.scroll_dy = pending.scroll_dy;

    // Pre-frame — move from pending (already efficient)
    c.pre_frame_id = pending.pre_frame_id;
    c.pre_frame_ts = pending.pre_frame_ts;
    c.pre_frame_rgb = std::move(pending.pre_frame_rgb);
    c.pre_w = pending.pre_w;
    c.pre_h = pending.pre_h;
    c.pre_degraded = pending.pre_degraded;

    // Post-frame — move from the passed-by-value FrameSlot (no extra copy)
    c.post_frame_id = post_frame.frame_id;
    c.post_frame_ts = post_frame.timestamp_sec;
    c.post_frame_rgb = std::move(post_frame.rgb_data);
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

    // Check if pending click has timed out (no double-click arrived)
    if (pending_click_.active) {
        double time_since_click = now - pending_click_.action.release_ts;
        if (time_since_click >= DOUBLE_CLICK_MAX_INTERVAL) {
            // Timeout expired - finalize the single click
            pending_.push_back(std::move(pending_click_.action));
            pending_click_.active = false;
        }
    }

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
            // Pass FrameSlot by value so finalize_action can move rgb_data
            auto completed = finalize_action(*it, std::move(post_frame));

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
