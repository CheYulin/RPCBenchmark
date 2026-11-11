#include "capture_abi.h"
#include "capture_observe.h"

#include <atomic>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <exception>
#include <functional>
#include <iostream>
#include <mutex>
#include <new>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

thread_local bool trap_allocations = false;
thread_local uint64_t trapped_allocations = 0;
// Keep replacement allocation interceptors out of line for GNU11's allocation-pair diagnostics.
__attribute__((noinline)) void* operator new(std::size_t bytes)
{
    if (trap_allocations) ++trapped_allocations;
    if (void* p = std::malloc(bytes ? bytes : 1)) return p;
    throw std::bad_alloc();
}
__attribute__((noinline)) void* operator new[](std::size_t bytes) { return ::operator new(bytes); }
__attribute__((noinline)) void operator delete(void* p) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete[](void* p) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete(void* p, std::size_t) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)
template <typename T> T Header()
{
    T value{}; value.abi = DS_CAPTURE_ABI; value.bytes = sizeof(T); return value;
}
using Emit = void (*)(const ds_bench_capture::RpcObservation*) noexcept;
using Install = void (*)(ds_bench_capture::TestHook, void*) noexcept;

struct Library {
    void* control;
    void* sdk;
    const DsBenchApiV2* api;
    Emit raw_emit;
    Install install;
    bool same_image = false;
    bool same_provider = false;
    Library(const char* control_path, const char* sdk_path)
    {
        control = dlopen(control_path, RTLD_NOW | RTLD_LOCAL);
        if (!control) throw std::runtime_error(dlerror());
        sdk = std::strcmp(control_path, sdk_path) == 0 ? control : dlopen(sdk_path, RTLD_NOW | RTLD_LOCAL);
        if (!sdk) throw std::runtime_error(dlerror());
        auto get = reinterpret_cast<const DsBenchApiV2* (*)() noexcept>(dlsym(control, "DsBenchRpcCaptureV2"));
        if (!get) throw std::runtime_error("missing getter is not semantic RED");
        api = get();
        if (!api || api->abi != DS_CAPTURE_ABI || api->bytes != sizeof(DsBenchApiV2)) throw std::runtime_error("getter ABI/bytes mismatch");
        raw_emit = reinterpret_cast<Emit>(dlsym(sdk, "DsBenchFakeSdkEmitV2"));
        install = reinterpret_cast<Install>(dlsym(sdk, "DsBenchFakeInstallHookV2"));
        CHECK(raw_emit && install);
        Dl_info control_image{}, sdk_image{};
        CHECK(dladdr(reinterpret_cast<void*>(get), &control_image) && dladdr(reinterpret_cast<void*>(raw_emit), &sdk_image));
        same_image = control_image.dli_fbase == sdk_image.dli_fbase;
        same_provider = dlsym(sdk, "DsBenchRpcCaptureV2") == reinterpret_cast<void*>(get);
        CHECK(same_provider);
    }
    void emit(uint64_t send, uint64_t recv, uint16_t method, int32_t controller, int32_t ds,
              uint64_t processing = 0, uint64_t execution = 0,
              ds_bench_capture::SpanState processing_state = ds_bench_capture::SpanState::Missing,
              ds_bench_capture::SpanState execution_state = ds_bench_capture::SpanState::Missing) const noexcept
    {
        const ds_bench_capture::RpcObservation rpc{send, recv, method, controller != 0 || ds != 0,
            controller, ds, processing, execution, processing_state, execution_state};
        raw_emit(&rpc);
    }
    ~Library() { if (sdk != control) dlclose(sdk); dlclose(control); }
};

