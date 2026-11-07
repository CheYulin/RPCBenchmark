# bRPC / coro_rpc 独立验证仓

两个二进制各自启动真实 RPC Server 和 Client，用同一个二进制 Echo 负载观测 Linux TCP 回环上的开销。运行时不依赖 YuanRong DataSystem 或 Mooncake 的服务进程。

## 源码锁定

| 组件 | 来源与版本 | 实验边界 |
| --- | --- | --- |
| Apache bRPC | [1.15.0](https://github.com/apache/brpc/tree/50a9075de62f1d2b825b902acbfecfddb3d9f314)，Apache 发布包 SHA256 `0bc8c2aee810c96e6c77886f828fbfdf32ae353ce997eb46f2772c0088010c35`，对应 DataSystem 的依赖版本 | 原版 bRPC 的 `baidu_std` + protobuf。CMake 路径使用构建主机的 protobuf、gflags、LevelDB、OpenSSL 与 zlib。DataSystem 的兼容补丁、生成 stub、认证和业务处理不在本仓 |
| yaLanTingLibs | [45c6463](https://github.com/alibaba/yalantinglibs/tree/45c6463c0afbcc46262e373effa30635be3846a0)，[Mooncake v0.3.14 的版本锁](https://github.com/kvcache-ai/Mooncake/blob/e1ea5d31bb5e46ff2df9f90e7c2fbaf52fcdee8c/mooncake-common/FindYLT.cmake)，GitHub 源码包 SHA256 `baa6b5c9ffcc0af8252742bba8f0fb9c6d37387c13b2ff5b26d29c6aaff6ee37` | coro_rpc + struct_pack，使用源码包自带 Asio 与 async_simple；可选应用 handler 线程池复现 `coro_io::post` 调度边界 |

## 构建和验收

### CMake

依赖 Linux、支持 C++20 的编译器、CMake 3.20+、Ninja、Python 3 和 bRPC 的原生依赖。以 Debian/Ubuntu 为例，所需开发包为 `libprotobuf-dev protobuf-compiler libprotoc-dev libgflags-dev libleveldb-dev libssl-dev zlib1g-dev`。以 `CXX=g++-13` 为例选择编译器。

```bash
CXX=g++-13 cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 6
ctest --test-dir build --output-on-failure
```

无网络构建可通过以下参数指定下载好的源码包，CMake 校验 SHA256。

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DRPC_LAB_BRPC_ARCHIVE=/path/apache-brpc-1.15.0-src.tar.gz \
  -DRPC_LAB_YLT_ARCHIVE=/path/ylt-45c6463.tar.gz
```

如已安装同一发布包编译出的共享 `libbrpc.so`，可通过 `-DRPC_LAB_BRPC_ROOT=/path/prefix` 复用。确保 lab 与 bRPC 使用同一 protobuf ABI，并让运行时能找到该前缀所需的动态库。`-DRPC_LAB_BUILD_BRPC=OFF` 或 `-DRPC_LAB_BUILD_CORO=OFF` 可分别跳过一个框架。运行测试需要可用的 IPv4 本地回环网络。

### Bazel

使用 Linux、支持 C++20 的编译器、Perl、Python 3 和 Bazel 7.4.1。若使用 Bazelisk，它会按 `.bazelversion` 选择 Bazel 版本。

```bash
CC=gcc-13 CXX=g++-13 bazel build //:rpc_brpc //:rpc_coro
CC=gcc-13 CXX=g++-13 bazel test //:rpc_brpc_smoke //:rpc_coro_smoke //:rpc_coro_pooled_smoke
```

产物为 `bazel-bin/rpc_brpc` 和 `bazel-bin/rpc_coro`。回环和矩阵脚本使用相同的参数，可把矩阵命令中的 `build` 换成 `bazel-bin`。

```bash
./bazel-bin/rpc_coro server --port 18081 --workers 4
```

Bazel 直接构建 bRPC 发布包内的 `@brpc//:brpc`，使用其 Bzlmod 依赖和锁定的 YLT 头文件，两个框架的源码包哈希与 CMake 路径一致。Bazel 依赖图中的 protobuf 和 OpenSSL 版本由模块锁定，CMake 版本由主机安装包决定。首次构建会下载依赖并编译相关库。重复构建复用 Bazel 的增量缓存，只验证 coro_rpc 时可单独构建 `//:rpc_coro`。

## 运行

在两个终端运行 Server 和 Client。更换框架时，两个命令使用同一个框架的二进制。

```bash
./build/rpc_brpc server --port 18081 --workers 4
```

```bash
./build/rpc_brpc client --port 18081 --workers 4 --concurrency 16 \
  --payload 64 --warmup 1 --seconds 3 --timeout-ms 1000
```

两端都绑定或连接 IPv4 `127.0.0.1`。Server 打印 `READY` 后接受连接，收到 SIGTERM 或 SIGINT 后停止。Client 打印 `WARMUP_DONE` 至 stderr，退出前输出一行 JSON。退出码 `0` 表示所有参与 lane 有有效样本且无 RPC 错误和数据校验错误，`1` 表示配置、启动或预热失败，`2` 表示已输出结果但测量阶段出现错误或空 lane。

- `--workers` 在 bRPC 中设置进程 bthread 运行池的并发度，取值至少 4，在 coro_rpc 中设置 Asio I/O 线程数。两框架辅助线程数量与职责由框架管理。可对 Server 和 Client 分别限制 CPU 亲和性。
- `--concurrency` 是独立长连接数。每条连接每次只保留一条未完成的 RPC。bRPC 为每个 Channel 设置独立 `connection_group`，通过 bthread 同步等待，关闭 RPC 重试。coro_rpc 每条连接持有一个客户端和一个所属 I/O executor，通过协程等待。
- `--payload` 是应用层请求和响应各自的有效字节数，包含 NUL 等非文本内容。服务器复制同样的数据作为响应，客户端逐次检查完整响应内容。协议头、元数据和编码字节由框架决定。
- `--handler-us` 给 Server 的 handler 增加指定微秒的忙等，默认 0。这个参数按单调时钟计时，目标是构造处理线程占用。被操作系统抢占的时间也会计入忙等目标。
- `rpc_coro server --handler-workers N` 让同一个 Echo 路由经 `coro_io::post` 切换到独立 Asio handler 池，模拟 Mooncake Store metadata handler 的调度结构。默认 0 走框架内联 handler。比较这两个模式时，外部 CPU 预算应单独固定或注明。

计数采用闭环负载。预热使用同一组连接，预热 RPC 不计入测量。Client 在约定时点开始测量，每个 lane 到测量截止时停止发起新请求，并等待已发请求返回。JSON 中 `drained` 是截止后完成的请求数，`elapsed_s` 至少为指定的测量时长，包含最后请求的排空时间。`rps = success / elapsed_s`。每个成功样本的耗时从发起到 RPC 返回且数据校验完成。全量成功样本写入逐 lane 的直方图，再合并生成 P50、P95、P99 和 P99.9；`histogram` 条目为 `[微秒上界, 次数]`。1,024 µs 以内桶宽 1 µs，之后按 2 的幂分桶，长尾量化上界在输出时不超过实测最大值向上取整。均值与最大值直接从成功样本计算。

该负载用于对比框架调度、编解码、拷贝和 TCP 路径的合计成本。一次 Echo 的延迟无法直接拆成这些阶段。应用层重试、队列、连接池、缓存/对象接口、跨机网络、TLS、共享内存和 RDMA 均属于另外的实验条件。

## Matched host matrix and process evidence

Use a Linux host with cgroup v2, systemd, `ss`, and separate CPU sets. The
recorded host used CPUs 16–23 for the server, 24–31 for the client, and CPU
15 for monitoring. The 4-worker cases use the first four CPUs per role.

    mkdir -p results
    sudo env "PATH=$PATH" "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" \
      RPC_SECONDS=4 RPC_WARMUP=1 bash tools/run-host.sh build results main
    python3 tools/summarize.py results/main/cases results/summary

The runner alternates the engine order across cases and repeats. It verifies
the observed TCP connection count, records cgroup CPU consumption for each role,
and saves per-thread Linux counters, process RSS, and host CPU counters.
For a different CPU layout, run `python3 tools/run_matrix.py --help` and
substitute the appropriate CPU sets. The profiling command in
`tools/profile_one.sh` runs separately from the comparison matrix.
