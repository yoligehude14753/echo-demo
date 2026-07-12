-- 027_ambient_audio_lifecycle.sql
-- Durable owner-scoped inventory for ambient WAV lifecycle, byte accounting,
-- retention/capacity GC and quota compensation.  The filesystem remains the
-- blob store; this table records only server-authored paths under one scope.

CREATE TABLE IF NOT EXISTS ambient_audio_files (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    audio_ref TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    captured_at TEXT NOT NULL,
    quota_charged INTEGER NOT NULL DEFAULT 0 CHECK(quota_charged IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, audio_ref)
);

CREATE INDEX IF NOT EXISTS idx_ambient_audio_owner_captured
    ON ambient_audio_files(tenant_id, owner_id, captured_at, audio_ref);
