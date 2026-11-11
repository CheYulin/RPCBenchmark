#include "capture_helper.h"
#include "capture_observe.h"
#include <array>
#include <cstdio>

#define CHECK(value) do { if (!(value)) { std::fprintf(stderr, "selection contract failed line%d\n", __LINE__); return 1; } } while (false)
int main()
{
    struct Case { std::uint64_t seed, phase, arrival; std::uint32_t modulus; bool selected; };
    constexpr Case cases[]{
    {UINT64_C(0), UINT64_C(1), UINT64_C(0), 7, true},
    {UINT64_C(0), UINT64_C(1), UINT64_C(1), 7, false},
    {UINT64_C(0), UINT64_C(1), UINT64_C(7), 7, false},
    {UINT64_C(0), UINT64_C(1), UINT64_C(31), 3, false},
    {UINT64_C(0), UINT64_C(2), UINT64_C(0), 7, false},
    {UINT64_C(0), UINT64_C(2), UINT64_C(1), 7, false},
    {UINT64_C(0), UINT64_C(2), UINT64_C(7), 7, false},
    {UINT64_C(0), UINT64_C(2), UINT64_C(31), 3, false},
    {UINT64_C(17), UINT64_C(1), UINT64_C(0), 7, false},
    {UINT64_C(17), UINT64_C(1), UINT64_C(1), 7, false},
    {UINT64_C(17), UINT64_C(1), UINT64_C(7), 7, false},
    {UINT64_C(17), UINT64_C(1), UINT64_C(31), 3, false},
    {UINT64_C(17), UINT64_C(2), UINT64_C(0), 7, true},
    {UINT64_C(17), UINT64_C(2), UINT64_C(1), 7, false},
    {UINT64_C(17), UINT64_C(2), UINT64_C(7), 7, false},
    {UINT64_C(17), UINT64_C(2), UINT64_C(31), 3, true},
    {UINT64_C(5573589319906701683), UINT64_C(1), UINT64_C(0), 7, false},
    {UINT64_C(5573589319906701683), UINT64_C(1), UINT64_C(1), 7, false},
    {UINT64_C(5573589319906701683), UINT64_C(1), UINT64_C(7), 7, false},
    {UINT64_C(5573589319906701683), UINT64_C(1), UINT64_C(31), 3, false},
    {UINT64_C(5573589319906701683), UINT64_C(2), UINT64_C(0), 7, true},
    {UINT64_C(5573589319906701683), UINT64_C(2), UINT64_C(1), 7, false},
    {UINT64_C(5573589319906701683), UINT64_C(2), UINT64_C(7), 7, false},
    {UINT64_C(5573589319906701683), UINT64_C(2), UINT64_C(31), 3, false},
    {UINT64_C(18446744073709551615), UINT64_C(1), UINT64_C(0), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(1), UINT64_C(1), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(1), UINT64_C(7), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(1), UINT64_C(31), 3, true},
    {UINT64_C(18446744073709551615), UINT64_C(2), UINT64_C(0), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(2), UINT64_C(1), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(2), UINT64_C(7), 7, false},
    {UINT64_C(18446744073709551615), UINT64_C(2), UINT64_C(31), 3, true},
    {UINT64_C(17), UINT64_C(1), UINT64_C(0), 1, true},
    {UINT64_C(0), UINT64_C(2), UINT64_C(18446744073709551615), 17, false},
    };
    const auto* api = DsBenchRpcCaptureV2();
    CHECK(api && api->abi == DS_CAPTURE_ABI && api->bytes == sizeof(DsBenchApiV2));
    for (const auto& item : cases) {
        std::array<DsCaptureSampleV2, 8> records{};
        std::array<DsCaptureApiSummaryV2, 4> ends{};
        DsCaptureRunConfigV2 config{DS_CAPTURE_ABI, sizeof(config), 1, DS_CAPTURE_ENABLED,
            4, 4, 2, 2, 2, item.modulus, item.seed, 1024 * 1024};
        DsCaptureRunV2 run{DS_CAPTURE_ABI, sizeof(run), nullptr, 0};
        CHECK(api->open(&config, &run) == DS_CAPTURE_OK);
        void* slot = nullptr;
        DsCaptureBuffersV2 buffers{DS_CAPTURE_ABI, sizeof(buffers), records.data(), ends.data(), records.size(), ends.size()};
        CHECK(api->register_caller(&run, 0, 1, &buffers, &slot) == DS_CAPTURE_OK);
        DsCaptureArmV2 arm{DS_CAPTURE_ABI, sizeof(arm), 1, 0, run.generation};
        CHECK(api->arm(&run, &arm) == DS_CAPTURE_OK);
        DsCaptureApiContextV2 context{DS_CAPTURE_ABI, sizeof(context), run.generation,
            item.phase, item.arrival, 100, 100, UINT64_C(1) << DS_METHOD_GET};
        CHECK(api->begin_api(slot, &context) == DS_CAPTURE_OK);
        ds_bench_capture::Observe({110, 120, DS_METHOD_GET, false, 0, 0});
        DsCaptureApiEndV2 end{DS_CAPTURE_ABI, sizeof(end), 130, 130, 0, 0};
        CHECK(api->end_api(slot, &end) == DS_CAPTURE_OK);
        DsCaptureSnapshotV2 snapshot{}; snapshot.abi = DS_CAPTURE_ABI; snapshot.bytes = sizeof(snapshot);
        CHECK(api->snapshot(&run, DS_CAPTURE_NORMAL, &snapshot) == DS_CAPTURE_OK);
        const auto& view = snapshot.slots[0];
        CHECK(view.api_count == 1 && view.rpc_count == 1 && view.method_counts[DS_METHOD_GET] == 1);
        CHECK(view.record_count == (item.selected ? 1u : 0u));
        CHECK(view.end_count == (item.selected ? 1u : 0u));
        if (item.selected) CHECK((records[0].flags & DS_SAMPLE_SELECTED) && (ends[0].flags & DS_SAMPLE_SELECTED));
        DsCaptureSealV2 seal{DS_CAPTURE_ABI, sizeof(seal), 0, 0, 0, 0, 0};
        CHECK(api->disarm(&run, &snapshot, &seal) == DS_CAPTURE_OK && seal.sealed && seal.valid_capture);
        CHECK(api->unregister_caller(slot) == DS_CAPTURE_OK);
        CHECK(api->close(&run) == DS_CAPTURE_OK);
#ifdef DS_TEST_BAD_DRIVER_SELECTION
        const bool driver = item.arrival % item.modulus == 0;
#else
        const bool driver = benchmark_capture::Selected(item.seed, item.phase, item.arrival, item.modulus);
#endif
        CHECK(driver == item.selected);
    }
    std::puts("selection core/driver/golden contracts passed");
    return 0;
}
