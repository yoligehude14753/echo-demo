-- 037_restore_speaker_label_user_set.sql
-- 收敛两条已发布的 schema lineage：
-- 1) v006 给 ambient_segments 增加了 source 和 nullable device_id；未走 v006 的
--    current lineage 则由 v012 直接得到 NOT NULL device_id。迁移器在事务内先补齐
--    缺失的 source，本文件再重建为唯一 canonical schema。
-- 2) 旧 v005 曾持久化 label_user_set，现链的 v013 rebuild 曾遗漏该列。迁移器
--    仅在旧链已完整保留同定义列时把最后一个 ALTER 视为已满足。

ALTER TABLE ambient_segments RENAME TO ambient_segments_pre_v037;

CREATE TABLE ambient_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local'
);

INSERT INTO ambient_segments (
    id, audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at,
    source, tenant_id, device_id, owner_id
)
SELECT
    id, audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at,
    source, tenant_id, COALESCE(device_id, 'legacy-local'), owner_id
FROM ambient_segments_pre_v037;

DROP TABLE ambient_segments_pre_v037;

CREATE INDEX idx_ambient_segments_captured ON ambient_segments(captured_at);
CREATE INDEX idx_ambient_segments_speaker ON ambient_segments(speaker_id);
CREATE INDEX idx_ambient_segments_source ON ambient_segments(source);
CREATE INDEX idx_ambient_segments_owner_captured
    ON ambient_segments(tenant_id, owner_id, captured_at DESC);

ALTER TABLE speakers ADD COLUMN label_user_set INTEGER NOT NULL DEFAULT 0;
