#include "capture_helper.h"
#include "closed_loop_plan.h"
#include "datasystem/kv_client.h"

#include <algorithm>
#include <array>
#include <barrier>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
using datasystem::KVClient;
using namespace closed_loop;
constexpr std::size_t PayloadBytes = 4096, KeyCount = 64;
constexpr int ConnectTimeoutMs = 5000, RequestTimeoutMs = 2000;
constexpr std::uint32_t WarmPhase = 1, MeasurePhase = 2;
constexpr std::int32_t ByteMismatchOutcome = -1;

std::uint64_t NowNs()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
}

int PositiveInteger(const char *text, int maximum)
{
    int value = 0;
    if (!text) throw std::invalid_argument("required integer missing");
    const std::string_view input(text);
    const auto result = std::from_chars(input.data(), input.data() + input.size(), value);
    if (result.ec != std::errc{} || result.ptr != input.data() + input.size() || value <= 0 || value > maximum)
        throw std::invalid_argument("invalid positive integer");
    return value;
}

void RequireStatus(const datasystem::Status &status, const char *operation)
{
    if (!status.IsOk()) throw std::runtime_error(std::string(operation) + " status=" + std::to_string(status.GetCode()));
}

void Require(bool value, const char *message)
{
    if (!value) throw std::runtime_error(message);
}

struct CpuSample {
    std::uint64_t read_begin_ns = 0, read_end_ns = 0, sdk_ticks = 0, worker_ticks = 0;
};

std::uint64_t CpuTicks(int pid)
{
    std::ifstream stream("/proc/" + std::to_string(pid) + "/stat");
    std::string line;
    if (!std::getline(stream, line)) throw std::runtime_error("owned process CPU unavailable");
    const auto close = line.rfind(')');
    if (close == std::string::npos || close + 2 >= line.size()) throw std::runtime_error("invalid process stat");
    std::istringstream fields(line.substr(close + 2));
    std::string token;
    std::uint64_t user = 0, system = 0;
    for (unsigned index = 0; index <= 12; ++index) {
        if (!(fields >> token)) throw std::runtime_error("short process stat");
        if (index == 11) user = std::stoull(token);
        if (index == 12) system = std::stoull(token);
    }
    return user + system;
}

CpuSample ReadCpu(int workerPid)
{
    CpuSample result;
    result.read_begin_ns = NowNs();
    result.sdk_ticks = CpuTicks(getpid());
    result.worker_ticks = CpuTicks(workerPid);
    result.read_end_ns = NowNs();
    return result;
}

struct LedgerRow {
    std::uint64_t arrival = 0, begin = 0, end = 0, validated = 0, observed_end = 0;
    std::uint32_t phase = 0, caller = 0, tid = 0;
    std::int32_t status = 0;
    bool selected = false, mismatch = false;
};

struct PhaseStats {
    Histogram api, observed;
    std::uint64_t first = 0, last = 0, attempts = 0, successes = 0, errors = 0, mismatches = 0;
    std::uint64_t returned_bytes = 0, validated_bytes = 0, selected = 0, error_rows = 0;
};

struct Caller {
    std::uint32_t tid = 0;
    std::array<PhaseStats, 2> phases;
    std::vector<LedgerRow> ledger;
    std::size_t used = 0;
};

struct Aggregate {
    PhaseStats stats;
    std::uint64_t first_finish = std::numeric_limits<std::uint64_t>::max();
    CpuSample before, after;
};

class ClientShutdown {
public:
    explicit ClientShutdown(KVClient &client) : client_(client) {}
    ~ClientShutdown()
    {
        if (active_) {
            try { client_.ShutDown(); } catch (...) {}
        }
    }
    void Complete() { active_ = false; }
private:
    KVClient &client_;
    bool active_ = true;
};

[[noreturn]] void Fatal(benchmark_capture::Session *capture, const char *reason) noexcept
{
    if (capture) capture->Partial("driver-fatal-partial");
    std::cerr << "CLOSED_LOOP_DRIVER_FATAL " << reason << std::endl;
    std::_Exit(2);
}

