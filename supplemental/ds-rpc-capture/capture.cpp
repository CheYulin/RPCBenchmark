#include "capture_observe.h"

#include <atomic>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <new>

#if defined(DS_CAPTURE_OLD_ADMISSION) && !defined(DS_CAPTURE_TESTING)
#error "old admission is a semantic-negative fixture only"
#endif

namespace ds_bench_capture {
namespace {
enum State : uint32_t { Closed, Unarmed, Armed, Disarming, Disarmed };
constexpr uint32_t kStateMask = 255, kEnabled = 256, kFreeze = 512;
constexpr uint64_t kMaxCapacity = 1024 * 1024;
static_assert(std::atomic<uint64_t>::is_always_lock_free && std::atomic<uint32_t>::is_always_lock_free, "lock-free capture atomics required");
struct Run;
struct alignas(64) Slot {
    Run* run = nullptr;
    uint32_t index = 0, tid = 0;
    bool registered = false, sealed = false;
    DsCaptureBuffersV2 buffers{};
    std::atomic<uint32_t> active{0};
    std::atomic<uint64_t> records{0}, ends{0}, errors{0}, error_generation{0};
    uint64_t rpc_count = 0, rpc_failures = 0, api_count = 0, api_failures = 0, methods[DS_CAPTURE_METHODS]{};
    uint64_t normal_records = 0, error_records = 0, normal_ends = 0, error_ends = 0;
    uint64_t api_methods = 0, latest_recv = 0, api_errors = 0;
    uint32_t ordinal = 0;
    uint32_t api_diagnostic_flags = 0;
    bool selected = false, api_rpc_failed = false;
    DsCaptureApiContextV2 context{};
};
struct Run {
    DsCaptureRunConfigV2 config{};
    uint64_t generation = 0;
    uint64_t global_errors = 0, global_error_generation = 0;
    std::unique_ptr<Slot[]> slots;
    DsCaptureSnapshotV2 sealed_snapshot{};
};

std::mutex control_mutex;
std::unique_ptr<Run> current_run;
std::atomic<uint32_t> state_word{Closed};
std::atomic<uint64_t> error_publishers{0};
std::atomic<bool> catastrophic_error{false};
uint64_t next_generation = 0;
thread_local Slot* caller_slot = nullptr;
#ifdef DS_CAPTURE_TESTING
std::atomic<TestHook> test_hook{nullptr};
std::atomic<void*> test_context{nullptr};
void Test(TestPoint point)
{
    if (auto hook = test_hook.load(std::memory_order_acquire)) hook(point, test_context.load());
}
#endif

template <typename T> bool Abi(const T* value)
{
    return value && value->abi == DS_CAPTURE_ABI && value->bytes == sizeof(T);
}
bool Capturing(uint32_t word)
{
    const auto state = word & kStateMask;
    return (word & kEnabled) && (state == Armed || state == Disarming);
}
void Error(Slot& slot, uint64_t flag)
{
    slot.api_errors |= flag;
    slot.errors.fetch_or(flag, std::memory_order_relaxed);
    slot.error_generation.fetch_add(1, std::memory_order_release);
}
void ScopeError(uint64_t flag)
{
#ifdef DS_CAPTURE_OLD_ADMISSION
    if (!Capturing(state_word.load(std::memory_order_acquire))) return;
    Test(TestPoint::ErrorAdmitted);
    std::lock_guard<std::mutex> lock(control_mutex);
    if (!current_run || !Capturing(state_word.load(std::memory_order_acquire))) return;
#else
    struct Admission {
        Admission() { error_publishers.fetch_add(1, std::memory_order_seq_cst); }
        ~Admission() { error_publishers.fetch_sub(1, std::memory_order_seq_cst); }
    } admission;
    const auto word = state_word.load(std::memory_order_seq_cst);
    if (!(word & kEnabled) || (word & kStateMask) != Armed) return;
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::ErrorAdmitted);
#endif
    std::lock_guard<std::mutex> lock(control_mutex);
#endif
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::ErrorLocked);
#endif
    current_run->global_errors |= flag;
    ++current_run->global_error_generation;
}
Run* Handle(const DsCaptureRunV2* handle)
{
    return Abi(handle) && current_run && handle->handle == current_run.get()
        && handle->generation == current_run->generation ? current_run.get() : nullptr;
}
Slot* Owner(void* handle)
{
    if (handle && caller_slot == handle) return caller_slot;
    ScopeError(DS_ERROR_OWNER);
    return nullptr;
}
bool Active(const Run& run)
{
    for (uint32_t i = 0; i < run.config.callers; ++i) {
        if (run.slots[i].active.load(std::memory_order_seq_cst)) return true;
    }
    return false;
}
uint64_t GlobalErrors(const Run& run)
{
    return run.global_errors | (catastrophic_error.load(std::memory_order_acquire) ? uint64_t(DS_ERROR_LIFECYCLE) : uint64_t{0});
}
uint64_t GlobalGeneration(const Run& run)
{
    return run.global_error_generation + uint64_t(catastrophic_error.load(std::memory_order_acquire));
}
bool Overlap(const void* left, uint64_t left_bytes, const void* right, uint64_t right_bytes)
{
    if (!left_bytes || !right_bytes) return false;
    const auto a = reinterpret_cast<uintptr_t>(left), b = reinterpret_cast<uintptr_t>(right);
    if (left_bytes > std::numeric_limits<uintptr_t>::max() - a
        || right_bytes > std::numeric_limits<uintptr_t>::max() - b) return true;
    return a < b + right_bytes && b < a + left_bytes;
}
bool BuffersOverlap(const DsCaptureBuffersV2& a, const DsCaptureBuffersV2& b)
{
    const uint64_t ar = a.record_capacity * sizeof(DsCaptureSampleV2), ae = a.end_capacity * sizeof(DsCaptureApiSummaryV2);
    const uint64_t br = b.record_capacity * sizeof(DsCaptureSampleV2), be = b.end_capacity * sizeof(DsCaptureApiSummaryV2);
    return Overlap(a.records, ar, b.records, br) || Overlap(a.records, ar, b.ends, be)
        || Overlap(a.ends, ae, b.records, br) || Overlap(a.ends, ae, b.ends, be);
}
bool Touches(const DsCaptureBuffersV2& buffers, const void* object, uint64_t bytes)
{
    return Overlap(buffers.records, buffers.record_capacity * sizeof(DsCaptureSampleV2), object, bytes)
        || Overlap(buffers.ends, buffers.end_capacity * sizeof(DsCaptureApiSummaryV2), object, bytes);
}
uint64_t Mix(uint64_t value)
{
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}
bool Selected(const Run& run, uint64_t phase, uint64_t id)
{
    return Mix(run.config.sample_seed ^ Mix(phase) ^ Mix(id + UINT64_C(0x9e3779b97f4a7c15)))
        % run.config.sample_modulus == 0;
}
void Increment(uint64_t& value, Slot& slot)
{
    if (value == std::numeric_limits<uint64_t>::max()) Error(slot, DS_ERROR_ORDINAL);
    else ++value;
}
bool Reserve(Slot& slot, bool failure, bool end)
{
    auto& count = end ? (failure ? slot.error_ends : slot.normal_ends)
                      : (failure ? slot.error_records : slot.normal_records);
    const auto& config = slot.run->config;
    const uint64_t limit = end ? (failure ? config.error_ends : config.normal_ends)
                               : (failure ? config.error_records : config.normal_records);
    if (count == limit) {
        Error(slot, end ? DS_ERROR_END_OVERFLOW : DS_ERROR_RECORD_OVERFLOW);
        return false;
    }
    ++count;
    return true;
}