DsCaptureRunConfigV2 Config(uint32_t callers = 1)
{
    auto c = Header<DsCaptureRunConfigV2>();
    c.callers = callers; c.mode = DS_CAPTURE_ENABLED;
    c.normal_records = c.normal_ends = 512;
    c.error_records = c.error_ends = 256;
    c.max_rpc_per_api = 8; c.sample_modulus = 1; c.sample_seed = 17;
    c.memory_budget_bytes = 16 * 1024 * 1024;
    return c;
}
struct Loan {
    std::vector<DsCaptureSampleV2> records;
    std::vector<DsCaptureApiSummaryV2> ends;
    DsCaptureBuffersV2 span;
    explicit Loan(const DsCaptureRunConfigV2& config)
        : records(uint64_t(config.normal_records) + config.error_records),
          ends(uint64_t(config.normal_ends) + config.error_ends), span(Header<DsCaptureBuffersV2>())
    {
        span.records = records.data(); span.ends = ends.data();
        span.record_capacity = records.size(); span.end_capacity = ends.size();
    }
};
struct Rig {
    Library& library;
    DsCaptureRunV2 run = Header<DsCaptureRunV2>();
    DsCaptureRunConfigV2 config;
    std::vector<Loan> loans;
    void* own = nullptr;
    bool closed = false;
    Rig(Library& lib, DsCaptureRunConfigV2 c = Config()) : library(lib), config(c)
    {
        loans.reserve(c.callers);
        for (uint32_t i = 0; i < c.callers; ++i) loans.emplace_back(c);
        CHECK(library.api->open(&config, &run) == DS_CAPTURE_OK);
    }
    void* Register(uint32_t i = 0)
    {
        void* slot = nullptr;
        CHECK(library.api->register_caller(&run, i, 1000 + i, &loans[i].span, &slot) == DS_CAPTURE_OK);
        return slot;
    }
    int Arm()
    {
        auto a = Header<DsCaptureArmV2>(); a.expected_callers = config.callers; a.generation = run.generation;
        return library.api->arm(&run, &a);
    }
    DsCaptureSnapshotV2 Snapshot(uint32_t mode = DS_CAPTURE_NORMAL)
    {
        auto s = Header<DsCaptureSnapshotV2>(); CHECK(library.api->snapshot(&run, mode, &s) == DS_CAPTURE_OK); return s;
    }
    DsCaptureSealV2 Seal(const DsCaptureSnapshotV2& snapshot)
    {
        auto s = Header<DsCaptureSealV2>(); CHECK(library.api->disarm(&run, &snapshot, &s) == DS_CAPTURE_OK); return s;
    }
    void Close()
    {
        CHECK(library.api->close(&run) == DS_CAPTURE_OK); closed = true;
    }
    ~Rig()
    {
        if (closed) return;
        auto snap = Header<DsCaptureSnapshotV2>();
        if (own) {
            if (library.api->snapshot(&run, DS_CAPTURE_PARTIAL, &snap) == 0 && snap.slots[0].active) {
                auto end = Header<DsCaptureApiEndV2>(); end.api_end_ns = end.validation_end_ns = UINT64_MAX;
                (void)library.api->end_api(own, &end);
            }
        }
        if (library.api->snapshot(&run, DS_CAPTURE_NORMAL, &snap) == 0) {
            auto seal = Header<DsCaptureSealV2>(); (void)library.api->disarm(&run, &snap, &seal);
        }
        if (own) (void)library.api->unregister_caller(own);
        if (library.api->close(&run) != 0) { std::cerr << "unreleased fixture owner\n"; std::_Exit(4); }
    }
};
DsCaptureApiContextV2 Context(const Rig& rig, uint64_t id = 0, uint64_t phase = 1, uint64_t methods = uint64_t{1} << DS_METHOD_GET)
{
    auto c = Header<DsCaptureApiContextV2>();
    c.generation = rig.run.generation; c.phase_id = phase; c.arrival_id = id;
    c.scheduled_ns = 1000 + id * 1000; c.api_begin_ns = c.scheduled_ns + 10; c.required_methods = methods;
    return c;
}
void End(Rig& rig, void* slot, const DsCaptureApiContextV2& c, int32_t outcome = 0)
{
    auto e = Header<DsCaptureApiEndV2>(); e.api_end_ns = c.api_begin_ns + 100;
    e.validation_end_ns = e.api_end_ns + 2; e.outcome = outcome;
    CHECK(rig.library.api->end_api(slot, &e) == DS_CAPTURE_OK);
}
void EmitGet(Rig& rig, void* slot, uint64_t id = 0, int32_t outcome = 0, uint64_t phase = 1)
{
    const auto c = Context(rig, id, phase);
    CHECK(rig.library.api->begin_api(slot, &c) == DS_CAPTURE_OK);
    const auto allocations = trapped_allocations;
    trap_allocations = true;
    rig.library.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, outcome, outcome);
    trap_allocations = false;
    CHECK(trapped_allocations == allocations);
    End(rig, slot, c, outcome);
}

int Contract(Library& lib)
{
    Rig rig(lib); rig.own = rig.Register(); CHECK(rig.Arm() == 0);
    const auto c = Context(rig, 7, 9, uint64_t{1} << DS_METHOD_PUBLISH);
    CHECK(lib.api->begin_api(rig.own, &c) == 0);
    const auto allocations = trapped_allocations; trap_allocations = true;
    lib.emit(c.api_begin_ns + 1, c.api_begin_ns + 2, DS_METHOD_CREATE, 0, 0);
    lib.emit(c.api_begin_ns + 3, c.api_begin_ns + 4, DS_METHOD_PUBLISH, 0, 0);
    trap_allocations = false;
    const bool allocation_free = allocations == trapped_allocations;
    End(rig, rig.own, c);
    auto snapshot = rig.Snapshot(); auto seal = rig.Seal(snapshot);
    const auto& v = snapshot.slots[0];
    const bool valid = seal.valid_capture && v.record_count == 2 && v.end_count == 1
        && v.records[0].rpc_ordinal == 1 && v.records[1].rpc_ordinal == 2
        && v.records[0].arrival_id == 7 && v.records[1].arrival_id == 7;
    std::cout << "CONTRACT records=" << v.record_count << " ends=" << v.end_count << " errors=" << v.errors
              << " valid=" << seal.valid_capture << " same_dso=" << lib.same_image << " same_provider=" << lib.same_provider
              << " allocations=" << (trapped_allocations - allocations) << '\n';
    CHECK(lib.api->unregister_caller(rig.own) == 0); rig.own = nullptr; rig.Close();
    if (!allocation_free) { std::cerr << "FAIL RPC allocation contract\n"; return 1; }
    if (!valid) { std::cerr << "FAIL cross-DSO primary sample contract: missing samples or owner scope\n"; return 1; }
    std::cout << "PASS cross-DSO primary sample contract\n"; return 0;
}

