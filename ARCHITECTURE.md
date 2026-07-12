# EchoDesk v0.3.1 架构

状态：实现快照 | 更新时间：2026-07-12 | 服务端口：`8769`

## 1. 架构目标

EchoDesk 把会议、知识、任务、产物和 Agent 长任务组织为一个本地优先、可恢复、按 principal 隔离的 workflow 系统。

核心约束：

1. Desktop Pro 默认使用本机 backend 和 SQLite；public demo 必须显式启用。
2. public 请求只能使用服务端验证后的 principal，客户端 body/query 不能自行指定授权 scope。
3. 有持久副作用或需要跨重启恢复的流程进入 Workflow Kernel；无副作用的实时读流随连接取消。
4. domain write、workflow state/event 和 outbox message 在同一个 SQLite transaction 中提交。
5. Artifact、Meeting、RAG、Agent 与 WebSocket 都按 tenant / owner scope 读写。
6. UI 是后端事实的投影，不承担业务事实源。

## 2. 运行拓扑

```text
Electron / Android / TV / Public Web
          │
          │ HTTPS / REST / SSE / WebSocket
          ▼
FastAPI transport boundary
  ├─ session / principal validation
  ├─ request context binding
  ├─ admin / host capability policy
  └─ route-specific input validation
          │
          ▼
Application layer
  ├─ WorkflowDispatcher + WorkflowService
  ├─ Meeting / Capture use cases
  ├─ Artifact + Todo workflows
  ├─ AgentTaskService + stream bridge
  └─ RAG ingest/delete + cancellable answer stream
          │
          ▼
SQLite WAL + scoped storage
  ├─ domain rows
  ├─ workflow run/event/outbox
  ├─ execution leases
  ├─ RAG manifest/revision
  └─ identity/session/quota records
          │
          ▼
Adapters
  ├─ OpenAI-compatible LLM
  ├─ STT / TTS / diarizer
  ├─ BM25 / web search
  ├─ Node/Python artifact workers
  └─ Claude Code / AgentOS runner
```

## 3. 运行模式

| 模式 | 选择方式 | Backend | 身份与存储 |
|---|---|---|---|
| Desktop Pro | 默认 | 安装包内 backend binary | 固定 local principal；`~/.echodesk/` |
| Public demo | `ECHO_PUBLIC_DEMO=1` | 配置的 HTTPS backend | server-issued session；tenant/owner scope |
| 强制本机 | `ECHO_FORCE_LOCAL_BACKEND=1` | 本机 backend | 优先级高于 public 开关 |
| Android / TV | 客户端配置 | HTTPS backend | 设备凭证 + session family |

Electron 的最终模式由 main process 计算，并通过 preload 暴露给 renderer。renderer 不能仅凭 URL 猜测 public/local。Desktop Pro 会监督本机 backend 的启动、健康和退出；public 模式不启动本机 backend。About、Settings 与健康 hook 共用这一权威 capability：public renderer 不请求 `/healthz/full`、`/admin/data-dir` 或远端服务配置，也不把预期的隔离拒绝显示成“本地服务不可达”。

## 4. Principal 与身份连续性

服务端授权主体：

```text
Principal = tenant_id + owner_id(user_id) + device_id + session_id + mode
```

- local-first 使用固定的 `legacy-local` scope 和 `local-fixed` session，只存在于本机信任边界。
- public 首次 enroll 后创建 tenant、user、device、session family 和 device credential。
- session token 只在签发时返回明文；服务端保存 hash。
- renew、credential rotation、additional-device enrollment 和 revoke 都由服务端校验当前身份。
- public HTTP 的 session/业务请求必须携带 `X-EchoDesk-Client-Version`，WebSocket `client_hello.client_version` 必填；缺失、非法或低于 `/bootstrap.minimum_client_version` 时分别以 HTTP `426` / WS `4426` fail closed，客户端进入升级态并停止重试。该自报版本只用于兼容性门禁，不是身份或授权凭据。
- `/session`、`/session/enroll`、`/session/renew` 与 credential rotation 的 request body 使用独立 pre-auth admission pool。lease 从 principal 解析前持有到 route 完成 body 解析/响应；pool 有多个 slot 时，同一 peer 不能全部占满，普通已认证业务路由也不进入这个 pool。
- Electron 身份 IPC 的成功响应必须携带主进程 credential vault 的 `backend_origin`；renderer 仅在它与当前请求 origin 完全一致时接纳 bearer。后端地址变化会关闭并代际废弃旧 WS、清 owner-scoped UI/cursor，再建立新 scope，旧 socket 的迟到 4426 不能污染新 origin。
- 401 表示凭证失效或身份丢失，409 表示身份冲突；客户端不能静默换 owner 继续执行。
- HTTP 和 WebSocket 在 transport boundary 验证后把 principal 绑定到 context；repository、RAG、Workflow 和 storage 从 context 读取 scope。

