-- 038_rag_projection_recovery.sql
-- Durable, fair repair scheduling for meeting and ambient BM25 projections.
-- Existing ambient rows predate operation IDs and can be either side of the
-- DB-commit/BM25-write crash window.  They start in reconcile_pending so the
-- repair loop checks stable legacy evidence before deciding whether to replay.
-- New rows explicitly insert index_pending.

ALTER TABLE meetings ADD COLUMN rag_projection_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meetings ADD COLUMN rag_projection_next_retry_at TEXT;
ALTER TABLE meetings ADD COLUMN rag_projection_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meetings ADD COLUMN minutes_generation_run_id TEXT;
ALTER TABLE meetings ADD COLUMN minutes_generation_cancelled_at TEXT;

ALTER TABLE ambient_segments
    ADD COLUMN rag_projection_state TEXT NOT NULL DEFAULT 'reconcile_pending';
ALTER TABLE ambient_segments ADD COLUMN rag_projection_error TEXT;
ALTER TABLE ambient_segments ADD COLUMN rag_projected_at TEXT;
ALTER TABLE ambient_segments ADD COLUMN rag_projection_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ambient_segments ADD COLUMN rag_projection_next_retry_at TEXT;

CREATE INDEX idx_meetings_rag_projection_due
    ON meetings(
        tenant_id,
        owner_id,
        rag_projection_state,
        rag_projection_next_retry_at,
        started_at
    );

CREATE INDEX idx_ambient_segments_rag_projection_due
    ON ambient_segments(
        tenant_id,
        owner_id,
        rag_projection_state,
        rag_projection_next_retry_at,
        captured_at,
        id
    );

CREATE TABLE IF NOT EXISTS bm25_document_projection_fences (
    index_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    operation TEXT NOT NULL CHECK(operation IN ('index', 'delete')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_key, tenant_id, owner_id, doc_id),
    FOREIGN KEY (index_key) REFERENCES bm25_index_state(index_key) ON DELETE CASCADE
);