int Open(const DsCaptureRunConfigV2* config, DsCaptureRunV2* output)
{
    if (!Abi(config) || !Abi(output)) return DS_CAPTURE_ABI_MISMATCH;
    if (!config->callers || config->callers > DS_CAPTURE_MAX_CALLERS || config->mode > DS_CAPTURE_ENABLED
        || !config->sample_modulus || !config->max_rpc_per_api || config->max_rpc_per_api > 65535
        || uint64_t(config->normal_records) + config->error_records > kMaxCapacity
        || uint64_t(config->normal_ends) + config->error_ends > kMaxCapacity) return DS_CAPTURE_INVALID;
    const uint64_t per_caller = (uint64_t(config->normal_records) + config->error_records) * sizeof(DsCaptureSampleV2)
        + (uint64_t(config->normal_ends) + config->error_ends) * sizeof(DsCaptureApiSummaryV2);
    const uint64_t metadata = sizeof(Run) + uint64_t(config->callers) * sizeof(Slot);
    if (per_caller > (std::numeric_limits<uint64_t>::max() - metadata) / config->callers
        || per_caller * config->callers + metadata > config->memory_budget_bytes) return DS_CAPTURE_LIMIT;
    std::lock_guard<std::mutex> lock(control_mutex);
    if (catastrophic_error.load()) return DS_CAPTURE_INVALID;
    if (current_run || caller_slot) return DS_CAPTURE_BUSY;
    auto run = std::make_unique<Run>();
    run->config = *config;
    run->slots = std::make_unique<Slot[]>(config->callers);
    run->generation = ++next_generation;
    for (uint32_t i = 0; i < config->callers; ++i) {
        run->slots[i].run = run.get(); run->slots[i].index = i;
    }
    output->handle = run.get(); output->generation = run->generation;
    current_run = std::move(run);
    state_word.store(Unarmed | (config->mode == DS_CAPTURE_ENABLED ? kEnabled : 0), std::memory_order_release);
    return DS_CAPTURE_OK;
}
int Register(const DsCaptureRunV2* handle, uint32_t index, uint32_t tid,
             const DsCaptureBuffersV2* buffers, void** output)
{
    if (!Abi(buffers)) return DS_CAPTURE_ABI_MISMATCH;
    std::lock_guard<std::mutex> lock(control_mutex);
    auto* run = Handle(handle);
    if (!run || !output || !tid || index >= run->config.callers) return DS_CAPTURE_INVALID;
    if ((state_word.load() & kStateMask) != Unarmed || caller_slot || run->slots[index].registered) return DS_CAPTURE_BUSY;
    const uint64_t records = uint64_t(run->config.normal_records) + run->config.error_records;
    const uint64_t ends = uint64_t(run->config.normal_ends) + run->config.error_ends;
    if (buffers->record_capacity != records || buffers->end_capacity != ends
        || (records && !buffers->records) || (ends && !buffers->ends)) return DS_CAPTURE_INVALID;
    const auto requested = *buffers;
    if ((records && reinterpret_cast<uintptr_t>(requested.records) % alignof(DsCaptureSampleV2))
        || (ends && reinterpret_cast<uintptr_t>(requested.ends) % alignof(DsCaptureApiSummaryV2))
        || Overlap(requested.records, records * sizeof(DsCaptureSampleV2), requested.ends, ends * sizeof(DsCaptureApiSummaryV2))) return DS_CAPTURE_INVALID;
    if (Touches(requested, run, sizeof(Run)) || Touches(requested, run->slots.get(), uint64_t(run->config.callers) * sizeof(Slot))) return DS_CAPTURE_INVALID;
    for (uint32_t i = 0; i < run->config.callers; ++i) {
        if (run->slots[i].registered
            && (run->slots[i].tid == tid || BuffersOverlap(requested, run->slots[i].buffers))) return DS_CAPTURE_INVALID;
    }
    if (records) std::memset(requested.records, 0, size_t(records) * sizeof(DsCaptureSampleV2));
    if (ends) std::memset(requested.ends, 0, size_t(ends) * sizeof(DsCaptureApiSummaryV2));
    auto& slot = run->slots[index];
    slot.buffers = requested; slot.tid = tid; slot.registered = true;
    caller_slot = &slot; *output = &slot;
    return DS_CAPTURE_OK;
}
int Arm(const DsCaptureRunV2* handle, const DsCaptureArmV2* request)
{
    if (!Abi(request) || request->reserved) return DS_CAPTURE_ABI_MISMATCH;
    std::lock_guard<std::mutex> lock(control_mutex);
    auto* run = Handle(handle);
    if (!run || request->generation != run->generation || request->expected_callers != run->config.callers) return DS_CAPTURE_INVALID;
    const auto word = state_word.load();
    if ((word & kStateMask) != Unarmed) return DS_CAPTURE_BUSY;
    for (uint32_t i = 0; i < run->config.callers; ++i) {
        const auto& slot = run->slots[i];
        if (!slot.registered || slot.sealed || slot.active.load() || slot.records.load() || slot.ends.load()
            || slot.errors.load()) return DS_CAPTURE_BUSY;
    }
    state_word.store(Armed | (word & kEnabled), std::memory_order_release);
    return DS_CAPTURE_OK;
}
int Begin(void* handle, const DsCaptureApiContextV2* context)
{
    auto* slot = Owner(handle);
    if (!slot) return DS_CAPTURE_OWNER;
    if (!Abi(context)) { ScopeError(DS_ERROR_ABI); return DS_CAPTURE_ABI_MISMATCH; }
    const uint64_t methods = ((uint64_t{1} << DS_CAPTURE_METHODS) - 1) & ~uint64_t{1};
    if (context->generation != slot->run->generation || context->phase_id > UINT32_MAX || context->scheduled_ns > context->api_begin_ns
        || !context->api_begin_ns || !(context->required_methods & methods) || (context->required_methods & ~methods)) {
        ScopeError(DS_ERROR_SCOPE); return DS_CAPTURE_INVALID;
    }
    auto word = state_word.load(std::memory_order_acquire);
    if ((word & kStateMask) != Armed || (word & kFreeze)) return DS_CAPTURE_BUSY;
    if (slot->active.load()) { ScopeError(DS_ERROR_LIFECYCLE); return DS_CAPTURE_BUSY; }
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::BeginBeforeActive);
#endif
    slot->active.store(1, std::memory_order_seq_cst);
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::BeginAfterActive);
#endif
    word = state_word.load(std::memory_order_seq_cst);
    if ((word & kStateMask) != Armed || (word & kFreeze)) {
        slot->active.store(0, std::memory_order_release); return DS_CAPTURE_BUSY;
    }
    slot->context = *context; slot->ordinal = 0; slot->api_methods = 0; slot->latest_recv = 0;
    slot->api_errors = 0; slot->api_rpc_failed = false; slot->api_diagnostic_flags = 0;
    slot->selected = Selected(*slot->run, context->phase_id, context->arrival_id);
    return DS_CAPTURE_OK;
}
int End(void* handle, const DsCaptureApiEndV2* result)
{
    auto* slot = Owner(handle);
    if (!slot) return DS_CAPTURE_OWNER;
    if (!slot->active.load()) { ScopeError(DS_ERROR_SCOPE); return DS_CAPTURE_BUSY; }
    if (!Abi(result)) { Error(*slot, DS_ERROR_ABI); return DS_CAPTURE_ABI_MISMATCH; }
    const auto& context = slot->context;
    if (result->reserved || result->api_end_ns < context.api_begin_ns || result->validation_end_ns < result->api_end_ns
        || slot->latest_recv > result->api_end_ns) Error(*slot, DS_ERROR_ENDPOINT);
    if (result->outcome != 0) Error(*slot, DS_ERROR_API_OUTCOME);
    Increment(slot->api_count, *slot);
    if (result->outcome != 0) Increment(slot->api_failures, *slot);
    const bool enabled = slot->run->config.mode == DS_CAPTURE_ENABLED;
    if (enabled && result->outcome == 0 && !(slot->api_methods & context.required_methods)) Error(*slot, DS_ERROR_MISSING_PRIMARY);
    const auto errors = slot->api_errors;
    const bool failure = errors != 0 || slot->api_rpc_failed || result->outcome != 0 || slot->api_diagnostic_flags != 0;
    if ((slot->selected || failure) && Reserve(*slot, failure, true)) {
        const uint64_t index = slot->ends.load(std::memory_order_relaxed);
        slot->buffers.ends[index] = {context.phase_id, context.arrival_id, context.scheduled_ns, context.api_begin_ns,
            result->api_end_ns, result->validation_end_ns, slot->api_methods, errors, slot->ordinal, slot->index,
            result->outcome, (slot->selected ? uint32_t(DS_SAMPLE_SELECTED) : 0u) | (enabled ? 0u : uint32_t(DS_SAMPLE_DISABLED))
                | (slot->api_rpc_failed ? uint32_t(DS_SAMPLE_RPC_FAILED) : 0u) | slot->api_diagnostic_flags};
        slot->ends.store(index + 1, std::memory_order_release);
    }
    slot->active.store(0, std::memory_order_release);
    return DS_CAPTURE_OK;
}

