"""Regression gates for the two migration lineages shipped before v0.3.1."""

from __future__ import annotations

import asyncio
import multiprocessing
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations

from tests.unit.test_migration_upgrade_chain import _insert_v11_legacy_data

_PUBLISHED_V005_SQL = """-- 005_speaker_label_user_set.sql
-- Published v0.2 lineage: preserve explicit user renames across restarts.
ALTER TABLE speakers ADD COLUMN label_user_set INTEGER NOT NULL DEFAULT 0;
"""
_RESTORED_HISTORY = frozenset({6, 7, 8, 9})
_LEGACY_CONTROL_TABLES = (
    "users",
    "sessions",
    "api_keys",
    "plans",
    "usage_events",
    "user_model_config",
)


def _version(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def _migration_files() -> list[Path]:
    return sorted(_DEFAULT_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"), key=_version)


def _published_v11_catalog(root: Path) -> Path:
    target = root / "published-v11-catalog"
    target.mkdir(parents=True)
    for source in _migration_files():
        version = _version(source)
        if version <= 11 and version != 5:
            shutil.copy2(source, target / source.name)
    (target / "005_speaker_label_user_set.sql").write_text(
        _PUBLISHED_V005_SQL,
        encoding="utf-8",
    )
    return target


def _catalog_through(root: Path, version: int, *, name: str) -> Path:
    target = root / name
    target.mkdir()
    for source in _migration_files():
        if _version(source) <= version:
            shutil.copy2(source, target / source.name)
    return target


def _current_v36_catalog(root: Path) -> Path:
    target = root / "current-v36-catalog"
    target.mkdir()
    for source in _migration_files():
        version = _version(source)
        if version <= 36 and version not in _RESTORED_HISTORY:
            shutil.copy2(source, target / source.name)
    return target


async def _build_published_v11(db_path: Path, root: Path) -> None:
    result = await run_migrations(db_path, migrations_dir=_published_v11_catalog(root))
    assert result.errors == [] and result.current_version == 11


async def _seed_published_v11(db_path: Path) -> None:
    await _insert_v11_legacy_data(db_path)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(
            """
            UPDATE speakers SET label_user_set = 1 WHERE speaker_id = 'speaker-1';
            UPDATE ambient_segments
               SET source = 'device', device_id = 'edge-synthetic'
             WHERE audio_ref = 'ambient.wav';

            INSERT INTO conversations (
                turn_id, role, text, source, device_id, speaker_id,
                speaker_label, trigger, created_at
            ) VALUES (
                'turn-synthetic', 'user', 'synthetic conversation', 'device',
                'edge-synthetic', 'speaker-1', 'Alice', 'manual',
                '2025-01-01T04:00:00+00:00'
            );
            INSERT INTO memory_nodes (
                content, kind, source, device_id, speaker_label, salience,
                hit_count, created_at, last_seen_at
            ) VALUES (
                'synthetic memory', 'fact', 'device', 'edge-synthetic', 'Alice',
                0.8, 2, '2025-01-01T04:00:00+00:00',
                '2025-01-01T04:05:00+00:00'
            );

            INSERT INTO users (
                id, email, display_name, password_hash, password_salt,
                plan_id, created_at, last_login_at
            ) VALUES (
                'legacy-user', 'legacy@example.invalid', 'Synthetic legacy user',
                'synthetic-password-hash', 'synthetic-salt', 'pro',
                '2025-01-01T00:00:00+00:00', NULL
            );
            INSERT INTO sessions (
                token_hash, user_id, created_at, expires_at, revoked
            ) VALUES (
                'synthetic-session-hash', 'legacy-user',
                '2025-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00', 0
            );
            INSERT INTO api_keys (
                id, user_id, name, key_prefix, key_hash, created_at,
                last_used_at, revoked
            ) VALUES (
                'legacy-key', 'legacy-user', 'Synthetic', 'synthetic_prefix',
                'synthetic-key-hash', '2025-01-01T00:00:00+00:00', NULL, 0
            );
            INSERT INTO usage_events (
                user_id, api_key_id, capability, units, unit_kind, provider,
                cost_micros, created_at
            ) VALUES (
                'legacy-user', 'legacy-key', 'llm', 7, 'tokens', 'ours', 0,
                '2025-01-01T00:00:00+00:00'
            );
            INSERT INTO user_model_config (user_id, updated_at)
            VALUES ('legacy-user', '2025-01-01T00:00:00+00:00');

            CREATE TABLE legacy_user_notes (
                user_id TEXT NOT NULL REFERENCES users(id),
                note TEXT NOT NULL
            );
            INSERT INTO legacy_user_notes(user_id, note)
            VALUES ('legacy-user', 'synthetic dependent row');
            CREATE VIEW legacy_user_emails AS SELECT id, email FROM users;
            """
        )
        await conn.commit()


async def _integrity(db_path: Path) -> tuple[str, list[tuple[Any, ...]]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        integrity = await (await conn.execute("PRAGMA integrity_check")).fetchone()
        foreign_keys = await (await conn.execute("PRAGMA foreign_key_check")).fetchall()
    assert integrity is not None
    return str(integrity[0]), [tuple(row) for row in foreign_keys]


async def _active_schema(db_path: Path) -> list[tuple[str, str, str, str]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_schema
                   WHERE sql IS NOT NULL
                     AND name NOT LIKE 'sqlite_%'
                     AND name <> 'schema_version'
                     AND name NOT LIKE 'legacy_%'
                     AND tbl_name NOT LIKE 'legacy_%'
                   ORDER BY type, name"""
            )
        ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]


@pytest.mark.unit
async def test_published_v11_upgrade_preserves_data_and_archives_old_control_plane(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "published-v11.db"
    await _build_published_v11(db_path, tmp_path)
    await _seed_published_v11(db_path)

    result = await run_migrations(db_path)

    assert result.errors == []
    assert result.current_version == 37
    assert result.not_applicable == []
    assert await _integrity(db_path) == ("ok", [])
    async with aiosqlite.connect(str(db_path)) as conn:
        core_counts = {
            table: int((await (await conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0])
            for table in ("meetings", "meeting_segments", "speakers", "artifacts", "artifact_links")
        }
        archived_counts = {
            table: int(
                (await (await conn.execute(f"SELECT COUNT(*) FROM legacy_v8_{table}")).fetchone())[
                    0
                ]
            )
            for table in _LEGACY_CONTROL_TABLES
        }
        conversation_count = await (
            await conn.execute("SELECT COUNT(*) FROM legacy_v6_conversations")
        ).fetchone()
        memory_count = await (
            await conn.execute("SELECT COUNT(*) FROM legacy_v7_memory_nodes")
        ).fetchone()
        speaker = await (
            await conn.execute(
                "SELECT label, label_user_set FROM speakers WHERE speaker_id = 'speaker-1'"
            )
        ).fetchone()
        ambient = await (
            await conn.execute(
                """SELECT source, device_id, tenant_id, owner_id
                   FROM ambient_segments WHERE audio_ref = 'ambient.wav'"""
            )
        ).fetchone()
        valid_link = await (
            await conn.execute(
                """SELECT l.link_id FROM artifact_links AS l
                   JOIN artifacts AS a
                     ON a.tenant_id = l.tenant_id AND a.owner_id = l.owner_id
                    AND a.artifact_id = l.artifact_id
                   WHERE l.link_id = 'legacy-link'"""
            )
        ).fetchone()
        archived_user = await (
            await conn.execute(
                "SELECT id, email, password_hash FROM legacy_v8_users WHERE id = 'legacy-user'"
            )
        ).fetchone()
        dependent_view = await (
            await conn.execute("SELECT id, email FROM legacy_user_emails")
        ).fetchone()
        fk_rows = await (
            await conn.execute("PRAGMA foreign_key_list(legacy_user_notes)")
        ).fetchall()
        active_legacy_names = await (
            await conn.execute(
                """SELECT name FROM sqlite_schema WHERE type = 'table'
                   AND name IN (
                       'sessions', 'api_keys', 'plans', 'usage_events',
                       'user_model_config', 'conversations', 'memory_nodes'
                   )"""
            )
        ).fetchall()
        registrations = await (
            await conn.execute(
                """SELECT version, description, migration_name, content_sha256
                   FROM schema_version WHERE version BETWEEN 5 AND 9
                   ORDER BY version"""
            )
        ).fetchall()

    assert core_counts == {
        "meetings": 1,
        "meeting_segments": 1,
        "speakers": 1,
        "artifacts": 1,
        "artifact_links": 1,
    }
    assert archived_counts == {
        "users": 1,
        "sessions": 1,
        "api_keys": 1,
        "plans": 2,
        "usage_events": 1,
        "user_model_config": 1,
    }
    assert conversation_count == (1,)
    assert memory_count == (1,)
    assert speaker == ("Alice", 1)
    assert ambient == ("device", "edge-synthetic", "legacy-local", "legacy-local")
    assert valid_link == ("legacy-link",)
    assert archived_user == (
        "legacy-user",
        "legacy@example.invalid",
        "synthetic-password-hash",
    )
    assert dependent_view == ("legacy-user", "legacy@example.invalid")
    assert any(str(row[2]) == "legacy_v8_users" for row in fk_rows)
    assert active_legacy_names == []

    registration_rows = [tuple(row) for row in registrations]
    assert registration_rows[0] == (5, "005_speaker_label_user_set", None, None)
    for version, description, migration_name, content_hash in registration_rows[1:]:
        source = next(path for path in _migration_files() if _version(path) == int(version))
        assert description == source.stem
        assert migration_name == source.name
        assert content_hash == sha256(source.read_bytes()).hexdigest()

    rerun = await run_migrations(db_path)
    assert rerun.errors == [] and rerun.applied == [] and rerun.not_applicable == []


@pytest.mark.unit
async def test_current_v36_switch_marks_restored_history_not_applicable_and_converges(
    tmp_path: Path,
) -> None:
    current_db = tmp_path / "current-v36.db"
    old_result = await run_migrations(
        current_db,
        migrations_dir=_current_v36_catalog(tmp_path),
    )
    assert old_result.errors == [] and old_result.current_version == 36
    async with aiosqlite.connect(str(current_db)) as conn:
        await conn.executescript(
            """
            INSERT INTO ambient_segments (
                audio_ref, text, duration_ms, captured_at,
                tenant_id, device_id, owner_id
            ) VALUES (
                'current.wav', 'current lineage', 100,
                '2026-01-01T00:00:00+00:00',
                'legacy-local', 'current-device', 'legacy-local'
            );
            INSERT INTO speakers (
                speaker_id, label, n_samples, first_seen_at, last_seen_at,
                tenant_id, device_id, owner_id
            ) VALUES (
                'current-speaker', 'Current', 1,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                'legacy-local', 'current-device', 'legacy-local'
            );
            """
        )
        await conn.commit()

    switched = await run_migrations(current_db)

    assert switched.errors == [] and switched.applied == [37]
    assert switched.not_applicable == [6, 7, 8, 9]
    assert switched.current_version == 37
    assert await _integrity(current_db) == ("ok", [])
    async with aiosqlite.connect(str(current_db)) as conn:
        history_rows = await (
            await conn.execute("SELECT version FROM schema_version WHERE version BETWEEN 6 AND 9")
        ).fetchall()
        ambient = await (
            await conn.execute(
                "SELECT source, device_id FROM ambient_segments WHERE audio_ref = 'current.wav'"
            )
        ).fetchone()
        speaker = await (
            await conn.execute(
                "SELECT label_user_set FROM speakers WHERE speaker_id = 'current-speaker'"
            )
        ).fetchone()
    assert history_rows == []
    assert ambient == ("local", "current-device")
    assert speaker == (0,)

    fresh_db = tmp_path / "fresh.db"
    assert (await run_migrations(fresh_db)).errors == []
    published_db = tmp_path / "published-v11-for-schema.db"
    await _build_published_v11(published_db, tmp_path / "published-schema")
    assert (await run_migrations(published_db)).errors == []
    assert await _active_schema(current_db) == await _active_schema(fresh_db)
    assert await _active_schema(fresh_db) == await _active_schema(published_db)

    rerun = await run_migrations(current_db)
    assert rerun.errors == [] and rerun.applied == []
    assert rerun.not_applicable == [6, 7, 8, 9]


@pytest.mark.unit
async def test_published_v18_archive_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "published-v17.db"
    await _build_published_v11(db_path, tmp_path)
    await _seed_published_v11(db_path)
    through_17 = _catalog_through(tmp_path, 17, name="through-17")
    assert (await run_migrations(db_path, migrations_dir=through_17)).errors == []

    failing = tmp_path / "failing-v18"
    failing.mkdir()
    (failing / "018_identity_fails.sql").write_text(
        """CREATE TABLE users (
               tenant_id TEXT NOT NULL,
               user_id TEXT NOT NULL,
               PRIMARY KEY (tenant_id, user_id)
           );
           INSERT INTO missing_after_archive(id) VALUES (1);""",
        encoding="utf-8",
    )
    failed = await run_migrations(db_path, migrations_dir=failing)

    assert failed.current_version == 17
    assert len(failed.errors) == 1 and "missing_after_archive" in failed.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        original_user = await (
            await conn.execute("SELECT id, email FROM users WHERE id = 'legacy-user'")
        ).fetchone()
        archived = await (
            await conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'legacy_v8_%'")
        ).fetchall()
        view_row = await (await conn.execute("SELECT id, email FROM legacy_user_emails")).fetchone()
        fk_rows = await (
            await conn.execute("PRAGMA foreign_key_list(legacy_user_notes)")
        ).fetchall()
        version_18 = await (
            await conn.execute("SELECT 1 FROM schema_version WHERE version = 18")
        ).fetchone()
    assert original_user == ("legacy-user", "legacy@example.invalid")
    assert archived == []
    assert view_row == original_user
    assert any(str(row[2]) == "users" for row in fk_rows)
    assert version_18 is None
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
async def test_v37_rebuild_guard_rolls_back_compatibility_prelude(tmp_path: Path) -> None:
    db_path = tmp_path / "guarded-current-v36.db"
    old_catalog = _current_v36_catalog(tmp_path)
    assert (await run_migrations(db_path, migrations_dir=old_catalog)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE VIEW custom_ambient_view AS SELECT id, text FROM ambient_segments"
        )
        await conn.commit()

    blocked = await run_migrations(db_path)

    assert blocked.current_version == 36
    assert len(blocked.errors) == 1
    assert "view:custom_ambient_view" in blocked.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        columns = {
            str(row[1])
            for row in await (await conn.execute("PRAGMA table_info(ambient_segments)")).fetchall()
        }
        view = await (
            await conn.execute("SELECT 1 FROM sqlite_schema WHERE name='custom_ambient_view'")
        ).fetchone()
        version_37 = await (
            await conn.execute("SELECT 1 FROM schema_version WHERE version = 37")
        ).fetchone()
        await conn.execute("DROP VIEW custom_ambient_view")
        await conn.commit()
    assert "source" not in columns
    assert view is not None and version_37 is None

    resumed = await run_migrations(db_path)
    assert resumed.errors == [] and resumed.applied == [37]
    assert await _integrity(db_path) == ("ok", [])


def _process_upgrade(
    db_path: str,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    ready_queue.put(True)
    start_event.wait(timeout=30)
    result = asyncio.run(run_migrations(Path(db_path)))
    result_queue.put((result.errors, result.current_version, result.applied))


@pytest.mark.unit
def test_two_processes_upgrade_published_v11_without_duplicate_or_lock_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "published-concurrent.db"
    asyncio.run(_build_published_v11(db_path, tmp_path))
    asyncio.run(_seed_published_v11(db_path))
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_process_upgrade,
            args=(str(db_path), ready_queue, start_event, result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready_queue.get(timeout=30) is True
    start_event.set()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    ready_queue.close()
    result_queue.close()

    pending_versions = [version for version in map(_version, _migration_files()) if version >= 12]
    assert all(errors == [] and current == 37 for errors, current, _applied in results)
    assert sum(len(applied) for _errors, _current, applied in results) == len(pending_versions)
    assert asyncio.run(_integrity(db_path)) == ("ok", [])

    async def read_label() -> tuple[str, int] | None:
        async with aiosqlite.connect(str(db_path)) as conn:
            row = await (
                await conn.execute(
                    "SELECT label, label_user_set FROM speakers WHERE speaker_id='speaker-1'"
                )
            ).fetchone()
        return tuple(row) if row is not None else None  # type: ignore[return-value]

    assert asyncio.run(read_label()) == ("Alice", 1)
