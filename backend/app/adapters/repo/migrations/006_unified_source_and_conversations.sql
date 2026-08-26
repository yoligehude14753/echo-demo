-- 006_unified_source_and_conversations.sql
-- 端边云统一架构 M1（ADR-ECM-002）：统一采集来源 + 对话持久化。
--
-- 背景：此前 ESP32(端)和本机(边)是两条平行数据流——本机走 /capture 进
-- ambient_segments，ESP32 走 edge_voice_gateway 的内存 deque，重启即丢、无声纹、
-- 不入库。本 migration 把"采集来源"沉到数据层，并新增统一的对话(问答)持久化表，
-- 为 ESP32 退化成"又一个采集源(source=device)"走同一条 pipeline 打地基。
--
-- 1) ambient_segments 加 source/device_id：标记每段转写来自哪个采集源。
--    - source='local'  本机 Mac 麦克风（/capture 默认）
--    - source='device' ESP32 设备麦克风（边网关喂入）
--    - device_id       source='device' 时的设备标识（如 esp32-014958）；local 为 NULL
--    存量行 ALTER ADD COLUMN DEFAULT 'local' 自动回填为本机来源。

ALTER TABLE ambient_segments ADD COLUMN source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE ambient_segments ADD COLUMN device_id TEXT;

CREATE INDEX IF NOT EXISTS idx_ambient_segments_source
    ON ambient_segments(source);

-- 2) conversations：统一的人机对话(问答)持久化。
--    此前 agent 问答仅存在前端 zustand 内存(store.ts 注释)，重启清空，
--    且 ESP32 触发的问答完全不落库。新表让本机与设备的问答统一可回看、
--    可被记忆抽取(M5)。
--    - role        user(用户问) / assistant(助手答)
--    - source      local / device，与 ambient_segments 对齐
--    - device_id   设备来源标识
--    - speaker_id/speaker_label  问句的说话人(复用声纹体系；助手答为 NULL)
--    - trigger     触发方式：wake/classifier/followup/manual(本机手动输入)
--    - turn_id     同一轮问答(user+assistant)共享，便于配对展示
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    device_id TEXT,
    speaker_id TEXT,
    speaker_label TEXT,
    trigger TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_created
    ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(turn_id);
