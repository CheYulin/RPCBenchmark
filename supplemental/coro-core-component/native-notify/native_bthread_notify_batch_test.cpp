#include "native_bthread_notify_batch.h"

#include <asio/io_context.hpp>
#include <asio/post.hpp>
#include <bthread/countdown_event.h>
#include <bthread/unstable.h>
#include <ylt/coro_io/io_context_pool.hpp>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <future>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using Batch = datasystem::coro_unary::diagnostic::NativeBthreadNotifyBatch;

void Check(bool value, const char *reason)
{
    if (!value) {
        throw std::runtime_error(reason);
    }
}

struct Control {
    std::mutex mutex;
    std::vector<bthread_t> ids;
    std::vector<unsigned> batches;
    std::vector<Batch::NativeId> origins;
    std::atomic<unsigned> ran{0}, postCalls{0}, tailCalls{0}, bad{0}, childDtors{0}, nativeDtors{0};
    Batch::NativeId expectedNative;
    std::atomic<bool> failStart{false}, failPost{false}, released{false};
    bthread::CountdownEvent gate{1};
    bthread_key_t key = INVALID_BTHREAD_KEY;
    bool hasKey = false;
    struct KeyValue {
        Control *control;
        bool heap;
    } nativeValue{this, false};
    Control()
    {
        ids.reserve(256);
        batches.reserve(256);
        origins.reserve(256);
    }
    void Release()
    {
        if (!released.exchange(true)) {
            gate.signal();
        }
    }
};
std::atomic<Control *> current{nullptr};

void DestroyKey(void *pointer)
{
    auto *value = static_cast<Control::KeyValue *>(pointer);
    if (value->heap) {
        ++value->control->childDtors;
        delete value;
    } else {
        ++value->control->nativeDtors;
    }
}

int StartProbe(bthread_t *tid, const bthread_attr_t *attr, void *(*fn)(void *), void *arg)
{
    auto &state = *current.load();
    Check(tid && attr && attr->stack_type == BTHREAD_STACKTYPE_NORMAL
          && attr->flags == BTHREAD_NOSIGNAL && attr->keytable_pool == nullptr, "fresh normal NOSIGNAL attributes lost");
    if (state.failStart.exchange(false)) {
        return ENOMEM;
    }
    const int code = bthread_start_background(tid, attr, fn, arg);
    if (code == 0) {
        std::lock_guard lock(state.mutex);
        state.ids.push_back(*tid);
    }
    return code;
}

void FlushProbe() noexcept
{
    auto &state = *current.load();
    {
        std::lock_guard lock(state.mutex);
        state.batches.push_back(Batch::InspectCurrent().pending);
        state.origins.push_back(std::this_thread::get_id());
    }
    bthread_flush();
}

void PostProbe(asio::io_context &context, Batch::NativeId origin)
{
    auto &state = *current.load();
    ++state.postCalls;
    if (state.failPost.exchange(false)) {
        throw std::bad_alloc();
    }
    asio::post(context.get_executor(), [origin] {
        auto &control = *current.load();
        if (origin != std::this_thread::get_id()) {
            ++control.bad;
        }
        ++control.tailCalls;
        Batch::RunQueued(origin);
    });
}

Batch::Hooks Hooks()
{
    return { StartProbe, FlushProbe, PostProbe };
}

class Fixture {
public:
    Control control;
    coro_io::io_context_pool pool{1};
    std::thread runner;

    Fixture() : runner([this] { pool.run(); })
    {
        Check(current.exchange(&control) == nullptr, "fixture overlap");
    }
    ~Fixture()
    {
        control.Release();
        Stop();
        Join();
        if (control.hasKey) {
            bthread_key_delete(control.key);
        }
        current.store(nullptr);
    }
    void Stop()
    {
        if (runner.joinable()) {
            pool.stop();
            runner.join();
        }
    }
    void Join()
    {
        for (;;) {
            bthread_t id = 0;
            {
                std::lock_guard lock(control.mutex);
                if (control.ids.empty()) {
                    return;
                }
                id = control.ids.back();
                control.ids.pop_back();
            }
            if (bthread_join(id, nullptr) != 0) {
                std::terminate();
            }
        }
    }
    template <typename F>
    void Do(F &&fn)
    {
        auto promise = std::make_shared<std::promise<void>>();
        auto future = promise->get_future();
        auto *executor = pool.get_executor();
        asio::post(executor->get_asio_executor(), [promise, fn = std::forward<F>(fn)]() mutable {
            try {
                fn();
                promise->set_value();
            } catch (...) {
                Batch::FlushPrior();
                promise->set_exception(std::current_exception());
            }
        });
        Check(future.wait_for(std::chrono::seconds(2)) == std::future_status::ready, "native IO callback timeout");
        future.get();
    }
    asio::io_context *Context()
    {
        return &pool.get_executor()->context();
    }
};

