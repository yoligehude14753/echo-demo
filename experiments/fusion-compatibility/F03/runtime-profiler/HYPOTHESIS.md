# F03 Electron Runtime Probe 假设

## 问题

EchoDesk 当前 lock 解析的 Electron 43.1.0 embedded runtime 是否能在真实 Electron main 中创建 `worker_threads`，并让 main/worker 共享可接受的 Node/V8/modules ABI。

## H1

目标 Electron 43.1.0 的真实 Electron main 与 `worker_threads` worker 都可启动；worker 与 main 同 PID，`isMainThread=false` 且 `threadId>0`，并且 Node/V8/modules ABI fingerprint 一致。

## H0

目标 Electron runtime 或 worker_threads probe 无法启动，或 main/worker 的 PID、版本或 ABI 不满足冻结合同。

## 单一变量

只操控 runtime：同一份 probe 分别在目标 Electron 43.1.0 与 shell Node 24.3.0 边界运行；不构建 EchoDesk、不加载产品代码、不共享另一平台的 `node_modules` 或 npm cache。

## 度量

- main/worker `process.versions`、V8、`modules`、N-API、`node_module_version`；
- `process.execPath`、`process.type`、PID/PPID、`isMainThread`、`threadId`；
- worker 是否在 Electron main 的同一 OS PID 内；
- probe 是否能在隔离 `HOME/PATH` 下完成。

## 判定门槛

- H1 通过：目标 Electron main 与 worker 均有原始 fingerprint，Electron 版本为 43.1.0，worker 同 PID、`isMainThread=false`、`threadId>0`，main/worker 的 `versions.node`、`v8`、`modules`、`node_module_version` 一致；
- H1 不确定：目标 Electron binary 不可执行或任一平台缺少真实 probe，不能用 shell Node 或其他 Electron 替代；
- H1 被反驳：目标 Electron 可执行但上述任一 runtime/worker 条件失败。
