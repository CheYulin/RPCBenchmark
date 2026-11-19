#ifndef DS_BENCH_RPC_CAPTURE_ABI_H
#define DS_BENCH_RPC_CAPTURE_ABI_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
#define DS_CAPTURE_NOEXCEPT noexcept
extern "C" {
#else
#define DS_CAPTURE_NOEXCEPT
#endif

#define DS_CAPTURE_ABI 2u
#define DS_CAPTURE_MAX_CALLERS 128u
#define DS_CAPTURE_METHODS 11u

enum DsCaptureStatus {
    DS_CAPTURE_OK = 0, DS_CAPTURE_INVALID = 1, DS_CAPTURE_BUSY = 2,
    DS_CAPTURE_OWNER = 3, DS_CAPTURE_ABI_MISMATCH = 4, DS_CAPTURE_CHANGED = 5,
    DS_CAPTURE_LIMIT = 6, DS_CAPTURE_NO_MEMORY = 7
};
enum DsCaptureMethod {
    DS_METHOD_UNKNOWN = 0, DS_METHOD_GET = 1, DS_METHOD_QUERY_AND_GET = 2,
    DS_METHOD_CREATE = 3, DS_METHOD_PUBLISH = 4, DS_METHOD_MULTI_CREATE = 5,
    DS_METHOD_MULTI_PUBLISH = 6, DS_METHOD_EXIST = 7, DS_METHOD_DELETE_ALL_COPY = 8,
    DS_METHOD_GET_REMOTE = 9, DS_METHOD_BATCH_GET_REMOTE = 10
};
enum DsCaptureMode { DS_CAPTURE_DISABLED = 0, DS_CAPTURE_ENABLED = 1 };
enum DsCaptureSnapshotMode { DS_CAPTURE_PARTIAL = 0, DS_CAPTURE_NORMAL = 1 };
enum DsCaptureSampleFlag {
    DS_SAMPLE_SELECTED = 1u, DS_SAMPLE_RPC_FAILED = 2u, DS_SAMPLE_BAD_ENDPOINT = 4u,
    DS_SAMPLE_UNKNOWN_METHOD = 8u, DS_SAMPLE_DISABLED = 16u, DS_SAMPLE_CONTRACT_ERROR = 32u,
    DS_SAMPLE_SERVER_PROCESSING_VALID = 256u, DS_SAMPLE_SERVER_PROCESSING_INVALID = 512u,
    DS_SAMPLE_SERVER_EXECUTION_VALID = 1024u, DS_SAMPLE_SERVER_EXECUTION_INVALID = 2048u,
    DS_SAMPLE_RESIDUAL_INVALID = 4096u
};
enum DsCaptureError {
    DS_ERROR_OWNER = 1u, DS_ERROR_SCOPE = 2u, DS_ERROR_ENDPOINT = 4u,
    DS_ERROR_UNKNOWN_METHOD = 8u, DS_ERROR_RECORD_OVERFLOW = 16u,
    DS_ERROR_END_OVERFLOW = 32u, DS_ERROR_ORDINAL = 64u,
    DS_ERROR_MISSING_PRIMARY = 128u, DS_ERROR_API_OUTCOME = 256u,
    DS_ERROR_ABI = 512u, DS_ERROR_LIFECYCLE = 1024u
};

typedef struct DsCaptureSampleV2 {
    uint64_t arrival_id, send_ns, recv_ns, server_processing_ns, server_execution_ns;
    uint32_t phase_id, rpc_ordinal;
    uint16_t caller_index, method;
    int32_t controller_error, ds_error;
    uint32_t flags;
} DsCaptureSampleV2;

typedef struct DsCaptureApiSummaryV2 {
    uint64_t phase_id, arrival_id, scheduled_ns, api_begin_ns, api_end_ns, validation_end_ns;
    uint64_t observed_methods, capture_errors;
    uint32_t rpc_count, caller_index;
    int32_t outcome;
    uint32_t flags;
} DsCaptureApiSummaryV2;

typedef struct DsCaptureRunConfigV2 {
    uint32_t abi, bytes, callers, mode;
    uint32_t normal_records, error_records, normal_ends, error_ends;
    uint32_t max_rpc_per_api, sample_modulus;
    uint64_t sample_seed, memory_budget_bytes;
} DsCaptureRunConfigV2;

typedef struct DsCaptureRunV2 { uint32_t abi, bytes; void* handle; uint64_t generation; } DsCaptureRunV2;
typedef struct DsCaptureBuffersV2 {
    uint32_t abi, bytes;
    DsCaptureSampleV2* records;
    DsCaptureApiSummaryV2* ends;
    uint64_t record_capacity, end_capacity;
} DsCaptureBuffersV2;

typedef struct DsCaptureArmV2 { uint32_t abi, bytes, expected_callers, reserved; uint64_t generation; } DsCaptureArmV2;
typedef struct DsCaptureApiContextV2 {
    uint32_t abi, bytes;
    uint64_t generation, phase_id, arrival_id, scheduled_ns, api_begin_ns, required_methods;
} DsCaptureApiContextV2;
typedef struct DsCaptureApiEndV2 {
    uint32_t abi, bytes;
    uint64_t api_end_ns, validation_end_ns;
    int32_t outcome;
    uint32_t reserved;
} DsCaptureApiEndV2;

typedef struct DsCaptureSlotViewV2 {
    const DsCaptureSampleV2* records;
    const DsCaptureApiSummaryV2* ends;
    uint64_t record_count, end_count, errors, error_generation;
    uint64_t rpc_count, rpc_failures, api_count, api_failures, method_counts[DS_CAPTURE_METHODS];
    uint32_t registered, sealed, active, counters_available, cached_tid, reserved;
} DsCaptureSlotViewV2;
typedef struct DsCaptureSnapshotV2 {
    uint32_t abi, bytes, mode, callers, state, reserved;
    uint64_t generation, global_errors, global_error_generation;
    DsCaptureSlotViewV2 slots[DS_CAPTURE_MAX_CALLERS];
} DsCaptureSnapshotV2;
typedef struct DsCaptureSealV2 {
    uint32_t abi, bytes, sealed, valid_capture;
    uint64_t generation, global_errors, global_error_generation;
} DsCaptureSealV2;

typedef struct DsBenchApiV2 {
    uint32_t abi, bytes;
    int (*open)(const DsCaptureRunConfigV2*, DsCaptureRunV2*) DS_CAPTURE_NOEXCEPT;
    int (*register_caller)(const DsCaptureRunV2*, uint32_t, uint32_t, const DsCaptureBuffersV2*, void**) DS_CAPTURE_NOEXCEPT;
    int (*arm)(const DsCaptureRunV2*, const DsCaptureArmV2*) DS_CAPTURE_NOEXCEPT;
    int (*begin_api)(void*, const DsCaptureApiContextV2*) DS_CAPTURE_NOEXCEPT;
    int (*end_api)(void*, const DsCaptureApiEndV2*) DS_CAPTURE_NOEXCEPT;
    int (*snapshot)(const DsCaptureRunV2*, uint32_t, DsCaptureSnapshotV2*) DS_CAPTURE_NOEXCEPT;
    int (*disarm)(const DsCaptureRunV2*, const DsCaptureSnapshotV2*, DsCaptureSealV2*) DS_CAPTURE_NOEXCEPT;
    int (*unregister_caller)(void*) DS_CAPTURE_NOEXCEPT;
    int (*close)(const DsCaptureRunV2*) DS_CAPTURE_NOEXCEPT;
} DsBenchApiV2;

const DsBenchApiV2* DsBenchRpcCaptureV2(void) DS_CAPTURE_NOEXCEPT;

#ifdef __cplusplus
}
static_assert(sizeof(DsCaptureSampleV2) == 64, "sample ABI");
static_assert(offsetof(DsCaptureSampleV2, server_processing_ns) == 24, "server span ABI");
static_assert(offsetof(DsCaptureSampleV2, rpc_ordinal) == 44, "ordinal ABI");
static_assert(offsetof(DsCaptureSampleV2, method) == 50, "method ABI");
#endif
#undef DS_CAPTURE_NOEXCEPT
#endif
