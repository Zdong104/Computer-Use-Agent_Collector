/**
 * @file input_monitor_win.cpp
 * @brief Win32 low-level mouse / keyboard input monitoring.
 */

#include "input_monitor.h"

#ifdef _WIN32

#include <chrono>
#include <cctype>
#include <cmath>
#include <iostream>
#include <algorithm>

namespace cua {

namespace {

constexpr int CUA_BTN_LEFT = 1;
constexpr int CUA_BTN_RIGHT = 2;
constexpr int CUA_BTN_MIDDLE = 3;

double steady_now_sec() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

DWORD normalized_vk(const KBDLLHOOKSTRUCT& event) {
    if (event.vkCode == VK_SHIFT) {
        return MapVirtualKeyW(event.scanCode, MAPVK_VSC_TO_VK_EX);
    }
    if (event.vkCode == VK_CONTROL) {
        return (event.flags & LLKHF_EXTENDED) ? VK_RCONTROL : VK_LCONTROL;
    }
    if (event.vkCode == VK_MENU) {
        return (event.flags & LLKHF_EXTENDED) ? VK_RMENU : VK_LMENU;
    }
    return event.vkCode;
}

bool is_ctrl_key(DWORD vk) {
    return vk == VK_CONTROL || vk == VK_LCONTROL || vk == VK_RCONTROL;
}

bool is_modifier_name(const std::string& name) {
    return name == "ctrl_l"  || name == "ctrl_r"  ||
           name == "shift_l" || name == "shift_r" ||
           name == "alt_l"   || name == "alt_r"   ||
           name == "super_l" || name == "super_r" ||
           name == "fn";
}

bool contains_name(const std::vector<std::string>& names,
                   const std::string& name) {
    return std::find(names.begin(), names.end(), name) != names.end();
}

void add_name(std::vector<std::string>& names, const std::string& name) {
    if (!contains_name(names, name)) {
        names.push_back(name);
    }
}

void remove_name(std::vector<std::string>& names, const std::string& name) {
    names.erase(std::remove(names.begin(), names.end(), name), names.end());
}

int wheel_units(DWORD mouse_data) {
    const auto delta = static_cast<short>(HIWORD(mouse_data));
    if (delta == 0) return 0;
    const int units = delta / WHEEL_DELTA;
    return units != 0 ? units : (delta > 0 ? 1 : -1);
}

}  // namespace

InputMonitor* InputMonitor::active_instance_ = nullptr;

InputMonitor::InputMonitor() = default;

InputMonitor::~InputMonitor() {
    stop();
}

void InputMonitor::set_capture_region(int left, int top, int width, int height,
                                      int output_width, int output_height,
                                      int logical_left, int logical_top) {
    if (width <= 0 || height <= 0) {
        region_set_ = false;
        recorded_keys_.clear();
        recorded_buttons_.clear();
        return;
    }

    region_left_ = left;
    region_top_ = top;
    region_width_ = width;
    region_height_ = height;
    output_width_ = output_width > 0 ? output_width : width;
    output_height_ = output_height > 0 ? output_height : height;
    logical_left_ = logical_left;
    logical_top_ = logical_top;
    logical_width_ = output_width_;
    logical_height_ = output_height_;
    region_set_ = true;
}

double InputMonitor::monotonic_now() const {
    return steady_now_sec();
}

std::string InputMonitor::button_to_name(int code) {
    switch (code) {
        case CUA_BTN_LEFT: return "left";
        case CUA_BTN_RIGHT: return "right";
        case CUA_BTN_MIDDLE: return "middle";
        default: return "btn_" + std::to_string(code);
    }
}

std::string InputMonitor::key_to_name(int code) {
    const DWORD vk = static_cast<DWORD>(code);
    switch (vk) {
        case VK_LCONTROL: return "ctrl_l";
        case VK_RCONTROL: return "ctrl_r";
        case VK_LSHIFT: return "shift_l";
        case VK_RSHIFT: return "shift_r";
        case VK_LMENU: return "alt_l";
        case VK_RMENU: return "alt_r";
        case VK_LWIN: return "super_l";
        case VK_RWIN: return "super_r";
        case VK_ESCAPE: return "esc";
        case VK_BACK: return "backspace";
        case VK_DELETE: return "delete";
        case VK_RETURN: return "enter";
        case VK_TAB: return "tab";
        case VK_SPACE: return "space";
        case VK_CAPITAL: return "capslock";
        case VK_INSERT: return "insert";
        case VK_HOME: return "home";
        case VK_END: return "end";
        case VK_PRIOR: return "pageup";
        case VK_NEXT: return "pagedown";
        case VK_SNAPSHOT: return "print";
        case VK_SCROLL: return "scrolllock";
        case VK_PAUSE: return "pause";
        case VK_APPS: return "menu";
        case VK_UP: return "up";
        case VK_DOWN: return "down";
        case VK_LEFT: return "left";
        case VK_RIGHT: return "right";
        case VK_NUMLOCK: return "numlock";
        case VK_OEM_3: return "`";
        case VK_OEM_MINUS: return "-";
        case VK_OEM_PLUS: return "=";
        case VK_OEM_4: return "[";
        case VK_OEM_6: return "]";
        case VK_OEM_5: return "\\";
        case VK_OEM_1: return ";";
        case VK_OEM_7: return "'";
        case VK_OEM_COMMA: return ",";
        case VK_OEM_PERIOD: return ".";
        case VK_OEM_2: return "/";
        case VK_MULTIPLY: return "kp_*";
        case VK_DIVIDE: return "kp_/";
        case VK_ADD: return "kp_+";
        case VK_SUBTRACT: return "kp_-";
        case VK_DECIMAL: return "kp_.";
        case VK_VOLUME_MUTE: return "mute";
        case VK_VOLUME_UP: return "volumeup";
        case VK_VOLUME_DOWN: return "volumedown";
        case VK_MEDIA_PLAY_PAUSE: return "playpause";
        case VK_MEDIA_STOP: return "stop";
        case VK_MEDIA_NEXT_TRACK: return "nextsong";
        case VK_MEDIA_PREV_TRACK: return "prevsong";
        default:
            break;
    }

    if (vk >= 'A' && vk <= 'Z') {
        char c = static_cast<char>(std::tolower(static_cast<int>(vk)));
        return std::string(1, c);
    }
    if (vk >= '0' && vk <= '9') {
        return std::string(1, static_cast<char>(vk));
    }
    if (vk >= VK_NUMPAD0 && vk <= VK_NUMPAD9) {
        return "kp_" + std::to_string(vk - VK_NUMPAD0);
    }
    if (vk >= VK_F1 && vk <= VK_F24) {
        return "f" + std::to_string(vk - VK_F1 + 1);
    }

    return "";
}

std::pair<int, int> InputMonitor::get_cursor_position() {
    POINT pt {};
    if (!GetCursorPos(&pt)) {
        return {0, 0};
    }
    int x = 0;
    int y = 0;
    translate_point(static_cast<int>(pt.x), static_cast<int>(pt.y), x, y, true);
    return {x, y};
}

bool InputMonitor::translate_point(int screen_x, int screen_y, int& out_x, int& out_y,
                                   bool clamp_to_region) const {
    const int left = region_set_ ? region_left_ : GetSystemMetrics(SM_XVIRTUALSCREEN);
    const int top = region_set_ ? region_top_ : GetSystemMetrics(SM_YVIRTUALSCREEN);
    const int width = region_set_ ? region_width_ : GetSystemMetrics(SM_CXVIRTUALSCREEN);
    const int height = region_set_ ? region_height_ : GetSystemMetrics(SM_CYVIRTUALSCREEN);
    const int out_width = region_set_ ? output_width_ : width;
    const int out_height = region_set_ ? output_height_ : height;
    const int logical_left = region_set_ ? logical_left_ : left;
    const int logical_top = region_set_ ? logical_top_ : top;
    const int logical_width = region_set_ ? logical_width_ : width;
    const int logical_height = region_set_ ? logical_height_ : height;

    bool inside = screen_x >= left && screen_x < left + width &&
                  screen_y >= top && screen_y < top + height;
    bool logical_inside = false;
    int mapped_x = screen_x;
    int mapped_y = screen_y;
    int source_left = left;
    int source_top = top;
    int source_width = width;
    int source_height = height;

    if (!inside && region_set_) {
        logical_inside = screen_x >= logical_left &&
                         screen_x < logical_left + logical_width &&
                         screen_y >= logical_top &&
                         screen_y < logical_top + logical_height;
        if (logical_inside) {
            inside = true;
            source_left = logical_left;
            source_top = logical_top;
            source_width = logical_width;
            source_height = logical_height;
        }
    }

    if (clamp_to_region && !inside) {
        mapped_x = std::clamp(screen_x, left, left + std::max(1, width) - 1);
        mapped_y = std::clamp(screen_y, top, top + std::max(1, height) - 1);
    }

    out_x = static_cast<int>(
        std::lround((mapped_x - source_left) * static_cast<double>(out_width) /
                    static_cast<double>(std::max(1, source_width)))
    );
    out_y = static_cast<int>(
        std::lround((mapped_y - source_top) * static_cast<double>(out_height) /
                    static_cast<double>(std::max(1, source_height)))
    );
    return inside;
}

bool InputMonitor::should_record_key(const std::string& name, bool is_down,
                                     int x, int y) {
    const bool inside = !region_set_ ||
        (x >= 0 && x < output_width_ && y >= 0 && y < output_height_);

    if (is_modifier_name(name)) {
        return true;
    }

    if (is_down) {
        if (inside) {
            add_name(recorded_keys_, name);
            return true;
        }
        return false;
    }

    if (contains_name(recorded_keys_, name)) {
        remove_name(recorded_keys_, name);
        return true;
    }
    return inside;
}

bool InputMonitor::should_record_button(const std::string& button_name, bool is_down,
                                        int x, int y) {
    const bool inside = !region_set_ ||
        (x >= 0 && x < output_width_ && y >= 0 && y < output_height_);

    if (is_down) {
        if (inside) {
            add_name(recorded_buttons_, button_name);
            return true;
        }
        return false;
    }

    if (contains_name(recorded_buttons_, button_name)) {
        remove_name(recorded_buttons_, button_name);
        return true;
    }
    return false;
}

void InputMonitor::start() {
    if (running_.exchange(true)) return;

    active_instance_ = this;
    monitor_thread_ = std::thread([this]() {
        monitor_loop();
    });
    for (int i = 0; i < 100 && hook_thread_id_.load() == 0; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    std::cerr << "[InputMonitor] Win32 input monitoring started" << std::endl;
}

void InputMonitor::stop() {
    if (!running_.exchange(false)) return;

    const DWORD hook_thread_id = hook_thread_id_.load();
    if (hook_thread_id != 0) {
        PostThreadMessageW(hook_thread_id, WM_QUIT, 0, 0);
    }
    if (monitor_thread_.joinable()) {
        monitor_thread_.join();
    }
    if (active_instance_ == this) {
        active_instance_ = nullptr;
    }

    std::cerr << "[InputMonitor] Win32 input monitoring stopped" << std::endl;
}

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

void InputMonitor::push_event(RawInputEvent&& ev) {
    std::lock_guard lock(queue_mu_);
    queue_.push_back(std::move(ev));
}

void InputMonitor::monitor_loop() {
    MSG init_msg {};
    PeekMessageW(&init_msg, nullptr, WM_USER, WM_USER, PM_NOREMOVE);
    hook_thread_id_.store(GetCurrentThreadId());

    keyboard_hook_ = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, nullptr, 0);
    mouse_hook_ = SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, nullptr, 0);

