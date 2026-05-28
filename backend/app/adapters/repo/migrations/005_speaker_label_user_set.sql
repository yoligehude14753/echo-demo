-- 005_speaker_label_user_set.sql
-- 用户 2026-05-28 期望：「用户改过的名称要持久化，下次同声纹自动改过来」。
--
-- 现状问题：speakers.label 字段同时存"自动分配的「说话人 N」"和"用户改过的
-- 真名"，hydrate 时无法区分。新需求要求只把 user-set label 跨进程加载，
-- 自动分配的「说话人 N」每进程从 1 起（避免 11/19 编号爆炸）。
--
-- 加一个 boolean flag 区分两者：
--   - label_user_set = 0：自动分配（hydrate 不加载）
--   - label_user_set = 1：用户 POST /speakers/{id}/rename 设置（hydrate 加载）

ALTER TABLE speakers ADD COLUMN label_user_set INTEGER NOT NULL DEFAULT 0;
