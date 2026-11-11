#include "pacer.h"
#include "priming_gate.h"
#include "datasystem/kv_client.h"

#include <atomic>
#include <charconv>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <set>
#include <sstream>
#include <thread>
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace {
using arrival::Ns;
using Clock = std::chrono::steady_clock;
constexpr Ns second = 1000000000;
Ns now() { return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count(); }
void waitUntil(Ns target, Ns spin)
{
    const auto point = [](Ns value) {
        return Clock::time_point(std::chrono::duration_cast<Clock::duration>(std::chrono::nanoseconds(value)));
    };
    if (now() < target - spin) std::this_thread::sleep_until(point(target - spin));
    while (now() < target) std::atomic_signal_fence(std::memory_order_seq_cst);
}
std::int64_t number(const std::string& text, std::int64_t low, std::int64_t high)
{
    std::int64_t value = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || value < low || value > high) {
        throw std::invalid_argument("invalid numeric option");
    }
    return value;
}
std::vector<int> cpuList(const std::string& text)
{
    std::vector<int> result;
    std::istringstream input(text);
    for (std::string part; std::getline(input, part, ',');) {
        result.push_back(static_cast<int>(number(part, 0, CPU_SETSIZE - 1)));
    }
    return result;
}
void affinity(const std::vector<int>& cpus)
{
    cpu_set_t mask;
    CPU_ZERO(&mask);
    for (int cpu : cpus) CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask) != 0) throw std::runtime_error("thread affinity failed");
}

struct Config {
    std::string host, operation, logDir;
    std::filesystem::path output;
    int port, workerPid, sdkTimeoutMs, producerCpu;
    std::vector<int> sdkCpus;
    std::size_t callers, capacity, payload, burstLimit;
    std::uint64_t rate;
    bool primePerCaller;
    Ns duration, warmup, budget, drain, spin, maxLateness, p99Lateness;
};
Config options(int argc, char** argv)
{
    std::map<std::string, std::string> args{{"host", ""}, {"port", ""}, {"rate", ""}, {"worker-pid", ""},
        {"out", ""}, {"log-dir", ""}, {"op", "get"}, {"measure-ms", "4000"}, {"warmup-ms", "1000"},
        {"budget-ms", "2000"}, {"drain-ms", "3000"}, {"sdk-timeout-ms", "2000"}, {"queue", "256"},
        {"callers", "32"}, {"payload", "4096"}, {"spin-us", "20"}, {"max-lateness-us", "1000"},
        {"p99-lateness-us", "100"}, {"max-burst", "64"}, {"producer-cpu", ""}, {"sdk-cpus", ""},
        {"prime-per-caller", "0"}};
    std::set<std::string> seen;
    for (int i = 1; i < argc; i += 2) {
        const std::string key(argv[i]);
        if (i + 1 == argc || key.substr(0, 2) != "--" || !args.count(key.substr(2))
            || !seen.insert(key).second) throw std::invalid_argument("unknown/duplicate/missing option");
        args[key.substr(2)] = argv[i + 1];
    }
    for (const auto* key : {"host", "port", "rate", "worker-pid", "out", "log-dir", "producer-cpu", "sdk-cpus"}) {
        if (args[key].empty()) throw std::invalid_argument(std::string("missing --") + key);
    }
    Config c{};
    c.host = args["host"]; c.logDir = args["log-dir"]; c.output = args["out"]; c.operation = args["op"];
    if (c.operation != "get" && c.operation != "set") throw std::invalid_argument("op must be get or set");
    c.port = static_cast<int>(number(args["port"], 1, 65535));
    c.workerPid = static_cast<int>(number(args["worker-pid"], 1, 2147483647));
    c.sdkTimeoutMs = static_cast<int>(number(args["sdk-timeout-ms"], 1, 30000));
    c.rate = static_cast<std::uint64_t>(number(args["rate"], 1, second));
    c.callers = static_cast<std::size_t>(number(args["callers"], 1, 128));
    c.capacity = static_cast<std::size_t>(number(args["queue"], 1, 65536));
    c.payload = static_cast<std::size_t>(number(args["payload"], 1, 8 * 1024 * 1024));
    c.burstLimit = static_cast<std::size_t>(number(args["max-burst"], 1, 65536));
    c.primePerCaller = number(args["prime-per-caller"], 0, 1) != 0;
    c.duration = number(args["measure-ms"], 1, 60000) * 1000000;
    c.warmup = number(args["warmup-ms"], 1, 30000) * 1000000;
    c.budget = number(args["budget-ms"], 1, 30000) * 1000000;
    c.drain = number(args["drain-ms"], 1, 60000) * 1000000;
    c.spin = number(args["spin-us"], 0, 100) * 1000;
    c.maxLateness = number(args["max-lateness-us"], 1, 1000000) * 1000;
    c.p99Lateness = number(args["p99-lateness-us"], 1, 1000000) * 1000;
    if (c.p99Lateness > c.maxLateness) throw std::invalid_argument("P99 lateness exceeds maximum lateness");
    c.producerCpu = static_cast<int>(number(args["producer-cpu"], 0, CPU_SETSIZE - 1));
    c.sdkCpus = cpuList(args["sdk-cpus"]);
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0 || CPU_COUNT(&allowed) != 8) {
        throw std::runtime_error("launch within the predeclared eight-CPU SDK mask");
    }
    const std::set<int> distinct(c.sdkCpus.begin(), c.sdkCpus.end());
    if (c.sdkCpus.size() != 7 || distinct.size() != 7 || distinct.count(c.producerCpu)) {
        throw std::invalid_argument("requires seven SDK CPUs and one distinct pacing CPU");
    }
    std::set<std::string> siblings;
    auto all = c.sdkCpus; all.push_back(c.producerCpu);
    for (int cpu : all) {
        if (!CPU_ISSET(cpu, &allowed)) throw std::invalid_argument("CPU outside initial mask");
        std::ifstream input("/sys/devices/system/cpu/cpu" + std::to_string(cpu) + "/topology/thread_siblings_list");
        std::string value;
        if (!std::getline(input, value) || !siblings.insert(value).second) throw std::invalid_argument("reused SMT sibling");
    }
    arrival::Plan(0, c.duration, c.rate, c.budget);
    arrival::Plan(0, c.warmup, c.rate, c.budget);
    return c;
}

