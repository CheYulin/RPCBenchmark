#include <chrono>

namespace native_poll_test {
void Phase(const void *, int, std::chrono::steady_clock::time_point);
}
#define DS_CORO_IO_NATIVE_POLL_TEST_PHASE(context, phase, deadline) \
    ::native_poll_test::Phase(context, phase, deadline)
#include <ylt/coro_io/io_context_pool.hpp>

#include <asio/defer.hpp>
#include <asio/steady_timer.hpp>
#include <atomic>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace native_poll_test {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

struct Observation {
    const void *context;
    int phase;
    Clock::time_point deadline;
    Clock::time_point observed;
};

struct Control {
    std::mutex mutex;
    std::condition_variable changed;
    std::vector<Observation> phases;
    std::vector<std::shared_ptr<asio::steady_timer>> timers;
    std::vector<std::thread::id> nativeIds;
    std::vector<unsigned> callbacks;
    unsigned markerDestructors = 0;
    unsigned nativeDestructors = 0;
    unsigned badIdentity = 0;
    unsigned sequence = 0;
    unsigned releaseSequence = 0;
    unsigned timerSequence = 0;
    unsigned stopSequence = 0;
    bool callbackEntered = false;
    bool release = false;
    bool stopStarted = false;
    bool stopReturned = false;
};

std::atomic<Control *> observer{nullptr};

void Phase(const void *context, int phase, Clock::time_point deadline)
{
    auto *state = observer.load(std::memory_order_acquire);
    if (state == nullptr) {
        return;
    }
    std::lock_guard lock(state->mutex);
    state->phases.push_back({context, phase, deadline, Clock::now()});
    state->changed.notify_all();
}

void Check(bool value, const char *message)
{
    if (!value) {
        throw std::runtime_error(message);
    }
}

template <typename Predicate>
void Wait(const std::shared_ptr<Control> &state, Predicate ready, const char *message)
{
    std::unique_lock lock(state->mutex);
    Check(state->changed.wait_for(lock, 2s, ready), message);
}

struct NativeLifetime {
    std::shared_ptr<Control> state;
    ~NativeLifetime()
    {
        if (state) {
            std::lock_guard lock(state->mutex);
            ++state->nativeDestructors;
            state->changed.notify_all();
        }
    }
};
thread_local NativeLifetime nativeLifetime;

void ObserveIdentity(const std::shared_ptr<Control> &state,
                     coro_io::ExecutorWrapper<> *executor, unsigned index)
{
    const auto identity = std::this_thread::get_id();
    std::lock_guard lock(state->mutex);
    if (*coro_io::get_current() != &executor->context()
        || executor->checkout() != &executor->context()
        || !executor->currentThreadInExecutor()
        || !executor->context().get_executor().running_in_this_thread()
        || (state->callbacks[index] != 0 && state->nativeIds[index] != identity)) {
        ++state->badIdentity;
    }
    state->nativeIds[index] = identity;
    ++state->callbacks[index];
    nativeLifetime.state = state;
    state->changed.notify_all();
}

struct Marker {
    std::shared_ptr<Control> state;
    coro_io::ExecutorWrapper<> *executor;
    Marker(std::shared_ptr<Control> control, coro_io::ExecutorWrapper<> *owner)
        : state(std::move(control)), executor(owner)
    {
    }
    ~Marker()
    {
        std::lock_guard lock(state->mutex);
        if (*coro_io::get_current() != &executor->context()
            || !executor->currentThreadInExecutor()) {
            ++state->badIdentity;
        }
        ++state->markerDestructors;
        state->changed.notify_all();
    }
};

class StopperJoin {
public:
    StopperJoin(std::thread &thread, std::shared_ptr<Control> state) : thread_(thread), state_(std::move(state)) {}
    ~StopperJoin()
    {
        {
            std::lock_guard lock(state_->mutex);
            if (!state_->release) {
                state_->release = true;
                state_->releaseSequence = ++state_->sequence;
            }
            state_->changed.notify_all();
        }
        if (thread_.joinable()) {
            thread_.join();
        }
    }
private:
    std::thread &thread_;
    std::shared_ptr<Control> state_;
};

class Fixture {
public:
    explicit Fixture(unsigned size) : state(std::make_shared<Control>()), pool(size)
    {
        state->callbacks.resize(size);
        state->nativeIds.resize(size);
        observer.store(state.get(), std::memory_order_release);
        runner = std::thread([this] { pool.run(); });
    }

    ~Fixture()
    {
        Release();
        Finish();
        observer.store(nullptr, std::memory_order_release);
    }

    void Release()
    {
        std::lock_guard lock(state->mutex);
        if (!state->release) {
            state->release = true;
            state->releaseSequence = ++state->sequence;
        }
        state->changed.notify_all();
    }

    void Finish()
    {
        if (finished) {
            return;
        }
        for (auto &executor : pool.get_all_executor()) {
            asio::post(executor->context(), [control = state, context = &executor->context()] {
                std::vector<std::shared_ptr<asio::steady_timer>> timers;
                {
                    std::lock_guard lock(control->mutex);
                    timers = control->timers;
                }
                for (auto &timer : timers) {
                    if (&timer->get_executor().context() == context) {
                        timer->cancel();
                    }
                }
            });
        }
        pool.stop();
        runner.join();
        finished = true;
    }

    std::shared_ptr<Control> state;
    coro_io::io_context_pool pool;

private:
    std::thread runner;
    bool finished = false;
};

