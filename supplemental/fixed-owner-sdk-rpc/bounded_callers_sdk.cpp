#include "capture_helper.h"
#include "closed_loop_plan.h"
#include "fixed_owner_plan.h"
#include "bounded_admission.h"
#include <atomic>
#include <span>
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
using closed_loop::Histogram;
using benchmark_capture::FixedOwnerPlan;
using fixed_arrival::Row;
using fixed_arrival::State;
constexpr std::uint32_t Callers = 32, SampleModulus = 1;
constexpr std::uint64_t Seed = 17, Rate = 500;
constexpr std::uint64_t PhaseLeadNs = 250'000'000;
#ifdef DS_FIXED_OWNER_SMOKE
constexpr std::uint64_t WarmCalls = 4, MeasureCalls = 8;
constexpr bool Smoke = true;
#else
constexpr std::uint64_t WarmCalls = 128, MeasureCalls = 1024;
constexpr bool Smoke = false;
#endif
constexpr std::size_t PayloadBytes = 4096, KeyCount = 64;
constexpr int ConnectTimeoutMs = 5000, RequestTimeoutMs = 2000;
constexpr std::uint32_t WarmPhase = 1, MeasurePhase = 2;
constexpr std::int32_t ByteMismatchOutcome = -1;

std::uint64_t NowNs() noexcept
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


