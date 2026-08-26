-- Durable capture idempotency without copying audio, transcript, or request payloads.
-- Existing rows remain untouched: new canonical correlation columns are nullable.

ALTER TABLE ambient_segments ADD COLUMN capture_operation_key TEXT;
ALTER TABLE ambient_segments ADD COLUMN capture_request_fingerprint TEXT;

CREATE UNIQUE INDEX idx_ambient_capture_operation
    ON ambient_segments(
        tenant_id, owner_id, device_id, capture_operation_key
    )
    WHERE capture_operation_key IS NOT NULL;

ALTER TABLE meeting_segments ADD COLUMN capture_operation_key TEXT;
ALTER TABLE meeting_segments ADD COLUMN capture_segment_ordinal INTEGER;
ALTER TABLE meeting_segments ADD COLUMN capture_request_fingerprint TEXT;

CREATE UNIQUE INDEX idx_meeting_segment_capture_operation
    ON meeting_segments(
        tenant_id, owner_id, device_id,
        capture_operation_key, capture_segment_ordinal
    )
    WHERE capture_operation_key IS NOT NULL;

CREATE TABLE capture_request_receipts (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) > 0),
    owner_id TEXT NOT NULL CHECK(length(owner_id) > 0),
    device_id TEXT NOT NULL CHECK(length(device_id) > 0),
    operation_key_hash TEXT NOT NULL CHECK(length(operation_key_hash) = 64),
    client_segment_hash TEXT NOT NULL CHECK(length(client_segment_hash) = 64),
    idempotency_key_hash TEXT CHECK(
        idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64
    ),
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    state TEXT NOT NULL CHECK(state IN ('processing', 'completed')),
    lease_holder TEXT NOT NULL CHECK(length(lease_holder) > 0),
    lease_fence INTEGER NOT NULL CHECK(lease_fence >= 1),
    lease_expires_at REAL NOT NULL,
    ambient_stored INTEGER NOT NULL DEFAULT 0 CHECK(ambient_stored IN (0, 1)),
    ambient_segment_id INTEGER CHECK(
        ambient_segment_id IS NULL OR ambient_segment_id > 0
    ),
    meeting_id TEXT,
    stt_status TEXT,
    capture_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (tenant_id, owner_id, device_id, operation_key_hash)
);

CREATE UNIQUE INDEX idx_capture_receipt_client_segment
    ON capture_request_receipts(
        tenant_id, owner_id, device_id, client_segment_hash
    );

CREATE UNIQUE INDEX idx_capture_receipt_idempotency_key
    ON capture_request_receipts(
        tenant_id, owner_id, device_id, idempotency_key_hash
    )
    WHERE idempotency_key_hash IS NOT NULL;

CREATE INDEX idx_capture_receipt_processing_expiry
    ON capture_request_receipts(state, lease_expires_at)
    WHERE state = 'processing';
