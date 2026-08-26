-- 002_schema_version.sql
-- 显式声明版本表，让 migrations/ 自身的演化也走版本化。
-- migrator.py 会在跑任何 migration 前 bootstrap 这张表（IF NOT EXISTS），
-- 但这里仍保留一份 DDL，作为"版本 002 引入了 schema tracking"的 audit trail。

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);
