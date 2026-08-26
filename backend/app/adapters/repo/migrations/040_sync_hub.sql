-- 040_sync_hub.sql
-- Minimal durable state for same-user multi-device synchronization.

CREATE TABLE IF NOT EXISTS sync_devices (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    sync_token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (tenant_id, owner_id, device_id),
    FOREIGN KEY (tenant_id, owner_id, device_id)
        REFERENCES devices(tenant_id, user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_devices_owner_active
    ON sync_devices(tenant_id, owner_id, revoked_at, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS device_pairings (
    pairing_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    pairing_code_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_device_id TEXT,
    FOREIGN KEY (tenant_id, owner_id, source_device_id)
        REFERENCES devices(tenant_id, user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_device_pairings_expiry
    ON device_pairings(pairing_code_hash, expires_at, claimed_at);

CREATE TABLE IF NOT EXISTS sync_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'transcript_segment', 'meeting_summary', 'memory'
    )),
    entity_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied', 'duplicate', 'conflict')),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    cursor INTEGER,
    current_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_operations_owner_entity
    ON sync_operations(tenant_id, owner_id, entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sync_events (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    cursor INTEGER NOT NULL CHECK(cursor > 0),
    source_device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'transcript_segment', 'meeting_summary', 'memory'
    )),
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, cursor)
);

CREATE INDEX IF NOT EXISTS idx_sync_events_owner_entity
    ON sync_events(tenant_id, owner_id, entity_type, entity_id, cursor);

CREATE TABLE IF NOT EXISTS device_cursors (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0 CHECK(cursor >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, device_id),
    FOREIGN KEY (tenant_id, owner_id, device_id)
        REFERENCES sync_devices(tenant_id, owner_id, device_id)
        ON DELETE CASCADE
);
