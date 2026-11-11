#ifndef PRIVATE_PACED_SDK_CAPTURE_HELPER_H
#define PRIVATE_PACED_SDK_CAPTURE_HELPER_H

#include "capture_abi.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>

namespace benchmark_capture {
constexpr std::uint64_t Mix(std::uint64_t value)
{
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}
constexpr bool Selected(std::uint64_t seed, std::uint64_t phase, std::uint64_t id, std::uint32_t modulus)
{
    return Mix(seed ^ Mix(phase) ^ Mix(id + UINT64_C(0x9e3779b97f4a7c15))) % modulus == 0;
}
constexpr std::uint64_t ReadMethods = UINT64_C(1) << DS_METHOD_GET;
constexpr std::uint64_t WriteMethods = UINT64_C(1) << DS_METHOD_PUBLISH;

class Session {
public:
    Session(const std::filesystem::path& output, std::size_t callers,
            std::uint64_t warmApis, std::uint64_t measureApis, bool primingEnabled);
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    bool Active() const noexcept;
    const char* Mode() const noexcept;
    bool Register(std::size_t caller, std::uint32_t tid) noexcept;
    bool Arm() noexcept;
    void Begin(std::size_t caller, std::uint64_t phase, std::uint64_t arrival,
               std::uint64_t scheduled, std::uint64_t begin, std::uint64_t requiredMethods) noexcept;
    void End(std::size_t caller, std::uint64_t end, std::uint64_t validated, std::int32_t outcome) noexcept;
    void Drained(const char* phase, bool final);
    void BeforeStop(bool quiescent);
    void Unregister(std::size_t caller) noexcept;
    void Partial(const char* label) noexcept;
    bool Close();
    bool Good() const noexcept;
    bool RpcAvailable() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace benchmark_capture
#endif
