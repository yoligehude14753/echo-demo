# EchoDesk 0.3 数据模型与契约

日期：2026-07-09  
状态：开发前设计  

## 1. 设计原则

1. 后端 SQLite 是 workflow 和 artifact 的事实源。
2. 前端 store 只做视图缓存。
3. WebSocket 只广播后端已确认事件。
4. AgentOS raw event 不直接暴露给普通 UI。
5. 任何可恢复长流程必须能通过 DB 重建最新状态。

## 2. 数据表草案

### 2.1 workflow_runs

```sql
CREATE TABLE workflow_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    origin TEXT NOT NULL,
    title TEXT NOT NULL,
    meeting_id TEXT,
    todo_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    runner TEXT,
    upstream_task_id TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0,
    timeout_s REAL NOT NULL DEFAULT 1800,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
```

约束：

- `kind` 允许值：`meeting_minutes`、`artifact_generate`、`agent_task`、`rag_ingest`、`rag_query`、`export`。
- `state` 允许值：`pending`、`running`、`cancel_requested`、`succeeded`、`failed`、`timeout`、`cancelled`、`cancel_failed`。
- `meeting_id`、`todo_id` 可空，但 Todo 触发时必须同时存在。
- `upstream_task_id` 用于 AgentOS / Claude Code runner task id。

### 2.2 workflow_events

```sql
CREATE TABLE workflow_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'user',
    payload_json TEXT NOT NULL DEFAULT '{}',
    raw_event_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_workflow_events_raw
    ON workflow_events(run_id, raw_event_hash)
    WHERE raw_event_hash IS NOT NULL;
```

约束：

- `seq` 按 run 单调递增。
- raw event 去重只基于同一 run。
- `visibility` 允许值：`user`、`debug`、`hidden`。

### 2.3 artifacts

```sql
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    model TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE SET NULL
);
```

约束：

- 所有新 artifact 必须写入本表。
- `file_path` 必须位于 `settings.storage_dir` 或明确允许的 build dir 下。
- Agent 产物导入后也进入本表。

### 2.4 artifact_links

```sql
CREATE TABLE artifact_links (
    link_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    source TEXT NOT NULL,
    meeting_id TEXT,
    todo_id TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX idx_artifact_links_dedupe
    ON artifact_links(
        artifact_id,
        source,
        COALESCE(meeting_id, ''),
        COALESCE(todo_id, ''),
        COALESCE(run_id, '')
    );
```

约束：

- `source` 允许值：`command`、`todo`、`agent`、`meeting`、`import`。
- 会议产物查询只读 `artifact_links.meeting_id`。
- 清理会议 outputs 只处理本会议 link 指向的 artifact。

## 3. 后端服务接口

### 3.1 WorkflowService

```python
class WorkflowService:
    async def create_run(...)
    async def start_run(run_id)
    async def record_event(run_id, event_type, payload, visibility="user", raw_hash=None)
    async def complete_run(run_id, output=None)
    async def fail_run(run_id, error)
    async def timeout_run(run_id, error)
    async def request_cancel(run_id)
    async def mark_cancelled(run_id)
    async def mark_cancel_failed(run_id, error)
    async def retry_run(run_id) -> new_run_id
    async def list_runs(kind=None, meeting_id=None, limit=50)
    async def list_events(run_id, after_seq=0)
    async def restore_unfinished()
```

### 3.2 ArtifactRepository

```python
class ArtifactRepository:
    async def save_artifact(...)
    async def link_artifact(...)
    async def list_artifacts(limit=100)
    async def list_meeting_artifacts(meeting_id)
    async def list_todo_artifacts(meeting_id, todo_id)
    async def get_artifact(artifact_id)
```

## 4. REST Contract

