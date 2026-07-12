from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations
from app.security import (
    LEGACY_DEVICE_ID,
    LEGACY_OWNER_ID,
    LEGACY_TENANT_ID,
    ExpiredSessionError,
    InvalidSessionError,
    ResourceTicketError,
    RevokedSessionError,
    SessionIssueLimiter,
    SessionIssueRateLimitError,
    SessionStore,
    local_principal,
)


async def _migrated_store(
    tmp_path: Path,
    *,
    now: datetime | None = None,
) -> tuple[SessionStore, Path, list[datetime]]:
    db_path = tmp_path / "sessions.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    clock = [now or datetime(2026, 7, 11, 8, 0, tzinfo=UTC)]
    return SessionStore(db_path, now=lambda: clock[0]), db_path, clock


@pytest.mark.unit
def test_session_issue_limiter_has_bounded_clients_and_recovers_after_window() -> None:
    clock = [100.0]
    limiter = SessionIssueLimiter(
        max_requests=1,
        window_s=10,
        max_clients=2,
        clock=lambda: clock[0],
    )

    limiter.check("client-a")
    with pytest.raises(SessionIssueRateLimitError):
        limiter.check("client-a")
    limiter.check("client-b")
    limiter.check("client-c")
    assert limiter.tracked_clients == 2

    clock[0] += 11
    limiter.check("client-c")


@pytest.mark.unit
def test_local_principal_is_fixed_legacy_owner() -> None:
    principal = local_principal()

    assert principal.tenant_id == LEGACY_TENANT_ID == "legacy-local"
    assert principal.device_id == LEGACY_DEVICE_ID == "legacy-local"
    assert principal.owner_id == LEGACY_OWNER_ID == "legacy-local"
    assert principal.session_id == "local-fixed"
    assert principal.mode == "local"


@pytest.mark.unit
async def test_public_session_is_server_issued_and_only_token_hash_is_stored(
    tmp_path: Path,
) -> None:
    store, db_path, _clock = await _migrated_store(tmp_path)

    issued = await store.issue_public_session(ttl=timedelta(hours=1))
    principal = await store.validate_public_token(issued.token)

    assert issued.token
    assert principal == issued.principal
    assert principal.mode == "public"
    assert principal.tenant_id.startswith("tenant_")
    assert principal.device_id.startswith("device_")
    assert principal.owner_id.startswith("owner_")
    assert principal.session_id.startswith("session_")

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (
            await conn.execute(
                "SELECT token_hash, tenant_id, device_id, owner_id FROM principal_sessions "
                "WHERE session_id = ?",
                (principal.session_id,),
            )
        ).fetchone()
        columns = {
            str(item[1])
            for item in await (
                await conn.execute("PRAGMA table_info(principal_sessions)")
            ).fetchall()
        }

    assert row is not None
    assert row["token_hash"] == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in row["token_hash"]
    assert "token" not in columns
    assert (row["tenant_id"], row["device_id"], row["owner_id"]) == (
        principal.tenant_id,
        principal.device_id,
        principal.owner_id,
    )


@pytest.mark.unit
async def test_public_session_rejects_expired_token(tmp_path: Path) -> None:
    store, _db_path, clock = await _migrated_store(tmp_path)
    issued = await store.issue_public_session(ttl=timedelta(seconds=30))

    clock[0] += timedelta(seconds=31)

    with pytest.raises(ExpiredSessionError):
        await store.validate_public_token(issued.token)


@pytest.mark.unit
async def test_public_session_rejects_revoked_token(tmp_path: Path) -> None:
    store, _db_path, _clock = await _migrated_store(tmp_path)
    issued = await store.issue_public_session()

    assert await store.revoke_session(issued.principal.session_id) is True
    assert await store.revoke_session(issued.principal.session_id) is False
    with pytest.raises(RevokedSessionError):
        await store.validate_public_token(issued.token)


