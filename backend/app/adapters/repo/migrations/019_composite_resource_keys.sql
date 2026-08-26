-- 019_composite_resource_keys.sql
-- Logical resource ids are only unique inside one tenant/user scope. Rebuild
-- every core table that previously used a global text primary key, and make
-- all existing parent-child foreign keys carry the same composite scope.

PRAGMA defer_foreign_keys=ON;

CREATE TABLE IF NOT EXISTS migration_orphan_quarantine (
    orphan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    relation_name TEXT NOT NULL,
    missing_reference TEXT NOT NULL,
    quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (table_name, tenant_id, owner_id, record_id, relation_name)
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'meeting_segments', s.tenant_id, s.owner_id, CAST(s.id AS TEXT),
       'meeting_id', s.meeting_id
FROM meeting_segments s
WHERE NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = s.tenant_id AND m.owner_id = s.owner_id AND m.id = s.meeting_id
);
DELETE FROM meeting_segments
WHERE NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = meeting_segments.tenant_id
      AND m.owner_id = meeting_segments.owner_id
      AND m.id = meeting_segments.meeting_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'meeting_speaker_labels', s.tenant_id, s.owner_id,
       s.meeting_id || ':' || s.speaker_id, 'meeting_id', s.meeting_id
FROM meeting_speaker_labels s
WHERE NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = s.tenant_id AND m.owner_id = s.owner_id AND m.id = s.meeting_id
);
DELETE FROM meeting_speaker_labels
WHERE NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = meeting_speaker_labels.tenant_id
      AND m.owner_id = meeting_speaker_labels.owner_id
      AND m.id = meeting_speaker_labels.meeting_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'workflow_runs', r.tenant_id, r.owner_id, r.run_id, 'meeting_id', r.meeting_id
FROM workflow_runs r
WHERE r.meeting_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = r.tenant_id AND m.owner_id = r.owner_id AND m.id = r.meeting_id
);
UPDATE workflow_runs
SET meeting_id = NULL
WHERE meeting_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = workflow_runs.tenant_id
      AND m.owner_id = workflow_runs.owner_id
      AND m.id = workflow_runs.meeting_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'workflow_runs', r.tenant_id, r.owner_id, r.run_id, 'parent_run_id', r.parent_run_id
FROM workflow_runs r
WHERE r.parent_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs p
    WHERE p.tenant_id = r.tenant_id
      AND p.owner_id = r.owner_id
      AND p.run_id = r.parent_run_id
);
UPDATE workflow_runs
SET parent_run_id = NULL
WHERE parent_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs p
    WHERE p.tenant_id = workflow_runs.tenant_id
      AND p.owner_id = workflow_runs.owner_id
      AND p.run_id = workflow_runs.parent_run_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'workflow_events', e.tenant_id, e.owner_id, e.run_id || ':' || e.seq,
       'run_id', e.run_id
FROM workflow_events e
WHERE NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = e.tenant_id AND r.owner_id = e.owner_id AND r.run_id = e.run_id
);
DELETE FROM workflow_events
WHERE NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = workflow_events.tenant_id
      AND r.owner_id = workflow_events.owner_id
      AND r.run_id = workflow_events.run_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'artifacts', a.tenant_id, a.owner_id, a.artifact_id, 'run_id', a.run_id
FROM artifacts a
WHERE a.run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = a.tenant_id AND r.owner_id = a.owner_id AND r.run_id = a.run_id
);
UPDATE artifacts
SET run_id = NULL
WHERE run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = artifacts.tenant_id
      AND r.owner_id = artifacts.owner_id
      AND r.run_id = artifacts.run_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'artifact_links', l.tenant_id, l.owner_id, l.link_id, 'artifact_id', l.artifact_id
