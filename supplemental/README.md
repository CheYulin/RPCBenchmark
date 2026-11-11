# coro_rpc 线程位置与队头阻塞探针

其他实验、验证入口及结果边界见 [实验索引](experiments.md)。

使用 Mooncake Store `v0.3.14` 固定的 yaLanTingLibs `45c6463c0afbcc46262e373effa30635be3846a0`。源码归档的 SHA-256 为 `baa6b5c9ffcc0af8252742bba8f0fb9c6d37387c13b2ff5b26d29c6aaff6ee37`。[Mooncake 依赖锁](https://github.com/kvcache-ai/Mooncake/blob/e1ea5d31bb5e46ff2df9f90e7c2fbaf52fcdee8c/mooncake-common/FindYLT.cmake#L20-L25)

将对应源码解压后，在本目录运行。工具链需支持 Linux C++20。

```bash
CXX=g++-13 bash run-coro-thread-probe.sh /path/to/yalantinglibs
```

每次运行启动一个独立进程，服务端在 loopback 上监听临时端口。服务端只有一个 I/O 线程，客户端有两条分别预热的 TCP 连接。慢 handler 通知控制线程自己已进入，然后等待信号量。另一条连接发出 fast 请求，控制线程观察最多 100 ms，再放行慢 handler。五组试验交替运行两种模式。

| 模式 | 慢 handler 位置 | 期望的返回顺序 |
| --- | --- | --- |
| `direct` | 同一个连接 I/O 线程 | 放行后 fast 才能返回 |
| `offload` | `coro_io::post` 投递到一个独立 Asio worker | 放行前 fast 已返回，慢 RPC 仍在等待 |

通过条件还包括两个 RPC 都成功，fast handler TID 等于慢 handler 入站 I/O TID，慢 handler TID 与 I/O TID 的关系符合所选模式。`fast_rpc_us` 是诊断字段，直接模式会包含实验故意插入的观察等待。快慢请求的先后关系与线程位置是这组实验的验证对象，报告中的吞吐、CPU、尾延迟采用独立的压测矩阵。

## 实测记录

2026-11-07 20:19–20:21（UTC+8），本地 WSL2，Linux `6.6.87.2-microsoft-standard-WSL2`，x86_64，GCC `13.3.0`。先用 `-O2` 单独完成 1024 B 回显冒烟验证。带多个 handler 的探针首次 `-O2` 构建到达 120 秒限制，随后按脚本以 `-std=c++20 -O0 -DNDEBUG` 完成构建并运行。进程之间逐个启动，使用同一 YLT 源码与 loopback TCP。

| 模式 | fast 在放行前返回 | 线程位置 | 两个 RPC 都成功 |
| --- | ---: | --- | ---: |
| `direct` | 0/5 | I/O、fast、slow 同 TID，5/5 | 5/5 |
| `offload` | 5/5 | fast 与 I/O 同 TID，slow 为不同 TID，5/5 | 5/5 |

10/10 次慢请求在放行前仍未返回。原始观测为 `coro-thread-probe-wsl2.jsonl`。该记录来自本地线程探针，主实验使用独立环境与 Release 构建。