@pytest.mark.unit
async def test_public_session_rejects_forged_token(tmp_path: Path) -> None:
    store, _db_path, _clock = await _migrated_store(tmp_path)
    issued = await store.issue_public_session()

    forged = issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B")
    with pytest.raises(InvalidSessionError):
        await store.validate_public_token(forged)


@pytest.mark.unit
async def test_resource_ticket_is_narrow_expires_and_dies_with_session(tmp_path: Path) -> None:
    store, _db_path, clock = await _migrated_store(tmp_path)
    issued = await store.issue_public_session(ttl=timedelta(hours=1))
    ticket = await store.issue_resource_ticket(
        issued.principal,
        resource_type="meeting",
        resource_id="meeting-a",
        ttl=timedelta(minutes=10),
    )

    assert (
        await store.validate_resource_ticket(
            ticket, resource_type="meeting", resource_id="meeting-a"
        )
        == issued.principal
    )
    with pytest.raises(ResourceTicketError):
        await store.validate_resource_ticket(
            ticket, resource_type="meeting", resource_id="meeting-b"
        )
    with pytest.raises(ResourceTicketError):
        await store.validate_resource_ticket(
            ticket, resource_type="artifact", resource_id="meeting-a"
        )

    clock[0] += timedelta(minutes=11)
    with pytest.raises(ResourceTicketError):
        await store.validate_resource_ticket(
            ticket, resource_type="meeting", resource_id="meeting-a"
        )

    clock[0] -= timedelta(minutes=11)
    assert await store.revoke_session(issued.principal.session_id)
    with pytest.raises(ResourceTicketError):
        await store.validate_resource_ticket(
            ticket, resource_type="meeting", resource_id="meeting-a"
        )


@pytest.mark.unit
async def test_cleanup_expired_sessions_removes_dependent_tickets_first(tmp_path: Path) -> None:
    store, db_path, clock = await _migrated_store(tmp_path)
    expired = await store.issue_public_session(ttl=timedelta(seconds=5))
    active = await store.issue_public_session(ttl=timedelta(hours=1))
    await store.issue_resource_ticket(
        expired.principal,
        resource_type="meeting",
        resource_id="stale-meeting",
        ttl=timedelta(hours=1),
    )

    clock[0] += timedelta(seconds=6)
    assert await store.cleanup_expired_sessions() == (1, 1)

    async with aiosqlite.connect(str(db_path)) as conn:
        sessions = await (
            await conn.execute("SELECT session_id FROM principal_sessions ORDER BY session_id")
        ).fetchall()
        tickets = await (await conn.execute("SELECT ticket_id FROM resource_tickets")).fetchall()
    assert sessions == [(active.principal.session_id,)]
    assert tickets == []


async def _apply_pre_012_migrations(db_path: Path, tmp_path: Path) -> None:
    old_dir = tmp_path / "old-migrations"
    old_dir.mkdir()
    for source in sorted(_DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        version = int(source.name.split("_", 1)[0])
        if version <= 11:
            (old_dir / source.name).write_bytes(source.read_bytes())
    result = await run_migrations(db_path, migrations_dir=old_dir)
    assert result.errors == []
    assert result.current_version == 11


async def _seed_legacy_rows(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO meetings (id, state, started_at) VALUES ('meeting-legacy', 'ended', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO ambient_segments (audio_ref, text, captured_at) "
            "VALUES ('a.wav', 'legacy', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO speakers (speaker_id, first_seen_at, last_seen_at) "
            "VALUES ('speaker-legacy', '2026-01-01', '2026-01-01')"
        )
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, created_at, updated_at)
               VALUES ('run-legacy', 'artifact.generate', 'test', 'succeeded', 'legacy',
                       '2026-01-01', '2026-01-01')"""
        )
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at)
               VALUES ('artifact-legacy', 'pdf', '/tmp/legacy.pdf', 'application/pdf',
                       '2026-01-01', '2026-01-01')"""
        )
        await conn.execute(
            """INSERT INTO agent_tasks
               (task_id, device_id, title, intent_text, state, submitted_at)
               VALUES ('task-legacy', 'old-device', 'legacy', 'legacy', 'succeeded', '2026-01-01')"""
        )
        await conn.execute(
            """INSERT INTO agent_runner_grants
               (grant_id, device_id, runner, permission_profile, permission_mode, granted_at)
               VALUES ('grant-legacy-old', 'old-device', 'claude_code', 'full', 'bypass',
                       '2026-01-01')"""
        )
        await conn.execute(
            """INSERT INTO agent_runner_grants
               (grant_id, device_id, runner, permission_profile, permission_mode, granted_at)
               VALUES ('grant-legacy-new', 'other-device', 'claude_code', 'full', 'bypass',
                       '2026-01-02')"""
        )
        await conn.commit()


