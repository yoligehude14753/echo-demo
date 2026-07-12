-- 014_workflow_kernel.sql
-- CAS/idempotency/deadline/retry metadata and transactional outbox.

ALTER TABLE workflow_runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN idempotency_key TEXT;
ALTER TABLE workflow_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE workflow_runs ADD COLUMN parent_run_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN deadline_at TEXT;
ALTER TABLE workflow_runs ADD COLUMN cancel_requested_at TEXT;

CREATE UNIQUE INDEX idx_workflow_active_idempotency
    ON workflow_runs(tenant_id, owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND state IN ('pending', 'running', 'cancel_requested');

CREATE TABLE workflow_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX idx_workflow_outbox_pending
    ON workflow_outbox(published_at, outbox_id);
CREATE INDEX idx_workflow_outbox_owner
    ON workflow_outbox(tenant_id, owner_id, outbox_id);