void CopyView(const Slot& slot, DsCaptureSlotViewV2& view, bool normal)
{
    view.records = slot.buffers.records; view.ends = slot.buffers.ends;
    view.record_count = slot.records.load(std::memory_order_acquire);
    view.end_count = slot.ends.load(std::memory_order_acquire);
    view.errors = slot.errors.load(std::memory_order_acquire);
    view.error_generation = slot.error_generation.load(std::memory_order_acquire);
    view.registered = slot.registered; view.sealed = slot.sealed;
    view.active = slot.active.load(std::memory_order_acquire); view.cached_tid = slot.tid;
    view.counters_available = normal;
    if (normal) {
        view.rpc_count = slot.rpc_count; view.rpc_failures = slot.rpc_failures;
        view.api_count = slot.api_count; view.api_failures = slot.api_failures;
        std::memcpy(view.method_counts, slot.methods, sizeof(slot.methods));
    }
}
int Snapshot(const DsCaptureRunV2* handle, uint32_t mode, DsCaptureSnapshotV2* output)
{
    if (!Abi(output)) return DS_CAPTURE_ABI_MISMATCH;
    if (mode > DS_CAPTURE_NORMAL) return DS_CAPTURE_INVALID;
    std::lock_guard<std::mutex> lock(control_mutex);
    auto* run = Handle(handle);
    if (!run) return DS_CAPTURE_INVALID;
    const auto word = state_word.load(std::memory_order_acquire);
    if (mode == DS_CAPTURE_NORMAL) {
        state_word.store(word | kFreeze, std::memory_order_seq_cst);
#ifdef DS_CAPTURE_TESTING
        Test(TestPoint::SnapshotFrozen);
#endif
        if (Active(*run)) { state_word.store(word, std::memory_order_seq_cst); return DS_CAPTURE_BUSY; }
    }
    DsCaptureSnapshotV2 value{};
    value.abi = DS_CAPTURE_ABI; value.bytes = sizeof(value); value.mode = mode;
    value.state = word & kStateMask;
    value.callers = run->config.callers; value.generation = run->generation;
    value.global_errors = GlobalErrors(*run); value.global_error_generation = GlobalGeneration(*run);
    for (uint32_t i = 0; i < run->config.callers; ++i) CopyView(run->slots[i], value.slots[i], mode == DS_CAPTURE_NORMAL);
    if (mode == DS_CAPTURE_NORMAL) {
        for (uint32_t i = 0; i < run->config.callers; ++i) {
            if (value.slots[i].active) { state_word.store(word, std::memory_order_seq_cst); return DS_CAPTURE_BUSY; }
        }
    }
    *output = value;
    if (mode == DS_CAPTURE_NORMAL) state_word.store(word, std::memory_order_seq_cst);
    return DS_CAPTURE_OK;
}
bool Matches(const Run& run, const DsCaptureSnapshotV2& token)
{
    if (token.mode != DS_CAPTURE_NORMAL || token.state != Armed || token.reserved
        || token.generation != run.generation || token.callers != run.config.callers
        || token.global_errors != GlobalErrors(run) || token.global_error_generation != GlobalGeneration(run)) return false;
    for (uint32_t i = 0; i < run.config.callers; ++i) {
        const auto& slot = run.slots[i]; const auto& view = token.slots[i];
        if (view.records != slot.buffers.records || view.ends != slot.buffers.ends || view.cached_tid != slot.tid || !view.counters_available
            || view.rpc_count != slot.rpc_count || view.rpc_failures != slot.rpc_failures
            || view.api_count != slot.api_count || view.api_failures != slot.api_failures
            || std::memcmp(view.method_counts, slot.methods, sizeof(slot.methods)) != 0
            || view.record_count != slot.records.load(std::memory_order_acquire)
            || view.end_count != slot.ends.load(std::memory_order_acquire)
            || view.errors != slot.errors.load(std::memory_order_acquire)
            || view.error_generation != slot.error_generation.load(std::memory_order_acquire)
            || view.registered != slot.registered || view.sealed != slot.sealed || view.active || slot.active.load()) return false;
    }
    return true;
}
int Disarm(const DsCaptureRunV2* handle, const DsCaptureSnapshotV2* token, DsCaptureSealV2* output)
{
    if (!Abi(token) || !Abi(output)) return DS_CAPTURE_ABI_MISMATCH;
    std::lock_guard<std::mutex> lock(control_mutex);
    auto* run = Handle(handle);
    if (!run) return DS_CAPTURE_INVALID;
    const auto word = state_word.load(std::memory_order_acquire);
    if ((word & kStateMask) != Armed || Active(*run)) return DS_CAPTURE_BUSY;
    state_word.store(Disarming | (word & kEnabled) | kFreeze, std::memory_order_seq_cst);
    if (error_publishers.load(std::memory_order_seq_cst) || Active(*run)) {
        state_word.store(word, std::memory_order_seq_cst); return DS_CAPTURE_BUSY;
    }
    if (!Matches(*run, *token)) { state_word.store(word, std::memory_order_seq_cst); return DS_CAPTURE_CHANGED; }
    uint64_t errors = GlobalErrors(*run);
    for (uint32_t i = 0; i < run->config.callers; ++i) {
        errors |= run->slots[i].errors.load();
        if (run->slots[i].rpc_failures) errors |= DS_ERROR_API_OUTCOME;
    }
    run->sealed_snapshot = *token;
    output->sealed = 1; output->valid_capture = errors == 0 && run->config.mode == DS_CAPTURE_ENABLED;
    output->generation = run->generation; output->global_errors = GlobalErrors(*run);
    output->global_error_generation = GlobalGeneration(*run);
    state_word.store(Disarmed | (word & kEnabled), std::memory_order_release);
    return DS_CAPTURE_OK;
}
int Unregister(void* handle)
{
    auto* slot = Owner(handle);
    if (!slot) return DS_CAPTURE_OWNER;
    std::lock_guard<std::mutex> lock(control_mutex);
    const auto state = state_word.load() & kStateMask;
    if (slot->active.load() || (state != Unarmed && state != Disarmed)) return DS_CAPTURE_BUSY;
    slot->sealed = true; caller_slot = nullptr;
    return DS_CAPTURE_OK;
}
int Close(const DsCaptureRunV2* handle)
{
    std::lock_guard<std::mutex> lock(control_mutex);
    auto* run = Handle(handle);
    if (!run) return DS_CAPTURE_INVALID;
    const auto state = state_word.load() & kStateMask;
    if ((state != Unarmed && state != Disarmed) || error_publishers.load(std::memory_order_seq_cst)
        || Active(*run)) return DS_CAPTURE_BUSY;
    for (uint32_t i = 0; i < run->config.callers; ++i) if (run->slots[i].registered && !run->slots[i].sealed) return DS_CAPTURE_BUSY;
    // Unregister changes sealed bits, not published records/errors. The driver joins before Close.
    if (state == Disarmed) {
        if (GlobalGeneration(*run) != run->sealed_snapshot.global_error_generation) return DS_CAPTURE_CHANGED;
        for (uint32_t i = 0; i < run->config.callers; ++i) {
            const auto& slot = run->slots[i]; const auto& sealed = run->sealed_snapshot.slots[i];
            if (slot.records.load() != sealed.record_count || slot.ends.load() != sealed.end_count
                || slot.error_generation.load() != sealed.error_generation) return DS_CAPTURE_CHANGED;
        }
    }
    state_word.store(Closed, std::memory_order_release);
    current_run.reset();
    return DS_CAPTURE_OK;
}