struct Workers {
    Rig& rig;
    std::mutex mutex;
    std::condition_variable cv;
    uint32_t ready = 0, done = 0;
    bool go = false, release = false;
    std::exception_ptr error;
    std::vector<std::thread> threads;
    Workers(Rig& r, const std::function<void(uint32_t, void*)>& work) : rig(r)
    {
        for (uint32_t i = 0; i < rig.config.callers; ++i) threads.emplace_back([this, i, work] {
            void* slot = nullptr;
            try { slot = rig.Register(i); }
            catch (...) { std::lock_guard<std::mutex> lock(mutex); error = std::current_exception(); }
            {
                std::unique_lock<std::mutex> lock(mutex); ++ready; cv.notify_all();
                cv.wait(lock, [&] { return go || release; });
            }
            try { if (slot && go) work(i, slot); }
            catch (...) { std::lock_guard<std::mutex> lock(mutex); error = std::current_exception(); }
            {
                std::unique_lock<std::mutex> lock(mutex); ++done; cv.notify_all();
                cv.wait(lock, [&] { return release; });
            }
            if (slot && rig.library.api->unregister_caller(slot) != 0) {
                std::lock_guard<std::mutex> lock(mutex); error = std::make_exception_ptr(std::runtime_error("unregister"));
            }
        });
        std::unique_lock<std::mutex> lock(mutex); cv.wait(lock, [&] { return ready == rig.config.callers; });
    }
    void Go() { std::lock_guard<std::mutex> lock(mutex); go = true; cv.notify_all(); }
    void Done() { std::unique_lock<std::mutex> lock(mutex); cv.wait(lock, [&] { return done == rig.config.callers; }); }
    void Join()
    {
        { std::lock_guard<std::mutex> lock(mutex); release = true; cv.notify_all(); }
        for (auto& t : threads) if (t.joinable()) t.join();
        if (error) std::rethrow_exception(error);
    }
    ~Workers() { for (auto& t : threads) if (t.joinable()) std::_Exit(4); }
};

void SetupAndDisabled(Library& lib)
{
    Rig rig(lib); lib.emit(1, 2, DS_METHOD_GET, 0, 0);
    rig.own = rig.Register(); lib.emit(1, 2, DS_METHOD_GET, 0, 0);
    auto before = rig.Snapshot(); CHECK(before.global_errors == 0 && before.slots[0].records == rig.loans[0].records.data());
    CHECK(before.slots[0].record_count == 0 && rig.Arm() == 0);
    CHECK(rig.Arm() == DS_CAPTURE_BUSY);
    auto prearm_seal = Header<DsCaptureSealV2>();
    CHECK(lib.api->disarm(&rig.run, &before, &prearm_seal) == DS_CAPTURE_CHANGED && !prearm_seal.sealed);
    EmitGet(rig, rig.own, 1, 0, 1); EmitGet(rig, rig.own, 2, 0, 2);
    auto snapshot = rig.Snapshot(); CHECK(snapshot.slots[0].record_count == 2 && snapshot.slots[0].end_count == 2);
    CHECK(snapshot.slots[0].records[0].phase_id == 1 && snapshot.slots[0].records[1].phase_id == 2);
    CHECK(rig.Seal(snapshot).valid_capture);
    lib.emit(1, 2, DS_METHOD_GET, 0, 0);
    CHECK(rig.Snapshot().slots[0].record_count == 2);
    CHECK(lib.api->unregister_caller(rig.own) == 0); rig.own = nullptr; rig.Close();
    auto config = Config(); config.mode = DS_CAPTURE_DISABLED;
    Rig disabled(lib, config); disabled.own = disabled.Register(); CHECK(disabled.Arm() == 0);
    EmitGet(disabled, disabled.own);
    auto view = disabled.Snapshot(); CHECK(view.slots[0].record_count == 0 && view.slots[0].rpc_count == 0);
    CHECK((view.slots[0].ends[0].flags & DS_SAMPLE_DISABLED) && view.slots[0].errors == 0);
    CHECK(!disabled.Seal(view).valid_capture);
}

