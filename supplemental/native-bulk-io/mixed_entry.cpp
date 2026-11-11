// Private experiment entry: reuse the fixed e31 harness without changing it.
#if defined(__GNUC__) && !defined(__clang__)
// GCC11 diagnoses compiler-generated coroutine frames of single-TU anonymous types when included.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wsubobject-linkage"
#endif
#define main original_pipeline_main
#include "../../src/coro_pipeline.cpp"
#undef main
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic pop
#endif

namespace {
int mixedExercise(const Options& options)
{
    const auto& common = options.common;
    if (common.workers != 4 || common.concurrency != 32 || options.depth != 1 || options.synchronous) {
        throw std::invalid_argument("mixed requires four IO executors,32 connections,depth1,coroutine callers");
    }
    Runtime runtime(common.workers);
    for (int i = 0; i < 32; ++i) runtime.addConnection(common.timeout_ms);
    for (auto& connection : runtime.connections) connect(*connection, common.port);
    std::vector<std::string> payloads;
    for (unsigned lane = 0; lane < 32; ++lane) {
        payloads.push_back(payloadFor(lane < 24 ? 4096 : 1048576, lane));
    }
    std::vector<lab::Stats> stats(32);
    runPhase(runtime.connections, payloads, 1, false,
             lab::schedulePhase(lab::seconds(common.warmup)), stats);
    lab::Stats warmup;
    for (const auto& lane : stats) warmup.merge(lane);
    std::cerr << "WARMUP_COUNTERS {\"rpc_errors\":" << warmup.rpcErrors
              << ",\"mismatches\":" << warmup.mismatches << ",\"success\":" << warmup.successes << "}\n";
    lab::finishWarmup(stats);
    for (auto& connection : runtime.connections) connection->resetPeaks();
    stats.assign(32, lab::Stats{});
    const auto phase = lab::schedulePhase(lab::seconds(common.duration));
    runPhase(runtime.connections, payloads, 1, false, phase, stats);
    auto drainEnd = phase.end;
    for (const auto& lane : stats) drainEnd = std::max(drainEnd, lane.lastCompletion);
    const lab::Phase reportPhase{phase.start, drainEnd};
    const auto ticks = [](lab::Clock::time_point value) {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(value.time_since_epoch()).count();
    };
    std::cout << "{\"record\":\"mixed\",\"pipeline\":1,\"caller\":\"coroutine\",\"connections\":32"
              << ",\"tiny_lanes\":24,\"tiny_payload\":4096,\"bulk_lanes\":8,\"bulk_payload\":1048576"
              << ",\"phase_start_ns\":" << ticks(phase.start) << ",\"phase_end_ns\":" << ticks(phase.end)
              << ",\"common_drain_end_ns\":" << ticks(drainEnd) << ",\"peak_native\":[";
    for (std::size_t i = 0; i < runtime.connections.size(); ++i) {
        const auto& connection = *runtime.connections[i];
        if (connection.active != 0 || connection.peakLogical != 1 || connection.peakNative != 1) {
            throw std::runtime_error("mixed did not drain or did not exercise exactly one call per connection");
        }
        if (i) std::cout << ',';
        std::cout << connection.peakNative;
    }
    std::cout << "]}" << std::endl;
    auto tinyOptions = common;
    tinyOptions.payload = 4096;
    tinyOptions.concurrency = 24;
    auto bulkOptions = common;
    bulkOptions.payload = 1048576;
    bulkOptions.concurrency = 8;
    const std::vector<lab::Stats> tiny(stats.begin(), stats.begin() + 24);
    const std::vector<lab::Stats> bulk(stats.begin() + 24, stats.end());
    const int tinyResult = lab::report("coro_mixed_tiny", tinyOptions, reportPhase, tiny);
    const int bulkResult = lab::report("coro_mixed_bulk", bulkOptions, reportPhase, bulk);
    return tinyResult != 0 || bulkResult != 0 ? 2 : 0;
}
}  // namespace

int main(int argc, char** argv)
{
    try {
        bool mixed = false;
        std::vector<char*> forwarded;
        for (int i = 0; i < argc; ++i) {
            if (std::string_view(argv[i]) == "--mixed") mixed = true;
            else forwarded.push_back(argv[i]);
        }
        if (!mixed) return original_pipeline_main(argc, argv);
        const auto options = parse(static_cast<int>(forwarded.size()), forwarded.data());
        return options.common.server ? serve(options) : mixedExercise(options);
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