void FreshChildTlsAndFallback()
{
    Fixture fixture;
    auto &state = fixture.control;
    Check(bthread_key_create(&state.key, DestroyKey) == 0, "key create failed");
    state.hasKey = true;
    fixture.Do([&] {
        Check(bthread_setspecific(state.key, &state.nativeValue) == 0, "native key set failed");
        Batch attempt(Hooks());
        Check(attempt.Start(fixture.Context(), nullptr, [&state] {
            if (bthread_self() == INVALID_BTHREAD || bthread_getspecific(state.key) != nullptr) {
                ++state.bad;
            }
            if (bthread_setspecific(state.key, new Control::KeyValue{ &state, true }) != 0) {
                ++state.bad;
            }
            ++state.ran;
        }) == 0, "native start failed");
        Check(bthread_getspecific(state.key) == &state.nativeValue, "native TLS changed");
    });
    fixture.Join();
    bthread_t fallback = 0;
    Batch attempt(Hooks());
    Check(attempt.Start(nullptr, &fallback, [&state] { ++state.ran; }) == 0, "ordinary native fallback failed");
    Check(bthread_join(fallback, nullptr) == 0, "fallback join failed");
    bthread_t outer = 0;
    auto *target = fixture.Context();
    Check(datasystem::StartBackgroundTask(&outer, [&state, target] {
        const auto parent = bthread_self();
        bthread_t child = 0;
        Batch nested(Hooks());
        if (nested.Start(target, &child, [&state, parent] {
                if (bthread_self() == INVALID_BTHREAD || bthread_self() == parent) {
                    ++state.bad;
                }
                ++state.ran;
            }) != 0 || bthread_join(child, nullptr) != 0) {
            ++state.bad;
        }
    }) == 0, "bthread fallback parent failed");
    Check(bthread_join(outer, nullptr) == 0, "bthread fallback parent join failed");
    fixture.Stop();
    Check(state.ran == 3 && state.childDtors == 1 && state.nativeDtors == 1 && state.bad == 0, "fresh child TLS teardown failed");
}

void Capacity64Reject()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        {
            Batch warm(Hooks());
            Check(warm.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "warm start failed");
        }
        bthread_flush();
        fixture.Join();
        for (unsigned i = 0; i < 64; ++i) {
            Batch attempt(Hooks());
            Check(attempt.Start(fixture.Context(), nullptr, [&state] { state.gate.wait(); ++state.ran; }) == 0, "held start failed");
        }
        {
            Batch rejected(Hooks());
        }
        Check(Batch::InspectCurrent().pending == 0, "rejected attempt did not flush prior");
        Check(state.ids.size() == 64, "unstarted rejected attempt allocated a task");
    });
    state.Release();
    fixture.Join();
    fixture.Stop();
    Check(state.ran == 65, "held work lost");
}

void SparseTailAndThreshold()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        for (unsigned i = 0; i < 17; ++i) {
            Batch attempt(Hooks());
            Check(attempt.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "batch start failed");
        }
        const auto snapshot = Batch::InspectCurrent();
        Check(snapshot.queued && snapshot.pending == 1 && state.postCalls == 1, "threshold/tail policy missing");
        Check(state.batches.size() == 1 && state.batches[0] == 16, "threshold did not flush16");
    });
    fixture.Stop();
    fixture.Join();
    Check(state.ran == 17 && state.tailCalls == 1 && state.batches.size() == 2 && state.batches[1] == 1, "sparse tail not flushed");
}

struct ThrowOnCopy {
    ThrowOnCopy() = default;
    ThrowOnCopy(const ThrowOnCopy &) { throw std::bad_alloc(); }
    void operator()() const {}
};

