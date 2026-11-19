# Fixed-owner unit fixtures

These five C++ groups test arrival-loop state, fixed-owner planning, the
non-divisible phase-end boundary, Session ownership/capacity, and immutable mode
capture. Their source files are copied without edits; `source-bindings.json`
records their exact relative-path hashes.

`provider/` is an isolated fake SDK capture provider and a public-header anchor.
It exists only for these unit executables. It does not implement KVClient
Get/Set, start a Worker, exercise a real SDK RPC path, or establish performance,
calibration, or RPC30 qualification. Never link this provider or anchor into a
production driver or runtime image.

Use `build_unit.py --help` for the explicit compiler, SDK-header, and fresh output
arguments. C++ compilation/execution requires Linux and C++20. Each executed
unit group has an eight-second deadline; `--build-only` reports tests as not run.
