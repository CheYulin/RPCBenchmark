#pragma once
#include <cstdint>
#include <vector>

// Extracted sdk_backend.cpp Benchmark control flow: read the next start after completing the RPC.
inline std::vector<std::int64_t> oldCompletionTimes(std::int64_t serviceNs)
{
    struct FakeClock {
        std::int64_t now() const { return time; }
        void rpc(std::int64_t service) { time += service; }
        std::int64_t time = 0;
    } clock;
    const std::int64_t measureEnd = 1000000000;
    std::vector<std::int64_t> times;
    for (auto start = clock.now(); start < measureEnd; start = clock.now()) {
        clock.rpc(serviceNs);
        times.push_back(start);
    }
    return times;
}
