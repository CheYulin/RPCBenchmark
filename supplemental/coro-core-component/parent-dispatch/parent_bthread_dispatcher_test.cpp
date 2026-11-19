#include "parent_bthread_dispatcher.h"
#include <bthread/countdown_event.h>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <memory>
#include <mutex>
#include <new>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
using datasystem::StatusCode;
using Pool = datasystem::coro_unary::diagnostic::ParentBthreadDispatcher;

void Check(bool value, const char *reason)
{
    if (!value) throw std::runtime_error(reason);
}

template <typename Predicate>
void Until(Predicate predicate)
{
    const auto end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (!predicate()) {
        Check(std::chrono::steady_clock::now() < end, "bounded state wait expired");
        std::this_thread::yield();
    }
}

struct Counts : Pool::Owner {
    std::atomic<unsigned> ran{0}, failed{0}, runFailed{0}, destroyed{0}, retired{0}, bad{0};
    std::atomic<bthread::CountdownEvent *> retireEntered{nullptr};
    bthread::CountdownEvent *retireGate = nullptr;
    void JobRetired() noexcept override
    {
        if (destroyed.load() <= retired.load()) ++bad;
        if (auto *event = retireEntered.exchange(nullptr)) event->signal();
        if (retireGate) retireGate->wait();
        ++retired;
    }
};

struct Record {
    std::atomic<bool> tlsDestroyed{false};
    std::atomic<bthread_t> child{0};
    bool expectTls = false;
};

struct Control {
    Counts *counts = nullptr;
    std::atomic<unsigned> parentCalls{0}, parentExits{0}, childCalls{0}, childDtors{0}, parentDtors{0};
    unsigned failParent = 0;
    std::atomic<unsigned> failChildren{0};
    bthread_key_t key = INVALID_BTHREAD_KEY;
    bool keyValid = false;
    bthread::CountdownEvent *tlsEntered = nullptr, *tlsGate = nullptr;
    std::mutex idsMutex;
    std::vector<bthread_t> parents;
};
std::atomic<Control *> current{nullptr};

struct KeyData {
    Control *control;
    std::atomic<bool> *destroyed;
};
void DestroyKey(void *value)
{
    std::unique_ptr<KeyData> data(static_cast<KeyData *>(value));
    if (data->destroyed) {
        if (data->control->tlsEntered) data->control->tlsEntered->signal();
        if (data->control->tlsGate) data->control->tlsGate->wait();
        data->destroyed->store(true);
        ++data->control->childDtors;
    } else {
        ++data->control->parentDtors;
    }
}

struct ParentCall {
    void *(*function)(void *);
    void *argument;
    Control *control;
};
void *ForwardParent(void *argument)
{
    std::unique_ptr<ParentCall> call(static_cast<ParentCall *>(argument));
    void *result = call->function(call->argument);
    ++call->control->parentExits;
    return result;
}
int StartParent(bthread_t *id, const bthread_attr_t *attr, void *(*function)(void *), void *argument)
{
    auto *control = current.load();
    const auto call = ++control->parentCalls;
    if (call == control->failParent) return EAGAIN;
    auto context = std::make_unique<ParentCall>(ParentCall{function, argument, control});
    const int code = bthread_start_background(id, attr, ForwardParent, context.get());
    if (!code) context.release();
    return code;
}
int StartChild(bthread_t *id, const bthread_attr_t *attr, void *(*function)(void *), void *argument)
{
    auto *control = current.load();
    ++control->childCalls;
    if (!attr || attr->flags != 0 || attr->keytable_pool != nullptr) ++control->counts->bad;
    auto remaining = control->failChildren.load();
    while (remaining && !control->failChildren.compare_exchange_weak(remaining, remaining - 1)) {}
    if (remaining) return ENOMEM;
    {
        std::lock_guard lock(control->idsMutex);
        control->parents.push_back(bthread_self());
    }
    if (control->keyValid && bthread_getspecific(control->key) == nullptr) {
        auto data = std::make_unique<KeyData>(KeyData{control, nullptr});
        const int code = bthread_setspecific(control->key, data.get());
        if (code) return code;
        data.release();
    }
    return bthread_start_urgent(id, attr, function, argument);
}

