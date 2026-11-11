#include "capture_observe.h"
#ifndef DS_CAPTURE_TESTING
#error This bridge is only for the shared-library test fixture.
#endif

extern "C" __attribute__((visibility("default"))) void DsBenchTestObserveFromSlotV2(
    const ds_bench_capture::RpcObservation* observation, const void* slot) noexcept
{
    if (observation) ds_bench_capture::ObserveFromSlotForTest(*observation, slot);
}
extern "C" __attribute__((visibility("default"))) void DsBenchTestInstallHookV2(
    ds_bench_capture::TestHook hook, void* context) noexcept
{
    ds_bench_capture::InstallTestHook(hook, context);
}