struct Usage { bool available = false; std::int64_t cpuTicks = 0, threads = 0, rssKiB = 0; };
Usage usage(int pid)
{
    Usage result;
    std::ifstream input("/proc/" + std::to_string(pid) + "/stat");
    std::string line;
    if (!std::getline(input, line) || line.rfind(')') == std::string::npos) return result;
    std::istringstream fields(line.substr(line.rfind(')') + 2));
    std::vector<std::string> values(22);
    for (auto& value : values) if (!(fields >> value)) return result;
    result.cpuTicks = std::stoll(values[11]) + std::stoll(values[12]);
    result.threads = std::stoll(values[17]);
    result.rssKiB = std::stoll(values[21]) * sysconf(_SC_PAGESIZE) / 1024;
    result.available = true;
    return result;
}
struct Snapshot { Ns before, after; Usage sdk, worker; };
Snapshot snapshot(int workerPid)
{
    Snapshot result{};
    result.before = now(); result.sdk = usage(getpid()); result.worker = usage(workerPid); result.after = now();
    return result;
}
void printSnapshot(std::ostream& out, const Snapshot& value)
{
    out << "{\"read_before_ns\":" << value.before << ",\"read_after_ns\":" << value.after;
    for (const auto& entry : {std::make_pair("sdk", value.sdk), std::make_pair("worker", value.worker)}) {
        out << ",\"" << entry.first << "\":{\"available\":" << (entry.second.available ? "true" : "false");
        if (entry.second.available) {
            out << ",\"cpu_ticks\":" << entry.second.cpuTicks << ",\"threads\":" << entry.second.threads
                << ",\"rss_kib\":" << entry.second.rssKiB;
        }
        out << '}';
    }
    out << '}';
}
void printHistogram(std::ostream& out, const arrival::Histogram& hist)
{
    out << "{\"count\":" << hist.count << ",\"sum_ns\":\"" << arrival::decimal(hist.sumNs) << "\"";
    if (hist.count) out << ",\"mean_ns\":" << static_cast<long double>(hist.sumNs) / hist.count
        << ",\"min_ns\":" << hist.minimumNs << ",\"max_ns\":" << hist.maximumNs
        << ",\"p50_us_upper\":" << hist.percentile(50) << ",\"p99_us_upper\":" << hist.percentile(99);
    out << ",\"bins_us\":[";
    bool first = true;
    for (const auto& bin : hist.bins) {
        if (!first) out << ',';
        first = false; out << '[' << bin.first << ',' << bin.second << ']';
    }
    out << "]}";
}

