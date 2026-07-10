"""Migration runner 单测（P2.4）。

覆盖：
- 全新 DB 跑完所有 migration → 版本号都登记到 schema_version
- 已应用版本被 skip（不重复执行）
- 单文件 SQL 语法错 → errors[] 记录，前面成功的版本仍登记
- 同一份幂等 SQL 连续跑两次 → 不炸（IF NOT EXISTS 兜底）
- 文件名 NNN 必须按数值排序，而非字典序（"010" 不应排在 "002" 前）
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations


def _write(d: Path, name: str, sql: str) -> None:
    (d / name).write_text(sql, encoding="utf-8")


async def _applied(db_path: Path) -> list[int]:
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("SELECT version FROM schema_version ORDER BY version")
        rows = await cur.fetchall()
        await cur.close()
    return [int(r[0]) for r in rows]


async def _has_table(db_path: Path, table: str) -> bool:
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        row = await cur.fetchone()
        await cur.close()
    return row is not None


@pytest.mark.unit
async def test_fresh_db_runs_all_migrations(tmp_path: Path) -> None:
    """空 DB → 所有 migration 应用，schema_version 表里有全部版本号。"""
    db = tmp_path / "fresh.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_a.sql", "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")
    _write(mdir, "002_b.sql", "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")

    result = await run_migrations(db, migrations_dir=mdir)

    assert result.errors == []
    assert result.applied == [1, 2]
    assert result.skipped == []
    assert result.current_version == 2
    assert await _applied(db) == [1, 2]
    assert await _has_table(db, "a")
    assert await _has_table(db, "b")


@pytest.mark.unit
async def test_existing_db_skips_applied(tmp_path: Path) -> None:
    """第二次启动：全部版本应进 skipped[]，applied[] 为空。"""
    db = tmp_path / "rerun.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_a.sql", "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")
    _write(mdir, "002_b.sql", "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")

    first = await run_migrations(db, migrations_dir=mdir)
    assert first.applied == [1, 2]

    second = await run_migrations(db, migrations_dir=mdir)
    assert second.applied == []
    assert second.skipped == [1, 2]
    assert second.errors == []
    assert second.current_version == 2


@pytest.mark.unit
async def test_partial_apply_on_syntax_error(tmp_path: Path) -> None:
    """中间一个文件 SQL 报错 → errors[] 记录，前面成功的版本保留。"""
    db = tmp_path / "partial.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_ok.sql", "CREATE TABLE IF NOT EXISTS ok (id INTEGER PRIMARY KEY);")
    _write(mdir, "002_bad.sql", "THIS IS NOT VALID SQL ;;;")
    _write(mdir, "003_unreached.sql", "CREATE TABLE IF NOT EXISTS u (id INTEGER PRIMARY KEY);")

    result = await run_migrations(db, migrations_dir=mdir)

    assert result.applied == [1]
    assert any("v2" in e for e in result.errors)
    assert result.current_version == 1
    # 失败后停止，003 不应被尝试
    assert 3 not in result.applied
    assert not await _has_table(db, "u")
    # 002 因失败 ROLLBACK，schema_version 里不应有 2
    assert await _applied(db) == [1]


@pytest.mark.unit
async def test_idempotent_sql_runs_twice_clean(tmp_path: Path) -> None:
    """同一份 IF NOT EXISTS SQL 直接绕过版本表跑两次也不应炸。"""
    db = tmp_path / "idem.db"
    sql = "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);"

    async with aiosqlite.connect(str(db)) as conn:
        await conn.executescript(sql)
        await conn.executescript(sql)
        await conn.commit()

    assert await _has_table(db, "t")


@pytest.mark.unit
async def test_version_ordering_by_int_not_lex(tmp_path: Path) -> None:
    """文件名 010 / 002 / 001 → 按 int 排，applied 应是 [1, 2, 10]。"""
    db = tmp_path / "order.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    # 故意按字典序倒置；如果 migrator 用 lex 排序，会按 001 → 010 → 002 跑
    _write(mdir, "010_c.sql", "CREATE TABLE IF NOT EXISTS c (id INTEGER PRIMARY KEY);")
    _write(mdir, "002_b.sql", "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")
    _write(mdir, "001_a.sql", "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")

    result = await run_migrations(db, migrations_dir=mdir)

    assert result.applied == [1, 2, 10]
    assert result.current_version == 10
    assert await _applied(db) == [1, 2, 10]


@pytest.mark.unit
async def test_real_migrations_dir_applies_on_fresh_db(tmp_path: Path) -> None:
    """对仓库实际打包的 migrations/ 在空 DB 上跑一遍，确保所有 NNN_*.sql 都能落地。

    这是对 sqlite.py 原 inline DDL 全量迁出的 smoke check。
    """
    db = tmp_path / "real.db"
    result = await run_migrations(db, migrations_dir=_DEFAULT_MIGRATIONS_DIR)

    assert result.errors == []
    assert result.current_version >= 11
    # 001_initial.sql 至少应建出这些核心表
    for tbl in (
        "meetings",
        "meeting_segments",
        "ambient_segments",
        "speakers",
        "workflow_runs",
        "workflow_events",
        "artifacts",
        "artifact_links",
    ):
        assert await _has_table(db, tbl), f"missing table {tbl}"
    assert await _has_table(db, "schema_version")
