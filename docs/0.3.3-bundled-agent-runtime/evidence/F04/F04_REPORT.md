# F04 Minimal Vertical Fusion Spike Report

## 最终 verdict

`FUSION_COMPATIBLE`

本轮 task-owned fused logic 与 macOS/Sunny 两端 F04-owned Electron worker trace 均通过。macOS 使用主 checkout 的 Electron `43.1.0` binary；Sunny 复用自有 task root 与 F03 exact launcher/runtime。两端 trace 均独立落盘，包含 main/worker fingerprint、同 PID、worker thread 与 fused cases。

Claude upstream SemVer/commit 未知没有被用作 F04 UNKNOWN_CRITICAL；本轮使用冻结的完整 snapshot identity。该未知身份仍列为 production/release provenance blocker。

## 身份与范围

- Compatibility evidence baseline：`492053c53441793c220f3b8e1dd231f1faea6e42`
- F04 effective working base：`9fef66c83e6a06541c98cca3194e37ac5a6dd533`
- F04 pre-closeout HEAD：`87de108fab64b43688463976b95cc92472c41721`
- Delta classification：`test-only; no production runtime change`
- Claude snapshot：`sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a`
- Claude manifest root：`b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a`
- Electron fingerprint：`43.1.0 / Node 24.18.0 / V8 15.0.245.13-electron.0 / modules 148`

写入严格限制为：

- `experiments/fusion-compatibility/F04/**`
- `docs/0.3.3-bundled-agent-runtime/evidence/F04/**`

未修改产品代码、Claude source、正式 package 配置、F01-F03 evidence 或冻结合同。

## 融合闭环结果

```text
Echo AgentTurnInput
  -> bounded adapted Claude loop
  -> deterministic fake model text + one tool call
  -> Echo fake tool correlated result
  -> continuation
  -> KernelEvent + terminal
```

实际结果：

| 场景 | 结果 | 证据 |
|---|---|---|
| success | PASS；7 events，1 fake tool invocation，`succeeded` | `integration.mjs macos/sunny` |
| one-tool correlation | PASS；`tool-1` result 必须匹配 `tool-1` | adapter test + loop trace |
| cancel | PASS；`cancelled`，0 tool invocation；loop late terminal audit-only | adapter test + loop trace |
| call-id mismatch | PASS；`MODEL_TOOL_CORRELATION_MISMATCH`，`toolInvoked=false` | adapter test + integration runner |
| model schema mismatch | PASS；`MODEL_SCHEMA_VERSION_MISMATCH`，terminal `failed` | adapter test |
| source snapshot mismatch | PASS；startup rejected | adapter test + integration runner |
| runtime fingerprint mismatch | PASS；startup rejected | adapter test + integration runner |

限定验证命令：

```text
node --check experiments/fusion-compatibility/F04/loop/loop.mjs
node --check experiments/fusion-compatibility/F04/loop/trace.mjs
node experiments/fusion-compatibility/F04/loop/trace.mjs --all --verify
F04_TRACE_OK cases=[success,cancel,mismatch] eventCounts=[7,6,5] toolInvocations=[1,0,0] terminalStates=[succeeded,cancelled,failed]

node --experimental-strip-types --check experiments/fusion-compatibility/F04/adapter/adapter.ts
node --experimental-strip-types --test experiments/fusion-compatibility/F04/adapter/test.mjs
5 tests passed; 0 failed; 0 skipped

node --experimental-strip-types experiments/fusion-compatibility/F04/integration.mjs macos
success=7 events/1 invocation/succeeded; cancel=2 events/0 invocation/cancelled; failClosed=call-id+source+runtime

node --experimental-strip-types experiments/fusion-compatibility/F04/integration.mjs sunny
success=7 events/1 invocation/succeeded; cancel=2 events/0 invocation/cancelled; failClosed=call-id+source+runtime
```

随后 Electron main + worker harness 在 macOS 与 Sunny 真实 runtime 中再次通过；完整原始摘要见 `macos-worker-trace.json` 与 `sunny-worker-trace.json`。

## Gate 映射

| Gate | 结果 | 说明 |
|---|---|---|
| exact Claude snapshot identity | PASS | F01 canonical snapshot + manifest hash |
| Echo baseline/effective base binding | PASS | 总控指定双基线字段已绑定 |
| F02 message/model/tool/event/cancel adapter | PASS | adapter 5 tests 全通过 |
| success + one-tool + continuation | PASS | loop 与 integrated runner 均通过 |
| cancel + first-terminal-wins | PASS | loop 额外验证 late terminal audit-only |
| call-id/schema/snapshot/runtime fail-closed | PASS | typed error code 与未调用 tool 断言通过 |
| worker no HOME/PATH/global Claude/port | PASS | F04 两端 worker trace forbidden hints 为空；F03 isolation 作为 supporting evidence |
| macOS F04-owned Electron worker trace | PASS | Electron 43.1.0 main/worker same PID，threadId=1，ABI 一致 |
| Sunny F04-owned Electron worker trace | PASS | Sunny exact launcher/runtime main/worker same PID，threadId=1，ABI 一致 |
| production package/installer/kernel closure | BLOCKED | 保留到 backlog；本轮明确禁止 |

## 三个 subagent 状态

1. `Loop slice integrator` — `019f64bf-0c06-7a50-8451-69d300de6a57`：`completed`；写入 `F04/loop/**`，loop trace 验证通过。
2. `Adapter implementer` — `019f64bf-0cbc-7501-b991-ee48c76ba1c9`：`errored_usage_limit_after_files_written`；已留下 `F04/adapter/**`，主线程独立运行其 5 项测试并通过。
3. `Compatibility verifier` — `019f64ca-0092-7ea3-8f44-a41583f6cfca`：tool status `not_found_no_report_returned`；未产生 evidence 文件，主线程完成限定 verifier 工作并保留实际 blocker。

没有创建或派生第 4 个 subagent。

## 保留的 production/release blockers

- 只能以 snapshot identity 做 F04；Claude upstream release/commit、package lock、SDK exact version、macro producer 未闭合。
- 完整 production query closure 仍包含 dynamic/Bun/native/auth/config/session 等 adapter/exclude 工作。
- Echo durable event sequence、checkpoint/resume、compact/budget、skills/hooks、grant revocation 的生产语义尚未在真实 kernel 闭合。
- 真实 packaged asar/unpacked resource readback、签名 app、Windows NSIS/ACL/UAC、worker manifest/hash 尚未执行。
- B01-B03 不因本报告自动 READY；必须先完成唯一 F04 repair 并重新验证两端 worker trace。

## 禁止项确认

本轮没有运行真实长时模型、全量测试、正式 DMG/NSIS、真实 AgentTaskService、产品启动、push、PR 或发布。`FUSION_COMPATIBLE` 仅解锁最小融合 spike gate，不清除 production/release backlog。

详细 evidence、baseline 与 backlog 见同目录其余四个文件。
