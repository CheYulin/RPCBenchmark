#include "closed_loop_plan.h"
#include <cassert>
#include <iostream>
#include <set>
#include <stdexcept>
using namespace closed_loop;
int main() {
    std::set<std::uint64_t> ids;
    for (std::uint32_t caller=0; caller<Callers; ++caller)
        for (std::uint64_t ordinal=0; ordinal<WarmCalls; ++ordinal)
            assert(ids.insert(ArrivalId(1, caller, ordinal)).second);
    assert(ids.size()==WarmCalls*Callers && *ids.begin()==0 && *ids.rbegin()==WarmCalls*Callers-1);
    assert(ArrivalId(2,Callers-1,MeasureCalls-1)==Callers*MeasureCalls-1);
    unsigned rejected=0;
    for (auto test : {0,1,2}) {
        try {
            if(test==0) ArrivalId(0,0,0);
            if(test==1) ArrivalId(1,Callers,0);
            if(test==2) ArrivalId(2,0,MeasureCalls);
        } catch(const std::out_of_range &) { ++rejected; }
    }
    assert(rejected==3);
    std::uint64_t selected=0;
    for(std::uint32_t phase=1;phase<=2;++phase)
        for(std::uint64_t id=0;id<PhaseCalls(phase)*Callers;++id) selected+=Selected(phase,id);
    assert(selected==3592);
    assert(CaptureBytes(selected)==128077824);
    assert(CaptureBytes(selected)+MetadataReserve<=CaptureBudget);
    assert(CaptureBytes(selected*2)+MetadataReserve>CaptureBudget);
    Histogram h;
    for (auto n : {0ULL,1ULL,999ULL,1000ULL,1001ULL,8192000ULL,8192001ULL,41000000ULL}) h.Record(n);
    assert(h.Count()==8 && h.Upper(50)==1 && h.Upper(99)==65536);
    assert(h.BucketUpper(8192)==16384);
    assert(h.BucketUpper(Histogram::Buckets-1)>=41000);
    Histogram extra; extra.Record(1234000);h.Merge(extra);
    assert(h.Count()==9 && h.Upper(50)==2);
    Histogram empty;
    assert(empty.Count()==0 && empty.Upper(99)==0);
    std::cout<<"CLOSED_LOOP_PLAN_TESTS_PASS 7\n";
}
