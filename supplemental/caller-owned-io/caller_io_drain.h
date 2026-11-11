#pragma once

#include <chrono>

namespace caller_io {
enum class DrainResult { Idle, PendingRoots, Deadline };

template <typename Context, typename Ready>
DrainResult drain(Context& context, std::chrono::steady_clock::time_point deadline, Ready ready)
{
    if (context.stopped()) context.restart();
    while (std::chrono::steady_clock::now() < deadline) {
        if (context.run_one_until(deadline) != 0) continue;
        if (context.stopped()) return ready() ? DrainResult::Idle : DrainResult::PendingRoots;
        return DrainResult::Deadline;
    }
    return DrainResult::Deadline;
}
}
