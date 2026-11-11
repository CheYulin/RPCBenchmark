#include "caller_io_drain.h"
#include "lab.h"

#include <ylt/coro_rpc/coro_rpc_client.hpp>

#include <algorithm>
#include <charconv>
#include <condition_variable>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>
#include <sys/syscall.h>
#include <unistd.h>

namespace {
using async_simple::coro::Lazy;
using namespace std::chrono_literals;

std::string Echo(const std::string& payload) { return payload; }

std::size_t processEntries(const char* path)
{
    return static_cast<std::size_t>(std::distance(std::filesystem::directory_iterator(path),
                                                  std::filesystem::directory_iterator{}));
}

[[noreturn]] void failDrain(const char* reason)
{
    std::cerr << "CALLER_IO_DRAIN_FAILURE " << reason << std::endl;
    std::_Exit(4);
}

class Owner {
public:
    explicit Owner(int timeoutMs)
        : executor_(context_.get_executor()), client_(std::make_unique<coro_rpc::coro_rpc_client>(&executor_, config(timeoutMs))),
          timeoutMs_(timeoutMs), ownerThread_(std::this_thread::get_id()) {}
    ~Owner() { close(); }
    Owner(const Owner&) = delete;
    Owner& operator=(const Owner&) = delete;

    void connect(int port)
    {
        auto error = perform(client_->connect("127.0.0.1", std::to_string(port)), std::max(timeoutMs_, 1000));
        if (error) throw std::runtime_error("native caller IO connection failed");
    }

    void reconnect(int port)
    {
        checkThread();
        close();
        client_ = std::make_unique<coro_rpc::coro_rpc_client>(&executor_, config(timeoutMs_));
        connect(port);
    }

    lab::Result request(const std::string& payload) { return perform(requestOnce(payload), timeoutMs_); }
    unsigned peak() const { return peakNative_; }
    void resetPeak() { peakNative_ = 0; }

    void close() noexcept
    {
        if (!client_) return;
        checkThread();
        try {
            client_->close();
            drain();
            client_.reset();
            drain();
        } catch (...) { failDrain("exception while owner is retained"); }
    }

private:
    static coro_rpc::coro_rpc_client::config config(int timeoutMs)
    {
        coro_rpc::coro_rpc_client::config result;
        result.connect_timeout_duration = lab::milliseconds(std::max(timeoutMs, 1000));
        result.request_timeout_duration = lab::milliseconds(timeoutMs);
        return result;
    }
    void checkThread() const
    {
        if (std::this_thread::get_id() != ownerThread_) failDrain("foreign owner thread");
    }
    void drain()
    {
        const auto result = caller_io::drain(context_, lab::Clock::now() + 2s, [this] {
            return active_ == 0 && (!client_ || client_->get_pipeline_size() == 0);
        });
        if (result != caller_io::DrainResult::Idle) failDrain("natural idle not proven");
    }
    template <typename T>
    T perform(Lazy<T> task, int nativeBudgetMs)
    {
        checkThread();
        if (active_ != 0) failDrain("nested operation");
        struct State { bool done = false; std::optional<T> value; std::exception_ptr error; };
        auto state = std::make_shared<State>();
        if (context_.stopped()) context_.restart();
        ++active_;
        try {
            std::move(task).via(&executor_).start([this, state](async_simple::Try<T>&& result) {
                checkThread();
                if (!executor_.currentThreadInExecutor()) failDrain("completion outside owned executor");
                if (result.hasError()) state->error = result.getException();
                else state->value.emplace(std::move(result).value());
                --active_;
                state->done = true;
            });
        } catch (...) { --active_; throw; }
        const auto safetyDeadline = lab::Clock::now() + lab::milliseconds(nativeBudgetMs) + 2s;
        bool invalid = false;
        while (!state->done) {
            if (lab::Clock::now() >= safetyDeadline
                || (context_.run_one_until(safetyDeadline) == 0 && !state->done)) {
                invalid = true;
                break;
            }
        }
        if (invalid || lab::Clock::now() >= safetyDeadline) {
            client_->close();
            drain();
            failDrain("native operation did not finish within safety bound");
        }
        if (state->error) std::rethrow_exception(state->error);
        return std::move(*state->value);
    }
    Lazy<lab::Result> requestOnce(const std::string& payload)
    {
        auto result = lab::Result::RpcError;
        try {
            auto response = co_await client_->send_request<Echo>(payload);
            peakNative_ = std::max(peakNative_, client_->get_pipeline_size());
            auto reply = co_await std::move(response);
            result = !reply ? lab::Result::RpcError
                : reply->result() == payload ? lab::Result::Success : lab::Result::Mismatch;
        } catch (...) {}
        co_return result;
    }

