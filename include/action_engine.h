#pragma once
/**
 * @file action_engine.h
 * @brief Correlates raw input events with ring buffer frames to produce
 *        complete (pre-frame, action, post-frame) records.
 *
 * State machine per action:
 *   Idle → RawEventArrived → [Merging/ScrollAccumulating] → WaitingPostFrame → Finalized
 *
 * Grouping rules:
 *   - Double-click: two left clicks within 250ms and 8px
 *   - Scroll burst: consecutive scrolls within 200ms merged
 *   - Drag: mouse down→up with distance > 3px or hold > 0.3s
 */

#include "input_monitor.h"
#include "ring_buffer.h"

#include <atomic>
#include <cstdint>
#include <deque>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace cua {

/// High-level action types
enum class ActionType {
    CLICK,
    DOUBLE_CLICK,
    DRAG,
    SCROLL,
    HOTKEY,
    UNKNOWN,
};

/// String conversion
inline const char* action_type_str(ActionType t) {
    switch (t) {
        case ActionType::CLICK:        return "click";
        case ActionType::DOUBLE_CLICK: return "double_click";
        case ActionType::DRAG:         return "drag";
        case ActionType::SCROLL:       return "scroll";
        case ActionType::HOTKEY:       return "hotkey";
        default:                       return "unknown";
    }
}

/// A fully completed action with pre-frame and post-frame data.
struct KeyActionRecord {
    std::string key_name;
    double      press_ts{0};
    double      release_ts{0};
};

struct CompletedAction {
    uint64_t    action_id{0};
    ActionType  type{ActionType::UNKNOWN};
    double      event_ts{0};       ///< Monotonic timestamp of the triggering event
    int         x{0}, y{0};        ///< Action coordinates (pixels)
    std::string button_name;       ///< "left", "right", "middle"
    int         scroll_dx{0}, scroll_dy{0};

    // Pre-frame
    uint64_t    pre_frame_id{0};
    double      pre_frame_ts{0};
    std::vector<uint8_t> pre_frame_rgb;
    int         pre_w{0}, pre_h{0};
    bool        pre_degraded{false};  ///< True if couldn't find frame ≤ t-0.2s

    // Post-frame
    uint64_t    post_frame_id{0};
    double      post_frame_ts{0};
    std::vector<uint8_t> post_frame_rgb;
    int         post_w{0}, post_h{0};

    // Raw sub-events
    std::vector<RawInputEvent> raw_events;

    // Drag specifics
    int press_x{0}, press_y{0};
    int release_x{0}, release_y{0};
    double press_ts{0};
    double release_ts{0};

    // Key events
    std::vector<std::string> keys_pressed;
    std::vector<KeyActionRecord> key_actions;
};

class ActionEngine {
public:
    /**
     * @param buffer  Ring buffer to read frames from
     * @param input   Input monitor to read events from
     */
    ActionEngine(RingBuffer& buffer, InputMonitor& input);
    ~ActionEngine();

    // Non-copyable
    ActionEngine(const ActionEngine&) = delete;
    ActionEngine& operator=(const ActionEngine&) = delete;

    /**
     * Start the action worker thread.
     */
    void start();

    /**
     * Stop the worker thread.
     */
    void stop();

    /// @return true if running
    bool is_running() const { return running_.load(); }

    // ── Output interface (called by Python) ─────────────────

    /**
     * Pop the next completed action from the output queue.
     * @param out  Output action
     * @return true if an action was available
     */
    bool pop_completed(CompletedAction& out);

    /**
     * @return Number of completed actions waiting
     */
    size_t completed_count() const;

    /**
     * @return Number of actions currently pending (waiting for post-frame)
     */
    size_t pending_count() const;

    // ── Timing parameters ───────────────────────────────────

    /// Pre-frame offset: look for frames at t - offset
    static constexpr double PRE_FRAME_OFFSET  = 0.200;  // seconds
    /// Post-frame offset: wait for frame at t + offset
    static constexpr double POST_FRAME_OFFSET = 0.200;  // seconds
    /// Max time between two clicks to count as double-click
    static constexpr double DOUBLE_CLICK_MAX_INTERVAL = 0.250;
    /// Max pixel distance between two clicks for double-click
    static constexpr double DOUBLE_CLICK_MAX_DISTANCE = 8.0;
    /// Max time between scroll events to merge
    static constexpr double SCROLL_MERGE_WINDOW = 0.200;
    /// Min distance or hold time to classify as drag
    static constexpr double DRAG_MIN_DISTANCE = 3.0;
    static constexpr double DRAG_MIN_HOLD_TIME = 0.300;
    /// Max time to wait for post-frame before giving up (seconds)
    static constexpr double POST_FRAME_TIMEOUT = 5.0;

    /// Min hold time for a modifier release to be considered intentional (ms).
    /// Releases shorter than this are ignored (accidental modifier taps).
    static constexpr double MODIFIER_DEBOUNCE_MS = 200.0;

    /// Inject a synthetic event (for testing)
    void inject_event(RawInputEvent ev);

private:
    RingBuffer& buffer_;
    InputMonitor& input_;

    std::thread worker_thread_;
    std::atomic<bool> running_{false};