template <typename Function, typename... Args> int Protect(Function function, Args... args) noexcept
{
    try { return function(args...); }
    catch (const std::bad_alloc&) { return DS_CAPTURE_NO_MEMORY; }
    catch (...) { catastrophic_error.store(true, std::memory_order_release); return DS_CAPTURE_INVALID; }
}
const DsBenchApiV2 api{
    DS_CAPTURE_ABI, sizeof(DsBenchApiV2),
    [](const DsCaptureRunConfigV2* c, DsCaptureRunV2* o) noexcept { return Protect(Open, c, o); },
    [](const DsCaptureRunV2* r, uint32_t i, uint32_t t, const DsCaptureBuffersV2* b, void** o) noexcept { return Protect(Register, r, i, t, b, o); },
    [](const DsCaptureRunV2* r, const DsCaptureArmV2* a) noexcept { return Protect(Arm, r, a); },
    [](void* s, const DsCaptureApiContextV2* c) noexcept { return Protect(Begin, s, c); },
    [](void* s, const DsCaptureApiEndV2* e) noexcept { return Protect(End, s, e); },
    [](const DsCaptureRunV2* r, uint32_t m, DsCaptureSnapshotV2* o) noexcept { return Protect(Snapshot, r, m, o); },
    [](const DsCaptureRunV2* r, const DsCaptureSnapshotV2* t, DsCaptureSealV2* o) noexcept { return Protect(Disarm, r, t, o); },
    [](void* s) noexcept { return Protect(Unregister, s); },
    [](const DsCaptureRunV2* r) noexcept { return Protect(Close, r); }
};
}  // namespace

