#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "capture_helper.h"
#include "datasystem/kv_client.h"

#include <atomic>
#include <charconv>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <dlfcn.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <link.h>
#include <stdexcept>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <vector>

namespace benchmark_capture {
namespace {
constexpr std::uint32_t MaxRpc = 16, ErrorRecords = 256, ErrorEnds = 256;
constexpr std::uint64_t Budget = UINT64_C(128) * 1024 * 1024, MaxPlan = 2'000'000;
constexpr std::uint64_t MetadataReserve = 1024 * 1024, CoreCapacity = 1024 * 1024;

struct SdkHandleCloser {
    void operator()(void* handle) const noexcept
    {
        if (handle) (void)dlclose(handle);
    }
};

std::uint64_t Setting(const char* name, std::uint64_t fallback, std::uint64_t minimum, std::uint64_t maximum)
{
    const char* value = std::getenv(name);
    if (!value) return fallback;
    std::uint64_t parsed = 0;
    const auto end = value + std::strlen(value);
    const auto result = std::from_chars(value, end, parsed);
    if (result.ec != std::errc{} || result.ptr != end || parsed < minimum || parsed > maximum)
        throw std::invalid_argument(name);
    return parsed;
}
void First(std::atomic<int>& destination, int status) noexcept
{
    if (status) { int empty = 0; destination.compare_exchange_strong(empty, status); }
}
int SdkImages(dl_phdr_info* image, std::size_t, void* count)
{
    const char* name = std::strrchr(image->dlpi_name, '/');
    name = name ? name + 1 : image->dlpi_name;
    constexpr auto length = sizeof("libdatasystem.so") - 1;
    if (std::strncmp(name, "libdatasystem.so", length) == 0 && (!name[length] || name[length] == '.'))
        ++*static_cast<unsigned*>(count);
    return 0;
}
} // namespace

struct Session::Impl {
    struct Slot {
        std::vector<DsCaptureSampleV2> records;
        std::vector<DsCaptureApiSummaryV2> ends;
        void* handle = nullptr;
        std::atomic<int> firstStatus{0};
        bool active = false;
    };
    std::filesystem::path output;
    const char* mode;
    std::size_t callers;
    std::unique_ptr<Slot[]> slots;
    std::unique_ptr<void, SdkHandleCloser> sdkHandle;
    const DsBenchApiV2* api = nullptr;
    void* sdkBase = nullptr;
    DsCaptureRunConfigV2 config{};
    DsCaptureRunV2 run{DS_CAPTURE_ABI, sizeof(DsCaptureRunV2), nullptr, 0};
    DsCaptureSnapshotV2 last{};
    DsCaptureSealV2 seal{DS_CAPTURE_ABI, sizeof(DsCaptureSealV2), 0, 0, 0, 0, 0};
    std::atomic<int> controlStatus{0};
    bool opened = false, armed = false, sealed = false, closed = false, started = false;

