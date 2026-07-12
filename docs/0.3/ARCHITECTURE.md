# EchoDesk 0.3 模块与 UX 架构

版本：0.3.1 | 状态：实现快照 | 更新时间：2026-07-12

总览与事务细节以 [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) 为主；本文说明 0.3 的模块责任、用户流程投影和完成标准。

## 1. 目标

EchoDesk 0.3 解决的不是“再加一个工作流页面”，而是把已有会议、知识、Artifact、Todo、Agent、分享和诊断收束到清晰的执行模型：

- 需要恢复或产生持久副作用：durable Workflow。
- 无副作用、依赖当前连接：cancellable read stream。
- public 数据：server-verified principal scope。
- local host 能力：显式受信任的 Desktop Pro 边界。
- UI：只投影后端事实，并把失败转译为用户可执行的下一步。

## 2. 组件边界

| 模块 | 入口 | 事实 Owner | 主要依赖 |
|---|---|---|---|
| Identity / Session | `/session*` | identity SQLite tables | access policy、credential vault |
| Workflow Kernel | `/workflows/runs*` | `workflow_runs/events/outbox` | Unit of Work、lease store |
| Meeting | `/meetings*`、`/capture*` | meetings/segments/minutes | STT、diarizer、Workflow |
| RAG Write | `/rag/ingest`、delete、scan | SQLite manifest/revision | BM25 cache、Workflow |
| RAG Answer | `/rag/ask` | 当前 SSE 连接 | LLM、RAG、Web ports |
| Artifact / Todo | `/artifacts*`、meeting todo | Artifact + Workflow | skill runner、staging |
| Agent | `/agents*` | `agent_tasks/events` + Workflow projection | runner、lease、bridge |
| Event Projection | `/ws/echo` | committed outbox/event | scoped event bus |
| Desktop UI | React store | 可丢失视图状态 | REST、SSE、WS、IPC |
| Local Capability | Electron main/preload | OS + local config | backend supervisor、vault |

API adapter 可以依赖 application service；use case、port 和 schema 不依赖 FastAPI 或具体外部 SDK。外部 LLM、STT、TTS、Web、Agent 和文件执行都在 adapter 边界。

## 3. Identity 与 Scope

public principal 由服务端根据 bearer/session 验证产生：

```text
(tenant_id, owner_id, device_id, session_id, family_id?)
```

请求 body、query 和 WebSocket hello 中的逻辑 ID 只能作为资源标识，不能提升或替换 principal。HTTP/WS 验证成功后绑定 context；repository、runtime registry、RAG、Workflow、storage 和 event bus 从 context 获取 scope。

隔离覆盖：

- meeting、segments、speaker labels、ambient audio；
- Workflow run/event/outbox；
- Artifact metadata/link/file；
- Agent task/event/grant；
- RAG document/content owner/index payload；
- WebSocket subscriber；
- quota、resource ticket 和 public enrollment。

Desktop local-first 使用固定 local principal；这不是 public 多租户身份，也不应通过网络暴露 host-admin 权限。

## 4. Workflow 执行模型

### 4.1 生命周期

```text
create -> pending -> running -> succeeded
                           \-> failed
                           \-> timeout
                           \-> cancel_requested -> cancelled | cancel_failed
```

规则：

- first terminal wins；冲突的晚到 terminal 仅记录 debug/ignored 信息。
- retry 创建新 run，保留 parent/attempt lineage。
- active key 和 idempotency key 在当前 scope 内生效。
- deadline、cancel request、revision 和 lease fence 都可持久化检查。

### 4.2 Atomic completion

成功/失败收口不允许拆成“先写 domain，之后再补 workflow”。处理器通过 WorkflowService atomic API 在一个 `BEGIN IMMEDIATE` transaction 中：

1. 校验 scope、state、revision 与 lease；
2. 写 domain rows；
3. 更新 run；
4. 追加 events；
5. 写 outbox；
6. commit 后再投影。

Artifact staging、meeting minutes/tombstone、RAG lifecycle 等都遵守这个顺序。

### 4.3 Outbox 与慢消费者

每个 backend 实例以自己的 consumer cursor 读取 committed outbox。失败 row 进入 backoff recovery；scope lane 保证某个 owner 的失败不会阻塞全部 owner；global recovery lease/fence 负责多实例接管。

WebSocket subscriber 队列满时连接被移除并结束 generator，不能留下永久等待任务。客户端重连后的 `server_resync` 触发 REST rehydrate，而不是只靠缺失事件猜状态。

### 4.4 Lease

Workflow Dispatcher 和 Agent bridge 通过 lease + heartbeat + fence 保证同一 durable work 的单一有效执行者。旧 worker 失去 lease 时停止 handler，不能继续写 terminal projection。

## 5. RAG 执行边界

### Write path

ingest、delete、workspace scan、meeting projection 是 durable workflow。SQLite 中的 `rag_documents`、`rag_content_owners`、`bm25_index_state` 和 `bm25_index_documents` 是 owner 与 revision 事实源。

