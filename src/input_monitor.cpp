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

bool is_ctrl_key(int code) {
    return code == KEY_LEFTCTRL || code == KEY_RIGHTCTRL;
}

// Maps evdev key codes to human-readable key names.
// Covers modifiers, letters, numbers, punctuation, function keys,
// navigation keys, and common special keys.
// Returns "" for unknown codes (but we now track ALL key events).
std::string key_to_name_impl(int code) {
    switch (code) {
        // ── Modifiers ──────────────────────────────────────────────
        case KEY_LEFTCTRL:   return "ctrl_l";
        case KEY_RIGHTCTRL:  return "ctrl_r";
        case KEY_LEFTSHIFT:  return "shift_l";
        case KEY_RIGHTSHIFT: return "shift_r";
        case KEY_LEFTALT:    return "alt_l";
        case KEY_RIGHTALT:   return "alt_r";
        case KEY_LEFTMETA:   return "super_l";
        case KEY_RIGHTMETA:  return "super_r";

        // ── Special keys ───────────────────────────────────────────
        case KEY_ESC:        return "esc";
        case KEY_BACKSPACE:  return "backspace";
        case KEY_DELETE:     return "delete";
        case KEY_ENTER:      return "enter";
        case KEY_TAB:        return "tab";
        case KEY_SPACE:      return "space";
        case KEY_CAPSLOCK:   return "capslock";
        case KEY_INSERT:     return "insert";
        case KEY_HOME:       return "home";
        case KEY_END:        return "end";
        case KEY_PAGEUP:     return "pageup";
        case KEY_PAGEDOWN:   return "pagedown";
        case KEY_LINEFEED:   return "linefeed";
        case KEY_CLEAR:      return "clear";
        case KEY_SYSRQ:      return "sysrq";
        case KEY_SCROLLLOCK: return "scrolllock";
        case KEY_PAUSE:      return "pause";
        case KEY_PRINT:      return "print";
        case KEY_MENU:       return "menu";
        case KEY_FN:         return "fn";

        // ── Navigation / Arrow keys ────────────────────────────────
        case KEY_UP:         return "up";
        case KEY_DOWN:       return "down";
        case KEY_LEFT:       return "left";
        case KEY_RIGHT:      return "right";

        // ── Letters ───────────────────────────────────────────────
        case KEY_A:          return "a";
        case KEY_B:          return "b";
        case KEY_C:          return "c";
        case KEY_D:          return "d";
        case KEY_E:          return "e";
        case KEY_F:          return "f";
        case KEY_G:          return "g";
        case KEY_H:          return "h";
        case KEY_I:          return "i";
        case KEY_J:          return "j";
        case KEY_K:          return "k";
        case KEY_L:          return "l";
        case KEY_M:          return "m";
        case KEY_N:          return "n";
        case KEY_O:          return "o";
        case KEY_P:          return "p";
        case KEY_Q:          return "q";
        case KEY_R:          return "r";
        case KEY_S:          return "s";
        case KEY_T:          return "t";
        case KEY_U:          return "u";
        case KEY_V:          return "v";
        case KEY_W:          return "w";
        case KEY_X:          return "x";
        case KEY_Y:          return "y";
        case KEY_Z:          return "z";

        // ── Numbers (top row, no shift) ────────────────────────────
        case KEY_1:          return "1";
        case KEY_2:          return "2";
        case KEY_3:          return "3";
        case KEY_4:          return "4";
        case KEY_5:          return "5";
        case KEY_6:          return "6";
        case KEY_7:          return "7";
        case KEY_8:          return "8";
        case KEY_9:          return "9";
        case KEY_0:          return "0";

        // ── Punctuation / Symbols ─────────────────────────────────
        case KEY_GRAVE:      return "`";
        case KEY_MINUS:      return "-";
        case KEY_EQUAL:      return "=";
        case KEY_LEFTBRACE:  return "[";
        case KEY_RIGHTBRACE: return "]";
        case KEY_BACKSLASH:  return "\\";
        case KEY_SEMICOLON:  return ";";
        case KEY_APOSTROPHE: return "'";
        case KEY_COMMA:      return ",";
        case KEY_DOT:        return ".";
        case KEY_SLASH:      return "/";
        case KEY_102ND:      return "102nd";
        case KEY_RO:         return "ro";
        case KEY_YEN:        return "yen";

        // ── Numpad keys ────────────────────────────────────────────
        case KEY_KP0:        return "kp_0";
        case KEY_KP1:        return "kp_1";
        case KEY_KP2:        return "kp_2";
        case KEY_KP3:        return "kp_3";
        case KEY_KP4:        return "kp_4";
        case KEY_KP5:        return "kp_5";
        case KEY_KP6:        return "kp_6";
        case KEY_KP7:        return "kp_7";
        case KEY_KP8:        return "kp_8";
        case KEY_KP9:        return "kp_9";
        case KEY_KPASTERISK: return "kp_*";
        case KEY_KPSLASH:    return "kp_/";
        case KEY_KPPLUS:     return "kp_+";
        case KEY_KPMINUS:    return "kp_-";
        case KEY_KPDOT:      return "kp_.";
        case KEY_KPEQUAL:    return "kp_=";
        case KEY_KPLEFTPAREN:return "kp_(";
        case KEY_KPRIGHTPAREN:return "kp_)";
        case KEY_KPCOMMA:    return "kp_,";
        case KEY_KPENTER:    return "kp_enter";
        case KEY_KPJPCOMMA:  return "kp_jpcomma";
        case KEY_NUMLOCK:    return "numlock";

        // ── Function keys ──────────────────────────────────────────
        case KEY_F1:         return "f1";
        case KEY_F2:         return "f2";
        case KEY_F3:         return "f3";
        case KEY_F4:         return "f4";
        case KEY_F5:         return "f5";
        case KEY_F6:         return "f6";
        case KEY_F7:         return "f7";
        case KEY_F8:         return "f8";
        case KEY_F9:         return "f9";
        case KEY_F10:        return "f10";
        case KEY_F11:        return "f11";
        case KEY_F12:        return "f12";
        case KEY_F13:        return "f13";
        case KEY_F14:        return "f14";
        case KEY_F15:        return "f15";
        case KEY_F16:        return "f16";
        case KEY_F17:        return "f17";
        case KEY_F18:        return "f18";
        case KEY_F19:        return "f19";
        case KEY_F20:        return "f20";
        case KEY_F21:        return "f21";
        case KEY_F22:        return "f22";
        case KEY_F23:        return "f23";
        case KEY_F24:        return "f24";

        // ── Media / AC keys ────────────────────────────────────────
        case KEY_PLAYPAUSE:  return "playpause";
        case KEY_STOP:       return "stop";
        case KEY_NEXTSONG:   return "nextsong";
        case KEY_PREVIOUSSONG: return "prevsong";
        case KEY_MUTE:       return "mute";
        case KEY_VOLUMEUP:   return "volumeup";
        case KEY_VOLUMEDOWN: return "volumedown";
        case KEY_POWER:      return "power";
        case KEY_WAKEUP:     return "wakeup";
        case KEY_SLEEP:     return "sleep";
        case KEY_EJECTCD:    return "ejectcd";
        case KEY_EJECTCLOSECD: return "ejectclosecd";
        case KEY_BRIGHTNESSDOWN: return "brightnessdown";
        case KEY_BRIGHTNESSUP:   return "brightnessup";
        case KEY_KBDILLUMTOGGLE: return "kbdillumtoggle";
        case KEY_KBDILLUMDOWN:   return "kbdillumdown";
        case KEY_KBDILLUMUP:     return "kbdillumup";
        case KEY_MICMUTE:    return "micmute";

        // ── Misc / AC shortcuts ────────────────────────────────────
        case KEY_WWW:        return "www";
        case KEY_MAIL:       return "mail";
        case KEY_CALC:       return "calc";
        case KEY_COMPUTER:   return "computer";
        case KEY_SEARCH:     return "search";
        case KEY_HOMEPAGE:   return "homepage";
        case KEY_BACK:       return "browser_back";
        case KEY_FORWARD:    return "browser_forward";
        case KEY_REFRESH:    return "browser_refresh";
        case KEY_BOOKMARKS:  return "browser_bookmarks";
        case KEY_ZOOMIN:     return "zoom_in";
        case KEY_ZOOMOUT:    return "zoom_out";
        case KEY_SCALE:      return "scale";

        default:             return "";  // Unknown key — skip
    }
}

}  // namespace

