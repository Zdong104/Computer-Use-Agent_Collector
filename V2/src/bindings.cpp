/**
 * @file bindings.cpp
 * @brief pybind11 bindings for the CUA capture engine.
 *
 * Exposes:
 *   - cua_capture.CaptureEngine: main orchestrator
 *   - cua_capture.CompletedAction: action data with frame RGB
 *   - cua_capture.HotkeyType: hotkey enum
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>

#include "action_engine.h"
#include "input_monitor.h"
#include "pipewire_capture.h"
#include "ring_buffer.h"

#include <atomic>
#include <deque>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace py = pybind11;

namespace cua {

/**
 * @class CaptureEngine
 * @brief Top-level orchestrator exposed to Python.
 *
 * Owns the RingBuffer, PipeWireCapture, InputMonitor, and ActionEngine.
 * Provides a simple start/stop/poll interface.
 */
class CaptureEngine {
public:
    /**
     * @param buffer_capacity  Number of frame slots in ring buffer
     * @param max_width        Max frame width
     * @param max_height       Max frame height
     * @param target_fps       Target capture FPS
     */
    CaptureEngine(int buffer_capacity = 10,
                  int max_width = 3840,
                  int max_height = 2400,
                  int target_fps = 10)
        : buffer_(buffer_capacity, max_width, max_height)
        , capture_(buffer_, target_fps)
        , engine_(buffer_, input_) {

        // Forward hotkeys from InputMonitor to a queue for Python
        input_.set_hotkey_callback([this](HotkeyType type) {
            std::lock_guard lock(hotkey_mu_);
            hotkey_queue_.push_back(type);
        });
    }

    /**
     * Initialize the PipeWire portal session.
     * May show the GNOME share dialog on first run.
     * @param gjs_script_path  Optional path to GJS script
     * @return true if portal setup succeeded
     */
    bool init_portal(const std::string& gjs_script_path = "") {
        return capture_.init_portal(gjs_script_path);
    }

    /**
     * Start all threads (capture, input, action worker).
     */
    void start() {
        input_.start();
        capture_.start();
        engine_.start();
        std::cerr << "[CaptureEngine] All subsystems started" << std::endl;
    }

    /**
     * Stop all threads.
     */
    void stop() {
        engine_.stop();
        capture_.stop();
        input_.stop();
        std::cerr << "[CaptureEngine] All subsystems stopped" << std::endl;
    }

    /**
     * Pop the next completed action.
     * @return CompletedAction or None if queue is empty
     */
    py::object pop_action() {
        CompletedAction action;
        if (engine_.pop_completed(action)) {
            return py::cast(std::move(action));
        }
        return py::none();
    }

    /**
     * Pop the next hotkey event.
     * @return HotkeyType or None
     */
    py::object pop_hotkey() {
        std::lock_guard lock(hotkey_mu_);
        if (hotkey_queue_.empty()) return py::none();
        auto hk = hotkey_queue_.front();
        hotkey_queue_.pop_front();
        return py::cast(hk);
    }

    /// Number of completed actions waiting
    size_t completed_count() const { return engine_.completed_count(); }

    /// Number of pending actions (waiting for post-frame)
    size_t pending_count() const { return engine_.pending_count(); }

    /// Number of frames captured so far
    uint64_t total_frames() const { return buffer_.total_frames_written(); }

    /// Latest frame timestamp
    double latest_frame_ts() const { return buffer_.latest_timestamp(); }

    /// Is capture running?
    bool is_running() const { return capture_.is_running(); }

    /// Get cursor position (for Python compat)
    std::pair<int, int> get_cursor_position() {
        return input_.get_cursor_position();
    }

    /// Inject a synthetic event (for testing)
    void inject_mouse_click(double ts, int x, int y,
                             const std::string& button = "left") {
        RawInputEvent down;
        down.type = RawEventType::MOUSE_BTN_DOWN;
        down.timestamp_sec = ts;
        down.x = x;
        down.y = y;
        down.button_name = button;
        engine_.inject_event(down);

        RawInputEvent up;
        up.type = RawEventType::MOUSE_BTN_UP;
        up.timestamp_sec = ts + 0.05;  // 50ms click
        up.x = x;
        up.y = y;
        up.button_name = button;
        engine_.inject_event(up);
    }

