-- Ancient crash rows are recovered once globally instead of being copied into
-- every newly registered process consumer.  Registration advances only this
-- constant-size watermark.  A short logical lease prevents multiple backend
-- processes from scanning the same ancient range concurrently.
CREATE TABLE workflow_outbox_global_recovery_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    recovery_through_outbox_id INTEGER NOT NULL DEFAULT 0
        CHECK(recovery_through_outbox_id >= 0),
    scan_cursor_outbox_id INTEGER NOT NULL DEFAULT 0
        CHECK(scan_cursor_outbox_id >= 0),
    lease_owner TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK(lease_fence >= 0),
    lease_expires_at REAL NOT NULL DEFAULT 0 CHECK(lease_expires_at >= 0),
    last_failed_owner TEXT,
    failed_owner_retry_at REAL NOT NULL DEFAULT 0 CHECK(failed_owner_retry_at >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO workflow_outbox_global_recovery_state (
    singleton,
    recovery_through_outbox_id,
    scan_cursor_outbox_id,
    lease_owner,
    lease_fence,
    lease_expires_at,
    last_failed_owner,
    failed_owner_retry_at
)
SELECT 1, COALESCE(MAX(cursor_outbox_id), 0), 0, NULL, 0, 0, NULL, 0
FROM workflow_outbox_consumers;

-- A failed ancient scope is represented by one ordered watermark regardless
-- of how many later rows that scope owns.  Healthy scopes remain directly
-- scannable while this lane observes durable retry backoff.
CREATE TABLE workflow_outbox_global_scope_recovery (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    next_outbox_id INTEGER NOT NULL CHECK(next_outbox_id > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at >= 0),
    last_error TEXT,
    PRIMARY KEY (tenant_id, owner_id)
);

CREATE INDEX idx_workflow_outbox_global_scope_recovery_due
    ON workflow_outbox_global_scope_recovery(next_retry_at, next_outbox_id);

CREATE INDEX idx_workflow_outbox_global_scope_recovery_row
    ON workflow_outbox_global_scope_recovery(
        tenant_id,
        owner_id,
        next_outbox_id
    );
