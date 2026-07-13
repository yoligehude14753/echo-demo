-- Durable fenced execution leases shared by workflow and agent runtimes.
CREATE TABLE IF NOT EXISTS execution_leases (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) > 0),
    owner_id TEXT NOT NULL CHECK(length(owner_id) > 0),
    resource_kind TEXT NOT NULL CHECK(length(resource_kind) > 0),
    resource_id TEXT NOT NULL CHECK(length(resource_id) > 0),
    holder_id TEXT NOT NULL CHECK(length(holder_id) > 0),
    fence_token INTEGER NOT NULL CHECK(fence_token >= 1),
    expires_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, resource_kind, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_leases_expiry
    ON execution_leases(expires_at);