    Impl(const std::filesystem::path& directory, std::size_t count,
         std::uint64_t warm, std::uint64_t measure, bool priming, const char* selectedMode)
        : output(directory / "capture"), mode(selectedMode), callers(count)
    {
        if (!priming || !callers || callers > DS_CAPTURE_MAX_CALLERS || warm > MaxPlan || measure > MaxPlan - warm)
            throw std::invalid_argument("capture requires priming and bounded finite warm+measure plan");
        std::filesystem::create_directory(output);
        Resolve();
        const auto modulus = Setting("DS_BENCH_RPC_CAPTURE_MODULUS", 1024, 1, UINT32_MAX);
        const auto seed = Setting("DS_BENCH_RPC_CAPTURE_SEED", UINT64_C(0x4d595df4d0f33173), 0, UINT64_MAX);
        std::uint64_t selected = 0;
        for (const auto phase : {UINT64_C(1), UINT64_C(2)}) {
            const auto countApis = phase == 1 ? warm : measure;
            for (std::uint64_t id = 0; id < countApis; ++id)
                selected += Selected(seed, phase, id, static_cast<std::uint32_t>(modulus));
        }
        if (!selected) throw std::invalid_argument("capture finite plan selects no API IDs");
        const auto normalRecords = selected * MaxRpc;
        const auto recordCount = normalRecords + ErrorRecords, endCount = selected + ErrorEnds;
        const __int128 bytes = __int128(callers)
            * (recordCount * sizeof(DsCaptureSampleV2) + endCount * sizeof(DsCaptureApiSummaryV2));
        if (recordCount > CoreCapacity || endCount > CoreCapacity || bytes + MetadataReserve > Budget)
            throw std::invalid_argument("capture per-caller conservative capacity exceeds fixed128MiB budget");
        config = {DS_CAPTURE_ABI, sizeof(config), static_cast<std::uint32_t>(callers),
                  static_cast<std::uint32_t>(std::strcmp(mode, "enabled") == 0 ? DS_CAPTURE_ENABLED : DS_CAPTURE_DISABLED),
                  static_cast<std::uint32_t>(normalRecords), ErrorRecords,
                  static_cast<std::uint32_t>(selected), ErrorEnds, MaxRpc,
                  static_cast<std::uint32_t>(modulus), seed, Budget};
        slots = std::make_unique<Slot[]>(callers);
        for (std::size_t i = 0; i < callers; ++i) {
            slots[i].records.resize(recordCount);
            slots[i].ends.resize(endCount);
        }
        std::ofstream file(output / "config.json");
        file << "{\"mode\":\"" << mode << "\",\"abi\":2,\"warm_planned\":" << warm
             << ",\"measure_planned\":" << measure << ",\"callers\":" << callers
             << ",\"sample_seed\":" << seed << ",\"sample_modulus\":" << modulus
             << ",\"selected_ids_both_phases\":" << selected << ",\"max_rpc_per_api\":" << MaxRpc
             << ",\"normal_records_per_caller\":" << normalRecords << ",\"error_records_per_caller\":" << ErrorRecords
             << ",\"normal_ends_per_caller\":" << selected << ",\"error_ends_per_caller\":" << ErrorEnds
             << ",\"lent_array_bytes\":" << static_cast<std::uint64_t>(bytes)
             << ",\"memory_budget_bytes\":" << Budget << ",\"maximum_planned_apis\":" << MaxPlan
             << ",\"metadata_reserve_bytes\":" << MetadataReserve
             << ",\"sample_bytes\":" << sizeof(DsCaptureSampleV2) << ",\"end_bytes\":" << sizeof(DsCaptureApiSummaryV2)
             << ",\"phase_ids\":{\"warmup\":1,\"measure\":2},\"normal_prefixes_reset_between_phases\":false}\n";
        file.close();
        if (!file) throw std::runtime_error("capture config write failed");
        if (api) {
            const int status = api->open(&config, &run);
            if (status != DS_CAPTURE_OK) throw std::runtime_error("capture Open failed: " + std::to_string(status));
            opened = true;
        }
    }

    void SameImage(const void* pointer) const
    {
        Dl_info info{};
        if (!pointer || !dladdr(pointer, &info) || info.dli_fbase != sdkBase)
            throw std::runtime_error("capture getter/table/function is not in the actual SDK image");
    }

