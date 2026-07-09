# EchoDesk 0.3 测试与验收计划

日期：2026-07-09  
状态：开发前测试设计  

## 1. 测试目标

0.3 测试目标不是证明接口返回 200，而是证明用户 workflow 可完成：

- 会议可生成纪要。
- Todo 可执行并生成产物。
- 产物可跨重启恢复。
- Claude Code Agent 可授权、执行、取消、超时、重试、归档。
- 分享导出能带上正确产物。
- contract gates 能阻止再次补丁化。

## 2. 测试分层

| 层级 | 范围 | 工具 |
|---|---|---|
| Unit | 状态机、repository、schema、adapter 翻译 | `pytest` / Vitest |
| Integration | DB migration、WorkflowService、Agent bridge、artifact link | `pytest` |
| Contract | REST route、WS event、IPC key | snapshot tests |
| E2E | CommandBar、outputs、Todo、meeting share | Playwright |
| Release Smoke | packaged app、TV、installer | existing scripts |

## 3. Workflow Core 测试

Unit：

- `create_run` 创建 pending run。
- `record_event` seq 单调递增。
- raw hash 去重。
- `complete_run` 只能从 pending/running/cancel_requested 合法迁移。
- terminal run 不能继续写非 replay event。
- retry 创建新 run，保留 parent reference。

Integration：

- migration 后旧 DB 可启动。
- 重启后 `restore_unfinished` 找到 pending/running/cancel_requested。
- REST `/workflows/runs/{id}/events?after_seq=N` 正确返回。

Sad Path：

- 无效 state transition 抛错。
- event payload 非 JSON 可序列化时报错。
- DB 写入失败不吞错。

## 4. Artifact 测试

Unit：

- artifact metadata 保存。
- artifact link 创建。
- meeting artifacts 从 `artifact_links` 查询。
- 文件路径必须在允许目录。

Integration：

- `/artifacts/generate` 创建 run。
- 成功生成 artifact 后 DB 有 artifact + link。
- `GET /meetings/{id}/artifacts` 返回真实数据。
- `DELETE /meetings/{id}/outputs` 只删除本会议 link 指向产物。

Sad Path：

- skill runner 失败生成 run failed。
- artifact 文件缺失时 download 返回 404。
- metadata 存在但文件缺失时分享页显示缺失。

Boundary：

- 同一 artifact 多个 link。
- 同一会议多个产物。
- 旧 artifact 没 meeting link。

## 5. Todo Workflow 测试

Unit：

- todo execution 创建 workflow run。
- todo status 从 run state 投影。
- old minutes JSON 无 todo id 时不崩。

E2E：

1. 会议有 Todo。
2. 用户点击执行。
3. UI 显示执行中。
4. artifact 生成成功。
5. Todo 显示完成并带下载入口。
6. 刷新/重启后状态仍存在。

Sad Path：

- artifact failed 后 Todo 显示失败。
- 点击重试创建新 run。
- 同一 Todo 已 running 时再次点击不会创建重复 run。

## 6. Agent Runner 测试

Unit：

- 无 grant 时 run 进入 waiting_permission。
- 授权后提交 AgentOS。
- AgentOS submit 失败 -> run failed。
- ClaudeCodeRunnerAdapter 翻译 text/tool/artifact/result。
- unknown raw event 只生成 debug event。
- cancel_requested / cancelled / cancel_failed 状态迁移。

Integration：

- Mock AgentOS HTTP submit。
- Mock AgentOS WS event stream。
- bridge 断线重连。
- terminal event 后 bridge 停止。
- upstream artifact 被导入统一 artifacts。

E2E：

1. CommandBar 输入长任务。
2. intent 返回 agent_task。
3. outputs 出现 AgentTask 卡片。
4. 用户点击允许并开始。
5. 卡片显示 running。
6. Mock event 推送产物。
7. 卡片显示完成，产物进入产物区。

Sad Path：

- `agent_os_enabled=false` 时用户看到 runner 未启用。
- AgentOS 断开时任务不假成功。
- 用户取消时先显示 cancel_requested。
- cancel 上游失败时显示 cancel_failed。
- timeout 后可 retry。

Boundary：

- backend 重启恢复 pending/running Agent run。
- 同一任务重复授权。
- Agent 生成多个文件。
- Agent 生成路径含空格/中文。

## 7. RAG / Workspace 测试

Integration：

- 文件 upload ingest 创建 run。
- workspace scan 创建 run 或记录 scan result。
- 删除 doc 后 UI 刷新。
- RAG answer 返回 citations。

Sad Path：

- 文件过大。
- PDF 解析失败。
- workspace path 权限失败。
- web fallback 不可用。

Boundary：

- 文件改名。
- 文件删除。
- 同名不同路径。
- 中英混合查询。

## 8. Share / Export 测试

Integration：

- 分享页列出 meeting minutes。
- 分享页列出 linked artifacts。
- 导出 zip 包含 minutes、transcript、artifacts。
- 诊断包 mask secret。

Sad Path：

- artifact link 存在但文件丢失。
- meeting 没有 minutes。
- zip 写入失败。

Boundary：

- 产物很多。
- 文件名含中文。
- LAN safe endpoint 访问。

## 9. Contract Tests

REST route snapshot：

```bash
pytest backend/tests/contracts/test_routes_snapshot.py
```

WS event snapshot：

```bash
pytest backend/tests/contracts/test_ws_events_snapshot.py
```

IPC snapshot：

```bash
cd desktop
npm run test:ipc-contract
```

Script matrix：

```bash
pytest backend/tests/contracts/test_script_matrix.py
```

## 10. PR 必跑命令

基础：

```bash
cd desktop
npm run typecheck
npm run build

cd ../backend
pytest
```

0.3 专项：

```bash
cd backend
pytest tests/unit/test_workflow_service.py
pytest tests/unit/test_agent_task_service.py
pytest tests/integration/test_echo_task_stream_bridge.py
pytest tests/integration/test_artifact_links.py
```

前端专项：

```bash
cd desktop
npm run e2e -- --grep "workflow|artifact|agent|todo"
```

## 11. 业务验收清单

进入 0.3 beta 前必须人工确认：

- 结束会议后纪要生成失败能看见原因并重试。
- Todo 执行失败能重试。
- 会议产物重启后仍能显示。
- Agent 任务能授权、执行、取消、重试。
- Agent 产物和普通产物展示一致。
- 分享页包含会议相关产物。
- 诊断包不泄露 secret。
- 断网/远端服务挂掉时 UI 不假绿。
