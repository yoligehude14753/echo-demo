-- Expand the sync entity catalog for legacy desktop history and artifacts.
-- The rebuilds preserve every existing row and keep the original primary keys
-- and indexes; no meeting, segment, audio, or artifact business row is reset.

DROP INDEX IF EXISTS idx_sync_operations_owner_entity;
ALTER TABLE sync_operations RENAME TO sync_operations_legacy_046;
CREATE TABLE sync_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'meeting', 'transcript_segment', 'meeting_summary', 'artifact', 'memory'
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
INSERT INTO sync_operations (
    operation_id, tenant_id, owner_id, source_device_id, entity_type,
    entity_id, base_revision, updated_at, payload_json, status, revision,
    cursor, current_json, created_at
)
SELECT operation_id, tenant_id, owner_id, source_device_id, entity_type,
       entity_id, base_revision, updated_at, payload_json, status, revision,
       cursor, current_json, created_at
FROM sync_operations_legacy_046;
DROP TABLE sync_operations_legacy_046;
CREATE INDEX idx_sync_operations_owner_entity
    ON sync_operations(tenant_id, owner_id, entity_type, entity_id, created_at DESC);

DROP INDEX IF EXISTS idx_sync_events_owner_entity;
ALTER TABLE sync_events RENAME TO sync_events_legacy_046;
CREATE TABLE sync_events (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    cursor INTEGER NOT NULL CHECK(cursor > 0),
    source_device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'meeting', 'transcript_segment', 'meeting_summary', 'artifact', 'memory'
    )),
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, cursor)
);
INSERT INTO sync_events (
    tenant_id, owner_id, cursor, source_device_id, entity_type, entity_id,
    revision, updated_at, payload_json
)
SELECT tenant_id, owner_id, cursor, source_device_id, entity_type, entity_id,
       revision, updated_at, payload_json
FROM sync_events_legacy_046;
DROP TABLE sync_events_legacy_046;
CREATE INDEX idx_sync_events_owner_entity
    ON sync_events(tenant_id, owner_id, entity_type, entity_id, cursor);

DROP INDEX IF EXISTS idx_hub_sync_outbox_pending;
ALTER TABLE hub_sync_outbox RENAME TO hub_sync_outbox_legacy_046;
CREATE TABLE hub_sync_outbox (
    operation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'meeting', 'transcript_segment', 'meeting_summary', 'artifact', 'memory'
    )),
    entity_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN (
        'pending', 'sending', 'applied', 'duplicate', 'conflict', 'failed'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    state_updated_at TEXT NOT NULL
);
INSERT INTO hub_sync_outbox (
    operation_id, device_id, entity_type, entity_id, base_revision,
    updated_at, payload_json, state, attempts, last_error, created_at,
    state_updated_at
)
SELECT operation_id, device_id, entity_type, entity_id, base_revision,
       updated_at, payload_json, state, attempts, last_error, created_at,
       state_updated_at
FROM hub_sync_outbox_legacy_046;
DROP TABLE hub_sync_outbox_legacy_046;
CREATE INDEX idx_hub_sync_outbox_pending
    ON hub_sync_outbox(state, created_at);

ALTER TABLE hub_sync_entities RENAME TO hub_sync_entities_legacy_046;
CREATE TABLE hub_sync_entities (
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'meeting', 'transcript_segment', 'meeting_summary', 'artifact', 'memory'
    )),
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    operation_id TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
INSERT INTO hub_sync_entities (
    entity_type, entity_id, revision, updated_at, payload_json,
    source_device_id, operation_id
)
SELECT entity_type, entity_id, revision, updated_at, payload_json,
       source_device_id, operation_id
FROM hub_sync_entities_legacy_046;
DROP TABLE hub_sync_entities_legacy_046;
CREATE INDEX idx_hub_sync_entities_payload
    ON hub_sync_entities(entity_type, payload_json);
