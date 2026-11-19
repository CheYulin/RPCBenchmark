#pragma once
#include <atomic>
#include <cstdint>
#include <limits>
#include <span>
namespace fixed_arrival {
enum class State : std::uint8_t {
    Planned, InFlight, Returned, RejectedEarly, RejectedLate, Cancelled, Exception, InvalidTime
};
inline constexpr auto CallbackFailure = std::numeric_limits<std::int32_t>::min();
struct Row {
    std::uint64_t id = 0, scheduled_ns = 0, begin_ns = 0, end_ns = 0;
    std::uint64_t validation_ns = 0, observer_ns = 0;
    std::int32_t status = 0;
    bool mismatch = false;
    State state = State::Planned;
};
template<class Clock, class Call>
void Execute(std::span<Row> rows, std::atomic<bool> &abort, Clock &clock,
             std::uint64_t lateness_limit_ns, Call &&call)
{
    static_assert(noexcept(clock.NowNs()));
    for (auto &row : rows) {
        if (abort.load(std::memory_order_acquire)) {
            row.state = State::Cancelled;
            continue;
        }
        try {
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
            if (row.begin_ns - row.scheduled_ns > lateness_limit_ns) {
                row.state = State::RejectedLate;
                abort.store(true, std::memory_order_release);
                continue;
            }
            row.state = State::InFlight;
            call(row);
            const bool ordered = row.begin_ns <= row.end_ns && row.end_ns <= row.validation_ns
                                 && row.validation_ns <= row.observer_ns;
            row.state = ordered ? State::Returned : State::InvalidTime;
            if (!ordered || row.status != 0 || row.mismatch) {
                abort.store(true, std::memory_order_release);
            }
        } catch (...) {
            row.state = State::Exception;
            row.status = CallbackFailure;
            row.end_ns = clock.NowNs();
            row.validation_ns = row.end_ns;
            row.observer_ns = row.end_ns;
            abort.store(true, std::memory_order_release);
        }
    }
}
} // namespace fixed_arrival