std::set<uint64_t> Sampling(Library& lib, bool skew)
{
    auto config = Config(2); config.sample_modulus = 4;
    Rig rig(lib, config);
    Workers workers(rig, [&](uint32_t i, void* slot) {
        for (uint64_t id = 0; id < 100; ++id) {
            if ((skew ? uint32_t{0} : uint32_t(id % 2)) != i) continue;
            const auto c = Context(rig, id, 3, uint64_t{1} << DS_METHOD_PUBLISH);
            CHECK(lib.api->begin_api(slot, &c) == 0);
            lib.emit(c.api_begin_ns + 1, c.api_begin_ns + 2, DS_METHOD_CREATE, 0, 0);
            lib.emit(c.api_begin_ns + 3, c.api_begin_ns + 4, DS_METHOD_PUBLISH, 0, 0);
            End(rig, slot, c);
        }
    });
    CHECK(rig.Arm() == 0); workers.Go(); workers.Done();
    auto snapshot = rig.Snapshot(); const auto seal = rig.Seal(snapshot); workers.Join();
    CHECK(seal.valid_capture);
    std::set<uint64_t> selected; uint64_t total = 0;
    for (uint32_t i = 0; i < 2; ++i) {
        const auto& view = snapshot.slots[i]; total += view.rpc_count;
        CHECK(view.cached_tid == 1000 + i && view.counters_available && view.record_count == view.end_count * 2);
        for (uint64_t j = 0; j < view.end_count; ++j) {
            CHECK(view.ends[j].rpc_count == 2 && view.ends[j].caller_index == i);
            CHECK(view.records[j * 2].rpc_ordinal == 1 && view.records[j * 2 + 1].rpc_ordinal == 2);
            selected.insert(view.ends[j].arrival_id);
        }
    }
    CHECK(total == 200 && !selected.empty()); rig.Close(); return selected;
}
void FailureAndOverflow(Library& lib)
{
    auto config = Config(); config.sample_modulus = 4;
    Rig rig(lib, config); rig.own = rig.Register(); CHECK(rig.Arm() == 0);
    for (uint64_t id = 0; id < 100; ++id) EmitGet(rig, rig.own, id, 7);
    auto snapshot = rig.Snapshot(); const auto& view = snapshot.slots[0];
    CHECK(view.record_count == 100 && view.end_count == 100 && view.rpc_failures == 100);
    bool unselected = false;
    for (uint64_t i = 0; i < 100; ++i) {
        CHECK(view.records[i].arrival_id == i && (view.records[i].flags & DS_SAMPLE_RPC_FAILED));
        unselected |= !(view.records[i].flags & DS_SAMPLE_SELECTED);
    }
    CHECK(unselected && !rig.Seal(snapshot).valid_capture);
    CHECK(lib.api->unregister_caller(rig.own) == 0); rig.own = nullptr; rig.Close();
    config = Config(); config.normal_records = 1; config.error_records = 1;
    Rig limited(lib, config); limited.own = limited.Register(); CHECK(limited.Arm() == 0);
    const auto c = Context(limited); CHECK(lib.api->begin_api(limited.own, &c) == 0);
    lib.emit(c.api_begin_ns + 1, c.api_begin_ns + 2, DS_METHOD_GET, 0, 0);
    lib.emit(c.api_begin_ns + 3, c.api_begin_ns + 4, DS_METHOD_GET, 0, 0);
    lib.emit(c.api_begin_ns + 5, c.api_begin_ns + 6, DS_METHOD_GET, 9, 9);
    End(limited, limited.own, c, 9); snapshot = limited.Snapshot();
    CHECK(snapshot.slots[0].record_count == 2 && snapshot.slots[0].rpc_count == 3);
    CHECK(snapshot.slots[0].records[1].controller_error == 9 && (snapshot.slots[0].errors & DS_ERROR_RECORD_OVERFLOW));
    CHECK(!limited.Seal(snapshot).valid_capture);
}