@pytest.mark.unit
async def test_012_migration_backfills_legacy_owner_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    await _apply_pre_012_migrations(db_path, tmp_path)
    await _seed_legacy_rows(db_path)
    migration_dir = tmp_path / "migration-012"
    migration_dir.mkdir()
    source = _DEFAULT_MIGRATIONS_DIR / "012_principals.sql"
    (migration_dir / source.name).write_bytes(source.read_bytes())

    result = await run_migrations(db_path, migrations_dir=migration_dir)

    assert result.errors == []
    assert result.applied == [12]
    expected_tables = {
        "meetings": "meeting-legacy",
        "ambient_segments": 1,
        "speakers": "speaker-legacy",
        "workflow_runs": "run-legacy",
        "artifacts": "artifact-legacy",
        "agent_tasks": "task-legacy",
        "agent_runner_grants": "grant-legacy-new",
    }
    pk_by_table = {
        "meetings": "id",
        "ambient_segments": "id",
        "speakers": "speaker_id",
        "workflow_runs": "run_id",
        "artifacts": "artifact_id",
        "agent_tasks": "task_id",
        "agent_runner_grants": "grant_id",
    }
    async with aiosqlite.connect(str(db_path)) as conn:
        scoped_tables = (
            "meetings",
            "meeting_segments",
            "meeting_speaker_labels",
            "ambient_segments",
            "speakers",
            "workflow_runs",
            "workflow_events",
            "artifacts",
            "artifact_links",
            "agent_tasks",
            "agent_task_events",
            "agent_runner_grants",
            "rag_documents",
        )
        for table in scoped_tables:
            columns = {
                str(item[1])
                for item in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
            }
            assert {"tenant_id", "device_id", "owner_id"} <= columns, table

        for table, pk in expected_tables.items():
            key = pk_by_table[table]
            row = await (
                await conn.execute(
                    f"SELECT tenant_id, device_id, owner_id FROM {table} WHERE {key} = ?",
                    (pk,),
                )
            ).fetchone()
            assert row == ("legacy-local", "legacy-local", "legacy-local"), table

        rag_columns = {
            str(item[1])
            for item in await (await conn.execute("PRAGMA table_info(rag_documents)")).fetchall()
        }
        index_names = {
            str(item[0])
            for item in await (
                await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%_owner%'"
                )
            ).fetchall()
        }
        active_grants = await (
            await conn.execute("SELECT grant_id FROM agent_runner_grants WHERE revoked_at IS NULL")
        ).fetchall()
        active_grant_ids = [str(row[0]) for row in active_grants]

    assert {"tenant_id", "device_id", "owner_id", "doc_id", "source"} <= rag_columns
    assert {
        "idx_meetings_owner_started",
        "idx_ambient_segments_owner_captured",
        "idx_workflow_runs_owner_state",
        "idx_artifacts_owner_created",
        "idx_agent_tasks_owner_state",
        "idx_agent_runner_grants_owner_active",
        "idx_rag_documents_owner_source",
    } <= index_names
    assert active_grant_ids == ["grant-legacy-new"]
