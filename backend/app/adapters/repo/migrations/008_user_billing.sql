-- 008_user_billing.sql
-- 用户管理 + API + 计费体系（F-GUGU-042）。
--
-- 控制面"云就绪"地基:账号(可选登录)、会话令牌(可吊销)、API Key、套餐、用量计量、
-- 按用户模型配置。当前免费 → 计费骨架先建好,真实支付后续接入。
-- 设计为可整体抽出独立托管;数据多用户强隔离是后续独立迁移,本期不在现有表加 user_id。
--
-- 安全:password_hash 用 pbkdf2_hmac(sha256) + 每用户 salt;token/api_key 只存 hash,
-- 明文仅签发时返回一次(见 app/use_cases/auth.py)。

-- 用户账号
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,                 -- usr_<random>
    email TEXT NOT NULL,                 -- 登录标识(邮箱或用户名),小写归一
    display_name TEXT,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    plan_id TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- 会话令牌(不透明,可吊销):只存 token 的 sha256,明文仅登录时返回
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- API Key:外部/桌面调用"我们的 API"的凭证。只存 hash;前缀明文便于识别。
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,                 -- key_<random>
    user_id TEXT NOT NULL,
    name TEXT,
    key_prefix TEXT NOT NULL,            -- 如 gugu_live_AbCd(明文前若干位,展示用)
    key_hash TEXT NOT NULL,              -- 完整 key 的 sha256
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- 套餐:配额上限(NULL=不限)。价格占位,真实支付后续。
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_stt_sec INTEGER,             -- STT 秒/月
    monthly_tts_chars INTEGER,           -- TTS 字符/月
    monthly_llm_tokens INTEGER,          -- LLM token/月
    price_micros INTEGER NOT NULL DEFAULT 0,  -- 月价(微元,0=免费)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- 内置免费套餐(当前全部免费,配额留宽松/不限)
INSERT OR IGNORE INTO plans (id, name, monthly_stt_sec, monthly_tts_chars, monthly_llm_tokens, price_micros)
VALUES ('free', '免费', NULL, NULL, NULL, 0);

-- 用量事件:每次走"我们的 API"的计量记录,计费与配额的事实来源
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    api_key_id TEXT,                     -- 经 API Key 调用时记录,桌面登录态为 NULL
    capability TEXT NOT NULL,            -- stt | tts | llm
    units REAL NOT NULL,                 -- 用量(秒/字符/token)
    unit_kind TEXT NOT NULL,             -- sec | chars | tokens
    provider TEXT,                       -- ours | <self_host 不计量,通常不写>
    cost_micros INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_cap ON usage_events(capability);

-- 按用户模型配置:服务模式 + 各能力 endpoint/key/model 覆盖。
-- service_mode: ours(用我们的API) | self_host(全自部署) | mixed(逐能力覆盖)
-- 各 *_mode: ours | self_host;self_host 时用对应 *_base_url/*_api_key/*_model。
CREATE TABLE IF NOT EXISTS user_model_config (
    user_id TEXT PRIMARY KEY,
    service_mode TEXT NOT NULL DEFAULT 'ours',
    stt_mode TEXT NOT NULL DEFAULT 'ours',
    stt_base_url TEXT,
    stt_api_key TEXT,
    tts_mode TEXT NOT NULL DEFAULT 'ours',
    tts_base_url TEXT,
    tts_api_key TEXT,
    llm_mode TEXT NOT NULL DEFAULT 'ours',
    llm_base_url TEXT,
    llm_api_key TEXT,
    llm_model TEXT,
    updated_at TEXT NOT NULL
);
