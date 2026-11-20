#pragma once
#include "arrival_loop.h"
#include <utility>

namespace owner8_queued {
inline constexpr std::uint64_t OwnerPeriodNs = 16'000'000;
inline constexpr std::uint64_t SoftLatenessNs = 1'000'000;
inline constexpr std::uint64_t SoftBudgetDenominator = 1000;
inline constexpr const char *Protocol = "fixed-owner8-queued-arrival-v1";

struct Row : fixed_arrival::Row {
    std::uint64_t head_enter_ns = 0, kv_call_begin_ns = 0;
};

inline bool SoftBudgetPassed(std::uint64_t count, std::uint64_t planned) noexcept
{
    return planned && count <= planned / SoftBudgetDenominator;
}

inline bool ObserverWithinPeriod(const Row &row) noexcept
{
    return row.observer_ns >= row.scheduled_ns && row.observer_ns - row.scheduled_ns < OwnerPeriodNs;
}

template<class Clock, class Call>
void Execute(std::span<Row> rows, std::atomic<bool> &abort, Clock &clock, Call &&call)
{
    static_assert(noexcept(clock.NowNs()));
    using fixed_arrival::State;
    for (auto &row : rows) {
        if (abort.load(std::memory_order_acquire)) {
            row.state = State::Cancelled;
            continue;
        }
        try {
            row.head_enter_ns = clock.NowNs();
            clock.SleepUntil(row.scheduled_ns);
            if (abort.load(std::memory_order_acquire)) {
                row.state = State::Cancelled;
                continue;
            }
            row.begin_ns = clock.NowNs();
            if (row.begin_ns < row.scheduled_ns) {
                row.state = State::RejectedEarly;
                abort.store(true, std::memory_order_release);
                continue;
            }
            if (row.head_enter_ns > row.begin_ns) {
                row.state = State::InvalidTime;
                abort.store(true, std::memory_order_release);
                continue;
            }
            row.state = State::InFlight;
            call(row);
            const bool ordered = row.begin_ns <= row.kv_call_begin_ns && row.kv_call_begin_ns <= row.end_ns
                                 && row.end_ns <= row.validation_ns && row.validation_ns <= row.observer_ns;
            row.state = ordered ? State::Returned : State::InvalidTime;
            if (!ordered || row.status != 0 || row.mismatch) {
                abort.store(true, std::memory_order_release);
            }
        } catch (...) {
            row.state = State::Exception;
            row.status = fixed_arrival::CallbackFailure;
            row.end_ns = clock.NowNs();
            row.validation_ns = row.end_ns;
            row.observer_ns = row.end_ns;
            abort.store(true, std::memory_order_release);
        }
    }
}
} // namespace owner8_queued
