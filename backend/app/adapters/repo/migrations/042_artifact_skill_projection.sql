-- 042_artifact_skill_projection.sql
-- B11 artifact/context links and B06P skill receipt/provenance projections.
-- These are derived, owner-scoped views; the artifact and B06P host remain
-- the authoritative sources.  No payload, filesystem path, or secret is kept.

CREATE TABLE IF NOT EXISTS agent_artifact_context_projections (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mapping_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    task_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    checkpoint_id TEXT,
    artifact_id TEXT NOT NULL,
    context_ref TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('input', 'output', 'derived')),
    artifact_sha256 TEXT,
    created_at TEXT NOT NULL,
    mapping_sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, mapping_id),
    FOREIGN KEY (tenant_id, owner_id, artifact_id)
        REFERENCES artifacts(tenant_id, owner_id, artifact_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_artifact_context_task
    ON agent_artifact_context_projections(
        tenant_id, owner_id, task_id, operation_key, created_at
    );

CREATE INDEX IF NOT EXISTS idx_agent_artifact_context_artifact
    ON agent_artifact_context_projections(
        tenant_id, owner_id, artifact_id, created_at
    );

CREATE TABLE IF NOT EXISTS agent_skill_receipt_projections (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    occurred_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    result TEXT NOT NULL,
    code TEXT NOT NULL,
    capability TEXT NOT NULL,
    task_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    grant_id TEXT,
    grant_revision INTEGER CHECK(grant_revision IS NULL OR grant_revision >= 1),
    policy_revision INTEGER CHECK(policy_revision IS NULL OR policy_revision >= 1),
    skill_identity TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    resource_hashes_json TEXT NOT NULL,
    provenance TEXT NOT NULL,
    signer_id TEXT NOT NULL,
    input_sha256 TEXT,
    output_sha256 TEXT,
    redacted INTEGER NOT NULL CHECK(redacted = 1),
    projection_sha256 TEXT NOT NULL,
    projection_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_skill_receipt_task
    ON agent_skill_receipt_projections(
        tenant_id, owner_id, task_id, operation_key, occurred_at
    );

CREATE INDEX IF NOT EXISTS idx_agent_skill_receipt_skill
    ON agent_skill_receipt_projections(
        tenant_id, owner_id, skill_identity, skill_version, occurred_at
    );
