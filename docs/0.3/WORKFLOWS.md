# EchoDesk 0.3 核心 Workflow

日期：2026-07-09  
状态：当前实现映射；历史步骤已按 0.3.1 运行时校准

## 1. Workflow 总览

0.3 的核心用户路径：

```text
Capture -> Meeting -> Minutes -> Todo -> Artifact -> Agent -> Share -> Diagnostics
```

每条 durable command 都必须满足：

- 有 `workflow_run` 和可 replay 的 `workflow_events`。
- 有明确 owner。
- 有 Happy Path、Sad Path、Boundary Path。
- UI 只展示投影，不作为事实源。

Capture 上传与 Chat/RAG ask 等无副作用读流绑定请求或连接生命周期，不伪造 durable run；只有产生持久副作用的命令进入 Workflow Kernel。

## 2. 会议采集 Workflow

入口：

- App 启动持续采集。
- 手动开始会议。
- 自动会议检测。

Happy Path：

1. 前端持续上传音频 chunk。
2. 后端完成 gate、STT、diarizer。
3. 自动检测进入会议。
4. `/ws/echo` 推送 `meeting.started`、`meeting.segment`。
5. 用户在 UI 看到实时转写。

Sad Path：

- 麦克风无权限：UI 显示可操作修复入口。
- STT 熔断：capture 状态显示原因，停止无意义上传。
- diarizer 失败：转写仍保留，speaker 可为空。
- 自动会议误判：用户可手动结束或忽略。

Boundary Path：

- 单人长时间说话。
- 多人短句插话。
- 会议跨进程重启。
- 会议超过最大时长。

0.3 要求：

- 会议状态 owner 是后端。
- capture 不创建 workflow run；会议 finalize 才创建 `meeting_minutes` run。
- 自动/手动会议状态必须可 hydrate。

## 3. 纪要生成 Workflow

入口：

- 会议自动结束。
- 用户手动结束会议。
- 用户点击重试纪要。

Run：

```text
kind = meeting_minutes
origin = meeting_end | manual | retry
meeting_id = required
```

Happy Path：

1. 创建 `workflow_run(kind=meeting_minutes)`。
2. 写入 `minutes.generation.started`。
3. 调 LLM 生成结构化纪要。
4. 校验 JSON schema。
5. 写入 meetings minutes。
6. 写入 `minutes.ready` 和 run succeeded。

Sad Path：

- LLM timeout：run timeout，UI 显示重试。
- JSON 校验失败：run failed，保存错误摘要。
- meeting 不存在：不创建 run，返回 404。
- transcript 为空：run failed，错误原因可见。

Boundary Path：

- ended 但 minutes 空的历史会议。
- finalize 重复点击。
- 同一会议并发重试。

0.3 要求：

- `recover_stuck_minutes` 改为基于 workflow run 恢复。
- 重试创建新 run，保留旧 run 失败记录。

## 4. Todo 执行 Workflow

入口：

- `MinutesView` 中点击 Todo 的执行按钮。
- CommandBar 预填来自 Todo 的建议命令。

Run：

```text
kind = artifact.generate | agent_task
origin = todo
meeting_id = required
todo_id = required
```

Happy Path：

1. 用户点击 Todo。
2. 后端创建 workflow run。
3. 根据 intent 决定走 Artifact 或 Agent。
4. 成功后创建 artifact。
5. 写入 `artifact_links(meeting_id, todo_id, run_id)`。
6. UI 投影 Todo 为 done，并显示产物。

Sad Path：

- artifact 生成失败：Todo 状态显示 failed，可 retry。
- Agent 需要授权：Todo 显示 waiting_permission。
- 会议纪要 JSON 旧结构没有 todo id：回退为不可执行项。

Boundary Path：

- 同一 Todo 被重复点击。
- 同一 Todo 已有 running run。
- 旧会议没有 todo schema。

0.3 要求：

- Todo 执行状态不再只靠 `minutes_json`。
- `minutes_json` 保留文本内容，执行态由 workflow/link 投影。

## 5. Artifact Workflow

入口：

- CommandBar `@生成`。
- Todo 执行。
- Agent 任务产物归档。

Run：

```text
kind = artifact.generate
origin = command | todo | agent
```

Happy Path：

1. 创建 run。
2. 写入 `artifact.generating`。
3. Skill runner 生成文件。
4. 写入 artifact metadata。
5. 写入 artifact link。
6. 写入 `artifact.ready` 和 run succeeded。

Sad Path：

- LLM 失败：run failed，失败卡片可 retry。
- 代码执行失败：run failed，保存错误摘要。
- 产物文件写入失败：run failed，不产生 artifact metadata。
- Artifact metadata、来源 link 与 terminal run/event 任一写入失败：同一 Unit of Work 整体回滚，不能留下“文件成功但来源丢失”的假成功。

