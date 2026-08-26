-- Operation idempotency is scoped to the authenticated sync owner.
-- A legacy client may legitimately reuse a deterministic operation id after
-- its data is paired into a different owner scope. Keep every historical row
-- and only treat the tuple (tenant_id, owner_id, operation_id) as idempotent.

DROP INDEX IF EXISTS idx_sync_operations_owner_entity;
ALTER TABLE sync_operations RENAME TO sync_operations_legacy_047;
CREATE TABLE sync_operations (
    operation_id TEXT NOT NULL,
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
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, operation_id)
);
INSERT INTO sync_operations (
    operation_id, tenant_id, owner_id, source_device_id, entity_type,
    entity_id, base_revision, updated_at, payload_json, status, revision,
    cursor, current_json, created_at
)
SELECT operation_id, tenant_id, owner_id, source_device_id, entity_type,
       entity_id, base_revision, updated_at, payload_json, status, revision,
       cursor, current_json, created_at
FROM sync_operations_legacy_047;
DROP TABLE sync_operations_legacy_047;
CREATE INDEX idx_sync_operations_owner_entity
    ON sync_operations(tenant_id, owner_id, entity_type, entity_id, created_at DESC);