主要持久表：`tenants`、`users`、`devices`、`session_families`、`device_credentials`、`principal_sessions`、`public_enrollments`、`public_enrollment_admissions`、`resource_tickets`、`principal_quota_ledger`。

资源主键和查询使用 `(tenant_id, owner_id, logical_id)` 复合边界。磁盘目录使用 tenant/owner 派生的 opaque scope key，避免把可控 ID 直接拼接为物理路径。

## 5. Workflow Kernel

### 5.1 适用范围

进入 durable workflow 的流程包括：

- meeting finalize / minutes；
- artifact generate 与 Todo 执行；
- RAG ingest、delete、workspace scan 与 meeting projection；
- Agent task、retry、cancel 与 artifact import；
- 其它需要幂等、恢复或持久副作用的命令。

`/rag/ask` 是无持久副作用的 SSE 读流：只在收到 `done` 终帧后算成功，客户端断开时取消上游，不伪装为可恢复长任务。音频 capture 是流式输入；产生持久业务结果的 finalize/projection 再进入 durable 边界。

### 5.2 状态机

```text
pending -> running -> succeeded
                   -> failed
                   -> timeout
                   -> cancel_requested -> cancelled
                                       -> cancel_failed
```

- 终态 first-terminal-wins；晚到的冲突终态不会覆盖既有结果。
- retry 创建新 run，通过 `parent_run_id` / attempt lineage 关联旧 run；永久 retry idempotency key 与继承的 `active_key` 共同仲裁 retry-vs-retry 和 fresh-create-vs-retry 并发。
- `idempotency_key` 与 `active_key` 阻止同一 scope 的重复活动执行。
- `revision` 保护并发状态更新；`deadline_at` 和 `cancel_requested_at` 保留恢复所需信息。

### 5.3 Unit of Work

`WorkflowService` 是 run、event 和 outbox 的统一写入口。需要同时修改 domain 与 workflow 的处理器使用 atomic API：

```text
BEGIN IMMEDIATE
  1. 校验 principal scope、revision、lease fence、当前状态
  2. 执行 domain_writer
  3. 更新 workflow_runs
  4. 追加 workflow_events
  5. 写 workflow_outbox
COMMIT
```

任一步失败则整体 rollback。Artifact metadata/link、minutes tombstone、RAG lifecycle 等不能先独立成功再补写 run/event。

retry 自身也是一个 Unit of Work：`BEGIN IMMEDIATE` 串行化 terminal parent 读取与 active-key 竞争，child run、parent `workflow.retry_created` event、对应 outbox 和可选 domain marker 一次提交。若 fresh create 已赢得同 scope 的 `active_key`，retry 返回冲突而不会产生第二个活动 run 或半条 lineage。

### 5.4 Transactional outbox

`workflow_outbox` 保存已提交、待投影的消息。每个 backend consumer 有独立 cursor/heartbeat；scope recovery lane 和 global recovery lease 负责慢消费者、崩溃与多实例补投。

- 消息只在 database commit 后发布。
- consumer 失败不会回滚已经提交的业务事实，而是进入带 backoff 的 recovery。
- global recovery 使用 lease + fence，避免多个实例同时推进同一恢复游标。
- WebSocket 慢消费者不能无限占用内存；连接被明确断开后，客户端收到 `server_resync` 语义并通过 REST 重新 hydrate。
- 全局 scope stream 满载时，新 distinct principal scope 进入有界 FIFO admission；每个 scope 最多一个候补，释放 subscriber 后按序授予保留 slot。队列满、重复候补或等待超时继续关闭为 `4429`，不会形成无界等待或绕过 principal WebSocket lease。

### 5.5 Execution lease

`execution_leases` 为每个 durable run 提供 holder、expiry 和 fence token。Dispatcher 只有成功 claim lease 后才能执行 handler；heartbeat 丢失或 fence 变化时停止旧 handler，避免双实例继续投影。

