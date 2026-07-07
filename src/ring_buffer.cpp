/**
 * @file ring_buffer.cpp
 * @brief Lock-free ring buffer implementation.
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
    std::lock_guard lock(write_mu_);
    assert(!write_in_progress_ && "commit_write must be called before next begin_write");

    size_t pos = head_.load(std::memory_order_relaxed) % capacity_;
    write_in_progress_ = true;
    // Increment seq: marks slot as "being written" (odd value)
    slots_[pos].seq++;
    return slots_[pos];
}

void RingBuffer::commit_write() {
    std::lock_guard lock(write_mu_);
    assert(write_in_progress_ && "begin_write must be called before commit_write");

    auto& slot = slots_[head_.load(std::memory_order_relaxed) % capacity_];
    slot.frame_id = next_frame_id_++;
    slot.valid = true;
    // Increment seq: marks slot as "ready to read" (even value again)
    slot.seq++;

    // Advance head atomically so readers see the new slot
    head_.store(head_.load(std::memory_order_relaxed) + 1, std::memory_order_release);
    write_in_progress_ = false;
}

bool RingBuffer::find_pre_frame(double target_ts, FrameSlot& out) const {
    // LOCK-FREE: read head once, scan slots without any lock.
    // A slot is safe to read if: valid == true && seq is even.
    // (seq starts at 0, increments to 1 on begin_write, increments to 2 on commit.
    //  Even seq means commit has happened.)

    uint64_t head = head_.load(std::memory_order_acquire);
    uint64_t head_wrap = head % capacity_;  // position in ring

    const FrameSlot* best = nullptr;
    double best_ts = -1.0;

    // Scan all slots. A slot is valid if:
    //   1. valid == true
    //   2. seq is even (begin_write and commit both incremented it)
    //   3. timestamp <= target_ts
    for (size_t i = 0; i < capacity_; ++i) {
        const auto& slot = slots_[i];
        // Safe to read? Even seq means both begin_write and commit happened.
        // We check seq >= 2 as a safety net (0=initial, 1=mid-write, 2+=committed)
        if (slot.valid && (slot.seq >= 2 && (slot.seq % 2) == 0)) {
            if (slot.timestamp_sec <= target_ts && slot.timestamp_sec > best_ts) {
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
    // LOCK-FREE: same approach as find_pre_frame
    uint64_t head = head_.load(std::memory_order_acquire);

    const FrameSlot* best = nullptr;
    double best_ts = 1e18;

    for (size_t i = 0; i < capacity_; ++i) {
        const auto& slot = slots_[i];
        if (slot.valid && (slot.seq >= 2 && (slot.seq % 2) == 0)) {
            if (slot.timestamp_sec >= target_ts && slot.timestamp_sec < best_ts) {
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

bool RingBuffer::get_latest_frame(FrameSlot& out) const {
    // LOCK-FREE: scan for the committed frame with the highest timestamp.
    const FrameSlot* best = nullptr;
    double best_ts = -1.0;

    for (size_t i = 0; i < capacity_; ++i) {
        const auto& slot = slots_[i];
        if (slot.valid && (slot.seq >= 2 && (slot.seq % 2) == 0)) {
            if (slot.timestamp_sec > best_ts) {
                best_ts = slot.timestamp_sec;
                best = &slot;
            }
        }
    }

    if (!best) return false;

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
    // LOCK-FREE
    double latest = 0.0;
    for (size_t i = 0; i < capacity_; ++i) {
        const auto& slot = slots_[i];
        if (slot.valid && (slot.seq >= 2 && (slot.seq % 2) == 0) &&
            slot.timestamp_sec > latest) {
            latest = slot.timestamp_sec;
        }
    }
    return latest;
}

uint64_t RingBuffer::total_frames_written() const {
    return next_frame_id_ - 1;
}

}  // namespace cua
