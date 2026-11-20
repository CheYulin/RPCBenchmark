#include "native_notify_diagnostics.h"

#include <array>
#include <atomic>
#include <exception>

namespace {
std::array<std::atomic<std::uint64_t>, 17> g_batches{};
}

extern "C" void DSCoroDiagRecordNotifyFlush(unsigned successfulStarts) noexcept
{
    if (successfulStarts == 0 || successfulStarts >= g_batches.size()) {
        std::terminate();
    }
    g_batches[successfulStarts].fetch_add(1, std::memory_order_relaxed);
}

extern "C" NativeNotifySnapshot DSCoroDiagNativeNotifySnapshot() noexcept
{
    NativeNotifySnapshot result{};
    for (unsigned size = 1; size < g_batches.size(); ++size) {
        const auto count = g_batches[size].load(std::memory_order_relaxed);
        result.submission_histogram[size] = count;
        result.successful_nosignal_starts += size * count;
        result.nonempty_flush_calls += count;
        if (count != 0) {
            result.max_successful_starts_per_flush = size;
        }
    }
    return result;
}
