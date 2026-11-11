# RPC 实验与验证入口

## 1. 实验范围

已完成验证的阶段提交统一合入 `master`；独立 worktree 用于隔离开发和构建。
实验保留显式入口，默认 adapter 不随某次筛选结果切换。这里的原生 Echo 实验不能替代 DataSystem 业务链路验证。

| 实验 | 入口 | 默认行为 |
|---|---|---|
| 原生 pipeline 与同步调用者对照 | [native-pipeline.md](native-pipeline.md) | CMake pipeline 选项关闭 |
| 调用线程推进 IO | [caller-owned-io/README.md](caller-owned-io/README.md) | CMake caller IO 选项关闭；已完成独立功能和 Memcheck 检查 |
| 固定到达率 SDK 驱动 | [ds-latency30/README.md](ds-latency30/README.md) | 独立构建、显式运行 |
| 私有 RPC 采集核心 | [ds-rpc-capture/README.md](ds-rpc-capture/README.md) | 独立 GNU11 语义测试通过；尚未接入实际 SDK 或用于性能验收 |
| 256 KiB write 上限 | [native-write256/README.md](native-write256/README.md) | 私有 overlay，宏默认关闭；保留拒绝迁移结论 |

## 2. 验证入口

各实验文档列出固定依赖、编译参数和功能检查。纯 Python 结果校验可在仓库根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/pipeline_counters_test.py
PYTHONDONTWRITEBYTECODE=1 python3 supplemental/native-bulk-io/validator_test.py
```

C++ 功能和内存检查须使用文档指定的 Linux 编译环境。新增 Bazel pipeline 目标没有完整构建验证，不能以目标存在代替通过记录。

## 3. 结果边界

保留原始失败、无效窗口、阶段源码和拒绝结论。历史功能证据只覆盖与其哈希一致的源码；不同编译参数、
源码或二进制需要重新绑定证据。固定到达率驱动的低率功能检查不证明高负载生成能力。

性能比较需要核对所有并行任务及测试核，特别是其他 worktree 的 IPC、RPC、编译和探针进程；启动检查
不等于整个计时区间独占。原生 Echo、SDK API、Worker 调度与实际 RPC 时延分别保留边界。
当前实验没有证明 DataSystem 相对 bRPC 的 RPC P99 降低 30%。

## 4. 演进与回滚

保留每个阶段和合并提交，不重写已经推送的历史。后续验证通过后继续合入 `master`，避免成果只留在实验分支。
实验回滚使用对应提交的正常 revert；不修改共享依赖缓存，不覆盖已冻结二进制。DataSystem 的后端选择及默认值由其自身仓库管理。
