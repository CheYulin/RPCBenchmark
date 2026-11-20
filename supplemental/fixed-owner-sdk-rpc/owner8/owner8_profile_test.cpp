#include "owner8_admission.h"
#include "owner8_profile.h"

#include <array>
#include <atomic>
#include <cstdio>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {
using fixed_arrival::Row;
using fixed_arrival::State;

void Check(bool value)
{
    if (!value) throw std::runtime_error("owner8 contract failed");
}

struct PacerClock {
    mutable std::uint64_t now = 0;
    mutable std::uint64_t last_read = 0;
    std::uint64_t step = 100'000;
    std::int64_t wake_offset = 0;
    std::vector<std::uint64_t> sleeps;

    std::uint64_t NowNs() const noexcept
    {
        last_read = now;
        now += step;
        return last_read;
    }
    void SleepUntil(std::uint64_t target)
    {
        sleeps.push_back(target);
        now = static_cast<std::uint64_t>(static_cast<std::int64_t>(target) + wake_offset);
    }
};

struct AdmissionClock {
    std::uint64_t now = 0, lateness = 0;
    bool first_only = false;
    std::uint64_t NowNs() const noexcept { return now; }
    void SleepUntil(std::uint64_t target)
    {
        if (now < target) now = target + lateness;
        if (first_only) lateness = 0;
    }
};

void Finish(Row &row, AdmissionClock &clock, std::uint64_t work = 1)
{
    clock.now += work;
    row.end_ns = row.validation_ns = row.observer_ns = clock.now;
}