FROM artifact_links l
WHERE NOT EXISTS (
    SELECT 1 FROM artifacts a
    WHERE a.tenant_id = l.tenant_id
      AND a.owner_id = l.owner_id
      AND a.artifact_id = l.artifact_id
);
DELETE FROM artifact_links
WHERE NOT EXISTS (
    SELECT 1 FROM artifacts a
    WHERE a.tenant_id = artifact_links.tenant_id
      AND a.owner_id = artifact_links.owner_id
      AND a.artifact_id = artifact_links.artifact_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'artifact_links', l.tenant_id, l.owner_id, l.link_id, 'meeting_id', l.meeting_id
FROM artifact_links l
WHERE l.meeting_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = l.tenant_id AND m.owner_id = l.owner_id AND m.id = l.meeting_id
);
UPDATE artifact_links
SET meeting_id = NULL
WHERE meeting_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM meetings m
    WHERE m.tenant_id = artifact_links.tenant_id
      AND m.owner_id = artifact_links.owner_id
      AND m.id = artifact_links.meeting_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'artifact_links', l.tenant_id, l.owner_id, l.link_id, 'run_id', l.run_id
FROM artifact_links l
WHERE l.run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = l.tenant_id AND r.owner_id = l.owner_id AND r.run_id = l.run_id
);
UPDATE artifact_links
SET run_id = NULL
WHERE run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = artifact_links.tenant_id
      AND r.owner_id = artifact_links.owner_id
      AND r.run_id = artifact_links.run_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'agent_tasks', t.tenant_id, t.owner_id, t.task_id, 'workflow_run_id', t.workflow_run_id
FROM agent_tasks t
WHERE t.workflow_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = t.tenant_id
      AND r.owner_id = t.owner_id
      AND r.run_id = t.workflow_run_id
);
UPDATE agent_tasks
SET workflow_run_id = NULL
WHERE workflow_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workflow_runs r
    WHERE r.tenant_id = agent_tasks.tenant_id
      AND r.owner_id = agent_tasks.owner_id
      AND r.run_id = agent_tasks.workflow_run_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'agent_tasks', t.tenant_id, t.owner_id, t.task_id, 'grant_id', t.grant_id
FROM agent_tasks t
WHERE t.grant_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM agent_runner_grants g
    WHERE g.tenant_id = t.tenant_id
      AND g.owner_id = t.owner_id
      AND g.grant_id = t.grant_id
);
UPDATE agent_tasks
SET grant_id = NULL
WHERE grant_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM agent_runner_grants g
    WHERE g.tenant_id = agent_tasks.tenant_id
      AND g.owner_id = agent_tasks.owner_id
      AND g.grant_id = agent_tasks.grant_id
);

-- A grant is device-scoped inside one tenant/user. Historical rows could
-- carry arbitrary device labels; do not silently turn those into active host
-- execution grants. Record them, detach referencing tasks, then quarantine by
-- removal before the composite device FK is enabled.
INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'agent_runner_grants', g.tenant_id, g.owner_id, g.grant_id,
       'device_id', g.device_id
FROM agent_runner_grants g
WHERE NOT EXISTS (
    SELECT 1 FROM devices d
    WHERE d.tenant_id = g.tenant_id
      AND d.user_id = g.owner_id
      AND d.device_id = g.device_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'agent_tasks', t.tenant_id, t.owner_id, t.task_id, 'grant_id', t.grant_id
FROM agent_tasks t
JOIN agent_runner_grants g
  ON g.tenant_id = t.tenant_id
 AND g.owner_id = t.owner_id
 AND g.grant_id = t.grant_id
WHERE NOT EXISTS (
    SELECT 1 FROM devices d
    WHERE d.tenant_id = g.tenant_id
      AND d.user_id = g.owner_id
      AND d.device_id = g.device_id
);

UPDATE agent_tasks
SET grant_id = NULL
WHERE grant_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM agent_runner_grants g
    WHERE g.tenant_id = agent_tasks.tenant_id
      AND g.owner_id = agent_tasks.owner_id
      AND g.grant_id = agent_tasks.grant_id
      AND NOT EXISTS (
          SELECT 1 FROM devices d
          WHERE d.tenant_id = g.tenant_id
            AND d.user_id = g.owner_id
            AND d.device_id = g.device_id
      )
);

