-- Per-consumer retry state lets one unavailable principal lane defer without
-- blocking unrelated scopes in the process-local WebSocket projection.
ALTER TABLE workflow_outbox_consumer_recovery
    ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0);
ALTER TABLE workflow_outbox_consumer_recovery
    ADD COLUMN next_retry_at REAL NOT NULL DEFAULT 0 CHECK(next_retry_at >= 0);
ALTER TABLE workflow_outbox_consumer_recovery
    ADD COLUMN last_error TEXT;

CREATE INDEX idx_workflow_outbox_consumer_recovery_due
    ON workflow_outbox_consumer_recovery(consumer_id, next_retry_at, outbox_id);
