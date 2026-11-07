#include "lab.h"

#include <algorithm>
#include <bit>
#include <charconv>
#include <cmath>
#include <csignal>
#include <iomanip>
#include <iostream>
#include <pthread.h>
#include <stdexcept>

namespace lab {
namespace {

constexpr auto startLead = milliseconds(30);

int parseInt(std::string_view name, std::string_view input, int min, int max)
{
    int value{};
    const auto result = std::from_chars(input.data(), input.data() + input.size(), value);
    if (result.ec != std::errc{} || result.ptr != input.data() + input.size() || value < min || value > max) {
        throw std::invalid_argument("invalid value for " + std::string(name));
    }
    return value;
}

sigset_t shutdownSignals()
{
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGINT);
    sigaddset(&set, SIGTERM);
    return set;
}

std::size_t histogramIndex(std::uint64_t us)
{
    if (us <= Stats::fineBuckets) {
        return us - 1;
    }
    return Stats::fineBuckets + std::bit_width(us - 1) - std::bit_width(Stats::fineBuckets);
}

std::uint64_t histogramBound(std::size_t index)
{
    if (index < Stats::fineBuckets) {
        return index + 1;
    }
    return std::uint64_t{1} << (index - Stats::fineBuckets + std::bit_width(Stats::fineBuckets));
}

std::uint64_t ceilUs(std::uint64_t ns)
{
    return std::max<std::uint64_t>(1, (ns + 999) / 1000);
}

}  // namespace

Options parse(int argc, char** argv)
{
    if (argc < 2 || (std::string_view(argv[1]) != "server" && std::string_view(argv[1]) != "client")) {
        throw std::invalid_argument(
            "usage: rpc_{brpc,coro} server --port N --workers N [--handler-us N] [--handler-workers N]\n"
            "       rpc_{brpc,coro} client --port N --workers N --concurrency N --payload N "
            "--warmup N --seconds N [--timeout-ms N]");
    }
    Options options{std::string_view(argv[1]) == "server"};
    for (int i = 2; i < argc; i += 2) {
        const std::string_view name(argv[i]);
        if (i + 1 == argc) {
            throw std::invalid_argument("missing value for " + std::string(name));
        }
        const std::string_view value(argv[i + 1]);
        if (name == "--port") {
            options.port = parseInt(name, value, 1, 65535);
        } else if (name == "--workers") {
            options.workers = parseInt(name, value, 1, 128);
        } else if (name == "--handler-us" && options.server) {
            options.handler_us = parseInt(name, value, 0, 10000);
        } else if (name == "--handler-workers" && options.server) {
            options.handler_workers = parseInt(name, value, 0, 128);
        } else if (name == "--concurrency" && !options.server) {
            options.concurrency = parseInt(name, value, 1, 1024);
        } else if (name == "--payload" && !options.server) {
            options.payload = parseInt(name, value, 0, 1024 * 1024);
        } else if (name == "--warmup" && !options.server) {
            options.warmup = parseInt(name, value, 1, 600);
        } else if (name == "--seconds" && !options.server) {
            options.duration = parseInt(name, value, 1, 600);
        } else if (name == "--timeout-ms" && !options.server) {
            options.timeout_ms = parseInt(name, value, 1, 10000);
        } else {
            throw std::invalid_argument("unknown option for this mode " + std::string(name));
        }
    }
    return options;
}

void ready()
{
    std::cout << "READY" << std::endl;
}

void blockShutdownSignals()
{
    auto signals = shutdownSignals();
    if (pthread_sigmask(SIG_BLOCK, &signals, nullptr) != 0) {
        throw std::runtime_error("pthread_sigmask failed");
    }
}

void waitForShutdown()
{
    auto signals = shutdownSignals();
    int received{};
    if (sigwait(&signals, &received) != 0) {
        throw std::runtime_error("sigwait failed");
    }
}

void handlerWork(microseconds duration)
{
    if (duration != microseconds::zero()) {
        const auto end = Clock::now() + duration;
        while (Clock::now() < end) {
        }
    }
}

std::string makePayload(int bytes)
{
    std::string data(bytes, '\0');
    for (std::size_t i = 0; i < data.size(); ++i) {
        data[i] = static_cast<char>(i % 251);
    }
    return data;
}

Phase schedulePhase(seconds length)
{
    const auto start = Clock::now() + startLead;
    return {start, start + length};
}