struct Shared {
    explicit Shared(std::size_t callers) : readiness(callers) {}
    std::mutex mutex;
    std::condition_variable changed, workReady;
    arrival::CallerReadiness readiness;
    arrival::Ledger* active = nullptr;
    std::atomic<Ns>* starts = nullptr;
    bool stop = false, producerDone = false;
    std::size_t working = 0;
};

struct PrimingRecord {
    Ns begin = -1, end = -1, validationEnd = -1, readbackBegin = -1, readbackEnd = -1;
    int status = -1, readbackStatus = -1;
    bool byteValidated = false, exception = false;
};

PrimingRecord primeCaller(const Config& c, datasystem::KVClient& client, const std::string& key,
                         const std::string& payload, std::string& actual)
{
    PrimingRecord result;
    if (!c.primePerCaller) return result;
    try {
        result.begin = now();
        const auto status = c.operation == "get" ? client.Get(key, actual) : client.Set(key, payload);
        result.end = now(); result.status = static_cast<int>(status.GetCode());
        if (result.status == 0) {
            if (c.operation == "set") {
                result.readbackBegin = now();
                const auto readback = client.Get(key, actual);
                result.readbackEnd = now(); result.readbackStatus = static_cast<int>(readback.GetCode());
            }
            result.byteValidated = (c.operation == "get" || result.readbackStatus == 0) && actual == payload;
        }
    } catch (...) {
        result.exception = true;
        if (result.end < 0) result.end = now();
        if (result.readbackBegin >= 0 && result.readbackEnd < 0) result.readbackEnd = now();
    }
    result.validationEnd = now();
    return result;
}

void persistPriming(const Config& c, const std::vector<long>& tids,
                    const std::vector<PrimingRecord>& records, bool ready)
{
    std::ofstream out(c.output / "priming.json");
    out << "{\"enabled\":" << (c.primePerCaller ? "true" : "false")
        << ",\"all_callers_ready\":" << (ready ? "true" : "false")
        << ",\"operation\":\"" << c.operation << "\",\"excluded_from_arrivals\":true,\"callers\":[";
    for (std::size_t i = 0; i < records.size(); ++i) {
        const auto& r = records[i];
        if (i) out << ',';
        out << "{\"caller\":" << i << ",\"tid\":" << tids[i] << ",\"payload_bytes\":" << c.payload
            << ",\"skipped\":" << (c.primePerCaller ? "false" : "true")
            << ",\"configured_api_calls\":" << (r.begin >= 0 ? 1 : 0)
            << ",\"api_start_ns\":" << r.begin << ",\"api_end_ns\":" << r.end
            << ",\"status_code\":" << (r.begin < 0 ? "null" : std::to_string(r.status))
            << ",\"readback_api_calls\":" << (r.readbackBegin >= 0 ? 1 : 0)
            << ",\"readback_start_ns\":" << r.readbackBegin << ",\"readback_end_ns\":" << r.readbackEnd
            << ",\"readback_status_code\":" << (r.readbackBegin < 0 ? "null" : std::to_string(r.readbackStatus))
            << ",\"validation_end_ns\":" << r.validationEnd << ",\"bytes_validated\":";
        if (c.primePerCaller) out << (r.byteValidated ? "true" : "false");
        else out << "null";
        out << ",\"exception\":" << (r.exception ? "true" : "false") << '}';
    }
    out << "]}\n";
    out.close();
    if (!out) throw std::runtime_error("cannot persist priming proof");
}