void Observe(const RpcObservation& rpc) noexcept
{
    auto* slot = caller_slot;
    if (!slot || !slot->active.load(std::memory_order_acquire)) {
        try { ScopeError(DS_ERROR_SCOPE); }
        catch (...) { catastrophic_error.store(true, std::memory_order_release); }
        return;
    }
    if (!Capturing(state_word.load(std::memory_order_acquire))) return;
    if (slot->ordinal == std::numeric_limits<uint32_t>::max()) Error(*slot, DS_ERROR_ORDINAL);
    else ++slot->ordinal;
    Increment(slot->rpc_count, *slot);
    uint32_t flags = slot->selected ? uint32_t(DS_SAMPLE_SELECTED) : uint32_t{0};
    bool failure = rpc.failed || rpc.controller_error || rpc.ds_error;
    if (failure) { flags |= DS_SAMPLE_RPC_FAILED; slot->api_rpc_failed = true; Increment(slot->rpc_failures, *slot); }
    if (rpc.method == DS_METHOD_UNKNOWN || rpc.method >= DS_CAPTURE_METHODS) {
        Error(*slot, DS_ERROR_UNKNOWN_METHOD); flags |= DS_SAMPLE_UNKNOWN_METHOD; failure = true;
        Increment(slot->methods[0], *slot);
    } else {
        Increment(slot->methods[rpc.method], *slot); slot->api_methods |= uint64_t{1} << rpc.method;
    }
    if (!rpc.send_ns || !rpc.recv_ns || rpc.recv_ns < rpc.send_ns || rpc.send_ns < slot->context.api_begin_ns) {
        Error(*slot, DS_ERROR_ENDPOINT); flags |= DS_SAMPLE_BAD_ENDPOINT; failure = true;
    }
    if (slot->ordinal > slot->run->config.max_rpc_per_api) {
        Error(*slot, DS_ERROR_ORDINAL); flags |= DS_SAMPLE_CONTRACT_ERROR; failure = true;
    }
    const auto span = [&](SpanState state, uint64_t value, uint32_t valid, uint32_t invalid) {
        if (state == SpanState::Valid) { flags |= valid; return value; }
        if (state == SpanState::Invalid || value != 0) flags |= invalid;
        if (state != SpanState::Missing && state != SpanState::Invalid) {
            Error(*slot, DS_ERROR_ABI); flags |= invalid | DS_SAMPLE_CONTRACT_ERROR; failure = true;
        }
        return uint64_t{0};
    };
    uint64_t processing = span(rpc.processing_state, rpc.server_processing_ns,
        DS_SAMPLE_SERVER_PROCESSING_VALID, DS_SAMPLE_SERVER_PROCESSING_INVALID);
    uint64_t execution = span(rpc.execution_state, rpc.server_execution_ns,
        DS_SAMPLE_SERVER_EXECUTION_VALID, DS_SAMPLE_SERVER_EXECUTION_INVALID);
    if ((flags & DS_SAMPLE_SERVER_PROCESSING_VALID) && (flags & DS_SAMPLE_SERVER_EXECUTION_VALID) && execution > processing) {
        flags = (flags & ~uint32_t(DS_SAMPLE_SERVER_EXECUTION_VALID)) | DS_SAMPLE_SERVER_EXECUTION_INVALID;
        execution = 0;
    }
    if ((flags & DS_SAMPLE_SERVER_PROCESSING_VALID) && rpc.recv_ns >= rpc.send_ns && processing > rpc.recv_ns - rpc.send_ns) {
        flags |= DS_SAMPLE_RESIDUAL_INVALID;
    }
    const uint32_t diagnostic = flags & (DS_SAMPLE_SERVER_PROCESSING_INVALID | DS_SAMPLE_SERVER_EXECUTION_INVALID | DS_SAMPLE_RESIDUAL_INVALID);
    slot->api_diagnostic_flags |= diagnostic;
    failure |= diagnostic != 0;
    if (rpc.recv_ns > slot->latest_recv) slot->latest_recv = rpc.recv_ns;
    if ((!slot->selected && !failure) || !Reserve(*slot, failure, false)) return;
    const auto index = slot->records.load(std::memory_order_relaxed);
    const auto& context = slot->context;
    slot->buffers.records[index] = {context.arrival_id, rpc.send_ns, rpc.recv_ns, processing, execution,
        uint32_t(context.phase_id), slot->ordinal, uint16_t(slot->index), rpc.method, rpc.controller_error, rpc.ds_error, flags};
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::BeforeRecordPublish);
#endif
    slot->records.store(index + 1, std::memory_order_release);
#ifdef DS_CAPTURE_TESTING
    Test(TestPoint::AfterRecordPublish);
#endif
}

#ifdef DS_CAPTURE_TESTING
void InstallTestHook(TestHook hook, void* context) noexcept
{
    test_context.store(context, std::memory_order_release);
    test_hook.store(hook, std::memory_order_release);
}
void ObserveFromSlotForTest(const RpcObservation& rpc, const void* candidate) noexcept
{
    if (candidate != caller_slot) {
        try { ScopeError(DS_ERROR_OWNER); }
        catch (...) { catastrophic_error.store(true, std::memory_order_release); }
        return;
    }
    Observe(rpc);
}
#endif
}  // namespace ds_bench_capture

extern "C" __attribute__((visibility("default"))) const DsBenchApiV2* DsBenchRpcCaptureV2(void) noexcept
{
    return &ds_bench_capture::api;
}