struct Fixture {
    Counts counts;
    Control control;
    Pool pool;
    explicit Fixture(bool key = false) : pool(counts, Pool::Hooks{StartParent, StartChild})
    {
        control.counts = &counts;
        control.parents.reserve(256);
        if (key) {
            Check(bthread_key_create(&control.key, DestroyKey) == 0, "key creation failed");
            control.keyValid = true;
        }
        Check(current.exchange(&control) == nullptr, "fixtures must be serial");
    }
    ~Fixture()
    {
        pool.Stop();
        if (control.keyValid) bthread_key_delete(control.key);
        current.store(nullptr);
    }
};

struct Latch {
    bthread::CountdownEvent value{1};
    bool released = false;
    void Release() {if (!released) {released = true; value.signal();}}
};
struct Drain {
    Pool &pool;
    std::vector<Latch *> gates;
    std::vector<std::thread *> threads;
    ~Drain()
    {
        for (auto *gate : gates) gate->Release();
        for (auto *thread : threads) if (thread->joinable()) thread->join();
        pool.Stop();
    }
};

struct Job : Pool::Job {
    Counts &counts;
    Control &control;
    std::shared_ptr<Record> record;
    bthread::CountdownEvent *entered = nullptr, *runGate = nullptr, *destructEntered = nullptr, *destructGate = nullptr;
    explicit Job(Fixture &fixture, std::shared_ptr<Record> value)
        : counts(fixture.counts), control(fixture.control), record(std::move(value)) {}
    ~Job() noexcept override
    {
        if (record->expectTls && !record->tlsDestroyed.load()) ++counts.bad;
        if (destructEntered) destructEntered->signal();
        if (destructGate) destructGate->wait();
        ++counts.destroyed;
    }
    void Run() override
    {
        ++counts.ran;
        record->child.store(bthread_self());
        {
            std::lock_guard lock(control.idsMutex);
            for (auto parent : control.parents) if (parent == bthread_self()) ++counts.bad;
        }
        if (control.keyValid) {
            if (bthread_getspecific(control.key) != nullptr) ++counts.bad;
            auto *data = new (std::nothrow) KeyData{&control, &record->tlsDestroyed};
            if (!data || bthread_setspecific(control.key, data) != 0) {
                delete data;
                ++counts.bad;
            }
        }
        if (entered) entered->signal();
        if (runGate) runGate->wait();
    }
    void ChildStartFailed(StatusCode code) noexcept override
    {
        if (code != datasystem::K_OUT_OF_MEMORY && code != datasystem::K_RPC_UNAVAILABLE) ++counts.bad;
        record->expectTls = false;
        ++counts.failed;
    }
    void RunFailed(StatusCode code) noexcept override
    {
        if (code != datasystem::K_OUT_OF_MEMORY && code != datasystem::K_RUNTIME_ERROR) ++counts.bad;
        ++counts.runFailed;
    }
};

void FreshChildTls()
{
    Fixture fixture(true);
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    std::array<std::shared_ptr<Record>, 2> records;
    for (unsigned i = 0; i < records.size(); ++i) {
        records[i] = std::make_shared<Record>(); records[i]->expectTls = true;
        Check(fixture.pool.TrySubmit([&] {return std::make_unique<Job>(fixture, records[i]);}) == datasystem::K_OK,
              "submit failed");
        Until([&] {return fixture.counts.retired.load() == i + 1 && fixture.pool.Inspect().occupied == 0;});
        Check(records[i]->tlsDestroyed.load(), "join did not include child TLS destruction");
    }
    Check(records[0]->child.load() != records[1]->child.load(), "child identity reused");
    Check(fixture.control.parents.size() == 2 && fixture.control.parentCalls.load() == 64,
          "request created an extra parent");
    Check(fixture.control.parentDtors.load() == 0, "parent ended before Stop");
    fixture.pool.Stop();
    const unsigned parents = fixture.control.parents[0] == fixture.control.parents[1] ? 1 : 2;
    Check(fixture.control.parentDtors.load() == parents && fixture.control.childDtors.load() == 2, "key lifetimes wrong");
    Check(fixture.counts.bad.load() == 0, "TLS/order/identity failure");
}

