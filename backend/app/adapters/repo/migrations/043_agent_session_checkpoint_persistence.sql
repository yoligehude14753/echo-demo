-- 043_agent_session_checkpoint_persistence.sql
-- B11 persistence core: one Echo-owned session identity, durable context events,
-- and atomically published versioned checkpoints.  Payload integrity is checked
-- by app.runtime.session_checkpoint_persistence before and after SQLite writes.

CREATE TABLE IF NOT EXISTS agent_runtime_sessions (
    session_id TEXT PRIMARY KEY CHECK(length(session_id) > 0),
    task_id TEXT NOT NULL CHECK(length(task_id) > 0),
    operation_key TEXT NOT NULL CHECK(length(operation_key) > 0),
    model_config_revision INTEGER NOT NULL CHECK(model_config_revision >= 1),
    grant_id TEXT NOT NULL CHECK(length(grant_id) > 0),
    grant_revision INTEGER NOT NULL CHECK(grant_revision >= 1),
    grant_snapshot_json TEXT NOT NULL,
    kernel_build_id TEXT NOT NULL CHECK(length(kernel_build_id) > 0),
    kernel_build_identity_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'paused', 'closed', 'stale')),
    latest_checkpoint_id TEXT,
    last_durable_event_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_durable_event_seq >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, operation_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_sessions_task
    ON agent_runtime_sessions(task_id, updated_at);

CREATE TABLE IF NOT EXISTS agent_runtime_events (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq >= 1),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'agent.brief',
        'agent.summary.updated',
        'agent.compaction.started',
        'agent.compaction.completed',
        'agent.compaction.failed'
    )),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    durable_event_seq INTEGER NOT NULL CHECK(durable_event_seq >= 0),
    checksum TEXT NOT NULL CHECK(length(checksum) = 71),
    PRIMARY KEY(session_id, event_seq),
    FOREIGN KEY(session_id) REFERENCES agent_runtime_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_events_durable_seq
    ON agent_runtime_events(session_id, durable_event_seq);

CREATE TABLE IF NOT EXISTS agent_runtime_checkpoints (
    session_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL CHECK(length(checkpoint_id) > 0),
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    task_id TEXT NOT NULL CHECK(length(task_id) > 0),
    operation_key TEXT NOT NULL CHECK(length(operation_key) > 0),
    model_config_revision INTEGER NOT NULL CHECK(model_config_revision >= 1),
    grant_revision INTEGER NOT NULL CHECK(grant_revision >= 1),
    last_durable_event_seq INTEGER NOT NULL CHECK(last_durable_event_seq >= 0),
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK(length(checksum) = 71),
    created_at TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY(session_id, checkpoint_id),
    FOREIGN KEY(session_id) REFERENCES agent_runtime_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_checkpoints_latest
    ON agent_runtime_checkpoints(session_id, saved_at DESC);
