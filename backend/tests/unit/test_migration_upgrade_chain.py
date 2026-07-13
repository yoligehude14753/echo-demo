"""Upgrade-chain, crash recovery, and concurrent migrator invariants."""

from __future__ import annotations

import asyncio
import multiprocessing
import shutil
from collections.abc import Iterable
from hashlib import sha256
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations


def _version(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def _migration_files() -> list[Path]:
    return sorted(_DEFAULT_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"), key=_version)


def _copy_migrations_through(root: Path, version: int) -> Path:
    target = root / f"migrations-through-{version}"
    target.mkdir()
    for source in _migration_files():
        if _version(source) <= version:
            shutil.copy2(source, target / source.name)
    return target


async def _integrity(db_path: Path) -> tuple[str, list[tuple[Any, ...]]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        integrity_row = await (await conn.execute("PRAGMA integrity_check")).fetchone()
        foreign_key_rows = await (await conn.execute("PRAGMA foreign_key_check")).fetchall()
    assert integrity_row is not None
    return str(integrity_row[0]), [tuple(row) for row in foreign_key_rows]


async def _schema_signature(db_path: Path) -> list[tuple[str, str, str, str | None]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_schema
                   WHERE name NOT LIKE 'sqlite_%'
                   ORDER BY type, name"""
            )
        ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2]), row[3]) for row in rows]


async def _migration_registrations(db_path: Path) -> dict[int, tuple[str | None, str | None]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT version, migration_name, content_sha256
                   FROM schema_version ORDER BY version"""
            )
        ).fetchall()
    return {int(row[0]): (row[1], row[2]) for row in rows}


def _expected_registrations() -> dict[int, tuple[str, str]]:
    return {
        _version(path): (path.name, sha256(path.read_bytes()).hexdigest())
        for path in _migration_files()
    }


async def _insert_v11_legacy_data(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(
            """
            INSERT INTO meetings (
                id, title, state, started_at, ended_at, finalized_at,
                auto_started, minutes_json, raw_transcript_ref,
                minutes_status, minutes_error, display_title
            ) VALUES (
                'legacy-meeting', 'Legacy meeting', 'finalized',
                '2025-01-01T00:00:00+00:00', '2025-01-01T01:00:00+00:00',
                '2025-01-01T01:01:00+00:00', 0, '{"summary":"kept"}',
                'legacy-transcript', 'ok', NULL, 'Legacy display title'
            );
            INSERT INTO meeting_segments (
                meeting_id, text, start_ms, end_ms, speaker_id,
                speaker_label, captured_at
            ) VALUES (
                'legacy-meeting', 'preserved transcript', 10, 20,
                'speaker-1', 'Alice', '2025-01-01T00:00:10+00:00'
            );
            INSERT INTO meeting_speaker_labels (meeting_id, speaker_id, label)
            VALUES ('legacy-meeting', 'speaker-1', 'Alice');
            INSERT INTO ambient_segments (
                audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at
            ) VALUES (
                'ambient.wav', 'preserved ambient', 'speaker-1', 'Alice', 500,
                '2025-01-01T02:00:00+00:00'
            );
            INSERT INTO speakers (
                speaker_id, label, n_samples, first_seen_at, last_seen_at,
                embedding_blob
            ) VALUES (
                'speaker-1', 'Alice', 3, '2025-01-01T00:00:00+00:00',
                '2025-01-01T02:00:00+00:00', X'0102'
            );
            INSERT INTO workflow_runs (
                run_id, kind, source, state, title, intent_text, meeting_id,
                todo_id, agent_task_id, input_json, output_json, error,
                timeout_s, created_at, started_at, finished_at, updated_at
            ) VALUES (
                'legacy-run', 'artifact', 'chat', 'succeeded', 'Legacy run',
                'make artifact', 'legacy-meeting', 'todo-1', 'legacy-task',
                '{"input":1}', '{"output":1}', NULL, 60,
                '2025-01-01T03:00:00+00:00', '2025-01-01T03:00:01+00:00',
                '2025-01-01T03:00:02+00:00', '2025-01-01T03:00:02+00:00'
            );
            INSERT INTO workflow_events (
                run_id, seq, event_type, state, visibility, message,
                payload_json, created_at
            ) VALUES (
                'legacy-run', 1, 'completed', 'succeeded', 'user', 'done',
                '{"kept":true}', '2025-01-01T03:00:02+00:00'
            );
            INSERT INTO artifacts (
                artifact_id, artifact_type, title, file_path, mime_type,
                size_bytes, generation_latency_ms, model, metadata_json,
                run_id, created_at, updated_at
            ) VALUES (
                'legacy-artifact', 'xlsx', 'Legacy artifact', '/tmp/legacy.xlsx',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                123, 45, 'legacy-model', '{"kept":true}', 'legacy-run',
                '2025-01-01T03:00:02+00:00', '2025-01-01T03:00:02+00:00'
            );
            INSERT INTO artifact_links (
                link_id, artifact_id, source, meeting_id, todo_id, run_id, created_at
            ) VALUES (
                'legacy-link', 'legacy-artifact', 'meeting_todo',
                'legacy-meeting', 'todo-1', 'legacy-run',
                '2025-01-01T03:00:02+00:00'
            );
            INSERT INTO agent_runner_grants (
                grant_id, device_id, runner, permission_profile, permission_mode,
                workspace_ids_json, granted_at, revoked_at, last_used_at
            ) VALUES (
                'legacy-grant', 'old-device', 'codex', 'full', 'allow', '[]',
                '2025-01-01T02:59:00+00:00', NULL, NULL
            );
            INSERT INTO agent_tasks (
                task_id, runner_task_id, device_id, conversation_id, message_id,
                title, intent_text, route, task_kind, state, progress_text,
                final_text, error, artifacts_json, snapshot_json, envelope_json,
                grant_id, permission_profile, last_seq, submitted_at, finished_at,
                timeout_s, workflow_run_id
            ) VALUES (
                'legacy-task', 'runner-task', 'old-device', 'conversation-1',
                'message-1', 'Legacy task', 'do work', 'codex', 'artifact',
                'succeeded', 'done', 'finished', NULL, '[]', '{}', '{}',
                'legacy-grant', 'full', 1, '2025-01-01T03:00:00+00:00',
                '2025-01-01T03:00:02+00:00', 60, 'legacy-run'
            );
            INSERT INTO agent_task_events (
                task_id, seq, event, state, visibility, payload_json,
                raw_event_hash, created_at
            ) VALUES (
                'legacy-task', 1, 'completed', 'succeeded', 'user',
                '{"kept":true}', 'event-hash', '2025-01-01T03:00:02+00:00'
            );
            """
        )
        await conn.commit()


async def _insert_v1_legacy_data(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(
            """
            INSERT INTO meetings (
                id, title, state, started_at, ended_at, finalized_at,
                auto_started, minutes_json, raw_transcript_ref
            ) VALUES (
                'v1-meeting', 'Version one meeting', 'ended',
                '2024-01-01T00:00:00+00:00', '2024-01-01T00:30:00+00:00',
                NULL, 1, NULL, 'v1-transcript'
            );
            INSERT INTO meeting_segments (
                meeting_id, text, start_ms, end_ms, speaker_id,
                speaker_label, captured_at
            ) VALUES (
                'v1-meeting', 'version one transcript', 0, 1000, 'v1-speaker',
                'Legacy speaker', '2024-01-01T00:00:01+00:00'
            );
            INSERT INTO meeting_speaker_labels (meeting_id, speaker_id, label)
            VALUES ('v1-meeting', 'v1-speaker', 'Legacy speaker');
            INSERT INTO ambient_segments (
                audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at
            ) VALUES (
                'v1.wav', 'version one ambient', 'v1-speaker', 'Legacy speaker',
                1000, '2024-01-01T01:00:00+00:00'
            );
            INSERT INTO speakers (
                speaker_id, label, n_samples, first_seen_at, last_seen_at,
                embedding_blob
            ) VALUES (
                'v1-speaker', 'Legacy speaker', 1,
                '2024-01-01T00:00:00+00:00', '2024-01-01T01:00:00+00:00',
                X'03'
            );
            """
        )
        await conn.commit()


async def _insert_v18_public_principal(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(
            """
            INSERT INTO tenants (tenant_id, status, created_at, updated_at)
            VALUES (
                'tenant-public', 'active', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO users (tenant_id, user_id, status, created_at, updated_at)
            VALUES (
                'tenant-public', 'owner-public', 'active',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO devices (
                tenant_id, user_id, device_id, display_name, created_at,
                last_seen_at, legacy_claimed_at, revoked_at
            ) VALUES (
                'tenant-public', 'owner-public', 'device-public', 'Public device',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                NULL, NULL
            );
            INSERT INTO session_families (
                family_id, tenant_id, user_id, device_id, created_at,
                last_renewed_at, generation, revoked_at
            ) VALUES (
                'family-public', 'tenant-public', 'owner-public', 'device-public',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                0, NULL
            );
            INSERT INTO device_credentials (
                credential_id, credential_hash, family_id, tenant_id, user_id,
                device_id, issued_at, expires_at, last_used_at, revoked_at,
                rotated_to_credential_id
            ) VALUES (
                'credential-public', 'credential-hash', 'family-public',
                'tenant-public', 'owner-public', 'device-public',
                '2026-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00',
                NULL, NULL, NULL
            );
            INSERT INTO public_enrollments (
                enrollment_id_hash, device_secret_hash, peer_key_hash, family_id,
                tenant_id, user_id, device_id, created_at
            ) VALUES (
                'enrollment-hash', 'device-secret-hash', 'peer-hash',
                'family-public', 'tenant-public', 'owner-public', 'device-public',
                '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO principal_sessions (
                session_id, token_hash, tenant_id, device_id, owner_id, mode,
                issued_at, expires_at, revoked_at, family_id, generation,
                renewed_from_session_id
            ) VALUES (
                'session-public', 'session-token-hash', 'tenant-public',
                'device-public', 'owner-public', 'public',
                '2026-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00',
                NULL, 'family-public', 0, NULL
            );
            INSERT INTO resource_tickets (
                ticket_id, token_hash, session_id, tenant_id, device_id,
                owner_id, resource_type, resource_id, capability, issued_at,
                expires_at, revoked_at
            ) VALUES (
                'ticket-public', 'ticket-token-hash', 'session-public',
                'tenant-public', 'device-public', 'owner-public', 'meeting',
                'resource-public', 'read', '2026-01-01T00:00:00+00:00',
                '2030-01-01T00:00:00+00:00', NULL
            );
            """
        )
        await conn.commit()


async def _assert_upgraded_data(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        meeting = await (
            await conn.execute(
                """SELECT title, minutes_json, tenant_id, device_id, owner_id
                   FROM meetings WHERE id = 'legacy-meeting'"""
            )
        ).fetchone()
        assert meeting is not None
        assert tuple(meeting) == (
            "Legacy meeting",
            '{"summary":"kept"}',
            "legacy-local",
            "legacy-local",
            "legacy-local",
        )
        segment = await (
            await conn.execute(
                """SELECT text, tenant_id, device_id, owner_id
                   FROM meeting_segments WHERE meeting_id = 'legacy-meeting'"""
            )
        ).fetchone()
        assert segment is not None
        assert tuple(segment) == (
            "preserved transcript",
            "legacy-local",
            "legacy-local",
            "legacy-local",
        )
        workflow = await (
            await conn.execute(
                """SELECT meeting_id, input_json, output_json, tenant_id, owner_id
                   FROM workflow_runs WHERE run_id = 'legacy-run'"""
            )
        ).fetchone()
        assert workflow is not None
        assert tuple(workflow) == (
            "legacy-meeting",
            '{"input":1}',
            '{"output":1}',
            "legacy-local",
            "legacy-local",
        )
        artifact = await (
            await conn.execute(
                """SELECT run_id, size_bytes, metadata_json, tenant_id, owner_id
                   FROM artifacts WHERE artifact_id = 'legacy-artifact'"""
            )
        ).fetchone()
        assert artifact is not None
        assert tuple(artifact) == (
            "legacy-run",
            123,
            '{"kept":true}',
            "legacy-local",
            "legacy-local",
        )
        principal = await (
            await conn.execute(
                """SELECT session.family_id, credential.credential_id,
                          enrollment.enrollment_id_hash, ticket.ticket_id
                   FROM principal_sessions AS session
                   JOIN device_credentials AS credential
                     ON credential.family_id = session.family_id
                    AND credential.tenant_id = session.tenant_id
                    AND credential.user_id = session.owner_id
                    AND credential.device_id = session.device_id
                   JOIN public_enrollments AS enrollment
                     ON enrollment.family_id = session.family_id
                    AND enrollment.tenant_id = session.tenant_id
                    AND enrollment.user_id = session.owner_id
                    AND enrollment.device_id = session.device_id
                   JOIN resource_tickets AS ticket
                     ON ticket.session_id = session.session_id
                    AND ticket.tenant_id = session.tenant_id
                    AND ticket.owner_id = session.owner_id
                    AND ticket.device_id = session.device_id
                   WHERE session.session_id = 'session-public'"""
            )
        ).fetchone()
        assert principal is not None
        assert tuple(principal) == (
            "family-public",
            "credential-public",
            "enrollment-hash",
            "ticket-public",
        )


async def _index_names(db_path: Path) -> set[str]:
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (
            await conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index' AND sql IS NOT NULL"
            )
        ).fetchall()
    return {str(row[0]) for row in rows}


async def _foreign_keys(db_path: Path, table: str) -> list[tuple[Any, ...]]:
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (await conn.execute(f"PRAGMA foreign_key_list({table})")).fetchall()
    return [tuple(row) for row in rows]


@pytest.mark.unit
async def test_v1_baseline_with_real_meeting_data_upgrades_without_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "v1-upgrade.db"
    through_1 = _copy_migrations_through(tmp_path, 1)
    assert (await run_migrations(db_path, migrations_dir=through_1)).errors == []
    await _insert_v1_legacy_data(db_path)

    result = await run_migrations(db_path)

    assert result.errors == []
    assert result.current_version == _version(_migration_files()[-1])
    assert await _migration_registrations(db_path) == _expected_registrations()
    async with aiosqlite.connect(str(db_path)) as conn:
        meeting = await (
            await conn.execute(
                """SELECT title, state, raw_transcript_ref, minutes_status,
                          tenant_id, device_id, owner_id
                   FROM meetings WHERE id = 'v1-meeting'"""
            )
        ).fetchone()
        segment = await (
            await conn.execute(
                """SELECT text, speaker_id, tenant_id, device_id, owner_id
                   FROM meeting_segments WHERE meeting_id = 'v1-meeting'"""
            )
        ).fetchone()
        speaker = await (
            await conn.execute(
                """SELECT label, n_samples, tenant_id, device_id, owner_id
                   FROM speakers WHERE speaker_id = 'v1-speaker'"""
            )
        ).fetchone()
        legacy_identity = await (
            await conn.execute(
                """SELECT tenant.tenant_id, user.user_id, device.device_id
                   FROM tenants AS tenant
                   JOIN users AS user ON user.tenant_id = tenant.tenant_id
                   JOIN devices AS device
                     ON device.tenant_id = user.tenant_id
                    AND device.user_id = user.user_id
                   WHERE tenant.tenant_id = 'legacy-local'"""
            )
        ).fetchone()
    assert meeting == (
        "Version one meeting",
        "ended",
        "v1-transcript",
        "generation_failed",
        "legacy-local",
        "legacy-local",
        "legacy-local",
    )
    assert segment == (
        "version one transcript",
        "v1-speaker",
        "legacy-local",
        "legacy-local",
        "legacy-local",
    )
    assert speaker == (
        "Legacy speaker",
        1,
        "legacy-local",
        "legacy-local",
        "legacy-local",
    )
    assert legacy_identity == ("legacy-local", "legacy-local", "legacy-local")
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
async def test_v11_legacy_data_and_v18_principal_upgrade_to_latest_matches_fresh_schema(
    tmp_path: Path,
) -> None:
    upgraded = tmp_path / "upgraded.db"
    through_11 = _copy_migrations_through(tmp_path, 11)
    through_18 = _copy_migrations_through(tmp_path, 18)
    assert (await run_migrations(upgraded, migrations_dir=through_11)).errors == []
    await _insert_v11_legacy_data(upgraded)
    assert (await run_migrations(upgraded, migrations_dir=through_18)).errors == []
    await _insert_v18_public_principal(upgraded)

    result = await run_migrations(upgraded)

    assert result.errors == []
    assert result.current_version == _version(_migration_files()[-1])
    assert await _migration_registrations(upgraded) == _expected_registrations()
    await _assert_upgraded_data(upgraded)
    assert await _integrity(upgraded) == ("ok", [])

    critical_indexes = {
        "idx_meeting_segments_meeting",
        "idx_workflow_idempotency",
        "idx_workflow_active_key",
        "idx_artifact_links_dedupe",
        "idx_agent_task_events_raw",
        "idx_agent_runner_grants_owner_active_unique",
        "idx_principal_sessions_one_active_family",
        "idx_device_credentials_one_active_family",
    }
    assert critical_indexes <= await _index_names(upgraded)
    meeting_segment_fks = await _foreign_keys(upgraded, "meeting_segments")
    assert any(row[2] == "meetings" and row[6] == "CASCADE" for row in meeting_segment_fks)
    ticket_fks = await _foreign_keys(upgraded, "resource_tickets")
    assert any(row[2] == "principal_sessions" and row[6] == "CASCADE" for row in ticket_fks)

    fresh = tmp_path / "fresh.db"
    fresh_result = await run_migrations(fresh)
    assert fresh_result.errors == []
    assert await _integrity(fresh) == ("ok", [])
    assert await _schema_signature(upgraded) == await _schema_signature(fresh)

    rerun = await run_migrations(upgraded)
    assert rerun.errors == []
    assert rerun.applied == []
    assert rerun.skipped == [_version(path) for path in _migration_files()]
    assert await _integrity(upgraded) == ("ok", [])


@pytest.mark.unit
async def test_v30_atomically_backfills_legacy_rows_without_checksum_columns(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-before-checksums.db"
    legacy_catalog = _copy_migrations_through(tmp_path, 31)
    next(legacy_catalog.glob("030_*.sql")).unlink()
    legacy = await run_migrations(db_path, migrations_dir=legacy_catalog)
    assert legacy.errors == []
    assert legacy.current_version == 31
    async with aiosqlite.connect(str(db_path)) as conn:
        columns = {
            str(row[1])
            for row in await (await conn.execute("PRAGMA table_info(schema_version)")).fetchall()
        }
    assert "migration_name" not in columns
    assert "content_sha256" not in columns

    upgraded = await run_migrations(db_path)

    assert upgraded.errors == []
    assert 30 in upgraded.applied
    assert 31 in upgraded.skipped
    assert await _migration_registrations(db_path) == _expected_registrations()
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
async def test_v36_backfills_existing_cancel_requested_agent_commands(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v35-cancel-requested.db"
    through_35 = _copy_migrations_through(tmp_path, 35)
    assert (await run_migrations(db_path, migrations_dir=through_35)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, title, intent_text, input_json,
                output_json, timeout_s, created_at, updated_at, tenant_id,
                device_id, owner_id, revision, attempt, cancel_requested_at)
               VALUES ('run-cancel', 'agent_task', 'command', 'cancel_requested',
                       'legacy cancel', 'cancel me', '{}', '{}', 30, 'now', 'now',
                       'tenant-a', 'device-a', 'owner-a', 1, 1, 'now')"""
        )
        await conn.execute(
            """INSERT INTO agent_tasks
               (task_id, runner_task_id, device_id, title, intent_text, route,
                state, artifacts_json, snapshot_json, envelope_json, last_seq,
                submitted_at, timeout_s, workflow_run_id, tenant_id, owner_id)
               VALUES ('task-cancel', 'runner-cancel', 'device-a', 'legacy cancel',
                       'cancel me', 'claude_code', 'cancel_requested', '[]', '{}',
                       '{}', 1, 'now', 30, 'run-cancel', 'tenant-a', 'owner-a')"""
        )
        await conn.commit()

    upgraded = await run_migrations(db_path)

    assert upgraded.errors == [] and upgraded.applied == [36, 37, 38]
    async with aiosqlite.connect(str(db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT tenant_id, owner_id, device_id, task_id, runner_task_id,
                          command_type, completed_at, force_remote
                   FROM agent_command_outbox"""
            )
        ).fetchone()
    assert row is not None and tuple(row) == (
        "tenant-a",
        "owner-a",
        "device-a",
        "task-cancel",
        "runner-cancel",
        "cancel",
        None,
        0,
    )
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
async def test_v33_audits_and_closes_historical_duplicate_active_meetings(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "duplicate-active-meetings.db"
    through_32 = _copy_migrations_through(tmp_path, 32)
    assert (await run_migrations(db_path, migrations_dir=through_32)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executemany(
            """INSERT INTO meetings
               (id, title, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, ?, 'in_meeting', ?, ?, ?, ?)""",
            (
                (
                    "m-old",
                    "old",
                    "2026-07-12T01:00:00+00:00",
                    "tenant-a",
                    "device-a",
                    "owner-a",
                ),
                (
                    "m-alpha",
                    "tie loser",
                    "2026-07-12T02:00:00+00:00",
                    "tenant-a",
                    "device-a",
                    "owner-a",
                ),
                (
                    "m-zeta",
                    "tie winner",
                    "2026-07-12T02:00:00+00:00",
                    "tenant-a",
                    "device-b",
                    "owner-a",
                ),
                (
                    "m-other-owner",
                    "other",
                    "2026-07-12T00:00:00+00:00",
                    "tenant-a",
                    "device-c",
                    "owner-b",
                ),
            ),
        )
        await conn.commit()

    upgraded = await run_migrations(db_path)

    assert upgraded.errors == []
    assert upgraded.applied == [33, 34, 35, 36, 37, 38]
    async with aiosqlite.connect(str(db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT id, state, ended_at FROM meetings
                   WHERE tenant_id = 'tenant-a' AND owner_id = 'owner-a'
                   ORDER BY id"""
            )
        ).fetchall()
        audit = await (
            await conn.execute(
                """SELECT meeting_id, authoritative_meeting_id, prior_state, next_state, reason
                   FROM meeting_state_migration_audit ORDER BY meeting_id"""
            )
        ).fetchall()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                """INSERT INTO meetings
                   (id, state, started_at, tenant_id, device_id, owner_id)
                   VALUES ('m-rejected', 'in_meeting', CURRENT_TIMESTAMP,
                           'tenant-a', 'device-a', 'owner-a')"""
            )

    assert [(row[0], row[1]) for row in rows] == [
        ("m-alpha", "ended"),
        ("m-old", "ended"),
        ("m-zeta", "in_meeting"),
    ]
    assert all(row[2] is not None for row in rows if row[1] == "ended")
    assert [tuple(row) for row in audit] == [
        ("m-alpha", "m-zeta", "in_meeting", "ended", "duplicate_active_meeting"),
        ("m-old", "m-zeta", "in_meeting", "ended", "duplicate_active_meeting"),
    ]
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["content", "name", "missing"])
async def test_applied_migration_content_or_name_tampering_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    db_path = tmp_path / f"tamper-{mutation}.db"
    assert (await run_migrations(db_path)).errors == []
    schema_before = await _schema_signature(db_path)
    copied = _copy_migrations_through(tmp_path, _version(_migration_files()[-1]))
    original = next(copied.glob("001_*.sql"))
    if mutation == "content":
        original.write_bytes(original.read_bytes() + b"\n-- post-apply tampering\n")
    elif mutation == "name":
        original.rename(copied / "001_renamed_after_apply.sql")
    else:
        original.unlink()

    rejected = await run_migrations(db_path, migrations_dir=copied)

    assert rejected.applied == []
    assert len(rejected.errors) == 1
    assert rejected.errors[0].startswith("migration integrity: v1")
    expected_field = {
        "content": "sha256=",
        "name": "name=",
        "missing": "registered migration file is missing",
    }[mutation]
    assert expected_field in rejected.errors[0]
    assert await _schema_signature(db_path) == schema_before
    assert await _migration_registrations(db_path) == _expected_registrations()
    assert await _integrity(db_path) == ("ok", [])
    assert (await run_migrations(db_path)).errors == []


@pytest.mark.unit
async def test_rebuild_refuses_to_silently_change_unknown_index_trigger_or_view(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "rebuild-guard.db"
    through_18 = _copy_migrations_through(tmp_path, 18)
    assert (await run_migrations(db_path, migrations_dir=through_18)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(
            """
            CREATE TABLE custom_meeting_audit (meeting_id TEXT NOT NULL);
            CREATE INDEX custom_meeting_title_idx ON meetings(title);
            CREATE TRIGGER custom_meeting_title_audit
            AFTER UPDATE OF title ON meetings
            BEGIN
                INSERT INTO custom_meeting_audit(meeting_id) VALUES (NEW.id);
            END;
            CREATE VIEW custom_meeting_titles AS
            SELECT id, title FROM meetings;
            """
        )
        await conn.commit()

    blocked = await run_migrations(db_path)

    assert blocked.current_version == 18
    assert len(blocked.errors) == 1
    assert "table rebuild would discard or alter schema objects" in blocked.errors[0]
    assert "view:custom_meeting_titles" in blocked.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        version_19 = await (
            await conn.execute("SELECT 1 FROM schema_version WHERE version = 19")
        ).fetchone()
        preserved = await (
            await conn.execute(
                """SELECT type, name FROM sqlite_schema
                   WHERE name IN (
                       'custom_meeting_title_idx', 'custom_meeting_title_audit',
                       'custom_meeting_titles'
                   ) ORDER BY type, name"""
            )
        ).fetchall()
        view_rows = await (
            await conn.execute("SELECT id, title FROM custom_meeting_titles")
        ).fetchall()
        legacy_table = await (
            await conn.execute(
                """SELECT 1 FROM sqlite_schema
                   WHERE type = 'table' AND name = 'meetings_legacy_global_key'"""
            )
        ).fetchone()
    assert version_19 is None
    assert [tuple(row) for row in preserved] == [
        ("index", "custom_meeting_title_idx"),
        ("trigger", "custom_meeting_title_audit"),
        ("view", "custom_meeting_titles"),
    ]
    assert view_rows == []
    assert legacy_table is None
    assert await _integrity(db_path) == ("ok", [])

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("DROP TRIGGER custom_meeting_title_audit")
        await conn.execute("DROP INDEX custom_meeting_title_idx")
        await conn.execute("DROP VIEW custom_meeting_titles")
        await conn.commit()
    resumed = await run_migrations(db_path)
    assert resumed.errors == []
    assert resumed.current_version == _version(_migration_files()[-1])
    assert await _integrity(db_path) == ("ok", [])


def _write_migrations(directory: Path, migrations: Iterable[tuple[str, str]]) -> None:
    directory.mkdir()
    for filename, sql in migrations:
        (directory / filename).write_text(sql, encoding="utf-8")


async def _prepare_custom_rebuild_guard(
    tmp_path: Path,
    name: str,
    *,
    view_sql: str = "SELECT id, label FROM items",
) -> tuple[Path, Path]:
    db_path = tmp_path / f"{name}.db"
    migrations = tmp_path / f"{name}-migrations"
    _write_migrations(
        migrations,
        (("001_base.sql", "CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT);"),),
    )
    assert (await run_migrations(db_path, migrations_dir=migrations)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("INSERT INTO items(id, label) VALUES (1, 'kept')")
        await conn.execute(f"CREATE VIEW custom_items AS {view_sql}")
        await conn.commit()
    return db_path, migrations


@pytest.mark.unit
async def test_rebuild_comment_pseudo_drop_does_not_allow_unknown_view(
    tmp_path: Path,
) -> None:
    db_path, migrations = await _prepare_custom_rebuild_guard(tmp_path, "comment-pseudo-drop")
    (migrations / "002_rebuild.sql").write_text(
        """-- DROP VIEW main.custom_items; this is documentation, not DDL
           ALTER TABLE items RENAME TO items_legacy;
           CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT, tag TEXT);
           INSERT INTO items(id, label) SELECT id, label FROM items_legacy;
           DROP TABLE items_legacy;""",
        encoding="utf-8",
    )

    blocked = await run_migrations(db_path, migrations_dir=migrations)

    assert blocked.current_version == 1
    assert len(blocked.errors) == 1
    assert "view:custom_items" in blocked.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        view = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='view' AND name='custom_items'"
            )
        ).fetchone()
        columns = await (await conn.execute("PRAGMA table_info(items)")).fetchall()
    assert view is not None
    assert [str(row[1]) for row in columns] == ["id", "label"]


@pytest.mark.unit
async def test_schema_qualified_rebuild_protects_unknown_view(
    tmp_path: Path,
) -> None:
    db_path, migrations = await _prepare_custom_rebuild_guard(
        tmp_path,
        "qualified-rebuild",
        view_sql='SELECT id, label FROM "main"."items"',
    )
    (migrations / "002_rebuild.sql").write_text(
        """ALTER TABLE "main"."items" RENAME TO items_legacy;
           CREATE TABLE "main"."items" (id INTEGER PRIMARY KEY, label TEXT, tag TEXT);
           INSERT INTO "main"."items"(id, label)
           SELECT id, label FROM "main"."items_legacy";
           DROP TABLE [main].[items_legacy];""",
        encoding="utf-8",
    )

    blocked = await run_migrations(db_path, migrations_dir=migrations)

    assert blocked.current_version == 1
    assert len(blocked.errors) == 1
    assert "view:custom_items" in blocked.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        row = await (await conn.execute("SELECT id, label FROM custom_items")).fetchone()
    assert row == (1, "kept")


@pytest.mark.unit
async def test_schema_qualified_drop_view_allows_rebuild(
    tmp_path: Path,
) -> None:
    db_path, migrations = await _prepare_custom_rebuild_guard(
        tmp_path,
        "qualified-drop-view",
        view_sql="SELECT id, label FROM main.items",
    )
    (migrations / "002_rebuild.sql").write_text(
        """DROP VIEW IF EXISTS main."custom_items";
           ALTER TABLE main.items RENAME TO items_legacy;
           CREATE TABLE main.items (id INTEGER PRIMARY KEY, label TEXT, tag TEXT);
           INSERT INTO main.items(id, label) SELECT id, label FROM main.items_legacy;
           DROP TABLE `main`.`items_legacy`;""",
        encoding="utf-8",
    )

    applied = await run_migrations(db_path, migrations_dir=migrations)

    assert applied.errors == []
    assert applied.current_version == 2
    async with aiosqlite.connect(str(db_path)) as conn:
        view = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='view' AND name='custom_items'"
            )
        ).fetchone()
        columns = await (await conn.execute("PRAGMA table_info(items)")).fetchall()
        row = await (await conn.execute("SELECT id, label, tag FROM items")).fetchone()
    assert view is None
    assert [str(item[1]) for item in columns] == ["id", "label", "tag"]
    assert row == (1, "kept", None)
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
async def test_failed_migration_rolls_back_and_clean_restart_applies_trigger_body(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restart.db"
    migrations = tmp_path / "restart-migrations"
    _write_migrations(
        migrations,
        (
            ("001_base.sql", "CREATE TABLE base (id INTEGER PRIMARY KEY);"),
            (
                "002_fails_midway.sql",
                """CREATE TABLE half_applied (id INTEGER PRIMARY KEY);
                   INSERT INTO table_that_does_not_exist(id) VALUES (1);""",
            ),
            ("003_after.sql", "CREATE TABLE after_restart (id INTEGER PRIMARY KEY);"),
        ),
    )

    failed = await run_migrations(db_path, migrations_dir=migrations)

    assert failed.applied == [1]
    assert failed.current_version == 1
    assert len(failed.errors) == 1
    async with aiosqlite.connect(str(db_path)) as conn:
        half_applied = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='half_applied'"
            )
        ).fetchone()
        false_success = await (
            await conn.execute("SELECT 1 FROM schema_version WHERE version = 2")
        ).fetchone()
    assert half_applied is None
    assert false_success is None

    (migrations / "002_fails_midway.sql").write_text(
        """CREATE TABLE recovered (id INTEGER PRIMARY KEY, touched INTEGER DEFAULT 0);
           CREATE TABLE recovered_audit (id INTEGER NOT NULL);
           CREATE TRIGGER recovered_insert_audit
           AFTER INSERT ON recovered
           BEGIN
               INSERT INTO recovered_audit(id) VALUES (NEW.id);
               UPDATE recovered SET touched = 1 WHERE id = NEW.id;
           END;""",
        encoding="utf-8",
    )
    resumed = await run_migrations(db_path, migrations_dir=migrations)

    assert resumed.errors == []
    assert resumed.applied == [2, 3]
    assert resumed.current_version == 3
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("INSERT INTO recovered(id) VALUES (7)")
        await conn.commit()
        recovered = await (await conn.execute("SELECT id, touched FROM recovered")).fetchone()
        audited = await (await conn.execute("SELECT id FROM recovered_audit")).fetchone()
        versions = await (
            await conn.execute("SELECT version FROM schema_version ORDER BY version")
        ).fetchall()
    assert recovered == (7, 1)
    assert audited == (7,)
    assert [int(row[0]) for row in versions] == [1, 2, 3]
    assert await _integrity(db_path) == ("ok", [])


@pytest.mark.unit
@pytest.mark.parametrize(
    "transaction_statement",
    [
        "BEGIN IMMEDIATE;",
        "-- never escape the runner\nCOMMIT;",
        "\ufeffCOMMIT;",
        "END TRANSACTION;",
        "ROLLBACK;",
        "SAVEPOINT nested;",
        "RELEASE nested;",
    ],
)
async def test_migration_rejects_top_level_transaction_control_before_any_ddl(
    tmp_path: Path,
    transaction_statement: str,
) -> None:
    db_path = tmp_path / "transaction-escape.db"
    migrations = tmp_path / "transaction-escape-migrations"
    _write_migrations(
        migrations,
        (
            (
                "001_transaction_escape.sql",
                "CREATE TABLE escaped_commit (id INTEGER PRIMARY KEY);\n"
                f"{transaction_statement}\n"
                "INSERT INTO missing_table(id) VALUES (1);",
            ),
        ),
    )

    rejected = await run_migrations(db_path, migrations_dir=migrations)

    assert rejected.applied == []
    assert rejected.current_version == 0
    assert len(rejected.errors) == 1
    assert "migration SQL must not control transactions" in rejected.errors[0]
    async with aiosqlite.connect(str(db_path)) as conn:
        escaped = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='escaped_commit'"
            )
        ).fetchone()
        registered = await (
            await conn.execute("SELECT 1 FROM schema_version WHERE version = 1")
        ).fetchone()
    assert escaped is None
    assert registered is None


@pytest.mark.unit
async def test_utf8_bom_at_start_of_migration_file_remains_supported(tmp_path: Path) -> None:
    db_path = tmp_path / "leading-bom.db"
    migrations = tmp_path / "leading-bom-migrations"
    _write_migrations(
        migrations,
        (("001_leading_bom.sql", "\ufeffCREATE TABLE bom_ok (id INTEGER PRIMARY KEY);"),),
    )

    applied = await run_migrations(db_path, migrations_dir=migrations)

    assert applied.errors == []
    assert applied.applied == [1]
    async with aiosqlite.connect(str(db_path)) as conn:
        table = await (
            await conn.execute("SELECT 1 FROM sqlite_schema WHERE name='bom_ok'")
        ).fetchone()
    assert table is not None


def _process_migrate(
    db_path: str,
    ready_queue: Queue[Any],
    start_event: Event,
    result_queue: Queue[Any],
) -> None:
    ready_queue.put(True)
    start_event.wait(timeout=30)
    try:
        result = asyncio.run(run_migrations(Path(db_path)))
        result_queue.put((result.errors, result.current_version, result.applied, result.skipped))
    except BaseException as exc:  # pragma: no cover - surfaced in parent assertion
        result_queue.put(([repr(exc)], -1, [], []))


@pytest.mark.unit
def test_two_processes_initialize_same_database_without_duplicate_or_lock_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "concurrent.db"
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_process_migrate,
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

    latest = _version(_migration_files()[-1])
    assert all(errors == [] and current == latest for errors, current, _, _ in results)
    assert sum(len(applied) for _, _, applied, _ in results) == len(_migration_files())
    assert asyncio.run(_integrity(db_path)) == ("ok", [])

    async def applied_versions() -> list[int]:
        async with aiosqlite.connect(str(db_path)) as conn:
            rows = await (
                await conn.execute("SELECT version FROM schema_version ORDER BY version")
            ).fetchall()
        return [int(row[0]) for row in rows]

    assert asyncio.run(applied_versions()) == [_version(path) for path in _migration_files()]
