# EchoDesk 0.3 数据模型与契约

版本：0.3.2 | 状态：实现快照 | 更新时间：2026-07-13

迁移范围：`001`–`039`

## 1. 数据原则

1. SQLite 是 domain、identity、Workflow 和 RAG manifest 的持久事实源。
2. public 资源使用 `(tenant_id, owner_id, logical_id)` 复合边界；客户端不能自行构造授权 scope。
3. UI store、WebSocket replay buffer 和 BM25 JSON 文件都是可重建投影/cache。
4. domain write、Workflow run/event 和 outbox 必须同事务提交。
5. session token、device credential 等明文只在必要响应/客户端安全存储中存在，服务端持久化 hash 或受控记录。
6. migration 文件内容由 checksum 保护；已应用 migration 发生漂移时 fail closed。

## 2. Scope 与复合键

服务端 Principal：

```text
tenant_id + owner_id(user_id) + device_id + session_id + mode
```

核心资源的逻辑主键：

| 资源 | 主键/边界 |
|---|---|
| meeting | `(tenant_id, owner_id, id)` |
| speaker | `(tenant_id, owner_id, speaker_id)` |
| workflow run | `(tenant_id, owner_id, run_id)` |
| workflow event | `(tenant_id, owner_id, run_id, seq)` |
| artifact | `(tenant_id, owner_id, artifact_id)` |
| agent task | `(tenant_id, owner_id, task_id)` |
| RAG document | `(tenant_id, owner_id, doc_id)` |
| memory node | `(tenant_id, owner_id, memory_id)` |
| memory provenance | `(tenant_id, owner_id, provenance_id)` |
| memory relation | `(tenant_id, owner_id, relation_id)` |
| memory profile setting | `(tenant_id, owner_id, config_key)` |
| memory extraction run | `(tenant_id, owner_id, run_id)` |
| session/device | tenant/user/device/family 组合外键 |

`device_id` 记录来源设备，但 owner 是资源授权边界；同 owner 的已授权设备可以在服务端规则允许时访问同一用户资源。磁盘使用 tenant/owner 派生的 opaque scope directory，不直接使用 untrusted logical id。

## 3. Identity 与 Session 表

| 表 | 责任 |
|---|---|
| `tenants` | tenant 状态与生命周期 |
| `users` | tenant 内稳定 user；资源列仍使用 `owner_id` 命名 |
| `devices` | 用户设备、状态和显示信息 |
| `session_families` | 可续签/撤销的身份连续性边界 |
| `device_credentials` | 设备凭证 hash、generation、有效期和撤销状态 |
| `principal_sessions` | 短期 bearer session hash、family、generation、expiry |
| `public_enrollments` | 首次/附加设备 enrollment challenge |
| `public_enrollment_admissions` | peer/global admission 窗口记录 |
| `resource_tickets` | 短期 owner-scoped 文件/分享访问票据 |
| `principal_quota_ledger` | principal 当前资源与模型配额账本 |

约束：

- 一个 session family 同时只有一个 active session generation。
- 一个 device credential family 同时只有一个 active credential generation。
- renew、rotate、additional-device 和 revoke 必须验证当前有效身份。
- session token 数据库只保存 hash；401/409/429 使用稳定错误类别。
- `POST /session`、`/session/enroll`、`/session/renew`、`/session/credential/rotate` 在 principal lookup 前获取独立 session-body admission lease。全局/peer 并发和速率同时受限，lease 覆盖 route body 解析并在完成或取消时释放；其它业务路由不占用该 pool。
- 除 `/healthz`、`/readyz`、`/bootstrap`、合法 share ticket 与 host-admin 外，public HTTP 请求必须携带 `X-EchoDesk-Client-Version`。缺失、非法或低于 `/bootstrap.minimum_client_version` 返回 `426 client_upgrade_required`；版本受支持但无有效 session 才返回 `401 session_required`。最低版本自报只做兼容性门禁，不替代 bearer 身份校验。
- Electron 主进程的 session DTO 必须包含其 credential vault 绑定的 `backend_origin`。renderer 只在该值与当前 transport origin 完全相等时接纳 token；不匹配或缺失时在任何业务 fetch / WS 前拒绝，防止 A 的 bearer 泄漏给 B。

主要 REST：