    void Resolve()
    {
        for (const char* name : {"LD_PRELOAD", "LD_AUDIT"})
            if (const char* value = std::getenv(name); value && *value)
                throw std::invalid_argument("capture rejects loader interposition");
        Dl_info anchor{};
        const auto function = &datasystem::KVClient::RegisterCudaFuncs;
        if (!dladdr(reinterpret_cast<const void*>(function), &anchor) || !anchor.dli_fname)
            throw std::runtime_error("cannot identify actual KVClient SDK");
        const char* expected = std::getenv("DS_BENCH_RPC_CAPTURE_SDK");
        if (!expected || std::filesystem::canonical(expected) != std::filesystem::canonical(anchor.dli_fname))
            throw std::runtime_error("KVClient SDK anchor does not match explicit expected DSO");
        unsigned images = 0;
        dl_iterate_phdr(SdkImages, &images);
        if (images != 1) throw std::runtime_error("capture requires one dynamic SDK image");
        sdkBase = anchor.dli_fbase;
        const auto path = std::filesystem::canonical(expected);
        if (path.string().find_first_of("\"\\\n\r") != std::string::npos)
            throw std::invalid_argument("SDK path is not suitable for fixed JSON identity proof");
        struct stat identity{};
        if (stat(path.c_str(), &identity) || !S_ISREG(identity.st_mode)) throw std::runtime_error("SDK stat failed");
        std::ifstream maps("/proc/self/maps");
        std::ofstream saved(output / "sdk-maps.txt");
        bool mapped = false, uniqueSdk = true;
        unsigned sdkRows = 0, zeroOffsetImages = 0;
        for (std::string line; std::getline(maps, line);) {
            saved << line << '\n';
            unsigned long long begin, end, offset, inode;
            unsigned majorValue, minorValue;
            char permissions[5];
            int pathBegin = 0;
            if (std::sscanf(line.c_str(), "%llx-%llx %4s %llx %x:%x %llu %n", &begin, &end, permissions,
                            &offset, &majorValue, &minorValue, &inode, &pathBegin) != 7)
                throw std::runtime_error("malformed actual process mapping");
            const bool wanted = inode == identity.st_ino && majorValue == major(identity.st_dev)
                && minorValue == minor(identity.st_dev);
            if (reinterpret_cast<std::uintptr_t>(function) >= begin
                && reinterpret_cast<std::uintptr_t>(function) < end) {
                mapped = wanted;
            }
            auto name = line.substr(static_cast<std::size_t>(pathBegin));
            constexpr const char* deleted = " (deleted)";
            const bool removed = name.size() >= std::strlen(deleted)
                && name.compare(name.size() - std::strlen(deleted), std::strlen(deleted), deleted) == 0;
            if (removed) name.resize(name.size() - std::strlen(deleted));
            name = std::filesystem::path(name).filename().string();
            if (wanted || name == "libdatasystem.so" || name.rfind("libdatasystem.so.", 0) == 0) {
                ++sdkRows;
                uniqueSdk &= wanted && !removed;
                if (offset == 0) {
                    ++zeroOffsetImages;
                    uniqueSdk &= begin == reinterpret_cast<std::uintptr_t>(sdkBase);
                }
            }
        }
        saved.close();
        if (!maps.eof() || !saved || !mapped || !uniqueSdk || !sdkRows || zeroOffsetImages != 1)
            throw std::runtime_error("actual SDK unique mapping inode/base proof failed");
        sdkHandle.reset(dlopen(path.c_str(), RTLD_NOW | RTLD_NOLOAD | RTLD_LOCAL));
        if (!sdkHandle) throw std::runtime_error("actual SDK is not already loaded");
        dlerror();
        auto* address = dlsym(sdkHandle.get(), "DsBenchRpcCaptureV2");
        const char* error = dlerror();
        if (std::strcmp(mode, "stock") == 0) {
            if (address || !error) throw std::runtime_error("stock mode requires SDK without capture getter");
        } else {
            if (!address || error) throw std::runtime_error("measurement SDK getter missing");
            SameImage(address);
            const auto getter = reinterpret_cast<const DsBenchApiV2* (*)() noexcept>(address);
            api = getter();
            SameImage(api);
            if (api->abi != DS_CAPTURE_ABI || api->bytes != sizeof(DsBenchApiV2))
                throw std::runtime_error("capture ABI2 table mismatch");
            SameImage(reinterpret_cast<const void*>(api->open));
            SameImage(reinterpret_cast<const void*>(api->register_caller));
            SameImage(reinterpret_cast<const void*>(api->arm));
            SameImage(reinterpret_cast<const void*>(api->begin_api));
            SameImage(reinterpret_cast<const void*>(api->end_api));
            SameImage(reinterpret_cast<const void*>(api->snapshot));
            SameImage(reinterpret_cast<const void*>(api->disarm));
            SameImage(reinterpret_cast<const void*>(api->unregister_caller));
            SameImage(reinterpret_cast<const void*>(api->close));
        }
        std::ofstream proof(output / "sdk-identity.json");
        proof << "{\"sdk_path\":\"" << path.string() << "\",\"inode\":" << identity.st_ino
              << ",\"device\":" << identity.st_dev << ",\"sdk_base\":\"" << sdkBase
              << "\",\"getter\":\"" << address << "\",\"same_image_function_table\":" << (api ? "true" : "false")
              << ",\"sdk_images\":" << images << ",\"SDK_mapping_rows\":" << sdkRows
              << ",\"SDK_zero_offset_images\":" << zeroOffsetImages
              << ",\"anchor_mapped_inode_match\":true,\"new_provider_dso\":false}\n";
        proof.close();
        if (!proof) throw std::runtime_error("SDK identity proof write failed");
    }

