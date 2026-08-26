-- 009_seed_plans.sql
-- 计费骨架(F-GUGU-042 P4):当前全部免费,但把"付费套餐"数据与计费流程占位先建好。
-- pro 套餐价格 > 0 → 订阅会被 billing 桩拦截(支付未接入),仅用于展示与流程占位。
-- 真实支付接入后,移除桩 + 接入 Stripe/微信支付等即可,无需改表结构。

INSERT OR IGNORE INTO plans (id, name, monthly_stt_sec, monthly_tts_chars, monthly_llm_tokens, price_micros)
VALUES ('pro', '专业版', NULL, NULL, NULL, 29000000);
