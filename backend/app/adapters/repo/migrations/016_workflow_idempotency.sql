-- 016_workflow_idempotency.sql
-- Idempotency survives terminal completion and lost HTTP responses.

-- Older builds only prevented duplicate *active* runs.  Preserve the first
-- authoritative run for each key and detach any historical duplicates before
-- replacing the partial index with a permanent owner-scoped unique index.
UPDATE workflow_runs
SET idempotency_key = NULL
WHERE idempotency_key IS NOT NULL
  AND rowid NOT IN (
      SELECT MIN(rowid)
      FROM workflow_runs
      WHERE idempotency_key IS NOT NULL
      GROUP BY tenant_id, owner_id, idempotency_key
  );

DROP INDEX IF EXISTS idx_workflow_active_idempotency;

ALTER TABLE workflow_runs ADD COLUMN active_key TEXT;

CREATE UNIQUE INDEX idx_workflow_idempotency
    ON workflow_runs(tenant_id, owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX idx_workflow_active_key
    ON workflow_runs(tenant_id, owner_id, active_key)
    WHERE active_key IS NOT NULL
      AND state IN ('pending', 'running', 'cancel_requested');
