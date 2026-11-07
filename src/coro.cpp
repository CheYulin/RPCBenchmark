#include "lab.h"

#include <asio/thread_pool.hpp>
#include <async_simple/coro/Lazy.h>
#include <async_simple/coro/SyncAwait.h>
#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <ylt/coro_rpc/coro_rpc_server.hpp>

#include <future>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <thread>

namespace {

lab::microseconds handlerDuration{};

std::string Echo(const std::string& payload)
{
    lab::handlerWork(handlerDuration);
    return payload;
}

class PooledHandler {
public:
    explicit PooledHandler(int workers) : pool_(workers) {}
    ~PooledHandler() { pool_.join(); }

    async_simple::coro::Lazy<std::string> handle(std::string payload)
    {
        auto job = [payload = std::move(payload)] { return Echo(payload); };
        auto result = co_await coro_io::post(std::move(job), pool_.get_executor());
        co_return std::move(result).value();
    }

private:
    asio::thread_pool pool_;
};

int serve(const lab::Options& options)
{
    lab::blockShutdownSignals();
    handlerDuration = lab::microseconds(options.handler_us);
    std::unique_ptr<PooledHandler> handler;
    coro_rpc::coro_rpc_server server(options.workers, options.port, "127.0.0.1");
    if (options.handler_workers > 0) {
        handler = std::make_unique<PooledHandler>(options.handler_workers);
        server.register_handler<&PooledHandler::handle>(handler.get(), coro_rpc::func_id<Echo>());
    } else {
        server.register_handler<Echo>();
    }
    auto completion = server.async_start();
    if (completion.hasResult()) {
        const auto error = std::move(completion).get();
        throw std::runtime_error("cannot listen on loopback: " + std::string(error.message()));
    }
    lab::ready();
    lab::waitForShutdown();
    server.stop();
    std::move(completion).get();
    return 0;
}

struct ClientRuntime {
    explicit ClientRuntime(int workers) : pool(workers), runner([this] { pool.run(); }) {}

    ~ClientRuntime()
    {
        pool.stop();
        runner.join();
    }

    coro_io::io_context_pool pool;
    std::thread runner;
};

struct Lane {
    Lane(coro_io::ExecutorWrapper<>* executor, int timeoutMs) : executor(executor), client(executor, makeConfig(timeoutMs)) {}

    static coro_rpc::coro_rpc_client::config makeConfig(int timeoutMs)
    {
        coro_rpc::coro_rpc_client::config config;
        config.connect_timeout_duration = lab::milliseconds(timeoutMs);
        config.request_timeout_duration = lab::milliseconds(timeoutMs);
        return config;
    }

    coro_io::ExecutorWrapper<>* executor;
    coro_rpc::coro_rpc_client client;
};

async_simple::coro::Lazy<void> runLane(
    Lane& lane, const std::string& payload, const lab::Phase& phase, lab::Stats& stats)
{
    for (auto now = lab::Clock::now(); now < phase.start; now = lab::Clock::now()) {
        co_await coro_io::sleep_for(phase.start - now, lane.executor);
    }
    for (auto sent = lab::Clock::now(); sent < phase.end; sent = lab::Clock::now()) {
        auto reply = co_await lane.client.call<Echo>(payload);
        const auto result = !reply ? lab::Result::RpcError
            : reply.value() == payload ? lab::Result::Success : lab::Result::Mismatch;
        stats.record(result, sent, lab::Clock::now(), phase);
    }
}

void runPhase(const std::vector<std::unique_ptr<Lane>>& lanes, const std::string& payload,
              const lab::Phase& phase, std::vector<lab::Stats>& stats)
{
    std::vector<std::future<void>> running;
    running.reserve(lanes.size());
    std::exception_ptr firstError;
    try {
        for (std::size_t i = 0; i < lanes.size(); ++i) {
            auto& lane = *lanes[i];
            std::promise<void> completed;
            auto result = completed.get_future();
            runLane(lane, payload, phase, stats[i]).via(lane.executor).start(
                [completed = std::move(completed)](async_simple::Try<void>&& outcome) mutable {
                    if (outcome.hasError()) {
                        completed.set_exception(outcome.getException());
                    } else {
                        completed.set_value();
                    }
                });
            running.push_back(std::move(result));
        }
    } catch (...) {
        firstError = std::current_exception();
    }
    for (auto& result : running) {
        try {
            result.get();
        } catch (...) {
            if (!firstError) {
                firstError = std::current_exception();
            }
        }
    }
    if (firstError) {
        std::rethrow_exception(firstError);
    }
}

int exercise(const lab::Options& options)
{
    ClientRuntime runtime(options.workers);
    auto payload = lab::makePayload(options.payload);
    const auto port = std::to_string(options.port);
    std::vector<std::unique_ptr<Lane>> lanes;
    lanes.reserve(options.concurrency);
    for (int i = 0; i < options.concurrency; ++i) {
        auto lane = std::make_unique<Lane>(runtime.pool.get_executor(), options.timeout_ms);
        const auto error = async_simple::coro::syncAwait(lane->client.connect("127.0.0.1", port).via(lane->executor));
        if (error) {
            throw std::runtime_error("coro_rpc connect failed: " + std::string(error.message()));
        }
        lanes.push_back(std::move(lane));
    }

    std::vector<lab::Stats> stats(lanes.size());
    runPhase(lanes, payload, lab::schedulePhase(lab::seconds(options.warmup)), stats);
    lab::finishWarmup(stats);

    stats.assign(lanes.size(), lab::Stats{});
    const auto phase = lab::schedulePhase(lab::seconds(options.duration));
    runPhase(lanes, payload, phase, stats);
    return lab::report("coro_rpc", options, phase, stats);
}

}  // namespace

int main(int argc, char** argv)
{
    try {
        const auto options = lab::parse(argc, argv);
        return options.server ? serve(options) : exercise(options);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
