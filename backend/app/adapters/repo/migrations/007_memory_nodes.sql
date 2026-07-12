-- 007_memory_nodes.sql
-- 端边云统一架构 M5b（ADR-ECM-002 延伸）：边侧结构化记忆。
--
-- 此前 echodesk 的"记忆"只有 RAG（对话/ambient 全文按日索引，BM25 检索原文）。
-- RAG 适合"我们刚才聊过什么"，但不适合"关于这个人/这件事，我沉淀了哪些结论"——
-- 后者需要从噪声对话里 LLM 抽取出结构化记忆点（偏好/事实/待办/人物），去重沉淀，
-- 带重要度与热度，供 agent 作为长期记忆引用。对齐 echo 云 MemoryGraph 的理念，
-- 边侧轻量落地（ADR-008 边权威：记忆先在边沉淀）。
--
-- 字段：
--   content        记忆内容（一句话，如"用户对花生过敏"）
--   kind           preference(偏好) / fact(事实) / todo(待办) / event(事件) / profile(人物)
--   source         local(本机) / device(ESP32)，与 ambient_segments/conversations 对齐
--   device_id      设备来源标识
--   speaker_label  记忆关联的说话人（谁的偏好/谁说的）
--   salience       重要度 0-1（LLM 抽取时给出）
--   hit_count      被提及/确认次数（热度，重复抽取到同一记忆 +1 而非新增）
--   created_at     首次记录
--   last_seen_at   最近一次提及/确认

CREATE TABLE IF NOT EXISTS memory_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    source TEXT NOT NULL DEFAULT 'local',
    device_id TEXT,
    speaker_label TEXT,
    salience REAL NOT NULL DEFAULT 0.5,
    hit_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_kind ON memory_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_salience ON memory_nodes(salience DESC);
-- content 唯一性：同一条记忆重复抽取时 bump hit_count，不重复入库
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_nodes_content ON memory_nodes(content);
