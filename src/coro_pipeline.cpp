#include "lab.h"

#include <async_simple/coro/SyncAwait.h>
#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <ylt/coro_rpc/coro_rpc_server.hpp>

#include <algorithm>
#include <charconv>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <unordered_map>

namespace {
using async_simple::coro::Lazy;
using Context = coro_rpc::context<std::string>;

struct Options {
    lab::Options common;
    int depth = 1;
    bool corrupt = false;
    bool synchronous = false;
};

int integer(std::string_view value)
{
    int result{};
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) {
        throw std::invalid_argument("invalid pipeline option");
    }
    return result;
}

Options parse(int argc, char** argv)
{
    int depth = 1;
    bool corrupt = false;
    bool synchronous = false;
    std::vector<char*> forwarded;
    for (int i = 0; i < argc; ++i) {
        const std::string_view option(argv[i]);
        if (option == "--caller") {
            if (++i == argc) throw std::invalid_argument("missing caller mode");
            const std::string_view mode(argv[i]);
            if (mode != "sync" && mode != "coroutine") throw std::invalid_argument("invalid caller mode");
            synchronous = mode == "sync";
        } else if (option == "--pipeline" || option == "--corrupt-response") {
            if (++i == argc) throw std::invalid_argument("missing pipeline option value");
            const auto value = integer(argv[i]);
            if (option == "--pipeline") depth = value;
            else {
                if (value != 0 && value != 1) throw std::invalid_argument("invalid corruption flag");
                corrupt = value != 0;
            }
        } else {
            forwarded.push_back(argv[i]);
        }
    }
    Options result{lab::parse(static_cast<int>(forwarded.size()), forwarded.data()), depth, corrupt, synchronous};
    if (depth != 1 && depth != 8 && depth != 16) throw std::invalid_argument("pipeline must be 1, 8 or 16");
    if (result.common.handler_workers != 0) throw std::invalid_argument("pipeline experiment uses native inline Echo");
    if (!result.common.server && (result.common.payload < 4 || result.common.concurrency % depth != 0 || corrupt)) {
        throw std::invalid_argument("client requires payload >= 4, divisible concurrency, and no server fault flags");
    }
    if (synchronous && (result.common.server || depth != 1)) throw std::invalid_argument("sync callers require client depth 1");
    return result;
}

lab::microseconds handlerDuration{};
bool corruptReplies = false;

std::string Echo(const std::string& payload)
{
    lab::handlerWork(handlerDuration);
    auto reply = payload;
    if (corruptReplies && !reply.empty()) reply.back() ^= 1;
    return reply;
}

int serve(const Options& options)
{
    lab::blockShutdownSignals();
    handlerDuration = lab::microseconds(options.common.handler_us);
    corruptReplies = options.corrupt;
    coro_rpc::coro_rpc_server server(options.common.workers, options.common.port, "127.0.0.1");
    server.register_handler<Echo>();
    auto completion = server.async_start();
    if (completion.hasResult()) throw std::runtime_error("pipeline server could not listen");
    lab::ready();
    lab::waitForShutdown();
    server.stop();
    std::move(completion).get();
    return 0;
}

struct Connection {
    Connection(coro_io::ExecutorWrapper<>* executor, int timeoutMs) : executor(executor), client(executor, config(timeoutMs)) {}
    static coro_rpc::coro_rpc_client::config config(int timeoutMs)
    {
        coro_rpc::coro_rpc_client::config result;
        result.connect_timeout_duration = lab::milliseconds(std::max(timeoutMs, 1000));
        result.request_timeout_duration = lab::milliseconds(timeoutMs);
        return result;
    }
    void resetPeaks() { peakLogical = peakNative = 0; }
    coro_io::ExecutorWrapper<>* executor;
    coro_rpc::coro_rpc_client client;
    // Request coroutines update counters on this executor; phase futures join before the control thread reads them.
    unsigned active = 0;
    unsigned peakLogical = 0;
    unsigned peakNative = 0;
};

struct Runtime {
    explicit Runtime(int workers) : pool(workers), runner([this] { pool.run(); }) {}
    ~Runtime()
    {
        // Join callbacks before destroying clients; keep executors alive through client destruction.
        for (auto& connection : connections) connection->client.close();
        pool.stop();
        runner.join();
        connections.clear();
    }
    Connection& addConnection(int timeoutMs)
    {
        connections.push_back(std::make_unique<Connection>(pool.get_executor(), timeoutMs));
        return *connections.back();
    }
    coro_io::io_context_pool pool;
    std::thread runner;
    std::vector<std::unique_ptr<Connection>> connections;
};

