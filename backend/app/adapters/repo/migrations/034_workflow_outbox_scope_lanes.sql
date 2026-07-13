-- Compact recovery state by consumer/principal scope.  One unavailable scope
-- may accumulate any number of ordered outbox rows without consuming one
-- recovery record per event or blocking the global consumer cursor.
CREATE TABLE workflow_outbox_consumer_scope_recovery (
    consumer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    next_outbox_id INTEGER NOT NULL CHECK(next_outbox_id > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at >= 0),
    last_error TEXT,
    PRIMARY KEY (consumer_id, tenant_id, owner_id),
    FOREIGN KEY (consumer_id)
        REFERENCES workflow_outbox_consumers(consumer_id) ON DELETE CASCADE
);

CREATE INDEX idx_workflow_outbox_scope_recovery_due
    ON workflow_outbox_consumer_scope_recovery(
        consumer_id,
        next_retry_at,
        next_outbox_id
    );

CREATE INDEX idx_workflow_outbox_scope_recovery_row
    ON workflow_outbox_consumer_scope_recovery(
        tenant_id,
        owner_id,
        next_outbox_id,
        consumer_id
    );
