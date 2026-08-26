-- Cross-process BM25 revision and authoritative payload manifest.
-- JSON index files remain atomically replaceable caches; SQLite is the
-- single-writer commit point used by every process sharing one index_key.

CREATE TABLE IF NOT EXISTS bm25_index_state (
    index_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bm25_index_documents (
    index_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_path TEXT,
    index_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_revision INTEGER NOT NULL CHECK(updated_revision >= 1),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_key, tenant_id, owner_id, doc_id),
    FOREIGN KEY (index_key) REFERENCES bm25_index_state(index_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bm25_index_documents_revision
    ON bm25_index_documents(index_key, updated_revision);

CREATE INDEX IF NOT EXISTS idx_bm25_index_documents_scope
    ON bm25_index_documents(index_key, tenant_id, owner_id, doc_id);