    /// Inject a synthetic frame into the ring buffer (for testing)
    void inject_frame(double ts, int width, int height) {
        auto& slot = buffer_.begin_write();
        slot.timestamp_sec = ts;
        slot.width = width;
        slot.height = height;
        // Fill with dummy data (just set first few bytes)
        size_t sz = static_cast<size_t>(width) * height * 3;
        if (slot.rgb_data.size() >= sz) {
            std::memset(slot.rgb_data.data(), 128, sz);
        }
        buffer_.commit_write();
    }

private:
    RingBuffer buffer_;
    PipeWireCapture capture_;
    InputMonitor input_;
    ActionEngine engine_;

    std::mutex hotkey_mu_;
    std::deque<HotkeyType> hotkey_queue_;
};

}  // namespace cua


// ═══════════════════════════════════════════════════════════════
// pybind11 Module Definition
// ═══════════════════════════════════════════════════════════════

PYBIND11_MODULE(cua_capture, m) {
    m.doc() = "CUA Capture Engine — high-performance screen capture with "
              "ring buffer and event correlation";

    // ── Enums ───────────────────────────────────────────────
    py::enum_<cua::ActionType>(m, "ActionType")
        .value("CLICK", cua::ActionType::CLICK)
        .value("DOUBLE_CLICK", cua::ActionType::DOUBLE_CLICK)
        .value("DRAG", cua::ActionType::DRAG)
        .value("SCROLL", cua::ActionType::SCROLL)
        .value("HOTKEY", cua::ActionType::HOTKEY)
        .value("UNKNOWN", cua::ActionType::UNKNOWN);

    py::enum_<cua::HotkeyType>(m, "HotkeyType")
        .value("START_TASK", cua::HotkeyType::START_TASK)
        .value("SCREENSHOT", cua::HotkeyType::SCREENSHOT)
        .value("END_TASK", cua::HotkeyType::END_TASK)
        .value("DROP_ACTION", cua::HotkeyType::DROP_ACTION);

    py::enum_<cua::RawEventType>(m, "RawEventType")
        .value("MOUSE_BTN_DOWN", cua::RawEventType::MOUSE_BTN_DOWN)
        .value("MOUSE_BTN_UP", cua::RawEventType::MOUSE_BTN_UP)
        .value("KEYBOARD_DOWN", cua::RawEventType::KEYBOARD_DOWN)
        .value("KEYBOARD_UP", cua::RawEventType::KEYBOARD_UP)
        .value("SCROLL_EVENT", cua::RawEventType::SCROLL_EVENT);

    py::class_<cua::KeyActionRecord>(m, "KeyActionRecord")
        .def_readonly("key_name", &cua::KeyActionRecord::key_name)
        .def_readonly("press_ts", &cua::KeyActionRecord::press_ts)
        .def_readonly("release_ts", &cua::KeyActionRecord::release_ts);

    // ── CompletedAction ─────────────────────────────────────
    py::class_<cua::CompletedAction>(m, "CompletedAction")
        .def_readonly("action_id", &cua::CompletedAction::action_id)
        .def_readonly("event_ts", &cua::CompletedAction::event_ts)
        .def_readonly("x", &cua::CompletedAction::x)
        .def_readonly("y", &cua::CompletedAction::y)
        .def_readonly("button_name", &cua::CompletedAction::button_name)
        .def_readonly("scroll_dx", &cua::CompletedAction::scroll_dx)
        .def_readonly("scroll_dy", &cua::CompletedAction::scroll_dy)
        .def_readonly("pre_frame_id", &cua::CompletedAction::pre_frame_id)
        .def_readonly("pre_frame_ts", &cua::CompletedAction::pre_frame_ts)
        .def_readonly("pre_w", &cua::CompletedAction::pre_w)
        .def_readonly("pre_h", &cua::CompletedAction::pre_h)
        .def_readonly("pre_degraded", &cua::CompletedAction::pre_degraded)
        .def_readonly("post_frame_id", &cua::CompletedAction::post_frame_id)
        .def_readonly("post_frame_ts", &cua::CompletedAction::post_frame_ts)
        .def_readonly("post_w", &cua::CompletedAction::post_w)
        .def_readonly("post_h", &cua::CompletedAction::post_h)
        .def_readonly("press_x", &cua::CompletedAction::press_x)
        .def_readonly("press_y", &cua::CompletedAction::press_y)
        .def_readonly("release_x", &cua::CompletedAction::release_x)
        .def_readonly("release_y", &cua::CompletedAction::release_y)
        .def_readonly("press_ts", &cua::CompletedAction::press_ts)
        .def_readonly("release_ts", &cua::CompletedAction::release_ts)
        .def_readonly("keys_pressed", &cua::CompletedAction::keys_pressed)
        .def_readonly("key_actions", &cua::CompletedAction::key_actions)
        // Type as string
        .def_property_readonly("type", [](const cua::CompletedAction& a) {
            return std::string(cua::action_type_str(a.type));
        })
        // Frame data as Python bytes (zero-copy via buffer protocol)
        .def_property_readonly("pre_frame_rgb", [](const cua::CompletedAction& a) {
            return py::bytes(
                reinterpret_cast<const char*>(a.pre_frame_rgb.data()),
                a.pre_frame_rgb.size());
        })
        .def_property_readonly("post_frame_rgb", [](const cua::CompletedAction& a) {
            return py::bytes(
                reinterpret_cast<const char*>(a.post_frame_rgb.data()),
                a.post_frame_rgb.size());
        })
        .def("__repr__", [](const cua::CompletedAction& a) {
            return "<CompletedAction id=" + std::to_string(a.action_id) +
                   " type=" + cua::action_type_str(a.type) +
                   " @ (" + std::to_string(a.x) + "," + std::to_string(a.y) +
                   ") pre_degraded=" + (a.pre_degraded ? "true" : "false") + ">";
        });

    // ── CaptureEngine ───────────────────────────────────────
    py::class_<cua::CaptureEngine>(m, "CaptureEngine")
        .def(py::init<int, int, int, int>(),
             py::arg("buffer_capacity") = 10,
             py::arg("max_width") = 3840,
             py::arg("max_height") = 2400,
             py::arg("target_fps") = 10)
        .def("init_portal", &cua::CaptureEngine::init_portal,
             py::arg("gjs_script_path") = "",
             "Initialize PipeWire portal (may show GNOME share dialog)")
        .def("start", &cua::CaptureEngine::start,
             "Start all capture/input/action threads")
        .def("stop", &cua::CaptureEngine::stop,
             "Stop all threads")
        .def("pop_action", &cua::CaptureEngine::pop_action,
             "Pop next completed action (None if empty)")
        .def("pop_hotkey", &cua::CaptureEngine::pop_hotkey,
             "Pop next hotkey event (None if empty)")
        .def_property_readonly("completed_count",
             &cua::CaptureEngine::completed_count)
        .def_property_readonly("pending_count",
             &cua::CaptureEngine::pending_count)
        .def_property_readonly("total_frames",
             &cua::CaptureEngine::total_frames)
        .def_property_readonly("latest_frame_ts",
             &cua::CaptureEngine::latest_frame_ts)
        .def_property_readonly("is_running",
             &cua::CaptureEngine::is_running)
        .def("get_cursor_position",
             &cua::CaptureEngine::get_cursor_position)
        // Testing helpers
        .def("inject_mouse_click",
             &cua::CaptureEngine::inject_mouse_click,
             py::arg("ts"), py::arg("x"), py::arg("y"),
             py::arg("button") = "left",
             "Inject a synthetic mouse click (for testing)")
        .def("inject_frame",
             &cua::CaptureEngine::inject_frame,
             py::arg("ts"), py::arg("width"), py::arg("height"),
             "Inject a synthetic frame into ring buffer (for testing)");
}
