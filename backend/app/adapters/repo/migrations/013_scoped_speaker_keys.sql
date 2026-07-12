-- 013_scoped_speaker_keys.sql
-- 012 为 speakers 增加了 principal 列，但 SQLite 原有 speaker_id PRIMARY KEY
-- 仍会让不同 owner 的同名 speaker 相互冲突。原子重建为 owner 内唯一键。

ALTER TABLE speakers RENAME TO speakers_legacy_global_key;

CREATE TABLE speakers (
    speaker_id TEXT NOT NULL,
    label TEXT,
    n_samples INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    embedding_blob BLOB,
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    PRIMARY KEY (tenant_id, owner_id, speaker_id)
);

INSERT INTO speakers (
    speaker_id,
    label,
    n_samples,
    first_seen_at,
    last_seen_at,
    embedding_blob,
    tenant_id,
    device_id,
    owner_id
)
SELECT
    speaker_id,
    label,
    n_samples,
    first_seen_at,
    last_seen_at,
    embedding_blob,
    tenant_id,
    device_id,
    owner_id
FROM speakers_legacy_global_key;

DROP TABLE speakers_legacy_global_key;

CREATE INDEX idx_speakers_owner_last_seen
    ON speakers(tenant_id, owner_id, last_seen_at DESC);
