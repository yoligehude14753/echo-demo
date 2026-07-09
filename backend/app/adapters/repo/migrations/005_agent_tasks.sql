-- 005_agent_tasks.sql
-- ADR-012 / Claude Code runner 主路径：EchoDesk 自有任务事件流。
-- 普通 UI 只消费 agent_task_events 投影后的 EchoTaskEvent，不暴露 AgentOS raw event。

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    runner_task_id TEXT,
    device_id TEXT NOT NULL,
    conversation_id TEXT,
    message_id TEXT,
    title TEXT NOT NULL,
    intent_text TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT 'claude_code',
    task_kind TEXT,
    state TEXT NOT NULL,
    progress_text TEXT,
    final_text TEXT,
    error TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    envelope_json TEXT NOT NULL DEFAULT '{}',
    grant_id TEXT,
    permission_profile TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL,
    finished_at TEXT,
    timeout_s REAL NOT NULL DEFAULT 1800.0
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_device_state
    ON agent_tasks(device_id, state, submitted_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_runner
    ON agent_tasks(runner_task_id);

CREATE TABLE IF NOT EXISTS agent_task_events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    state TEXT NOT NULL,
    visibility TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    raw_event_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq),
    FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_task_events_task
    ON agent_task_events(task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_task_events_raw
    ON agent_task_events(task_id, raw_event_hash)
    WHERE raw_event_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_runner_grants (
    grant_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    runner TEXT NOT NULL,
    permission_profile TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    workspace_ids_json TEXT NOT NULL DEFAULT '[]',
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runner_grants_active
    ON agent_runner_grants(device_id, runner, revoked_at, granted_at);
