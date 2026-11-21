#ifndef DS_PRIVATE_REQUEST_MARKER_H
#define DS_PRIVATE_REQUEST_MARKER_H

#include <atomic>
#include <cstdint>

namespace datasystem::coro_unary::private_diagnostic {
constexpr std::uint32_t kRequestIdCeiling = 65535;
constexpr std::uint32_t kSubmission = 1;
constexpr std::uint32_t kLambdaEntry = 2;
constexpr std::uint32_t kReplyReady = 3;
static_assert(std::atomic<std::uint64_t>::is_always_lock_free);

inline std::uint32_t NextRequestId(std::atomic<std::uint64_t> &issued, std::uint32_t ceiling) noexcept
{
    auto current = issued.load(std::memory_order_relaxed);
    while (current < ceiling) {
        if (issued.compare_exchange_strong(current, current + 1, std::memory_order_relaxed)) {
            return static_cast<std::uint32_t>(current + 1);
        }
    }
    return 0;
}
}  // namespace datasystem::coro_unary::private_diagnostic

#endif