void Capacity64()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    bthread::CountdownEvent entered(64); Latch gate; Drain drain{fixture.pool, {&gate}, {}};
    for (unsigned i = 0; i < 64; ++i) {
        Check(fixture.pool.TrySubmit([&] {
            auto job = std::make_unique<Job>(fixture, std::make_shared<Record>());
            job->entered = &entered; job->runGate = &gate.value; return job;
        }) == datasystem::K_OK, "bounded admission failed");
    }
    entered.wait();
    Check(fixture.pool.Inspect().occupied == 64, "64 jobs not charged");
    unsigned invoked = 0;
    const auto rejected = fixture.pool.TrySubmit([&] {++invoked; return std::make_unique<Job>(fixture, std::make_shared<Record>());});
    Check(rejected == datasystem::K_TRY_AGAIN && invoked == 0, "65th job allocated or accepted");
    gate.Release(); fixture.pool.Stop();
    Check(fixture.counts.retired.load() == 64 && fixture.counts.destroyed.load() == 64, "64 jobs not retired");
    Check(fixture.control.parentExits.load() == 64 && fixture.pool.Inspect().parents == 0, "parents not joined");
}

void StopHeldChild()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    bthread::CountdownEvent entered(1); Latch gate; std::thread stopper; std::atomic<bool> stopped{false};
    Drain drain{fixture.pool, {&gate}, {&stopper}};
    Check(fixture.pool.TrySubmit([&] {
        auto job = std::make_unique<Job>(fixture, std::make_shared<Record>());
        job->entered = &entered; job->runGate = &gate.value; return job;
    }) == datasystem::K_OK, "submit failed");
    entered.wait();
    stopper = std::thread([&] {fixture.pool.Stop(); stopped.store(true);});
    Until([&] {return fixture.pool.Inspect().stopping;});
    Check(!stopped.load() && fixture.counts.destroyed.load() == 0, "Stop released a live child");
    gate.Release(); stopper.join();
    Check(stopped.load() && fixture.counts.retired.load() == 1, "Stop failed to drain");
}

void StopReservedFactory()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    std::atomic<int> result{datasystem::K_RUNTIME_ERROR};
    std::atomic<bool> stopped{false};
    bthread::CountdownEvent entered(1); Latch gate; std::thread submit, stopper;
    Drain drain{fixture.pool, {&gate}, {&submit, &stopper}};
    submit = std::thread([&] {
        result.store(fixture.pool.TrySubmit([&] {
            entered.signal(); gate.value.wait(); return std::make_unique<Job>(fixture, std::make_shared<Record>());
        }));
    });
    entered.wait();
    stopper = std::thread([&] {fixture.pool.Stop(); stopped.store(true);});
    Until([&] {return fixture.pool.Inspect().stopping;});
    Check(!stopped.load() && fixture.pool.Inspect().constructing == 1, "Stop discarded reserved factory");
    gate.Release(); submit.join(); stopper.join();
    Check(result.load() == datasystem::K_OK && fixture.counts.retired.load() == 1, "accepted reservation not drained");
}

void RetirementCapacity()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    std::atomic<bool> stopped{false};
    bthread::CountdownEvent entered(1), retired(1); Latch gate, retireGate; std::thread stopper;
    Drain drain{fixture.pool, {&gate, &retireGate}, {&stopper}};
    fixture.counts.retireEntered = &retired; fixture.counts.retireGate = &retireGate.value;
    Check(fixture.pool.TrySubmit([&] {
        auto job = std::make_unique<Job>(fixture, std::make_shared<Record>());
        job->destructEntered = &entered; job->destructGate = &gate.value; return job;
    }) == datasystem::K_OK, "submit failed");
    entered.wait();
    Check(fixture.pool.Inspect().occupied == 1 && fixture.counts.retired.load() == 0, "slot freed before Job destruction");
    gate.Release(); retired.wait();
    Check(fixture.pool.Inspect().occupied == 0 && fixture.counts.destroyed.load() == 1,
          "slot not free before external Finish");
    Check(fixture.pool.TrySubmit([&] {return std::make_unique<Job>(fixture, std::make_shared<Record>());}) == datasystem::K_OK,
          "released slot could not be reused during Finish");
    stopper = std::thread([&] {fixture.pool.Stop(); stopped.store(true);});
    Until([&] {return fixture.pool.Inspect().stopping;});
    Check(!stopped.load(), "occupied zero was mistaken for parent/callback drain");
    retireGate.Release(); stopper.join();
    Check(fixture.counts.retired.load() == 2 && fixture.counts.ran.load() == 2 && fixture.counts.bad.load() == 0,
          "Finish or reused slot was lost");
}

