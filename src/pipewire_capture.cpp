/**
 * @file pipewire_capture.cpp
 * @brief Continuous screen capture via the PipeWire screencast portal.
 */

#include "pipewire_capture.h"

#include <pipewire/core.h>
#include <pipewire/properties.h>
#include <pipewire/keys.h>
#include <pipewire/stream.h>
#include <spa/pod/builder.h>
#include <spa/pod/vararg.h>
#include <spa/param/video/raw-utils.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <filesystem>

#if defined(__SSE2__) || defined(__SSSE3__)
#include <x86intrin.h>
#elif defined(_M_X64) || defined(_M_IX86)
#include <intrin.h>
#endif

#include <iostream>
#include <regex>
#include <stdexcept>
#include <string_view>
#include <thread>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

namespace cua {

namespace {

double monotonic_now_sec() {
    struct timespec ts {};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) +
           static_cast<double>(ts.tv_nsec) / 1e9;
}

std::string extract_json_string(const std::string& payload, const std::string& key) {
    std::regex re("\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
    std::smatch match;
    if (std::regex_search(payload, match, re) && match.size() >= 2) {
        return match[1].str();
    }
    return "";
}

int extract_json_int(const std::string& payload, const std::string& key, int fallback = -1) {
    std::regex re("\"" + key + "\"\\s*:\\s*(-?[0-9]+)");
    std::smatch match;
    if (std::regex_search(payload, match, re) && match.size() >= 2) {
        return std::stoi(match[1].str());
    }
    return fallback;
}

int bytes_per_pixel(uint32_t format) {
    switch (format) {
        case SPA_VIDEO_FORMAT_BGRx:
        case SPA_VIDEO_FORMAT_BGRA:
        case SPA_VIDEO_FORMAT_RGBx:
        case SPA_VIDEO_FORMAT_RGBA:
            return 4;
        default:
            return 0;
    }
}

}  // namespace

PipeWireCapture::PipeWireCapture(RingBuffer& buffer, int target_fps)
    : buffer_(buffer)
    , target_fps_(std::max(1, target_fps))
    , min_frame_interval_(1.0 / static_cast<double>(std::max(1, target_fps))) {
    pw_init(nullptr, nullptr);
}

PipeWireCapture::~PipeWireCapture() {
    stop();
}

void PipeWireCapture::log_status(const std::string& msg) {
    if (status_cb_) {
        status_cb_(msg);
    } else {
        std::cerr << "[PipeWireCapture] " << msg << std::endl;
    }
}

bool PipeWireCapture::init_portal(const std::string& gjs_script_path) {
    if (pw_fd_ >= 0 && pw_node_id_ >= 0) return true;
    return spawn_gjs_helper(gjs_script_path);
}

void PipeWireCapture::set_capture_region(int, int, int, int, int, int, int, int) {
    // Wayland monitor selection is delegated to the XDG portal dialog.
}

void PipeWireCapture::start() {
    if (running_.load()) return;
    if (pw_fd_ < 0 || pw_node_id_ < 0) {
        log_status("No portal session initialized; skipping live capture start");
        return;
    }

    running_.store(true);
    capture_thread_ = std::thread(&PipeWireCapture::capture_loop, this);
}

void PipeWireCapture::stop() {
    bool was_running = running_.exchange(false);

    if (loop_) {
        pw_main_loop_quit(loop_);
    }

    if (capture_thread_.joinable()) {
        capture_thread_.join();
    }

    if (stream_) {
        pw_stream_disconnect(stream_);
        pw_stream_destroy(stream_);
        stream_ = nullptr;
    }
    if (core_) {
        pw_core_disconnect(core_);
        core_ = nullptr;
    }
    if (context_) {
        pw_context_destroy(context_);
        context_ = nullptr;
    }
    if (loop_) {
        pw_main_loop_destroy(loop_);
        loop_ = nullptr;
    }
    std::memset(&stream_listener_, 0, sizeof(stream_listener_));

    if (pw_fd_ >= 0 && !was_running) {
        close(pw_fd_);
        pw_fd_ = -1;
    }

    kill_gjs_helper();
}

bool PipeWireCapture::spawn_gjs_helper(const std::string& script_path) {
    std::filesystem::path helper_path = script_path.empty()
        ? (std::filesystem::path(__FILE__).parent_path() / "portal_helper.py")
        : std::filesystem::path(script_path);

    if (!std::filesystem::exists(helper_path)) {
        log_status("Portal helper not found: " + helper_path.string());
        return false;
    }

    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
        log_status("socketpair() failed: " + std::string(std::strerror(errno)));
        return false;
    }

