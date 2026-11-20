#pragma once

#include <cstdint>

struct NativeNotifySnapshot {
    std::uint64_t successful_nosignal_starts;
    std::uint64_t nonempty_flush_calls;
    std::uint64_t max_successful_starts_per_flush;
    std::uint64_t submission_histogram[17];
};

extern "C" void DSCoroDiagRecordNotifyFlush(unsigned successfulStarts) noexcept;
extern "C" NativeNotifySnapshot DSCoroDiagNativeNotifySnapshot() noexcept;