JSON BM25 文件是原子替换的 cache；任意 backend 实例都根据 SQLite revision 加载同一 payload manifest。因此 Query、Ambient 与 Meeting 不再拥有互相不可见的独立索引。

### Read path

`/rag/ask` 直接流式返回 provider delta、citations 和 terminal frame：

- `done`：回答完成；
- `error`：失败，不显示“已回答”；
- disconnect：取消 provider 与 Web fallback；
- 不把已无人接收的回答伪装成跨重启 workflow。

## 6. Meeting 与 Artifact

- 同 tenant/owner 同时最多一个 active meeting。
- capture 的原始流与 meeting finalize 分开；finalize/minutes 才进入 durable completion。
- `minutes_cleared_at` 是用户清除纪要的 tombstone。
- meeting RAG projection 记录 state/error/projected_at，启动恢复可以补投。
- Artifact 文件先在安全 staging 生成，再原子登记 metadata 与 links。
- UI 的 outputs 来自 Artifact/Agent/Workflow 持久投影，不从内存数组推断。

## 7. Agent

`agent_tasks` 是 runner task 的权威记录，关联 `workflow_run_id`；raw event 先进入 `agent_task_events`，stream bridge 再投影 Workflow 和 Artifact。

边界：

- create/cancel/retry 和 grant 需要本机或 host-admin 权限；
- bridge lease 过期、heartbeat 失败或进程重启后可接管；
- Artifact import/proxy 有 path、Content-Length、chunk 和总大小限制；
- Agent terminal 与 linked Workflow terminal/event/outbox 在同一 Unit of Work 内仲裁；read barrier 与 recovery 仅作为崩溃恢复护栏。

## 8. UI/UX 架构

### 三个稳定区域

- **Session Navigation**：实时记录入口、会议搜索、历史会议。
- **Workbench**：转写 / 助手互斥切换；CommandBar 固定属于当前工作上下文。
- **Inspector**：会议纪要 / 工作产物互斥切换；Todo、Agent 和失败重试归入产物视图。

### 视觉与内容规则

- 全局系统字体：`-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif`。
- 一套线性图标；Emoji 和文本符号不承担核心语义。
- 青绿表示选中/连接，琥珀表示可恢复降级，红色只表示失败或危险操作。
- 状态使用“标题 + 说明 + 下一步”，不直接展示异常类名。
- 输入框在 1–6 行内增长；转写正文自然换行；标题和元数据按容器省略。
- icon-only 操作必须有 accessible name 和 tooltip。

### 响应式

- 宽屏：Navigation、Workbench、Inspector 并列。
- 960px：Inspector 使用可关闭 drawer，并恢复触发按钮焦点。
- 411px：纵向组织，输入与主要操作保持可达。
- TV：转写优先双栏，使用远距离可读尺寸。

## 9. 运行与发布模式

| 模式 | 默认行为 |
|---|---|
| Electron packaged | 启动 bundled backend，写本机数据 |
| Electron public | 仅显式 `ECHO_PUBLIC_DEMO=1`，不启动本机 backend |
| Android / TV | 连接配置的 HTTPS backend |
| Dev Vite | 通过配置 proxy 到本机或测试 backend |

桌面包必须包含对应平台 backend binary。Android / TV 公开资产必须使用稳定 release 身份签名并校验；Windows 在 Authenticode 未配置前拒绝 public publish；macOS ad-hoc 只用于本机测试。

## 10. Agent 一致性门禁

1. Agent HTTP 终态读取经过 Workflow read barrier；任务详情、列表与事件快照只在关联 Workflow 修复并核对为相同终态后返回。
2. Agent submit 使用 tenant/owner/task 派生的 opaque key、heartbeat submit lease 和 fenced CAS；AgentOS durable UNIQUE reservation 精确复用同一 runner，响应丢失不依赖任务列表扫描，也不把 key 写进 prompt。
3. migration 036 的 `agent_command_outbox` 与 Agent/Workflow `cancel_requested` 同事务提交；恢复 worker 使用 fenced lease 和稳定 `Idempotency-Key`。晚到 runner id 触发 `force_remote` cancel，不复活本地状态。
4. Agent terminal、linked Workflow terminal/event/outbox，以及 cancel command completion 在同一个 Unit of Work 内仲裁；已有 Workflow terminal 胜出时迟到 Agent 终态降为 debug audit event。

## 11. 完成标准与证据

- tenant/owner 隔离覆盖 HTTP、WS、DB、RAG 和文件边界。
- domain write、run/event 与 outbox 同事务。
- 多实例执行由 lease/fence 保护。
- RAG 使用 SQLite revision/manifest 单一提交点。
- Desktop 不混排转写、助手、纪要与产物。
- 失败路径有反例测试和用户可执行恢复入口。

当前证据：Backend `898 passed / 0 skipped`（`916 collected`、`18 live deselected`、coverage `87%`、自然退出）；GLM live `2 / 2`；Electron `70`；Desktop E2E `95`；scenarios `29`；真实安装态 workflow `1 / 1`；packaged smoke 通过。
