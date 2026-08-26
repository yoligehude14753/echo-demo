-- Desktop Hub sync state: durable outbox, entity revisions and received ops.
-- Payloads are the three user data entities supported by v0.3.3.

CREATE TABLE IF NOT EXISTS hub_sync_outbox (
    operation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'transcript_segment', 'meeting_summary', 'memory'
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

CREATE INDEX IF NOT EXISTS idx_hub_sync_outbox_pending
    ON hub_sync_outbox(state, created_at);

CREATE TABLE IF NOT EXISTS hub_sync_entities (
    entity_type TEXT NOT NULL CHECK(entity_type IN (
        'transcript_segment', 'meeting_summary', 'memory'
    )),
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    operation_id TEXT,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_hub_sync_entities_payload
    ON hub_sync_entities(entity_type, payload_json);

CREATE TABLE IF NOT EXISTS hub_sync_applied_operations (
    operation_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