DELETE FROM agent_runner_grants
WHERE NOT EXISTS (
    SELECT 1 FROM devices d
    WHERE d.tenant_id = agent_runner_grants.tenant_id
      AND d.user_id = agent_runner_grants.owner_id
      AND d.device_id = agent_runner_grants.device_id
);

INSERT OR IGNORE INTO migration_orphan_quarantine
    (table_name, tenant_id, owner_id, record_id, relation_name, missing_reference)
SELECT 'agent_task_events', e.tenant_id, e.owner_id, e.task_id || ':' || e.seq,
       'task_id', e.task_id
FROM agent_task_events e
WHERE NOT EXISTS (
    SELECT 1 FROM agent_tasks t
    WHERE t.tenant_id = e.tenant_id AND t.owner_id = e.owner_id AND t.task_id = e.task_id
);
DELETE FROM agent_task_events
WHERE NOT EXISTS (
    SELECT 1 FROM agent_tasks t
    WHERE t.tenant_id = agent_task_events.tenant_id
      AND t.owner_id = agent_task_events.owner_id
      AND t.task_id = agent_task_events.task_id
);

ALTER TABLE meetings RENAME TO meetings_legacy_global_key;
ALTER TABLE meeting_segments RENAME TO meeting_segments_legacy_global_key;
ALTER TABLE meeting_speaker_labels RENAME TO meeting_speaker_labels_legacy_global_key;

ALTER TABLE workflow_runs RENAME TO workflow_runs_legacy_global_key;
ALTER TABLE workflow_events RENAME TO workflow_events_legacy_global_key;
ALTER TABLE artifacts RENAME TO artifacts_legacy_global_key;
ALTER TABLE artifact_links RENAME TO artifact_links_legacy_global_key;
ALTER TABLE agent_tasks RENAME TO agent_tasks_legacy_global_key;
ALTER TABLE agent_task_events RENAME TO agent_task_events_legacy_global_key;
ALTER TABLE agent_runner_grants RENAME TO agent_runner_grants_legacy_global_key;

CREATE TABLE meetings (
    id TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL CHECK(state IN ('in_meeting','ended','finalized')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    finalized_at TEXT,
    auto_started INTEGER NOT NULL DEFAULT 0,
    minutes_json TEXT,
    raw_transcript_ref TEXT,
    minutes_status TEXT,
    minutes_error TEXT,
    display_title TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    minutes_cleared_at TEXT,
    PRIMARY KEY (tenant_id, owner_id, id)
);

CREATE TABLE meeting_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    text TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    captured_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    FOREIGN KEY (tenant_id, owner_id, meeting_id)
        REFERENCES meetings(tenant_id, owner_id, id) ON DELETE CASCADE
);

CREATE TABLE meeting_speaker_labels (
    meeting_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    label TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, meeting_id, speaker_id),
    FOREIGN KEY (tenant_id, owner_id, meeting_id)
        REFERENCES meetings(tenant_id, owner_id, id) ON DELETE CASCADE
);

CREATE TABLE workflow_runs (
    run_id TEXT NOT NULL,
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
    updated_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    revision INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    parent_run_id TEXT,
    deadline_at TEXT,
    cancel_requested_at TEXT,
    active_key TEXT,
    PRIMARY KEY (tenant_id, owner_id, run_id),
    FOREIGN KEY (tenant_id, owner_id, meeting_id)
        REFERENCES meetings(tenant_id, owner_id, id),
    FOREIGN KEY (tenant_id, owner_id, parent_run_id)
        REFERENCES workflow_runs(tenant_id, owner_id, run_id)
);

CREATE TABLE workflow_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('user', 'debug', 'hidden')),
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, run_id, seq),
    FOREIGN KEY (tenant_id, owner_id, run_id)
        REFERENCES workflow_runs(tenant_id, owner_id, run_id) ON DELETE CASCADE
);

CREATE TABLE artifacts (
    artifact_id TEXT NOT NULL,
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
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, artifact_id),
    FOREIGN KEY (tenant_id, owner_id, run_id)
        REFERENCES workflow_runs(tenant_id, owner_id, run_id)
);

