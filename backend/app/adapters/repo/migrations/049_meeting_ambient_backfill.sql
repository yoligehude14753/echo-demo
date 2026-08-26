-- Preserve the durable source of transcript rows copied from ambient history.
-- The scoped unique index makes auto-meeting backfill idempotent across retries.

ALTER TABLE meeting_segments ADD COLUMN source_ambient_segment_id INTEGER;

CREATE UNIQUE INDEX idx_meeting_segment_ambient_source
    ON meeting_segments(
        tenant_id, owner_id, device_id, source_ambient_segment_id
    )
    WHERE source_ambient_segment_id IS NOT NULL;