    // ── Pending actions waiting for post-frame ──────────────

    struct PendingAction {
        uint64_t    action_id{0};
        ActionType  type{ActionType::UNKNOWN};
        double      event_ts{0};
        double      required_post_ts{0};   // event_ts + POST_FRAME_OFFSET
        int         x{0}, y{0};
        std::string button_name;
        int         scroll_dx{0}, scroll_dy{0};
        uint64_t    pre_frame_id{0};
        double      pre_frame_ts{0};
        std::vector<uint8_t> pre_frame_rgb;
        int         pre_w{0}, pre_h{0};
        bool        pre_degraded{false};
        std::vector<RawInputEvent> raw_events;
        double      last_event_ts{0};      // for scroll grouping
        double      creation_ts{0};        // for timeout

        // Drag tracking
        int press_x{0}, press_y{0};
        int release_x{0}, release_y{0};
        double press_ts{0};
        double release_ts{0};

        // Key tracking
        std::vector<std::string> keys_pressed;
        std::vector<KeyActionRecord> key_actions;
    };

    mutable std::mutex pending_mu_;
    std::vector<PendingAction> pending_;
    uint64_t next_action_id_{1};

    // ── Output queue ────────────────────────────────────────

    mutable std::mutex output_mu_;
    std::deque<CompletedAction> completed_;

    // ── Injected event queue (for testing) ──────────────────
    std::mutex inject_mu_;
    std::deque<RawInputEvent> injected_;

    // ── Mouse state tracking ────────────────────────────────

    struct MouseButtonState {
        bool pressed{false};
        double down_ts{0};
        int down_x{0}, down_y{0};
        std::vector<std::string> down_modifiers;
    };
    struct KeyState {
        bool pressed{false};
        double down_ts{0};
        int down_x{0}, down_y{0};
    };
    MouseButtonState mouse_left_;
    MouseButtonState mouse_right_;
    MouseButtonState mouse_middle_;
    std::unordered_map<std::string, KeyState> key_states_;

    // Last click info for double-click detection
    double last_click_ts_{0};
    int    last_click_x_{0}, last_click_y_{0};
    std::string last_click_button_;

    // Pending click that might become part of a double-click
    struct PendingClick {
        bool active{false};
        PendingAction action;
    };
    PendingClick pending_click_;

    // Last scroll for burst merging
    double last_scroll_ts_{0};

    // ── Worker loop ─────────────────────────────────────────

    void worker_loop();
    void process_event(const RawInputEvent& ev);
    void check_pending_completions();

    // Event handlers
    void handle_mouse_down(const RawInputEvent& ev);
    void handle_mouse_up(const RawInputEvent& ev);
    void handle_scroll(const RawInputEvent& ev);
    void handle_key(const RawInputEvent& ev);

    // Helper: look up pre-frame and create pending action
    PendingAction create_pending(ActionType type, double event_ts,
                                  int x, int y, const std::string& button_name,
                                  int scroll_dx = 0, int scroll_dy = 0);

    // Helper: finalize a pending action with a post-frame
    // Takes FrameSlot by VALUE so we can move rgb_data instead of copying
    CompletedAction finalize_action(PendingAction& pending,
                                     FrameSlot post_frame);
    PendingAction* find_merge_candidate_locked(double start_ts, double end_ts);
    void merge_key_into_pending_locked(PendingAction& pending,
                                       const RawInputEvent& down_ev,
                                       const RawInputEvent& up_ev,
                                       double press_ts,
                                       double release_ts,
                                       const std::vector<std::string>& combo = {});
    void merge_mouse_into_pending_locked(PendingAction& pending,
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
                                         int release_y);
    std::vector<std::string> active_modifiers_locked() const;
    std::vector<std::string> active_non_modifiers_locked() const;
    std::vector<std::string> merge_modifier_sets_locked(
        const std::vector<std::string>& first,
        const std::vector<std::string>& second) const;
    void attach_keys_to_pending_locked(PendingAction& pending,
                                       const std::vector<std::string>& keys,
                                       double action_release_ts,
                                       const std::string& trigger_key = "",
                                       double trigger_press_ts = 0.0,
                                       double trigger_release_ts = 0.0);
    void mark_modifiers_consumed_locked(const std::vector<std::string>& keys);
    void erase_active_key_locked(const std::string& key_name);
    bool has_active_key_locked(const std::string& key_name) const;

    MouseButtonState& get_button_state(const std::string& name);

    // ── State-based key combination tracking ─────────────────────
    // Tracks ALL currently-pressed keys (including modifiers) so we can
    // record the full combination on key release.
    std::set<std::string> active_keys_;
    std::vector<std::string> key_order_;
    std::unordered_set<std::string> consumed_modifiers_;
    std::unordered_set<std::string> consumed_keys_;

    // Tracks when each modifier key was pressed, for debouncing.
    // Only used for modifier keys to ignore accidental taps < MODIFIER_DEBOUNCE_MS.
    std::unordered_map<std::string, double> modifier_press_ts_;

    // Returns true for modifier key names (ctrl, shift, alt, super, fn).
    static bool is_modifier_key(const std::string& name);

    double monotonic_now() const;
};

}  // namespace cua
