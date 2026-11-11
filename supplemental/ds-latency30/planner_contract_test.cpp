#include <cstdint>
#include <iostream>
#include <vector>
#ifdef PACER_OLD_COMPLETION_DRIVEN
#include "old_completion_planner.h"
#else
#include "pacer.h"
#endif

std::vector<std::int64_t> planned(std::int64_t serviceNs)
{
#ifdef PACER_OLD_COMPLETION_DRIVEN
    return oldCompletionTimes(serviceNs);
#else
    (void)serviceNs;
    arrival::Plan plan(0, 1000000000, 3, 2000000000);
    std::vector<std::int64_t> times;
    for (std::size_t id = 0; id < plan.count; ++id) times.push_back(plan.scheduled(id));
    return times;
#endif
}

int main()
{
    const auto fast = planned(100000000);
    const auto slow = planned(400000000);
    const std::vector<std::int64_t> expected{0, 333333333, 666666666};
    if (fast != slow || fast != expected) {
        std::cerr << "FAIL completion independence/absolute arrivals fast=" << fast.size()
                  << " slow=" << slow.size() << " expected=3\n";
        return 1;
    }
    std::cout << "PASS completion-independent absolute arrival contract\n";
}
