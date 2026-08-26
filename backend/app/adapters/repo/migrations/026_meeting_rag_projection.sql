-- Durable state for the rebuildable Meeting -> RAG projection.
ALTER TABLE meetings ADD COLUMN rag_projection_state TEXT
    CHECK(rag_projection_state IS NULL OR rag_projection_state IN (
        'index_pending', 'indexed', 'index_failed',
        'delete_pending', 'deleted', 'delete_failed'
    ));
ALTER TABLE meetings ADD COLUMN rag_projection_error TEXT;
ALTER TABLE meetings ADD COLUMN rag_projected_at TEXT;

-- Existing finalized meetings may already be indexed, but replaying ingest is
-- an idempotent replacement and is safer than silently trusting memory state.
UPDATE meetings
SET rag_projection_state = 'index_pending',
    rag_projection_error = NULL,
    rag_projected_at = NULL
WHERE minutes_json IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_meetings_rag_projection_repair
    ON meetings(rag_projection_state, tenant_id, owner_id, started_at);
