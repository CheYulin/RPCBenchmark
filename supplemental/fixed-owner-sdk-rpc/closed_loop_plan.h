#pragma once
#include <array>
#include <bit>
#include <cstdint>
#include <limits>
#include <stdexcept>
namespace closed_loop {
inline constexpr std::uint32_t Callers = 32, SampleModulus = 512;
#ifdef DS_CLOSED_LOOP_SMOKE
inline constexpr std::uint64_t WarmCalls = 8, MeasureCalls = 16;
#else
inline constexpr std::uint64_t WarmCalls = 8192, MeasureCalls = 49152;
#endif
inline constexpr std::uint64_t Seed = 17;
inline constexpr std::uint64_t CaptureBudget = 128ULL*1024*1024, MetadataReserve = 1024*1024;
inline constexpr std::uint64_t MaxRpcPerApi = 16, ErrorReserve = 256, SampleBytes = 64, EndBytes = 80;
constexpr std::uint64_t Mix(std::uint64_t value) {
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}
constexpr bool Selected(std::uint32_t phase, std::uint64_t id) {
    return Mix(Seed ^ Mix(phase) ^ Mix(id + UINT64_C(0x9e3779b97f4a7c15))) % SampleModulus == 0;
}
inline std::uint64_t PhaseCalls(std::uint32_t phase) {
    if (phase != 1 && phase != 2) throw std::out_of_range("phase");
    return phase == 1 ? WarmCalls : MeasureCalls;
}
inline std::uint64_t ArrivalId(std::uint32_t phase, std::uint32_t caller, std::uint64_t ordinal) {
    if (caller >= Callers || ordinal >= PhaseCalls(phase)) throw std::out_of_range("arrival ID");
    return ordinal * Callers + caller;
}
inline std::uint64_t CaptureBytes(std::uint64_t selected) {
    const __uint128_t bytes=__uint128_t(Callers) * ((__uint128_t(selected)*MaxRpcPerApi+ErrorReserve)*SampleBytes
                                                    +(__uint128_t(selected)+ErrorReserve)*EndBytes);
    if (bytes > std::numeric_limits<std::uint64_t>::max()) throw std::overflow_error("capture bytes");
    return static_cast<std::uint64_t>(bytes);
}
class Histogram {
public:
    static constexpr std::size_t FineBuckets=8192, LogBuckets=42, Buckets=FineBuckets+LogBuckets;
    Histogram() = default;
    ~Histogram() = default;
    void Record(std::uint64_t ns) {
        const auto us=ns/1000+(ns%1000!=0);
        const auto value=us ? us : 1;
        const std::size_t index=value<=FineBuckets ? value-1 : FineBuckets+std::bit_width(value-1)-14;
        if (index>=Buckets) throw std::overflow_error("histogram range");
        ++bins_[index];++count_;
    }
    void Merge(const Histogram &other) {
        for(std::size_t i=0;i<Buckets;++i) bins_[i]+=other.bins_[i];
        count_+=other.count_;
    }
    static std::uint64_t BucketUpper(std::size_t index) {
        if(index>=Buckets) throw std::out_of_range("histogram bucket");
        return index<FineBuckets ? index+1 : UINT64_C(1)<<(14+index-FineBuckets);
    }
    std::uint64_t Count() const { return count_; }
    std::uint64_t Upper(unsigned percentile) const {
        if(!percentile || percentile>100) throw std::out_of_range("percentile");
        const auto rank=(count_/100)*percentile+((count_%100)*percentile+99)/100;
        std::uint64_t seen=0;
        for(std::size_t i=0;i<Buckets && count_;++i) { seen+=bins_[i];if(seen>=rank)return BucketUpper(i); }
        return 0;
    }
    const auto &Bins() const { return bins_; }
private:
    std::array<std::uint64_t,Buckets> bins_{};
    std::uint64_t count_=0;
};
}
