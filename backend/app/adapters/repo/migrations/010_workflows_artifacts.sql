-- 010_workflows_artifacts.sql
-- EchoDesk 0.3 workflow core + artifact metadata/link tables.
-- 选用 010 避开真实用户库里可能已经存在的 006-009 历史实验版本。

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending',
        'running',
        'cancel_requested',
        'succeeded',
        'failed',
        'timeout',
        'cancelled',
        'cancel_failed'
    )),
    title TEXT,
    intent_text TEXT NOT NULL,
    meeting_id TEXT,
    todo_id TEXT,
    agent_task_id TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    timeout_s REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_state
    ON workflow_runs(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_meeting
    ON workflow_runs(meeting_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_todo
    ON workflow_runs(meeting_id, todo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_agent_task
    ON workflow_runs(agent_task_id);

CREATE TABLE IF NOT EXISTS workflow_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('user', 'debug', 'hidden')),
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_run
    ON workflow_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_workflow_events_type
    ON workflow_events(event_type, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    generation_latency_ms REAL NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_created
    ON artifacts(created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_run
    ON artifacts(run_id);

CREATE TABLE IF NOT EXISTS artifact_links (
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

CREATE INDEX IF NOT EXISTS idx_artifact_links_meeting
    ON artifact_links(meeting_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifact_links_todo
    ON artifact_links(meeting_id, todo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifact_links_run
    ON artifact_links(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_links_dedupe
    ON artifact_links(
        artifact_id,
        source,
        COALESCE(meeting_id, ''),
        COALESCE(todo_id, ''),
        COALESCE(run_id, '')
    );
