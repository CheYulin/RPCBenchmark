#ifdef PRIMING_LEGACY_READY
#include "legacy_priming_ready.h"
using Gate = LegacyRegisteredReady;
#else
#include "priming_gate.h"
using Gate = arrival::CallerReadiness;
#endif

#include <functional>
#include <iostream>
#include <stdexcept>
#include <vector>

#define CHECK(value) do { if (!(value)) throw std::runtime_error(#value); } while (false)

void pendingCaller()
{
    Gate gate(2);
    gate.Register(0);
    gate.Register(1);
    gate.Complete(0, true);
    const auto finishPendingApi = [&] { gate.Complete(1, true); };
    CHECK(!gate.AllComplete());
    CHECK(!gate.ClaimReady());
    finishPendingApi();
    CHECK(gate.AllComplete() && gate.ClaimReady());
}

void failedCaller()
{
    Gate gate(2);
    gate.Register(0);
    gate.Register(1);
    gate.Complete(0, true);
    gate.Complete(1, false);
    CHECK(gate.AllComplete());
    CHECK(!gate.ClaimReady());
    CHECK(!gate.ClaimReady());
}

void readyOnce()
{
    Gate gate(32);
    for (std::size_t i = 0; i < 32; ++i) gate.Register(i);
    for (std::size_t i = 0; i < 32; ++i) gate.Complete(i, true);
    unsigned epochs = 0;
    for (unsigned attempt = 0; attempt < 4; ++attempt) if (gate.ClaimReady()) ++epochs;
    CHECK(epochs == 1);
}

void misuse()
{
    Gate gate(1);
    bool rejected = false;
    try { gate.Complete(0, true); } catch (const std::logic_error&) { rejected = true; }
    CHECK(rejected);
    gate.Register(0);
    rejected = false;
    try { gate.Register(0); } catch (const std::logic_error&) { rejected = true; }
    CHECK(rejected);
    gate.Complete(0, true);
    rejected = false;
    try { gate.Complete(0, true); } catch (const std::logic_error&) { rejected = true; }
    CHECK(rejected);
}

void immediateSetup()
{
    Gate gate(2);
    for (std::size_t i = 0; i < 2; ++i) {
        gate.Register(i);
        gate.Complete(i, true);
    }
    CHECK(gate.AllComplete() && gate.ClaimReady());
}

int main()
{
    const std::vector<std::pair<const char*, std::function<void()>>> tests{
        {"unfinished priming blocks epoch", pendingCaller}, {"failed priming blocks epoch", failedCaller},
        {"all ready claims epoch once", readyOnce}, {"duplicate lifecycle rejected", misuse},
        {"immediate setup releases readiness", immediateSetup}};
    unsigned failed = 0;
    for (const auto& test : tests) {
        try { test.second(); std::cout << "PASS " << test.first << '\n'; }
        catch (const std::exception& error) {
            ++failed;
            std::cerr << "FAIL " << test.first << ": " << error.what() << '\n';
        }
    }
    return failed ? 1 : 0;
}
