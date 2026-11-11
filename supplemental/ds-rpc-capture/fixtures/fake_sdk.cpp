#include "capture_observe.h"
#ifdef DS_CAPTURE_FAULT_ALLOC
#include <new>
#endif
#ifdef DS_CAPTURE_SPLIT_TLS
extern "C" void DsBenchTestObserveFromSlotV2(const ds_bench_capture::RpcObservation*, const void*) noexcept;
extern "C" void DsBenchTestInstallHookV2(ds_bench_capture::TestHook, void*) noexcept;
namespace { thread_local void* sdk_caller_slot = nullptr; }
#endif

extern "C" __attribute__((visibility("default"))) void DsBenchFakeSdkEmitV2(
    const ds_bench_capture::RpcObservation* rpc) noexcept
{
    if (!rpc) return;
#ifdef DS_CAPTURE_FAULT_ALLOC
    void* (*volatile allocate)(std::size_t) = static_cast<void* (*)(std::size_t)>(&::operator new);
    void* allocation = allocate(8);
    ::operator delete(allocation);
#endif
#ifdef DS_CAPTURE_SPLIT_TLS
    DsBenchTestObserveFromSlotV2(rpc, sdk_caller_slot);
#else
    ds_bench_capture::Observe(*rpc);
#endif
}

#ifdef DS_CAPTURE_TESTING
extern "C" __attribute__((visibility("default"))) void DsBenchFakeInstallHookV2(
    ds_bench_capture::TestHook hook, void* context) noexcept
{
#ifdef DS_CAPTURE_SPLIT_TLS
    DsBenchTestInstallHookV2(hook, context);
#else
    ds_bench_capture::InstallTestHook(hook, context);
#endif
}
#endif