std::string payloadFor(int bytes, unsigned lane)
{
    auto payload = lab::makePayload(bytes);
    for (unsigned i = 0; i < 4; ++i) payload[i] = static_cast<char>((lane >> (8 * i)) & 255);
    return payload;
}

template <auto Route>
Lazy<lab::Result> requestOnce(Connection& connection, const std::string& payload)
{
    ++connection.active;
    connection.peakLogical = std::max(connection.peakLogical, connection.active);
    lab::Result result = lab::Result::RpcError;
    try {
        // The first await sends the frame; the returned Lazy waits for this request's response.
        auto response = co_await connection.client.send_request<Route>(payload);
        connection.peakNative = std::max(connection.peakNative, connection.client.get_pipeline_size());
        auto reply = co_await std::move(response);
        result = !reply ? lab::Result::RpcError
            : reply->result() == payload ? lab::Result::Success : lab::Result::Mismatch;
    } catch (...) {
        result = lab::Result::RpcError;
    }
    --connection.active;
    co_return result;
}

Lazy<void> runLane(Connection& connection, const std::string& payload, const lab::Phase& phase, lab::Stats& stats)
{
    for (auto now = lab::Clock::now(); now < phase.start; now = lab::Clock::now()) {
        co_await coro_io::sleep_for(phase.start - now, connection.executor);
    }
    for (auto sent = lab::Clock::now(); sent < phase.end; sent = lab::Clock::now()) {
        auto result = co_await requestOnce<Echo>(connection, payload);
        stats.record(result, sent, lab::Clock::now(), phase);
        if (result != lab::Result::Success) break;
    }
}

std::future<void> launch(Lazy<void> task, coro_io::ExecutorWrapper<>* executor)
{
    std::promise<void> completed;
    auto future = completed.get_future();
    std::move(task).via(executor).start([completed = std::move(completed)](async_simple::Try<void>&& result) mutable {
        if (result.hasError()) completed.set_exception(result.getException());
        else completed.set_value();
    });
    return future;
}

void awaitAll(std::vector<std::future<void>>& tasks, std::exception_ptr error = {})
{
    for (auto& task : tasks) {
        try { task.get(); }
        catch (...) { if (!error) error = std::current_exception(); }
    }
    if (error) std::rethrow_exception(error);
}

void runPhase(std::vector<std::unique_ptr<Connection>>& connections, const std::vector<std::string>& payloads,
              int depth, bool synchronous, const lab::Phase& phase, std::vector<lab::Stats>& stats)
{
    if (synchronous) {
        std::vector<std::thread> callers;
        callers.reserve(payloads.size());
        std::exception_ptr error;
        try {
            for (std::size_t i = 0; i < payloads.size(); ++i) {
                callers.emplace_back([&, i] {
                    auto& connection = *connections[i];
                    std::this_thread::sleep_until(phase.start);
                    for (auto sent = lab::Clock::now(); sent < phase.end; sent = lab::Clock::now()) {
                        auto result = lab::Result::RpcError;
                        try {
                            result = async_simple::coro::syncAwait(
                                requestOnce<Echo>(connection, payloads[i]).via(connection.executor));
                        } catch (...) {
                        }
                        stats[i].record(result, sent, lab::Clock::now(), phase);
                        if (result != lab::Result::Success) break;
                    }
                });
            }
        } catch (...) { error = std::current_exception(); }
        for (auto& caller : callers) caller.join();
        if (error) std::rethrow_exception(error);
        return;
    }
    std::vector<std::future<void>> tasks;
    tasks.reserve(payloads.size());
    std::exception_ptr error;
    try {
        for (std::size_t i = 0; i < payloads.size(); ++i) {
            auto& connection = *connections[i / depth];
            tasks.push_back(launch(runLane(connection, payloads[i], phase, stats[i]), connection.executor));
        }
    } catch (...) { error = std::current_exception(); }
    awaitAll(tasks, error);
}

void connect(Connection& connection, int port)
{
    auto result = async_simple::coro::syncAwait(
        connection.client.connect("127.0.0.1", std::to_string(port)).via(connection.executor));
    if (result) throw std::runtime_error("native pipeline connection failed");
}

