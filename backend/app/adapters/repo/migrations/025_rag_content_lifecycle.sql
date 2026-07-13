-- Durable lifecycle for globally content-addressed RAG upload bytes.
--
-- The ACL row is the authority for logical ownership, workflow recovery and
-- per-principal storage accounting.  The physical blob is shared by hash and
-- may only be collected after the final ACL disappears.

ALTER TABLE rag_content_owners ADD COLUMN workflow_run_id TEXT;
ALTER TABLE rag_content_owners ADD COLUMN file_suffix TEXT NOT NULL DEFAULT '';
ALTER TABLE rag_content_owners ADD COLUMN state TEXT NOT NULL DEFAULT 'claimed'
    CHECK(state IN ('claimed', 'staged', 'ready'));
ALTER TABLE rag_content_owners ADD COLUMN quota_managed INTEGER NOT NULL DEFAULT 0
    CHECK(quota_managed IN (0, 1));

UPDATE rag_content_owners
SET state = 'ready'
WHERE doc_id IS NOT NULL;

-- Historical non-local ACLs were created only by authenticated public users.
UPDATE rag_content_owners
SET quota_managed = 1
WHERE tenant_id <> 'legacy-local' OR owner_id <> 'legacy-local';

CREATE INDEX IF NOT EXISTS idx_rag_content_owners_workflow
    ON rag_content_owners(tenant_id, owner_id, workflow_run_id)
    WHERE workflow_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_rag_content_owners_state
    ON rag_content_owners(state, updated_at);
