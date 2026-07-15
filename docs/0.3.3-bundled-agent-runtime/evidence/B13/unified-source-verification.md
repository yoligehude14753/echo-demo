# B13 Unified Source Verification

## 当前状态

- 日期：2026-07-16（Asia/Shanghai）
- worktree：`/Users/yoligehude/.codex/worktrees/1fd3/echo`
- B10 起点：`8d5bdb6fdaa0b0d8e2be8275f98b4f6f862ccab5`
- B11 输入：`d2fae70a203510ea5ecbee4c8238da41a1608c79`，本地整合 commit：`a5bf3308f58ae9958b27dce9b03f1a5b6d1d2c47`
- B12 输入：`4742d9d4fd42e5bf90e0ba1be7babc4554d438a2`，本地整合 commit：`6f22e75fc5c219ac9bce10635e0ce0fdbdb4ffbb`
- 外部 `_platforms` 固定 transport：`158844db23cc5884889233fb8bdd7d943f3002f9`
- 固定 SHA 的 detached 只读语义 worktree：`/tmp/echodesk-b13-platforms`
- 当前 verdict：`REWORK_REQUIRED`

## 本轮窄修与冲突裁定

- B11→B12 机械 lineage 保持不变；B11 persistence 继续拥有 session/checkpoint durable state，B12 embedded backend 继续拥有 inherited-FD runtime 语义。
- B10 identity 与 B11 的持久化 identity 冲突由新增 `make_b13_resume_identity` 裁定：`taskId + operationKey` 生成稳定 session id，并把 operation key 补入持久化 grant snapshot；原始 B10 grant 不变，错配 fail closed。
- Electron 生产接线新增 `factoryData` 只读、secret-free descriptor，经 `resolveFactoryModule → WorkerManager → worker-entry` 进入可执行 `b13-worker-factory.ts`；缺少 deps module、provenance 或完整 `KernelDeps` 均 fail closed。
- 未恢复任何 HTTP/WebSocket fallback。stale bridge/cancel tests 改为验证 `EmbeddedTaskStreamBridge` 与当前 local terminal/outbox 语义。

## 三个原 subagent

1. `runtime-persistence-integration`，id `019f66a0-d862-7272-b218-846162aedd57`：产出 `backend/app/runtime/b13_composition.py` 与 composition/persistence focused tests；修复 inherited-FD backend 实例化和 B12 backend 关闭条件。
2. `model-tool-provider-integration`，id `019f66a0-d8d3-73c1-8463-3f18625516bd`：产出 `backend/app/runtime/b13_model_tool_provider.py` 与 focused tests；绑定 B05M gateway、固定 yoli transport、B06P file host/receipt，并执行 bounded live smoke。
3. `unified-source-verification`，id `019f66a0-d94e-7750-a83c-e8657aeb8327`：维护本目录矩阵证据。其原始基线记录曾停在 B12 冲突；主任务完成整合后由同一 id 复用继续核对。后续 tool 状态显示原实例不可再轮询，未创建第四个 agent。

## 通过证据