int exercise(const Options& options)
{
    const auto& common = options.common;
    Runtime runtime(common.workers);
    for (int i = 0; i < common.concurrency / options.depth; ++i) runtime.addConnection(common.timeout_ms);
    auto& connections = runtime.connections;
    for (auto& connection : connections) connect(*connection, common.port);
    std::vector<std::string> payloads;
    for (int i = 0; i < common.concurrency; ++i) payloads.push_back(payloadFor(common.payload, i));
    std::vector<lab::Stats> stats(payloads.size());
    runPhase(connections, payloads, options.depth, options.synchronous,
             lab::schedulePhase(lab::seconds(common.warmup)), stats);
    lab::Stats warmup;
    for (const auto& lane : stats) warmup.merge(lane);
    std::cerr << "WARMUP_COUNTERS {\"rpc_errors\":" << warmup.rpcErrors
              << ",\"mismatches\":" << warmup.mismatches << ",\"success\":" << warmup.successes << "}\n";
    lab::finishWarmup(stats);
    for (auto& connection : connections) connection->resetPeaks();
    stats.assign(payloads.size(), lab::Stats{});
    const auto phase = lab::schedulePhase(lab::seconds(common.duration));
    runPhase(connections, payloads, options.depth, options.synchronous, phase, stats);
    std::cout << "{\"record\":\"pipeline\",\"pipeline\":" << options.depth
              << ",\"caller\":\"" << (options.synchronous ? "sync" : "coroutine") << '"'
              << ",\"connections\":" << connections.size() << ",\"peak_native\":[";
    for (std::size_t i = 0; i < connections.size(); ++i) {
        if (i) std::cout << ',';
        std::cout << connections[i]->peakNative;
        if (connections[i]->active != 0 || connections[i]->peakLogical > static_cast<unsigned>(options.depth)) {
            throw std::runtime_error("pipeline did not drain or exceeded its logical bound");
        }
    }
    std::cout << "]}" << std::endl;
    return lab::report("coro_rpc_pipeline", common, phase, stats);
}

class BarrierService {
public:
    explicit BarrierService(unsigned depth) : depth_(depth) {}
    void EchoAll(Context context, std::string payload)
    {
        std::vector<std::pair<Context, std::string>> ready;
        {
            std::lock_guard lock(mutex_);
            auto& pending = pending_[context.get_context_info()->get_connection_id()];
            pending.emplace_back(std::move(context), std::move(payload));
            if (pending.size() == depth_) ready.swap(pending);
        }
        for (auto it = ready.rbegin(); it != ready.rend(); ++it) it->first.response_msg(std::move(it->second));
    }
private:
    unsigned depth_;
    std::mutex mutex_;
    std::unordered_map<uint64_t, std::vector<std::pair<Context, std::string>>> pending_;
};

Lazy<void> verifyOnce(Connection& connection, const std::string& payload, lab::Result& result)
{
    result = co_await requestOnce<&BarrierService::EchoAll>(connection, payload);
}

int verify()
{
    unsigned requests = 0;
    for (unsigned depth : {1u, 8u, 16u}) {
        for (int bytes : {4, 4096}) {
            BarrierService service(depth);
            coro_rpc::coro_rpc_server server(4, 0, "127.0.0.1");
            server.register_handler<&BarrierService::EchoAll>(&service);
            auto completion = server.async_start();
            if (completion.hasResult()) throw std::runtime_error("barrier server failed");
            {
                Runtime runtime(4);
                auto& connection = runtime.addConnection(1000);
                connect(connection, server.port());
                std::vector<std::string> payloads;
                for (unsigned i = 0; i < depth; ++i) payloads.push_back(payloadFor(bytes, i + 1));
                for (int wave = 0; wave < 2; ++wave) {
                    std::vector<lab::Result> results(depth, lab::Result::RpcError);
                    std::vector<std::future<void>> tasks;
                    tasks.reserve(depth);
                    std::exception_ptr error;
                    try {
                        for (unsigned i = 0; i < depth; ++i) {
                            tasks.push_back(launch(verifyOnce(connection, payloads[i], results[i]), connection.executor));
                        }
                    } catch (...) { error = std::current_exception(); }
                    awaitAll(tasks, error);
                    for (auto result : results) {
                        if (result != lab::Result::Success) throw std::runtime_error("pipeline barrier/data validation failed");
                        ++requests;
                    }
                }
                if (connection.active != 0 || connection.peakLogical != depth || connection.peakNative != depth) {
                    throw std::runtime_error("native pipeline depth not reached");
                }
            }
            server.stop();
            std::move(completion).get();
        }
    }
    std::cout << "{\"verified\":true,\"requests\":" << requests << ",\"waves\":12}" << std::endl;
    return 0;
}
}  // namespace

int main(int argc, char** argv)
{
    try {
        if (argc == 2 && std::string_view(argv[1]) == "verify") return verify();
        const auto options = parse(argc, argv);
        return options.common.server ? serve(options) : exercise(options);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