Boundary Path：

- 大文件产物。
- 同一 brief 重复生成。
- 产物文件存在但 metadata 缺失。

0.3 要求：

- 失败卡片必须接真实 retry。
- 所有 artifact 都能通过 DB 找到来源。
- `GET /meetings/{id}/artifacts` 不再返回固定空数组。

## 6. Knowledge / RAG Workflow

入口：

- 文件拖入。
- workspace 扫描。
- 会议结束后沉淀。
- 用户提问。

Durable Run：

```text
kind = rag.ingest | rag.delete | workspace.scan | meeting projection
origin = upload | workspace | meeting
```

Happy Path：

1. ingest 创建 run。
2. 解析文件。
3. 写入 doc metadata 和 index。
4. `/rag/ask` 作为 cancellable SSE 读流执行，不创建成功 run。
5. 只有 `done` 终帧后 UI 才显示完整答案和 citations。

Sad Path：

- 文件过大：run failed，错误可见。
- 解析失败：doc 不入库，run failed。
- RAG 空命中：明确显示无来源或走 web fallback。
- 删除 doc：索引和 UI 同步。

Boundary Path：

- workspace 文件移动。
- 同名文件更新。
- 大 PDF。
- 中英混合查询。

0.3 要求：

- RAG 问答必须带来源。
- workspace 本地路径 owner 明确。
- 删除/清空必须有破坏性确认。

## 7. Agent Runner Workflow

入口：

- CommandBar 被 intent 判为 `agent_task`。
- Todo 执行被判断为长任务。
- 用户手动 retry Agent task。

Run：

```text
kind = agent_task
origin = command | todo | retry
runner = claude_code | agentos
```

Happy Path：

1. 创建 EchoDesk workflow run。
2. 无 grant 时进入 waiting_permission。
3. 用户授权 full access。
4. EchoDesk 提交到 AgentOS / Claude Code。
5. bridge 订阅 runner WS。
6. raw event 翻译为 EchoDesk workflow event。
7. Agent 产物归档到统一 artifact。
8. run succeeded。

Sad Path：

- AgentOS disabled：run failed，提示 runner 未启用。
- AgentOS submit 失败：run failed。
- WS 断开：bridge 重连，并记录 reconnect event。
- 本地 timeout：run timeout。
- cancel upstream 失败：run cancel_failed。
- 上游完成但产物代理失败：run succeeded，但 artifact import failed event。

Boundary Path：

- 用户授权后关闭 App。
- 任务运行中 backend 重启。
- 同一任务重复授权。
- Agent 生成多个产物。
- Agent 返回未知 event kind。

0.3 要求：

- 权限保持 full access。
- 授权、执行、取消、超时、重试、产物必须可追踪。
- Agent 产物进入统一 artifacts。
- 旧 `/agents/tasks/{id}/artifacts/{path}` 仅保留兼容代理。

## 8. 分享 / 导出 Workflow

入口：

- 会议分享页。
- 下载纪要。
- 导出会议 zip。
- 诊断包导出。

仅持久导出创建 Run：

```text
kind = meeting.export | diagnostics.export
origin = admin | diagnostics
```

Happy Path：

1. 用户选择持久会议导出或诊断包导出；普通分享页准备与已有文件下载不伪造通用 export run。
2. 后端读取 meeting、minutes、artifact_links。
3. 生成分享页或 zip。
4. 记录对应的 `meeting.export` 或 `diagnostics.export` run。
5. UI 显示下载/打开入口。

Sad Path：

- 会议不存在：404。
- artifact 文件丢失：分享页显示缺失项，导出记录 warning。
- 诊断包脱敏失败：阻止导出或强制 mask。

Boundary Path：

- 没有纪要的会议。
- 有纪要但没有 artifact。
- 部分 artifact 缺文件。

0.3 要求：

- 分享页以 DB link 为准。
- 导出必须脱敏。
- LAN share 只开放只读端点。

## 9. 设置 / 诊断 Workflow

入口：

- SettingsPanel。
- StatusBar。
- Admin APIs。

Happy Path：

1. 用户查看 backend、TTS、mic、workspace 状态。
2. 用户导出诊断包。
3. 用户重启 backend 或重新扫描 workspace。

Sad Path：

- 后端不可达：可手动重启。
- TTS 合成静音：`/tts/diag` 真实失败。
- workspace 扫描失败：错误可见。

Boundary Path：

- 无网络。
- 远端模型 key 缺失。
- 用户取消目录授权。

0.3 要求：

- 诊断状态来自真实探针。
- 破坏性操作统一 Modal 确认。
- secret 永远 mask。
