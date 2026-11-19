#include "arrival_loop.h"
#include <array>
#include <cstdio>
#include <stdexcept>
#include <utility>
using namespace fixed_arrival;
namespace {
void Check(bool value) { if (!value) throw std::runtime_error("arrival contract failed"); }
struct Clock {
    std::uint64_t now = 0, sleeps = 0, overshoot = 0;
    bool early = false;
    std::uint64_t NowNs() const noexcept { return now; }
    void SleepUntil(std::uint64_t target) {
        ++sleeps;
        if (early) now = target - 1;
        else if (now < target) now = target + overshoot;
    }
};
auto Rows() { return std::array<Row, 3>{{{0, 10}, {32, 20}, {64, 30}}}; }
void Finish(Row &row, Clock &clock, std::uint64_t work = 1) {
    clock.now += work;
    row.end_ns = clock.now; row.validation_ns = clock.now; row.observer_ns = clock.now;
}
void OnTime() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{false}; unsigned calls = 0;
    Execute(rows, abort, clock, 1, [&](Row &row) { ++calls; Finish(row, clock); });
    Check(calls == rows.size() && !abort && clock.sleeps == rows.size());
    for (unsigned i = 0; i < rows.size(); ++i) {
        Check(rows[i].id == i * 32 && rows[i].scheduled_ns == (i + 1) * 10);
        Check(rows[i].begin_ns == rows[i].scheduled_ns && rows[i].state == State::Returned);
    }
}
void PreservesDebt() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{false}; unsigned calls = 0;
    Execute(rows, abort, clock, 1, [&](Row &row) { ++calls; Finish(row, clock, 25); });
    Check(calls == 1 && abort && rows[0].state == State::Returned);
    Check(rows[1].id == 32 && rows[1].scheduled_ns == 20 && rows[1].begin_ns == 35);
    Check(rows[1].state == State::RejectedLate && rows[2].state == State::Cancelled);
    Check(rows[2].id == 64 && rows[2].scheduled_ns == 30 && rows[2].begin_ns == 0);
}
void EarlyWakeIsInvalid() {
    auto rows = Rows(); Clock clock; clock.early = true;
    std::atomic<bool> abort{false}; unsigned calls = 0;
    Execute(rows, abort, clock, 1, [&](Row &) { ++calls; });
    Check(calls == 0 && abort && rows[0].state == State::RejectedEarly);
    Check(rows[1].state == State::Cancelled && rows[2].state == State::Cancelled);
}
void CallbackExceptionEndsBeforeReturn() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{false}; unsigned paired = 0; bool active = false;
    struct EndGuard {
        bool &active; unsigned &paired;
        ~EndGuard() { active = false; ++paired; }
    };
    Execute(rows, abort, clock, 1, [&](Row &) {
        active = true; EndGuard guard{active, paired}; clock.now += 2;
        throw std::runtime_error("controlled callback exception");
    });
    Check(!active && paired == 1 && abort && rows[0].state == State::Exception);
    Check(rows[0].status == CallbackFailure && rows[0].begin_ns <= rows[0].end_ns);
    Check(rows[1].state == State::Cancelled && rows[2].state == State::Cancelled);
}
void PriorAbortHasAllIdentities() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{true}; unsigned calls = 0;
    Execute(rows, abort, clock, 1, [&](Row &) { ++calls; });
    Check(calls == 0 && clock.sleeps == 0);
    for (unsigned i = 0; i < rows.size(); ++i)
        Check(rows[i].state == State::Cancelled && rows[i].id == i * 32 && rows[i].scheduled_ns == (i + 1) * 10);
}
void InclusiveLatenessBoundary() {
    for (auto overshoot : {1u, 2u}) {
        std::array<Row, 1> rows{{{0, 10}}}; Clock clock; clock.overshoot = overshoot;
        std::atomic<bool> abort{false}; unsigned calls = 0;
        Execute(rows, abort, clock, 1, [&](Row &row) { ++calls; Finish(row, clock); });
        Check(calls == (overshoot == 1 ? 1u : 0u));
        Check(abort == (overshoot != 1));
    }
}
void InvalidTimestampCannotPass() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{false};
    Execute(rows, abort, clock, 1, [&](Row &row) { row.end_ns = row.begin_ns - 1; });
    Check(abort && rows[0].state == State::InvalidTime && rows[1].state == State::Cancelled);
}
void ErrorStopsFurtherCalls() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{false}; unsigned calls = 0;
    Execute(rows, abort, clock, 1, [&](Row &row) { ++calls; Finish(row, clock); row.status = 9; });
    Check(calls == 1 && abort && rows[0].state == State::Returned && rows[0].status == 9);
    Check(rows[1].state == State::Cancelled && rows[2].state == State::Cancelled);
}
}
int main() {
    const std::pair<const char*, void(*)()> tests[] = {
        {"on-time identity", OnTime}, {"real debt", PreservesDebt}, {"early wake", EarlyWakeIsInvalid},
        {"exception End pairing", CallbackExceptionEndsBeforeReturn}, {"abort identities", PriorAbortHasAllIdentities},
        {"inclusive lateness", InclusiveLatenessBoundary}, {"invalid timestamp", InvalidTimestampCannotPass},
        {"nonzero API stops", ErrorStopsFurtherCalls}
    };
    unsigned failed = 0;
    for (auto [name, test] : tests) {
        try { test(); std::printf("PASS %s\n", name); }
        catch (...) { ++failed; std::printf("FAIL %s\n", name); }
    }
    std::printf("ARRIVAL_TESTS total=%zu failed=%u\n", std::size(tests), failed);
    return failed ? 1 : 0;
}