    void Dump(const char* label, const DsCaptureSnapshotV2& value)
    {
        const std::uint16_t endian = 1;
        const char* byteOrder = *reinterpret_cast<const unsigned char*>(&endian) ? "little" : "big";
        std::ofstream metadata(output / (std::string(label) + "-prefix.json"));
        metadata << "{\"abi\":2,\"snapshot_mode\":" << value.mode << ",\"state\":" << value.state
                 << ",\"generation\":" << value.generation << ",\"global_errors\":" << value.global_errors
                 << ",\"global_error_generation\":" << value.global_error_generation
                 << ",\"control_status\":" << controlStatus.load() << ",\"sample_bytes\":" << sizeof(DsCaptureSampleV2)
                 << ",\"end_bytes\":" << sizeof(DsCaptureApiSummaryV2)
                 << ",\"byte_order\":\"" << byteOrder << '"'
                 << ",\"encoding\":\"native-POD; frozenABI2; no pointer fields in rows\",\"slots\":[";
        for (std::size_t i = 0; i < callers; ++i) {
            const auto& view = value.slots[i];
            if (view.records != slots[i].records.data() || view.ends != slots[i].ends.data()
                || view.record_count > slots[i].records.size() || view.end_count > slots[i].ends.size())
                throw std::runtime_error("capture prefix exceeds lent capacity");
            if (i) metadata << ',';
            metadata << "{\"caller\":" << i << ",\"cached_tid\":" << view.cached_tid
                     << ",\"record_count\":" << view.record_count << ",\"end_count\":" << view.end_count
                     << ",\"errors\":" << view.errors << ",\"error_generation\":" << view.error_generation
                     << ",\"active\":" << view.active << ",\"first_driver_status\":" << slots[i].firstStatus.load()
                     << ",\"registered\":" << view.registered << ",\"sealed\":" << view.sealed
                     << ",\"counters_available\":" << (view.counters_available ? "true" : "false")
                     << ",\"rpc_count\":";
            if (view.counters_available) metadata << view.rpc_count; else metadata << "null";
            metadata << ",\"api_count\":";
            if (view.counters_available) metadata << view.api_count; else metadata << "null";
            metadata << ",\"rpc_failures\":";
            if (view.counters_available) metadata << view.rpc_failures; else metadata << "null";
            metadata << ",\"api_failures\":";
            if (view.counters_available) metadata << view.api_failures; else metadata << "null";
            metadata << ",\"method_counts\":";
            if (view.counters_available) {
                metadata << '[';
                for (std::size_t method = 0; method < DS_CAPTURE_METHODS; ++method) {
                    if (method) metadata << ',';
                    metadata << view.method_counts[method];
                }
                metadata << ']';
            } else metadata << "null";
            metadata << '}';
            const auto name = std::string(label) + "-caller-" + std::to_string(i);
            std::ofstream records(output / (name + ".rpc.bin"), std::ios::binary);
            std::ofstream ends(output / (name + ".ends.bin"), std::ios::binary);
            if (view.record_count) records.write(reinterpret_cast<const char*>(view.records),
                static_cast<std::streamsize>(view.record_count * sizeof(DsCaptureSampleV2)));
            if (view.end_count) ends.write(reinterpret_cast<const char*>(view.ends),
                static_cast<std::streamsize>(view.end_count * sizeof(DsCaptureApiSummaryV2)));
            records.close(); ends.close();
            if (!records || !ends) throw std::runtime_error("capture prefix write failed");
        }
        metadata << "]}\n"; metadata.close();
        if (!metadata) throw std::runtime_error("capture prefix metadata write failed");
    }

