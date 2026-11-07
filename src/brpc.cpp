#include "lab.h"

#include <brpc/channel.h>
#include <brpc/closure_guard.h>
#include <brpc/server.h>
#include <bthread/bthread.h>

#include <iostream>
#include <memory>
#include <stdexcept>

#include "echo.pb.h"

namespace {

class EchoService final : public rpc_lab_wire::EchoService {
public:
    explicit EchoService(lab::microseconds duration) : duration_(duration) {}

    void Echo(google::protobuf::RpcController*, const rpc_lab_wire::EchoRequest* request,
              rpc_lab_wire::EchoResponse* response, google::protobuf::Closure* done) override
    {
        brpc::ClosureGuard completed(done);
        lab::handlerWork(duration_);
        response->set_payload(request->payload());
    }

private:
    const lab::microseconds duration_;
};

int serve(const lab::Options& options)
{
    lab::blockShutdownSignals();
    EchoService service(lab::microseconds(options.handler_us));
    brpc::Server server;
    if (server.AddService(&service, brpc::SERVER_DOESNT_OWN_SERVICE) != 0) {
        throw std::runtime_error("cannot register bRPC Echo service");
    }

    brpc::ServerOptions settings;
    settings.num_threads = options.workers;
    settings.has_builtin_services = false;
    const auto address = "127.0.0.1:" + std::to_string(options.port);
    if (server.Start(address.c_str(), &settings) != 0) {
        throw std::runtime_error("cannot listen on loopback");
    }
    lab::ready();
    lab::waitForShutdown();
    server.Stop(0);
    server.Join();
    return 0;
}

struct Lane {
    brpc::Channel channel;
    rpc_lab_wire::EchoRequest request;
    const std::string* payload;
    const lab::Phase* phase;
    lab::Stats* stats;
};

void* runLane(void* argument)
{
    auto& lane = *static_cast<Lane*>(argument);
    auto& phase = *lane.phase;
    auto& stats = *lane.stats;
    rpc_lab_wire::EchoService_Stub stub(&lane.channel);
    for (auto now = lab::Clock::now(); now < phase.start; now = lab::Clock::now()) {
        bthread_usleep(std::chrono::ceil<lab::microseconds>(phase.start - now).count());
    }

    for (auto sent = lab::Clock::now(); sent < phase.end; sent = lab::Clock::now()) {
        brpc::Controller controller;
        rpc_lab_wire::EchoResponse response;
        stub.Echo(&controller, &lane.request, &response, nullptr);
        const auto result = controller.Failed() ? lab::Result::RpcError
            : response.payload() == *lane.payload ? lab::Result::Success : lab::Result::Mismatch;
        stats.record(result, sent, lab::Clock::now(), phase);
    }
    return nullptr;
}

void runPhase(const std::vector<std::unique_ptr<Lane>>& lanes, const lab::Phase& phase, std::vector<lab::Stats>& stats)
{
    std::vector<bthread_t> running;
    running.reserve(lanes.size());
    for (std::size_t i = 0; i < lanes.size(); ++i) {
        auto& lane = *lanes[i];
        lane.phase = &phase;
        lane.stats = &stats[i];
        bthread_t thread{};
        if (bthread_start_background(&thread, &BTHREAD_ATTR_NORMAL, runLane, &lane) != 0) {
            break;
        }
        running.push_back(thread);
    }
    for (auto thread : running) {
        bthread_join(thread, nullptr);
    }
    if (running.size() != lanes.size()) {
        throw std::runtime_error("failed to start all bthread lanes");
    }
}

int exercise(const lab::Options& options)
{
    auto payload = lab::makePayload(options.payload);
    std::vector<std::unique_ptr<Lane>> lanes;
    lanes.reserve(options.concurrency);
    for (int i = 0; i < options.concurrency; ++i) {
        auto lane = std::make_unique<Lane>();
        brpc::ChannelOptions settings;
        settings.protocol = "baidu_std";
        settings.connection_type = "single";
        settings.connection_group = std::to_string(i);
        settings.max_retry = 0;
        settings.connect_timeout_ms = options.timeout_ms;
        settings.timeout_ms = options.timeout_ms;
        if (lane->channel.Init("127.0.0.1", options.port, &settings) != 0) {
            throw std::runtime_error("bRPC channel init failed");
        }
        lane->payload = &payload;
        lane->request.set_payload(payload);
        lanes.push_back(std::move(lane));
    }

    std::vector<lab::Stats> stats(lanes.size());
    runPhase(lanes, lab::schedulePhase(lab::seconds(options.warmup)), stats);
    lab::finishWarmup(stats);

    stats.assign(lanes.size(), lab::Stats{});
    const auto phase = lab::schedulePhase(lab::seconds(options.duration));
    runPhase(lanes, phase, stats);
    return lab::report("brpc", options, phase, stats);
}

}  // namespace

int main(int argc, char** argv)
{
    try {
        const auto options = lab::parse(argc, argv);
        if (options.workers < BTHREAD_MIN_CONCURRENCY) {
            throw std::invalid_argument("bRPC requires at least four workers");
        }
        if (bthread_setconcurrency(options.workers) != 0) {
            throw std::runtime_error("cannot configure bthread worker count");
        }
        if (options.handler_workers != 0) {
            throw std::invalid_argument("--handler-workers is supported by coro_rpc only");
        }
        return options.server ? serve(options) : exercise(options);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