CREATE TABLE artifact_links (
    link_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    source TEXT NOT NULL,
    meeting_id TEXT,
    todo_id TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, link_id),
    FOREIGN KEY (tenant_id, owner_id, artifact_id)
        REFERENCES artifacts(tenant_id, owner_id, artifact_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_id, meeting_id)
        REFERENCES meetings(tenant_id, owner_id, id),
    FOREIGN KEY (tenant_id, owner_id, run_id)
        REFERENCES workflow_runs(tenant_id, owner_id, run_id)
);

CREATE TABLE agent_tasks (
    task_id TEXT NOT NULL,
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
    timeout_s REAL NOT NULL DEFAULT 1800.0,
    workflow_run_id TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, task_id),
    FOREIGN KEY (tenant_id, owner_id, workflow_run_id)
        REFERENCES workflow_runs(tenant_id, owner_id, run_id),
    FOREIGN KEY (tenant_id, owner_id, grant_id)
        REFERENCES agent_runner_grants(tenant_id, owner_id, grant_id)
);

CREATE TABLE agent_task_events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    state TEXT NOT NULL,
    visibility TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    raw_event_hash TEXT,
    created_at TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, task_id, seq),
    FOREIGN KEY (tenant_id, owner_id, task_id)
        REFERENCES agent_tasks(tenant_id, owner_id, task_id) ON DELETE CASCADE
);

CREATE TABLE agent_runner_grants (
    grant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    runner TEXT NOT NULL,
    permission_profile TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    workspace_ids_json TEXT NOT NULL DEFAULT '[]',
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, grant_id),
    FOREIGN KEY (tenant_id, owner_id, device_id)
        REFERENCES devices(tenant_id, user_id, device_id)
);

INSERT INTO meetings (
    id, title, state, started_at, ended_at, finalized_at, auto_started,
    minutes_json, raw_transcript_ref, minutes_status, minutes_error,
    display_title, tenant_id, device_id, owner_id, minutes_cleared_at
)
SELECT
    id, title, state, started_at, ended_at, finalized_at, auto_started,
    minutes_json, raw_transcript_ref, minutes_status, minutes_error,
    display_title, tenant_id, device_id, owner_id, minutes_cleared_at
FROM meetings_legacy_global_key;

INSERT INTO meeting_segments (
    id, meeting_id, text, start_ms, end_ms, speaker_id, speaker_label,
    captured_at, tenant_id, device_id, owner_id
)
SELECT
    id, meeting_id, text, start_ms, end_ms, speaker_id, speaker_label,
    captured_at, tenant_id, device_id, owner_id
FROM meeting_segments_legacy_global_key;

INSERT INTO meeting_speaker_labels (
    meeting_id, speaker_id, label, tenant_id, device_id, owner_id
)
SELECT meeting_id, speaker_id, label, tenant_id, device_id, owner_id
FROM meeting_speaker_labels_legacy_global_key;

INSERT INTO workflow_runs (
    run_id, kind, source, state, title, intent_text, meeting_id, todo_id,
    agent_task_id, input_json, output_json, error, timeout_s, created_at,
    started_at, finished_at, updated_at, tenant_id, device_id, owner_id,
    revision, idempotency_key, attempt, parent_run_id, deadline_at,
    cancel_requested_at, active_key
)
SELECT
    run_id, kind, source, state, title, intent_text, meeting_id, todo_id,
    agent_task_id, input_json, output_json, error, timeout_s, created_at,
    started_at, finished_at, updated_at, tenant_id, device_id, owner_id,
    revision, idempotency_key, attempt, parent_run_id, deadline_at,
    cancel_requested_at, active_key
FROM workflow_runs_legacy_global_key;

INSERT INTO workflow_events (
    run_id, seq, event_type, state, visibility, message, payload_json,
    created_at, tenant_id, device_id, owner_id
)
SELECT
    run_id, seq, event_type, state, visibility, message, payload_json,
    created_at, tenant_id, device_id, owner_id