| Method | Path | 语义 |
|---|---|---|
| `POST` | `/session` | 兼容首次 enrollment |
| `POST` | `/session/enroll` | 首次设备 enrollment |
| `POST` | `/session/renew` | 使用 device credential 续签 session |
| `POST` | `/session/claim` | claim legacy identity |
| `POST` | `/session/credential/rotate` | 凭证 rotation |
| `POST` | `/session/devices/enroll` | 附加设备 enrollment |
| `POST` | `/session/revoke` | 撤销 session family 或 device |

## 4. Meeting 与 Capture

主要表：

- `meetings`：state、时间、minutes、status/error、display title、tombstone、RAG projection state/attempt/next-retry/generation，以及当前 minutes generation run/cancel marker。
- `meeting_segments`：owner-scoped transcript segment。
- `meeting_speaker_labels`、`speakers`：会议标签和 owner-scoped speaker profile。
- `ambient_segments`、`ambient_audio_files`：ambient 文本与文件 lifecycle；segment 记录 RAG projection state/error/projected-at/attempt/next-retry。
- `meeting_state_migration_audit`：single-active-meeting migration 审计。

约束：

- 同 tenant/owner 只有一个 `in_meeting` row。
- segment/label 的复合外键不能指向其他 owner 的 meeting。
- `minutes_cleared_at` 存在时，恢复流程不能重新写回已清除纪要。
- `minutes_generation_run_id` 是纪要生成写权；terminal projector 仅在 run id 匹配时清 marker 并写 `generation_failed`，`minutes_generation_cancelled_at` 区分显式取消，旧 run 不能覆盖 retry。
- `rag_projection_state/error/projected_at/attempts/next_retry_at` 使 meeting 与 ambient 投影可退避修复；meeting 的 `rag_projection_generation` 使 finalize/clear/repair completion 可做 CAS。

主要 REST：`/meetings`、`/meetings/current`、manual start/end、meeting start/chunk/finalize/end、transcript、minutes、Artifact、share ticket、outputs clear，以及 `/capture/*`。

## 5. Workflow 数据模型

### `workflow_runs`

关键字段：

- identity：`tenant_id`、`owner_id`、`device_id`；
- identity key：`run_id`、`kind`、`source`；
- state：`pending`、`running`、`cancel_requested`、`succeeded`、`failed`、`timeout`、`cancelled`、`cancel_failed`；
- relation：`meeting_id`、`todo_id`、`agent_task_id`、`parent_run_id`；
- concurrency：`revision`、`idempotency_key`、`active_key`、`attempt`；
- timing：`timeout_s`、`deadline_at`、`cancel_requested_at`、created/started/finished/updated；
- payload：`input_json`、`output_json`、`error`。

`idempotency_key` 和 `active_key` 的 unique index 都包含 tenant/owner scope。retry 创建新 run，不改写旧 run 的历史。retry 的 idempotency key 永久派生自 parent+attempt，并继承 parent `active_key`；terminal parent 读取、child insert、parent retry event/outbox 和可选 domain writer 在一个 `BEGIN IMMEDIATE` 内提交。fresh create 或其它 retry 先占用 active key 时，不得再提交第二个活动 child。

### `workflow_events`

主键为 `(tenant_id, owner_id, run_id, seq)`；包含 event type、当时 state、visibility、message、payload 与 created_at。`seq` 在同一 run 内单调递增。

### `workflow_outbox`

保存 commit 后要投影的 aggregate/event/payload、attempt、error 与 published timestamp。相关恢复表：

- `workflow_outbox_consumers`：每个进程的 cursor 与 heartbeat；
- `workflow_outbox_consumer_recovery`：兼容 row recovery；
- `workflow_outbox_consumer_scope_recovery`：按 consumer + tenant/owner 压缩的 backoff lane；
- `workflow_outbox_global_recovery_state`：全局 ancient-row cursor 与 lease/fence；
- `workflow_outbox_global_scope_recovery`：失败 scope 的全局 watermark/backoff。

### `execution_leases`

记录 lease key、holder、fence token、expiry 与 heartbeat。Workflow Dispatcher 和 Agent bridge 只有持有有效 lease/fence 才能继续写投影。

### Unit of Work 契约

```text
BEGIN IMMEDIATE
  validate scope/state/revision/lease
  domain writer
  update workflow run
  append workflow events
  append outbox rows
COMMIT
publish committed outbox
```

