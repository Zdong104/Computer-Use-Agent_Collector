/**
 * @file ring_buffer.cpp
 * @brief Ring buffer implementation for frame storage.
 */

#include "ring_buffer.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <stdexcept>

namespace cua {

RingBuffer::RingBuffer(size_t capacity, int max_w, int max_h)
    : capacity_(capacity), max_w_(max_w), max_h_(max_h) {
    if (capacity == 0)
        throw std::invalid_argument("RingBuffer capacity must be > 0");
    if (max_w <= 0 || max_h <= 0)
        throw std::invalid_argument("RingBuffer max dimensions must be > 0");

    slots_.resize(capacity);
    for (auto& slot : slots_) {
        slot.allocate(max_w, max_h);
    }
}

FrameSlot& RingBuffer::begin_write() {
    mu_.lock();  // exclusive lock
    write_locked_ = true;
    return slots_[write_pos_];
}

void RingBuffer::commit_write() {
    assert(write_locked_ && "commit_write called without begin_write");

    auto& slot = slots_[write_pos_];
    slot.frame_id = next_frame_id_++;
    slot.valid = true;

    write_pos_ = (write_pos_ + 1) % capacity_;
    write_locked_ = false;
    mu_.unlock();
}

bool RingBuffer::find_pre_frame(double target_ts, FrameSlot& out) const {
    std::shared_lock lock(mu_);

    const FrameSlot* best = nullptr;
    double best_ts = -1.0;

    for (const auto& slot : slots_) {
        if (!slot.valid) continue;
        if (slot.timestamp_sec <= target_ts) {
            if (slot.timestamp_sec > best_ts) {
                best_ts = slot.timestamp_sec;
                best = &slot;
            }
        }
    }

    if (!best) return false;

    // Deep copy the frame data
    out.frame_id = best->frame_id;
    out.timestamp_sec = best->timestamp_sec;
    out.width = best->width;
    out.height = best->height;
    out.valid = true;

    size_t data_size = static_cast<size_t>(best->width) * best->height * 3;
    out.rgb_data.resize(data_size);
    std::memcpy(out.rgb_data.data(), best->rgb_data.data(), data_size);

    return true;
}

bool RingBuffer::find_post_frame(double target_ts, FrameSlot& out) const {
    std::shared_lock lock(mu_);

    const FrameSlot* best = nullptr;
    double best_ts = 1e18;

    for (const auto& slot : slots_) {
        if (!slot.valid) continue;
        if (slot.timestamp_sec >= target_ts) {
            if (slot.timestamp_sec < best_ts) {
                best_ts = slot.timestamp_sec;
                best = &slot;
            }
        }
    }

    if (!best) return false;

    // Deep copy
    out.frame_id = best->frame_id;
    out.timestamp_sec = best->timestamp_sec;
    out.width = best->width;
    out.height = best->height;
    out.valid = true;

    size_t data_size = static_cast<size_t>(best->width) * best->height * 3;
    out.rgb_data.resize(data_size);
    std::memcpy(out.rgb_data.data(), best->rgb_data.data(), data_size);

    return true;
}

double RingBuffer::latest_timestamp() const {
    std::shared_lock lock(mu_);
    double latest = 0.0;
    for (const auto& slot : slots_) {
        if (slot.valid && slot.timestamp_sec > latest) {
            latest = slot.timestamp_sec;
        }
    }
    return latest;
}

uint64_t RingBuffer::total_frames_written() const {
    std::shared_lock lock(mu_);
    return next_frame_id_ - 1;
}

}  // namespace cua