struct Gate {
    std::mutex mutex; std::condition_variable cv;
    ds_bench_capture::TestPoint point;
    bool entered = false, release = false;
    explicit Gate(ds_bench_capture::TestPoint p) : point(p) {}
    static void Hook(ds_bench_capture::TestPoint point, void* context)
    {
        auto& self = *static_cast<Gate*>(context);
        if (point != self.point) return;
        std::unique_lock<std::mutex> lock(self.mutex); self.entered = true; self.cv.notify_all();
        self.cv.wait(lock, [&] { return self.release; });
    }
    void Entered() { std::unique_lock<std::mutex> lock(mutex); cv.wait(lock, [&] { return entered; }); }
    void Release() { std::lock_guard<std::mutex> lock(mutex); release = true; cv.notify_all(); }
};
int AdmissionContract(Library& lib)
{
    Rig rig(lib); rig.own = rig.Register(); CHECK(rig.Arm() == 0);
    const auto old = rig.Snapshot();
    Gate gate(ds_bench_capture::TestPoint::ErrorAdmitted); lib.install(Gate::Hook, &gate);
    std::thread error([&] { lib.emit(10, 20, DS_METHOD_GET, 0, 0); }); gate.Entered();
    auto output = Header<DsCaptureSealV2>();
    const int pending = lib.api->disarm(&rig.run, &old, &output);
    const int close_pending = lib.api->close(&rig.run);
    gate.Release(); error.join(); lib.install(nullptr, nullptr);
    int changed = -1; bool retained_error = false, invalid_seal = false;
    if (pending != DS_CAPTURE_OK) {
        auto retry = Header<DsCaptureSealV2>();
        changed = lib.api->disarm(&rig.run, &old, &retry);
        const auto final = rig.Snapshot();
        retained_error = (final.global_errors & DS_ERROR_SCOPE)
            && final.global_error_generation > old.global_error_generation;
        invalid_seal = !rig.Seal(final).valid_capture;
    }
    const bool valid = pending == DS_CAPTURE_BUSY && !output.sealed && close_pending == DS_CAPTURE_BUSY
        && changed == DS_CAPTURE_CHANGED && retained_error && invalid_seal;
    std::cout << "ADMISSION paused_disarm=" << pending << " sealed=" << output.sealed
              << " valid=" << output.valid_capture << " counterexample=" << (output.sealed && output.valid_capture)
              << " retry=" << changed << " retained_error=" << retained_error << '\n';
    CHECK(lib.api->unregister_caller(rig.own) == 0); rig.own = nullptr; rig.Close();
    if (!valid) { std::cerr << "FAIL exceptional admission seal contract\n"; return 1; }
    std::cout << "PASS exceptional admission seal contract\n"; return 0;
}
void PrefixPublication(Library& lib)
{
    Rig rig(lib); Gate gate(ds_bench_capture::TestPoint::BeforeRecordPublish); lib.install(Gate::Hook, &gate);
    Workers workers(rig, [&](uint32_t, void* slot) { EmitGet(rig, slot); });
    CHECK(rig.Arm() == 0); workers.Go(); gate.Entered();
    auto partial = rig.Snapshot(DS_CAPTURE_PARTIAL);
    auto normal = Header<DsCaptureSnapshotV2>(); const int busy = lib.api->snapshot(&rig.run, DS_CAPTURE_NORMAL, &normal);
    auto seal = Header<DsCaptureSealV2>(); const int disarm_busy = lib.api->disarm(&rig.run, &partial, &seal);
    const int close_busy = lib.api->close(&rig.run);
    gate.Release(); workers.Done(); lib.install(nullptr, nullptr);
    normal = rig.Snapshot(); auto final = rig.Seal(normal); workers.Join();
    CHECK(partial.slots[0].record_count == 0 && !partial.slots[0].counters_available && partial.slots[0].active);
    CHECK(busy == DS_CAPTURE_BUSY && disarm_busy == DS_CAPTURE_BUSY && close_busy == DS_CAPTURE_BUSY);
    CHECK(normal.slots[0].record_count == 1 && normal.slots[0].end_count == 1 && final.valid_capture);
    CHECK(normal.slots[0].records[0].send_ns == 1020 && normal.slots[0].records[0].recv_ns == 1030);
    rig.Close();
}
void AcquireWhileActive(Library& lib)
{
    struct RelaxedGate {
        std::atomic<bool> entered{false}, release{false};
        static void Hook(ds_bench_capture::TestPoint point, void* context)
        {
            if (point != ds_bench_capture::TestPoint::AfterRecordPublish) return;
            auto& gate = *static_cast<RelaxedGate*>(context); gate.entered.store(true, std::memory_order_relaxed);
            while (!gate.release.load(std::memory_order_relaxed)) std::this_thread::yield();
        }
    } gate;
    Rig rig(lib); lib.install(RelaxedGate::Hook, &gate);
    Workers workers(rig, [&](uint32_t, void* slot) { EmitGet(rig, slot); });
    CHECK(rig.Arm() == 0); workers.Go();
    while (!gate.entered.load(std::memory_order_relaxed)) std::this_thread::yield();
    auto prefix = rig.Snapshot(DS_CAPTURE_PARTIAL);
    while (prefix.slots[0].record_count == 0) prefix = rig.Snapshot(DS_CAPTURE_PARTIAL);
    CHECK(prefix.slots[0].record_count == 1);
    const auto row = prefix.slots[0].records[0];
    gate.release.store(true, std::memory_order_relaxed); workers.Done(); lib.install(nullptr, nullptr);
    const auto final = rig.Snapshot(); const auto seal = rig.Seal(final); workers.Join();
    CHECK(prefix.slots[0].record_count == 1 && prefix.slots[0].end_count == 0 && prefix.slots[0].active);
    CHECK(!prefix.slots[0].counters_available && prefix.slots[0].rpc_count == 0);
    CHECK(row.phase_id == 1 && row.arrival_id == 0 && row.send_ns == 1020 && row.recv_ns == 1030);
    CHECK(row.rpc_ordinal == 1 && row.caller_index == 0 && row.method == DS_METHOD_GET
        && row.flags == DS_SAMPLE_SELECTED && row.controller_error == 0 && row.ds_error == 0
        && row.server_processing_ns == 0 && row.server_execution_ns == 0);
    CHECK(final.slots[0].ends[0].scheduled_ns == 1000);
    CHECK(seal.valid_capture); rig.Close();
}