    if (!keyboard_hook_) {
        std::cerr << "[InputMonitor] WARNING: SetWindowsHookEx keyboard failed: "
                  << GetLastError() << std::endl;
    }
    if (!mouse_hook_) {
        std::cerr << "[InputMonitor] WARNING: SetWindowsHookEx mouse failed: "
                  << GetLastError() << std::endl;
    }

    MSG msg {};
    while (running_.load()) {
        BOOL result = GetMessageW(&msg, nullptr, 0, 0);
        if (result <= 0) break;
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (keyboard_hook_) {
        UnhookWindowsHookEx(keyboard_hook_);
        keyboard_hook_ = nullptr;
    }
    if (mouse_hook_) {
        UnhookWindowsHookEx(mouse_hook_);
        mouse_hook_ = nullptr;
    }
    hook_thread_id_.store(0);
}

LRESULT CALLBACK InputMonitor::keyboard_proc(int code, WPARAM wparam, LPARAM lparam) {
    if (code == HC_ACTION && active_instance_) {
        auto* event = reinterpret_cast<KBDLLHOOKSTRUCT*>(lparam);
        if (event) {
            active_instance_->handle_keyboard_event(wparam, *event);
        }
    }
    return CallNextHookEx(nullptr, code, wparam, lparam);
}

LRESULT CALLBACK InputMonitor::mouse_proc(int code, WPARAM wparam, LPARAM lparam) {
    if (code == HC_ACTION && active_instance_) {
        auto* event = reinterpret_cast<MSLLHOOKSTRUCT*>(lparam);
        if (event) {
            active_instance_->handle_mouse_event(wparam, *event);
        }
    }
    return CallNextHookEx(nullptr, code, wparam, lparam);
}

void InputMonitor::handle_keyboard_event(WPARAM wparam, const KBDLLHOOKSTRUCT& event) {
    const bool is_down = (wparam == WM_KEYDOWN || wparam == WM_SYSKEYDOWN);
    const bool is_up = (wparam == WM_KEYUP || wparam == WM_SYSKEYUP);
    if (!is_down && !is_up) return;

    const DWORD vk = normalized_vk(event);
    const std::string name = key_to_name(static_cast<int>(vk));
    if (name.empty()) return;

    if (is_ctrl_key(vk)) {
        ctrl_pressed_ = is_down;
    }

    if (is_down && ctrl_pressed_) {
        if (vk == VK_F8 && hotkey_cb_) {
            hotkey_cb_(HotkeyType::START_TASK);
        } else if (vk == VK_F9 && hotkey_cb_) {
            hotkey_cb_(HotkeyType::SCREENSHOT);
        } else if (vk == VK_F12 && hotkey_cb_) {
            hotkey_cb_(HotkeyType::END_TASK);
        }
    }
    if (is_down && vk == VK_ESCAPE && hotkey_cb_) {
        hotkey_cb_(HotkeyType::DROP_ACTION);
    }

    RawInputEvent raw;
    raw.type = is_down ? RawEventType::KEYBOARD_DOWN : RawEventType::KEYBOARD_UP;
    raw.timestamp_sec = monotonic_now();
    raw.key_code = static_cast<int>(vk);
    raw.key_name = name;
    POINT pt {};
    if (!GetCursorPos(&pt)) {
        pt.x = 0;
        pt.y = 0;
    }
    int cx = 0;
    int cy = 0;
    translate_point(static_cast<int>(pt.x), static_cast<int>(pt.y), cx, cy, true);
    if (!should_record_key(name, is_down, cx, cy)) {
        return;
    }
    raw.x = cx;
    raw.y = cy;
    push_event(std::move(raw));
}

void InputMonitor::handle_mouse_event(WPARAM wparam, const MSLLHOOKSTRUCT& event) {
    RawInputEvent raw;
    raw.timestamp_sec = monotonic_now();
    bool inside = translate_point(static_cast<int>(event.pt.x),
                                  static_cast<int>(event.pt.y),
                                  raw.x, raw.y, false);

    switch (wparam) {
        case WM_LBUTTONDOWN:
            raw.type = RawEventType::MOUSE_BTN_DOWN;
            raw.button = CUA_BTN_LEFT;
            raw.button_name = button_to_name(CUA_BTN_LEFT);
            if (!should_record_button(raw.button_name, true, raw.x, raw.y)) return;
            break;
        case WM_LBUTTONUP:
            raw.type = RawEventType::MOUSE_BTN_UP;
            raw.button = CUA_BTN_LEFT;
            raw.button_name = button_to_name(CUA_BTN_LEFT);
            if (!should_record_button(raw.button_name, false, raw.x, raw.y)) return;
            if (!inside) translate_point(static_cast<int>(event.pt.x),
                                         static_cast<int>(event.pt.y),
                                         raw.x, raw.y, true);
            break;
        case WM_RBUTTONDOWN:
            raw.type = RawEventType::MOUSE_BTN_DOWN;
            raw.button = CUA_BTN_RIGHT;
            raw.button_name = button_to_name(CUA_BTN_RIGHT);
            if (!should_record_button(raw.button_name, true, raw.x, raw.y)) return;
            break;
        case WM_RBUTTONUP:
            raw.type = RawEventType::MOUSE_BTN_UP;
            raw.button = CUA_BTN_RIGHT;
            raw.button_name = button_to_name(CUA_BTN_RIGHT);
            if (!should_record_button(raw.button_name, false, raw.x, raw.y)) return;
            if (!inside) translate_point(static_cast<int>(event.pt.x),
                                         static_cast<int>(event.pt.y),
                                         raw.x, raw.y, true);
            break;
        case WM_MBUTTONDOWN:
            raw.type = RawEventType::MOUSE_BTN_DOWN;
            raw.button = CUA_BTN_MIDDLE;
            raw.button_name = button_to_name(CUA_BTN_MIDDLE);
            if (!should_record_button(raw.button_name, true, raw.x, raw.y)) return;
            break;
        case WM_MBUTTONUP:
            raw.type = RawEventType::MOUSE_BTN_UP;
            raw.button = CUA_BTN_MIDDLE;
            raw.button_name = button_to_name(CUA_BTN_MIDDLE);
            if (!should_record_button(raw.button_name, false, raw.x, raw.y)) return;
            if (!inside) translate_point(static_cast<int>(event.pt.x),
                                         static_cast<int>(event.pt.y),
                                         raw.x, raw.y, true);
            break;
        case WM_MOUSEWHEEL:
            raw.type = RawEventType::SCROLL_EVENT;
            raw.scroll_dy = wheel_units(event.mouseData);
            if (region_set_ && !inside) return;
            break;
        case WM_MOUSEHWHEEL:
            raw.type = RawEventType::SCROLL_EVENT;
            raw.scroll_dx = wheel_units(event.mouseData);
            if (region_set_ && !inside) return;
            break;
        default:
            return;
    }

    push_event(std::move(raw));
}

}  // namespace cua

#endif