### 5.6 Agent 取消一致性

Agent 提交使用 tenant/owner/task 派生的 opaque operation key；`agent_submit` lease 持续 heartbeat，并在同一事务内用 fence + CAS 绑定 runner id。AgentOS 以 durable UNIQUE reservation、payload fingerprint 和确定性 task id 实现并发与重启重放，operation key 不进入 prompt 或 UI。响应丢失时 EchoDesk 重放同一个 POST，不扫描有限历史窗口。

Agent 取消不再把远程调用夹在两个独立提交之间。migration 036 的 `agent_command_outbox` 与 Agent `cancel_requested`、Workflow cancel state/event/outbox 在同一个 `BEGIN IMMEDIATE` 中提交；恢复 worker 通过 `execution_leases` 竞争 fenced command lease，并用稳定 `Idempotency-Key` 重放远程取消。晚到的 submit 结果只补 runner id 并以 `force_remote` 重开 cancel command，绝不把本地终态复活为 pending。

Agent terminal write 会在同一个 SQLite Unit of Work 中先仲裁并写入 linked Workflow terminal；已有 Workflow terminal 胜出时，迟到 Agent event 转成 debug `task.terminal_ignored`，Agent 行收敛到同一终态。cancel worker 还会在该事务内校验 command fence 并完成 outbox。HTTP read barrier 保留为恢复护栏；正常写路径不再制造可见的双终态窗口。

## 6. RAG 单一事实源

旧实现中 Query、Ambient、Meeting 各自持有独立 BM25 内存实例。v0.3.1 改为：

- `rag_documents`：owner-scoped 文档 manifest；
- `rag_content_owners`：content hash 的逻辑 owner、quota 和 lifecycle；
- `bm25_index_state`：物理 index 的单调 revision；
- `bm25_index_documents`：authoritative payload manifest；
- JSON index：可原子替换、可从 SQLite manifest 重建的 cache。

SQLite 是跨进程 commit point。每个实例比较 revision 并加载同一 manifest，查询不再依赖某个先启动的内存实例是否看见其他实例的增量。上传、删除、workspace scan 和 meeting projection 都经过 owner scope 与 workflow lifecycle。

meeting projection 额外携带单调 `rag_projection_generation`；BM25 的 `bm25_document_projection_fences` 持久记录每个 scope/doc 的 generation 与 `index|delete` 意图，迟到 index/delete 不能覆盖新 generation。查询会把缓存 meeting 文档与 SQLite 当前 generation/state 对账，`delete_pending`、`delete_failed`、`deleted` 立即不可见，即使物理 cache 删除失败也 fail closed。

meeting 与 ambient 的 pending/failed 投影都保存 attempts 和 next-retry 时间。启动及周期 repair 按 tenant/owner 合并 due scope、有界取数并退避重试；ambient 使用 `ambient-segment:<id>` 稳定 operation id，成功写入后崩溃重放也不会重复追加内容。schema 37 的旧 ambient 行先进入 `reconcile_pending`，按 owner scope、规范化时间与正文（以及仍存在时的 audio ref）核对旧 BM25 chunk；已有投影只确认状态，crash-gap 才补写稳定 operation。

## 7. Meeting、Artifact 与 Agent

### Meeting

- `meetings`、segments、speaker labels、ambient audio 都使用复合 scope key。
- 同一 owner 只允许一个 active meeting。
- 清空纪要写 `minutes_cleared_at` tombstone；恢复流程不能重新生成用户已清除的纪要。
- `minutes_generation_run_id` 标记当前纪要生成 owner。只有持有该 marker 的 run 能写 minutes 或清理 marker；取消、超时和失败通过 Workflow terminal projector 与 run/event/outbox 同一事务投影为 `generation_failed`，不会永久停在 generating。
- meeting RAG projection 有显式 state/error/projected_at/attempt/generation，可在重启后按退避修复；显式清除先递增 generation 再投影 delete，阻止旧 finalize/repair 把内容写回。

### Artifact

- `artifacts` 保存 metadata，`artifact_links` 保存 meeting/todo/run 来源。
- 文件先写受控 staging，再在 Workflow Unit of Work 中登记和链接。
- public 下载必须命中 owner-scoped metadata；只凭可猜目录名的 legacy fallback 仅限 local-first。
- Agent artifact 使用有大小上限的 streaming import/proxy，并拒绝路径逃逸。

### Agent

