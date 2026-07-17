# F04 生产 Adapter Backlog

本清单是 `FUSION_COMPATIBLE` 后仍保留的 production/release backlog。F04 只验证 task-owned deterministic slice；以下项目没有被本轮假设关闭。

## 必须先修复的 F04 条件

### F04-R1 — 两端 F04-owned Electron worker trace（已关闭）

- **状态**：`PASS`
- **证据**：`macos-worker-trace.json` 与 `sunny-worker-trace.json`。
- **结果**：两端同 PID worker、`isMainThread=false`、`threadId=1`、Node/V8/modules/N-API 与 Electron `43.1.0` 完全匹配；trace 含 success/one-tool/cancel/mismatch/fail-closed。
- **边界**：F03 旧 trace 仅作 supporting evidence；F04-owned trace 已独立落盘。

## 生产 adapter 工作

### PROD-A1 — Claude source provenance 与 lock

- **状态**：`BLOCKED_WITH_EVIDENCE`
- **缺口**：upstream release/commit、package manifest、SDK exact version、dependency lock、macro producer 未能从 snapshot 还原。
- **后续**：取得可验证发布身份与依赖 lock；重新生成 source manifest 与 production import graph。

### PROD-A2 — 完整 query closure 与 kernel-safe bundle

- **状态**：`BLOCKED_WITH_EVIDENCE`
- **缺口**：dynamic import/require、Bun macro/FFI、auth/config/session、filesystem/process/network、native/optional/WASM 与生成文件尚未完成闭包。
- **后续**：按 `DIRECT / LOSSLESS_ADAPTER / SEMANTIC_REWRITE / UNSUPPORTED` 逐边分类，生成 manifest/hash/asar layout，并证明 kernel 禁止 import 不会进入 bundle。

### PROD-A3 — Echo authoritative model/grant/session ports

- **状态**：`ADAPTER_REQUIRED`
- **缺口**：生产模型 config revision、credential handle、GrantSnapshot revocation、checkpoint/resume、compact/budget 尚未形成完整 runtime contract。
- **后续**：只通过 Echo ports 注入；禁止 Claude kernel 读取 HOME/PATH/settings/凭证或直接执行副作用。

### PROD-A4 — Durable event/terminal semantics

- **状态**：`ADAPTER_REQUIRED`
- **缺口**：F04 fake trace 已验证 identity/seq/terminal 形状，尚未证明 Echo durable seq、raw hash 去重、worker 重启恢复和 late terminal audit 在真实生产链路成立。
- **后续**：补真实 worker-to-Echo event sink contract 与 first-terminal-wins integration evidence。

### PROD-A5 — Permission/skills/hooks/compact

- **状态**：`UNKNOWN_CRITICAL`
- **缺口**：permission persistence、skills/hooks provenance、compact strategy、summary/budget/checkpoint resume 仍来自 F02/F03 gap ledger，未在 F04 关闭。
- **后续**：每个面单独定义 typed port、ordered canonical trace、拒绝/取消/恢复语义；在生产 Batch 前重新 gate。

### PROD-A6 — Packaged layout / installer / signing

- **状态**：`BLOCKED_WITH_EVIDENCE`
- **缺口**：真实 asar/unpacked resource readback、签名 app、Windows NSIS/ACL/UAC、安装卸载与 worker manifest 未验证。
- **后续**：仅在 F04 compatible 且生产 adapter 完成后，按单独发布门禁执行；本轮禁止 DMG/NSIS。

## 交付限制

- 不允许因本 backlog 直接修改产品代码、正式 package 配置、F01-F03 evidence 或冻结合同。
- F04 verdict `FUSION_COMPATIBLE` 只表示本轮最小融合 spike gate 通过；B01-B03 仍需处理下列生产/release blockers，不得把 spike 结果当作生产实现完成。
- 不允许把本实验目录升级为 production implementation；F04 产物只作为 compatibility evidence。