### 4.1 Workflow APIs

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/workflows/runs` | 列出 workflow runs |
| `GET` | `/workflows/runs/{run_id}` | 获取 run |
| `GET` | `/workflows/runs/{run_id}/events` | replay events |
| `POST` | `/workflows/runs/{run_id}/cancel` | 请求取消 |
| `POST` | `/workflows/runs/{run_id}/retry` | 基于旧 run 创建重试 |

说明：

- `/agents/*` 可以继续存在，但内部应委托 WorkflowService。
- `/artifacts/*` 可以继续存在，但必须写 artifact DB。

### 4.2 Artifact APIs

| Method | Path | 0.3 要求 |
|---|---|---|
| `POST` | `/artifacts/generate` | 创建 `artifact_generate` run |
| `GET` | `/artifacts` | 从 DB 列表返回 |
| `GET` | `/artifacts/{artifact_id}/download` | 校验 DB + 文件路径 |
| `GET` | `/meetings/{id}/artifacts` | 从 `artifact_links` 返回 |
| `DELETE` | `/meetings/{id}/outputs` | 清理会议 link 指向的产物 |

### 4.3 Agent APIs

| Method | Path | 0.3 要求 |
|---|---|---|
| `POST` | `/agents/tasks` | 创建 `agent_task` run |
| `GET` | `/agents/tasks` | 从 workflow runs 投影 |
| `GET` | `/agents/tasks/{task_id}` | 兼容旧 task id，内部映射 run |
| `GET` | `/agents/tasks/{task_id}/events` | 从 workflow events 返回 |
| `POST` | `/agents/tasks/{task_id}/cancel` | 进入 `cancel_requested` |
| `POST` | `/agents/tasks/{task_id}/retry` | 创建新 run |
| `GET` | `/agents/tasks/{task_id}/artifacts/{path}` | 兼容代理，成功后可导入 artifacts |

## 5. WebSocket Contract

主路径仍为 `/ws/echo`。

新增或统一事件：

| 事件 | payload |
|---|---|
| `workflow.event` | `WorkflowEventDTO` |
| `workflow.snapshot` | `WorkflowRunDTO` |
| `artifact.ready` | `GeneratedArtifact` + `run_id` + links |
| `artifact.failed` | `run_id` + artifact_type + error |
| `agent.task.event` | 兼容旧 UI，来源为 workflow event 投影 |
| `meeting.todo.updated` | meeting_id + todo_id + status + run_id |

`WorkflowEventDTO`：

```json
{
  "run_id": "run_x",
  "seq": 1,
  "kind": "agent_task",
  "state": "running",
  "event_type": "agent.step_started",
  "visibility": "user",
  "payload": {},
  "created_at": "2026-07-09T23:30:00+08:00"
}
```

规则：

- WS event 必须能通过 REST replay 复原。
- 前端收到 WS 只更新 store，不写事实。
- `server_resync` 后前端必须重新拉取 current meeting、runs、artifacts。

## 6. IPC Contract

0.3 不新增随意 IPC。需要新增时必须登记：

| 域 | IPC | 0.3 状态 |
|---|---|---|
| artifact | `echo:open-artifact-in-system` | 保留 |
| workspace | `workspace:*` | 保留，作为本地目录授权入口 |
| updates | `updates:*` | 保留 |
| mic | `mic:*` | 保留 |
| backend | `backend:*` | 保留 |

门禁：

- preload 暴露的 key 必须有 main handler。
- renderer wrapper 必须覆盖 preload key。
- 删除 IPC 必须改测试快照。

## 7. Migration 策略

0.3 migration 原则：

1. 新建表，不破坏旧表。
2. 不强行猜旧 artifact 与 meeting 的关联。
3. 对能从 `minutes_json.todos[*].artifact_id` 得到的关系，写入 `artifact_links(source='todo')`。
4. 对旧全局 artifacts，仅保留 artifact metadata，不强造 meeting link。
5. migration 必须幂等。

## 8. Contract Snapshot

开发前准备完成后，第一批实现 PR 必须补：

- REST route snapshot。
- WS event type snapshot。
- IPC key snapshot。
- DB migration smoke。
- Workflow state transition tests。