void FactoryAndStartFailure()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        Batch first(Hooks());
        Check(first.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "prior task failed");
        ThrowOnCopy callable;
        try {
            Batch failed(Hooks());
            failed.Start(fixture.Context(), nullptr, callable);
            Check(false, "copy did not throw");
        } catch (const std::bad_alloc &) {
        }
        Check(Batch::InspectCurrent().pending == 0, "factory failure did not flush prior");
        {
            Batch beforeExpression(Hooks());
            Check(beforeExpression.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "prior expression task failed");
        }
        try {
            Batch failed(Hooks());
            failed.Start(fixture.Context(), nullptr, []() -> ThrowOnCopy { throw std::bad_alloc(); }());
            Check(false, "factory expression did not throw");
        } catch (const std::bad_alloc &) {
        }
        Check(Batch::InspectCurrent().pending == 0, "expression failure did not flush prior");
        Batch next(Hooks());
        Check(next.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "next task failed");
        auto owned = std::make_shared<int>(1);
        std::weak_ptr<int> observer = owned;
        state.failStart = true;
        {
            Batch failed(Hooks());
            Check(failed.Start(fixture.Context(), nullptr, [owner = std::move(owned)] { (void)owner; }) == ENOMEM, "start failure lost errno");
        }
        Check(observer.expired() && Batch::InspectCurrent().pending == 0, "failed callable or prior work leaked");
    });
    fixture.Stop();
    fixture.Join();
    Check(state.ran == 3, "failure lost accepted work");
}

void PostFailureAccepted()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        state.failPost = true;
        Batch attempt(Hooks());
        Check(attempt.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "post failure misreported accepted task");
        const auto snapshot = Batch::InspectCurrent();
        Check(state.postCalls == 1 && snapshot.pending == 0 && !snapshot.queued, "post failure did not flush/reset");
    });
    fixture.Stop();
    fixture.Join();
    Check(state.ran == 1 && state.tailCalls == 0, "post failure lost/duplicated work");
}

void ZeroJobsPendingCallbackStop()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        Batch attempt(Hooks());
        Check(attempt.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "start failed");
        Batch::FlushPrior();
        fixture.Join();
        Check(state.ran == 1 && Batch::InspectCurrent().queued && state.tailCalls == 0, "zero jobs with callback not represented");
    });
    fixture.Stop();
    Check(state.tailCalls == 1 && state.bad == 0, "normal pool stop discarded queued tail");
}

void StartupAndForeignContextFallback()
{
    {
        coro_io::io_context_pool neverRun{1};
        auto *executor = neverRun.get_executor();
        asio::post(executor->get_asio_executor(), [&] {
            bthread_t id = 0;
            Batch attempt;
            Check(attempt.Start(&executor->context(), &id, [] {}) == 0, "pre-run fallback failed");
            Check(bthread_join(id, nullptr) == 0, "pre-run fallback join failed");
        });
        neverRun.stop();
    }
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        asio::io_context foreign;
        asio::post(foreign, [&] {
            bthread_t id = 0;
            Batch attempt(Hooks());
            Check(attempt.Start(&foreign, &id, [&state] { ++state.ran; }) == 0, "foreign fallback failed");
            Check(bthread_join(id, nullptr) == 0, "foreign fallback join failed");
            Check(state.ids.empty() && state.postCalls == 0, "foreign context used NOSIGNAL batching");
        });
        Check(foreign.poll() == 1, "foreign callback missing");
    });
    fixture.Stop();
    Check(state.ran == 1, "foreign fallback task lost");
}

void SameNativeProducer()
{
    Fixture fixture;
    auto &state = fixture.control;
    fixture.Do([&] {
        state.expectedNative = std::this_thread::get_id();
        Batch attempt(Hooks());
        Check(attempt.Start(fixture.Context(), nullptr, [&state] { ++state.ran; }) == 0, "start failed");
    });
    fixture.Stop();
    fixture.Join();
    Check(state.ran == 1 && state.tailCalls == 1 && state.bad == 0, "same producer tail failed");
    Check(!state.origins.empty(), "no flush observed");
    for (const auto id : state.origins) {
        Check(id == state.expectedNative, "flush moved native producer");
    }
}

using Test = std::pair<const char *, void (*)()>;
const Test tests[] = {
    { "fresh_child_tls_and_fallback", FreshChildTlsAndFallback },
    { "capacity64_reject", Capacity64Reject },
    { "sparse_tail_and_threshold", SparseTailAndThreshold },
    { "factory_and_start_failure", FactoryAndStartFailure },
    { "post_failure_accepted", PostFailureAccepted },
    { "zero_jobs_pending_callback_stop", ZeroJobsPendingCallbackStop },
    { "startup_and_foreign_context_fallback", StartupAndForeignContextFallback },
    { "same_native_producer", SameNativeProducer },
};
}  // namespace

int main(int argc, char **argv)
{
    unsigned selected = 0, failed = 0;
    for (const auto &[name, run] : tests) {
        if (argc > 1 && std::string(argv[1]) != name) {
            continue;
        }
        ++selected;
        try {
            run();
            std::cout << "PASS " << name << '\n';
        } catch (const std::exception &error) {
            ++failed;
            std::cerr << "FAIL " << name << ": " << error.what() << '\n';
        }
    }
    return selected ? (failed ? 1 : 0) : 2;
}
