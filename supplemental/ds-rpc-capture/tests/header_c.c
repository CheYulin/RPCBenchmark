#include "capture_abi.h"
_Static_assert(sizeof(DsCaptureSampleV2) == 64, "sample layout");
_Static_assert(offsetof(DsCaptureSampleV2, method) == 50, "method layout");
int main(void) { return DS_CAPTURE_ABI != 2; }