void LateErrorAndOwners(Library& lib)
{
    Rig rig(lib); rig.own = rig.Register(); CHECK(rig.Arm() == 0); EmitGet(rig, rig.own);
    auto old = rig.Snapshot();
    Gate gate(ds_bench_capture::TestPoint::ErrorLocked); lib.install(Gate::Hook, &gate);
    std::thread error([&] { lib.emit(10, 20, DS_METHOD_GET, 0, 0); }); gate.Entered();
    std::atomic<bool> attempting{false}, completed{false}; int code = -1;
    auto output = Header<DsCaptureSealV2>();
    std::thread disarm([&] { attempting.store(true); code = lib.api->disarm(&rig.run, &old, &output); completed.store(true); });
    while (!attempting.load()) std::this_thread::yield();
    const bool prematurely_done = completed.load(); gate.Release(); error.join(); disarm.join(); lib.install(nullptr, nullptr);
    const int first = code;
    if (code == DS_CAPTURE_BUSY) {
        CHECK(!output.sealed);
        code = lib.api->disarm(&rig.run, &old, &output);
    }
    CHECK(!prematurely_done && (first == DS_CAPTURE_BUSY || first == DS_CAPTURE_CHANGED)
        && code == DS_CAPTURE_CHANGED && !output.sealed);
    auto changed = rig.Snapshot(); CHECK(changed.global_error_generation > old.global_error_generation && (changed.global_errors & DS_ERROR_SCOPE));
    CHECK(!rig.Seal(changed).valid_capture);
}

struct Handshake {
    std::mutex mutex; std::condition_variable cv;
    bool begin_entered = false, snapshot_entered = false, allow_begin = false, allow_snapshot = false;
    ds_bench_capture::TestPoint begin_point;
    explicit Handshake(ds_bench_capture::TestPoint p) : begin_point(p) {}
    static void Hook(ds_bench_capture::TestPoint point, void* context)
    {
        auto& h = *static_cast<Handshake*>(context); std::unique_lock<std::mutex> lock(h.mutex);
        if (point == h.begin_point) { h.begin_entered = true; h.cv.notify_all(); h.cv.wait(lock, [&] { return h.allow_begin; }); }
        if (point == ds_bench_capture::TestPoint::SnapshotFrozen) {
            h.snapshot_entered = true; h.cv.notify_all(); h.cv.wait(lock, [&] { return h.allow_snapshot; });
        }
    }
    void Wait(bool& flag) { std::unique_lock<std::mutex> lock(mutex); cv.wait(lock, [&] { return flag; }); }
    void Allow(bool& flag) { std::lock_guard<std::mutex> lock(mutex); flag = true; cv.notify_all(); }
};
void FreezeHandshake(Library& lib, bool before_active)
{
    Rig rig(lib); Handshake h(before_active ? ds_bench_capture::TestPoint::BeginBeforeActive : ds_bench_capture::TestPoint::BeginAfterActive);
    lib.install(Handshake::Hook, &h); std::atomic<int> begin_code{-1};
    Workers workers(rig, [&](uint32_t, void* slot) {
        const auto c = Context(rig); const int code = lib.api->begin_api(slot, &c); begin_code.store(code);
        if (code == 0) { lib.emit(c.api_begin_ns + 1, c.api_begin_ns + 2, DS_METHOD_GET, 0, 0); End(rig, slot, c); }
    });
    CHECK(rig.Arm() == 0); workers.Go(); h.Wait(h.begin_entered);
    auto snapshot = Header<DsCaptureSnapshotV2>(); int snapshot_code = -1;
    std::thread reader([&] { snapshot_code = lib.api->snapshot(&rig.run, DS_CAPTURE_NORMAL, &snapshot); });
    h.Wait(h.snapshot_entered);
    if (before_active) {
        h.Allow(h.allow_begin); workers.Done(); h.Allow(h.allow_snapshot); reader.join();
    } else {
        h.Allow(h.allow_snapshot); reader.join(); h.Allow(h.allow_begin); workers.Done();
    }
    lib.install(nullptr, nullptr);
    auto final = rig.Snapshot(); CHECK(rig.Seal(final).valid_capture); workers.Join();
    CHECK(begin_code.load() == (before_active ? DS_CAPTURE_BUSY : DS_CAPTURE_OK));
    CHECK(snapshot_code == (before_active ? DS_CAPTURE_OK : DS_CAPTURE_BUSY));
    CHECK(final.slots[0].record_count == (before_active ? 0u : 1u)); rig.Close();
}