void RunPhase(std::uint32_t callerIndex, std::uint32_t phase, bool write, KVClient &client,
              const std::array<std::string, KeyCount> &keys, const std::string &payload,
              Caller &caller, benchmark_capture::Session &capture)
{
    auto &stats = caller.phases[phase - 1];
    std::string actual;
    actual.reserve(PayloadBytes);
    const auto methods = write ? benchmark_capture::WriteMethods : benchmark_capture::ReadMethods;
    for (std::uint64_t ordinal = 0; ordinal < PhaseCalls(phase); ++ordinal) {
        const auto id = ArrivalId(phase, callerIndex, ordinal);
        const bool selected = Selected(phase, id);
        const auto begin = NowNs();
        if (!ordinal) stats.first = begin;
        capture.Begin(callerIndex, phase, id, begin, begin, methods);
        const auto status = write ? client.Set(keys[callerIndex], payload)
                                  : client.Get(keys[(ordinal + callerIndex) % KeyCount], actual);
        const auto end = NowNs();
        const bool mismatch = !write && status.IsOk() && actual != payload;
        const auto validated = NowNs();
        const auto statusCode = static_cast<std::int32_t>(status.GetCode());
        const auto outcome = mismatch && statusCode == 0 ? ByteMismatchOutcome : statusCode;
        capture.End(callerIndex, end, validated, outcome);
        const auto observedEnd = NowNs();
        Require(begin <= end && end <= validated && validated <= observedEnd, "clock order");
        stats.api.Record(end - begin);
        stats.observed.Record(observedEnd - begin);
        ++stats.attempts;
        stats.successes += status.IsOk() && !mismatch;
        stats.errors += !status.IsOk();
        stats.mismatches += mismatch;
        if (!write && status.IsOk()) {
            stats.returned_bytes += actual.size();
            if (!mismatch) stats.validated_bytes += actual.size();
        }
        stats.selected += selected;
        const bool failed = !status.IsOk() || mismatch;
        if (selected || failed) {
            Require(caller.used < caller.ledger.size(), "bounded ledger capacity exceeded");
            caller.ledger[caller.used++] = {id, begin, end, validated, observedEnd, phase, callerIndex,
                                            caller.tid, statusCode, selected, mismatch};
            stats.error_rows += failed;
        }
        stats.last = observedEnd;
    }
}

Aggregate Combine(const std::array<Caller, Callers> &callers, std::uint32_t phase)
{
    Aggregate aggregate;
    for (const auto &caller : callers) {
        const auto &source = caller.phases[phase - 1];
        auto &dest = aggregate.stats;
        dest.api.Merge(source.api);
        dest.observed.Merge(source.observed);
        dest.first = dest.first ? std::min(dest.first, source.first) : source.first;
        dest.last = std::max(dest.last, source.last);
        dest.attempts += source.attempts;
        dest.successes += source.successes;
        dest.errors += source.errors;
        dest.mismatches += source.mismatches;
        dest.returned_bytes += source.returned_bytes;
        dest.validated_bytes += source.validated_bytes;
        dest.selected += source.selected;
        dest.error_rows += source.error_rows;
        aggregate.first_finish = std::min(aggregate.first_finish, source.last);
        Require(source.attempts == PhaseCalls(phase), "caller attempt count");
    }
    const auto &s = aggregate.stats;
    Require(s.attempts == PhaseCalls(phase) * Callers && s.api.Count() == s.attempts
                && s.observed.Count() == s.attempts && s.successes + s.errors + s.mismatches == s.attempts,
            "full cohort or histogram count mismatch");
    Require(s.last > s.first && aggregate.first_finish >= s.first, "phase timing invalid");
    return aggregate;
}

