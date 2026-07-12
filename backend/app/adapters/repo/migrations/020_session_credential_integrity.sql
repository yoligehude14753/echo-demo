-- 020_session_credential_integrity.sql
-- Bind access sessions and narrow resource tickets to the authoritative
-- tenant/user/device/family tuple. Session and credential rotation is atomic:
-- one family can expose at most one non-revoked bearer of each kind.

ALTER TABLE resource_tickets RENAME TO resource_tickets_legacy_unscoped_fk;
ALTER TABLE principal_sessions RENAME TO principal_sessions_legacy_unscoped_fk;

CREATE TABLE principal_sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('public')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    family_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    renewed_from_session_id TEXT,
    UNIQUE (session_id, tenant_id, owner_id, device_id),
    FOREIGN KEY (family_id, tenant_id, owner_id, device_id)
        REFERENCES session_families(family_id, tenant_id, user_id, device_id),
    FOREIGN KEY (tenant_id, owner_id, device_id)
        REFERENCES devices(tenant_id, user_id, device_id),
    FOREIGN KEY (renewed_from_session_id)
        REFERENCES principal_sessions(session_id) ON DELETE SET NULL
);

CREATE TABLE resource_tickets (
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
    FOREIGN KEY (session_id, tenant_id, owner_id, device_id)
        REFERENCES principal_sessions(session_id, tenant_id, owner_id, device_id)
        ON DELETE CASCADE
);

INSERT INTO principal_sessions (
    session_id, token_hash, tenant_id, device_id, owner_id, mode, issued_at,
    expires_at, revoked_at, family_id, generation, renewed_from_session_id
)
SELECT
    session_id, token_hash, tenant_id, device_id, owner_id, mode, issued_at,
    expires_at, revoked_at, family_id, generation, renewed_from_session_id
FROM principal_sessions_legacy_unscoped_fk;

INSERT INTO resource_tickets (
    ticket_id, token_hash, session_id, tenant_id, device_id, owner_id,
    resource_type, resource_id, capability, issued_at, expires_at, revoked_at
)
SELECT
    ticket_id, token_hash, session_id, tenant_id, device_id, owner_id,
    resource_type, resource_id, capability, issued_at, expires_at, revoked_at
FROM resource_tickets_legacy_unscoped_fk;

DROP TABLE resource_tickets_legacy_unscoped_fk;
DROP TABLE principal_sessions_legacy_unscoped_fk;

CREATE INDEX idx_principal_sessions_owner_expiry
    ON principal_sessions(tenant_id, owner_id, expires_at, revoked_at);
CREATE INDEX idx_principal_sessions_device
    ON principal_sessions(tenant_id, owner_id, device_id, revoked_at);
CREATE INDEX idx_principal_sessions_family_active
    ON principal_sessions(family_id, revoked_at, expires_at, generation);
CREATE UNIQUE INDEX idx_principal_sessions_one_active_family
    ON principal_sessions(family_id)
    WHERE revoked_at IS NULL;

CREATE INDEX idx_resource_tickets_lookup
    ON resource_tickets(
        tenant_id, owner_id, resource_type, resource_id, expires_at, revoked_at
    );
CREATE INDEX idx_resource_tickets_owner
    ON resource_tickets(tenant_id, owner_id, expires_at, revoked_at);

CREATE UNIQUE INDEX idx_device_credentials_one_active_family
    ON device_credentials(family_id)
    WHERE revoked_at IS NULL;