    int flags = fcntl(sv[1], F_GETFD);
    if (flags >= 0) {
        fcntl(sv[1], F_SETFD, flags & ~FD_CLOEXEC);
    }

    pid_t pid = fork();
    if (pid < 0) {
        log_status("fork() failed: " + std::string(std::strerror(errno)));
        close(sv[0]);
        close(sv[1]);
        return false;
    }

    if (pid == 0) {
        close(sv[0]);
        std::string fd_arg = std::to_string(sv[1]);
        execl("/usr/bin/python3", "python3",
              helper_path.c_str(), fd_arg.c_str(), static_cast<char*>(nullptr));
        execlp("python3", "python3",
               helper_path.c_str(), fd_arg.c_str(), static_cast<char*>(nullptr));
        _exit(127);
    }

    close(sv[1]);
    gjs_pid_ = pid;
    gjs_stdin_fd_ = sv[0];
    gjs_stdout_fd_ = sv[0];

    char msg_buf[4096] = {};
    char control[CMSG_SPACE(sizeof(int))] = {};
    struct iovec iov {
        .iov_base = msg_buf,
        .iov_len = sizeof(msg_buf) - 1,
    };
    struct msghdr msg {};
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control;
    msg.msg_controllen = sizeof(control);

    ssize_t n = recvmsg(gjs_stdout_fd_, &msg, 0);
    if (n <= 0) {
        log_status("Portal helper did not return screencast details");
        kill_gjs_helper();
        return false;
    }
    msg_buf[n] = '\0';
    std::string payload(msg_buf);

    std::string error = extract_json_string(payload, "error");
    if (!error.empty()) {
        log_status("Portal helper error: " + error);
        kill_gjs_helper();
        return false;
    }

    int received_fd = -1;
    for (struct cmsghdr* cmsg = CMSG_FIRSTHDR(&msg);
         cmsg != nullptr;
         cmsg = CMSG_NXTHDR(&msg, cmsg)) {
        if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS) {
            std::memcpy(&received_fd, CMSG_DATA(cmsg), sizeof(int));
            break;
        }
    }

    pw_node_id_ = extract_json_int(payload, "node_id", -1);
    if (pw_node_id_ < 0 || received_fd < 0) {
        log_status("Portal helper response missing node_id or PipeWire fd");
        if (received_fd >= 0) close(received_fd);
        kill_gjs_helper();
        return false;
    }

    pw_fd_ = received_fd;
    log_status("Portal ready: node=" + std::to_string(pw_node_id_));
    return true;
}

