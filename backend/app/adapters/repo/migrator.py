"""SQLite schema migration runner（P2.4）。

设计要点：
- migrations/ 目录下 ``NNN_<slug>.sql`` 形式的纯 SQL 文件，按 NNN（int）排序执行
- 每个 .sql 文件必须自身幂等（``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``）
- 已应用版本登记在 ``schema_version`` 表，二次启动 skip
- 单文件包裹在 ``BEGIN .. COMMIT`` 里；任一语句失败 ROLLBACK + errors[] 记录，
  后续文件不再执行（避免基于不一致 schema 继续 migrate）
- 不上 alembic / sqlmodel：当前需要的就是几十行可读的 runner

调用方（lifespan startup）::

    from app.adapters.repo.migrator import run_migrations
    result = await run_migrations(settings.db_path)
    if result.errors:
        raise RuntimeError(f"db migrations failed: {result.errors}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

logger = logging.getLogger("echodesk.migrator")

# NNN_<slug>.sql：NNN 必须 3 位以上数字
_MIGRATION_NAME_RE = re.compile(r"^(\d{3,})_[A-Za-z0-9_\-]+\.sql$")

# bootstrap：在跑任何文件前先建好 schema_version 自身（不算 migration）。
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);
"""

_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass
class MigrationResult:
    """run_migrations 的产物。"""

    applied: list[int] = field(default_factory=list)
    """这次实际跑过的版本号，按执行顺序。"""

    current_version: int = 0
    """跑完后 schema_version 里的最大版本（不含失败的）。"""

    skipped: list[int] = field(default_factory=list)
    """启动前已应用、本次跳过的版本号。"""

    errors: list[str] = field(default_factory=list)
    """失败信息（人类可读），格式 ``"v{N} ({name}): {err}"``。"""


@dataclass(frozen=True)
class _MigrationFile:
    version: int
    name: str  # 如 "001_initial"
    path: Path


def _discover(migrations_dir: Path) -> list[_MigrationFile]:
    """扫描目录，按 NNN 数字排序（而非字典序：避免 010 排在 002 之前）。"""
    if not migrations_dir.is_dir():
        return []
    out: list[_MigrationFile] = []
    for p in migrations_dir.iterdir():
        if not p.is_file() or p.suffix != ".sql":
            continue
        m = _MIGRATION_NAME_RE.match(p.name)
        if not m:
            logger.warning("migrator: skip unrecognized file %s", p.name)
            continue
        out.append(_MigrationFile(version=int(m.group(1)), name=p.stem, path=p))
    out.sort(key=lambda mf: mf.version)
    return out


async def _ensure_bootstrap(conn: aiosqlite.Connection) -> None:
    """建 schema_version 自身（IF NOT EXISTS，幂等）。"""
    await conn.executescript(_BOOTSTRAP_SQL)
    await conn.commit()


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT version FROM schema_version")
    rows = await cur.fetchall()
    await cur.close()
    return {int(r[0]) for r in rows}


async def _apply_one(
    conn: aiosqlite.Connection,
    mf: _MigrationFile,
) -> None:
    """单文件原子应用：BEGIN → 执行内容 → 写 schema_version → COMMIT。

    用 executescript() 支持多语句。SQL 文件**不应**包含 BEGIN/COMMIT，
    由本函数统一包裹。失败时 ROLLBACK 由 caller 触发。
    """
    sql = mf.path.read_text(encoding="utf-8")
    # 文件名作为 description（filename basename without ext），单引号转义防 SQL 注入。
    desc = mf.name.replace("'", "''")
    script = (
        "BEGIN;\n"
        f"{sql}\n"
        f"INSERT INTO schema_version (version, description) VALUES ({mf.version}, '{desc}');\n"
        "COMMIT;\n"
    )
    await conn.executescript(script)


async def _rollback_quiet(conn: aiosqlite.Connection) -> None:
    """容忍 ROLLBACK 时没有活跃事务的情形（executescript 失败位置不定）。"""
    try:
        await conn.execute("ROLLBACK")
    except Exception as e:  # pragma: no cover - 仅日志
        logger.debug("rollback noop: %s", e)


async def run_migrations(
    db_path: Path | str,
    *,
    migrations_dir: Path | None = None,
) -> MigrationResult:
    """对 ``db_path`` 跑全部待应用的 migration，返回结构化结果。

    参数：
        db_path: SQLite 文件路径；父目录会被自动创建。
        migrations_dir: 默认 ``<package>/migrations/``；测试可注入临时目录。

    返回：
        :class:`MigrationResult`，调用方可据 ``errors`` 决定是否中止启动。
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    mdir = migrations_dir or _DEFAULT_MIGRATIONS_DIR

    result = MigrationResult()
    files = _discover(mdir)
    if not files:
        logger.info("migrator: no migration files under %s", mdir)

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await _ensure_bootstrap(conn)
        applied = await _applied_versions(conn)

        for mf in files:
            if mf.version in applied:
                result.skipped.append(mf.version)
                continue
            try:
                await _apply_one(conn, mf)
                result.applied.append(mf.version)
                logger.info("migrator: applied v%d (%s)", mf.version, mf.name)
            except Exception as e:
                await _rollback_quiet(conn)
                msg = f"v{mf.version} ({mf.name}): {e}"
                result.errors.append(msg)
                logger.error("migrator: failed %s", msg)
                # 不再继续：后续 migration 可能依赖失败的 schema 状态
                break

        # current_version：最新成功应用 / 已存在的最大版本
        applied_now = await _applied_versions(conn)
        result.current_version = max(applied_now, default=0)

    return result


__all__ = ["MigrationResult", "run_migrations"]
