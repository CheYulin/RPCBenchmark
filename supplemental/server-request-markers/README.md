# Experimental server request markers

This source checkpoint preserves a private, default-off instrumentation experiment. It is not an optimization, a production integration, a runnable benchmark package, or evidence that either RPC backend meets a performance target. Production defaults remain unchanged.

`coro_unary.cpp` is the candidate translation unit derived from DataSystem source `23bb005761a9012c994a532f04fc2c7c60b446737b003c86ce63999936437566`. It includes `private_request_marker.h` only when the compile-time `DS_PRIVATE_CORO_REQUEST_MARKERS` macro is `1`; its default is `0`. The enabled translation unit requires GNU C++ 15 or later. There is no runtime option or operator flag.

The enabled path assigns a bounded, process-local request number after admission, before attempting background submission. It passes that value into the fresh business task and emits three calls to `DSPrivateCoroServerRequestPhase`: submission attempt, lambda entry, and reply ready before `Respond`. A rejected submission can therefore leave a submission-only prefix. Reply ready does not mean the response has been sent. The counter saturates at 65535; subsequent zero values are invalid for joining and do not change the business reply. Numbers can be observed out of order across native threads and bthreads.

The request number joins server-side phases within one process lifetime. It does not identify an SDK call, change the request wire format, change the capture ABI, or establish complete coverage of all planned calls. Missing, repeated, invalid, cancelled, or partially observed phases must remain incomplete or unknown. These files contain no captured requests, process identifiers, event streams, runtime maps, build artifacts, or private deployment configuration.

## Standard-only unit seam

`invoke_admitted_seam.inc` is an exact extraction of the candidate admitted-dispatch block. `test_private_request_marker.cpp` compiles that block and the actual counter helper with small task, reply, and observation seams. It does not compile the complete RPC translation unit or run YLT, an SDK, a Worker, or a BPF probe. The test marker has an observable implementation for assertions; it is not the production marker implementation.

From this directory, with a C++20 compiler and threads:

```sh
c++ -std=c++20 -O2 -pthread -DDS_PRIVATE_CORO_REQUEST_MARKERS=1 test_private_request_marker.cpp -o marker-unit-enabled
./marker-unit-enabled
c++ -std=c++20 -O2 -pthread -DDS_PRIVATE_CORO_REQUEST_MARKERS=0 test_private_request_marker.cpp -o marker-unit-off
./marker-unit-off
```

An actual GNU C++ 15 standard-only run passed seven enabled cases and one default-off business-seam case. The default-off object had neither the marker nor the diagnostic counter symbol. Two semantic mutations, changing finite-counter bounds and replacing captured request identity with the latest global value, each compiled and failed assertions. That evidence validates the unit contract only. It does not establish selected Worker execution, live argument capture, BPF verification, performance improvement, or CPU and tail-latency acceptance.

The experiment preserves existing business-task ownership and completion behavior. Enabling it adds a relaxed atomic allocation and three diagnostic call sites on admitted requests. Any production or performance use requires separate source, build, selected-code, lifetime, and workload review. No such acceptance is asserted by this checkpoint.