FROM workflow_events_legacy_global_key;

INSERT INTO artifacts (
    artifact_id, artifact_type, title, file_path, mime_type, size_bytes,
    generation_latency_ms, model, metadata_json, run_id, created_at,
    updated_at, tenant_id, device_id, owner_id
)
SELECT
    artifact_id, artifact_type, title, file_path, mime_type, size_bytes,
    generation_latency_ms, model, metadata_json, run_id, created_at,
    updated_at, tenant_id, device_id, owner_id
FROM artifacts_legacy_global_key;

INSERT INTO artifact_links (
    link_id, artifact_id, source, meeting_id, todo_id, run_id, created_at,
    tenant_id, device_id, owner_id
)
SELECT
    link_id, artifact_id, source, meeting_id, todo_id, run_id, created_at,
    tenant_id, device_id, owner_id
FROM artifact_links_legacy_global_key;

INSERT INTO agent_runner_grants (
    grant_id, device_id, runner, permission_profile, permission_mode,
    workspace_ids_json, granted_at, revoked_at, last_used_at,
    tenant_id, owner_id
)
SELECT
    grant_id, device_id, runner, permission_profile, permission_mode,
    workspace_ids_json, granted_at, revoked_at, last_used_at,
    tenant_id, owner_id
FROM agent_runner_grants_legacy_global_key;

INSERT INTO agent_tasks (
    task_id, runner_task_id, device_id, conversation_id, message_id, title,
    intent_text, route, task_kind, state, progress_text, final_text, error,
    artifacts_json, snapshot_json, envelope_json, grant_id,
    permission_profile, last_seq, submitted_at, finished_at, timeout_s,
    workflow_run_id, tenant_id, owner_id
)
SELECT
    task_id, runner_task_id, device_id, conversation_id, message_id, title,
    intent_text, route, task_kind, state, progress_text, final_text, error,
    artifacts_json, snapshot_json, envelope_json, grant_id,
    permission_profile, last_seq, submitted_at, finished_at, timeout_s,
    workflow_run_id, tenant_id, owner_id
FROM agent_tasks_legacy_global_key;

INSERT INTO agent_task_events (
    task_id, seq, event, state, visibility, payload_json, raw_event_hash,
    created_at, tenant_id, device_id, owner_id
)
SELECT
    task_id, seq, event, state, visibility, payload_json, raw_event_hash,
    created_at, tenant_id, device_id, owner_id
FROM agent_task_events_legacy_global_key;

DROP TABLE meeting_segments_legacy_global_key;
DROP TABLE meeting_speaker_labels_legacy_global_key;
DROP TABLE workflow_events_legacy_global_key;
DROP TABLE artifact_links_legacy_global_key;
DROP TABLE artifacts_legacy_global_key;
DROP TABLE agent_task_events_legacy_global_key;
DROP TABLE agent_tasks_legacy_global_key;
DROP TABLE agent_runner_grants_legacy_global_key;
DROP TABLE workflow_runs_legacy_global_key;
DROP TABLE meetings_legacy_global_key;

CREATE INDEX idx_meetings_owner_started
    ON meetings(tenant_id, owner_id, started_at DESC);
CREATE INDEX idx_meetings_owner_minutes_cleared
    ON meetings(tenant_id, owner_id, minutes_cleared_at);

CREATE INDEX idx_meeting_segments_meeting
    ON meeting_segments(tenant_id, owner_id, meeting_id, start_ms);
CREATE INDEX idx_meeting_segments_owner_meeting
    ON meeting_segments(tenant_id, owner_id, meeting_id, id);
CREATE INDEX idx_meeting_speaker_labels_owner_meeting
    ON meeting_speaker_labels(tenant_id, owner_id, meeting_id, speaker_id);

CREATE INDEX idx_workflow_runs_state
    ON workflow_runs(tenant_id, owner_id, state, updated_at);
CREATE INDEX idx_workflow_runs_meeting
    ON workflow_runs(tenant_id, owner_id, meeting_id, created_at);
