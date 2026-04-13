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
#include <string>
#include <thread>
#include <unordered_map>
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
    CompletedAction finalize_action(PendingAction& pending,
                                     const FrameSlot& post_frame);

    MouseButtonState& get_button_state(const std::string& name);

    double monotonic_now() const;
};

}  // namespace cua