void IdentityAndOwnedData()
{
    Fixture fixture(2);
    auto state = fixture.state;
    unsigned index = 0;
    for (auto &owned : fixture.pool.get_all_executor()) {
        auto *executor = owned.get();
        asio::post(executor->context(), [state, executor, index] {
            ObserveIdentity(state, executor, index);
            executor->set_data("native-poll-lifetime",
                std::make_shared<Marker>(state, executor));
            asio::defer(executor->context(), [state, executor, index] {
                ObserveIdentity(state, executor, index);
            });
        });
        ++index;
    }
    Wait(state, [&] { return state->callbacks[0] == 2 && state->callbacks[1] == 2; },
         "deferred real Asio tasks did not drain");
    fixture.Finish();
    std::lock_guard lock(state->mutex);
    Check(state->badIdentity == 0, "context/executor/native TLS identity changed");
    Check(state->nativeIds[0] != state->nativeIds[1], "two contexts used one native worker");
    Check(state->markerDestructors == 2, "executor-owned data did not clear before stop returned");
    Check(state->nativeDestructors == 2, "native TLS destructors not joined before stop returned");
}

void FixedIdleBudgetThenBlocking()
{
    Fixture fixture(1);
    auto state = fixture.state;
    auto *executor = fixture.pool.get_all_executor()[0].get();
    asio::post(executor->context(), [state, executor] {
        ObserveIdentity(state, executor, 0);
        auto timer = std::make_shared<asio::steady_timer>(executor->context(), 1h);
        {
            std::lock_guard lock(state->mutex);
            state->timers.push_back(timer);
        }
        timer->async_wait([state, timer](const std::error_code &) {
            std::lock_guard lock(state->mutex);
            state->changed.notify_all();
        });
    });
    Wait(state, [&] {
        unsigned blocking = 0;
        bool ended = false;
        for (const auto &row : state->phases) {
            blocking += row.phase == 1;
            ended |= row.phase == 3;
        }
        return ended && blocking >= 2;
    }, "nonzero fixed idle window did not end and return to run_one");
    {
        std::lock_guard lock(state->mutex);
        std::vector<Observation> begin, end;
        for (const auto &row : state->phases) {
            if (row.phase == 2) {
                begin.push_back(row);
            }
            if (row.phase == 3) {
                end.push_back(row);
            }
        }
        Check(begin.size() == 1 && end.size() == 1, "idle polling renewed its budget or looped without a ready task");
        Check(begin[0].context == &executor->context() && end[0].context == begin[0].context,
              "polling moved to another context");
        Check(begin[0].deadline == end[0].deadline && end[0].observed >= begin[0].deadline,
              "fixed idle deadline changed or ended before its declared deadline");
    }
    asio::post(executor->context(), [state, executor] { ObserveIdentity(state, executor, 0); });
    Wait(state, [&] { return state->callbacks[0] == 2; }, "blocking run_one did not resume for newly posted work");
    fixture.Finish();
    std::lock_guard lock(state->mutex);
    Check(state->badIdentity == 0 && state->nativeDestructors == 1,
          "idle/wakeup shutdown changed native ownership");
}

void StopWaitsForHeldCallbackAndTimer()
{
    Fixture fixture(1);
    auto state = fixture.state;
    auto *executor = fixture.pool.get_all_executor()[0].get();
    asio::post(executor->context(), [state, executor] {
        ObserveIdentity(state, executor, 0);
        auto timer = std::make_shared<asio::steady_timer>(executor->context(), 30ms);
        timer->async_wait([state, executor, timer](const std::error_code &error) {
            ObserveIdentity(state, executor, 0);
            std::lock_guard lock(state->mutex);
            if (error) {
                ++state->badIdentity;
            }
            state->timerSequence = ++state->sequence;
            state->changed.notify_all();
        });
        std::unique_lock lock(state->mutex);
        state->callbackEntered = true;
        state->changed.notify_all();
        state->changed.wait(lock, [&] { return state->release; });
    });
    Wait(state, [&] { return state->callbackEntered; }, "held callback never entered");
    std::thread stopper([&fixture, state] {
        {
            std::lock_guard lock(state->mutex);
            state->stopStarted = true;
            state->changed.notify_all();
        }
        fixture.Finish();
        {
            std::lock_guard lock(state->mutex);
            state->stopReturned = true;
            state->stopSequence = ++state->sequence;
            state->changed.notify_all();
        }
    });
    StopperJoin joinOnExit(stopper, state);
    Wait(state, [&] { return state->stopStarted; }, "stop controller never started");
    fixture.Release();
    stopper.join();
    std::lock_guard lock(state->mutex);
    Check(state->badIdentity == 0 && state->callbacks[0] == 2, "pending real timer was abandoned or canceled by normal stop");
    Check(state->releaseSequence < state->timerSequence && state->timerSequence < state->stopSequence,
          "stop returned before the held callback and pending timer drained");
    Check(state->stopReturned && state->nativeDestructors == 1, "stop did not join its native worker/TLS teardown");
}
}  // namespace native_poll_test

int main(int argc, char **argv)
{
    try {
        native_poll_test::Check(argc == 2, "one case number is required");
        const std::string which(argv[1]);
        if (which == "1") {
            native_poll_test::IdentityAndOwnedData();
        } else if (which == "2") {
            native_poll_test::FixedIdleBudgetThenBlocking();
        } else if (which == "3") {
            native_poll_test::StopWaitsForHeldCallbackAndTimer();
        } else {
            throw std::runtime_error("unknown case");
        }
        std::cout << "PASS native-poll-real-asio case=" << which << '\n';
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL native-poll-real-asio " << error.what() << '\n';
        return 1;
    }
}