bool cohort(const Config& c, Shared& shared, const std::string& label, const std::vector<long>& tids)
{
    const Ns duration = label == "warmup" ? c.warmup : c.duration;
    arrival::Ledger ledger(arrival::Plan(arrival::checkedTime(__int128(now()) + second), duration, c.rate, c.budget),
                           c.capacity, c.callers, c.maxLateness, c.burstLimit);
    std::vector<std::atomic<Ns>> starts(ledger.plan.count);
    for (auto& start : starts) start.store(-1, std::memory_order_relaxed);
    const Ns cut = arrival::checkedTime(__int128(ledger.plan.end) + c.drain);
    std::ofstream initial(c.output / (label + "-plan.json"));
    initial << "{\"planned\":" << ledger.plan.count << ",\"epoch_ns\":" << ledger.plan.epoch
            << ",\"end_ns\":" << ledger.plan.end << ",\"drain_cut_ns\":" << cut
            << ",\"rate\":" << c.rate
            << ",\"missing_final_receipt_means_unresolved\":true}\n";
    initial.close();
    if (!initial) throw std::runtime_error("cannot persist initial cohort plan");
    std::cout << "PHASE {\"cohort\":\"" << label << "\",\"begin_ns\":" << ledger.plan.epoch
              << ",\"end_ns\":" << ledger.plan.end << ",\"drain_cut_ns\":" << cut << "}\n" << std::flush;
    std::thread producer;
    try {
    {
        std::lock_guard<std::mutex> guard(shared.mutex);
        if (shared.active || shared.working) throw std::logic_error("previous cohort still active");
        shared.producerDone = false;
    }
    const auto cpuBeforeArrival = snapshot(c.workerPid);
    producer = std::thread([&] {
      try {
        affinity({c.producerCpu});
        std::size_t burst = 0;
        for (std::size_t id = 0; id < ledger.plan.count; ++id) {
            const auto target = ledger.plan.scheduled(id);
            if (now() < target) {
                if (burst) { std::lock_guard<std::mutex> lock(shared.mutex); ledger.burst(burst); }
                burst = 0;
                waitUntil(target, c.spin);
            }
            ++burst;
            const auto observed = now();
            {
                std::lock_guard<std::mutex> lock(shared.mutex);
                ledger.observe(id, observed, now());
            }
            shared.workReady.notify_one();
        }
        { std::lock_guard<std::mutex> lock(shared.mutex); ledger.burst(burst); shared.producerDone = true; }
        shared.changed.notify_all();
      } catch (...) {
        { std::lock_guard<std::mutex> guard(shared.mutex);
          ledger.generatorInvalid = true; shared.producerDone = true; }
        shared.changed.notify_all();
      }
    });
    {
        std::lock_guard<std::mutex> guard(shared.mutex);
        shared.active = &ledger; shared.starts = starts.data();
    }
    shared.workReady.notify_all();
    waitUntil(ledger.plan.epoch, 0);
    const auto cpuBegin = snapshot(c.workerPid);
    waitUntil(ledger.plan.end, 0);
    const auto cpuEnd = snapshot(c.workerPid);
    std::unique_lock<std::mutex> lock(shared.mutex);
    const bool drained = shared.changed.wait_until(lock, Clock::time_point(std::chrono::nanoseconds(cut)), [&] {
        return shared.producerDone && ledger.queue.empty() && shared.working == 0;
    });
    const Ns checkpoint = now();
    const auto cpuDrain = snapshot(c.workerPid);
    const auto counts = ledger.counts();
    arrival::Histogram generation, observation, dispatchWait, queueDelay, service, e2e, verification;
    std::uint64_t fixedStarts = 0, fixedCompletions = 0, phaseStarts = 0, phaseCompletions = 0;
    bool completedBeforeCut = true;
    std::ofstream raw(c.output / (label + "-arrivals.jsonl"));
    for (std::size_t id = 0; id < ledger.records.size(); ++id) {
        auto r = ledger.records[id];
        const auto liveStart = starts[id].load(std::memory_order_acquire);
        if (r.start < 0 && liveStart >= 0 && liveStart <= checkpoint) r.start = liveStart;
        if (r.observed >= 0) observation.add(static_cast<std::uint64_t>(r.observed - r.scheduled));
        if (r.enqueue >= 0) generation.add(static_cast<std::uint64_t>(r.enqueue - r.scheduled));
        if (r.start >= cpuBegin.after && r.start < cpuEnd.before) ++fixedStarts;
        if (r.end >= cpuBegin.after && r.end < cpuEnd.before) ++fixedCompletions;
        if (r.start >= ledger.plan.epoch && r.start < ledger.plan.end) ++phaseStarts;
        if (r.end >= ledger.plan.epoch && r.end < ledger.plan.end) ++phaseCompletions;
        if (r.end >= 0) {
            completedBeforeCut &= r.end <= cut && r.validated <= cut;
            dispatchWait.add(static_cast<std::uint64_t>(r.start - r.enqueue));
            queueDelay.add(static_cast<std::uint64_t>(r.start - r.scheduled));
            service.add(static_cast<std::uint64_t>(r.end - r.start));
            e2e.add(static_cast<std::uint64_t>(r.end - r.scheduled));
            verification.add(static_cast<std::uint64_t>(r.validated - r.end));
        }
        raw << "{\"id\":" << id << ",\"linux_tid\":" << (r.worker < 0 ? -1 : tids[static_cast<std::size_t>(r.worker)])
            << ",\"key_index\":" << (c.operation == "get" ? static_cast<long>(id % std::max<std::size_t>(64, c.callers))
                                                            : r.worker)
            << ",\"state\":\"" << arrival::name(r.state) << "\",\"rejection\":" << static_cast<int>(r.rejection)
            << ",\"scheduled_ns\":" << r.scheduled << ",\"deadline_ns\":" << r.deadline
            << ",\"observed_ns\":" << r.observed << ",\"enqueue_ns\":" << r.enqueue
            << ",\"dequeue_ns\":" << r.dequeue << ",\"logical_dispatch_ns\":" << r.dispatch
            << ",\"api_start_ns\":" << r.start << ",\"api_end_ns\":" << r.end
            << ",\"validation_end_ns\":" << r.validated << ",\"status_code\":"
            << (r.end < 0 ? "null" : std::to_string(r.statusCode)) << "}\n";
    }
    const bool valid = drained && completedBeforeCut && counts.valid() && !ledger.generatorInvalid
        && generation.count == ledger.plan.count
        && generation.percentile(99) * 1000 <= std::uint64_t(c.p99Lateness)
        && cpuBegin.sdk.available && cpuBegin.worker.available && cpuEnd.sdk.available && cpuEnd.worker.available
        && cpuDrain.sdk.available && cpuDrain.worker.available;
    std::ofstream summary(c.output / (label + "-summary.json"));
    summary << std::setprecision(18) << "{\"api_arrival_valid\":" << (valid ? "true" : "false")
        << ",\"rpc_csend_crecv_available\":false,\"rpc_goal_eligible_without_external_probe\":false"
        << ",\"drained\":" << (drained ? "true" : "false") << ",\"planned\":" << counts.planned
        << ",\"completions_before_drain_cut\":" << (completedBeforeCut ? "true" : "false")
        << ",\"producer_observed\":" << counts.observed << ",\"producer_unissued\":" << counts.unissued
        << ",\"queue_admitted\":" << counts.admitted << ",\"queue_rejected\":" << counts.rejected
        << ",\"dispatched\":" << counts.dispatched << ",\"expired_in_queue\":" << counts.expired
        << ",\"pending_in_queue\":" << counts.pendingQueue << ",\"pending_inflight\":" << counts.pendingInflight
        << ",\"timely_success\":" << counts.timely << ",\"late_success\":" << counts.late
        << ",\"rpc_error\":" << counts.errors << ",\"mismatch\":" << counts.mismatches
        << ",\"conserved\":" << (counts.conserved() ? "true" : "false")
        << ",\"queue_high_water\":" << ledger.queueHighWater << ",\"inflight_high_water\":" << ledger.inflightHighWater
        << ",\"maximum_burst\":" << ledger.maximumBurst
        << ",\"maximum_generation_lateness_ns\":" << ledger.maximumLateness
        << ",\"phase_begin_ns\":" << ledger.plan.epoch << ",\"phase_end_ns\":" << ledger.plan.end
        << ",\"checkpoint_ns\":" << checkpoint << ",\"record_bytes\":" << ledger.records.size() * sizeof(arrival::Record)
        << ",\"offered_api_ops_s\":" << static_cast<long double>(counts.planned) * second / duration
        << ",\"admitted_per_arrival_second\":" << static_cast<long double>(counts.admitted) * second / duration
        << ",\"dispatched_per_arrival_second\":" << static_cast<long double>(counts.dispatched) * second / duration
        << ",\"cohort_completed_per_arrival_second\":"
        << static_cast<long double>(counts.timely + counts.late + counts.errors + counts.mismatches) * second / duration
        << ",\"timely_success_per_arrival_second\":" << static_cast<long double>(counts.timely) * second / duration
        << ",\"actual_api_starts_in_fixed_phase\":" << phaseStarts
        << ",\"actual_api_completions_in_fixed_phase\":" << phaseCompletions
        << ",\"actual_completions_s_fixed_phase\":" << static_cast<long double>(phaseCompletions) * second / duration
        << ",\"fixed_cpu_interval_api_starts\":" << fixedStarts
        << ",\"fixed_cpu_interval_api_completions\":" << fixedCompletions
        << ",\"clock_ticks_per_second\":" << sysconf(_SC_CLK_TCK) << ",\"cpu_begin\":";
    printSnapshot(summary, cpuBegin); summary << ",\"cpu_fixed_end\":"; printSnapshot(summary, cpuEnd);
    summary << ",\"cpu_pre_arrival\":"; printSnapshot(summary, cpuBeforeArrival);
    summary << ",\"cpu_cohort_drain_end\":"; printSnapshot(summary, cpuDrain);
    const std::pair<const char*, const arrival::Histogram*> histograms[]{
        {"producer_observation_lateness", &observation}, {"generation_lateness", &generation},
        {"dispatch_wait", &dispatchWait}, {"queue_delay", &queueDelay}, {"api_service", &service},
        {"arrival_e2e", &e2e}, {"driver_byte_validation", &verification}};
    for (const auto& item : histograms) {
        summary << ",\"" << item.first << "\":"; printHistogram(summary, *item.second);
    }
    summary << "}\n";
    raw.flush(); summary.flush();
    if (!raw || !summary) throw std::runtime_error("evidence write failed");
    if (!drained) {
        // Synchronous SDK work cannot safely be detached from its shared client.
        std::cerr << "bounded drain expired; pending receipt persisted; terminating callers\n" << std::flush;
        std::_Exit(3);
    }
    shared.active = nullptr; shared.starts = nullptr;
    lock.unlock(); producer.join();
    return valid;
    } catch (...) {
        // Preserve the initial planned denominator and fail the process before shared callers can outlive this ledger.
        std::cerr << "cohort failed; initial plan retained, final accounting unresolved\n" << std::flush;
        std::_Exit(4);
    }
}

