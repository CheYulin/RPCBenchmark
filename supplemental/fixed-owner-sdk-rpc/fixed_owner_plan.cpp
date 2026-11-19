#include "fixed_owner_plan.h"
#include "capture_abi.h"
#include "capture_helper.h"
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace benchmark_capture {
namespace {
constexpr std::uint64_t NanosPerSecond = 1'000'000'000;
std::uint64_t TimeAt(std::uint64_t epoch, std::uint64_t id, std::uint64_t rate)
{
    const auto value = __uint128_t(epoch) + __uint128_t(id) * NanosPerSecond / rate;
    if (value > std::numeric_limits<std::uint64_t>::max()) throw std::overflow_error("fixed-owner schedule overflow");
    return static_cast<std::uint64_t>(value);
}
}
FixedOwnerPlan::FixedOwnerPlan(FixedOwnerConfig config) : config_(config)
{
    if (!config.callers || config.callers > MaxCallers || !config.warm_calls_per_owner
        || !config.measure_calls_per_owner || !config.rate_per_second || config.rate_per_second > NanosPerSecond
        || !config.sample_modulus)
        throw std::invalid_argument("invalid fixed-owner plan");
    const auto total = (__uint128_t(config.warm_calls_per_owner) + config.measure_calls_per_owner) * config.callers;
    if (total > MaxTotalCalls) throw std::invalid_argument("fixed-owner finite call bound exceeded");
    selected_.resize(config.callers);
    for (std::uint64_t phase = 1; phase <= 2; ++phase)
        for (std::size_t owner = 0; owner < config.callers; ++owner)
            for (std::uint64_t round = 0; round < Calls(phase); ++round)
                selected_[owner] += Selected(phase, Id(phase, owner, round));
    maximum_selected_ = *std::max_element(selected_.begin(), selected_.end());
    const auto records = __uint128_t(maximum_selected_) * MaxRpcPerApi + ErrorRecords;
    const auto ends = __uint128_t(maximum_selected_) + ErrorEnds;
    const auto bytes = __uint128_t(config.callers) * (records * sizeof(DsCaptureSampleV2) + ends * sizeof(DsCaptureApiSummaryV2));
    if (records > 1024 * 1024 || ends > 1024 * 1024 || bytes + MetadataReserveBytes > BudgetBytes)
        throw std::invalid_argument("fixed-owner capture budget exceeded");
    capture_bytes_ = static_cast<std::uint64_t>(bytes);
}
std::uint32_t FixedOwnerPlan::Callers() const noexcept { return config_.callers; }
std::uint64_t FixedOwnerPlan::Calls(std::uint64_t phase) const
{
    if (phase != 1 && phase != 2) throw std::out_of_range("fixed-owner phase");
    return phase == 1 ? config_.warm_calls_per_owner : config_.measure_calls_per_owner;
}
std::uint64_t FixedOwnerPlan::TotalCalls(std::uint64_t phase) const { return Calls(phase) * Callers(); }
std::uint64_t FixedOwnerPlan::Rate() const noexcept { return config_.rate_per_second; }
std::uint64_t FixedOwnerPlan::Seed() const noexcept { return config_.seed; }
std::uint32_t FixedOwnerPlan::Modulus() const noexcept { return config_.sample_modulus; }
std::uint64_t FixedOwnerPlan::Id(std::uint64_t phase, std::size_t owner, std::uint64_t round) const
{
    if (owner >= Callers() || round >= Calls(phase)) throw std::out_of_range("fixed-owner owner/round");
    return round * Callers() + owner;
}
std::uint64_t FixedOwnerPlan::ScheduledNs(std::uint64_t epoch, std::uint64_t phase, std::size_t owner,
                                         std::uint64_t round) const
{
    return TimeAt(epoch, Id(phase, owner, round), Rate());
}
std::uint64_t FixedOwnerPlan::EndNs(std::uint64_t epoch, std::uint64_t phase) const
{
    const auto numerator = __uint128_t(TotalCalls(phase)) * NanosPerSecond;
    const auto value = __uint128_t(epoch) + (numerator + Rate() - 1) / Rate();
    if (value > std::numeric_limits<std::uint64_t>::max()) throw std::overflow_error("fixed-owner phase end overflow");
    return static_cast<std::uint64_t>(value);
}
bool FixedOwnerPlan::ValidateBegin(std::size_t owner, std::uint64_t phase, std::uint64_t id) const noexcept
{
    if (owner >= Callers() || (phase != 1 && phase != 2)) return false;
    const auto calls = phase == 1 ? config_.warm_calls_per_owner : config_.measure_calls_per_owner;
    return id < calls * Callers() && id % Callers() == owner;
}
bool FixedOwnerPlan::Selected(std::uint64_t phase, std::uint64_t id) const
{
    if ((phase != 1 && phase != 2) || id >= TotalCalls(phase)) throw std::out_of_range("fixed-owner selected ID");
    return benchmark_capture::Selected(Seed(), phase, id, Modulus());
}
std::uint64_t FixedOwnerPlan::SelectedForOwner(std::size_t owner) const
{
    if (owner >= Callers()) throw std::out_of_range("fixed-owner selected owner");
    return selected_[owner];
}
std::uint64_t FixedOwnerPlan::MaxSelectedPerOwner() const noexcept { return maximum_selected_; }
std::uint64_t FixedOwnerPlan::CaptureBytes() const noexcept { return capture_bytes_; }
}
