#include <asio/thread_pool.hpp>
#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <ylt/coro_rpc/coro_rpc_server.hpp>

#include <chrono>
#include <cstdint>
#include <future>
#include <iostream>
#include <memory>
#include <semaphore>
#include <string>
#include <string_view>
#include <sys/syscall.h>
#include <unistd.h>
#include <utility>

namespace {

using namespace std::chrono_literals;
using async_simple::coro::syncAwait;
using Clock = std::chrono::steady_clock;

constexpr auto observationWindow = 100ms;
constexpr auto startupTimeout = 3s;
constexpr auto gateTimeout = 5s;
constexpr auto requestTimeout = 8s;

std::int64_t ThreadId() {
    return ::syscall(SYS_gettid);
}

struct ThreadMark {
    std::int64_t io;
    std::int64_t handler;
};

struct Service {
    std::promise<ThreadMark> entered;
    std::binary_semaphore release{0};
    asio::thread_pool* worker = nullptr;

    std::int64_t Fast() {
        return ThreadId();
    }

    bool SlowDirect() {
        return Block(ThreadId());
    }

    async_simple::coro::Lazy<bool> SlowOffload() {
        const auto io = ThreadId();
        auto done = co_await coro_io::post([this, io] { return Block(io); },
                                          worker->get_executor());
        co_return std::move(done).value();
    }

    bool Block(std::int64_t io) {
        entered.set_value({io, ThreadId()});
        return release.try_acquire_for(gateTimeout);
    }
};

int Run(bool offload) {
    Service service;
    auto entered = service.entered.get_future();
    std::unique_ptr<asio::thread_pool> worker;
    if (offload) {
        worker = std::make_unique<asio::thread_pool>(1);
        service.worker = worker.get();
    }
    coro_rpc::coro_rpc_server server(1, 0, "127.0.0.1");
    server.register_handler<&Service::Fast>(&service);
    if (offload) {
        server.register_handler<&Service::SlowOffload>(&service);
    } else {
        server.register_handler<&Service::SlowDirect>(&service);
    }
    auto serverLifetime = server.async_start();
    if (server.port() == 0) {
        std::cerr << "Could not bind loopback listener\n";
        return 1;
    }

    auto& clientIo = coro_io::g_io_context_pool(2);
    coro_rpc::coro_rpc_client::config config;
    config.connect_timeout_duration = startupTimeout;
    config.request_timeout_duration = requestTimeout;
    coro_rpc::coro_rpc_client slowClient(clientIo.get_executor(), config);
    coro_rpc::coro_rpc_client fastClient(clientIo.get_executor(), config);
    const std::string address = "127.0.0.1:" + std::to_string(server.port());
    if (syncAwait(slowClient.connect(address)) ||
        syncAwait(fastClient.connect(address)) ||
        !syncAwait(slowClient.call<&Service::Fast>()) ||
        !syncAwait(fastClient.call<&Service::Fast>())) {
        std::cerr << "Could not warm both connections\n";
        return 1;
    }

    auto slow = std::async(std::launch::async, [&] {
        return offload ? syncAwait(slowClient.call<&Service::SlowOffload>())
                       : syncAwait(slowClient.call<&Service::SlowDirect>());
    });
    if (entered.wait_for(startupTimeout) != std::future_status::ready) {
        service.release.release();
        std::cerr << "Slow handler did not reach the gate\n";
        return 1;
    }
    const auto marks = entered.get();

    std::promise<void> attemptingFast;
    auto fastStarted = attemptingFast.get_future();
    auto fast = std::async(std::launch::async, [&] {
        const auto start = Clock::now();
        attemptingFast.set_value();
        auto response = syncAwait(fastClient.call<&Service::Fast>());
        auto latency = std::chrono::duration_cast<std::chrono::microseconds>(
            Clock::now() - start);
        return std::pair{std::move(response), latency};
    });
    if (fastStarted.wait_for(startupTimeout) != std::future_status::ready) {
        service.release.release();
        std::cerr << "Fast caller did not start\n";
        return 1;
    }
    const bool fastBeforeRelease =
        fast.wait_for(observationWindow) == std::future_status::ready;
    const bool slowPendingBeforeRelease =
        slow.wait_for(0ms) != std::future_status::ready;
    service.release.release();
    const auto [fastReply, fastLatency] = fast.get();
    const auto slowReply = slow.get();
    server.stop();

    const auto fastTid = fastReply ? fastReply.value() : 0;
    const bool bothSucceeded = fastReply && slowReply && slowReply.value();
    const bool workerPlacement = offload ? marks.io != marks.handler
                                         : marks.io == marks.handler;
    const bool passed = bothSucceeded && slowPendingBeforeRelease &&
                        fastTid == marks.io && workerPlacement &&
                        fastBeforeRelease == offload;
    std::cout << std::boolalpha
              << "{\"mode\":\"" << (offload ? "offload" : "direct")
              << "\",\"observation_window_ms\":" << observationWindow.count()
              << ",\"io_tid\":" << marks.io
              << ",\"slow_handler_tid\":" << marks.handler
              << ",\"fast_handler_tid\":" << fastTid
              << ",\"fast_before_release\":" << fastBeforeRelease
              << ",\"slow_pending_before_release\":" << slowPendingBeforeRelease
              << ",\"fast_rpc_us\":" << fastLatency.count()
              << ",\"both_succeeded\":" << bothSucceeded
              << ",\"passed\":" << passed << "}\n";
    return passed ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2 || (std::string_view(argv[1]) != "direct" &&
                      std::string_view(argv[1]) != "offload")) {
        std::cerr << "Usage: coro-thread-probe direct|offload\n";
        return 2;
    }
    return Run(std::string_view(argv[1]) == "offload");
}