void PipeWireCapture::kill_gjs_helper() {
    if (gjs_stdin_fd_ >= 0) {
        (void)send(gjs_stdin_fd_, "quit", 4, MSG_NOSIGNAL);
        close(gjs_stdin_fd_);
        gjs_stdin_fd_ = -1;
        gjs_stdout_fd_ = -1;
    }

    if (gjs_pid_ > 0) {
        int status = 0;
        for (int i = 0; i < 20; ++i) {
            pid_t waited = waitpid(gjs_pid_, &status, WNOHANG);
            if (waited == gjs_pid_) {
                gjs_pid_ = -1;
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        kill(gjs_pid_, SIGTERM);
        waitpid(gjs_pid_, &status, 0);
        gjs_pid_ = -1;
    }
}

void PipeWireCapture::capture_loop() {
    loop_ = pw_main_loop_new(nullptr);
    if (!loop_) {
        log_status("pw_main_loop_new() failed");
        running_.store(false);
        return;
    }

    context_ = pw_context_new(pw_main_loop_get_loop(loop_), nullptr, 0);
    if (!context_) {
        log_status("pw_context_new() failed");
        running_.store(false);
        return;
    }

    core_ = pw_context_connect_fd(context_, pw_fd_, nullptr, 0);
    if (!core_) {
        log_status("pw_context_connect_fd() failed");
        running_.store(false);
        return;
    }
    pw_fd_ = -1;  // ownership transferred to PipeWire

    auto* props = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Video",
        PW_KEY_MEDIA_CATEGORY, "Capture",
        PW_KEY_MEDIA_ROLE, "Screen",
        nullptr
    );

    stream_ = pw_stream_new(core_, "cua-screen-capture", props);
    if (!stream_) {
        log_status("pw_stream_new() failed");
        running_.store(false);
        return;
    }

    static constexpr pw_stream_events stream_events = {
        .version = PW_VERSION_STREAM_EVENTS,
        .destroy = nullptr,
        .state_changed = &PipeWireCapture::on_state_changed,
        .control_info = nullptr,
        .io_changed = nullptr,
        .param_changed = &PipeWireCapture::on_param_changed,
        .add_buffer = nullptr,
        .remove_buffer = nullptr,
        .process = &PipeWireCapture::on_process,
        .drained = nullptr,
        .command = nullptr,
        .trigger_done = nullptr,
    };
    pw_stream_add_listener(stream_, &stream_listener_, &stream_events, this);

    uint8_t buffer[1024];
    spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const spa_pod* params[1];
    params[0] = reinterpret_cast<spa_pod*>(spa_pod_builder_add_object(
        &b,
        SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(
            4,
            SPA_VIDEO_FORMAT_BGRx,
            SPA_VIDEO_FORMAT_BGRA,
            SPA_VIDEO_FORMAT_RGBx,
            SPA_VIDEO_FORMAT_RGBA
        )
    ));

    int res = pw_stream_connect(
        stream_,
        PW_DIRECTION_INPUT,
        static_cast<uint32_t>(pw_node_id_),
        static_cast<pw_stream_flags>(
            PW_STREAM_FLAG_AUTOCONNECT |
            PW_STREAM_FLAG_MAP_BUFFERS
        ),
        params,
        1
    );
    if (res < 0) {
        log_status("pw_stream_connect() failed: " + std::to_string(res));
        running_.store(false);
        return;
    }

    log_status("Capture stream connected");
    pw_main_loop_run(loop_);
}

void PipeWireCapture::on_state_changed(void* userdata,
                                       enum pw_stream_state /*old_state*/,
                                       enum pw_stream_state state,
                                       const char* error) {
    auto* self = static_cast<PipeWireCapture*>(userdata);
    if (!self) return;

    if (state == PW_STREAM_STATE_ERROR) {
        self->log_status(std::string("PipeWire stream error: ") +
                         (error ? error : "unknown"));
        self->running_.store(false);
        if (self->loop_) pw_main_loop_quit(self->loop_);
    }
}

void PipeWireCapture::on_param_changed(void* userdata,
                                       uint32_t id,
                                       const struct spa_pod* param) {
    auto* self = static_cast<PipeWireCapture*>(userdata);
    if (!self || !param || id != SPA_PARAM_Format || !self->stream_) return;

    uint32_t media_type = 0, media_subtype = 0;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) return;
    if (media_type != SPA_MEDIA_TYPE_video || media_subtype != SPA_MEDIA_SUBTYPE_raw) return;

    spa_video_info_raw info {};
    if (spa_format_video_raw_parse(param, &info) < 0) return;

    self->stream_width_ = static_cast<int>(info.size.width);
    self->stream_height_ = static_cast<int>(info.size.height);
    self->stream_format_ = info.format;

    int bpp = bytes_per_pixel(self->stream_format_);
    if (self->stream_width_ <= 0 || self->stream_height_ <= 0 || bpp == 0) {
        self->log_status("Unsupported screencast format negotiated");
        return;
    }

    uint8_t buffer[512];
    spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const spa_pod* params[1];
    params[0] = reinterpret_cast<spa_pod*>(spa_pod_builder_add_object(
        &b,
        SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(8, 2, 16),
        SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
        SPA_PARAM_BUFFERS_size, SPA_POD_Int(self->stream_width_ * self->stream_height_ * bpp),
        SPA_PARAM_BUFFERS_stride, SPA_POD_Int(self->stream_width_ * bpp),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int(
            (1 << SPA_DATA_MemPtr) | (1 << SPA_DATA_MemFd)
        )
    ));
    pw_stream_update_params(self->stream_, params, 1);

    self->log_status(
        "Negotiated stream format " + std::to_string(self->stream_width_) + "x" +
        std::to_string(self->stream_height_)
    );
}

