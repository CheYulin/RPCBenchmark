#include "fixed_owner_plan.h"
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
using benchmark_capture::FixedOwnerPlan;
using benchmark_capture::FixedOwnerConfig;
int main() {
 try {
  FixedOwnerPlan p(FixedOwnerConfig{1,1,2,3,17,1});
  if (p.ScheduledNs(7,2,0,1)!=333333340) throw std::runtime_error("arrival schedule must retain floor");
  if (p.EndNs(7,2)!=666666674) throw std::runtime_error("half-open duration must ceil nondivisible end");
  bool overflow=false;
  try { (void)p.EndNs(std::numeric_limits<std::uint64_t>::max()-666666666,2); }
  catch (const std::overflow_error&) { overflow=true; }
  if (!overflow) throw std::runtime_error("ceil-only one-nanosecond overflow must reject");
  std::cout<<"FIXED_OWNER_END_CEIL_PASS\n";return 0;
 } catch(const std::exception& e) {std::cerr<<"END_BOUNDARY_FAIL "<<e.what()<<'\n';return 1;}
}