    asio::io_context context_;
    coro_io::ExecutorWrapper<> executor_;
    std::unique_ptr<coro_rpc::coro_rpc_client> client_;
    int timeoutMs_;
    std::thread::id ownerThread_;
    unsigned active_ = 0;
    unsigned peakNative_ = 0;
};

struct Group {
    std::mutex mutex;
    std::condition_variable changed;
    unsigned ready = 0;
    unsigned completed = 0;
    unsigned generation = 0;
    bool stop = false;
    lab::Phase phase{};
    std::exception_ptr error;
};

std::string payloadFor(int bytes, unsigned lane)
{
    auto payload = lab::makePayload(bytes);
    for (unsigned i = 0; i < 4; ++i) payload[i] = static_cast<char>((lane >> (8 * i)) & 255);
    return payload;
}

void lane(const lab::Options& options, unsigned index, Group& group, lab::Stats& stats, unsigned& peak, long& tid)
{
    try {
        Owner owner(options.timeout_ms);
        owner.connect(options.port);
        const auto payload = payloadFor(options.payload, index);
        tid = syscall(SYS_gettid);
        std::unique_lock lock(group.mutex);
        ++group.ready;
        group.changed.notify_all();
        unsigned generation = 0;
        while (true) {
            group.changed.wait(lock, [&] { return group.stop || group.generation != generation; });
            if (group.stop) break;
            generation = group.generation;
            const auto phase = group.phase;
            lock.unlock();
            stats = lab::Stats{};
            owner.resetPeak();
            std::this_thread::sleep_until(phase.start);
            for (auto sent = lab::Clock::now(); sent < phase.end; sent = lab::Clock::now()) {
                const auto result = owner.request(payload);
                stats.record(result, sent, lab::Clock::now(), phase);
                if (result != lab::Result::Success) break;
            }
            peak = owner.peak();
            lock.lock();
            ++group.completed;
            group.changed.notify_all();
        }
        lock.unlock();
        owner.close();
    } catch (...) {
        std::lock_guard lock(group.mutex);
        if (!group.error) group.error = std::current_exception();
        group.changed.notify_all();
    }
}

int verify(const lab::Options& options, unsigned rounds, bool firstError)
{
    auto payload = payloadFor(options.payload, 0);
    std::size_t baselineFds = 0;
    std::size_t baselineThreads = 0;
    for (unsigned i = 0; i <= rounds; ++i) {
        {
            Owner owner(options.timeout_ms);
            owner.connect(options.port);
            const auto first = owner.request(payload);
            const auto expected = firstError && i == 0 ? lab::Result::RpcError : lab::Result::Success;
            if (first != expected) throw std::runtime_error("caller IO verification failed");
            owner.reconnect(options.port);
            if (owner.request(payload) != lab::Result::Success) throw std::runtime_error("caller IO reconnect failed");
            owner.close();
        }
        const auto fds = processEntries("/proc/self/fd");
        const auto threads = processEntries("/proc/self/task");
        if (i == 0) { baselineFds = fds; baselineThreads = threads; }
        else if (fds != baselineFds || threads != baselineThreads) throw std::runtime_error("caller IO FD/thread growth");
    }
    std::cout << "{\"verified\":true,\"rounds\":" << rounds << ",\"live_fds\":" << baselineFds
              << ",\"process_threads\":" << baselineThreads << ",\"cleanup\":\"natural_idle_twice\"}" << std::endl;
    return 0;
}

int exercise(const lab::Options& options)
{
    Group group;
    std::vector<lab::Stats> stats(options.concurrency);
    std::vector<unsigned> peaks(options.concurrency);
    std::vector<long> tids(options.concurrency);
    std::vector<std::thread> threads;
    auto stop = [&] {
        { std::lock_guard lock(group.mutex); group.stop = true; group.changed.notify_all(); }
        for (auto& thread : threads) if (thread.joinable()) thread.join();
    };
    try {
        for (int i = 0; i < options.concurrency; ++i) {
            threads.emplace_back(lane, std::cref(options), i, std::ref(group), std::ref(stats[i]), std::ref(peaks[i]), std::ref(tids[i]));
        }
        std::unique_lock lock(group.mutex);
        group.changed.wait(lock, [&] { return group.ready == threads.size() || group.error; });
        if (group.error) std::rethrow_exception(group.error);
        const auto readyThreads = processEntries("/proc/self/task");
        const auto readyFds = processEntries("/proc/self/fd");
        auto phase = [&](int seconds) {
            group.completed = 0;
            group.phase = lab::schedulePhase(lab::seconds(seconds));
            ++group.generation;
            group.changed.notify_all();
            group.changed.wait(lock, [&] { return group.completed == threads.size() || group.error; });
            if (group.error) std::rethrow_exception(group.error);
        };
        phase(options.warmup);
        lab::Stats warmup;
        for (const auto& entry : stats) warmup.merge(entry);
        std::cerr << "WARMUP_COUNTERS {\"rpc_errors\":" << warmup.rpcErrors << ",\"mismatches\":" << warmup.mismatches
                  << ",\"success\":" << warmup.successes << "}\n";
        lab::finishWarmup(stats);
        phase(options.duration);
        const auto measurement = group.phase;
        lock.unlock();
        stop();
        std::cout << "{\"record\":\"pipeline\",\"pipeline\":1,\"caller\":\"owned\",\"connections\":" << peaks.size()
                  << ",\"owner_threads\":" << peaks.size() << ",\"io_workers\":0,\"requested_workers\":" << options.workers
                  << ",\"fd_count_ready\":" << readyFds
                  << ",\"process_threads_ready\":" << readyThreads
                  << ",\"process_threads_after_join\":" << processEntries("/proc/self/task")
                  << ",\"route_id\":" << coro_rpc::func_id<Echo>()
                  << ",\"cleanup\":\"natural_idle_twice\",\"peak_native\":[";
        for (std::size_t i = 0; i < peaks.size(); ++i) { if (i) std::cout << ','; std::cout << peaks[i]; }
        std::cout << "],\"owner_tids\":[";
        for (std::size_t i = 0; i < tids.size(); ++i) { if (i) std::cout << ','; std::cout << tids[i]; }
        std::cout << "]}" << std::endl;
        auto reported = options;
        reported.workers = 0;
        return lab::report("coro_rpc_caller_io", reported, measurement, stats);
    } catch (...) { stop(); throw; }
}
}

int main(int argc, char** argv)
{
    try {
        unsigned rounds = 0;
        bool firstError = false;
        std::vector<char*> forwarded;
        for (int i = 0; i < argc; ++i) {
            if (std::string_view(argv[i]) == "--verify-rounds") {
                if (++i == argc) throw std::invalid_argument("missing verification rounds");
                const std::string_view value(argv[i]);
                const auto parsed = std::from_chars(value.data(), value.data() + value.size(), rounds);
                if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() || rounds == 0 || rounds > 1000)
                    throw std::invalid_argument("invalid verification rounds");
            } else if (std::string_view(argv[i]) == "--verify-first-error") {
                if (++i == argc || std::string_view(argv[i]) != "1") throw std::invalid_argument("invalid first-error flag");
                firstError = true;
            } else forwarded.push_back(argv[i]);
        }
        const auto options = lab::parse(static_cast<int>(forwarded.size()), forwarded.data());
        if (options.server || options.payload < 4) throw std::invalid_argument("caller IO requires client mode and payload >= 4");
        if (firstError && rounds == 0) throw std::invalid_argument("first-error requires verification mode");
        return rounds ? verify(options, rounds, firstError) : exercise(options);
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
