-- 015_resource_tickets.sql
-- Narrow, expiring bearer links for public meeting shares and artifact downloads.
-- Only hashes are persisted; a ticket is bound to one owner and one resource.

CREATE TABLE IF NOT EXISTS resource_tickets (
    ticket_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('meeting', 'artifact')),
    resource_id TEXT NOT NULL,
    capability TEXT NOT NULL CHECK(capability = 'read'),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(session_id) REFERENCES principal_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_tickets_lookup
    ON resource_tickets(resource_type, resource_id, expires_at, revoked_at);
CREATE INDEX IF NOT EXISTS idx_resource_tickets_owner
    ON resource_tickets(tenant_id, owner_id, expires_at, revoked_at);