struct PhaseStats {
    Histogram api, observed, e2e, dispatch;
    std::uint64_t attempts = 0, successes = 0, errors = 0, mismatches = 0;
    std::uint64_t cancelled = 0, rejected_early = 0, rejected_late = 0, exceptions = 0;
    std::uint64_t returned_bytes = 0, validated_bytes = 0, max_lateness = 0, last_api_end = 0;
    std::uint64_t soft_late_count = 0, debt_violations = 0;
};
struct Caller {
    std::atomic<std::uint32_t> tid{0};
    std::array<PhaseStats, 2> phases;
    std::array<std::vector<Row>, 2> ledger;
    std::string actual;
};
struct Phase {
    std::uint64_t epoch_created = 0, epoch = 0, planned_end = 0;
    CpuSample before, after;
};
struct ArrivalClock {
    ~ArrivalClock() = default;
    std::uint64_t NowNs() const noexcept { return ::NowNs(); }
    void SleepUntil(std::uint64_t target) const {
        Require(target <= std::uint64_t(std::numeric_limits<std::int64_t>::max()), "clock target overflow");
        std::this_thread::sleep_until(Clock::time_point(std::chrono::nanoseconds(static_cast<std::int64_t>(target))));
    }
};
class ClientShutdown {
public:
    explicit ClientShutdown(KVClient &client) : client_(client) {}
    ~ClientShutdown() { if (active_) { try { client_.ShutDown(); } catch (...) {} } }
    ClientShutdown(const ClientShutdown &) = delete;
    ClientShutdown &operator=(const ClientShutdown &) = delete;
    void Complete() noexcept { active_ = false; }
private:
    KVClient &client_;
    bool active_ = true;
};
class CaptureEnd {
public:
    CaptureEnd(benchmark_capture::Session &capture, std::size_t owner, std::uint32_t phase,
               Row &row, std::uint64_t methods) : capture_(capture), owner_(owner), row_(row) {
        capture_.Begin(owner_, phase, row_.id, row_.scheduled_ns, row_.begin_ns, methods);
    }
    ~CaptureEnd() {
        if (!ended_) {
            const auto now = NowNs();
            capture_.End(owner_, now, now, fixed_arrival::CallbackFailure);
        }
    }
    CaptureEnd(const CaptureEnd &) = delete;
    CaptureEnd &operator=(const CaptureEnd &) = delete;
    void Complete() noexcept {
        capture_.End(owner_, row_.end_ns, row_.validation_ns, row_.status);
        ended_ = true;
    }
private:
    benchmark_capture::Session &capture_;
    std::size_t owner_;
    Row &row_;
    bool ended_ = false;
};
bool Attempted(const Row &row) {
    return row.begin_ns && (row.state == State::Returned || row.state == State::Exception || row.state == State::InvalidTime);
}
const char *StateName(const Row &row, bool started) {
    if (!started) return "unattempted_prior_phase";
    switch (row.state) {
        case State::Planned: return "planned";
        case State::InFlight: return "in_flight";
        case State::Returned: return "returned";
        case State::RejectedEarly: return "rejected_early";
        case State::RejectedLate: return "rejected_late";
        case State::Cancelled: return "cancelled_window";
        case State::Exception: return "execution_exception";
        case State::InvalidTime: return "invalid_time";
    }
    return "invalid_state";
}
void Stamp(std::ostream &out, std::uint64_t value, bool present) {
    if (present) out << value; else out << "null";
}
void SaveLedger(const std::filesystem::path &file, const std::array<Caller, Callers> &callers,
                const std::array<Phase, 2> &phases, const FixedOwnerPlan &plan, bool write) {
    std::ofstream out(file);
    for (std::uint32_t owner = 0; owner < Callers; ++owner) {
        const auto &caller = callers[owner];
        for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase) {
            for (const auto &row : caller.ledger[phase - 1]) {
                const bool attempted = Attempted(row), started = phases[phase - 1].epoch != 0;
                out << "{\"caller\":" << owner << ",\"tid\":" << caller.tid.load(std::memory_order_acquire)
                    << ",\"phase_id\":" << phase << ",\"arrival_id\":" << row.id << ",\"scheduled_ns\":";
                Stamp(out, row.scheduled_ns, started);
                out << ",\"dispatch_checked_ns\":"; Stamp(out, row.begin_ns, row.begin_ns != 0);
                out << ",\"api_begin_ns\":"; Stamp(out, row.begin_ns, attempted);
                out << ",\"api_end_ns\":"; Stamp(out, row.end_ns, attempted);
                out << ",\"validation_end_ns\":"; Stamp(out, row.validation_ns, attempted);
                out << ",\"observer_end_ns\":"; Stamp(out, row.observer_ns, attempted);
                out << ",\"state\":\"" << StateName(row, started) << "\",\"attempted\":" << (attempted ? "true" : "false")
                    << ",\"status\":";
                if (attempted) out << row.status; else out << "null";
                out << ",\"selected\":" << (plan.Selected(phase, row.id) ? "true" : "false")
                    << ",\"mismatch\":" << (row.mismatch ? "true" : "false")
                    << ",\"required_methods\":" << (write ? benchmark_capture::WriteMethods : benchmark_capture::ReadMethods)
                    << "}\n";
            }
        }
    }
    out.close(); Require(bool(out), "full arrival ledger write failed");
}
void RunPhase(std::uint32_t owner, std::uint32_t phase, bool write, KVClient &client,
              const std::array<std::string, KeyCount> &keys, const std::string &payload,
              Caller &caller, benchmark_capture::Session &capture, std::atomic<bool> &abort) {
    auto &stats = caller.phases[phase - 1];
    auto &actual = caller.actual;
    const auto methods = write ? benchmark_capture::WriteMethods : benchmark_capture::ReadMethods;
    ArrivalClock clock;
    bounded_admission::Execute(caller.ledger[phase - 1], abort, clock, [&](Row &row) {
        CaptureEnd ending(capture, owner, phase, row, methods);
        const auto round = row.id / Callers;
        const auto status = write ? client.Set(keys[owner], payload) : client.Get(keys[(round + owner) % KeyCount], actual);
        row.end_ns = NowNs();
        row.mismatch = !write && status.IsOk() && actual != payload;
        row.validation_ns = NowNs();
        row.status = row.mismatch && status.IsOk() ? ByteMismatchOutcome : static_cast<std::int32_t>(status.GetCode());
        ending.Complete();
        row.observer_ns = NowNs();
        if (!write && status.IsOk()) {
            stats.returned_bytes += actual.size();
            if (!row.mismatch) stats.validated_bytes += actual.size();
        }
    });
    for (const auto &row : caller.ledger[phase - 1]) {
        stats.cancelled += row.state == State::Cancelled || row.state == State::Planned;
        stats.rejected_early += row.state == State::RejectedEarly;
        stats.rejected_late += row.state == State::RejectedLate;
        stats.exceptions += row.state == State::Exception || row.state == State::InvalidTime;
        if (row.begin_ns >= row.scheduled_ns && row.begin_ns) {
            const auto lateness = row.begin_ns - row.scheduled_ns;
            stats.max_lateness = std::max(stats.max_lateness, lateness);
            stats.soft_late_count += lateness > bounded_admission::SoftLatenessNs;
        }
        if (!Attempted(row)) continue;
        stats.debt_violations += !bounded_admission::ObserverWithinPeriod(row);
        ++stats.attempts; stats.successes += row.state == State::Returned && row.status == 0 && !row.mismatch;
        stats.errors += row.status != 0 && !row.mismatch;
        stats.mismatches += row.mismatch;
        stats.last_api_end = std::max(stats.last_api_end, row.end_ns);
        if (row.state == State::Returned) {
            stats.api.Record(row.end_ns - row.begin_ns);
            stats.observed.Record(row.observer_ns - row.begin_ns);
            stats.e2e.Record(row.end_ns - row.scheduled_ns);
            stats.dispatch.Record(row.begin_ns - row.scheduled_ns);
        }
    }
}
PhaseStats Combine(const std::array<Caller, Callers> &callers, std::uint32_t phase) {
    PhaseStats result;
    for (const auto &caller : callers) {
        const auto &s = caller.phases[phase - 1];
        result.api.Merge(s.api); result.observed.Merge(s.observed); result.e2e.Merge(s.e2e); result.dispatch.Merge(s.dispatch);
        result.attempts += s.attempts; result.successes += s.successes; result.errors += s.errors;
        result.mismatches += s.mismatches; result.cancelled += s.cancelled;
        result.rejected_early += s.rejected_early; result.rejected_late += s.rejected_late;
        result.exceptions += s.exceptions; result.returned_bytes += s.returned_bytes;
        result.validated_bytes += s.validated_bytes;
        result.soft_late_count += s.soft_late_count; result.debt_violations += s.debt_violations;
        result.max_lateness = std::max(result.max_lateness, s.max_lateness);
        result.last_api_end = std::max(result.last_api_end, s.last_api_end);
    }
    return result;
}
std::uint64_t Quantile(std::vector<std::uint64_t> values, unsigned percentile) {
    if (values.empty()) return 0;
    const auto rank = (values.size() / 100) * percentile + ((values.size() % 100) * percentile + 99) / 100;
    std::nth_element(values.begin(), values.begin() + rank - 1, values.end());
    return values[rank - 1];
}
void WriteAdmissionProfile(std::ostream &out) {
    out << "{\"protocol\":\"" << bounded_admission::Protocol
        << "\",\"owner_period_ns\":" << bounded_admission::OwnerPeriodNs
        << ",\"hard_dispatch_lateness_ns_max_inclusive\":" << bounded_admission::HardLatenessNs
        << ",\"soft_late_threshold_ns_exclusive\":" << bounded_admission::SoftLatenessNs
        << ",\"soft_late_budget_numerator\":1,\"soft_late_budget_denominator\":" << bounded_admission::SoftBudgetDenominator
        << ",\"no_debt_policy\":\"observer_end_strictly_before_scheduled_plus_owner_period_including_last\"}";
}
void WriteCpuSample(std::ostream &out, const CpuSample &s) {
    out << "{\"read_begin_ns\":" << s.read_begin_ns << ",\"read_end_ns\":" << s.read_end_ns
        << ",\"sdk_ticks\":" << s.sdk_ticks << ",\"worker_ticks\":" << s.worker_ticks << '}';
}
void WritePhase(std::ostream &out, const std::array<Caller, Callers> &callers, const Phase &p,
                const FixedOwnerPlan &plan, std::uint32_t phase, long hz) {
    const auto s = Combine(callers, phase);
    std::array<std::vector<std::uint64_t>, 4> times;
    for (const auto &caller : callers) for (const auto &row : caller.ledger[phase - 1]) {
        if (row.state != State::Returned) continue;
        times[0].push_back(row.end_ns - row.begin_ns); times[1].push_back(row.end_ns - row.scheduled_ns);
        times[2].push_back(row.begin_ns - row.scheduled_ns); times[3].push_back(row.observer_ns - row.begin_ns);
    }
    const auto completion = std::max(p.planned_end, s.last_api_end);
    const auto duration = p.epoch ? completion - p.epoch : 0;
    const auto cpuTicks = (p.after.sdk_ticks - p.before.sdk_ticks) + (p.after.worker_ticks - p.before.worker_ticks);
    out << "{\"phase_id\":" << phase << ",\"phase_started\":" << (p.epoch ? "true" : "false")
        << ",\"planned\":" << plan.TotalCalls(phase) << ",\"epoch_created_ns\":" << p.epoch_created
        << ",\"epoch_ns\":" << p.epoch << ",\"planned_end_ns\":" << p.planned_end
        << ",\"planned_duration_ns\":" << (p.epoch ? p.planned_end - p.epoch : 0)
        << ",\"completion_end_ns\":" << completion << ",\"elapsed_s\":" << double(duration) / 1e9
        << ",\"offered_rate\":" << (p.epoch ? double(plan.TotalCalls(phase)) * 1e9 / (p.planned_end - p.epoch) : 0)
        << ",\"qps\":" << (duration ? double(s.successes) * 1e9 / duration : 0)
        << ",\"attempts\":" << s.attempts << ",\"success\":" << s.successes
        << ",\"errors\":" << s.errors << ",\"mismatches\":" << s.mismatches
        << ",\"cancelled\":" << s.cancelled << ",\"rejected_early\":" << s.rejected_early
        << ",\"rejected_late\":" << s.rejected_late << ",\"exceptions\":" << s.exceptions
        << ",\"max_dispatch_lateness_ns\":" << s.max_lateness
        << ",\"soft_late_count\":" << s.soft_late_count
        << ",\"soft_late_budget_ok\":" << (p.epoch && bounded_admission::SoftBudgetPassed(s.soft_late_count, plan.TotalCalls(phase)) ? "true" : "false")
        << ",\"debt_violations\":" << s.debt_violations
        << ",\"returned_bytes\":" << s.returned_bytes << ",\"validated_bytes\":" << s.validated_bytes;
    constexpr const char *names[] = {"api", "e2e", "dispatch", "observer"};
    for (unsigned i = 0; i < times.size(); ++i)
        out << ",\"" << names[i] << "_p50_ns\":" << Quantile(times[i], 50)
            << ",\"" << names[i] << "_p99_ns\":" << Quantile(times[i], 99);
    out << ",\"CPU_us_per_success\":";
    if (s.successes) out << double(cpuTicks) * 1e6 / hz / s.successes; else out << "null";
    out << ",\"cpu_before\":"; WriteCpuSample(out, p.before);
    out << ",\"cpu_after\":"; WriteCpuSample(out, p.after);
    out << '}';
}
int Run(int argc, char **argv) {
    Require(argc == 5, "usage: paced_callers_sdk get|set WORKER_PORT LOG_DIR OUTPUT_DIR");
    const std::string operation(argv[1]); Require(operation == "get" || operation == "set", "operation must be get or set");
    const bool write = operation == "set";
    const char *backendOption = std::getenv("DATASYSTEM_RPC_BACKEND");
    const std::string backendValue = backendOption ? backendOption : "brpc";
    Require(backendValue == "brpc" || backendValue == "coro_rpc", "unsupported backend");
    const std::string backend = backendValue == "coro_rpc" ? "coro" : "brpc";
    const int port = PositiveInteger(argv[2], 65535);
    const int workerPid = PositiveInteger(std::getenv("DS_BENCH_WORKER_PID"), std::numeric_limits<int>::max());
    const long hz = sysconf(_SC_CLK_TCK); Require(hz > 0, "CPU tick frequency");
    const char *rawMode = std::getenv("DS_BENCH_RPC_CAPTURE_MODE"); const std::string mode = rawMode ? rawMode : "off";
    Require(mode == "off" || mode == "enabled", "unsupported capture mode");
    Require(PositiveInteger(std::getenv("DS_BENCH_RPC_CAPTURE_MODULUS"), std::numeric_limits<int>::max()) == SampleModulus,
            "capture modulus must equal frozen full capture");
    Require(PositiveInteger(std::getenv("DS_BENCH_RPC_CAPTURE_SEED"), std::numeric_limits<int>::max()) == Seed,
            "capture seed must equal frozen plan");
    const FixedOwnerPlan plan({Callers, WarmCalls, MeasureCalls, Rate, Seed, SampleModulus});
    Require(plan.CaptureBytes() + FixedOwnerPlan::MetadataReserveBytes <= FixedOwnerPlan::BudgetBytes, "capture budget");
    const std::filesystem::path output(argv[4]); Require(std::filesystem::create_directory(output), "output directory must be new");
    const std::string payload(PayloadBytes, 'v'), seedPayload(PayloadBytes, write ? 's' : 'v'), warmPayload(PayloadBytes, write ? 'w' : 'v');
    std::array<std::string, KeyCount> keys;
    for (std::size_t i = 0; i < KeyCount; ++i) keys[i] = "rpc-fixed-arrival-" + std::to_string(i);
    datasystem::ConnectOptions options{}; const char *host = std::getenv("DS_BENCH_WORKER_HOST");
    options.host = host ? host : "127.0.0.1"; options.enableLocalCache = std::getenv("DS_BENCH_ROUTED") == nullptr;
    options.port = port; options.connectTimeoutMs = ConnectTimeoutMs; options.requestTimeoutMs = RequestTimeoutMs;
    datasystem::KVClientConfig config;
    RequireStatus(datasystem::KVClientConfig::Builder().LogDir(argv[3]).MinLogLevel(2).LogMonitorEnable(false).Build(config), "configure SDK");
    KVClient client(options); RequireStatus(client.Init(config), "initialize SDK"); ClientShutdown shutdown(client);
    for (const auto &key : keys) RequireStatus(client.Set(key, seedPayload), "seed key");
    benchmark_capture::Session capture(output, plan, true);
    auto callers = std::make_unique<std::array<Caller, Callers>>();
    std::uint64_t ledgerBytes = 0;
    for (std::uint32_t owner = 0; owner < Callers; ++owner) for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase) {
        auto &rows = (*callers)[owner].ledger[phase - 1]; rows.resize(plan.Calls(phase));
        for (std::uint64_t round = 0; round < rows.size(); ++round) rows[round].id = plan.Id(phase, owner, round);
        ledgerBytes += rows.size() * sizeof(Row);
    }
    std::array<Phase, 2> phases;
    std::atomic<bool> abort{false};
    std::barrier gate(static_cast<std::ptrdiff_t>(Callers + 1));
    std::vector<std::thread> threads; threads.reserve(Callers);
    bool threadsLive = false, rpcAvailable = false;
    try {
        threadsLive = true;
        for (std::uint32_t owner = 0; owner < Callers; ++owner) threads.emplace_back([&, owner] {
            auto &caller = (*callers)[owner]; bool registered = false;
            try {
                const auto tid = syscall(SYS_gettid); Require(tid > 0, "caller TID unavailable");
                caller.tid.store(static_cast<std::uint32_t>(tid), std::memory_order_release);
                auto &actual = caller.actual; actual.reserve(PayloadBytes);
                if (write) RequireStatus(client.Set(keys[owner], seedPayload), "prime set");
                RequireStatus(client.Get(keys[owner], actual), "prime get"); Require(actual == seedPayload, "prime bytes");
                registered = capture.Register(owner, caller.tid.load(std::memory_order_relaxed));
                if (!registered) abort.store(true, std::memory_order_release);
            } catch (...) { abort.store(true, std::memory_order_release); }
            gate.arrive_and_wait();
            for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase) {
                gate.arrive_and_wait();
                try { RunPhase(owner, phase, write, client, keys, phase == WarmPhase ? warmPayload : payload, caller, capture, abort); }
                catch (...) { abort.store(true, std::memory_order_release); }
                gate.arrive_and_wait();
            }
            gate.arrive_and_wait();
            if (registered) capture.Unregister(owner);
        });
        gate.arrive_and_wait();
        std::ofstream ready(output / "ready.json");
        ready << "{\"pid\":" << getpid() << ",\"worker_pid\":" << workerPid << ",\"callers\":" << Callers
              << ",\"capture_mode\":\"" << mode << "\",\"rate_per_second\":" << Rate << ",\"admission_profile\":";
        WriteAdmissionProfile(ready); ready << ",\"tids\":[";
        for (std::uint32_t owner = 0; owner < Callers; ++owner) {
            if (owner) ready << ',';
            ready << (*callers)[owner].tid.load(std::memory_order_acquire);
        }
        ready << "],\"lent_capture_bytes\":" << (capture.Active() ? plan.CaptureBytes() : 0)
              << ",\"ledger_bytes\":" << ledgerBytes << ",\"caller_state_bytes\":" << sizeof(*callers)
              << ",\"monotonic_ns\":" << NowNs() << "}\n";
        ready.close(); Require(bool(ready), "ready metadata write");
        if (!abort.load(std::memory_order_acquire)) {
            std::cout << "READY FIXED_OWNER_ARRIVAL capture=" << mode << std::endl;
            std::string command; Require(bool(std::getline(std::cin, command)) && command == "GO", "cold GO handshake");
            if (!capture.Arm()) abort.store(true, std::memory_order_release);
        }
        for (std::uint32_t phase = WarmPhase; phase <= MeasurePhase; ++phase) {
            auto &p = phases[phase - 1];
            if (!abort.load(std::memory_order_acquire)) {
                p.epoch_created = NowNs(); Require(p.epoch_created <= std::numeric_limits<std::uint64_t>::max() - PhaseLeadNs, "epoch overflow");
                p.epoch = p.epoch_created + PhaseLeadNs; p.planned_end = plan.EndNs(p.epoch, phase);
                Require(p.planned_end <= std::uint64_t(std::numeric_limits<std::int64_t>::max()), "clock range");
                for (std::uint32_t owner = 0; owner < Callers; ++owner) {
                    auto &rows = (*callers)[owner].ledger[phase - 1];
                    for (std::uint64_t round = 0; round < rows.size(); ++round)
                        rows[round].scheduled_ns = plan.ScheduledNs(p.epoch, phase, owner, round);
                }
                p.before = ReadCpu(workerPid);
            }
            gate.arrive_and_wait(); gate.arrive_and_wait();
            if (p.epoch) p.after = ReadCpu(workerPid);
            // Persist before capture control can retain borrowed owners and exit.
            SaveLedger(output / (phase == WarmPhase ? "all-arrivals-warm-checkpoint.jsonl" : "all-arrivals.jsonl"), *callers, phases, plan, write);
            if (p.epoch) {
                std::uint64_t softLate = 0;
                for (const auto &caller : *callers) softLate += caller.phases[phase - 1].soft_late_count;
                if (!bounded_admission::SoftBudgetPassed(softLate, plan.TotalCalls(phase)))
                    abort.store(true, std::memory_order_release);
                capture.Drained(phase == WarmPhase ? "warmup" : "measure", phase == MeasurePhase);
            }
        }
        capture.BeforeStop(true); rpcAvailable = capture.RpcAvailable();
        gate.arrive_and_wait();
        for (auto &thread : threads) thread.join();
        threadsLive = false;
    } catch (...) {
        if (threadsLive) {
            try { SaveLedger(output / "all-arrivals-partial.jsonl", *callers, phases, plan, write); } catch (...) {}
            capture.Partial("driver-exception-partial");
            std::cerr << "FIXED_OWNER_DRIVER_FATAL_PARTIAL owners retained\n" << std::flush;
            std::_Exit(2);
        }
        throw;
    }
    std::size_t readbackErrors = 0;
    for (std::size_t i = 0; i < KeyCount; ++i) {
        std::string actual; const auto status = client.Get(keys[i], actual);
        const auto &expected = write && i >= Callers ? seedPayload : payload;
        readbackErrors += !status.IsOk() || actual != expected;
    }
    const auto shutdownStatus = client.ShutDown(); shutdown.Complete();
    const bool captureClosed = capture.Close();
    const auto warmStats = Combine(*callers, WarmPhase), measureStats = Combine(*callers, MeasurePhase);
    const bool good = !abort.load(std::memory_order_acquire) && shutdownStatus.IsOk() && captureClosed && !readbackErrors
        && warmStats.successes == plan.TotalCalls(WarmPhase) && measureStats.successes == plan.TotalCalls(MeasurePhase);
    std::ostringstream metric; metric.precision(17);
    metric << "{\"scope\":\"FIXED_OWNER_ARRIVAL_DIAGNOSTIC_NOT_OVERALL_PROMOTION\",\"admission_profile\":";
    WriteAdmissionProfile(metric);
    metric << ",\"operation\":\"" << operation
           << "\",\"backend\":\"" << backend << "\",\"capture_mode\":\"" << mode << "\",\"callers\":" << Callers << ",\"payload_bytes\":" << PayloadBytes
           << ",\"rate_per_second\":" << Rate << ",\"clock_ticks_per_second\":" << hz << ",\"sdk_pid\":" << getpid()
           << ",\"worker_pid\":" << workerPid << ",\"warm_calls_per_caller\":" << WarmCalls
           << ",\"measure_calls_per_caller\":" << MeasureCalls << ",\"smoke\":" << (Smoke ? "true" : "false")
           << ",\"ledger_bytes\":" << ledgerBytes << ",\"ledger_row_bytes\":" << sizeof(Row)
           << ",\"caller_state_bytes\":" << sizeof(*callers) << ",\"capture_lent_bytes\":" << (capture.Active() ? plan.CaptureBytes() : 0)
           << ",\"readback_keys\":" << KeyCount << ",\"readback_errors\":" << readbackErrors
           << ",\"sdk_shutdown_ok\":" << (shutdownStatus.IsOk() ? "true" : "false")
           << ",\"capture_close_ok\":" << (captureClosed ? "true" : "false")
           << ",\"rpc_capture_available\":" << (rpcAvailable ? "true" : "false")
           << ",\"quality_valid\":" << (good ? "true" : "false")
           << ",\"RPC30_accepted\":false,\"fixed_offered_load\":" << (good ? "true" : "false")
           << ",\"scheduled_equals_api_begin\":false,\"arrival_E2E_available\":true,\"phases\":[";
    WritePhase(metric, *callers, phases[0], plan, WarmPhase, hz); metric << ',';
    WritePhase(metric, *callers, phases[1], plan, MeasurePhase, hz); metric << "]}";
    std::ofstream out(output / "metric.json"); out << metric.str() << '\n'; out.close(); Require(bool(out), "metric write");
    std::cout << "RESULT " << metric.str() << std::endl;
    return good ? 0 : 1;
}
} // namespace
int main(int argc, char **argv) {
    try { return Run(argc, argv); }
    catch (const std::exception &error) { std::cerr << "FIXED_OWNER_DRIVER_ERROR " << error.what() << std::endl; return 2; }
}
