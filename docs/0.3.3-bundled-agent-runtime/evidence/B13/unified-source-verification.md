# B13 Unified Source Verification

## 当前状态

- 日期：2026-07-16（Asia/Shanghai）
- worktree：`/Users/yoligehude/.codex/worktrees/1fd3/echo`
- B10 起点：`8d5bdb6fdaa0b0d8e2be8275f98b4f6f862ccab5`
- B11 输入：`d2fae70a203510ea5ecbee4c8238da41a1608c79`，本地整合 commit：`a5bf3308f58ae9958b27dce9b03f1a5b6d1d2c47`
- B12 输入：`4742d9d4fd42e5bf90e0ba1be7babc4554d438a2`，本地整合 commit：`6f22e75fc5c219ac9bce10635e0ce0fdbdb4ffbb`
- 外部 `_platforms` 固定 transport：`158844db23cc5884889233fb8bdd7d943f3002f9`
- 固定 SHA 的 detached 只读语义 worktree：`/tmp/echodesk-b13-platforms`
- 候选 verdict：`REWORK_REQUIRED`

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
| Electron agent-runtime | `node --experimental-strip-types --test desktop/electron/agent-runtime/test/*.test.ts` → `8 passed` |
| Python quality | `ruff check` on B13 glue/tests and resolved `agentos.py` → pass；`compileall` → pass |

这些结果证明了 source-level persistence identity、grant/receipt/provenance、cancel/crash/fail-closed focused contracts，以及 WorkerManager worker crash/restart/identity contracts；它们不等价于完整 fused production turn 的闭合证明。

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

1. `desktop/node_modules/.bin/tsc` 不存在，agent-kernel 与 agent-runtime `tsc --noEmit` 均为 `BLOCKED_TOOLCHAIN`；未安装依赖。
2. `backend/tests/unit/model_runtime/test_contract_verifier.py::test_fixture_manifest_hashes_and_redaction_are_stable` 失败：现有 fixture hash 与 manifest 不一致（`d82eee...` vs `34a0f7...`）。本 B13 未修改其 fixture。
3. `backend/tests/unit/test_agent_bridge_recovery.py` 有 5 个旧测试仍 monkeypatch `EchoTaskStreamBridge`，而 B11 accepted source 已切换 `EmbeddedTaskStreamBridge`；未恢复旧 HTTP/WebSocket fallback。
4. `backend/tests/unit/test_agent_cancel_outbox.py::test_resume_submit_race_never_revives_cancelled_task` 失败：旧测试期望 B12 HTTP backend cancel 调用；B13 production composition 最终必须使用 inherited-FD embedded backend。
5. 当前 TS `production-factory.ts` 对缺失 `KernelDeps` fail-closed；本次最小接线已让 production composition 按 task 解析 host-owned `resolveFactoryModule` 并交给真实 `WorkerManager`，但仓库中没有可执行的 B05M/B06P worker-local factory module，因此完整 Electron fused turn 仍未闭合。
6. FactStore health check 返回 exit 2：`echo/_state/events` 不存在；本任务冻结写集未越界修复该治理目录。

## 禁止项审计

未运行 package build、签名、公证、staple、Authenticode、NSIS、安装态、跨平台验证、provider 稳定性/吞吐/压测、长实验；未新增 Claude CLI/daemon/runtime install/PATH/HOME/global auth fallback；未 push、未建 PR。

## Verdict

`REWORK_REQUIRED`：B10→B11→B12 本地整合、B11 persistence identity、B06P receipt、固定 transport 的真实 provider smoke 和大部分源码 focused gates 已有证据；但完整 Electron WorkerManager→production KernelDeps→B05M/B06P fused production turn 仍因 host-owned worker factory/toolchain/stale baseline failures 未闭合。不得据此启动 B14/B15。