void Stats::record(Result result, Clock::time_point sent, Clock::time_point received, const Phase& phase)
{
    ++attempts;
    lastCompletion = received;
    drained += received >= phase.end;
    if (result == Result::RpcError) {
        ++rpcErrors;
    } else if (result == Result::Mismatch) {
        ++mismatches;
    } else {
        ++successes;
        const auto ns = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(received - sent).count());
        totalNs += static_cast<double>(ns);
        maximumNs = std::max(maximumNs, ns);
        ++histogram[histogramIndex(ceilUs(ns))];
    }
}

void Stats::merge(const Stats& lane)
{
    attempts += lane.attempts;
    rpcErrors += lane.rpcErrors;
    mismatches += lane.mismatches;
    successes += lane.successes;
    drained += lane.drained;
    maximumNs = std::max(maximumNs, lane.maximumNs);
    totalNs += lane.totalNs;
    lastCompletion = std::max(lastCompletion, lane.lastCompletion);
    for (std::size_t i = 0; i < histogram.size(); ++i) {
        histogram[i] += lane.histogram[i];
    }
}

std::uint64_t Stats::quantile(double fraction) const
{
    const auto rank = static_cast<std::uint64_t>(std::ceil(fraction * static_cast<double>(successes)));
    std::uint64_t found = 0;
    for (std::size_t i = 0; i < histogram.size(); ++i) {
        found += histogram[i];
        if (rank > 0 && found >= rank) {
            return std::min(ceilUs(maximumNs), histogramBound(i));
        }
    }
    return 0;
}

void finishWarmup(const std::vector<Stats>& lanes)
{
    Stats total;
    for (const auto& lane : lanes) {
        if (lane.successes == 0) {
            throw std::runtime_error("at least one lane had zero successful warmup requests");
        }
        total.merge(lane);
    }
    if (total.rpcErrors != 0 || total.mismatches != 0) {
        throw std::runtime_error("warmup had RPC errors or response mismatches");
    }
    std::cerr << "WARMUP_DONE successes=" << total.successes << std::endl;
}

int report(std::string_view framework, const Options& options, const Phase& phase, const std::vector<Stats>& lanes)
{
    Stats total;
    std::uint64_t activeLanes = 0;
    for (const auto& lane : lanes) {
        total.merge(lane);
        activeLanes += lane.attempts > 0;
    }
    const auto elapsed = std::chrono::duration<double>(std::max(phase.end, total.lastCompletion) - phase.start).count();
    const auto meanUs = total.successes == 0 ? 0.0 : total.totalNs / static_cast<double>(total.successes) / 1000.0;
    std::cout << std::fixed << std::setprecision(3)
              << "{\"framework\":\"" << framework << "\",\"workers\":" << options.workers
              << ",\"concurrency\":" << options.concurrency << ",\"payload\":" << options.payload
              << ",\"elapsed_s\":" << elapsed << ",\"attempts\":" << total.attempts
              << ",\"success\":" << total.successes << ",\"rpc_errors\":" << total.rpcErrors
              << ",\"mismatches\":" << total.mismatches << ",\"errors\":" << total.rpcErrors + total.mismatches
              << ",\"active_lanes\":" << activeLanes << ",\"drained\":" << total.drained
              << ",\"rps\":" << static_cast<double>(total.successes) / elapsed
              << ",\"samples\":" << total.successes << ",\"mean_us\":" << meanUs
              << ",\"p50_us\":" << total.quantile(0.50) << ",\"p95_us\":" << total.quantile(0.95)
              << ",\"p99_us\":" << total.quantile(0.99) << ",\"p999_us\":" << total.quantile(0.999)
              << ",\"max_us\":" << static_cast<double>(total.maximumNs) / 1000.0 << ",\"histogram\":[";
    bool first = true;
    for (std::size_t i = 0; i < total.histogram.size(); ++i) {
        if (total.histogram[i] == 0) {
            continue;
        }
        if (!first) {
            std::cout << ',';
        }
        first = false;
        std::cout << '[' << histogramBound(i) << ',' << total.histogram[i] << ']';
    }
    std::cout << "]}" << std::endl;
    return total.rpcErrors == 0 && total.mismatches == 0 && total.successes > 0
        && activeLanes == lanes.size() ? 0 : 2;
}

}  // namespace lab