CREATE INDEX idx_workflow_runs_parent
    ON workflow_runs(tenant_id, owner_id, parent_run_id);
CREATE INDEX idx_workflow_runs_todo
    ON workflow_runs(tenant_id, owner_id, meeting_id, todo_id, created_at);
CREATE INDEX idx_workflow_runs_agent_task
    ON workflow_runs(tenant_id, owner_id, agent_task_id);
CREATE INDEX idx_workflow_runs_owner_state
    ON workflow_runs(tenant_id, owner_id, state, updated_at);
CREATE UNIQUE INDEX idx_workflow_idempotency
    ON workflow_runs(tenant_id, owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX idx_workflow_active_key
    ON workflow_runs(tenant_id, owner_id, active_key)
    WHERE active_key IS NOT NULL
      AND state IN ('pending', 'running', 'cancel_requested');

CREATE INDEX idx_workflow_events_run
    ON workflow_events(tenant_id, owner_id, run_id, seq);
CREATE INDEX idx_workflow_events_type
    ON workflow_events(tenant_id, owner_id, event_type, created_at);
CREATE INDEX idx_workflow_events_owner_run
    ON workflow_events(tenant_id, owner_id, run_id, seq);

CREATE INDEX idx_artifacts_created
    ON artifacts(tenant_id, owner_id, created_at);
CREATE INDEX idx_artifacts_owner_created
    ON artifacts(tenant_id, owner_id, created_at DESC);
CREATE INDEX idx_artifacts_run
    ON artifacts(tenant_id, owner_id, run_id);

CREATE INDEX idx_artifact_links_meeting
    ON artifact_links(tenant_id, owner_id, meeting_id, created_at);
CREATE INDEX idx_artifact_links_todo
    ON artifact_links(tenant_id, owner_id, meeting_id, todo_id, created_at);
CREATE INDEX idx_artifact_links_run
    ON artifact_links(tenant_id, owner_id, run_id);
CREATE INDEX idx_artifact_links_owner_meeting
    ON artifact_links(tenant_id, owner_id, meeting_id, created_at);
CREATE UNIQUE INDEX idx_artifact_links_dedupe
    ON artifact_links(
        tenant_id,
        owner_id,
        artifact_id,
        source,
        COALESCE(meeting_id, ''),
        COALESCE(todo_id, ''),
        COALESCE(run_id, '')
    );

CREATE INDEX idx_agent_tasks_device_state
    ON agent_tasks(tenant_id, owner_id, device_id, state, submitted_at);
CREATE INDEX idx_agent_tasks_owner_state
    ON agent_tasks(tenant_id, owner_id, device_id, state, submitted_at);
CREATE INDEX idx_agent_tasks_runner
    ON agent_tasks(tenant_id, owner_id, runner_task_id);
CREATE INDEX idx_agent_tasks_workflow_run
    ON agent_tasks(tenant_id, owner_id, workflow_run_id);
CREATE INDEX idx_agent_tasks_grant
    ON agent_tasks(tenant_id, owner_id, grant_id);

CREATE INDEX idx_agent_task_events_task
    ON agent_task_events(tenant_id, owner_id, task_id, seq);
CREATE UNIQUE INDEX idx_agent_task_events_raw
    ON agent_task_events(tenant_id, owner_id, task_id, raw_event_hash)
    WHERE raw_event_hash IS NOT NULL;
CREATE INDEX idx_agent_task_events_owner_task
    ON agent_task_events(tenant_id, owner_id, task_id, seq);

CREATE INDEX idx_agent_runner_grants_active
    ON agent_runner_grants(tenant_id, owner_id, device_id, runner, revoked_at, granted_at);
CREATE INDEX idx_agent_runner_grants_owner_active
    ON agent_runner_grants(tenant_id, owner_id, device_id, runner, revoked_at, granted_at);
CREATE UNIQUE INDEX idx_agent_runner_grants_owner_active_unique
    ON agent_runner_grants(tenant_id, owner_id, device_id, runner)
    WHERE revoked_at IS NULL;
