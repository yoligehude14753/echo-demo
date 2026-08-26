-- 021_principal_quota_ledger.sql
-- Public-backend quota usage is durable across process restarts.  Concurrency
-- leases remain process-local, while cumulative request/upload/storage/LLM
-- budgets are accounted here per server-authored principal scope.

CREATE TABLE IF NOT EXISTS principal_quota_ledger (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    metric TEXT NOT NULL CHECK(metric IN (
        'requests',
        'upload_bytes',
        'storage_bytes',
        'llm_tokens'
    )),
    window_key TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK(used >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, metric, window_key),
    FOREIGN KEY (tenant_id, owner_id)
        REFERENCES users(tenant_id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_principal_quota_ledger_updated
    ON principal_quota_ledger(metric, window_key, updated_at);