异常必须 rollback；不能用两次独立 commit 模拟原子性。

Workflow REST：

| Method | Path | 语义 |
|---|---|---|
| `GET` | `/workflows/runs` | 当前 principal 的 runs |
| `GET` | `/workflows/runs/{run_id}` | 当前 scope snapshot |
| `GET` | `/workflows/runs/{run_id}/events` | `after_seq` replay |
| `POST` | `/workflows/runs/{run_id}/cancel` | 进入取消协议 |
| `POST` | `/workflows/runs/{run_id}/retry` | 创建新 attempt |

## 6. Artifact 与 Todo

### `artifacts`

owner-scoped metadata：type、title、file path、MIME、size、latency、model、metadata、run id 与 timestamps。

### `artifact_links`

把 Artifact 关联到 meeting、todo、run 和 source。UI 的工作产物列表、meeting outputs 和分享页都从 metadata/link 投影，不依赖前端内存。

写入顺序：安全 staging -> 生成/验证 -> Unit of Work 登记 Artifact/link/run/event/outbox -> commit。失败时清理 staging，不留下已成功假象。

REST：

| Method | Path | 语义 |
|---|---|---|
| `POST` | `/artifacts/generate` | 创建并等待 Artifact workflow |
| `GET` | `/artifacts` | 当前 principal Artifact |
| `GET` | `/artifacts/{artifact_id}/download` | scope + metadata + path 校验 |
| `GET` | `/meetings/{id}/artifacts` | linked Artifact |
| `DELETE` | `/meetings/{id}/outputs` | 原子清理本 meeting outputs |

public 下载找不到 owner-scoped metadata 时返回 404；legacy build-directory fallback 仅限 local-first。

## 7. Agent 数据模型

| 表 | 责任 |
|---|---|
| `agent_tasks` | runner task、state、snapshot、grant、timeout、workflow link、bridge completion |
| `agent_task_events` | raw/normalized event、hash 去重、projection marker |
| `agent_runner_grants` | owner/device/runner/workspace Full Access grant |
| `agent_command_outbox` | durable cancel command、稳定 operation key、fenced lease、attempt/outcome/backoff |

Agent terminal first-wins。raw event 先持久化，bridge 再投影 Workflow；pending projection 和 bridge recovery index 支持重启接管。

REST：task create/list/get/events/artifact proxy/cancel/retry，以及 grant status/create/revoke。创建 task、取消、重试和创建 grant 需要 admin/host capability；普通 public principal 无权调用。

## 8. RAG 数据模型

| 表 | 责任 |
|---|---|
| `rag_documents` | owner-scoped document manifest、source/path/hash/status |
| `rag_content_owners` | content hash owner、size、doc、workflow、lifecycle、quota |
| `bm25_index_state` | physical index key 的单调 revision |
| `bm25_index_documents` | authoritative payload、scope、cache path/hash/revision |
| `bm25_document_projection_fences` | meeting doc 的 owner-scoped generation 与 `index|delete` 最新意图 |

SQLite transaction 是跨进程提交点；JSON 文件是内容 hash 可验证、可重建 cache。实例读取 revision 后刷新 snapshot，不能把某个进程的内存 BM25 当全局事实源。

meeting BM25 payload 携带 projection generation；低 generation 或同 generation 相反操作不能覆盖 fence。查询以 `meetings.rag_projection_state/generation` 再过滤缓存：`delete_pending`、`delete_failed`、`deleted` 都立即不可见，物理文件删除失败不恢复可见性。meeting/ambient repair 只读取 due 的 owner scope 和有界 batch；失败递增 attempts 并写 next-retry。ambient ingest 使用 `ambient-segment:<id>` 稳定 operation id，崩溃重放保持幂等。

REST：`POST /rag/ingest`、`GET /rag/stats`、`GET /rag/docs`、`DELETE /rag/docs/{doc_id}`、`POST /rag/ask`。

`/rag/ask` 是 SSE：delta/citation 后必须有 `done` 才算完成；error/disconnect 取消上游，不创建虚假成功 run。

## 9. Memory 数据模型

schema `039` 建立 owner-scoped 在线记忆系统：