InputMonitor::InputMonitor() = default;

InputMonitor::~InputMonitor() {
    stop();
}

void InputMonitor::set_capture_region(int, int, int, int, int, int, int, int) {
    // Wayland/PipeWire screen selection is handled by the portal itself.
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
    return key_to_name_impl(code);
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
    std::lock_guard lock(cursor_mu_);
    return {cursor_x_, cursor_y_};
}

void InputMonitor::update_cursor_cache_loop() {
    while (running_.load()) {
        {
            std::lock_guard lock(cursor_mu_);
            switch (cursor_method_) {
                case CursorMethod::CUA_PIXEL:
                    [[fallthrough]];
                case CursorMethod::GNOME_EVAL: {
                    auto result = get_cursor_position_uncached();
                    if (result.first >= 0) {
                        cursor_x_ = result.first;
                        cursor_y_ = result.second;
                    }
                    break;
                }
                default:
                    break;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
}

std::pair<int, int> InputMonitor::get_cursor_position_uncached() {
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
    if (!fs::exists(input_dir)) {
        std::cerr << "[InputMonitor] WARNING: " << input_dir
                  << " not found; input monitoring disabled" << std::endl;
        return;
    }

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

        // Track Ctrl state for Ctrl+Fx collector hotkey detection.
        if (is_ctrl_key(code)) {
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

        // ── ALL key events (for hotkey / combo recording) ──────────
        // Previously this only tracked a restrictive "special key" allowlist,
        // which silently dropped letters/numbers/symbols — breaking combos
        // like Ctrl+A, Shift+1, Ctrl+Shift+5, etc.
        if (dev.is_keyboard && (value == 0 || value == 1)) {
            std::string name = key_to_name_impl(code);
            // Skip keys with no valid name mapping (e.g. KEY_UNKNOWN)
            if (!name.empty()) {
                RawInputEvent raw;
                raw.type = (value == 1) ? RawEventType::KEYBOARD_DOWN : RawEventType::KEYBOARD_UP;
                raw.timestamp_sec = monotonic_now();
                raw.key_code = code;
                raw.key_name = std::move(name);

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

    // Start cursor cache background thread (only if method is available)
    if (cursor_method_ != CursorMethod::NONE) {
        cursor_cache_thread_ = std::thread([this]() {
            update_cursor_cache_loop();
        });
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

    if (cursor_cache_thread_.joinable()) {
        cursor_cache_thread_.join();
    }

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
