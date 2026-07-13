-- 039_memory_system.sql
--
-- EchoDesk online memory system.  The published v7 table was archived as
-- legacy_v7_memory_nodes because it has no principal scope or provenance.
-- Keep that archive untouched; only explicitly reviewed data may be imported
-- into these owner-scoped tables later.

CREATE TABLE IF NOT EXISTS memory_nodes (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'fact', 'preference', 'decision', 'todo', 'relationship'
    )),
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    subject TEXT,
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    salience REAL NOT NULL CHECK(salience >= 0.0 AND salience <= 1.0),
    scope TEXT NOT NULL DEFAULT 'owner',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN (
        'active', 'superseded', 'deleted'
    )),
    hit_count INTEGER NOT NULL DEFAULT 1 CHECK(hit_count >= 1),
    source_count INTEGER NOT NULL DEFAULT 1 CHECK(source_count >= 1),
    user_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(user_confirmed IN (0, 1)),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    superseded_at TEXT,
    superseded_by TEXT,
    deleted_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, owner_id, memory_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_nodes_active_key
    ON memory_nodes(tenant_id, owner_id, kind, canonical_key)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_memory_nodes_recall
    ON memory_nodes(
        tenant_id,
        owner_id,
        status,
        salience DESC,
        last_seen_at DESC
    );

CREATE INDEX IF NOT EXISTS idx_memory_nodes_normalized
    ON memory_nodes(tenant_id, owner_id, normalized_content);

CREATE TABLE IF NOT EXISTS memory_provenance (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    provenance_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN (
        'conversation_user',
        'conversation_assistant',
        'meeting_segment',
        'meeting_minutes',
        'ambient_segment',
        'artifact',
        'user_explicit',
        'legacy_import'
    )),
    source_id TEXT NOT NULL,
    source_segment_id TEXT,
    meeting_id TEXT,
    artifact_id TEXT,
    excerpt TEXT NOT NULL,
    excerpt_sha256 TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, owner_id, provenance_id),
    FOREIGN KEY (tenant_id, owner_id, memory_id)
        REFERENCES memory_nodes(tenant_id, owner_id, memory_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_provenance_dedupe
    ON memory_provenance(
        tenant_id,
        owner_id,
        memory_id,
        source_kind,
        source_id,
        COALESCE(source_segment_id, ''),
        excerpt_sha256
    );

CREATE INDEX IF NOT EXISTS idx_memory_provenance_source
    ON memory_provenance(
        tenant_id,
        owner_id,
        source_kind,
        source_id,
        occurred_at DESC
    );

CREATE TABLE IF NOT EXISTS memory_relations (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    relation_id TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    relation_kind TEXT NOT NULL CHECK(relation_kind IN (
        'related_to', 'supports', 'contradicts', 'supersedes'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    evidence_provenance_id TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, owner_id, relation_id),
    FOREIGN KEY (tenant_id, owner_id, source_memory_id)
        REFERENCES memory_nodes(tenant_id, owner_id, memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_id, target_memory_id)
        REFERENCES memory_nodes(tenant_id, owner_id, memory_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_relations_dedupe
    ON memory_relations(
        tenant_id,
        owner_id,
        source_memory_id,
        target_memory_id,
        relation_kind
    )
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations(tenant_id, owner_id, target_memory_id, deleted_at);

-- L3 is deliberately separate from inferred semantic memory.  Only the
-- explicit configuration API may write rows here.
CREATE TABLE IF NOT EXISTS memory_profile_settings (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL DEFAULT 'user_explicit'
        CHECK(source = 'user_explicit'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    deleted_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    PRIMARY KEY (tenant_id, owner_id, config_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_profile_active
    ON memory_profile_settings(tenant_id, owner_id, deleted_at, updated_at DESC);

-- Every small-model extraction is auditable without retaining the complete
-- prompt.  input_sha256 + provenance excerpt are sufficient to trace it.
CREATE TABLE IF NOT EXISTS memory_extraction_runs (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    model_display_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running', 'succeeded', 'failed', 'skipped')),
    latency_ms REAL NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (tenant_id, owner_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_extraction_source
    ON memory_extraction_runs(
        tenant_id,
        owner_id,
        source_kind,
        source_id,
        created_at DESC
    );