void PipeWireCapture::on_process(void* userdata) {
    auto* self = static_cast<PipeWireCapture*>(userdata);
    if (!self || !self->stream_ || !self->running_.load()) return;

    pw_buffer* buffer = pw_stream_dequeue_buffer(self->stream_);
    if (!buffer || !buffer->buffer || buffer->buffer->n_datas == 0) return;

    spa_buffer* spa_buf = buffer->buffer;
    spa_data& data = spa_buf->datas[0];
    spa_chunk* chunk = data.chunk;

    const int width = self->stream_width_;
    const int height = self->stream_height_;
    const uint32_t format = self->stream_format_;
    const int bpp = bytes_per_pixel(format);
    if (width <= 0 || height <= 0 || bpp == 0) {
        pw_stream_queue_buffer(self->stream_, buffer);
        return;
    }

    const double now = monotonic_now_sec();
    if (self->last_frame_ts_ > 0.0 &&
        (now - self->last_frame_ts_) < self->min_frame_interval_) {
        pw_stream_queue_buffer(self->stream_, buffer);
        return;
    }

    void* mapped = nullptr;
    uint8_t* base = static_cast<uint8_t*>(data.data);
    if (!base && data.fd >= 0) {
        mapped = mmap(nullptr, data.maxsize, PROT_READ, MAP_PRIVATE, data.fd, data.mapoffset);
        if (mapped != MAP_FAILED) {
            base = static_cast<uint8_t*>(mapped);
        } else {
            mapped = nullptr;
        }
    }

    if (!base) {
        pw_stream_queue_buffer(self->stream_, buffer);
        return;
    }

    const uint32_t offset = chunk ? chunk->offset : 0;
    const int stride = (chunk && chunk->stride > 0) ? chunk->stride : width * bpp;
    const uint8_t* src = base + offset;

    FrameSlot& slot = self->buffer_.begin_write();
    slot.timestamp_sec = now;
    slot.width = width;
    slot.height = height;

    uint8_t* dst = slot.rgb_data.data();

    // Optimized color conversion: pointer arithmetic + SSE2.
    // Pointer-based loops are ~2-3x faster than indexed access.
    // SSE2 processes 4 pixels (16 bytes) per iteration.
    for (int y = 0; y < height; ++y) {
        const uint8_t* src_row = src + static_cast<size_t>(y) * stride;
        uint8_t* dst_row = dst + static_cast<size_t>(y) * width * 3;

        const bool is_bgrx = (format == SPA_VIDEO_FORMAT_BGRx ||
                              format == SPA_VIDEO_FORMAT_BGRA);
        const bool is_rgBx = (format == SPA_VIDEO_FORMAT_RGBx ||
                              format == SPA_VIDEO_FORMAT_RGBA);

        if (is_bgrx || is_rgBx) {
            const int bulk = width & ~3;
            // Process 4 pixels at a time with SSE2
#if defined(__SSE2__) || (defined(_M_X64) && !defined(__clang__))
            const uint8_t* sp = src_row;
            const uint8_t* send = src_row + bulk * 4;
            uint8_t* dp = dst_row;
            for (; sp < send; sp += 16, dp += 12) {
                __m128i v = _mm_loadu_si128(reinterpret_cast<const __m128i*>(sp));
                // BGRx/BGRA memory bytes are [B,G,R,X/A]; RGBx/RGBA are [R,G,B,X/A].
                // _mm_extract_epi32 extracts at 32-bit lane boundaries
                // Lane 0 contains pixel 0 in the source byte order, lane 1 pixel 1, etc.
                uint32_t w0 = static_cast<uint32_t>(_mm_cvtsi128_si32(v));
                uint32_t w1 = static_cast<uint32_t>(_mm_extract_epi32(v, 1));
                uint32_t w2 = static_cast<uint32_t>(_mm_extract_epi32(v, 2));
                uint32_t w3 = static_cast<uint32_t>(_mm_extract_epi32(v, 3));
                if (is_bgrx) {
                    dp[0]  = static_cast<uint8_t>(w0 >> 16);
                    dp[1]  = static_cast<uint8_t>(w0 >> 8);
                    dp[2]  = static_cast<uint8_t>(w0);
                    dp[3]  = static_cast<uint8_t>(w1 >> 16);
                    dp[4]  = static_cast<uint8_t>(w1 >> 8);
                    dp[5]  = static_cast<uint8_t>(w1);
                    dp[6]  = static_cast<uint8_t>(w2 >> 16);
                    dp[7]  = static_cast<uint8_t>(w2 >> 8);
                    dp[8]  = static_cast<uint8_t>(w2);
                    dp[9]  = static_cast<uint8_t>(w3 >> 16);
                    dp[10] = static_cast<uint8_t>(w3 >> 8);
                    dp[11] = static_cast<uint8_t>(w3);
                } else {
                    dp[0]  = static_cast<uint8_t>(w0);
                    dp[1]  = static_cast<uint8_t>(w0 >> 8);
                    dp[2]  = static_cast<uint8_t>(w0 >> 16);
                    dp[3]  = static_cast<uint8_t>(w1);
                    dp[4]  = static_cast<uint8_t>(w1 >> 8);
                    dp[5]  = static_cast<uint8_t>(w1 >> 16);
                    dp[6]  = static_cast<uint8_t>(w2);
                    dp[7]  = static_cast<uint8_t>(w2 >> 8);
                    dp[8]  = static_cast<uint8_t>(w2 >> 16);
                    dp[9]  = static_cast<uint8_t>(w3);
                    dp[10] = static_cast<uint8_t>(w3 >> 8);
                    dp[11] = static_cast<uint8_t>(w3 >> 16);
                }
            }
            // Remainder: 0-3 pixels (simple pointer loop)
            for (int x = bulk; x < width; ++x) {
                const uint8_t* px = src_row + x * 4;
                if (is_bgrx) {
                    dst_row[x * 3 + 0] = px[2];
                    dst_row[x * 3 + 1] = px[1];
                    dst_row[x * 3 + 2] = px[0];
                } else {
                    dst_row[x * 3 + 0] = px[0];
                    dst_row[x * 3 + 1] = px[1];
                    dst_row[x * 3 + 2] = px[2];
                }
            }
#else
            // Pointer-based fallback (still much faster than indexed access)
            const uint8_t* sp = src_row;
            const uint8_t* send = sp + width * 4;
            uint8_t* dp = dst_row;
            if (is_bgrx) {
                for (; sp < send; sp += 4, dp += 3) {
                    dp[0] = sp[2];
                    dp[1] = sp[1];  // G
                    dp[2] = sp[0];
                }
            } else {
                for (; sp < send; sp += 4, dp += 3) {
                    dp[0] = sp[0];
                    dp[1] = sp[1];
                    dp[2] = sp[2];
                }
            }
#endif
        } else {
            // Unknown format: zero the row
            std::memset(dst_row, 0, static_cast<size_t>(width) * 3);
        }
    }
    self->buffer_.commit_write();
    self->last_frame_ts_ = now;

    if (mapped) {
        munmap(mapped, data.maxsize);
    }
    pw_stream_queue_buffer(self->stream_, buffer);
}

void PipeWireCapture::handle_frame() {
    // Frame handling is performed directly in on_process().
}

const char* PipeWireCapture::embedded_gjs_script() {
    return "";
}

}  // namespace cua
