#ifndef DS_BENCH_RPC_CAPTURE_OBSERVE_H
#define DS_BENCH_RPC_CAPTURE_OBSERVE_H

#include "capture_abi.h"

namespace ds_bench_capture {
enum class SpanState : uint8_t { Missing, Valid, Invalid };
struct RpcObservation {
    uint64_t send_ns, recv_ns;
    uint16_t method;
    bool failed;
    int32_t controller_error, ds_error;
    uint64_t server_processing_ns = 0, server_execution_ns = 0;
    SpanState processing_state = SpanState::Missing, execution_state = SpanState::Missing;
};

__attribute__((visibility("hidden"))) void Observe(const RpcObservation&) noexcept;

#ifdef DS_CAPTURE_TESTING
enum class TestPoint { BeforeRecordPublish = 1, ErrorLocked = 2, BeginBeforeActive = 3,
                       BeginAfterActive = 4, SnapshotFrozen = 5, AfterRecordPublish = 6, ErrorAdmitted = 7 };
using TestHook = void (*)(TestPoint, void*);
__attribute__((visibility("hidden"))) void InstallTestHook(TestHook, void*) noexcept;
__attribute__((visibility("hidden"))) void ObserveFromSlotForTest(const RpcObservation&, const void*) noexcept;
#endif
}
#endif
