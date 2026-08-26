-- Echo AI — SQLite Schema
-- All timestamps stored as ISO-8601 text (UTC)

-- ── Memory Graph ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_nodes (
    node_id       TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL DEFAULT 'default',  -- isolate per device
    name          TEXT NOT NULL,
    aliases       TEXT DEFAULT '[]',        -- JSON array of alias strings
    dimension     TEXT NOT NULL,            -- person / event / logic / knowledge
    description   TEXT DEFAULT '',
    valid_at      TEXT NOT NULL,            -- when this info became true (reality time)
    invalid_at    TEXT,                     -- when this info stopped being true (NULL = still valid)
    created_at    TEXT NOT NULL,            -- when system learned this
    heat          REAL DEFAULT 0.0,         -- access_count × avg_conversation_turns
    access_count  INTEGER DEFAULT 0,
    last_accessed TEXT,
    layer         TEXT DEFAULT 'episodic',  -- episodic / semantic
    embedding     BLOB,                     -- sentence-transformer vector (numpy float32 bytes)
    session_id    TEXT,
    somatic_marker TEXT DEFAULT '{}',       -- JSON: PAD state at time of recording
    source_confidence REAL NOT NULL DEFAULT 0.7,  -- quality of originating transcript (v4)
    CONSTRAINT dimension_check CHECK (dimension IN ('person', 'event', 'logic', 'knowledge')),
    CONSTRAINT layer_check CHECK (layer IN ('episodic', 'semantic'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_device ON memory_nodes(device_id);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON memory_nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_layer ON memory_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_nodes_heat ON memory_nodes(heat DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_invalid ON memory_nodes(invalid_at);
CREATE INDEX IF NOT EXISTS idx_nodes_confidence ON memory_nodes(device_id, source_confidence DESC);

CREATE TABLE IF NOT EXISTS memory_edges (
    edge_id    TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL DEFAULT 'default',  -- mirrors node device_id
    from_id    TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    relation   TEXT NOT NULL,
    valid_at   TEXT NOT NULL,
    invalid_at TEXT,
    created_at TEXT NOT NULL,
    session_id TEXT,
    FOREIGN KEY (from_id) REFERENCES memory_nodes(node_id),
    FOREIGN KEY (to_id)   REFERENCES memory_nodes(node_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_device ON memory_edges(device_id);
CREATE INDEX IF NOT EXISTS idx_edges_from ON memory_edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON memory_edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_invalid ON memory_edges(invalid_at);

-- View: only currently valid graph
CREATE VIEW IF NOT EXISTS current_graph AS
SELECT
    n.node_id, n.name, n.aliases, n.dimension, n.description,
    n.heat, n.access_count, n.layer,
    e.edge_id, e.relation, e.to_id
FROM memory_nodes n
LEFT JOIN memory_edges e ON n.node_id = e.from_id AND e.invalid_at IS NULL
WHERE n.invalid_at IS NULL;

-- ── Soul ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS soul_state (
    device_id     TEXT PRIMARY KEY,
    -- PAD emotion dimensions (Ornstein-Uhlenbeck persisted)
    pleasure      REAL DEFAULT 0.0,
    arousal       REAL DEFAULT 0.0,
    dominance     REAL DEFAULT 0.0,
    -- Relationship dimensions
    trust         REAL DEFAULT 0.5,
    attachment    REAL DEFAULT 0.1,
    respect       REAL DEFAULT 0.5,
    frustration   REAL DEFAULT 0.0,
    -- Relation stage
    stage         TEXT DEFAULT 'stranger',
    depth_score   REAL DEFAULT 0.0,
    interaction_count INTEGER DEFAULT 0,
    days_known    INTEGER DEFAULT 0,
    -- Catastrophe cooldown
    last_catastrophe_at TEXT,
    -- Timestamps
    updated_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- ── Sessions ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    device_id    TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    turn_count   INTEGER DEFAULT 0,
    notes        TEXT DEFAULT '',        -- Session Memory Notes markdown
    consolidated INTEGER DEFAULT 0       -- 0 = pending reflection, 1 = done
);

CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
CREATE INDEX IF NOT EXISTS idx_sessions_consolidated ON sessions(consolidated);

-- ── Tasks ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    device_id    TEXT,
    type         TEXT NOT NULL,          -- reminder / research / monitoring / periodic
    title        TEXT NOT NULL,
    description  TEXT DEFAULT '',
    due_at       TEXT,
    interval_hours INTEGER,
    status       TEXT DEFAULT 'pending', -- pending / running / done / failed
    result       TEXT,
    created_at   TEXT NOT NULL,
    last_run_at  TEXT,
    CONSTRAINT type_check CHECK (type IN ('reminder', 'research', 'monitoring', 'periodic')),
    CONSTRAINT status_check CHECK (status IN ('pending', 'running', 'done', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_device ON tasks(device_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);

-- ── Dream logs ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dream_logs (
    log_id           TEXT PRIMARY KEY,
    device_id        TEXT,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    processed_count  INTEGER DEFAULT 0,
    promoted_count   INTEGER DEFAULT 0,
    pruned_count     INTEGER DEFAULT 0,
    merged_count     INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'running', -- running / done / failed
    summary          TEXT DEFAULT ''
);

-- ── Offline buffer ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS offline_buffer (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT,
    text         TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    processed    INTEGER DEFAULT 0
);

-- ── Ambient Transcripts ───────────────────────────────────────────
-- 存储所有 Deepgram 流式 ASR 输出（全量保留，Dream 闲时处理提取记忆）

CREATE TABLE IF NOT EXISTS ambient_transcripts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    text            TEXT NOT NULL,
    speaker_label   TEXT,           -- Deepgram 临时标签 SPEAKER_0, SPEAKER_1 ...
    speaker_uuid    TEXT,           -- resemblyzer 映射后的持久 UUID（NULL = 未知）
    confidence      REAL DEFAULT 0.0,
    source          TEXT NOT NULL DEFAULT 'device', -- device | desktop
    router_action   TEXT,           -- activate | ambient | ignore | NULL(未路由)
    recorded_at     TEXT NOT NULL,  -- ISO-8601 UTC
    processed_for_memory INTEGER NOT NULL DEFAULT 0  -- 0=待处理, 1=Dream已处理
);

CREATE INDEX IF NOT EXISTS idx_at_device    ON ambient_transcripts(device_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_at_pending   ON ambient_transcripts(processed_for_memory, recorded_at);
CREATE INDEX IF NOT EXISTS idx_at_speaker   ON ambient_transcripts(speaker_uuid);
CREATE INDEX IF NOT EXISTS idx_at_action    ON ambient_transcripts(router_action);

-- ── Device Profiles ───────────────────────────────────────────────
-- 用户档案与设备绑定（单用户，无需认证）

CREATE TABLE IF NOT EXISTS device_profiles (
    device_id    TEXT PRIMARY KEY,
    user_name    TEXT DEFAULT '',          -- 用户姓名
    nickname     TEXT DEFAULT '',          -- 设备昵称（如"客厅的Echo"）
    bio          TEXT DEFAULT '',          -- 用户自我介绍，注入 LLM system prompt
    preferences  TEXT DEFAULT '{}',        -- JSON: {"wake_sensitivity":"medium", ...}
    updated_at   TEXT NOT NULL
);

-- ── Schema version ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (4, datetime('now'));
