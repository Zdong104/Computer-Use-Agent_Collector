#pragma once
/**
 * @file pipewire_capture.h
 * @brief Continuous screen capture via PipeWire screencast portal.
 *
 * Strategy:
 *   1. Spawn the GJS helper (same as V1) to get PipeWire FD + node ID
 *      from the XDG ScreenCast portal (persist_mode=2).
 *   2. Create a pw_stream connected to that node.
 *   3. On each frame callback, convert SPA format → RGB → ring buffer.
 *   4. Frame rate throttled to target FPS (skip excess frames).
 */

#ifdef _WIN32

#include "ring_buffer.h"

#include <atomic>
#include <functional>
#include <string>
#include <thread>

namespace cua {

class PipeWireCapture {
public:
    explicit PipeWireCapture(RingBuffer& buffer, int target_fps = 10);
    ~PipeWireCapture();

    PipeWireCapture(const PipeWireCapture&) = delete;
    PipeWireCapture& operator=(const PipeWireCapture&) = delete;

    bool init_portal(const std::string& gjs_script_path = "");
    void set_capture_region(int left, int top, int width, int height,
                            int output_width = 0, int output_height = 0,
                            int logical_left = 0, int logical_top = 0);
    void start();
    void stop();

    bool is_running() const { return running_.load(); }
    int node_id() const { return -1; }
    int pw_fd() const { return -1; }

    using StatusCallback = std::function<void(const std::string&)>;
    void set_status_callback(StatusCallback cb) { status_cb_ = std::move(cb); }

private:
    RingBuffer& buffer_;
    int target_fps_;
    double min_frame_interval_;
    std::thread capture_thread_;
    std::atomic<bool> running_{false};
    bool initialized_{false};
    bool region_set_{false};
    int source_left_{0};
    int source_top_{0};
    int source_width_{0};
    int source_height_{0};
    int capture_width_{0};
    int capture_height_{0};
    double last_frame_ts_{0.0};
    StatusCallback status_cb_;

    void log_status(const std::string& msg);
    void capture_loop();
    bool capture_frame();
};

}  // namespace cua

#else

// Include PipeWire/SPA headers BEFORE namespace to avoid scoping issues
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/debug/types.h>
#include <spa/param/video/type-info.h>
#include <spa/utils/result.h>
#include <spa/utils/hook.h>

#include "ring_buffer.h"

#include <atomic>
#include <functional>
#include <string>
#include <thread>

namespace cua {

class PipeWireCapture {
public:
    /**
     * @param buffer    Ring buffer to write frames into
     * @param target_fps  Target capture rate (default 10)
     */
    explicit PipeWireCapture(RingBuffer& buffer, int target_fps = 10);
    ~PipeWireCapture();

    // Non-copyable
    PipeWireCapture(const PipeWireCapture&) = delete;
    PipeWireCapture& operator=(const PipeWireCapture&) = delete;

    /**
     * Initialize the PipeWire portal session.
     * This spawns the GJS helper and may show the GNOME share dialog
     * on first run (subsequent runs are auto-approved via persist_mode=2).
     *
     * @param gjs_script_path  Path to the GJS screencast script.
     *                          If empty, uses the embedded script.
     * @return true if portal session established successfully
     */
    bool init_portal(const std::string& gjs_script_path = "");
    void set_capture_region(int left, int top, int width, int height,
                            int output_width = 0, int output_height = 0,
                            int logical_left = 0, int logical_top = 0);

    /**
     * Start the capture thread. Frames begin flowing into the ring buffer.
     * Must call init_portal() first.
     */
    void start();

    /**
     * Stop the capture thread and tear down PipeWire resources.
     */
    void stop();

    /// @return true if capture is actively running
    bool is_running() const { return running_.load(); }

    /// @return PipeWire node ID (after init_portal)
    int node_id() const { return pw_node_id_; }

    /// @return PipeWire FD (after init_portal)
    int pw_fd() const { return pw_fd_; }

    /// Callback type for status notifications
    using StatusCallback = std::function<void(const std::string&)>;
    void set_status_callback(StatusCallback cb) { status_cb_ = std::move(cb); }

    // PipeWire stream callbacks (public for C callback dispatch)
    static void on_state_changed(void* userdata, enum pw_stream_state old_state,
                                 enum pw_stream_state state, const char* error);
    static void on_process(void* userdata);
    static void on_param_changed(void* userdata, uint32_t id,
                                  const struct ::spa_pod* param);

private:
    RingBuffer& buffer_;
    int target_fps_;
    double min_frame_interval_;  // 1.0 / target_fps_

    // Portal session state
    int pw_fd_{-1};
    int pw_node_id_{-1};
    pid_t gjs_pid_{-1};
    int gjs_stdin_fd_{-1};
    int gjs_stdout_fd_{-1};

    // PipeWire state
    ::pw_main_loop* loop_{nullptr};
    ::pw_context*   context_{nullptr};
    ::pw_core*      core_{nullptr};
    ::pw_stream*    stream_{nullptr};
    ::spa_hook      stream_listener_{};

    // Stream format info (set by on_param_changed)
    int stream_width_{0};
    int stream_height_{0};
    uint32_t stream_format_{0};

    // Thread management
    std::thread capture_thread_;
    std::atomic<bool> running_{false};
    double last_frame_ts_{0.0};

    StatusCallback status_cb_;

    void log_status(const std::string& msg);

    // GJS helper management
    bool spawn_gjs_helper(const std::string& script_path);
    void kill_gjs_helper();

    // Frame handling
    void handle_frame();

    // Capture thread entry point
    void capture_loop();

    // Embedded GJS script (same as V1's screenshot_wayland.py)
    static const char* embedded_gjs_script();
};

}  // namespace cua

#endif