void WriteHistogram(std::ostream &out, const Histogram &histogram)
{
    bool comma = false;
    out << '[';
    for (std::size_t bucket = 0; bucket < Histogram::Buckets; ++bucket) {
        const auto count = histogram.Bins()[bucket];
        if (!count) continue;
        if (comma) out << ',';
        comma = true;
        out << '[' << Histogram::BucketUpper(bucket) << ',' << count << ']';
    }
    out << ']';
}

void WriteCpuSample(std::ostream &out, const CpuSample &sample)
{
    out << "{\"read_begin_ns\":" << sample.read_begin_ns << ",\"read_end_ns\":" << sample.read_end_ns
        << ",\"sdk_ticks\":" << sample.sdk_ticks << ",\"worker_ticks\":" << sample.worker_ticks << '}';
}

void WritePhase(std::ostream &out, const Aggregate &aggregate, std::uint32_t phase, long clockTicks)
{
    const auto &s = aggregate.stats;
    Require(aggregate.after.sdk_ticks >= aggregate.before.sdk_ticks
                && aggregate.after.worker_ticks >= aggregate.before.worker_ticks, "CPU counters reversed");
    const auto sdkCpu = double(aggregate.after.sdk_ticks - aggregate.before.sdk_ticks) / clockTicks;
    const auto workerCpu = double(aggregate.after.worker_ticks - aggregate.before.worker_ticks) / clockTicks;
    const auto elapsed = double(s.last - s.first) / 1e9;
    out << "{\"phase_id\":" << phase << ",\"planned\":" << PhaseCalls(phase) * Callers
        << ",\"attempts\":" << s.attempts << ",\"success\":" << s.successes
        << ",\"errors\":" << s.errors << ",\"mismatches\":" << s.mismatches
        << ",\"returned_bytes\":" << s.returned_bytes << ",\"validated_bytes\":" << s.validated_bytes
        << ",\"selected_ledger_rows\":" << s.selected << ",\"error_ledger_rows\":" << s.error_rows
        << ",\"first_api_begin_ns\":" << s.first << ",\"first_caller_finish_ns\":" << aggregate.first_finish
        << ",\"last_caller_finish_ns\":" << s.last << ",\"taper_ns\":" << s.last - aggregate.first_finish
        << ",\"elapsed_s\":" << elapsed << ",\"qps\":" << s.attempts / elapsed
        << ",\"api_p50_upper_us\":" << s.api.Upper(50) << ",\"api_p99_upper_us\":" << s.api.Upper(99)
        << ",\"observer_p50_upper_us\":" << s.observed.Upper(50)
        << ",\"observer_p99_upper_us\":" << s.observed.Upper(99)
        << ",\"sdk_cpu_s\":" << sdkCpu << ",\"worker_cpu_s\":" << workerCpu
        << ",\"CPU_us_per_success\":";
    if (s.successes) out << (sdkCpu + workerCpu) * 1e6 / s.successes; else out << "null";
    out << ",\"cpu_before\":";
    WriteCpuSample(out, aggregate.before);
    out << ",\"cpu_after\":";
    WriteCpuSample(out, aggregate.after);
    out << ",\"api_histogram_upper_us\":";
    WriteHistogram(out, s.api);
    out << ",\"observer_histogram_upper_us\":";
    WriteHistogram(out, s.observed);
    out << '}';
}

void SaveLedger(const std::filesystem::path &output, const std::array<Caller, Callers> &callers)
{
    std::ofstream ledger(output / "sampled-api-ledger.jsonl");
    for (const auto &caller : callers) {
        for (std::size_t row = 0; row < caller.used; ++row) {
            const auto &s = caller.ledger[row];
            ledger << "{\"caller\":" << s.caller << ",\"tid\":" << s.tid << ",\"phase_id\":" << s.phase
                   << ",\"arrival_id\":" << s.arrival << ",\"scheduled_ns\":" << s.begin << ",\"api_begin_ns\":" << s.begin
                   << ",\"api_end_ns\":" << s.end << ",\"validation_end_ns\":" << s.validated
                   << ",\"observer_end_ns\":" << s.observed_end << ",\"status\":" << s.status
                   << ",\"selected\":" << (s.selected ? "true" : "false")
                   << ",\"mismatch\":" << (s.mismatch ? "true" : "false") << "}\n";
        }
    }
    ledger.close();
    Require(bool(ledger), "sampled ledger write failed");
}

