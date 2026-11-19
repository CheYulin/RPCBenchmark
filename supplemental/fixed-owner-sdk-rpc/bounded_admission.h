#pragma once
#include "arrival_loop.h"
#include <utility>

namespace bounded_admission {
inline constexpr std::uint64_t OwnerPeriodNs = 64'000'000;
inline constexpr std::uint64_t HardLatenessNs = OwnerPeriodNs - 1;
inline constexpr std::uint64_t SoftLatenessNs = 1'000'000;
inline constexpr std::uint64_t SoftBudgetDenominator = 1000;
inline constexpr const char *Protocol = "fixed-owner-bounded-admission-v1";

inline bool SoftBudgetPassed(std::uint64_t soft_late, std::uint64_t planned) noexcept
{
    return planned != 0 && soft_late <= planned / SoftBudgetDenominator;
}

inline bool ObserverWithinPeriod(const fixed_arrival::Row &row) noexcept
{
    return row.observer_ns >= row.scheduled_ns && row.observer_ns - row.scheduled_ns < OwnerPeriodNs;
}

template<class Clock, class Call>
void Execute(std::span<fixed_arrival::Row> rows, std::atomic<bool> &abort, Clock &clock, Call &&call)
{
    fixed_arrival::Execute(rows, abort, clock, HardLatenessNs, [&](fixed_arrival::Row &row) {
        call(row);
        if (!ObserverWithinPeriod(row)) abort.store(true, std::memory_order_release);
    });
}
} // namespace bounded_admission
