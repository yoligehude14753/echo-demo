# B13 Unified Source Verification

## 当前状态

- 日期：2026-07-16（Asia/Shanghai）
- worktree：`/Users/yoligehude/.codex/worktrees/1fd3/echo`
- B10 起点：`8d5bdb6fdaa0b0d8e2be8275f98b4f6f862ccab5`
- B11 输入：`d2fae70a203510ea5ecbee4c8238da41a1608c79`，本地整合 commit：`a5bf3308f58ae9958b27dce9b03f1a5b6d1d2c47`
- B12 输入：`4742d9d4fd42e5bf90e0ba1be7babc4554d438a2`，本地整合 commit：`6f22e75fc5c219ac9bce10635e0ce0fdbdb4ffbb`
- 外部 `_platforms` 固定 transport：`158844db23cc5884889233fb8bdd7d943f3002f9`
- 固定 SHA 的 detached 只读语义 worktree：`/tmp/echodesk-b13-platforms`
- 当前 verdict：`SOURCE_INTEGRATION_READY`（仅 source/runtime candidate；必须先 B12R current-SHA rebind）

## B14R 窄返工回收

- B14R 观察到的固定 release SHA：`78616ca6f4f3212a2dd35801ac797f5dba76b55e`。
- 观察缺口：安装后的 Preview 已走 `127.0.0.1:8769`，但 packaged `main.cjs` 没有 `agent-runtime`/`worker-entry`/`runTurn` 接线，因而没有启动 B13 fused worker。
- 本轮只修 desktop production runtime composition：release local backend spawn 通过 inherited FD 3 + nonce 绑定 backend；local health 成功后，`main.cjs` 调用资源 hash 校验的 `PackagedFusedWorkerBridge`，由真实 `Worker` 加载既有 `worker.mjs`、production factory 与 B13 host-deps，并路由 framed task/host/event；bridge 或资源缺失即 fail closed。显式 `ECHO_PRINCIPAL_MODE=public` 仍只走 public service，未修改 bootstrap、签名或发布逻辑。
- focused selection/renderer/route gate：`26 passed`；desktop `tsc --project desktop/tsconfig.json --noEmit`：通过。
- 该 source candidate 尚未安装或重新打包。必须先由 B12R 对新 candidate SHA 做 current-SHA rebind，并生成包含 `worker.mjs`、production factory、B13 host-deps 和 fusion manifest 的 hash-verified `Resources/agent-runtime`；再由 B14R/B15R 重建验收资产并重跑 quarantined bootstrap/安装态 turn-tool-cancel-checkpoint-restart-resume 证据；旧 `78616ca...` artifact 不可复用。

## 本轮窄修与冲突裁定

- B11→B12 机械 lineage 保持不变；B11 persistence 继续拥有 session/checkpoint durable state，B12 embedded backend 继续拥有 inherited-FD runtime 语义。
- B10 identity 与 B11 的持久化 identity 冲突由新增 `make_b13_resume_identity` 裁定：`taskId + operationKey` 生成稳定 session id，并把 operation key 补入持久化 grant snapshot；原始 B10 grant 不变，错配 fail closed。
- Electron 生产接线新增 `factoryData` 只读、secret-free descriptor，经 `resolveFactoryModule → WorkerManager → worker-entry` 进入可执行 `b13-worker-factory.ts`；缺少 deps module、provenance 或完整 `KernelDeps` 均 fail closed。
- 本轮补齐 `b13-host-ipc.ts`、`b13-host-kernel-deps.ts` 与 Python `b13_host_ipc.py`：worker 只持有 `MessagePort` 与 value envelope，Python 侧保留 B05M `AgentModelGateway`、B06P `CapabilityHostRegistry/receipt`、B11 session/checkpoint port；credential handle 只在 host resolver 内使用，不进入 IPC payload。
- 未恢复任何 HTTP/WebSocket fallback。stale bridge/cancel tests 改为验证 `EmbeddedTaskStreamBridge` 与当前 local terminal/outbox 语义。
- `desktop/electron/tests/packaged-fused-worker-bridge.test.cjs` → `2 passed`：验证 packaged main 的 FD/nonce/worker lifecycle 调用链，并验证缺失 B12R resource manifest 时实际 bridge 构造 fail closed；不把静态字串或 deterministic transport 当作安装态 fused PASS。

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
| B13 Python host adapter | `PYTHONPATH=/tmp/echodesk-b13-platforms/llm/src /opt/homebrew/opt/python@3.11/bin/python3.11 -m pytest -q backend/tests/unit/agent_runtime/test_b13_host_ipc.py` → `1 passed`；覆盖 camelCase worker envelope→B05M request、B06P receipt、B11 session event |
| B13 Electron fused host IPC | `node --experimental-strip-types --test desktop/electron/agent-runtime/test/b13-fused-host-ipc.test.ts` → `1 passed`；覆盖 worker-local factory、model tool-call、receipt、durable event/checkpoint、restart/resume identity |
| B13 incremental TypeScript | `desktop/node_modules/.bin/tsc --project desktop/agent-kernel/tsconfig.json --noEmit` 与 `desktop/node_modules/.bin/tsc --project desktop/electron/agent-runtime/tsconfig.json --noEmit` → pass |