int Run(int argc, char **argv)
{
    Require(argc == 5, "usage: closed_loop_sdk get|set WORKER_PORT LOG_DIR OUTPUT_DIR");
    const std::string operation(argv[1]);
    Require(operation == "get" || operation == "set", "operation must be get or set");
    const bool write = operation == "set";
    const int port = PositiveInteger(argv[2], 65535);
    const int workerPid = PositiveInteger(std::getenv("DS_BENCH_WORKER_PID"), std::numeric_limits<int>::max());
    const auto clockTicks = sysconf(_SC_CLK_TCK);
    Require(clockTicks > 0, "CPU clock tick frequency");
    const char *rawMode = std::getenv("DS_BENCH_RPC_CAPTURE_MODE");
    const std::string mode = rawMode ? rawMode : "off";
    Require(mode == "off" || mode == "enabled", "only off or enabled capture is supported");
    Require(PositiveInteger(std::getenv("DS_BENCH_RPC_CAPTURE_MODULUS"), std::numeric_limits<int>::max())
                == SampleModulus, "capture modulus must match frozen ledger selector");
    Require(PositiveInteger(std::getenv("DS_BENCH_RPC_CAPTURE_SEED"), std::numeric_limits<int>::max())
                == Seed, "capture seed must match frozen ledger selector");
    const std::filesystem::path output(argv[4]);
    Require(std::filesystem::create_directory(output), "output directory must be new");
    const std::string payload(PayloadBytes, 'v');
    const std::string seedPayload(PayloadBytes, write ? 's' : 'v');
    const std::string warmPayload(PayloadBytes, write ? 'w' : 'v');
    std::array<std::string, KeyCount> keys;
    for (std::size_t index = 0; index < KeyCount; ++index) keys[index] = "rpc-closed-loop-" + std::to_string(index);

    datasystem::ConnectOptions options{};
    const char *host = std::getenv("DS_BENCH_WORKER_HOST");
    options.host = host ? host : "127.0.0.1";
    options.enableLocalCache = std::getenv("DS_BENCH_ROUTED") == nullptr;
    options.port = port;
    options.connectTimeoutMs = ConnectTimeoutMs;
    options.requestTimeoutMs = RequestTimeoutMs;
    datasystem::KVClientConfig config;
    RequireStatus(datasystem::KVClientConfig::Builder().LogDir(argv[3]).MinLogLevel(2).LogMonitorEnable(false).Build(config),
                  "configure SDK");
    KVClient client(options);
    RequireStatus(client.Init(config), "initialize SDK");
    ClientShutdown shutdown(client);
    for (const auto &key : keys) RequireStatus(client.Set(key, seedPayload), "seed key");
    benchmark_capture::Session capture(output, Callers, WarmCalls * Callers, MeasureCalls * Callers, true);
    auto callers = std::make_unique<std::array<Caller, Callers>>();
    std::uint64_t totalSelected = 0, ledgerBytes = 0;
    for (std::uint32_t index = 0; index < Callers; ++index) {
        std::size_t selected = 0;
        for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase)
            for (std::uint64_t ordinal = 0; ordinal < PhaseCalls(phase); ++ordinal)
                selected += Selected(phase, ArrivalId(phase, index, ordinal));
        (*callers)[index].ledger.resize(selected + ErrorReserve);
        ledgerBytes += (*callers)[index].ledger.size() * sizeof(LedgerRow);
        totalSelected += selected;
    }
    Require(totalSelected > 0 && CaptureBytes(totalSelected) + MetadataReserve <= CaptureBudget,
            "frozen finite selector or conservative capture bound mismatch");
