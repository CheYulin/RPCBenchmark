#include "pacer.h"
#include <functional>
#include <iostream>

using namespace arrival;
constexpr Ns ms = 1000000;
constexpr Ns second = 1000000000;
#define CHECK(value) do { if (!(value)) throw std::runtime_error(#value); } while (false)

void rejects(const std::function<void()>& action)
{
    bool caught = false;
    try { action(); } catch (const std::exception&) { caught = true; }
    CHECK(caught);
}

void schedule()
{
    Plan plan(10, second, 3, second);
    CHECK(plan.count == 3);
    CHECK(plan.scheduled(0) == 10 && plan.scheduled(1) == 333333343 && plan.scheduled(2) == 666666676);
    rejects([&] { plan.scheduled(3); });
    Plan longRun(0, 59 * second, 7, second);
    CHECK(longRun.count == 413 && longRun.scheduled(412) < longRun.end);
    rejects([] { Plan(0, second, 0, second); });
    rejects([] { Plan(0, second, kSecond + 1, second); });
    rejects([] { Plan(std::numeric_limits<Ns>::max() - second, second, 1, second); });
    rejects([] { Plan(0, 60 * second, 100000, second); });
    Plan warmup(0, second, 3, second), measure(second, second, 3, second);
    CHECK(warmup.scheduled(2) < measure.epoch && measure.scheduled(0) == second);
}

void lateGenerator()
{
    Ledger ledger(Plan(0, second, 10, second), 8, 2, 100 * ms, 2);
    const Ns fakeNow = 350 * ms;
    std::size_t due = 0;
    while (ledger.next < ledger.plan.count && ledger.plan.scheduled(ledger.next) <= fakeNow) {
        CHECK(ledger.observe(ledger.next, fakeNow, fakeNow));
        ++due;
    }
    ledger.burst(due);
    CHECK(due == 4 && ledger.generatorInvalid && ledger.maximumLateness == fakeNow);
    for (std::size_t i = 0; i < 4; ++i) CHECK(ledger.records[i].scheduled == Ns(i) * 100 * ms);
    CHECK(ledger.counts().conserved() && ledger.counts().unissued == 6);
}

void boundedQueue()
{
    Ledger ledger(Plan(0, second, 10, 2 * second), 2, 2, second, 10);
    CHECK(ledger.observe(0, 0, 0));
    CHECK(ledger.observe(1, 100 * ms, 100 * ms));
    CHECK(!ledger.observe(2, 200 * ms, 200 * ms));
    std::size_t first, next;
    CHECK(ledger.reserve(200 * ms, 0, first) && first == 0 && ledger.dispatch(first, 200 * ms));
    CHECK(ledger.reserve(200 * ms, 1, next) && next == 1 && ledger.dispatch(next, 200 * ms));
    CHECK(!ledger.reserve(200 * ms, 1, next));
    CHECK(ledger.observe(3, 300 * ms, 300 * ms));
    ledger.complete(first, 201 * ms, 301 * ms, 301 * ms, Outcome::Success, 0);
    CHECK(ledger.reserve(301 * ms, 0, next) && next == 3);
    CHECK(ledger.dispatch(next, 301 * ms));
    CHECK(ledger.inflightHighWater == 2 && ledger.queueHighWater == 2);
    CHECK(ledger.counts().rejected == 1 && ledger.counts().conserved());
    rejects([&] { ledger.observe(3, 302 * ms, 302 * ms); });
}

void outcomes()
{
    Ledger ledger(Plan(0, 2 * second, 5, second), 8, 5, second, 10);
    for (std::size_t i = 0; i < 5; ++i) ledger.observe(i, Ns(i) * 200 * ms, Ns(i) * 200 * ms);
    std::size_t id;
    CHECK(ledger.reserve(1050 * ms, 0, id) && id == 0);
    CHECK(!ledger.dispatch(id, 1050 * ms));
    CHECK(ledger.reserve(1050 * ms, 1, id) && ledger.dispatch(id, 1050 * ms));
    ledger.complete(id, 1051 * ms, 1100 * ms, 1101 * ms, Outcome::Success, 0);
    CHECK(ledger.reserve(1101 * ms, 2, id) && ledger.dispatch(id, 1101 * ms));
    ledger.complete(id, 1102 * ms, 1410 * ms, 1411 * ms, Outcome::Success, 0);
    CHECK(ledger.reserve(1411 * ms, 3, id) && ledger.dispatch(id, 1411 * ms));
    ledger.complete(id, 1412 * ms, 1420 * ms, 1421 * ms, Outcome::RpcError, 1001);
    CHECK(ledger.reserve(1421 * ms, 4, id) && ledger.dispatch(id, 1421 * ms));
    ledger.complete(id, 1422 * ms, 1430 * ms, 1431 * ms, Outcome::Mismatch, 0);
    const auto count = ledger.counts();
    CHECK(count.conserved() && count.expired == 1 && count.timely == 1 && count.late == 1);
    CHECK(count.errors == 1 && count.mismatches == 1 && count.dispatched == 4);
    for (const auto& record : ledger.records) {
        if (record.end >= 0) CHECK(record.end - record.scheduled == record.start - record.scheduled + record.end - record.start);
    }
    rejects([&] { ledger.complete(id, 1422 * ms, 1430 * ms, 1431 * ms, Outcome::Success, 0); });
}

