#include "fixed_owner_plan.h"
#include "capture_helper.h"
#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <vector>
using benchmark_capture::FixedOwnerConfig;
using benchmark_capture::FixedOwnerPlan;
namespace {
std::size_t checks = 0;
void Check(bool value, const char *reason) { ++checks; if (!value) throw std::runtime_error(reason); }
template<class F> void Reject(F call, const char *reason) {
    bool rejected=false; try { call(); } catch(const std::invalid_argument&) { rejected=true; }
    catch(const std::out_of_range&) { rejected=true; } catch(const std::overflow_error&) { rejected=true; }
    Check(rejected,reason);
}
}
int main() {
 try {
    FixedOwnerPlan plan(FixedOwnerConfig{});
    Check(plan.Callers()==32 && plan.Calls(1)==128 && plan.Calls(2)==1024,"profile calls");
    Check(plan.TotalCalls(1)==4096 && plan.TotalCalls(2)==32768,"honest global counts");
    Check(plan.MaxSelectedPerOwner()==1152,"global count must not be replicated per owner");
    Check(plan.CaptureBytes()==41877504 && plan.CaptureBytes()+plan.MetadataReserveBytes==42926080,"exact bounded ABI2 bytes");
    Check(plan.CaptureBytes()+plan.MetadataReserveBytes<=plan.BudgetBytes,"fixed128MiB budget");
    Check(plan.ScheduledNs(100,2,31,0)==62000100 && plan.ScheduledNs(100,2,0,1)==64000100,"interleaved schedule");
    Check(plan.EndNs(100,2)==65536000100 && plan.Id(2,31,1023)==32767,"finite half-open endpoint");
    FixedOwnerPlan thirds(FixedOwnerConfig{1,1,2,3,17,1});
    Check(thirds.ScheduledNs(7,2,0,1)==333333340,"nondivisible arrival retains floor");
    Check(thirds.EndNs(7,2)==666666674,"nondivisible window duration uses ceil");
    Reject([&]{thirds.EndNs(std::numeric_limits<std::uint64_t>::max()-666666666,2);},"ceil-only end overflow must reject");
    FixedOwnerConfig c{3,2,4,7,17,3}; FixedOwnerPlan sample(c);
    std::vector<std::uint64_t> expected(3); std::set<std::uint64_t> ids;
    for (std::uint64_t phase=1; phase<=2; ++phase) {
      const auto count=phase==1?2ULL:4ULL;
      for (std::uint64_t id=0;id<count*3;++id)
        expected[id%3]+=benchmark_capture::Selected(17,phase,id,3);
      ids.clear();
      for (std::size_t owner=0;owner<3;++owner)
        for(std::uint64_t round=0;round<count;++round) Check(ids.insert(sample.Id(phase,owner,round)).second,"duplicate partition");
      Check(ids.size()==count*3 && *ids.begin()==0 && *ids.rbegin()==count*3-1,"all planned IDs");
    }
    for(std::size_t owner=0;owner<3;++owner) Check(sample.SelectedForOwner(owner)==expected[owner],"per-owner selector partition");
    Check(sample.MaxSelectedPerOwner()==*std::max_element(expected.begin(),expected.end()),"max owner bound");
    Check(!sample.ValidateBegin(0,1,1) && !sample.ValidateBegin(3,1,0) && !sample.ValidateBegin(0,0,0)
          && !sample.ValidateBegin(0,UINT64_C(0x100000001),0) && !sample.ValidateBegin(0,2,12),"invalid context must reject before indexing");
    Check(sample.ValidateBegin(2,2,11),"last valid context");
    Reject([&]{sample.Id(1,0,2);},"invalid round"); Reject([&]{sample.Calls(3);},"invalid phase");
    Reject([&]{sample.Selected(1,6);},"invalid selected ID"); Reject([&]{sample.SelectedForOwner(3);},"invalid selected owner");
    Reject([&]{plan.ScheduledNs(std::numeric_limits<std::uint64_t>::max(),2,1,0);},"schedule addition overflow");
    Reject([&]{plan.EndNs(std::numeric_limits<std::uint64_t>::max(),1);},"phase end overflow");
    for(int invalid=0;invalid<8;++invalid) {
      auto x=FixedOwnerConfig{};
      switch(invalid) {case 0:x.callers=0;break;case 1:x.callers=129;break;case 2:x.warm_calls_per_owner=0;break;
       case 3:x.measure_calls_per_owner=0;break;case 4:x.rate_per_second=0;break;case 5:x.rate_per_second=1000000001;break;
       case 6:x.sample_modulus=0;break;default:x.warm_calls_per_owner=std::numeric_limits<std::uint64_t>::max();}
      Reject([&]{FixedOwnerPlan bad(x);},"invalid configuration");
    }
    Reject([]{FixedOwnerPlan tooLarge(FixedOwnerConfig{128,1000,1000,500,17,1});},"budget cannot silently grow");
    // A fake service stall crosses two owner slots. Future IDs and epoch stay unchanged; lateness invalidates the cohort.
    FixedOwnerPlan debt(FixedOwnerConfig{2,1,3,4,17,1});
    std::uint64_t now=100;std::vector<std::uint64_t> scheduled,observedIds,lateness;
    for(std::uint64_t round=0;round<3;++round) {
      const auto due=debt.ScheduledNs(100,2,0,round);now=std::max(now,due);
      scheduled.push_back(due);observedIds.push_back(debt.Id(2,0,round));lateness.push_back(now-due);
      if(round==0)now+=1250000000;
    }
    Check(observedIds==std::vector<std::uint64_t>({0,2,4}),"stall cannot skip IDs");
    Check(scheduled==std::vector<std::uint64_t>({100,500000100,1000000100}),"stall cannot reset epoch");
    Check(lateness[1]==750000000 && lateness[2]==250000000 && *std::max_element(lateness.begin(),lateness.end())>1000000,"late debt must not be hidden");
    std::cout<<"FIXED_OWNER_PLAN_TESTS_PASS checks="<<checks<<'\n';return 0;
 } catch(const std::exception& e) {std::cerr<<"PLAN_TEST_FAIL "<<e.what()<<'\n';return 1;}
}
