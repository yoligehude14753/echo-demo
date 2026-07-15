"""B12 migration rollback and external-runner route gates."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from app.adapters.repo.migrator import run_migrations
from app.agents.agentos import AgentOSBackend
from app.agents.base import AgentIntent
from app.config import Settings


def _write_migration(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


@pytest.mark.unit
def test_failed_upgrade_rolls_back_the_current_migration_and_resumes_cleanly(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "upgrade.db"
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations,
        "001_base.sql",
        "CREATE TABLE base_items (id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        migrations,
        "002_partial.sql",
        "CREATE TABLE partial_items (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO missing_table VALUES (1);",
    )

    failed = asyncio.run(run_migrations(db_path, migrations_dir=migrations))

    assert failed.applied == [1]
    assert failed.current_version == 1
    assert failed.errors
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'partial_items'"
        ).fetchone() is None

    _write_migration(
        migrations,
        "002_partial.sql",
        "CREATE TABLE partial_items (id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        migrations,
        "003_after.sql",
        "CREATE TABLE after_items (id INTEGER PRIMARY KEY);",
    )

    resumed = asyncio.run(run_migrations(db_path, migrations_dir=migrations))

    assert resumed.errors == []
    assert resumed.applied == [2, 3]
    assert resumed.current_version == 3

@pytest.mark.unit
def test_external_runner_requires_explicit_endpoint_before_any_submit() -> None:
    settings = Settings(
        db_path=Path(":memory:"),
        storage_dir=Path("/tmp/echodesk-b12-test-storage"),
        agent_os_enabled=True,
    )
    backend = AgentOSBackend(settings)

    result = asyncio.run(
        backend.submit(
            AgentIntent(
                text="不应连接外部 runner",
                device_id="b12-test",
                echo_task_id="echo_task_b12",
                runner_operation_key="agent-submit-b12",
            )
        )
    )

    assert backend.base_url == ""
    assert backend.enabled is False
    assert result.accepted is False
    assert result.error == "agent runner endpoint is not explicitly configured"
