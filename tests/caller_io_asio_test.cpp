#include "caller_io_drain.h"

#include <asio/io_context.hpp>
#include <asio/post.hpp>
#include <asio/steady_timer.hpp>
#include <chrono>
#include <iostream>
#include <functional>
#include <thread>

int main()
{
    using namespace std::chrono_literals;
    unsigned failures = 0;
    auto check = [&](bool condition, const char* name) {
        std::cout << (condition ? "PASS " : "FAIL ") << name << '\n';
        failures += !condition;
    };
    asio::io_context context;
    const auto owner = std::this_thread::get_id();
    unsigned callbacks = 0;
    bool correctThread = true;
    auto touched = [&] { ++callbacks; correctThread &= std::this_thread::get_id() == owner; };
    bool done = false;
    asio::post(context, [&] {
        done = true;
        touched();
        asio::post(context, touched);
    });
    context.run_one();
    check(done && callbacks == 1, "done before followup is observable");
    auto result = caller_io::drain(context, std::chrono::steady_clock::now() + 1s, [&] { return done; });
    check(result == caller_io::DrainResult::Idle && callbacks == 2, "real followup drained");
    asio::steady_timer timer(context, 1h);
    bool canceled = false;
    timer.async_wait([&](const std::error_code& error) { canceled = error == asio::error::operation_aborted; touched(); });
    timer.cancel();
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1s, [] { return true; });
    check(result == caller_io::DrainResult::Idle && canceled && callbacks == 3, "real timer cancel handler drained");
    asio::post(context, touched);
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1s, [] { return true; });
    check(result == caller_io::DrainResult::Idle && callbacks == 4, "restart after natural stop");
    timer.expires_after(1h);
    timer.async_wait([&](const std::error_code&) { touched(); });
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1ms, [] { return true; });
    check(result == caller_io::DrainResult::Deadline && !context.stopped(), "unresolved timer is not idle");
    timer.cancel();
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1s, [] { return true; });
    check(result == caller_io::DrainResult::Idle && callbacks == 5, "retain owner and drain after failed deadline");
    bool keepPosting = true;
    unsigned chain = 0;
    std::function<void()> followup;
    followup = [&] {
        ++chain;
        if (keepPosting) asio::post(context, followup);
    };
    asio::post(context, followup);
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1ms, [] { return true; });
    check(result == caller_io::DrainResult::Deadline && chain > 0, "ready callback chain cannot bypass deadline");
    keepPosting = false;
    result = caller_io::drain(context, std::chrono::steady_clock::now() + 1s, [] { return true; });
    check(result == caller_io::DrainResult::Idle, "ready chain owner retained until final drain");
    check(correctThread, "all real callbacks on owner thread");
    std::cout << "checks=9 failures=" << failures << '\n';
    return failures ? 1 : 0;
}