#ifndef DS_CLOSED_LOOP_SMOKE
    Require(totalSelected == 3592, "formal finite selector mismatch");
#endif
    std::barrier gate(static_cast<std::ptrdiff_t>(Callers + 1));
    std::vector<std::thread> threads;
    threads.reserve(Callers);
    std::array<Aggregate, 2> phases;
    bool threadsLive = false;
    bool rpcAvailable = false;
    try {
        threadsLive = true;
        for (std::uint32_t index = 0; index < Callers; ++index) {
            threads.emplace_back([&, index] {
                try {
                    auto &caller = (*callers)[index];
                    caller.tid = static_cast<std::uint32_t>(syscall(SYS_gettid));
                    std::string actual;
                    if (write) RequireStatus(client.Set(keys[index], seedPayload), "caller prime set");
                    RequireStatus(client.Get(keys[index], actual), "caller prime get");
                    Require(actual == seedPayload, "caller prime bytes");
                    Require(capture.Register(index, caller.tid), "capture caller registration");
                    gate.arrive_and_wait();
                    gate.arrive_and_wait();
                    RunPhase(index, WarmPhase, write, client, keys, warmPayload, caller, capture);
                    gate.arrive_and_wait();
                    gate.arrive_and_wait();
                    RunPhase(index, MeasurePhase, write, client, keys, payload, caller, capture);
                    gate.arrive_and_wait();
                    gate.arrive_and_wait();
                    capture.Unregister(index);
                } catch (const std::exception &error) {
                    Fatal(&capture, error.what());
                } catch (...) {
                    Fatal(&capture, "caller unknown exception");
                }
            });
        }
        gate.arrive_and_wait();
        std::ofstream ready(output / "ready.json");
        ready << "{\"pid\":" << getpid() << ",\"worker_pid\":" << workerPid << ",\"callers\":" << Callers << ",\"capture_mode\":\"" << mode << "\",\"tids\":[";
        for (std::uint32_t index = 0; index < Callers; ++index) {
            if (index) ready << ',';
            ready << (*callers)[index].tid;
        }
        ready << "],\"lent_capture_bytes\":" << (capture.Active() ? CaptureBytes(totalSelected) : 0)
              << ",\"ledger_bytes\":" << ledgerBytes << ",\"caller_state_bytes\":" << sizeof(*callers)
              << ",\"monotonic_ns\":" << NowNs() << "}\n";
        ready.close();
        Require(bool(ready), "ready metadata write");
        std::cout << "READY CLOSED_LOOP_DIAGNOSTIC capture=" << mode << std::endl;
        std::string command;
        Require(bool(std::getline(std::cin, command)) && command == "GO", "cold validation handshake failed");
        Require(capture.Arm(), "capture arm");
        phases[0].before = ReadCpu(workerPid);
        gate.arrive_and_wait();
        gate.arrive_and_wait();
        phases[0].after = ReadCpu(workerPid);
        const auto warmBefore = phases[0].before, warmAfter = phases[0].after;
        phases[0] = Combine(*callers, WarmPhase);
        phases[0].before = warmBefore;
        phases[0].after = warmAfter;
        capture.Drained("warmup", false);
        phases[1].before = ReadCpu(workerPid);
        gate.arrive_and_wait();
        gate.arrive_and_wait();
        phases[1].after = ReadCpu(workerPid);
        const auto measureBefore = phases[1].before, measureAfter = phases[1].after;
        phases[1] = Combine(*callers, MeasurePhase);
        phases[1].before = measureBefore;
        phases[1].after = measureAfter;
        capture.Drained("measure", true);
        capture.BeforeStop(true);
        rpcAvailable = capture.RpcAvailable();
        gate.arrive_and_wait();
        for (auto &thread : threads) thread.join();
        threadsLive = false;
    } catch (const std::exception &error) {
        if (threadsLive) Fatal(&capture, error.what());
        throw;
    } catch (...) {
        if (threadsLive) Fatal(&capture, "main unknown exception");
        throw;
    }
    std::size_t readbackErrors = 0;
    for (std::size_t index = 0; index < KeyCount; ++index) {
        std::string actual;
        const auto status = client.Get(keys[index], actual);
        const auto &expected = write && index >= Callers ? seedPayload : payload;
        readbackErrors += !status.IsOk() || actual != expected;
    }
    const auto shutdownStatus = client.ShutDown();
    shutdown.Complete();
    const bool captureClosed = capture.Close();
    SaveLedger(output, *callers);
    const bool good = shutdownStatus.IsOk() && captureClosed && !readbackErrors
        && !phases[0].stats.errors && !phases[0].stats.mismatches
        && !phases[1].stats.errors && !phases[1].stats.mismatches;
    std::ostringstream json;
    json.precision(17);
    json << "{\"scope\":\"FINITE_CLOSED_LOOP_CAPTURE_DIAGNOSTIC_NOT_RPC30_ACCEPTANCE\",\"operation\":\""
         << operation << "\",\"capture_mode\":\"" << mode << "\",\"callers\":" << Callers
         << ",\"payload_bytes\":" << PayloadBytes << ",\"clock_ticks_per_second\":" << clockTicks
         << ",\"sdk_pid\":" << getpid() << ",\"worker_pid\":" << workerPid
         << ",\"warm_calls_per_caller\":" << WarmCalls << ",\"measure_calls_per_caller\":" << MeasureCalls
         << ",\"ledger_bytes\":" << ledgerBytes << ",\"ledger_row_bytes\":" << sizeof(LedgerRow)
         << ",\"caller_state_bytes\":" << sizeof(*callers) << ",\"capture_lent_bytes\":"
         << (capture.Active() ? CaptureBytes(totalSelected) : 0)
         << ",\"readback_keys\":" << KeyCount << ",\"readback_errors\":" << readbackErrors
         << ",\"sdk_shutdown_ok\":" << (shutdownStatus.IsOk() ? "true" : "false")
         << ",\"capture_close_ok\":" << (captureClosed ? "true" : "false")
         << ",\"rpc_capture_available\":" << (rpcAvailable ? "true" : "false")
         << ",\"quality_valid\":" << (good ? "true" : "false")
         << ",\"RPC30_accepted\":false,\"fixed_offered_load\":false,\"scheduled_equals_api_begin\":true,\"arrival_E2E_available\":false,\"phases\":[";
    WritePhase(json, phases[0], WarmPhase, clockTicks);
    json << ',';
    WritePhase(json, phases[1], MeasurePhase, clockTicks);
    json << "],\"caller_phases\":[";
    bool comma = false;
    for (std::uint32_t index = 0; index < Callers; ++index) {
        const auto &caller = (*callers)[index];
        for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase) {
            if (comma) json << ',';
            comma = true;
            const auto &stats = caller.phases[phase - 1];
            json << "{\"caller\":" << index << ",\"tid\":" << caller.tid << ",\"phase_id\":" << phase
                 << ",\"attempts\":" << stats.attempts << ",\"api_begin_ns\":" << stats.first
                 << ",\"observer_end_ns\":" << stats.last << ",\"ledger_rows_both_phases\":" << caller.used
                 << ",\"ledger_capacity\":" << caller.ledger.size() << '}';
        }
    }
    json << "]}";
    std::ofstream metric(output / "metric.json");
    metric << json.str() << '\n';
    metric.close();
    Require(bool(metric), "metric write failed");
    std::cout << "RESULT " << json.str() << std::endl;
    return good ? 0 : 1;
}
} // namespace

int main(int argc, char **argv)
{
    try {
        return Run(argc, argv);
    } catch (const std::exception &error) {
        std::cerr << "CLOSED_LOOP_DRIVER_ERROR " << error.what() << std::endl;
        return 2;
    }
}
