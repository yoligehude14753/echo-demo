# F04 Minimal Vertical Fusion Spike — bounded loop slice

这是一个 task-owned、non-production 的 Echo-shaped loop 实验。它只绑定 Claude snapshot 身份：

```text
sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a
```

实验没有 import Claude source、Echo 产品代码、真实模型、AgentTaskService、文件/网络依赖或正式 package 配置。

## 输入

driver 使用固定输入：

```json
{
  "taskId": "f04-task-<case>",
  "operationKey": "f04-op-<case>",
  "requestId": "f04-request-<case>",
  "grantId": "grant-f04-001",
  "grantRevision": 7,
  "userText": "Read demo.txt and summarize it"
}
```

embedded boundary 会拒绝 `runnerModel`、`runnerBaseUrl`、`credential`、`credentials`。

## Loop 与可调用接口

`BoundedFusionLoop` 暴露：

- `startTurn(input)`：发出 turn start、fake deterministic text 与一个 `tool-1` 调用。
- `invokePendingTool()`：调用 fake `Read` registry，返回带同一 `toolUseId` 的结果，并携带 `grantId/grantRevision`。
- `resumeWithToolResult(result)`：只接受与 pending `toolUseId` 完全相同的结果，随后发出 continuation、文本和 terminal `agent.turn.completed`。
- `cancel(request)`：发出 cancel request；未执行的工具收到 correlated synthetic error result，随后 terminal `agent.turn.cancelled`。
- `rejectMismatch(request)`：发出 `MODEL_TOOL_CORRELATION_MISMATCH` terminal failure；`toolInvoked=false`，fake tool 不会被调用。
- `recordLateTerminal(...)`：在 cancel terminal 已胜出后仅写 audit，不改变 durable terminal。

所有 `KernelEvent` 带 `schemaVersion/eventId/seq/taskId/operationKey/requestId/event/payload/source/emittedAt/terminal`；`seq`、ID、时间和 fake 输出均为确定性的。

## 确定性命令

在仓库根目录执行：

```bash
node --check experiments/fusion-compatibility/F04/loop/loop.mjs
node --check experiments/fusion-compatibility/F04/loop/trace.mjs
node experiments/fusion-compatibility/F04/loop/trace.mjs --all --verify
```

预期最后一条命令精确输出：

```text
F04_TRACE_OK {"cases":["success","cancel","mismatch"],"eventCounts":{"success":7,"cancel":6,"mismatch":5},"toolInvocations":{"success":1,"cancel":0,"mismatch":0},"terminalStates":{"success":"succeeded","cancel":"cancelled","mismatch":"failed"}}
```

查看完整 JSONL trace：

```bash
node experiments/fusion-compatibility/F04/loop/trace.mjs --all
```

## 边界与剩余 blocker

- `success`：fake text → one tool call → correlated fake result → continuation → terminal KernelEvent。
- `cancel`：cancel 与 pending tool 竞争时 first-terminal-wins；late success 只进入 audit。
- `mismatch`：错误 `toolUseId` fail-closed，且不会执行 fake tool。
- 本 slice 不声称生产兼容、Claude source direct-vendoring、checkpoint/compact、skills/hooks、Electron/package/runtime 或真实权限持久化已闭合。
- 当前 checkout 是 detached `87de108...`，不是用户描述的 `9fef66c...`；本任务未切换、重置或提交。
- 任务要求点名的完整 F04 task book、`FUSION_COMPATIBILITY_GATE.md`、`CONTRACT_FREEZE_V1.md` 在当前 checkout/本地文件系统中不可读；本实验只使用当前 F01–F03 evidence 对 F04 adapter work 的已明确约束，未改写冻结合同。