| 表 | 责任 |
|---|---|
| `memory_nodes` | L2 语义记忆；保存 kind、规范化内容、canonical key、置信度、显著度、确认/命中/来源计数、状态与 revision |
| `memory_provenance` | 记忆来源；保存 source kind/id、可选 meeting/Artifact/segment 引用、摘录及其 SHA-256 |
| `memory_relations` | owner 内记忆关系；支持 `related_to`、`supports`、`contradicts`、`supersedes` |
| `memory_profile_settings` | L3 用户显式配置；只接受 `user_explicit` 来源并保留确认、删除与 revision 时间线 |
| `memory_extraction_runs` | 小模型抽取审计；保存输入 hash、模型、状态、耗时、候选数、输出 JSON 和错误 |

召回分层：

- L0：当前 conversation working memory 与时间窗内的当前会议 segment；不复制到 039 表。
- L1：已结束会议 segment/纪要、ambient segment 和 Artifact 的 owner-scoped 情景投影。
- L2：`memory_nodes` 中 status 为 `active` 的事实、偏好、决策、todo 和关系记忆；最近 provenance 作为 source reference。
- L3：`memory_profile_settings` 中未删除的用户显式配置；不能由推断式抽取写入。

约束：

- 所有读写从已认证 Principal 生成 `(tenant_id, owner_id)` scope；客户端不能提交 scope。
- 同 owner/kind/canonical key 同时最多一条 active node；reaffirm、supersede、confirm、soft delete 都保留计数、时间和 revision。
- provenance 按 owner、memory、source、segment 与 excerpt hash 去重；默认管理 API 对 node 做软删除并保留 provenance/relation，只有物理删除 row 时复合外键才级联。
- profile setting 只允许显式配置 API 写入；删除采用 tombstone，不把推断内容提升为 L3。
- 小模型关联失败时允许回退 deterministic ranking；抽取 run 必须留审计状态，不能把失败伪装成已写入记忆。

主要 REST：`POST /memory/recall`、`POST /memory/extract`、node list/get/provenance/confirm/update/delete、profile list/upsert/delete，以及 `DELETE /memory/working/{conversation_id}`。

## 10. WebSocket Contract

主路径：`/ws/echo`，协议版本 `1.0`。

public 模式首帧必须是有界 `client_hello`，`client_version` 必须满足最低客户端版本，bearer 放在 `auth` 对象中；服务端验证版本、origin、admission、session 和 quota 后才绑定 scope。主要协议事件：

- `server_hello`：版本、epoch、max seq；
- `server_ping`：15 秒心跳；
- `server_resync`：历史 gap，需要 REST replace；
- `server_sync`：gap fence 后的资源同步描述；
- `workflow.event` / `workflow.snapshot`；
- meeting、minutes、Artifact、Agent 等已提交 domain event。

close code：`4401` 身份失效，`4408` 握手协议错误，`4409` 慢消费者，`4426` 客户端必须升级，`4429` admission/quota。收到 `4426` 后客户端停止普通重连。全局 scope stream 满载时，distinct principal scope 只进入有界 FIFO 候补；同 scope 最多一个候补。队列满、重复候补或等待超时仍返回 `4429`。public session 会周期 revalidate；失效后不能普通重连并沿用旧 owner。

## 11. Electron IPC Contract

IPC 只用于桌面本机能力，按域登记并由 contract test 对账：

- backend endpoint/status/restart；
- workspace pick/status/add/remove/scan/clear/cancel；每个调用携带 renderer `expectedBackendOrigin`；
- Artifact open/save；
- update check/install/open；
- microphone/system settings；
- credential vault 和 public identity session。

preload key、main handler 和 renderer wrapper 必须同时存在。public Web 内容不能通过导航串接到任意 IPC；Desktop local host capability 是明确的受信任边界。

public Electron workspace transport 只接受无 userinfo/path/query/fragment 的精确 HTTPS origin，且 backend、credential vault、renderer expected origin、session `backend_origin` 必须完全相同。请求附 client version 和 bearer，3xx 一律拒绝，401 最多 renew 一次，426 进入 terminal upgrade，timeout/cancel 覆盖响应体读取并限制响应大小。registry schema 3 以 origin 为一级键；同 origin mutation 串行并持有 epoch lease，切换 origin 会 cancel 旧操作，orphan doc 删除失败保留 doc id 供后续 scan 重试。

## 12. Migration 契约

