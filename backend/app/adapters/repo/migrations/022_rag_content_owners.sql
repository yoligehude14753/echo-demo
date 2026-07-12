-- Logical storage accounting/ACL for globally content-addressed RAG inputs.
CREATE TABLE IF NOT EXISTS rag_content_owners (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    doc_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, content_hash)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_content_owners_doc
    ON rag_content_owners(tenant_id, owner_id, doc_id)
    WHERE doc_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_rag_content_owners_hash
    ON rag_content_owners(content_hash);
