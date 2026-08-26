-- 017_meeting_minutes_tombstone.sql
-- Distinguish an intentional user clear from a legacy/stuck empty minutes row.

ALTER TABLE meetings ADD COLUMN minutes_cleared_at TEXT;

CREATE INDEX idx_meetings_owner_minutes_cleared
    ON meetings(tenant_id, owner_id, minutes_cleared_at);
