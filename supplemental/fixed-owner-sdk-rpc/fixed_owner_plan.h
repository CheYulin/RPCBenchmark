#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>
namespace benchmark_capture {
struct FixedOwnerConfig {
    std::uint32_t callers = 32;
    std::uint64_t warm_calls_per_owner = 128;
    std::uint64_t measure_calls_per_owner = 1024;
    std::uint64_t rate_per_second = 500;
    std::uint64_t seed = 17;
    std::uint32_t sample_modulus = 1;
};
class FixedOwnerPlan {
public:
    static constexpr std::uint64_t BudgetBytes = UINT64_C(128) * 1024 * 1024;
    static constexpr std::uint64_t MetadataReserveBytes = 1024 * 1024;
    static constexpr std::uint64_t MaxTotalCalls = 2'000'000;
    static constexpr std::uint32_t MaxCallers = 128;
    static constexpr std::uint32_t MaxRpcPerApi = 16;
    static constexpr std::uint32_t ErrorRecords = 256;
    static constexpr std::uint32_t ErrorEnds = 256;
    explicit FixedOwnerPlan(FixedOwnerConfig config);
    ~FixedOwnerPlan() = default;
    FixedOwnerPlan(const FixedOwnerPlan &) = default;
    FixedOwnerPlan(FixedOwnerPlan &&) noexcept = default;
    FixedOwnerPlan &operator=(const FixedOwnerPlan &) = delete;
    FixedOwnerPlan &operator=(FixedOwnerPlan &&) = delete;
    std::uint32_t Callers() const noexcept;
    std::uint64_t Calls(std::uint64_t phase) const;
    std::uint64_t TotalCalls(std::uint64_t phase) const;
    std::uint64_t Rate() const noexcept;
    std::uint64_t Seed() const noexcept;
    std::uint32_t Modulus() const noexcept;
    std::uint64_t Id(std::uint64_t phase, std::size_t owner, std::uint64_t round) const;
    std::uint64_t ScheduledNs(std::uint64_t epoch, std::uint64_t phase, std::size_t owner,
                              std::uint64_t round) const;
    std::uint64_t EndNs(std::uint64_t epoch, std::uint64_t phase) const;
    bool Selected(std::uint64_t phase, std::uint64_t id) const;
    bool ValidateBegin(std::size_t owner, std::uint64_t phase, std::uint64_t id) const noexcept;
    std::uint64_t SelectedForOwner(std::size_t owner) const;
    std::uint64_t MaxSelectedPerOwner() const noexcept;
    std::uint64_t CaptureBytes() const noexcept;
private:
    FixedOwnerConfig config_;
    std::vector<std::uint64_t> selected_;
    std::uint64_t maximum_selected_ = 0;
    std::uint64_t capture_bytes_ = 0;
};
} // namespace benchmark_capture