void AbiAndLifecycle(Library& lib)
{
    auto bad = Config(); auto output = Header<DsCaptureRunV2>(); bad.bytes = 0;
    CHECK(lib.api->open(&bad, &output) == DS_CAPTURE_ABI_MISMATCH);
    bad = Config(); bad.callers = 0; CHECK(lib.api->open(&bad, &output) == DS_CAPTURE_INVALID);
    bad = Config(); bad.memory_budget_bytes = 1; CHECK(lib.api->open(&bad, &output) == DS_CAPTURE_LIMIT);
    bad = Config(); bad.normal_records = UINT32_MAX; bad.error_records = 1;
    CHECK(lib.api->open(&bad, &output) == DS_CAPTURE_INVALID);
    Rig rig(lib); CHECK(rig.Arm() == DS_CAPTURE_BUSY); rig.own = rig.Register();
    CHECK(lib.api->close(&rig.run) == DS_CAPTURE_BUSY && rig.Arm() == 0);
    const auto context = Context(rig); int wrong = -1;
    auto wide_phase = context; wide_phase.phase_id = UINT64_MAX;
    CHECK(lib.api->begin_api(rig.own, &wide_phase) == DS_CAPTURE_INVALID);
    std::thread stranger([&] { wrong = lib.api->begin_api(rig.own, &context); }); stranger.join();
    CHECK(wrong == DS_CAPTURE_OWNER);
    CHECK(lib.api->begin_api(rig.own, &context) == 0 && lib.api->begin_api(rig.own, &context) == DS_CAPTURE_BUSY);
    auto end = Header<DsCaptureApiEndV2>(); end.bytes = 0; CHECK(lib.api->end_api(rig.own, &end) == DS_CAPTURE_ABI_MISMATCH);
    lib.emit(0, 0, DS_METHOD_UNKNOWN, 0, 0); End(rig, rig.own, context);
    auto snapshot = rig.Snapshot(); CHECK(snapshot.global_errors & DS_ERROR_OWNER);
    CHECK((snapshot.slots[0].errors & (DS_ERROR_ABI | DS_ERROR_ENDPOINT | DS_ERROR_UNKNOWN_METHOD))
        == (DS_ERROR_ABI | DS_ERROR_ENDPOINT | DS_ERROR_UNKNOWN_METHOD));
    CHECK(!rig.Seal(snapshot).valid_capture && lib.api->close(&rig.run) == DS_CAPTURE_BUSY);
    CHECK(lib.api->unregister_caller(rig.own) == 0); rig.own = nullptr; rig.Close();
    CHECK(lib.api->close(&rig.run) == DS_CAPTURE_INVALID);
}
void ZeroCapsAndAliasing(Library& lib)
{
    auto config = Config(); config.normal_records = config.error_records = 0; config.max_rpc_per_api = 1;
    Rig zero(lib, config); zero.own = zero.Register(); CHECK(zero.Arm() == 0);
    const auto c = Context(zero); CHECK(lib.api->begin_api(zero.own, &c) == 0);
    lib.emit(c.api_begin_ns + 1, c.api_begin_ns + 2, DS_METHOD_GET, 0, 0);
    lib.emit(c.api_begin_ns + 3, c.api_begin_ns + 4, DS_METHOD_GET, 0, 0);
    End(zero, zero.own, c); auto snap = zero.Snapshot();
    CHECK(snap.slots[0].record_count == 0 && snap.slots[0].rpc_count == 2);
    CHECK((snap.slots[0].errors & (DS_ERROR_RECORD_OVERFLOW | DS_ERROR_ORDINAL)) == (DS_ERROR_RECORD_OVERFLOW | DS_ERROR_ORDINAL));
    CHECK(!zero.Seal(snap).valid_capture);
    CHECK(lib.api->unregister_caller(zero.own) == 0); zero.own = nullptr; zero.Close();
    config = Config(); config.normal_ends = config.error_ends = 0;
    Rig end_zero(lib, config); end_zero.own = end_zero.Register(); CHECK(end_zero.Arm() == 0);
    EmitGet(end_zero, end_zero.own); snap = end_zero.Snapshot();
    CHECK(snap.slots[0].record_count == 1 && snap.slots[0].end_count == 0 && (snap.slots[0].errors & DS_ERROR_END_OVERFLOW));
    CHECK(!end_zero.Seal(snap).valid_capture);
    CHECK(lib.api->unregister_caller(end_zero.own) == 0); end_zero.own = nullptr; end_zero.Close();
    Rig alias(lib, Config(2)); alias.own = alias.Register();
    int overlap = -1, duplicate_tid = -1, normal = -1, released = -1;
    std::thread second([&] {
        void* slot = nullptr;
        overlap = lib.api->register_caller(&alias.run, 1, 1001, &alias.loans[0].span, &slot);
        duplicate_tid = lib.api->register_caller(&alias.run, 1, 1000, &alias.loans[1].span, &slot);
        normal = lib.api->register_caller(&alias.run, 1, 1001, &alias.loans[1].span, &slot);
        if (normal == 0) released = lib.api->unregister_caller(slot);
    });
    second.join();
    CHECK(overlap == DS_CAPTURE_INVALID && duplicate_tid == DS_CAPTURE_INVALID && normal == 0 && released == 0);
    CHECK(lib.api->unregister_caller(alias.own) == 0); alias.own = nullptr; alias.Close();
}
void UnsampledSnapshotChange(Library& lib)
{
    auto config = Config(); config.sample_modulus = 4;
    Rig rig(lib, config); rig.own = rig.Register(); CHECK(rig.Arm() == 0);
    bool saw_unselected = false;
    for (uint64_t id = 0; id < 100 && !saw_unselected; ++id) {
        const auto before = rig.Snapshot(); EmitGet(rig, rig.own, id);
        const auto after = rig.Snapshot(); auto seal = Header<DsCaptureSealV2>();
        CHECK(lib.api->disarm(&rig.run, &before, &seal) == DS_CAPTURE_CHANGED && !seal.sealed);
        saw_unselected = before.slots[0].record_count == after.slots[0].record_count
            && before.slots[0].end_count == after.slots[0].end_count;
        CHECK(after.slots[0].api_count == before.slots[0].api_count + 1);
    }
    CHECK(saw_unselected && rig.Seal(rig.Snapshot()).valid_capture);
}
void OptionalServerSpans(Library& lib)
{
    using ds_bench_capture::SpanState;
    Rig rig(lib); rig.own = rig.Register(); CHECK(rig.Arm() == 0);
    for (uint64_t id = 0; id < 6; ++id) {
        const auto c = Context(rig, id); CHECK(lib.api->begin_api(rig.own, &c) == 0);
        if (id == 0) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0);
        if (id == 1) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0, 0, 0, SpanState::Valid, SpanState::Valid);
        if (id == 2) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0, 5, 2, SpanState::Valid, SpanState::Valid);
        if (id == 3) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0, 999, 999, SpanState::Invalid, SpanState::Invalid);
        if (id == 4) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0, 2, 3, SpanState::Valid, SpanState::Valid);
        if (id == 5) lib.emit(c.api_begin_ns + 10, c.api_begin_ns + 20, DS_METHOD_GET, 0, 0, 20, 10, SpanState::Valid, SpanState::Valid);
        End(rig, rig.own, c);
    }
    const auto snapshot = rig.Snapshot(); const auto* rows = snapshot.slots[0].records;
    CHECK(snapshot.slots[0].record_count == 6 && snapshot.slots[0].errors == 0);
    CHECK(rows[0].flags == DS_SAMPLE_SELECTED);
    CHECK(rows[1].server_processing_ns == 0 && rows[1].server_execution_ns == 0
        && (rows[1].flags & DS_SAMPLE_SERVER_PROCESSING_VALID) && (rows[1].flags & DS_SAMPLE_SERVER_EXECUTION_VALID));
    CHECK(rows[2].server_processing_ns == 5 && rows[2].server_execution_ns == 2);
    CHECK(rows[3].server_processing_ns == 0 && rows[3].server_execution_ns == 0
        && (rows[3].flags & DS_SAMPLE_SERVER_PROCESSING_INVALID) && (rows[3].flags & DS_SAMPLE_SERVER_EXECUTION_INVALID));
    CHECK(rows[4].server_processing_ns == 2 && rows[4].server_execution_ns == 0
        && (rows[4].flags & DS_SAMPLE_SERVER_EXECUTION_INVALID) && !(rows[4].flags & DS_SAMPLE_SERVER_EXECUTION_VALID));
    CHECK(rows[5].server_processing_ns == 20 && rows[5].server_execution_ns == 10 && (rows[5].flags & DS_SAMPLE_RESIDUAL_INVALID));
    CHECK(rig.Seal(snapshot).valid_capture);
}