void GeometryAndBusinessTrace()
{
    Check(owner8_profile::Callers == 8 && owner8_profile::Rate == 500);
    Check(owner8_profile::FormalWarmCalls * owner8_profile::Callers == 4096);
    Check(owner8_profile::FormalMeasureCalls * owner8_profile::Callers == 32768);
    Check(owner8_profile::SmokeWarmCalls * owner8_profile::Callers == 128);
    Check(owner8_profile::SmokeMeasureCalls * owner8_profile::Callers == 256);
    Check(owner8_admission::OwnerPeriodNs == owner8_profile::Callers * 1'000'000'000ULL / owner8_profile::Rate);
    std::array<std::uint64_t, 32> set_visits{};
    for (std::uint64_t id = 0; id < 32768; ++id) {
        const auto old_owner = id % 32;
        const auto old_round = id / 32;
        Check(owner8_profile::SetKey(id) == old_owner);
        Check(owner8_profile::GetKey(id) == (old_round + old_owner) % 64);
        Check((id / 8) * 8 + id % 8 == id);
        ++set_visits[owner8_profile::SetKey(id)];
    }
    for (auto visits : set_visits) Check(visits == 1024);
    for (std::size_t key = 0; key < 64; ++key) Check(owner8_profile::IsSetHotKey(key) == (key < 32));
}

void EarlyWakeAndNeverEarly()
{
    PacerClock clock;
    constexpr std::uint64_t target = 10'000'000;
    owner8_profile::WaitUntil(clock, target);
    Check(clock.sleeps == std::vector<std::uint64_t>{target - 1'000'000});
    Check(clock.last_read >= target);
    Check(clock.last_read < target + clock.step);
}

void EarlyLateAndNearWake()
{
    for (std::int64_t offset : {-200'000LL, 1'500'000LL}) {
        PacerClock clock;
        clock.wake_offset = offset;
        owner8_profile::WaitUntil(clock, 10'000'000);
        Check(clock.sleeps == std::vector<std::uint64_t>{9'000'000});
        Check(clock.last_read >= 10'000'000);
        if (offset > 1'000'000) Check(clock.last_read == static_cast<std::uint64_t>(9'000'000 + offset));
    }
    for (auto now : {9'500'000ULL, 10'000'000ULL, 10'100'000ULL}) {
        PacerClock clock;
        clock.now = now;
        owner8_profile::WaitUntil(clock, 10'000'000);
        Check(clock.sleeps.empty() && clock.last_read >= 10'000'000);
    }
    PacerClock near_zero;
    owner8_profile::WaitUntil(near_zero, 500'000);
    Check(near_zero.sleeps.empty() && near_zero.last_read >= 500'000);
}

void PacedIDsAndEpochStayFixed()
{
    struct Clock {
        PacerClock raw;
        std::uint64_t NowNs() const noexcept { return raw.NowNs(); }
        void SleepUntil(std::uint64_t target) { owner8_profile::WaitUntil(raw, target); }
    } clock;
    std::array<Row, 3> rows{{{3, 1'006'000'000}, {11, 1'022'000'000}, {19, 1'038'000'000}}};
    const auto original = rows;
    std::atomic<bool> abort{false};
    std::vector<std::uint64_t> calls;
    owner8_admission::Execute(rows, abort, clock, [&](Row &row) {
        Check(row.begin_ns >= row.scheduled_ns);
        calls.push_back(row.id);
        row.end_ns = row.validation_ns = row.observer_ns = clock.NowNs();
    });
    Check(!abort && calls == std::vector<std::uint64_t>({3, 11, 19}));
    for (std::size_t i = 0; i < rows.size(); ++i) {
        Check(rows[i].id == original[i].id && rows[i].scheduled_ns == original[i].scheduled_ns);
        Check(rows[i].state == State::Returned);
    }
}

void Exact16msDispatchBoundary()
{
    for (auto late : {15'999'999ULL, 16'000'000ULL}) {
        std::array<Row, 1> rows{{{0, 1'000'000'000}}};
        AdmissionClock clock;
        clock.lateness = late;
        std::atomic<bool> abort{false};
        unsigned calls = 0;
        owner8_admission::Execute(rows, abort, clock, [&](Row &row) { ++calls; Finish(row, clock, 0); });
        Check(calls == (late < 16'000'000 ? 1u : 0u));
        Check(abort == (late >= 16'000'000));
        Check(rows[0].state == (calls ? State::Returned : State::RejectedLate));
    }
}

void ObserverLastCallAndNoCatchup()
{
    for (auto work : {15'999'999ULL, 16'000'000ULL, 16'000'001ULL}) {
        std::array<Row, 2> rows{{{0, 1'000'000'000}, {8, 1'016'000'000}}};
        AdmissionClock clock;
        std::atomic<bool> abort{false};
        unsigned calls = 0;
        owner8_admission::Execute(rows, abort, clock, [&](Row &row) { ++calls; Finish(row, clock, work); });
        Check(calls == (work < 16'000'000 ? 2u : 1u));
        Check(abort == (work >= 16'000'000));
        if (abort) Check(rows[1].state == State::Cancelled);
    }
    std::array<Row, 1> last{{{32767, 1'000'000'000}}};
    AdmissionClock clock;
    std::atomic<bool> abort{false};
    owner8_admission::Execute(last, abort, clock, [&](Row &row) { Finish(row, clock, 16'000'000); });
    Check(abort && last[0].state == State::Returned);
}

void FullPhaseSoftBudgetAndPriorAbort()
{
    Check(owner8_admission::SoftBudgetPassed(4, 4096) && !owner8_admission::SoftBudgetPassed(5, 4096));
    Check(owner8_admission::SoftBudgetPassed(32, 32768) && !owner8_admission::SoftBudgetPassed(33, 32768));
    Check(owner8_admission::SoftBudgetPassed(0, 128) && !owner8_admission::SoftBudgetPassed(1, 128));
    Check(owner8_admission::SoftBudgetPassed(0, 256) && !owner8_admission::SoftBudgetPassed(1, 256));
    Check(!owner8_admission::SoftBudgetPassed(0, 0));
    std::atomic<bool> abort{false};
    std::uint64_t called = 0, soft_late = 0;
    for (std::uint64_t owner = 0; owner < 8; ++owner) {
        std::array<Row, 512> rows{};
        for (std::uint64_t round = 0; round < rows.size(); ++round) {
            rows[round].id = round * 8 + owner;
            rows[round].scheduled_ns = 1'000'000'000 + rows[round].id * 2'000'000;
        }
        AdmissionClock clock;
        if (owner < 5) { clock.lateness = 1'000'001; clock.first_only = true; }
        owner8_admission::Execute(rows, abort, clock, [&](Row &row) { ++called; Finish(row, clock); });
        for (const auto &row : rows) {
            Check(row.state == State::Returned);
            soft_late += row.begin_ns - row.scheduled_ns > 1'000'000;
        }
    }
    Check(!abort && called == 4096 && soft_late == 5);
    if (!owner8_admission::SoftBudgetPassed(soft_late, called)) abort.store(true);
    std::array<Row, 2> next{{{0, 0}, {8, 0}}};
    AdmissionClock clock;
    unsigned next_calls = 0;
    owner8_admission::Execute(next, abort, clock, [&](Row &) { ++next_calls; });
    Check(next_calls == 0 && next[0].id == 0 && next[1].id == 8);
    for (const auto &row : next) Check(row.state == State::Cancelled);
}

void OriginalArrivalGateStillRejectsEarlyAndStrictLate()
{
    std::array<Row, 1> rows{{{0, 1'000'000'000}}};
    AdmissionClock clock;
    clock.lateness = 1'000'001;
    std::atomic<bool> abort{false};
    fixed_arrival::Execute(rows, abort, clock, 1'000'000, [&](Row &) { Check(false); });
    Check(abort && rows[0].state == State::RejectedLate);
}
}

int main()
{
    const std::pair<const char *, void (*)()> tests[] = {
        {"geometry and unchanged global key trace", GeometryAndBusinessTrace},
        {"early wake and no early return", EarlyWakeAndNeverEarly},
        {"early late near and already-late wake", EarlyLateAndNearWake},
        {"pacing preserves IDs and epoch", PacedIDsAndEpochStayFixed},
        {"strict 16ms dispatch boundary", Exact16msDispatchBoundary},
        {"observer last call and no catchup", ObserverLastCallAndNoCatchup},
        {"full phase integer budget and prior abort", FullPhaseSoftBudgetAndPriorAbort},
        {"original strict arrival gate", OriginalArrivalGateStillRejectsEarlyAndStrictLate}
    };
    unsigned failed = 0;
    for (auto [name, test] : tests) {
        try { test(); std::printf("PASS %s\n", name); }
        catch (...) { ++failed; std::printf("FAIL %s\n", name); }
    }
    std::printf("OWNER8_PROFILE_TESTS total=%zu failed=%u\n", std::size(tests), failed);
    return failed ? 1 : 0;
}
