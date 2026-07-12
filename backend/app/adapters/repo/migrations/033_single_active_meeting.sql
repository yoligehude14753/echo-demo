-- A principal can own at most one active meeting.  Older releases enforced
-- this only in the process-local MeetingState singleton, so two backend
-- processes could both observe idle and persist different active meetings.

CREATE TABLE meeting_state_migration_audit (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    authoritative_meeting_id TEXT NOT NULL,
    prior_state TEXT NOT NULL,
    next_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, meeting_id, reason)
);

-- Keep the most recently started row.  id DESC is the deterministic tie
-- breaker when legacy rows share the same started_at timestamp.
WITH ranked AS (
    SELECT
        tenant_id,
        owner_id,
        id,
        FIRST_VALUE(id) OVER (
            PARTITION BY tenant_id, owner_id
            ORDER BY started_at DESC, id DESC
        ) AS authoritative_meeting_id,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id, owner_id
            ORDER BY started_at DESC, id DESC
        ) AS active_rank
    FROM meetings
    WHERE state = 'in_meeting'
)
INSERT OR IGNORE INTO meeting_state_migration_audit (
    tenant_id,
    owner_id,
    meeting_id,
    authoritative_meeting_id,
    prior_state,
    next_state,
    reason
)
SELECT
    tenant_id,
    owner_id,
    id,
    authoritative_meeting_id,
    'in_meeting',
    'ended',
    'duplicate_active_meeting'
FROM ranked
WHERE active_rank > 1;

WITH ranked AS (
    SELECT
        tenant_id,
        owner_id,
        id,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id, owner_id
            ORDER BY started_at DESC, id DESC
        ) AS active_rank
    FROM meetings
    WHERE state = 'in_meeting'
)
UPDATE meetings
SET state = 'ended',
    ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
WHERE (tenant_id, owner_id, id) IN (
    SELECT tenant_id, owner_id, id
    FROM ranked
    WHERE active_rank > 1
);

CREATE UNIQUE INDEX idx_meetings_one_active_owner
    ON meetings(tenant_id, owner_id)
    WHERE state = 'in_meeting';
