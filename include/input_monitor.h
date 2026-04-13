#pragma once
/**
 * @file input_monitor.h
 * @brief Mouse / keyboard input monitoring via libevdev.
 *
 * Scans /dev/input/event* for keyboard and mouse devices,
 * monitors them via epoll, and pushes raw events into a queue.
 *
 * Also provides cursor position via the CUA GNOME extension D-Bus API
 * (GetPositionPixel), with fallback to Shell.Eval.
 */

// Include linux headers BEFORE any namespace to avoid
// struct name conflicts (input_event, libevdev get pulled into cua::)
#include <libevdev/libevdev.h>
#include <linux/input.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace cua {

/// Types of raw input events
/// Note: names avoid collision with linux/input-event-codes.h macros
/// (KEY_DOWN = 108 in linux headers)
enum class RawEventType {
    MOUSE_BTN_DOWN,
    MOUSE_BTN_UP,
    KEYBOARD_DOWN,
    KEYBOARD_UP,
    SCROLL_EVENT,
};

/// A single raw input event with timestamp
struct RawInputEvent {
    RawEventType type;
    double       timestamp_sec;   ///< CLOCK_MONOTONIC seconds
    int          x{0}, y{0};     ///< Cursor coords (filled for mouse events)
    int          button{0};       ///< BTN_LEFT/RIGHT/MIDDLE code
    int          key_code{0};     ///< evdev scancode (for key events)
    int          scroll_dx{0};
    int          scroll_dy{0};
    std::string  key_name;        ///< Human-readable key name (e.g., "ctrl_l")
    std::string  button_name;     ///< Human-readable button name (e.g., "left")
};

/// Hotkey types detected in the input thread
enum class HotkeyType {
    START_TASK,    // Ctrl+F8
    SCREENSHOT,    // Ctrl+F9 (kept for compat; V2 auto-captures)
    END_TASK,      // Ctrl+F12
    DROP_ACTION,   // Esc
};

class InputMonitor {
public:
    /// Callback for hotkey detection (delivered from input thread)
    using HotkeyCallback = std::function<void(HotkeyType)>;

    InputMonitor();
    ~InputMonitor();

    // Non-copyable
    InputMonitor(const InputMonitor&) = delete;
    InputMonitor& operator=(const InputMonitor&) = delete;

    /**
     * Set a callback for hotkey events.
     * Called from the input monitoring thread — keep it lightweight.
     */
    void set_hotkey_callback(HotkeyCallback cb) { hotkey_cb_ = std::move(cb); }

    /**
     * Start monitoring. Scans for input devices and begins the monitor thread.
     */
    void start();

    /**
     * Stop monitoring. Joins the monitor thread.
     */
    void stop();

    /// @return true if monitoring is active
    bool is_running() const { return running_.load(); }

    // ── Event queue interface (called by ActionEngine) ──────

    /**
     * Pop the next raw event from the queue.
     * @param out  Output event
     * @return true if an event was available
     */
    bool pop_event(RawInputEvent& out);

    /**
     * @return Number of events currently in the queue
     */
    size_t pending_count() const;

    /**
     * Get the current cursor position (pixel coords).
     * Uses CUA extension (GetPositionPixel) or fallback.
     * Thread-safe.
     *
     * @return (x, y) in monitor-relative pixel coordinates
     */
    std::pair<int, int> get_cursor_position();

private:
    std::atomic<bool> running_{false};
    std::thread monitor_thread_;

    // Event queue
    mutable std::mutex queue_mu_;
    std::deque<RawInputEvent> queue_;

    // Device tracking — use fully-qualified ::libevdev and ::input_event
    // to avoid C++ namespace scoping issues
    struct DeviceInfo {
        int fd;
        ::libevdev* dev;
        bool is_keyboard;
        bool is_mouse;
        std::string name;
    };
    std::vector<DeviceInfo> devices_;
    int epoll_fd_{-1};

    // Keyboard modifier state
    bool ctrl_pressed_{false};

    // Hotkey callback
    HotkeyCallback hotkey_cb_;

    // Cursor position method
    enum class CursorMethod {
        CUA_PIXEL,    // CUA extension v2 (GetPositionPixel)
        CUA_V1,       // CUA extension v1 (GetPosition + transform)
        GNOME_EVAL,   // org.gnome.Shell.Eval
        NONE,         // No cursor tracking available
    };
    CursorMethod cursor_method_{CursorMethod::NONE};
    int monitor_offset_x_{0}, monitor_offset_y_{0};
    double monitor_scale_{1.0};

    void detect_cursor_method();
    std::pair<int, int> cursor_cua_pixel();
    std::pair<int, int> cursor_gnome_eval();

    // Monitor thread
    void scan_devices();
    void monitor_loop();
    void process_event(DeviceInfo& dev, const ::input_event& ev);
    void push_event(RawInputEvent&& ev);

    // Helpers
    double monotonic_now() const;
    static std::string button_to_name(int code);
    static std::string key_to_name(int code);
};

}  // namespace cua
