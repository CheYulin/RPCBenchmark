#include "capture_abi.h"
extern "C" __attribute__((visibility("default"))) const DsBenchApiV2* DsBenchRpcCaptureV2(void) noexcept
{
    static const DsBenchApiV2 wrong{1, sizeof(DsBenchApiV2), nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr};
    return &wrong;
}
