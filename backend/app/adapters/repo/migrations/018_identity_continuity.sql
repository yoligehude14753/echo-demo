-- 018_identity_continuity.sql
-- Stable public identity: tenant -> user -> device -> credential/session family.
-- Existing opaque sessions remain valid and are backfilled into one legacy family
-- each. They can later claim a durable device credential without changing scope.

CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suspended')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE users (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suspended')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE devices (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    legacy_claimed_at TEXT,
    revoked_at TEXT,
    PRIMARY KEY (tenant_id, user_id, device_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, user_id)
);

CREATE TABLE session_families (
    family_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_renewed_at TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    FOREIGN KEY (tenant_id, user_id, device_id)
        REFERENCES devices(tenant_id, user_id, device_id),
    UNIQUE (family_id, tenant_id, user_id, device_id)
);

CREATE TABLE device_credentials (
    credential_id TEXT PRIMARY KEY,
    credential_hash TEXT NOT NULL UNIQUE,
    family_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT,
    rotated_to_credential_id TEXT,
    FOREIGN KEY (tenant_id, user_id, device_id)
        REFERENCES devices(tenant_id, user_id, device_id),
    FOREIGN KEY (family_id, tenant_id, user_id, device_id)
        REFERENCES session_families(family_id, tenant_id, user_id, device_id),
    FOREIGN KEY (rotated_to_credential_id) REFERENCES device_credentials(credential_id)
);

CREATE TABLE public_enrollments (
    enrollment_id_hash TEXT PRIMARY KEY,
    device_secret_hash TEXT NOT NULL,
    peer_key_hash TEXT NOT NULL,
    family_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (family_id, tenant_id, user_id, device_id)
        REFERENCES session_families(family_id, tenant_id, user_id, device_id)
);

ALTER TABLE principal_sessions ADD COLUMN family_id TEXT;
ALTER TABLE principal_sessions ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE principal_sessions ADD COLUMN renewed_from_session_id TEXT;

INSERT INTO tenants (tenant_id, status, created_at, updated_at)
VALUES ('legacy-local', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

INSERT INTO users (tenant_id, user_id, status, created_at, updated_at)
VALUES ('legacy-local', 'legacy-local', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

INSERT INTO devices (
    tenant_id, user_id, device_id, display_name, created_at, last_seen_at
)
VALUES (
    'legacy-local', 'legacy-local', 'legacy-local', 'Legacy local device',
    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
);

INSERT INTO tenants (tenant_id, status, created_at, updated_at)
SELECT tenant_id, 'active', MIN(issued_at), MAX(issued_at)
FROM principal_sessions
GROUP BY tenant_id
ON CONFLICT(tenant_id) DO NOTHING;

INSERT INTO users (tenant_id, user_id, status, created_at, updated_at)
SELECT tenant_id, owner_id, 'active', MIN(issued_at), MAX(issued_at)
FROM principal_sessions
GROUP BY tenant_id, owner_id
ON CONFLICT(tenant_id, user_id) DO NOTHING;

INSERT INTO devices (
    tenant_id, user_id, device_id, display_name, created_at, last_seen_at
)
SELECT tenant_id, owner_id, device_id, NULL, MIN(issued_at), MAX(issued_at)
FROM principal_sessions
GROUP BY tenant_id, owner_id, device_id
ON CONFLICT(tenant_id, user_id, device_id) DO NOTHING;

INSERT INTO session_families (
    family_id, tenant_id, user_id, device_id, created_at, last_renewed_at,
    generation, revoked_at
)
SELECT
    'family:legacy:' || session_id,
    tenant_id,
    owner_id,
    device_id,
    issued_at,
    issued_at,
    0,
    revoked_at
FROM principal_sessions;

UPDATE principal_sessions
SET family_id = 'family:legacy:' || session_id
WHERE family_id IS NULL;

CREATE INDEX idx_devices_user_active
    ON devices(tenant_id, user_id, revoked_at, last_seen_at);
CREATE INDEX idx_session_families_device_active
    ON session_families(tenant_id, user_id, device_id, revoked_at, last_renewed_at);
CREATE INDEX idx_device_credentials_device_active
    ON device_credentials(tenant_id, user_id, device_id, family_id, revoked_at, expires_at);
CREATE INDEX idx_public_enrollments_peer
    ON public_enrollments(peer_key_hash, created_at);
CREATE INDEX idx_principal_sessions_family_active
    ON principal_sessions(family_id, revoked_at, expires_at, generation);
