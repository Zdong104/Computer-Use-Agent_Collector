#pragma once
/**
 * @file ring_buffer.h
 * @brief Lock-protected fixed-capacity ring buffer for screen frames.
 *
 * Design:
 *   - Single writer (capture thread) calls begin_write() / commit_write().
 *   - Multiple readers (action worker) call find_pre_frame / find_post_frame.
 *   - Frames are stored as raw RGB bytes in pre-allocated vectors.
 *   - A shared_mutex allows concurrent reads while blocking writes.
 */

#include <atomic>
#include <cstdint>
#include <shared_mutex>
#include <vector>

namespace cua {

/// One slot in the ring buffer holding a single screenshot frame.
struct FrameSlot {
    uint64_t frame_id{0};       ///< Monotonic frame counter (0 = invalid)
    double   timestamp_sec{0};  ///< CLOCK_MONOTONIC seconds
    int      width{0};
    int      height{0};
    std::vector<uint8_t> rgb_data;  ///< Raw RGB pixel data (w*h*3 bytes)
    bool     valid{false};      ///< True once first write committed

    /// Pre-allocate storage for max resolution
    void allocate(int max_w, int max_h) {
        rgb_data.resize(static_cast<size_t>(max_w) * max_h * 3);
    }
};

class RingBuffer {
public:
    /**
     * @param capacity  Number of frame slots (e.g. 10 for 1 second at 10 FPS)
     * @param max_w     Maximum frame width (pixels)
     * @param max_h     Maximum frame height (pixels)
     */
    RingBuffer(size_t capacity, int max_w, int max_h);

    /// @return Number of slots
    size_t capacity() const { return capacity_; }

    /// @return Max frame dimensions
    int max_width() const { return max_w_; }
    int max_height() const { return max_h_; }

    // ── Writer interface (capture thread only) ──────────────

    /**
     * Get a reference to the next slot to fill.
     * The caller should write frame data into rgb_data, then call commit_write().
     * Holds exclusive lock until commit_write().
     */
    FrameSlot& begin_write();

    /**
     * Mark the current write slot as valid and advance the head pointer.
     * Releases the exclusive lock acquired by begin_write().
     */
    void commit_write();

    // ── Reader interface (action worker thread) ─────────────

    /**
     * Find the latest frame with timestamp <= target_ts.
     * @param target_ts  Target timestamp (CLOCK_MONOTONIC seconds)
     * @param out        Output frame (deep copy of RGB data)
     * @return true if a valid frame was found
     */
    bool find_pre_frame(double target_ts, FrameSlot& out) const;

    /**
     * Find the earliest frame with timestamp >= target_ts.
     * @param target_ts  Target timestamp
     * @param out        Output frame (deep copy)
     * @return true if a valid frame was found
     */
    bool find_post_frame(double target_ts, FrameSlot& out) const;

    /**
     * Get the timestamp of the latest committed frame.
     * @return timestamp, or 0.0 if no frames written yet
     */
    double latest_timestamp() const;

    /**
     * Get total number of frames written since construction.
     */
    uint64_t total_frames_written() const;

private:
    size_t capacity_;
    int    max_w_, max_h_;

    std::vector<FrameSlot> slots_;
    uint64_t next_frame_id_{1};  ///< Next frame ID to assign
    size_t   write_pos_{0};      ///< Current write position in slots_

    mutable std::shared_mutex mu_;
    bool write_locked_{false};   ///< Debug: track begin/commit pairs
};

}  // namespace cua
