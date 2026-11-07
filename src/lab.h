#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace lab {

using Clock = std::chrono::steady_clock;
using std::chrono::milliseconds;
using std::chrono::microseconds;
using std::chrono::seconds;

struct Options {
    bool server;
    int port = 18081;
    int workers = 4;
    int concurrency = 1;
    int payload = 64;
    int warmup = 1;
    int duration = 3;
    int timeout_ms = 1000;
    int handler_us = 0;
    int handler_workers = 0;
};

struct Phase {
    Clock::time_point start;
    Clock::time_point end;
};

enum class Result { Success, RpcError, Mismatch };

struct Stats {
    void record(Result result, Clock::time_point sent, Clock::time_point received, const Phase& phase);
    void merge(const Stats& lane);
    std::uint64_t quantile(double fraction) const;

    static constexpr std::size_t fineBuckets = 1024;
    static constexpr std::size_t totalBuckets = fineBuckets + 54;

    std::array<std::uint64_t, totalBuckets> histogram{};
    std::uint64_t attempts = 0;
    std::uint64_t rpcErrors = 0;
    std::uint64_t mismatches = 0;
    std::uint64_t successes = 0;
    std::uint64_t drained = 0;
    std::uint64_t maximumNs = 0;
    double totalNs = 0;
    Clock::time_point lastCompletion{};
};

Options parse(int argc, char** argv);
void ready();
void blockShutdownSignals();
void waitForShutdown();
void handlerWork(microseconds duration);
std::string makePayload(int bytes);
Phase schedulePhase(seconds length);
void finishWarmup(const std::vector<Stats>& lanes);
int report(std::string_view framework, const Options& options, const Phase& phase, const std::vector<Stats>& lanes);

}  // namespace lab
