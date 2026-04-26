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

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
// Win32 headers expose generic macros that collide with our scoped enum names.
#ifdef DOUBLE_CLICK
#undef DOUBLE_CLICK
#endif
#ifdef UNKNOWN
#undef UNKNOWN
#endif
#else
// Include linux headers BEFORE any namespace to avoid
// struct name conflicts (input_event, libevdev get pulled into cua::)
#include <libevdev/libevdev.h>
#include <linux/input.h>
#endif

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
     * Limit recorded input coordinates to a selected capture region.
     * Collector hotkeys remain global; recorded actions use region-local coords.
     */
    void set_capture_region(int left, int top, int width, int height,
                            int output_width = 0, int output_height = 0,
                            int logical_left = 0, int logical_top = 0);

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
#ifdef _WIN32
    HHOOK keyboard_hook_{nullptr};
    HHOOK mouse_hook_{nullptr};
    std::atomic<DWORD> hook_thread_id_{0};
    bool ctrl_pressed_{false};
    bool region_set_{false};
    int region_left_{0};
    int region_top_{0};
    int region_width_{0};
    int region_height_{0};
    int output_width_{0};
    int output_height_{0};
    int logical_left_{0};
    int logical_top_{0};
    int logical_width_{0};
    int logical_height_{0};
    std::vector<std::string> recorded_keys_;
    std::vector<std::string> recorded_buttons_;

    static InputMonitor* active_instance_;
    static LRESULT CALLBACK keyboard_proc(int code, WPARAM wparam, LPARAM lparam);
    static LRESULT CALLBACK mouse_proc(int code, WPARAM wparam, LPARAM lparam);
    void handle_keyboard_event(WPARAM wparam, const KBDLLHOOKSTRUCT& event);
    void handle_mouse_event(WPARAM wparam, const MSLLHOOKSTRUCT& event);
    bool translate_point(int screen_x, int screen_y, int& out_x, int& out_y,
                         bool clamp_to_region = false) const;
    bool should_record_key(const std::string& name, bool is_down, int x, int y);
    bool should_record_button(const std::string& button_name, bool is_down,
                              int x, int y);
#else
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
#endif

    // Hotkey callback
    HotkeyCallback hotkey_cb_;

#ifndef _WIN32
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

    // Cursor position cache (updated periodically, not per-event)
    mutable std::mutex cursor_mu_;
    int cursor_x_{0};
    int cursor_y_{0};

    // Background cursor update thread
    std::thread cursor_cache_thread_;
    void update_cursor_cache_loop();
    std::pair<int, int> get_cursor_position_uncached();

    void detect_cursor_method();
    std::pair<int, int> cursor_cua_pixel();
    std::pair<int, int> cursor_gnome_eval();
#endif

    // Monitor thread
#ifndef _WIN32
    void scan_devices();
#endif
    void monitor_loop();
#ifndef _WIN32
    void process_event(DeviceInfo& dev, const ::input_event& ev);
#endif
    void push_event(RawInputEvent&& ev);

    // Helpers
    double monotonic_now() const;
    static std::string button_to_name(int code);
    static std::string key_to_name(int code);
};

}  // namespace cua
