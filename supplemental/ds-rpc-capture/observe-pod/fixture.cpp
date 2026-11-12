#include "../capture_observe.h"
#include <array>
#include <condition_variable>
#include <cstring>
#include <functional>
#include <iostream>
#include <mutex>
#include <string>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <vector>

using ds_bench_capture::RpcObservation;
using ds_bench_capture::SpanState;
extern "C" __attribute__((noinline, noclone, noipa, visibility("default")))
void fixture_observe(const RpcObservation* value) noexcept
{
    asm volatile("" : : "r"(value) : "memory");
}
struct State {
    std::mutex mutex;
    std::condition_variable cv;
    std::array<uint32_t, 33> tids{};
    unsigned ready = 0, generation = 0, done = 0, checked_bytes = 0, bad_bytes = 0;
    bool quit = false, null_read = false;
};
struct Join {
    State& state;
    std::vector<std::thread>& threads;
    ~Join()
    {
        { std::lock_guard<std::mutex> lock(state.mutex); state.quit = true; }
        state.cv.notify_all();
        for (auto& thread : threads) if (thread.joinable()) thread.join();
    }
};
void Caller(State& state, unsigned index)
{
    unsigned seen = 0;
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        state.tids[index] = static_cast<uint32_t>(syscall(SYS_gettid));
        ++state.ready;
    }
    state.cv.notify_all();
    for (;;) {
        bool null_read;
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            state.cv.wait(lock, [&] { return state.quit || seen != state.generation; });
            if (state.quit) return;
            seen = state.generation; null_read = state.null_read;
        }
        unsigned checked = 0, bad = 0;
        if (null_read) fixture_observe(nullptr);
        else for (unsigned sequence = 0; sequence < 2; ++sequence) {
            RpcObservation value;
            std::memset(static_cast<void*>(&value), 0xa5, sizeof(value));
            value.send_ns = 100000 + 1000 * index + 10 * sequence;
            value.recv_ns = value.send_ns + 5;
            value.method = sequence ? 4 : 1; value.failed = sequence != 0;
            value.controller_error = sequence ? -17 : 0;
            value.ds_error = sequence ? -19 : 0;
            value.server_processing_ns = 3; value.server_execution_ns = 2;
            value.processing_state = SpanState::Valid; value.execution_state = SpanState::Valid;
            auto* bytes = reinterpret_cast<volatile unsigned char*>(&value);
            for (unsigned offset : {19u, 28u, 29u, 30u, 31u, 50u, 51u, 52u, 53u, 54u, 55u}) bytes[offset] = 0xa5;
            for (unsigned offset : {19u, 28u, 29u, 30u, 31u, 50u, 51u, 52u, 53u, 54u, 55u}) {
                ++checked; bad += bytes[offset] != 0xa5;
            }
            fixture_observe(&value);
        }
        { std::lock_guard<std::mutex> lock(state.mutex); ++state.done; state.checked_bytes += checked; state.bad_bytes += bad; }
        state.cv.notify_all();
    }
}
int main()
{
    try {
        State state;
        std::vector<std::thread> threads;
        Join join{state, threads};
        threads.reserve(33);
        for (unsigned i = 0; i < 33; ++i) threads.emplace_back(Caller, std::ref(state), i);
        std::unique_lock<std::mutex> lock(state.mutex);
        state.cv.wait(lock, [&] { return state.ready == 33; });
        std::cout << "{\"event\":\"ready\",\"pid\":" << getpid() << ",\"tids\":[";
        for (unsigned i = 0; i < 33; ++i) std::cout << (i ? "," : "") << state.tids[i];
        std::cout << "]}" << std::endl;
        lock.unlock();
        for (std::string command; std::getline(std::cin, command);) {
            if (command == "exit") return 0;
            if (command != "emit" && command != "null") return 2;
            lock.lock(); state.done = 0; state.checked_bytes = 0; state.bad_bytes = 0; state.null_read = command == "null"; ++state.generation;
            state.cv.notify_all();
            state.cv.wait(lock, [&] { return state.done == 33; });
            lock.unlock();
            std::cout << "{\"event\":\"done\",\"command\":\"" << command << "\",\"padding_checked_bytes\":" << state.checked_bytes << ",\"padding_bad_bytes\":" << state.bad_bytes << "}" << std::endl;
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "SETUP_FAILURE " << error.what() << std::endl;
        return 2;
    }
}
