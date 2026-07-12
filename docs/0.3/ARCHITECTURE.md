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

public session credential POST 在 principal lookup 前进入独立 body admission：全局与 peer 同时限流，lease 覆盖 route 的 body 解析并在完成/取消时释放。该 pool 与普通 HTTP pre-auth lookup 分开；多-slot 配置下单个 peer 不能占满全部 slot，已有 bearer 的普通业务路由也不受其容量影响。

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
- active key 和 idempotency key 在当前 scope 内生效；retry 使用永久派生 key 并继承 parent `active_key`，与 fresh create 竞争同一个唯一活动位置。
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

retry 不是事务外的“复制 run”：terminal parent 读取、child insert、parent retry event/outbox 与可选 domain marker 在同一个 `BEGIN IMMEDIATE` 内完成。两个 backend 实例同时发起 retry，或 fresh create 与 retry 同时竞争时，只允许一个 active-key winner；loser 不留下孤立 child 或缺 event 的 lineage。

### 4.3 Outbox 与慢消费者

每个 backend 实例以自己的 consumer cursor 读取 committed outbox。失败 row 进入 backoff recovery；scope lane 保证某个 owner 的失败不会阻塞全部 owner；global recovery lease/fence 负责多实例接管。

WebSocket subscriber 队列满时连接被移除并结束 generator，不能留下永久等待任务。客户端重连后的 `server_resync` 触发 REST rehydrate，而不是只靠缺失事件猜状态。

### 4.4 Lease

Workflow Dispatcher 和 Agent bridge 通过 lease + heartbeat + fence 保证同一 durable work 的单一有效执行者。旧 worker 失去 lease 时停止 handler，不能继续写 terminal projection。

## 5. RAG 执行边界

### Write path

ingest、delete、workspace scan、meeting projection 是 durable workflow。SQLite 中的 `rag_documents`、`rag_content_owners`、`bm25_index_state` 和 `bm25_index_documents` 是 owner 与 revision 事实源。

JSON BM25 文件是原子替换的 cache；任意 backend 实例都根据 SQLite revision 加载同一 payload manifest。因此 Query、Ambient 与 Meeting 不再拥有互相不可见的独立索引。

schema 038 为 meeting intent 增加单调 generation，并以 `bm25_document_projection_fences` 持久化每个 owner/doc 的 `index|delete` fence。迟到投影低于当前 generation 时是 no-op；同 generation 的相反意图也不能翻转。查询还会用 meeting 当前 state/generation 过滤缓存，delete pending/failed/deleted 在物理清理成功前就不可检索。

meeting 与 ambient 都持久化 projection state、attempts 与 next retry。repair 先合并所有 due tenant/owner scope，再按时间和上限公平推进；ambient 使用 `ambient-segment:<id>` 作为稳定 operation id，使“BM25 成功、状态提交前崩溃”的重放保持幂等。schema 37 旧 ambient 行先标为 `reconcile_pending`，以 scope、规范化 captured time/text 和可用 audio ref 对账旧 chunk，避免把已索引历史全量重复追加。

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
- `minutes_generation_run_id`/`minutes_generation_cancelled_at` 将生成权和显式取消归属到具体 Workflow run。terminal projector 只有 marker 匹配时才把取消、超时或失败写成 `generation_failed`，并与 run/event/outbox 同事务提交。
- meeting RAG projection 记录 state/error/projected_at/attempt/generation，启动和周期恢复可以补投；clear 先递增 generation，旧 finalize/repair completion 不能写回。
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

### Workspace capability

- `host-backend`：local-first 或显式 self-hosted 服务使用后端授权目录。
- `local-electron`：public Electron 在主进程读取用户选择的本机目录，并通过绑定当前 session 的安全 transport 上传。
- `unavailable`：public 浏览器、Android 与 TV 不提供服务器目录扫描，但保留文件上传和知识库管理。

public Electron transport 只接受精确无凭证 HTTPS origin；backend、vault、renderer expected origin 和 session `backend_origin` 必须相同，3xx 全部拒绝，401 最多 renew 一次，timeout/cancel 覆盖 body drain。registry schema 3 按 origin 分区；同 origin mutation 串行且持有 generation lease，origin 切换取消旧操作，失败 orphan cleanup 留待下次扫描重试。

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

当前本地源码证据：Backend `1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`（`1045 collected`、`18 live deselected`、line coverage `87.46%`，终端显示 `87%`，自然退出），Ruff check / format `250 files` / mypy `128 source files` / compile 通过；Electron `176 / 176`；Desktop E2E `150`；scenarios `29`；public isolation self-test 与双 principal 完整 smoke 通过；release aggregate `28 / 28`、actionlint 和 action pins 通过 [F-ECHO-028]。

Android / TV current exact-SHA phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)`、unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，Capacitor `Hint 2` 单列；debug APK 不可公开发布。npm 两处为 `0` finding；Python runtime/dev/build 各保留同一项受控 `torch` `CVE-2025-3000` 至 2026-08-12，且上游无 `fix_versions`；lint/typecheck/audit-tool 为 `0`，不能宣称 Python 总体 clean 或零漏洞。

current exact-SHA macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 通过；read-only DMG smoke `1 / 1`、安装态完整 workflow `1 / 1`、live `2 / 2` 均通过且 `0 skipped / 0 failed`。安装态覆盖真实下载 `0600`、marker、安全文件名、无 partial、GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart。Developer ID、notary、staple、Gatekeeper 正式链路 external skipped。公共 Release / 生产 / bootstrap 当前分别为 `v0.2.50` / `0.2.49` / `0.2.45`，bootstrap 未声明 `minimum_client_version` [F-ECHO-029]；正式 signed cross-platform、受保护 environment/secret、最终包与公网证据必须绑定最终 exact SHA，不能由本段替代。
