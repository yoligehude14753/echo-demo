-- 012_principals.sql
-- 公共后端身份与资源隔离基础：服务端签发 opaque session，并给所有持久资源
-- 增加 tenant/device/owner scope。存量 local-first 数据统一归入 legacy-local。

CREATE TABLE IF NOT EXISTS principal_sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('public')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_principal_sessions_owner_expiry
    ON principal_sessions(tenant_id, owner_id, expires_at, revoked_at);
CREATE INDEX IF NOT EXISTS idx_principal_sessions_device
    ON principal_sessions(tenant_id, device_id, revoked_at);

ALTER TABLE meetings ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meetings ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meetings ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE meeting_segments ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meeting_segments ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meeting_segments ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE meeting_speaker_labels ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meeting_speaker_labels ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE meeting_speaker_labels ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE ambient_segments ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE ambient_segments ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE ambient_segments ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE speakers ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE speakers ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE speakers ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE workflow_runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE workflow_runs ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE workflow_runs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE workflow_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE workflow_events ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE workflow_events ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE artifacts ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE artifacts ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE artifacts ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE artifact_links ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE artifact_links ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE artifact_links ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE agent_tasks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE agent_tasks ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';
UPDATE agent_tasks SET device_id = 'legacy-local';

ALTER TABLE agent_task_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE agent_task_events ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE agent_task_events ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';

ALTER TABLE agent_runner_grants ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy-local';
ALTER TABLE agent_runner_grants ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy-local';
-- 多个历史 device 可能各自留有 active grant。归一到 fixed local principal 前，
-- 每个 runner 只保留最新一条 active grant，避免 owner-aware 唯一索引迁移失败。
UPDATE agent_runner_grants AS legacy_grant
SET revoked_at = CURRENT_TIMESTAMP
WHERE legacy_grant.revoked_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM agent_runner_grants AS newer_grant
      WHERE newer_grant.revoked_at IS NULL
        AND newer_grant.runner = legacy_grant.runner
        AND (
            newer_grant.granted_at > legacy_grant.granted_at
            OR (
                newer_grant.granted_at = legacy_grant.granted_at
                AND newer_grant.grant_id > legacy_grant.grant_id
            )
        )
  );
UPDATE agent_runner_grants SET device_id = 'legacy-local';

-- BM25 正文仍是可重建文件；这张表保存 owner-aware manifest，后续单 writer
-- 用它做 source_path 去重、原子替换与重建审计。
CREATE TABLE IF NOT EXISTS rag_documents (
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'upload',
    source_path TEXT,
    index_path TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_meetings_owner_started
    ON meetings(tenant_id, owner_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_segments_owner_meeting
    ON meeting_segments(tenant_id, owner_id, meeting_id, id);
CREATE INDEX IF NOT EXISTS idx_meeting_speaker_labels_owner_meeting
    ON meeting_speaker_labels(tenant_id, owner_id, meeting_id, speaker_id);
CREATE INDEX IF NOT EXISTS idx_ambient_segments_owner_captured
    ON ambient_segments(tenant_id, owner_id, captured_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_speakers_owner_speaker_unique
    ON speakers(tenant_id, owner_id, speaker_id);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_owner_state
    ON workflow_runs(tenant_id, owner_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_events_owner_run
    ON workflow_events(tenant_id, owner_id, run_id, seq);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner_created
    ON artifacts(tenant_id, owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_links_owner_meeting
    ON artifact_links(tenant_id, owner_id, meeting_id, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner_state
    ON agent_tasks(tenant_id, owner_id, device_id, state, submitted_at);
CREATE INDEX IF NOT EXISTS idx_agent_task_events_owner_task
    ON agent_task_events(tenant_id, owner_id, task_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_runner_grants_owner_active
    ON agent_runner_grants(tenant_id, owner_id, device_id, runner, revoked_at, granted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runner_grants_owner_active_unique
    ON agent_runner_grants(tenant_id, owner_id, device_id, runner)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_rag_documents_owner_source
    ON rag_documents(tenant_id, owner_id, source, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_documents_owner_source_path_unique
    ON rag_documents(tenant_id, owner_id, source_path)
    WHERE source_path IS NOT NULL;