既有结果证明了 source-level persistence identity、grant/receipt/provenance、embedded cancel/crash/fail-closed focused contracts，以及 WorkerManager worker crash/restart/identity contracts；本轮新增的 Python host adapter gate 与 Electron fused host IPC gate 才共同闭合了 B05M/B06P/B11 到 worker-local KernelDeps 的 source-level production seam。

## 真实 provider/tool 证据

命令：

```text
PYTHONPATH=/tmp/echodesk-b13-platforms/llm/src \
/opt/homebrew/opt/python@3.11/bin/python3.11 \
-m app.runtime.b13_model_tool_provider --task-id b13-live-smoke
```

结果：`status=PASS code=PROVIDER_STREAM_OK model_events=7 EXIT_CODE:0`。transport 日志只显示非秘密 endpoint/protocol 元数据；没有输出或复制 credential secret。该 smoke 使用现有 Settings/config credential source，经 B05M `AgentModelGateway` 和固定 SHA 的 yoli SSE transport；不是 deterministic test transport。

B06P controlled tool path 在 focused source test 中读取 task-owned 临时文件并产生成功 receipt，覆盖 grant binding、toolUseId、revision 和 receipt result；该路径不是 provider 稳定性测试。

## 本轮闭合证据与边界

1. `b13-fused-host-ipc.test.ts` 是真实 EchoAgentKernel/WorkerManager/worker-local KernelDeps 的 deterministic fused turn；它使用同一 B13 host IPC contract 的 bounded parent handler，不能被表述为 live provider 成功。
2. `test_b13_host_ipc.py` 直接实例化真实 B05M `AgentModelGateway` 与 B06P `CapabilityHostRegistry`，验证 Python authority 对 worker camelCase envelope 的实际适配；生产 `create_b13_runtime_composition(..., provider_factory=...)` 只接受显式 config-store/credential-resolver factory，缺省时保持 `B13_HOST_IPC_UNBOUND` fail closed。
3. `backend/tests/unit/model_runtime/fixtures/fixture_manifest.json` 已按实际 `openai_error.jsonl` 更新为 `d82eee...6c8cd4`，model-runtime 增量 gate 已 `38 passed`。
4. stale `EchoTaskStreamBridge`/WebSocket 夹具已改为 `EmbeddedTaskStreamBridge` typed events；cancel race 断言已按当前 embedded local terminal/outbox 语义收口，增量 gate `16 passed`。
5. FactStore health check 返回 exit 2：`echo/_state/events` 不存在；本任务冻结写集未越界修复该治理目录。

## 禁止项审计

未运行 package build、签名、公证、staple、Authenticode、NSIS、安装态、跨平台验证、provider 稳定性/吞吐/压测、长实验；未新增 Claude CLI/daemon/runtime install/PATH/HOME/global auth fallback；未 push、未建 PR。

## Verdict

`SOURCE_INTEGRATION_READY`：B10→B11→B12 本地整合、B11 persistence identity、真实 Python B05M/B06P host adapter、同协议 Electron WorkerManager→worker-local `KernelDeps` fused turn、receipt/durable checkpoint/restart-resume、embedded/cancel 增量 gates、固定 transport 的 bounded live provider smoke、三套 TypeScript `--noEmit` 与本轮 packaged-main executable wiring gate 均有 source 证据。当前 packaged resource manifest/factory artifacts 仍需 B12R 对新 candidate SHA rebind；在该 rebind 之前不得声称安装态 fused PASS，也不得复用 `78616ca...` artifact。仍不包含 package/sign/notarize/install/cross-platform acceptance；不得把 deterministic fused turn 当作 provider stability 或 live provider fused PASS。

本轮 B14R 窄返工的最终 verdict 仍为 `SOURCE_INTEGRATION_READY`；它表示 source/runtime fused-worker wiring 修复已完成。后续 B12R current-SHA rebind 与 B14R/B15R 安装态重建是强制前置条件，不能把旧 `78616ca6f4f3212a2dd35801ac797f5dba76b55e` artifact 当作修复后资产。
