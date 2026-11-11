# 私有 coro_rpc 256 KiB write 实验

本实验基于固定生命周期的 e31cfd1 harness。DataSystem、bRPC、既有冻结二进制、共享 YLT cache 均不修改。
唯一上游代码补丁是 pinned `coro_io::async_write` 的调用点：定义 `RPC_LAB_CORO_WRITE_256K` 时传入
`error ? 0 : 262144` 的 completion condition，未定义时保留原来的默认 `asio::async_write` 调用。
不改变 read、协议、序列化、deadline、请求 owner、lane 数或 I/O worker 数。

源依据是 pin `45c6463c0afbcc46262e373effa30635be3846a0` 的 `coro_io.hpp`、Asio
`completion_condition.hpp` 和 `impl/write.hpp`：默认 `transfer_all` 每步提供至多 65536 字节；组合 write
按该上限调用 `async_write_some`，自行处理部分写入、错误、零进展和 cancellation。新 condition 只改变每步
提供的最大字节数，error 时仍返回零终止。更大的 offered buffer 可能仍部分写入，不保证真实 socket 一次发完。

`prepare_overlay.py` 校验固定 archive SHA256，提取完整私有 include tree 后应用单文件补丁。完整 tree 用于
避免内部 quoted relative include 绕开单头文件覆盖；两臂使用完全相同的 tree，仅编译宏不同。不会在共享源码
或 cache 中运行 patch，不把 vendor tree 提交到仓库。

## 构建和功能验证

在 Linux GCC 11 环境，工作目录为此 worktree。构建目录必须是新的，防止覆盖旧产物：

```bash
python3 supplemental/native-write256/run_preflight.py --stage unit \
  --archive /opt/rpc-framework-lab-tiantiyun-20261108/upstream/ylt-45c6463.tar.gz \
  --build-dir build-write256
python3 supplemental/native-write256/run_preflight.py --stage native \
  --build-dir build-write256 --entry supplemental/native-bulk-io/mixed_entry.cpp
python3 supplemental/native-write256/mixed_smoke.py \
  --frozen build-write256/frozen --out build-write256/mixed-preflight
```

编译固定使用 `-std=c++20 -O2 -DNDEBUG -pthread -Wall -Wextra -Werror`。fake AsyncWriteStream 经真正的
`coro_io::async_write` / Asio 组合 write 验证六项：含二进制字节的 1 MiB scatter buffer 理想调用次数、部分
写入、带已传字节的中途错误、首次错误、空缓冲区的异步完成、非空缓冲区零进展时终止。新四次调用要求在旧宏
OFF binary 上必须 RED（实际 16 次），宏 ON binary 上 GREEN（四次）；旧实现另按 64 KiB 契约验证全部语义。
这是一块严格 1 MiB buffer 的 ideal-stream 计数，实际 RPC 带 header/序列化前缀，可能是理想 17→5 而非 16→4；
真实 syscall 次数还受部分写入、EAGAIN 等影响。

随后两个 native binary 分别复用既有 smoke 与 128-client lifecycle，检查逆序响应、全字节匹配、同步调用者、
请求超时/连接失败与 teardown。源码 hash 在编译前后必须一致；成功后才在 `build-write256/frozen/` 冻结两臂
binary、编译命令、preflight 结果、输入源码及 overlay manifest。此脚本不启动性能窗口。

## 性能门槛与回滚

仅由外部调度统一运行 1 MiB 纯负载、4 KiB 纯负载、24 tiny + 8 bulk 的混合负载。混合为一个四 I/O worker
Runtime、32 个连接、depth 1、共同 phase 和共同 drain-end 分母，按两组分别保留直方图和 P99。
两臂使用同一 entry、payload 校验、CPU 绑定和编译 flags。大包 write chunk 增大可能影响同 executor 的小请求
调度公平性，因此混合 small P99 是必要门槛。没有清楚收益或 tail 恶化即拒绝本候选，不增加更多参数扫描。
原始 f92dd5809b0d… 冻结 binary 保留，实验结果不能直接推断 DataSystem Get 收益；产品不切换此补丁。

## 固定 12 窗结果：拒绝迁移

已按预声明顺序对三类负载各运行 64K、256K、256K、64K，共 12 个有效窗口。1 s warmup + 4 s 计量，
四个 I/O executors/角色、32 连接、depth 1、同一编译 entry；混合为一个 client Runtime 的 24 tiny + 8 bulk。
测量共 5,189,106 次成功，RPC/字节错误均为零。以下是每臂两个窗口的中位数；这是有界筛选，不是统计显著性声明。

| 负载 | 64 KiB QPS | 256 KiB QPS | 中位 QPS 变化 | 其他证据 |
|---|---:|---:|---:|---|
| 1 MiB / c32 | 1811.6625 | 2172.7230 | +19.9298% | CPU/成功 3632.0675→3091.0880 us，−14.8945% |
| 4 KiB / c32 | 287486.0120 | 290364.4295 | +1.0012% | reported P99 123→119.5 us，−2.8455% |
| mixed tiny / c24 | 46240.0870 | 17746.0220 | −61.6220% | reported P50 195.5→836 us |
| mixed bulk / c8 | 1347.8550 | 1378.3040 | +2.2591% | 两个配对 QPS 变化 +10.4280% / −4.7881% |

纯大包的两个配对 QPS 均改善（+18.8957% / +20.9881%），CPU/成功均下降（−12.6192% / −17.0609%），
通过该负载的门槛。但 mixed tiny 的两个配对 QPS 分别下降 61.0648% / 62.1624%，直接违反最多下降 3% 的
预声明要求，因此决策为 `reject_migration`。没有改门槛、增加参数或补跑窗口。

Mixed tiny 四个窗口的 reported P99 都是 4096 us 粗桶，不能据相同桶值宣称 tail 无退化或满足 5% margin；
分辨率检查也拒绝。所有高于 1024 us 的 quantile 都使用 power-of-two 桶，纯大包 32768→16384 的 P50 桶
变化不能称为延迟精确减半。CPU 角色窗口从 WARMUP_DONE 到进程完成，包含 phase lead/drain/teardown；
不按 mixed 类别虚构 CPU 归属。原生 echo 的大 payload 双向传输和 inline handler 不能直接外推 DataSystem Get。

工具与预声明方案保存在 stage 1 提交 `985b7aa4da7e73efc816b0cfdfa80f464ef24b89`。十个 frozen 源码输入
逐个匹配该提交 blob；具体结果、逐窗数据摘要、预声明检查和 receipt/source-binding 见 [gate-result.json](gate-result.json)。

- 64K binary SHA256：`d06b135fd5e26e7b62f0823ecca2673d2c95b3a315daeacb2c64a1c1925bf42f`
- 256K binary SHA256：`c2e4f49880e6486063e6a03e360bc4971ed704b819e938aed03bb4621c4ddb7d`
- 原 frozen manifest SHA256：`d39f4f3caf3e2eb50d346d1c3c3c8e944cf0e6c9b13e8af92cbe5477c4226d3f`
- 原始 176983 B 证据归档 SHA256：`87519923841cff3c9c6b1a6f322275237b28f54ff23a313df38047bb5c5debc3`

终态 binary/manifest 校验保持不变，bench/build/perf 进程均为空。未执行 counter pair；不再追加测试、性能窗或
其他实验。候选作为默认关闭的实验保留在私有 RPCBenchmark 的 `master`，DataSystem 产品维持 `b3df79f0ee6445e06ceeae446b987e6ea06c5846`。