    void Partial(const char* label) noexcept
    {
        if (!api || !opened || closed) return;
        try {
            DsCaptureSnapshotV2 value{}; value.abi = DS_CAPTURE_ABI; value.bytes = sizeof(value);
            const int status = api->snapshot(&run, DS_CAPTURE_PARTIAL, &value);
            First(controlStatus, status);
            std::ofstream result(output / (std::string(label) + "-partial-status.json"));
            result << "{\"snapshot_rc\":" << status << ",\"owners_retained\":true,\"normal_seal\":false}\n";
            result.close();
            if (!result) throw std::runtime_error("capture partial status write failed");
            if (status == DS_CAPTURE_OK) Dump(label, value);
        } catch (...) { std::cerr << "capture partial output unavailable; run remains invalid\n" << std::flush; }
    }
    [[noreturn]] void RetainAndExit(const char* label) noexcept
    {
        Partial(label);
        std::cerr << "capture owners not safely closed; retained until process exit\n" << std::flush;
        std::_Exit(4);
    }
    void Snapshot(const char* label)
    {
        last = {}; last.abi = DS_CAPTURE_ABI; last.bytes = sizeof(last);
        const int status = api->snapshot(&run, DS_CAPTURE_NORMAL, &last);
        First(controlStatus, status);
        if (status != DS_CAPTURE_OK) RetainAndExit("snapshot-busy-partial");
        Dump(label, last);
    }
    void Seal()
    {
        for (unsigned attempt = 0; attempt != 2; ++attempt) {
            const int status = api->disarm(&run, &last, &seal);
            if (status == DS_CAPTURE_OK) { sealed = true; return; }
            First(controlStatus, status);
            if (attempt == 0 && (status == DS_CAPTURE_CHANGED || status == DS_CAPTURE_BUSY))
                Snapshot("invalid-fresh-token");
            else RetainAndExit("disarm-unresolved-partial");
        }
    }
    bool Good() const noexcept
    {
        if (!closed || !started || controlStatus.load()) return false;
        for (std::size_t i = 0; i < callers; ++i) if (slots[i].firstStatus.load()) return false;
        if (!api) return true;
        if (!sealed || last.global_errors) return false;
        for (std::size_t i = 0; i < callers; ++i)
            if (last.slots[i].errors || last.slots[i].rpc_failures) return false;
        return std::strcmp(mode, "enabled") != 0 || seal.valid_capture;
    }
    ~Impl()
    {
        if (opened && !closed) {
            const int status = api->close(&run);
            if (status != DS_CAPTURE_OK) RetainAndExit("destructor-unresolved-partial");
        }
    }
};

Session::Session(const std::filesystem::path& output, std::size_t callers,
                 std::uint64_t warm, std::uint64_t measure, bool priming)
{
    const char* mode = std::getenv("DS_BENCH_RPC_CAPTURE_MODE");
    if (!mode || std::strcmp(mode, "off") == 0) return;
    const char* selected = nullptr;
    for (const char* value : {"stock", "disabled", "enabled"})
        if (std::strcmp(mode, value) == 0) selected = value;
    if (!selected) throw std::invalid_argument("DS_BENCH_RPC_CAPTURE_MODE must be off|stock|disabled|enabled");
    impl_ = std::make_unique<Impl>(output, callers, warm, measure, priming, selected);
}
Session::~Session() = default;
bool Session::Active() const noexcept { return impl_ != nullptr; }
const char* Session::Mode() const noexcept { return impl_ ? impl_->mode : "off"; }
bool Session::Register(std::size_t caller, std::uint32_t tid) noexcept
{
    if (!impl_ || !impl_->api) return true;
    auto& slot = impl_->slots[caller];
    DsCaptureBuffersV2 buffers{DS_CAPTURE_ABI, sizeof(buffers), slot.records.data(), slot.ends.data(),
                              slot.records.size(), slot.ends.size()};
    const int status = impl_->api->register_caller(&impl_->run, static_cast<std::uint32_t>(caller), tid, &buffers, &slot.handle);
    First(slot.firstStatus, status);
    return status == DS_CAPTURE_OK;
}
bool Session::Arm() noexcept
{
    if (!impl_) return true;
    impl_->started = true;
    if (!impl_->api) return true;
    DsCaptureArmV2 arm{DS_CAPTURE_ABI, sizeof(arm), static_cast<std::uint32_t>(impl_->callers), 0, impl_->run.generation};
    const int status = impl_->api->arm(&impl_->run, &arm);
    First(impl_->controlStatus, status);
    impl_->armed = status == DS_CAPTURE_OK;
    return impl_->armed;
}
void Session::Begin(std::size_t caller, std::uint64_t phase, std::uint64_t arrival,
                    std::uint64_t scheduled, std::uint64_t begin, std::uint64_t methods) noexcept
{
    if (!impl_ || !impl_->api) return;
    auto& slot = impl_->slots[caller];
    DsCaptureApiContextV2 context{DS_CAPTURE_ABI, sizeof(context), impl_->run.generation,
                                  phase, arrival, scheduled, begin, methods};
    const int status = impl_->api->begin_api(slot.handle, &context);
    First(slot.firstStatus, status);
    slot.active = status == DS_CAPTURE_OK;
}
void Session::End(std::size_t caller, std::uint64_t end, std::uint64_t validated, std::int32_t outcome) noexcept
{
    if (!impl_ || !impl_->api) return;
    auto& slot = impl_->slots[caller];
    if (!slot.active) return;
    DsCaptureApiEndV2 result{DS_CAPTURE_ABI, sizeof(result), end, validated, outcome, 0};
    const int status = impl_->api->end_api(slot.handle, &result);
    First(slot.firstStatus, status);
    if (status == DS_CAPTURE_OK) slot.active = false;
}
void Session::Drained(const char* phase, bool final)
{
    if (!impl_ || !impl_->api) return;
    impl_->Snapshot(phase);
    if (final) impl_->Seal();
}
void Session::BeforeStop(bool quiescent)
{
    if (!impl_ || !impl_->api || !impl_->armed || impl_->sealed) return;
    First(impl_->controlStatus, DS_CAPTURE_INVALID);
    if (!quiescent) impl_->RetainAndExit("unsafe-stop-partial");
    impl_->Snapshot("aborted-quiescent"); impl_->Seal();
}
void Session::Unregister(std::size_t caller) noexcept
{
    if (!impl_ || !impl_->api || !impl_->slots[caller].handle) return;
    const int status = impl_->api->unregister_caller(impl_->slots[caller].handle);
    First(impl_->slots[caller].firstStatus, status);
    if (status == DS_CAPTURE_OK) impl_->slots[caller].handle = nullptr;
}
void Session::Partial(const char* phase) noexcept { if (impl_) impl_->Partial(phase); }
bool Session::Close()
{
    if (!impl_) return true;
    if (impl_->api) {
        const int status = impl_->api->close(&impl_->run);
        First(impl_->controlStatus, status);
        if (status != DS_CAPTURE_OK) impl_->RetainAndExit("close-unresolved-partial");
    }
    impl_->closed = true;
    std::ofstream output(impl_->output / "final.json");
    output << "{\"mode\":\"" << impl_->mode << "\",\"control_status\":" << impl_->controlStatus.load()
           << ",\"resource_sealed\":" << (impl_->sealed ? "true" : "false")
           << ",\"core_valid_capture\":" << (impl_->seal.valid_capture ? "true" : "false")
           << ",\"capture_or_control_ok\":" << (impl_->Good() ? "true" : "false")
           << ",\"rpc30_budget_verified\":false,\"close_ok\":true}\n";
    output.close();
    if (!output) throw std::runtime_error("capture final receipt write failed");
    return impl_->Good();
}
bool Session::Good() const noexcept { return !impl_ || impl_->Good(); }
bool Session::RpcAvailable() const noexcept
{
    if (!impl_ || !impl_->api || std::strcmp(impl_->mode, "enabled") || impl_->controlStatus.load()
        || impl_->last.global_errors) return false;
    std::uint64_t records = 0;
    for (std::size_t i = 0; i < impl_->callers; ++i) {
        if (impl_->slots[i].firstStatus.load() || impl_->last.slots[i].errors
            || impl_->last.slots[i].rpc_failures) return false;
        records += impl_->last.slots[i].record_count;
    }
    return records != 0;
}
} // namespace benchmark_capture
