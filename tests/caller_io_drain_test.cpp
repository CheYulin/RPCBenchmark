#include <chrono>
#include <deque>
#include <functional>
#include <iostream>
#include <string_view>

#ifndef RPC_LAB_DRAIN_NEGATIVE
#include "caller_io_drain.h"
#endif

namespace {
struct FakeContext {
    bool exhausted = false;
    bool outstanding = false;
    unsigned restarts = 0;
    std::deque<std::function<void()>> queued;
    bool stopped() const { return exhausted; }
    void restart() { exhausted = false; ++restarts; }
    std::size_t run_one_until(std::chrono::steady_clock::time_point)
    {
        if (exhausted) return 0;
        if (queued.empty()) { exhausted = !outstanding; return 0; }
        auto task = std::move(queued.front());
        queued.pop_front();
        task();
        return 1;
    }
};

bool drain(FakeContext& context, bool ready)
{
#ifdef RPC_LAB_DRAIN_NEGATIVE
    (void)context;
    return ready;
#else
    return caller_io::drain(context, std::chrono::steady_clock::now() + std::chrono::seconds(1),
                            [ready] { return ready; }) == caller_io::DrainResult::Idle;
#endif
}
}

int main()
{
    unsigned failures = 0;
    auto check = [&failures](bool condition, std::string_view name) {
        std::cout << (condition ? "PASS " : "FAIL ") << name << '\n';
        failures += !condition;
    };
    {
        FakeContext context;
        unsigned executed = 0;
        context.queued.push_back([&] { ++executed; context.queued.push_back([&] { ++executed; }); });
        check(drain(context, true) && executed == 2 && context.stopped(), "done is not queued followup drained");
    }
    {
        FakeContext context;
        context.outstanding = true;
        check(!drain(context, true), "empty ready queue is not outstanding work drained");
    }
    {
        FakeContext context;
        context.exhausted = true;
        unsigned executed = 0;
        context.queued.push_back([&] { ++executed; });
        check(drain(context, true) && executed == 1 && context.restarts == 1, "stopped flag is not cancellation");
    }
    {
        FakeContext context;
        check(!drain(context, false), "natural idle still requires all roots complete");
    }
    {
        FakeContext context;
        check(drain(context, true) && context.stopped(), "natural empty work accepted");
    }
    std::cout << "checks=5 failures=" << failures << '\n';
    return failures ? 1 : 0;
}
