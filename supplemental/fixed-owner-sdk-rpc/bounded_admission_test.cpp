#include "bounded_admission.h"
#include <array>
#include <cstdio>
#include <stdexcept>
#include <utility>
#include <vector>

using fixed_arrival::Row;
using fixed_arrival::State;
namespace {
void Check(bool value) { if (!value) throw std::runtime_error("bounded admission contract failed"); }
struct Clock {
    std::uint64_t now = 0, lateness = 0;
    bool first_only = false;
    std::uint64_t NowNs() const noexcept { return now; }
    void SleepUntil(std::uint64_t target) {
        if (now < target) now = target + lateness;
        if (first_only) lateness = 0;
    }
};
auto Rows() {
    return std::array<Row, 3>{{{0, 1'000'000'000}, {32, 1'064'000'000}, {64, 1'128'000'000}}};
}
void Finish(Row &row, Clock &clock, std::uint64_t work = 1) {
    clock.now += work;
    row.end_ns = row.validation_ns = row.observer_ns = clock.now;
}
void SoftLateCompletesOwner() {
    auto rows = Rows(); Clock clock; clock.lateness = 1'000'001;
    std::atomic<bool> abort{false}; unsigned called = 0;
    bounded_admission::Execute(rows, abort, clock, [&](Row &row) { ++called; Finish(row, clock); });
    Check(called == rows.size() && !abort);
    for (const auto &row : rows) Check(row.state == State::Returned && row.begin_ns - row.scheduled_ns == 1'000'001);
}
void ExactHardDispatchBoundary() {
    for (auto lateness : {63'999'999ULL, 64'000'000ULL}) {
        std::array<Row, 1> rows{{{0, 1'000'000'000}}}; Clock clock; clock.lateness = lateness;
        std::atomic<bool> abort{false}; unsigned called = 0;
        bounded_admission::Execute(rows, abort, clock, [&](Row &row) { ++called; Finish(row, clock, 0); });
        Check(called == (lateness == 63'999'999 ? 1u : 0u));
        Check(abort == (lateness == 64'000'000));
        Check(rows[0].state == (called ? State::Returned : State::RejectedLate));
    }
}
void ObserverBoundaryCancelsCatchup() {
    for (auto work : {63'999'999ULL, 64'000'000ULL, 64'000'001ULL}) {
        auto rows = Rows(); Clock clock; std::atomic<bool> abort{false}; unsigned called = 0;
        bounded_admission::Execute(rows, abort, clock, [&](Row &row) { ++called; Finish(row, clock, work); });
        Check(rows[0].state == State::Returned && rows[0].status == 0);
        Check(called == (work < 64'000'000 ? 3u : 1u));
        Check(abort == (work >= 64'000'000));
        if (abort) Check(rows[1].state == State::Cancelled && rows[2].state == State::Cancelled);
    }
}
void LastCallStillHasDebtBoundary() {
    std::array<Row, 1> rows{{{0, 1'000'000'000}}}; Clock clock; std::atomic<bool> abort{false};
    bounded_admission::Execute(rows, abort, clock, [&](Row &row) { Finish(row, clock, 64'000'000); });
    Check(abort && rows[0].state == State::Returned && rows[0].observer_ns == rows[0].scheduled_ns + 64'000'000);
}
void ExactIntegerBudgets() {
    Check(bounded_admission::SoftBudgetPassed(4, 4096));
    Check(!bounded_admission::SoftBudgetPassed(5, 4096));
    Check(bounded_admission::SoftBudgetPassed(32, 32768));
    Check(!bounded_admission::SoftBudgetPassed(33, 32768));
    Check(bounded_admission::SoftBudgetPassed(0, 128));
    Check(!bounded_admission::SoftBudgetPassed(1, 128));
    Check(!bounded_admission::SoftBudgetPassed(0, 0));
}
void FullPhaseThenBarrierReject() {
    std::atomic<bool> abort{false}; std::uint64_t called = 0, soft_late = 0;
    std::vector<Row> completed;
    for (std::uint64_t owner = 0; owner < 32; ++owner) {
        std::array<Row, 128> rows{};
        for (std::uint64_t round = 0; round < rows.size(); ++round) {
            rows[round].id = round * 32 + owner;
            rows[round].scheduled_ns = 1'000'000'000 + rows[round].id * 2'000'000;
        }
        Clock clock;
        if (owner < 5) { clock.lateness = 1'000'001; clock.first_only = true; }
        bounded_admission::Execute(rows, abort, clock, [&](Row &row) {
            ++called;
            Finish(row, clock);
        });
        for (const auto &row : rows) {
            Check(row.state == State::Returned);
            soft_late += row.begin_ns - row.scheduled_ns > bounded_admission::SoftLatenessNs;
            completed.push_back(row);
        }
    }
    Check(!abort && called == 4096 && completed.size() == 4096 && soft_late == 5);
    if (!bounded_admission::SoftBudgetPassed(soft_late, completed.size())) abort.store(true);
    auto next = Rows(); Clock clock; unsigned next_calls = 0;
    bounded_admission::Execute(next, abort, clock, [&](Row &) { ++next_calls; });
    Check(abort && next_calls == 0);
    for (const auto &row : completed) Check(row.state == State::Returned);
    for (const auto &row : next) Check(row.state == State::Cancelled);
}
void PriorAbortKeepsAllIDs() {
    auto rows = Rows(); Clock clock; std::atomic<bool> abort{true}; unsigned called = 0;
    bounded_admission::Execute(rows, abort, clock, [&](Row &) { ++called; });
    Check(called == 0);
    for (unsigned i = 0; i < rows.size(); ++i) Check(rows[i].id == i * 32 && rows[i].state == State::Cancelled);
}
void LegacyStrictDefaultUnchanged() {
    auto rows = Rows(); Clock clock; clock.lateness = 1'000'001;
    std::atomic<bool> abort{false}; unsigned called = 0;
    fixed_arrival::Execute(rows, abort, clock, 1'000'000, [&](Row &) { ++called; });
    Check(abort && called == 0 && rows[0].state == State::RejectedLate);
}
}
int main() {
    const std::pair<const char *, void (*)()> tests[] = {
        {"soft late completes", SoftLateCompletesOwner}, {"hard dispatch boundary", ExactHardDispatchBoundary},
        {"observer deadline and no catchup", ObserverBoundaryCancelsCatchup}, {"last-call debt", LastCallStillHasDebtBoundary},
        {"integer phase budgets", ExactIntegerBudgets}, {"complete cohort before phase rejection", FullPhaseThenBarrierReject},
        {"abort keeps all IDs", PriorAbortKeepsAllIDs}, {"legacy strict default", LegacyStrictDefaultUnchanged}
    };
    unsigned failed = 0;
    for (auto [name, test] : tests) {
        try { test(); std::printf("PASS %s\n", name); }
        catch (...) { ++failed; std::printf("FAIL %s\n", name); }
    }
    std::printf("BOUNDED_ADMISSION_TESTS total=%zu failed=%u\n", std::size(tests), failed);
    return failed ? 1 : 0;
}
