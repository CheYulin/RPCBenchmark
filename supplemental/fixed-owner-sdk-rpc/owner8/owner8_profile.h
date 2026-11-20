#pragma once
#include <cstddef>
#include <cstdint>

namespace owner8_profile {
inline constexpr std::uint32_t Callers = 8;
inline constexpr std::uint64_t Rate = 500;
inline constexpr std::uint64_t FormalWarmCalls = 512, FormalMeasureCalls = 4096;
inline constexpr std::uint64_t SmokeWarmCalls = 16, SmokeMeasureCalls = 32;
inline constexpr std::size_t KeyCount = 64, FrozenWriteHotKeys = 32;
inline constexpr std::uint64_t FrozenTraceOwners = 32;
inline constexpr std::uint64_t EarlyWakeNs = 1'000'000;
inline constexpr const char *PacingPolicy = "early_wake_then_spin_v1";

inline constexpr std::size_t SetKey(std::uint64_t id) noexcept
{
    return id % FrozenWriteHotKeys;
}

inline constexpr std::size_t GetKey(std::uint64_t id) noexcept
{
    return (id / FrozenTraceOwners + id % FrozenTraceOwners) % KeyCount;
}

inline constexpr bool IsSetHotKey(std::size_t key) noexcept
{
    return key < FrozenWriteHotKeys;
}

template<class Clock>
void WaitUntil(Clock &clock, std::uint64_t target)
{
    static_assert(noexcept(clock.NowNs()));
    if (clock.NowNs() >= target) {
        return;
    }
    const auto early = target > EarlyWakeNs ? target - EarlyWakeNs : 0;
    if (clock.NowNs() < early) {
        clock.SleepUntil(early);
    }
    while (clock.NowNs() < target) {
    }
}
} // namespace owner8_profile
