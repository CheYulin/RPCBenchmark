# 原生 coro_rpc pipeline：固定 logical concurrency

本实验保存在私有 RPCBenchmark 的 `master`，通过独立 worktree 开发和验证。原 `rpc_coro`、`rpc_brpc`、`lab` 数据路径不变；新增二进制
`rpc_coro_pipeline` 重用原生 coroutine loop 与 `lab::Stats`。不连接 DataSystem，也不对外发布此仓源码。

| 总 logical concurrency | depth 1 连接数 | depth 8 连接数 | depth 16 连接数 |
|---|---:|---:|---:|
| 32 | 32 | 4 | 2 |
| 128 | 128 | 16 | 8 |
| 256 | 256 | 32 | 16 |

两端均为四个 I/O workers，payload 请求/响应各为 4 B 或 4096 B，服务端为原生立即 Echo。每个 logical lane
仅持有一个请求，depth 个 lane 共享一个持久 client/连接。`send_request` 的第一个 `co_await` 发送帧并返回
独立响应 Lazy，第二个 `co_await` 等该请求的响应；所有 lane 独立 await，因此共享连接可有多条未完成请求。
不会批量发送后按固定 FIFO 顺序等响应。每条 payload 前四字节编码 lane ID，完整比较所有响应字节，避免同连接
回复错配被相同字符串掩盖。并发 32/depth 16 只有两条连接，因此虽配置四个 workers，也可能只驱动两个 client
executor；结果必须保留此边界，不能称为保持了有效并行 I/O 线程数。

每条连接的 active/peak 只由绑定到同一 executor 的 request coroutine 更新；主控线程只在全部 phase futures
或 caller threads join 后读取。Runtime 统一拥有连接，退出先 close 全部 client，再停止并 join I/O，随后销毁
client，最后销毁 pool/executor。连接在 connect 前已纳入 owner，异常退出也遵守此顺序；已取消的 YLT timer
回调不会越过 client 析构继续运行，client 析构也不会访问已销毁的 executor。统计字段不要求跨 executor 的无锁读写。

构建依赖仅 Linux、C++20、pthread 与仓内锁定的 YLT 头文件（含 Asio/async_simple），不需 bRPC/protobuf。

```bash
CXX=g++ cmake -S . -B build-pipeline -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DRPC_LAB_BUILD_BRPC=OFF -DRPC_LAB_BUILD_PIPELINE_EXPERIMENT=ON \
  -DRPC_LAB_YLT_ARCHIVE=/path/to/pinned-ylt.tar.gz
cmake --build build-pipeline --target rpc_coro_pipeline -j 2
ctest --test-dir build-pipeline -R rpc_coro_pipeline_checks --output-on-failure
# 或：bazel build //:rpc_coro_pipeline && bazel test //:rpc_coro_pipeline_checks
```

`verify` 子命令覆盖 depth 1/8/16 × 4 B/4 KiB × 两波。服务端在同一连接收满 depth 个请求前不回复，再逆序
返回全部请求；串行伪 pipeline 会超时。逐个请求验证完整字节，检查 native `get_pipeline_size()` 达到配置
深度及最终排空。`pipeline_smoke.py` 还检查直方图样本守恒、实际错误和末字节破坏不会被算为成功。错误后该
logical lane 停止发起请求，防止连接坏掉后的快速失败形成假吞吐。`pipeline_counters_test.py` 可先在本地运行。

```bash
python3 tests/pipeline_counters_test.py
./build-pipeline/rpc_coro_pipeline verify
python3 supplemental/run-native-pipeline.py --binary build-pipeline/rpc_coro_pipeline \
  --out results/native-pipeline --server-cpus 0,2,4,6 --client-cpus 16,18,20,22 \
  --monitor-cpu 34 --rounds 4 --warmup 1 --seconds 3
```

运行前用 `lscpu -e=CPU,CORE,NODE,SOCKET` 确认物理核和 NUMA，并等待外部调度放行。脚本复用原 lab 的 `ss`
连接数验证、进程/线程/RSS 与可选 cgroup CPU 采样，每个 case 重新启动服务端，轮换 depth 顺序。每个成功调用
均记录原有直方图；截止后排空计入 elapsed，不丢弃慢请求。四轮完整矩阵为 72 个窗口，1 s 预热+3 s 计时约
五分钟。cgroup CPU 窗口从 WARMUP_DONE 到退出，包含约 30 ms phase lead 与 drain，并非精确的计时区间。