void PartialStart()
{
    Fixture fixture; fixture.control.failParent = 6;
    Check(fixture.pool.Start() == datasystem::K_RPC_UNAVAILABLE, "partial startup not rejected");
    const auto state = fixture.pool.Inspect();
    Check(!state.accepting && state.parents == 0 && state.occupied == 0, "partial startup left state");
    Check(fixture.control.parentCalls.load() == 6 && fixture.control.parentExits.load() == 5, "partial parents not joined");
}

struct Resource {
    std::atomic<int> &live;
    explicit Resource(std::atomic<int> &count) : live(count) {++live;}
    ~Resource() {--live;}
};
struct ThrowingCopy {
    bool oom;
    ThrowingCopy(const ThrowingCopy &other) : oom(other.oom)
    {
        if (oom) throw std::bad_alloc();
        throw std::runtime_error("copy-init failure");
    }
    explicit ThrowingCopy(bool value) : oom(value) {}
};
struct IncompleteJob : Pool::Job {
    Resource resource;
    ThrowingCopy value;
    IncompleteJob(std::atomic<int> &live, const ThrowingCopy &copy) : resource(live), value(copy) {}
    void Run() noexcept override {}
    void ChildStartFailed(StatusCode) noexcept override {}
    void RunFailed(StatusCode) noexcept override {}
};

void FailedCopyInit()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    std::atomic<int> live{0};
    for (bool oom : {false, true}) {
        ThrowingCopy value(oom);
        Check(fixture.pool.TrySubmit([&] {return std::make_unique<IncompleteJob>(live, value);}) ==
              (oom ? datasystem::K_OUT_OF_MEMORY : datasystem::K_RUNTIME_ERROR), "copy failure code wrong");
        Check(live.load() == 0 && fixture.pool.Inspect().occupied == 0, "failed init leaked resource or slot");
    }
    Check(fixture.pool.TrySubmit([] {return std::unique_ptr<Pool::Job>{};}) == datasystem::K_INVALID,
          "null factory accepted");
    fixture.pool.Stop();
    Check(fixture.counts.retired.load() == 0 && fixture.control.childCalls.load() == 0, "rejected job retired or ran");
}

void ChildStartFailure()
{
    Fixture fixture; fixture.control.failChildren.store(1);
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    auto record = std::make_shared<Record>();
    Check(fixture.pool.TrySubmit([&] {return std::make_unique<Job>(fixture, record);}) == datasystem::K_OK, "submit failed");
    Until([&] {return fixture.counts.retired.load() == 1 && fixture.pool.Inspect().occupied == 0;});
    Check(fixture.counts.ran.load() == 0 && fixture.counts.failed.load() == 1 && record->child.load() == 0,
          "child failure ran business fallback");
    Check(fixture.pool.TrySubmit([&] {return std::make_unique<Job>(fixture, std::make_shared<Record>());}) == datasystem::K_OK,
          "healthy job after failure rejected");
    fixture.pool.Stop();
    Check(fixture.counts.ran.load() == 1 && fixture.counts.retired.load() == 2 && fixture.counts.bad.load() == 0,
          "child-start failure cleanup wrong");
}

void JoinIncludesTlsDestruction()
{
    Fixture fixture(true);
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    auto record = std::make_shared<Record>(); record->expectTls = true;
    bthread::CountdownEvent entered(1); Latch gate;
    Drain drain{fixture.pool, {&gate}, {}};
    fixture.control.tlsEntered = &entered; fixture.control.tlsGate = &gate.value;
    Check(fixture.pool.TrySubmit([&] {return std::make_unique<Job>(fixture, record);}) == datasystem::K_OK,
          "submit failed");
    entered.wait();
    const bool charged = fixture.pool.Inspect().occupied == 1 && fixture.counts.destroyed.load() == 0
        && fixture.counts.retired.load() == 0;
    gate.Release(); Until([&] {return fixture.control.childDtors.load() == 1;}); fixture.pool.Stop();
    Check(charged && fixture.counts.bad.load() == 0, "urgent return or function return was mistaken for full child exit");
}

