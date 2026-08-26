-- Per-process workflow outbox cursors and crash-row recovery snapshots.
CREATE TABLE workflow_outbox_consumers (
    consumer_id TEXT PRIMARY KEY,
    cursor_outbox_id INTEGER NOT NULL DEFAULT 0 CHECK(cursor_outbox_id >= 0),
    started_at TEXT NOT NULL,
    heartbeat_at REAL NOT NULL
);

CREATE INDEX idx_workflow_outbox_consumers_heartbeat
    ON workflow_outbox_consumers(heartbeat_at, cursor_outbox_id);

CREATE TABLE workflow_outbox_consumer_recovery (
    consumer_id TEXT NOT NULL,
    outbox_id INTEGER NOT NULL,
    PRIMARY KEY (consumer_id, outbox_id),
    FOREIGN KEY (consumer_id)
        REFERENCES workflow_outbox_consumers(consumer_id) ON DELETE CASCADE,
    FOREIGN KEY (outbox_id)
        REFERENCES workflow_outbox(outbox_id) ON DELETE CASCADE
);

CREATE INDEX idx_workflow_outbox_consumer_recovery_row
    ON workflow_outbox_consumer_recovery(outbox_id, consumer_id);
