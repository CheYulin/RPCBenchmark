#include "owner8_queued_admission.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using owner8_queued::Row;
using fixed_arrival::State;
constexpr std::uint64_t Epoch = 1'000'000'000;

void Check(bool ok, const char *reason) {
    if (!ok) throw std::runtime_error(reason);
}
struct FakeClock {
    std::uint64_t now = Epoch - 100, wake_delay = 0;
    bool early = false;
    std::uint64_t NowNs() const noexcept { return now; }
    void SleepUntil(std::uint64_t target) {
        now = early ? target - 1 : std::max(now, target) + wake_delay;
        wake_delay = 0;
    }
};
std::vector<Row> Rows(std::size_t count) {
    std::vector<Row> rows(count);
    for (std::size_t i = 0; i < count; ++i) {
        rows[i].id = i * 8 + 2;
        rows[i].scheduled_ns = Epoch + i * owner8_queued::OwnerPeriodNs;
    }
    return rows;
}
void Finish(Row &row, FakeClock &clock, std::uint64_t service = 100) {
    row.kv_call_begin_ns = clock.NowNs();
    clock.now += service; row.end_ns = clock.NowNs();
    clock.now += 3; row.validation_ns = clock.NowNs();
    clock.now += 5; row.observer_ns = clock.NowNs();
}
void Complete(const std::vector<Row> &rows, const std::vector<std::uint64_t> &called, bool abort) {
    Check(!abort && called.size() == rows.size(), "late/debt cancelled planned calls");
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto &r = rows[i];
        Check(r.id == i * 8 + 2 && called[i] == r.id, "FIFO/ID changed");
        Check(r.scheduled_ns == Epoch + i * owner8_queued::OwnerPeriodNs, "schedule rebased");
        Check(r.state == State::Returned, "incomplete row");
        Check(r.head_enter_ns <= r.begin_ns && r.scheduled_ns <= r.begin_ns && r.begin_ns <= r.kv_call_begin_ns
              && r.kv_call_begin_ns <= r.end_ns && r.end_ns <= r.validation_ns && r.validation_ns <= r.observer_ns,
              "invalid actual timeline");
        if (i) Check(rows[i-1].observer_ns <= r.head_enter_ns, "same owner overlapped");
        const auto headDelay = r.head_enter_ns > r.scheduled_ns ? r.head_enter_ns-r.scheduled_ns : 0;
        Check(r.begin_ns-r.scheduled_ns == headDelay + r.begin_ns-std::max(r.scheduled_ns,r.head_enter_ns),
              "queue decomposition");
    }
}
void OnTime() {
    auto rows=Rows(4); FakeClock clock; std::atomic<bool> abort{false}; std::vector<std::uint64_t> called;
    owner8_queued::Execute(rows,abort,clock,[&](Row&r){called.push_back(r.id);Finish(r,clock);});
    Complete(rows,called,abort.load());
}
void SchedulerLate() {
    auto rows=Rows(4); FakeClock clock;clock.wake_delay=17'000'000;
    std::atomic<bool> abort{false};std::vector<std::uint64_t> called;
    owner8_queued::Execute(rows,abort,clock,[&](Row&r){called.push_back(r.id);Finish(r,clock);});
    Complete(rows,called,abort.load());Check(rows[0].begin_ns-rows[0].scheduled_ns==17'000'000,"late observation lost");
}
void PriorDebt() {
    auto rows=Rows(4);FakeClock clock;std::atomic<bool> abort{false};std::vector<std::uint64_t> called;
    owner8_queued::Execute(rows,abort,clock,[&](Row&r){called.push_back(r.id);Finish(r,clock,called.size()==1?47'000'000:100);});
    Complete(rows,called,abort.load());Check(rows[1].head_enter_ns>rows[1].scheduled_ns,"backlog not retained");
    const auto gap=static_cast<std::int64_t>(rows[1].scheduled_ns)-static_cast<std::int64_t>(rows[0].observer_ns);
    Check(gap<0,"signed previous overlap discarded");
}
void LastDebt() {
    auto rows=Rows(1);FakeClock clock;std::atomic<bool> abort{false};std::vector<std::uint64_t> called;
    owner8_queued::Execute(rows,abort,clock,[&](Row&r){called.push_back(r.id);Finish(r,clock,47'000'000);});
    Complete(rows,called,abort.load());Check(!owner8_queued::ObserverWithinPeriod(rows[0]),"last debt observation lost");
}
void SoftLate() {
    auto rows=Rows(2);FakeClock clock;clock.wake_delay=1'000'001;
    std::atomic<bool> abort{false};std::vector<std::uint64_t> called;
    owner8_queued::Execute(rows,abort,clock,[&](Row&r){called.push_back(r.id);Finish(r,clock);});
    Complete(rows,called,abort.load());Check(!owner8_queued::SoftBudgetPassed(1,rows.size()),"soft observation fixture");
}
void BusinessAndBytes() {
    for (bool mismatch:{false,true}) {
        auto rows=Rows(3);FakeClock clock;std::atomic<bool> abort{false};unsigned calls=0;
        owner8_queued::Execute(rows,abort,clock,[&](Row&r){++calls;Finish(r,clock);r.status=mismatch?0:17;r.mismatch=mismatch;});
        Check(abort.load()&&calls==1&&rows[0].state==State::Returned,"business failure accepted");
        Check(rows[1].state==State::Cancelled&&rows[2].state==State::Cancelled&&rows[2].id==18,"cancelled IDs lost");
    }
}
void InvalidTime() {
    {
        auto rows=Rows(2);FakeClock clock;clock.early=true;std::atomic<bool> abort{false};unsigned calls=0;
        owner8_queued::Execute(rows,abort,clock,[&](Row&){++calls;});
        Check(abort.load()&&calls==0&&rows[0].state==State::RejectedEarly&&rows[1].state==State::Cancelled,"early call accepted");
    }
    {
        auto rows=Rows(2);FakeClock clock;std::atomic<bool> abort{false};
        owner8_queued::Execute(rows,abort,clock,[&](Row&r){Finish(r,clock);r.end_ns=r.begin_ns-1;});
        Check(abort.load()&&rows[0].state==State::InvalidTime&&rows[1].state==State::Cancelled,"reverse time accepted");
    }
}
void Exception() {
    auto rows=Rows(2);FakeClock clock;std::atomic<bool> abort{false};
    owner8_queued::Execute(rows,abort,clock,[&](Row&){throw std::runtime_error("injected callback error");});
    Check(abort.load()&&rows[0].state==State::Exception&&rows[0].status==fixed_arrival::CallbackFailure
          &&rows[1].state==State::Cancelled,"exception accepted");
}
}
int main() {
    struct Test {const char*name;void(*run)();};
    const std::array<Test,8> tests={{{"on_time_fifo",OnTime},{"scheduler_late_keeps_all_ids",SchedulerLate},
      {"prior_debt_fifo_no_rebase",PriorDebt},{"last_call_debt_allowed",LastDebt},{"soft_late_is_observation",SoftLate},
      {"business_and_bytes_fail",BusinessAndBytes},{"invalid_time_fails",InvalidTime},{"exception_fails",Exception}}};
    unsigned failed=0;
    for (const auto&t:tests) {
        try {t.run();std::cout<<"PASS "<<t.name<<'\n';}
        catch(const std::exception&e){++failed;std::cout<<"FAIL "<<t.name<<'\n';std::cerr<<t.name<<": "<<e.what()<<'\n';}
    }
    std::cout<<"OWNER8_QUEUED_TESTS total="<<tests.size()<<" failed="<<failed<<'\n';
    return failed?1:0;
}