- `agent_tasks` 是 runner 任务的权威记录，`workflow_run_id` 将其映射到统一 workflow。
- raw runner event 先持久化到 `agent_task_events`，stream bridge 再做可恢复投影。
- Agent bridge 使用 lease、heartbeat 和 backoff 自动接管过期任务。
- Full Access 是本机用户显式授权的能力；public 普通 principal 不能创建 host-level Agent task 或 grant。

## 8. 客户端投影

React store 只保存当前视图和后端快照：

- REST 用于 bootstrap、列表与 resync；
- WebSocket 用于已提交事件；
- SSE 用于 Chat/RAG 无副作用读流；
- Electron IPC 用于本机 backend、workspace、artifact、update、mic 和 credential vault。

public Electron 的 workspace IPC 不直接复用 renderer fetch：主进程 transport 只接受无凭证的精确 HTTPS origin，要求 backend、credential vault、renderer expected origin 与 session `backend_origin` 四者一致，附加 client version + bearer，只允许一次 401 renew，拒绝全部 3xx，并让 timeout/cancel 覆盖响应体读取。workspace registry schema 3 按 origin 分区；每个 origin 的 mutation 串行化并持有代际 lease，切换 origin 会取消旧操作，失败的 orphan doc 删除保留到后续扫描继续回收。

UI 采用 Session Navigation、Workbench、Inspector 三层结构。转写、助手、纪要和产物互不混排；内部 ID、原始异常和底层事件不直接暴露给普通用户。

## 9. 发布边界

- macOS / Windows / Linux 桌面包必须携带对应平台 backend binary。
- Android / TV 公开资产必须使用稳定 release 身份签名并校验产物；开发签名不能发布。
- Windows 在 Authenticode 未配置前拒绝 public publish。
- macOS ad-hoc 签名只用于本机测试，不等同于 Developer ID 与 notarization。
- public backend 切流必须在隔离 smoke、客户端兼容和回滚验证之后完成。

## 10. 已知非阻断 P2

这些边界没有被伪装为已解决：

1. **SQLite 元数据尚无统一生命周期预算**：RAG blob、ambient WAV 与 transcript inject 已计量，但 meeting、workflow 与 event 等长期元数据仍依赖运维 retention。

## 11. 架构门禁证据

- Backend：`1045 collected`，`18 live deselected`，确定性 `1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`，line coverage `87.46%`（终端显示 `87%`），进程自然退出；Ruff check、Ruff format `250 files`、mypy `128 source files` 与 compile 通过。
- Electron contracts：`177 / 177 passed`；Desktop E2E：`150 passed`；scenarios：`29 passed`。
- public isolation self-test 与双 principal 完整 smoke 通过；release aggregate `31 / 31 passed`，actionlint 与 action pins 通过。
- Android / TV current exact-SHA phone/TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint `Fatal 0 / Error 0 / Warning 0`，Capacitor `Hint 2` 单列。debug APK 不可公开发布。
- npm 两处为 `0` finding；Python six locks 均有效，runtime/dev/build 各有同一项上游无 `fix_versions` 的 `torch` `CVE-2025-3000`，受控例外至 2026-08-12，lint/typecheck/audit-tool 为 `0`。不得把该非零 Python 审计写成 clean 或零漏洞。

以上 current exact-SHA 本地门禁由 [F-ECHO-028] 记录。macOS arm64 fresh ad-hoc DMG/ZIP、metadata/blockmap、codesign/plist/asar/forbidden scan、SBOM `1066` 与 SHA-256 通过，read-only DMG smoke `1 / 1`、安装态完整 workflow `1 / 1`、live contract `2 / 2` 均通过且 `0 skipped / 0 failed`。安装态 workflow 覆盖真实下载 `0600`、marker、安全文件名、无 partial，以及 GLM/RAG、失败注入、重启、retry 和 AgentOS success/cancel/timeout/restart。Developer ID、notary、staple、Gatekeeper 正式链路是 external skipped，ad-hoc 证据不等同正式签名发布。

截至 2026-07-13，公共 Release / 生产 / bootstrap 仍分别为 `v0.2.50` / `0.2.49` / `0.2.45`，bootstrap 未声明 `minimum_client_version` [F-ECHO-029]。正式 signed cross-platform、受保护 environment/secret 和 public cutover 仍为外部阻塞；本段不替代跨平台 CI、签名、公开 Release 或生产可用性证据。