2026-11-11 已完成 Linux GCC 11.5.0 手工 `-std=c++20 -O2 -DNDEBUG -pthread -Wall -Wextra -Werror` 编译及
`pipeline_smoke.py` 全部功能检查。旧 native binary 跑新检查因缺少 verify 契约而 RED；新 binary GREEN。
另以相同固定 YLT 头文件完成 CMake coro-only 新目标构建，CTest `rpc_coro_pipeline_checks` 1/1 PASS，4.41 s。
主机缺 Ninja，第一次 configure 失败；改 `-G "Unix Makefiles"` 成功，没有重建 bRPC。Bazel 新 target 尚未验证。
CMake 默认 Release 为 O3；后续计时使用显式 O2 的手工 binary，不能把两种编译产物混用。性能计时仍待统一调度，
不可用 pipeline 结果直接解释 DataSystem 的 handler、桥接、认证、对象缓存或物理跨机成本。

无需 CMake 的精确 O2 编译方式（`ylt` 指向校验过 SHA256 的固定源码目录）：

```bash
g++ -std=c++20 -O2 -DNDEBUG -pthread -Wall -Wextra -Werror -Isrc \
  -isystem "$ylt/include" -isystem "$ylt/include/ylt/thirdparty" -isystem "$ylt/include/ylt/standalone" \
  src/lab.cpp src/coro_pipeline.cpp -o build-native/rpc_coro_pipeline
python3 tests/pipeline_smoke.py build-native/rpc_coro_pipeline
```

`--caller coroutine`（默认）在原生协程内持续循环；`--caller sync --pipeline 1` 为每个 lane 建一个
`std::thread`，每次 RPC 都 `syncAwait(requestOnce(...).via(executor))` 后回到调用线程，再发下一次。
两模式共用相同 native Echo（与原程序一样返回 const-reference 输入的副本）、连接、全字节校验与直方图。
线程在每 phase 开始前创建；32 lane 的创建使用 30 ms start lead，不纳入该提前量内的样本。
该对照量化的是每请求调用者/IO 交接及 syncAwait 的整体成本，并非纯 context switch、纯 CPU overhead 或
DataSystem Client::Call 的逐指令复刻。只跑 32 caller / 32 连接 / 4 KiB 的判别点：

```bash
python3 supplemental/run-native-pipeline.py --binary build-pipeline/rpc_coro_pipeline \
  --out results/native-caller --server-cpus 0,2,4,6 --client-cpus 16,18,20,22 \
  --monitor-cpu 34 --rounds 4 --warmup 1 --seconds 4 \
  --concurrency 32 --depth 1 --payload 4096 --caller coroutine --caller sync
# 小规模 pipeline 门槛：4B/4096 × c32/128 × depth1/8/16 × 两轮，共24窗口
python3 supplemental/run-native-pipeline.py --binary build-pipeline/rpc_coro_pipeline \
  --out results/native-pipeline-gate --server-cpus 0,2,4,6 --client-cpus 16,18,20,22 \
  --monitor-cpu 34 --rounds 2 --warmup 1 --seconds 3 --concurrency 32 --concurrency 128
```

生命周期回归 `pipeline_lifecycle.py` 对 128 个 owner / depth 1 重复正常退出、请求超时和连接拒绝各三次。
`--timeout-ms` 控制请求 deadline；连接建立 timeout 至少为 1000 ms，使 1 ms 请求超时测试不会因 setup 抢先
失败。原 2901706 在输出完整统计后 client 析构向已销毁 executor 投递 close，原始 core 与首次新回归均确认
teardown SIGSEGV；该失败窗口不计入性能结果。修复仅改变 setup/teardown ownership，正常请求、payload、
统计和计时循环未改。Valgrind 对原 128-client 退出报告 2569 errors / 26 contexts，修复后 focused case 为
0 errors、0 outstanding heap blocks；该证据不代表遍历了所有请求失败/上游库路径。