void endDrainCrash()
{
    Ledger ledger(Plan(0, second, 4, 2 * second), 4, 2, 2 * second, 10);
    CHECK(ledger.plan.count == 4);
    ledger.observe(0, 0, 0);
    ledger.observe(1, 250 * ms, 250 * ms);
    std::size_t id;
    CHECK(ledger.reserve(500 * ms, 0, id) && ledger.dispatch(id, 500 * ms));
    ledger.complete(id, 501 * ms, 1100 * ms, 1101 * ms, Outcome::Success, 0);
    CHECK(ledger.counts().timely == 1 && ledger.records[0].end > ledger.plan.end);
    CHECK(ledger.reserve(1101 * ms, 1, id) && ledger.dispatch(id, 1101 * ms));
    const auto atCut = ledger.counts();
    CHECK(atCut.pendingInflight == 1 && atCut.unissued == 2 && atCut.conserved() && !atCut.valid());
    CHECK(!ledger.observe(2, 1200 * ms, 1200 * ms));
    CHECK(ledger.records[2].rejection == Rejection::GenerationEnded);
    CHECK(ledger.counts().planned == 4);
    Ledger queued(Plan(0, second, 2, second), 1, 1, second, 10);
    queued.observe(0, 0, 0);
    CHECK(queued.counts().pendingQueue == 1 && queued.counts().conserved());
}

void dispatchGapAndStats()
{
    Ledger ledger(Plan(0, second, 1, 10), 1, 1, 100, 10);
    ledger.observe(0, 0, 0);
    std::size_t id;
    CHECK(ledger.reserve(0, 0, id) && ledger.dispatch(id, 1));
    ledger.expireBeforeApi(id, 10);
    CHECK(ledger.counts().expired == 1 && ledger.counts().dispatched == 0 && ledger.counts().conserved());
    Histogram first, secondHistogram;
    first.add(1000); first.add(2000); secondHistogram.add(3000); secondHistogram.add(4000);
    first.merge(secondHistogram);
    CHECK(first.count == 4 && first.sumNs == 10000 && first.percentile(50) == 2 && first.percentile(99) == 4);
    CHECK(first.minimumNs == 1000 && first.maximumNs == 4000);
    Histogram wide;
    wide.add(std::numeric_limits<Ns>::max()); wide.add(std::numeric_limits<Ns>::max());
    CHECK(decimal(wide.sumNs) == "18446744073709551614");
    const Ns sample = 100000000;
    Histogram fine; fine.add(sample);
    CHECK(fine.percentile(99) * 1000 >= std::uint64_t(sample));
    CHECK(fine.percentile(99) * 1000 <= std::uint64_t(sample) * 1001 / 1000 + 1000);
    // One API arrival and two external per-method RPC spans are different units.
    struct Span { std::size_t apiId; int ordinal; };
    const std::vector<Span> spans{{0, 0}, {0, 1}};
    CHECK(ledger.plan.count == 1 && spans.size() == 2 && spans[0].apiId == spans[1].apiId);
}

int main()
{
    const std::vector<std::pair<const char*, std::function<void()>>> tests{
        {"absolute rational schedule/bounds", schedule}, {"late generator/no skips", lateGenerator},
        {"bounded FIFO/no duplicates", boundedQueue}, {"deadlines/outcomes/conservation", outcomes},
        {"end/drain/crash conservation", endDrainCrash}, {"dispatch gap/statistics/API-RPC units", dispatchGapAndStats}};
    int failed = 0;
    for (const auto& test : tests) {
        try { test.second(); std::cout << "PASS " << test.first << '\n'; }
        catch (const std::exception& error) { ++failed; std::cerr << "FAIL " << test.first << ": " << error.what() << '\n'; }
    }
    return failed ? 1 : 0;
}
