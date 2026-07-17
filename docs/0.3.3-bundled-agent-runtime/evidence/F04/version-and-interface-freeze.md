# F04 版本与接口冻结记录

## 结论

F04 的 task-owned deterministic fused logic 与两端 Electron worker trace 均已通过，本轮最终 verdict 为 `FUSION_COMPATIBLE`。该 verdict 不把 Claude upstream SemVer/commit 未知误判为 F04 blocker；snapshot identity 已冻结并绑定。upstream provenance、package closure、签名安装态和生产 kernel 仍是 release blockers。

## 身份冻结

| 项目 | 冻结值 | 证据 |
|---|---|---|
| Compatibility evidence baseline | `492053c53441793c220f3b8e1dd231f1faea6e42` | F01/F02/F03 reports、FUSION gate |
| F04 effective working base | `9fef66c83e6a06541c98cca3194e37ac5a6dd533` | 总控裁定；test-only 后继 |
| F04 pre-closeout HEAD | `87de108fab64b43688463976b95cc92472c41721` | cherry-pick 三枚 evidence commit 后 |
| Echo delta | `test-only; no production runtime change` | `9fef66c` subject |
| Claude snapshot | `sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a` | F01 canonical manifest |
| Claude upstream version | `unknown` | provenance 不可还原；列为 release blocker |
| Claude manifest root | `b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a` | F01 manifest readback |
| Electron | `43.1.0` | F03 macOS/Sunny fingerprints |
| Embedded Node | `24.18.0` | F03 macOS/Sunny fingerprints |
| V8 | `15.0.245.13-electron.0` | F03 macOS/Sunny fingerprints |
| Modules ABI | `148` | F03 macOS/Sunny fingerprints |
| Contract versions | Kernel/IPC/Model/Grant/Checkpoint/Event = `1` | `CONTRACT_FREEZE_V1.md` |

## 接口冻结

F04 实验 adapter 固定以下语义：

- `AgentTurnInput` 的 `taskId`、`operationKey`、`systemPrompt`、ordered `messages` 和 `deadlineAt` 必须显式提供；runner model/base URL/raw credential 字段拒绝进入 embedded boundary。
- model request 固定 `requestId + taskId + operationKey + configRevision + routeId`；model event 必须带 `schemaVersion=1` 且 request ID 与 active request 一致。
- tool correlation 只使用 `toolUseId`；未知、重复或不匹配结果返回 `MODEL_TOOL_CORRELATION_MISMATCH`，`toolInvoked=false`，不执行 fake tool。
- tool arguments 完成后必须是 JSON object；schema mismatch 返回 typed failure，不把半截 JSON 交给 tool。
- grant 固定 `grantId + grantRevision + taskId`，session 内不变；tool invocation 复核 grant context。
- cancel 是幂等的，终态 first-terminal-wins；loop 中迟到 terminal 只进入 audit，不覆盖已提交终态。
- source snapshot、manifest、Echo baseline、runtime fingerprint 任一不匹配，session startup fail closed。

## 平台 trace 状态

本轮 `integration.mjs macos` 与 `integration.mjs sunny` 已在 deterministic host runner 中得到相同的 success/cancel/fail-closed 结果；随后 `electron-worker-harness.cjs` 在两端真实 Electron worker 中再次得到相同 fused trace。

- macOS F04-owned Electron worker trace：`PASS`；Electron `43.1.0`、main/worker same PID、worker `threadId=1`、ABI 一致。
- Sunny F04-owned Electron worker trace：`PASS`；复用 Sunny 自有 `%TEMP%\\echodesk-f03-electron-43` runtime 与 launcher，main/worker same PID、worker `threadId=1`、ABI 一致。
- F03 macOS/Sunny worker evidence：保留为 supporting evidence；F04-owned traces 已独立落在本目录。

## F04 gate 判定

| Gate | 结果 |
|---|---|
| Source snapshot exact identity | PASS |
| Echo baseline/effective base binding | PASS |
| Adapter schema/call-id/runtime fail-closed | PASS |
| Deterministic success + one-tool continuation | PASS |
| Deterministic cancel + first-terminal-wins | PASS |
| macOS F04-owned Electron worker trace | PASS |
| Sunny F04-owned Electron worker trace | PASS |
| Production closure/package/install | BLOCKED; out of scope and remains backlog |

F04 worker repair 已完成。本轮后续不再有 F04 compatibility repair；production/release blockers 继续按 `production-adapter-backlog.md` 管理。