int Suite(Library& lib)
{
    const std::vector<std::pair<const char*, std::function<void()>>> tests{
        {"unarmed setup, phases, disabled", [&] { SetupAndDisabled(lib); }},
        {"two callers and assignment independent IDs", [&] { CHECK(Sampling(lib, false) == Sampling(lib, true)); }},
        {"unselected failures and reserved overflow", [&] { FailureAndOverflow(lib); }},
        {"release prefix and partial lifetime", [&] { PrefixPublication(lib); }},
        {"acquire immutable row while writer active", [&] { AcquireWhileActive(lib); }},
        {"snapshot to late error seal", [&] { LateErrorAndOwners(lib); }},
        {"freeze before active", [&] { FreezeHandshake(lib, true); }},
        {"freeze after active", [&] { FreezeHandshake(lib, false); }},
        {"ABI owner endpoint and CloseBusy", [&] { AbiAndLifecycle(lib); }},
        {"zero caps ordinal and disjoint loans", [&] { ZeroCapsAndAliasing(lib); }},
        {"unsampled API invalidates old snapshot", [&] { UnsampledSnapshotChange(lib); }},
        {"optional server spans keep RPC contract", [&] { OptionalServerSpans(lib); }}};
    unsigned failed = 0;
    for (const auto& test : tests) {
        try { test.second(); std::cout << "PASS " << test.first << '\n'; }
        catch (const std::exception& e) { ++failed; std::cerr << "FAIL " << test.first << ": " << e.what() << '\n'; }
    }
    return failed ? 1 : 0;
}
int main(int argc, char** argv)
{
    try {
        if (argc == 4 && std::string(argv[1]) == "--contract") { Library library(argv[2], argv[3]); return Contract(library); }
        if (argc == 3 && std::string(argv[1]) == "--admission") { Library library(argv[2], argv[2]); return AdmissionContract(library); }
        if (argc == 3 && std::string(argv[1]) == "--suite") { Library library(argv[2], argv[2]); return Suite(library); }
        throw std::invalid_argument("expected --contract control.so sdk.so, --admission sdk.so or --suite sdk.so");
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 3; }
}