int run(int argc, char** argv)
{
    const auto c = options(argc, argv);
    if (std::filesystem::exists(c.output)) throw std::invalid_argument("evidence directory already exists");
    std::filesystem::create_directories(c.output);
    affinity(c.sdkCpus);
    cpu_set_t sdkMask;
    CPU_ZERO(&sdkMask);
    for (int cpu : c.sdkCpus) CPU_SET(cpu, &sdkMask);
    for (const auto& entry : std::filesystem::directory_iterator("/proc/self/task")) {
        const auto tid = static_cast<pid_t>(number(entry.path().filename().string(), 1, 2147483647));
        if (sched_setaffinity(tid, sizeof(sdkMask), &sdkMask) != 0 && errno != ESRCH) {
            throw std::runtime_error("cannot reserve producer CPU from existing SDK threads");
        }
    }
    datasystem::ConnectOptions connect{};
    connect.host = c.host; connect.port = c.port;
    connect.requestTimeoutMs = c.sdkTimeoutMs; connect.connectTimeoutMs = 5000;
    datasystem::KVClientConfig sdkConfig;
    if (datasystem::KVClientConfig::Builder().LogDir(c.logDir).MinLogLevel(2).LogMonitorEnable(false)
        .Build(sdkConfig).IsError()) throw std::runtime_error("SDK config failed");
    datasystem::KVClient client(connect);
    if (client.Init(sdkConfig).IsError()) throw std::runtime_error("SDK Init failed");
    const std::string payload(c.payload, 'p');
    std::vector<std::string> keys;
    for (std::size_t key = 0; key < std::max<std::size_t>(64, c.callers); ++key) {
        keys.push_back("rpc-paced-key-" + std::to_string(key));
        std::string actual;
        if (client.Set(keys.back(), payload).IsError() || client.Get(keys.back(), actual).IsError() || actual != payload) {
            throw std::runtime_error("seed/full-byte precheck failed");
        }
    }
    std::ofstream config(c.output / "config.json");
    config << "{\"schema\":1,\"operation\":\"" << c.operation << "\",\"rate_api_ops_per_second\":" << c.rate
        << ",\"callers\":" << c.callers << ",\"queue_capacity\":" << c.capacity << ",\"payload_bytes\":" << c.payload
        << ",\"prime_per_caller\":" << (c.primePerCaller ? "true" : "false")
        << ",\"seeded_key_count\":" << keys.size() << ",\"get_key_policy\":\"arrival_id modulo seeded_key_count\""
        << ",\"measure_ns\":" << c.duration << ",\"warmup_ns\":" << c.warmup << ",\"arrival_budget_ns\":" << c.budget
        << ",\"drain_ns\":" << c.drain << ",\"sdk_fixed_timeout_ms\":" << c.sdkTimeoutMs
        << ",\"remaining_budget_passed_to_sdk\":false,\"spin_ns\":" << c.spin << ",\"producer_cpu\":" << c.producerCpu
        << ",\"sdk_cpus\":[";
    for (std::size_t i = 0; i < c.sdkCpus.size(); ++i) {
        if (i) config << ',';
        config << c.sdkCpus[i];
    }
    config << "],\"sdk_cpu_count\":7,\"total_sdk_physical_cpus\":8,\"maximum_lateness_ns\":" << c.maxLateness
        << ",\"p99_lateness_ns\":" << c.p99Lateness << ",\"maximum_burst\":" << c.burstLimit
        << ",\"latency_histogram\":\"1us then <=0.1percent upper bounds; merge bins,not windowP99\"}\n";
    config.close();
    if (!config) throw std::runtime_error("cannot persist config");
    Shared shared(c.callers);
    std::vector<long> tids(c.callers, -1);
    std::vector<PrimingRecord> priming(c.callers);
    std::vector<std::thread> callers;
    const auto stop = [&] {
        { std::lock_guard<std::mutex> lock(shared.mutex); shared.stop = true; }
        shared.workReady.notify_all();
        for (auto& caller : callers) if (caller.joinable()) caller.join();
    };
    bool valid = false, primingReady = false, warmupStarted = false, measureStarted = false;
    try {
        callers.reserve(c.callers);
        for (std::size_t worker = 0; worker < c.callers; ++worker) {
            callers.emplace_back([&, worker] {
                std::string actual;
                actual.reserve(c.payload);
                std::unique_lock<std::mutex> lock(shared.mutex);
                tids[worker] = syscall(SYS_gettid);
                shared.readiness.Register(worker);
                lock.unlock();
                const auto setup = primeCaller(c, client, keys[worker], payload, actual);
                lock.lock();
                priming[worker] = setup;
                shared.readiness.Complete(worker, !c.primePerCaller
                    || (!setup.exception && setup.status == 0 && setup.byteValidated));
                shared.changed.notify_all();
                for (;;) {
                    shared.workReady.wait(lock, [&] {
                        return shared.stop || (shared.active && !shared.active->queue.empty());
                    });
                    if (shared.stop) return;
                    auto& ledger = *shared.active;
                    std::size_t id;
                    if (!ledger.reserve(now(), static_cast<int>(worker), id)) continue;
                    if (!ledger.dispatch(id, now())) { shared.changed.notify_all(); continue; }
                    ++shared.working;
                    auto* liveStart = shared.starts;
                    lock.unlock();
                    const auto key = c.operation == "get" ? id % keys.size() : worker;
                    const auto begin = now();
                    if (begin >= ledger.records[id].deadline) {
                        lock.lock(); ledger.expireBeforeApi(id, begin); --shared.working;
                        shared.changed.notify_all(); continue;
                    }
                    liveStart[id].store(begin, std::memory_order_release);
                    int code = -1;
                    Ns end;
                    bool mismatch = false;
                    try {
                        const auto status = c.operation == "get"
                            ? client.Get(keys[key], actual) : client.Set(keys[key], payload);
                        end = now(); code = static_cast<int>(status.GetCode());
                        mismatch = code == 0 && c.operation == "get" && actual != payload;
                    } catch (const std::exception&) { end = now(); }
                    const auto validated = now();
                    lock.lock();
                    ledger.complete(id, begin, end, validated, code != 0 ? arrival::Outcome::RpcError
                        : mismatch ? arrival::Outcome::Mismatch : arrival::Outcome::Success, code);
                    --shared.working; shared.changed.notify_all();
                }
            });
        }
        {
            std::unique_lock<std::mutex> lock(shared.mutex);
            shared.changed.wait(lock, [&] { return shared.readiness.AllComplete(); });
            primingReady = shared.readiness.ClaimReady();
        }
        persistPriming(c, tids, priming, primingReady);
        std::ofstream workers(c.output / "caller-tids.json");
        workers << "{\"linux_tids\":[";
        for (std::size_t i = 0; i < tids.size(); ++i) {
            if (i) workers << ',';
            workers << tids[i];
        }
        workers << "]}\n";
        workers.close();
        if (!workers) throw std::runtime_error("cannot persist caller TIDs");
        if (primingReady) {
            warmupStarted = true;
            valid = cohort(c, shared, "warmup", tids);
            if (valid) {
                measureStarted = true;
                valid = cohort(c, shared, "measure", tids);
            }
        }
    } catch (...) { stop(); throw; }
    stop();
    if (primingReady && c.operation == "set") {
        for (const auto& key : keys) {
            std::string actual;
            if (client.Get(key, actual).IsError() || actual != payload) valid = false;
        }
    }
    const bool shutdown = client.ShutDown().IsOk();
    std::ofstream final(c.output / "driver-final.json");
    final << "{\"api_arrival_valid\":" << (valid && shutdown ? "true" : "false")
          << ",\"sdk_shutdown_ok\":" << (shutdown ? "true" : "false")
          << ",\"priming_ready\":" << (primingReady ? "true" : "false")
          << ",\"warmup_started\":" << (warmupStarted ? "true" : "false")
          << ",\"measure_started\":" << (measureStarted ? "true" : "false")
          << ",\"rpc_goal_eligible_without_external_probe\":false}\n";
    final.close();
    if (!final) throw std::runtime_error("cannot persist final receipt");
    return valid && shutdown ? 0 : 2;
}
}  // namespace

int main(int argc, char** argv)
{
    try { return run(argc, argv); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
