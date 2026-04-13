/**
 * @file input_monitor.cpp
 * @brief libevdev-based input monitoring implementation.
 *
 * Scans /dev/input/event* for mouse/keyboard devices, monitors via epoll,
 * and pushes events to a thread-safe queue. Also handles cursor position
 * queries via the CUA GNOME extension D-Bus interface.
 */

#include "input_monitor.h"

#include <sys/epoll.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>

#include <array>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <regex>
#include <sstream>

namespace cua {

namespace {

bool is_tracked_special_key(int code) {
    switch (code) {
        case KEY_LEFTCTRL:
        case KEY_RIGHTCTRL:
        case KEY_LEFTSHIFT:
        case KEY_RIGHTSHIFT:
        case KEY_LEFTALT:
        case KEY_RIGHTALT:
        case KEY_LEFTMETA:
        case KEY_RIGHTMETA:
        case KEY_ESC:
        case KEY_BACKSPACE:
        case KEY_DELETE:
        case KEY_ENTER:
        case KEY_TAB:
        case KEY_FN:
            return true;
        default:
            return false;
    }
}

}  // namespace

InputMonitor::InputMonitor() = default;

InputMonitor::~InputMonitor() {
    stop();
}

double InputMonitor::monotonic_now() const {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

// ─── Button/Key Name Mapping ──────────────────────────────────

std::string InputMonitor::button_to_name(int code) {
    switch (code) {
        case BTN_LEFT:   return "left";
        case BTN_RIGHT:  return "right";
        case BTN_MIDDLE: return "middle";
        default:         return "btn_" + std::to_string(code);
    }
}

std::string InputMonitor::key_to_name(int code) {
    switch (code) {
        case KEY_LEFTCTRL:   return "ctrl_l";
        case KEY_RIGHTCTRL:  return "ctrl_r";
        case KEY_LEFTSHIFT:  return "shift_l";
        case KEY_RIGHTSHIFT: return "shift_r";
        case KEY_LEFTALT:    return "alt_l";
        case KEY_RIGHTALT:   return "alt_r";
        case KEY_LEFTMETA:   return "super_l";
        case KEY_RIGHTMETA:  return "super_r";
        case KEY_ESC:        return "esc";
        case KEY_BACKSPACE:  return "backspace";
        case KEY_DELETE:     return "delete";
        case KEY_ENTER:      return "enter";
        case KEY_TAB:        return "tab";
        case KEY_FN:         return "fn";
        default:             return "";
    }
}

// ─── Cursor Position ──────────────────────────────────────────

void InputMonitor::detect_cursor_method() {
    // Try CUA extension v2 (GetPositionPixel)
    auto result = cursor_cua_pixel();
    if (result.first >= 0) {
        cursor_method_ = CursorMethod::CUA_PIXEL;
        std::cerr << "[InputMonitor] Cursor method: CUA extension (pixel coords)" << std::endl;
        return;
    }

    // Try gnome-eval
    result = cursor_gnome_eval();
    if (result.first >= 0) {
        cursor_method_ = CursorMethod::GNOME_EVAL;
        std::cerr << "[InputMonitor] Cursor method: GNOME Shell.Eval" << std::endl;
        return;
    }

    cursor_method_ = CursorMethod::NONE;
    std::cerr << "[InputMonitor] WARNING: No cursor position method available!" << std::endl;
}

std::pair<int, int> InputMonitor::cursor_cua_pixel() {
    std::array<char, 256> buf;
    FILE* pipe = popen(
        "gdbus call --session "
        "--dest org.cua.CursorTracker "
        "--object-path /org/cua/CursorTracker "
        "--method org.cua.CursorTracker.GetPositionPixel 2>/dev/null",
        "r");
    if (!pipe) return {-1, -1};

    std::string output;
    while (fgets(buf.data(), buf.size(), pipe)) {
        output += buf.data();
    }
    int status = pclose(pipe);
    if (status != 0) return {-1, -1};

    // Parse "(x, y, w, h)"
    std::regex re(R"(\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\))");
    std::smatch match;
    if (std::regex_search(output, match, re) && match.size() >= 3) {
        return {std::stoi(match[1]), std::stoi(match[2])};
    }
    return {-1, -1};
}

std::pair<int, int> InputMonitor::cursor_gnome_eval() {
    std::array<char, 256> buf;
    FILE* pipe = popen(
        "gdbus call --session "
        "--dest org.gnome.Shell "
        "--object-path /org/gnome/Shell "
        "--method org.gnome.Shell.Eval "
        "\"let [x,y]=global.get_pointer(); x+','+y\" 2>/dev/null",
        "r");
    if (!pipe) return {-1, -1};

    std::string output;
    while (fgets(buf.data(), buf.size(), pipe)) {
        output += buf.data();
    }
    int status = pclose(pipe);
    if (status != 0 || output.find("true") == std::string::npos) return {-1, -1};

    // Parse "(true, 'x,y')"
    std::regex re(R"('(\d+),(\d+)')");
    std::smatch match;
    if (std::regex_search(output, match, re) && match.size() >= 3) {
        int x = std::stoi(match[1]);
        int y = std::stoi(match[2]);
        // Apply transform if needed
        int px = static_cast<int>((x - monitor_offset_x_) * monitor_scale_);
        int py = static_cast<int>((y - monitor_offset_y_) * monitor_scale_);
        return {px, py};
    }
    return {-1, -1};
}

std::pair<int, int> InputMonitor::get_cursor_position() {
    switch (cursor_method_) {
        case CursorMethod::CUA_PIXEL:
            return cursor_cua_pixel();
        case CursorMethod::GNOME_EVAL:
            return cursor_gnome_eval();
        default:
            return {0, 0};
    }
}

// ─── Device Scanning ──────────────────────────────────────────

void InputMonitor::scan_devices() {
    namespace fs = std::filesystem;

    std::string input_dir = "/dev/input";
    for (auto& entry : fs::directory_iterator(input_dir)) {
        std::string path = entry.path().string();
        if (path.find("event") == std::string::npos) continue;

        int fd = open(path.c_str(), O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;

        ::libevdev* dev = nullptr;
        int rc = libevdev_new_from_fd(fd, &dev);
        if (rc < 0) {
            close(fd);
            continue;
        }

        bool is_keyboard = libevdev_has_event_type(dev, EV_KEY) &&
                           libevdev_has_event_code(dev, EV_KEY, KEY_A);
        bool is_mouse = (libevdev_has_event_type(dev, EV_KEY) &&
                         libevdev_has_event_code(dev, EV_KEY, BTN_LEFT)) ||
                        (libevdev_has_event_type(dev, EV_REL) &&
                         libevdev_has_event_code(dev, EV_REL, REL_X));

        if (!is_keyboard && !is_mouse) {
            libevdev_free(dev);
            close(fd);
            continue;
        }

        const char* name = libevdev_get_name(dev);
        std::string dev_name = name ? name : "unknown";

        devices_.push_back({fd, dev, is_keyboard, is_mouse, dev_name});

        std::string kind;
        if (is_keyboard) kind += "kbd";
        if (is_mouse) kind += (kind.empty() ? "" : "+") + std::string("mouse");
        std::cerr << "[InputMonitor] Monitoring: " << dev_name
                  << " (" << kind << ")" << std::endl;
    }
}

// ─── Event Processing ─────────────────────────────────────────

void InputMonitor::process_event(DeviceInfo& dev, const ::input_event& ev) {
    if (ev.type == EV_KEY) {
        int code = ev.code;
        int value = ev.value;  // 0=up, 1=down, 2=repeat

        // Track Ctrl state
        if (code == KEY_LEFTCTRL || code == KEY_RIGHTCTRL) {
            ctrl_pressed_ = (value != 0);
        }

        // Hotkey detection (on key-down only)
        if (value == 1 && ctrl_pressed_) {
            if (code == KEY_F8 && hotkey_cb_)
                hotkey_cb_(HotkeyType::START_TASK);
            else if (code == KEY_F9 && hotkey_cb_)
                hotkey_cb_(HotkeyType::SCREENSHOT);
            else if (code == KEY_F12 && hotkey_cb_)
                hotkey_cb_(HotkeyType::END_TASK);
        }
        if (value == 1 && code == KEY_ESC && hotkey_cb_) {
            hotkey_cb_(HotkeyType::DROP_ACTION);
        }

        // Mouse button events
        if (dev.is_mouse && (code == BTN_LEFT || code == BTN_RIGHT || code == BTN_MIDDLE)) {
            if (value == 0 || value == 1) {
                RawInputEvent raw;
                raw.type = (value == 1) ? RawEventType::MOUSE_BTN_DOWN : RawEventType::MOUSE_BTN_UP;
                raw.timestamp_sec = monotonic_now();
                raw.button = code;
                raw.button_name = button_to_name(code);

                // Get cursor position
                auto [cx, cy] = get_cursor_position();
                raw.x = cx;
                raw.y = cy;

                push_event(std::move(raw));
            }
        }

        // Key events (modifiers and special keys)
        if (dev.is_keyboard && (value == 0 || value == 1)) {
            // Match V1 policy: explicit special-key allowlist only.
            if (is_tracked_special_key(code)) {
                RawInputEvent raw;
                raw.type = (value == 1) ? RawEventType::KEYBOARD_DOWN : RawEventType::KEYBOARD_UP;
                raw.timestamp_sec = monotonic_now();
                raw.key_code = code;
                raw.key_name = key_to_name(code);
                if (raw.key_name.empty()) return;

                auto [cx, cy] = get_cursor_position();
                raw.x = cx;
                raw.y = cy;

                push_event(std::move(raw));
            }
        }
    }
    else if (ev.type == EV_REL && dev.is_mouse) {
        if (ev.code == REL_WHEEL || ev.code == REL_WHEEL_HI_RES) {
            RawInputEvent raw;
            raw.type = RawEventType::SCROLL_EVENT;
            raw.timestamp_sec = monotonic_now();
            raw.scroll_dy = ev.value;

            auto [cx, cy] = get_cursor_position();
            raw.x = cx;
            raw.y = cy;

            push_event(std::move(raw));
        }
        else if (ev.code == REL_HWHEEL || ev.code == REL_HWHEEL_HI_RES) {
            RawInputEvent raw;
            raw.type = RawEventType::SCROLL_EVENT;
            raw.timestamp_sec = monotonic_now();
            raw.scroll_dx = ev.value;

            auto [cx, cy] = get_cursor_position();
            raw.x = cx;
            raw.y = cy;

            push_event(std::move(raw));
        }
    }
}

void InputMonitor::push_event(RawInputEvent&& ev) {
    std::lock_guard lock(queue_mu_);
    queue_.push_back(std::move(ev));
}

// ─── Monitor Loop ─────────────────────────────────────────────

void InputMonitor::monitor_loop() {
    // Create epoll
    epoll_fd_ = epoll_create1(0);
    if (epoll_fd_ < 0) {
        std::cerr << "[InputMonitor] epoll_create1 failed" << std::endl;
        return;
    }

    for (auto& dev : devices_) {
        struct epoll_event epev;
        epev.events = EPOLLIN;
        epev.data.ptr = &dev;
        epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, dev.fd, &epev);
    }

    struct epoll_event events[16];

    while (running_) {
        int nfds = epoll_wait(epoll_fd_, events, 16, 50);  // 50ms timeout
        if (nfds < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < nfds; i++) {
            auto* dinfo = static_cast<DeviceInfo*>(events[i].data.ptr);
            ::input_event iev;
            int rc;

            while ((rc = libevdev_next_event(dinfo->dev,
                        LIBEVDEV_READ_FLAG_NORMAL, &iev)) == LIBEVDEV_READ_STATUS_SUCCESS) {
                process_event(*dinfo, iev);
            }
            // Handle SYN_DROPPED
            if (rc == LIBEVDEV_READ_STATUS_SYNC) {
                while (libevdev_next_event(dinfo->dev,
                           LIBEVDEV_READ_FLAG_SYNC, &iev) == LIBEVDEV_READ_STATUS_SYNC) {
                    // Drain sync events
                }
            }
        }
    }

    // Cleanup
    close(epoll_fd_);
    epoll_fd_ = -1;

    for (auto& dev : devices_) {
        libevdev_free(dev.dev);
        close(dev.fd);
    }
    devices_.clear();
}

// ─── Start / Stop ─────────────────────────────────────────────

void InputMonitor::start() {
    if (running_.exchange(true)) return;

    detect_cursor_method();
    scan_devices();

    if (devices_.empty()) {
        std::cerr << "[InputMonitor] WARNING: No input devices found! "
                  << "Run as root or add user to 'input' group." << std::endl;
    }

    monitor_thread_ = std::thread([this]() {
        monitor_loop();
        running_ = false;
    });

    std::cerr << "[InputMonitor] Input monitoring started ("
              << devices_.size() << " devices)" << std::endl;
}

void InputMonitor::stop() {
    if (!running_.exchange(false)) return;

    if (monitor_thread_.joinable()) {
        monitor_thread_.join();
    }

    std::cerr << "[InputMonitor] Input monitoring stopped" << std::endl;
}

// ─── Queue Interface ──────────────────────────────────────────

bool InputMonitor::pop_event(RawInputEvent& out) {
    std::lock_guard lock(queue_mu_);
    if (queue_.empty()) return false;
    out = std::move(queue_.front());
    queue_.pop_front();
    return true;
}

size_t InputMonitor::pending_count() const {
    std::lock_guard lock(queue_mu_);
    return queue_.size();
}

}  // namespace cua
