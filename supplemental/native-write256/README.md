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