struct ThrowingJob : Job {
    bool oom;
    ThrowingJob(Fixture &fixture, std::shared_ptr<Record> record, bool value)
        : Job(fixture, std::move(record)), oom(value) {}
    void Run() override
    {
        Job::Run();
        if (oom) throw std::bad_alloc();
        throw std::runtime_error("run failed");
    }
};

void RunExceptionsDrain()
{
    Fixture fixture(true);
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    for (bool oom : {false, true}) {
        auto record = std::make_shared<Record>(); record->expectTls = true;
        Check(fixture.pool.TrySubmit([&] {return std::make_unique<ThrowingJob>(fixture, record, oom);}) == datasystem::K_OK,
              "submit failed");
    }
    fixture.pool.Stop();
    Check(fixture.counts.runFailed.load() == 2 && fixture.counts.retired.load() == 2
          && fixture.control.childDtors.load() == 2 && fixture.counts.bad.load() == 0, "run exception leaked Job or TLS");
}

struct ConcurrentStop {
    Pool *pool;
    std::atomic<unsigned> *entered;
    static void *Run(void *pointer)
    {
        auto &context = *static_cast<ConcurrentStop *>(pointer);
        ++*context.entered;
        context.pool->Stop();
        return nullptr;
    }
};
struct JoinStops {
    std::vector<bthread_t> ids;
    ~JoinStops() {for (auto id : ids) if (bthread_join(id, nullptr) != 0) std::terminate();}
};
void ConcurrentStopProgress()
{
    Fixture fixture;
    Check(fixture.pool.Start() == datasystem::K_OK, "start failed");
    bthread::CountdownEvent entered(1); Latch gate;
    std::atomic<unsigned> stopsEntered{0};
    ConcurrentStop context{&fixture.pool, &stopsEntered};
    JoinStops stoppers;
    stoppers.ids.reserve(8);
    Drain drain{fixture.pool, {&gate}, {}};
    Check(fixture.pool.TrySubmit([&] {
        auto job = std::make_unique<Job>(fixture, std::make_shared<Record>());
        job->entered = &entered; job->runGate = &gate.value; return job;
    }) == datasystem::K_OK, "submit failed");
    entered.wait();
    for (unsigned i = 0; i < 8; ++i) {
        bthread_t id = 0;
        Check(bthread_start_background(&id, nullptr, ConcurrentStop::Run, &context) == 0, "stop bthread start failed");
        stoppers.ids.push_back(id);
    }
    Until([&] {return stopsEntered.load() == 8;});
    gate.Release(); fixture.pool.Stop();
    Check(fixture.counts.retired.load() == 1, "concurrent Stop lost work");
}

using Test = std::pair<const char *, void (*)()>;
const Test tests[] = { {"fresh_child_tls", FreshChildTls}, {"capacity64", Capacity64},
    {"stop_held_child", StopHeldChild}, {"stop_reserved_factory", StopReservedFactory},
    {"retirement_capacity", RetirementCapacity}, {"partial_start", PartialStart},
    {"failed_copy_init", FailedCopyInit}, {"child_start_failure", ChildStartFailure},
    {"join_includes_tls_destruction", JoinIncludesTlsDestruction}, {"run_exceptions_drain", RunExceptionsDrain},
    {"concurrent_stop_progress", ConcurrentStopProgress} };
}

int main(int argc, char **argv)
{
    if ((argc == 1 || std::string(argv[1]) == "concurrent_stop_progress") && bthread_setconcurrency(4) != 0) {
        std::cerr << "FAIL isolated unit worker setup\n";
        return 2;
    }
    unsigned selected = 0, failed = 0;
    for (const auto &[name, run] : tests) {
        if (argc > 1 && std::string(argv[1]) != name) continue;
        ++selected;
        try {run(); std::cout << "PASS " << name << '\n';}
        catch (const std::exception &error) {++failed; std::cerr << "FAIL " << name << ": " << error.what() << '\n';}
    }
    if (!selected) return 2;
    return failed ? 1 : 0;
}
