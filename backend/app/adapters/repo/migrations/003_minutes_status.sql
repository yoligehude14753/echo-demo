-- 003_minutes_status.sql
-- 在 meetings 表上增加 minutes_status / minutes_error 两列，区分纪要四态：
--   - NULL：会议进行中（state=in_meeting）或尚未尝试 finalize
--   - 'generating'：finalize 正在跑（兜底用，正常情况下从 in_meeting 直接进 ok/failed）
--   - 'ok'：已成功生成（与 state='finalized' 同步）
--   - 'generation_failed'：LLM 调用 / JSON 校验失败；用户可触发重试
--
-- 解决的 bug（2026-05-28 backend.log）：
--   manual_end 调 MeetingPipeline.finalize_meeting() 缺 title → finalize 抛错 →
--   fallback 走 end_meeting() 把 state 置 'ended' 但 minutes_json 仍为 NULL，
--   前端 MinutesPanel 永远显示「纪要尚未生成」却没有重试入口。
--
-- 让 UI 能区分「正在生成」「生成失败可重试」「已生成」三种 ended 子态。

ALTER TABLE meetings ADD COLUMN minutes_status TEXT;
ALTER TABLE meetings ADD COLUMN minutes_error TEXT;

-- 历史数据回填（对存量 sqlite 一次性生效；新库走 baseline 时这步是 noop）
--   - 已 finalized 且有 minutes_json → 'ok'
--   - 已 ended 且 minutes_json 为空 → 'generation_failed'（用户可看到「重试」按钮）
--   - 其余（in_meeting）保持 NULL
UPDATE meetings SET minutes_status = 'ok'
    WHERE state = 'finalized' AND minutes_json IS NOT NULL AND minutes_status IS NULL;
UPDATE meetings SET minutes_status = 'generation_failed',
                    minutes_error = COALESCE(minutes_error, 'unknown (legacy ended without minutes)')
    WHERE state = 'ended' AND (minutes_json IS NULL OR minutes_json = '')
      AND minutes_status IS NULL;