- migration catalog 按编号严格递增到 `039`。已越过高水位但从未执行过的 restored
  historical `006`–`009` 记为 not-applicable，不倒序执行，也不伪造 `schema_version` 行。
- `schema_version` 对实际执行且能对应当前文件的 migration 记录 version、migration
  name 和 content SHA-256。已发布的 `005_speaker_label_user_set` 与当前
  `005_agent_tasks` 是历史 version fork：保留原 description，name/hash 维持 NULL，
  绝不把旧 schema 伪绑定到新文件。
- 旧 global key 数据迁移到 composite key；无法安全归属的 row 进入 `migration_orphan_quarantine`。
- migration 重跑幂等；checksum 不一致 fail closed。
- archived 原版 `006`–`009` 已恢复进 catalog。走过该 lineage 的
  conversations/memory 与 users/sessions/API key/billing 表在 `018` 原子改名为
  `legacy_v6_*`、`legacy_v7_*`、`legacy_v8_*`；历史行、index、FK/view 引用保留，
  active identity 只使用 tenant/user/device/session-family 表。
- `033` 收口历史多 active meeting 并保留 audit。
- `031` 建立 BM25 跨进程 revision；`034`/`035` 建立 scope/global outbox recovery。
- `036` 建立 `agent_command_outbox`：`operation_key` 全局唯一，同一 owner/task/command type 只允许一条逻辑命令；`attempts`、`next_attempt_at`、`outcome` 和 `completed_at` 支持退避恢复与审计。
- `037` 把两条 lineage 的 `ambient_segments` 收敛为同一 source/scope schema，并恢复
  `speakers.label_user_set`。published v5 的非零值在 `013` rebuild transaction 内按
  composite speaker key 快照并恢复；定义不兼容、未知 rebuild dependent 或行映射丢失均整体 rollback。
- `038` 为 meeting 增加 RAG repair attempts/next-retry/generation 与 minutes run-owned
  markers，为 ambient segment 增加 durable projection/repair 字段，并创建
  `bm25_document_projection_fences` 及 due-work indexes。fence table 使用
  `CREATE TABLE IF NOT EXISTS`，兼容 BM25 adapter 在 migration runner 前先建立协调表。
  schema 37 的 ambient 行一律从 `reconcile_pending` 开始；repair 先按 owner scope、
  规范化 `captured_at`/正文和仍可用的 `audio_ref` 查旧 chunk，命中只确认 `indexed`，
  未命中才用 `ambient-segment:<row_id>` 补投影。retention 已清空 DB `audio_ref` 时仍以
  时间与正文识别旧投影，避免重复追加。
- `039` 创建新的 owner-scoped `memory_nodes`、`memory_provenance`、
  `memory_relations`、`memory_profile_settings` 和 `memory_extraction_runs`。published v7
  的无 principal/provenance `memory_nodes` 已由兼容迁移路径归档为
  `legacy_v7_memory_nodes`；039 不自动导入该归档，只有显式审查后的数据才可进入新表。

## 13. Agent 一致性契约

1. Agent `cancel_requested`、Workflow state/event/outbox 与 `agent_command_outbox` 在同一个 SQLite transaction 提交；任一步失败整体回滚。
2. command worker 通过 `execution_leases` 的 holder/expiry/fence 跨实例竞争；崩溃重放复用同一个 `Idempotency-Key`，而不是生成新逻辑操作。
3. Agent terminal HTTP read 必须先修复并核对关联 Workflow terminal；不一致时 fail closed，不把领先终态返回给客户端。
4. Agent 与 Workflow 的冲突终态遵循 first-terminal-wins；完成先赢时 pending cancel command 以 `terminal_won` 完成且不调用远程 runner。
5. Agent submit 用 scope-derived opaque key 和 `agent_submit` heartbeat lease；runner id 只通过 fenced CAS 绑定。AgentOS 将 key、request fingerprint、deterministic task id 持久化为 UNIQUE reservation。
6. Agent terminal write 先在同一 transaction 仲裁 linked Workflow；cancel worker 的 terminal write 还必须在该 transaction 校验 command fence 并完成 command outbox。
7. 已完成的无-runner cancel 后若收到晚到 runner id，command 以 `force_remote=1` 重开并真实发送 cancel；Agent 本地终态保持不变。