| 范围 | 命令/结果 |
|---|---|
| B13 composition/persistence | `pytest -q backend/tests/unit/agent_runtime/test_b13_composition.py` → `5 passed` |
| B13 model/tool/provider source | 固定 detached `_platforms` SHA 的 `PYTHONPATH=/tmp/echodesk-b13-platforms/llm/src pytest -q backend/tests/unit/agent_runtime/test_b13_model_tool_provider.py` → `3 passed` |
| B11+B12 focused | `pytest -q .../test_b11_resume_identity.py .../test_b12_migration_route_policy.py` → `11 passed` |
| agent-runtime backend source | `pytest -q backend/tests/unit/agent_runtime` → `17 passed` |
| B06P capability hosts | `pytest -q backend/tests/unit/agent_capabilities` → `69 passed` |
| AgentTaskService | `pytest -q backend/tests/unit/test_agent_task_service.py` → `24 passed` |
| Embedded bridge/cancel increment | `pytest -q backend/tests/unit/test_agent_bridge_recovery.py backend/tests/unit/test_agent_cancel_outbox.py::test_resume_submit_race_never_revives_cancelled_task backend/tests/integration/test_echo_task_stream_bridge.py` → `16 passed` |
| Model-runtime increment | `pytest -q backend/tests/unit/model_runtime` → `38 passed`; authoritative `openai_error.jsonl` hash updated to `d82eee...6c8cd4` |
| Electron agent-runtime | `node --experimental-strip-types --test desktop/electron/agent-runtime/test/*.test.ts` → `8 passed` |
| TypeScript toolchain | existing lock-resolved workspace `desktop/node_modules/.bin/tsc`; `agent-kernel`, `desktop`, `electron/agent-runtime` `--noEmit` → pass |
| Python quality | `ruff check` on B13 glue/tests and resolved `agentos.py` → pass；`compileall` → pass |

这些结果证明了 source-level persistence identity、grant/receipt/provenance、embedded cancel/crash/fail-closed focused contracts，以及 WorkerManager worker crash/restart/identity contracts；它们不等价于 B05M/B06P 真实绑定后的完整 fused production turn 闭合证明。

## 真实 provider/tool 证据

命令：

```text
PYTHONPATH=/tmp/echodesk-b13-platforms/llm/src \
/opt/homebrew/opt/python@3.11/bin/python3.11 \
-m app.runtime.b13_model_tool_provider --task-id b13-live-smoke
```

结果：`status=PASS code=PROVIDER_STREAM_OK model_events=7 EXIT_CODE:0`。transport 日志只显示非秘密 endpoint/protocol 元数据；没有输出或复制 credential secret。该 smoke 使用现有 Settings/config credential source，经 B05M `AgentModelGateway` 和固定 SHA 的 yoli SSE transport；不是 deterministic test transport。

B06P controlled tool path 在 focused source test 中读取 task-owned 临时文件并产生成功 receipt，覆盖 grant binding、toolUseId、revision 和 receipt result；该路径不是 provider 稳定性测试。

## 未闭合项与失败证据

1. `b13-worker-factory.ts` 已形成可执行 worker-local host-owned factory contract，但实际 host module 仍需把 Python B05M `AgentModelGateway`、B06P `CapabilityHostRegistry/receipts`、B11 session port 转成 worker 可用的 TypeScript ports；当前不存在 Python→worker IPC/serialization adapter，因此不能把 deterministic Electron fixture 记为真实 fused production PASS。
2. `backend/tests/unit/model_runtime/fixtures/fixture_manifest.json` 已按实际 `openai_error.jsonl` 更新为 `d82eee...6c8cd4`，model-runtime 增量 gate 已 `38 passed`。
3. stale `EchoTaskStreamBridge`/WebSocket 夹具已改为 `EmbeddedTaskStreamBridge` typed events；cancel race 断言已按当前 embedded local terminal/outbox 语义收口，增量 gate `16 passed`。
4. FactStore health check 返回 exit 2：`echo/_state/events` 不存在；本任务冻结写集未越界修复该治理目录。

## 禁止项审计

未运行 package build、签名、公证、staple、Authenticode、NSIS、安装态、跨平台验证、provider 稳定性/吞吐/压测、长实验；未新增 Claude CLI/daemon/runtime install/PATH/HOME/global auth fallback；未 push、未建 PR。

## Verdict

`REWORK_REQUIRED`：B10→B11→B12 本地整合、B11 persistence identity、B06P receipt、固定 transport 的真实 provider smoke、embedded/cancel 增量 gates 与三套 TypeScript `--noEmit` 已有证据；但完整 Electron WorkerManager→worker-local `KernelDeps`→B05M/B06P/B11 fused production turn 仍因缺少真实 Python-to-worker host adapter 未闭合。不得据此启动 B14/B15。
