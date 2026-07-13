from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.artifacts.staging import workflow_build_dir
from app.config import Settings
from app.security import (
    DeviceIdentityAlreadyClaimedError,
    ExpiredDeviceCredentialError,
    IdentityAlreadyEnrolledError,
    InvalidDeviceCredentialError,
    RevokedDeviceCredentialError,
    RevokedSessionError,
    SessionStore,
)
from app.security.context import bind_principal, reset_principal
from app.security.models import Principal
from app.security.scope import physical_resource_id, scoped_directory


async def _store(
    tmp_path: Path,
    *,
    credential_ttl: timedelta = timedelta(days=180),
) -> tuple[SessionStore, Path, list[datetime]]:
    db_path = tmp_path / "identity.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    clock = [datetime(2026, 7, 11, 12, 0, tzinfo=UTC)]
    return (
        SessionStore(
            db_path,
            credential_ttl=credential_ttl,
            now=lambda: clock[0],
        ),
        db_path,
        clock,
    )


async def _run_through(db_path: Path, root: Path, version: int) -> None:
    migration_dir = root / f"migrations-through-{version}"
    migration_dir.mkdir()
    for source in sorted(_DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if int(source.name.split("_", 1)[0]) <= version:
            (migration_dir / source.name).write_bytes(source.read_bytes())
    result = await run_migrations(db_path, migrations_dir=migration_dir)
    assert result.errors == []


@pytest.mark.unit
async def test_enrollment_persists_hashes_but_never_plaintext_bearers(tmp_path: Path) -> None:
    store, db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device(display_name="MacBook")

    async with aiosqlite.connect(str(db_path)) as conn:
        session_hash = (
            await (
                await conn.execute(
                    "SELECT token_hash FROM principal_sessions WHERE session_id = ?",
                    (identity.session.principal.session_id,),
                )
            ).fetchone()
        )[0]
        credential_hash = (
            await (
                await conn.execute(
                    "SELECT credential_hash FROM device_credentials WHERE credential_id = ?",
                    (identity.credential_id,),
                )
            ).fetchone()
        )[0]

    assert session_hash == hashlib.sha256(identity.session.token.encode()).hexdigest()
    assert credential_hash == hashlib.sha256(identity.device_credential.encode()).hexdigest()
    assert identity.session.token not in session_hash
    assert identity.device_credential not in credential_hash


@pytest.mark.unit
async def test_renew_preserves_scope_and_revokes_the_previous_access_token(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()

    renewed = await store.renew_public_session(identity.device_credential)

    assert renewed.token != identity.session.token
    assert renewed.principal.tenant_id == identity.session.principal.tenant_id
    assert renewed.principal.owner_id == identity.session.principal.owner_id
    assert renewed.principal.device_id == identity.session.principal.device_id
    assert renewed.principal.family_id == identity.session.principal.family_id
    with pytest.raises(RevokedSessionError):
        await store.validate_public_token(identity.session.token)
    assert await store.validate_public_token(renewed.token) == renewed.principal


@pytest.mark.unit
async def test_forged_device_credential_cannot_renew_identity(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()
    forged = identity.device_credential[:-1] + (
        "A" if identity.device_credential[-1] != "A" else "B"
    )

    with pytest.raises(InvalidDeviceCredentialError):
        await store.renew_public_session(forged)


@pytest.mark.unit
async def test_expired_device_credential_cannot_renew_identity(tmp_path: Path) -> None:
    store, _db_path, clock = await _store(
        tmp_path,
        credential_ttl=timedelta(seconds=5),
    )
    identity = await store.enroll_public_device()
    clock[0] += timedelta(seconds=6)

    with pytest.raises(ExpiredDeviceCredentialError):
        await store.renew_public_session(identity.device_credential)


@pytest.mark.unit
async def test_rotated_device_credential_invalidates_the_old_secret(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()
    new_secret = "new-device-secret-" + "n" * 40
    new_id, _expires_at = await store.rotate_device_credential(
        identity.session.principal,
        current_credential=identity.device_credential,
        new_credential=new_secret,
    )

    assert new_id != identity.credential_id
    with pytest.raises(RevokedDeviceCredentialError):
        await store.renew_public_session(identity.device_credential)
    renewed = await store.renew_public_session(new_secret)
    assert renewed.principal.user_id == identity.session.principal.user_id


@pytest.mark.unit
async def test_other_family_credential_cannot_rotate_or_revoke_original_identity(
    tmp_path: Path,
) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity_a = await store.enroll_public_device()
    identity_b = await store.enroll_public_device()

    with pytest.raises(InvalidDeviceCredentialError):
        await store.rotate_device_credential(
            identity_a.session.principal,
            current_credential=identity_b.device_credential,
            new_credential="unauthorized-rotation-" + "x" * 40,
        )
    with pytest.raises(InvalidDeviceCredentialError):
        await store.revoke_device(
            identity_a.session.principal,
            current_credential=identity_b.device_credential,
        )
    renewed_a = await store.renew_public_session(identity_a.device_credential)
    renewed_b = await store.renew_public_session(identity_b.device_credential)
    assert renewed_a.principal.owner_id == identity_a.session.principal.owner_id
    assert renewed_b.principal.owner_id == identity_b.session.principal.owner_id


@pytest.mark.unit
async def test_same_user_devices_share_resources_and_one_device_revoke_keeps_data(
    tmp_path: Path,
) -> None:
    store, db_path, clock = await _store(tmp_path)
    first = await store.enroll_public_device(
        enrollment_id="primary-enrollment-" + "p" * 40,
        device_secret="primary-device-secret-" + "p" * 40,
    )
    second_secret = "secondary-device-secret-" + "s" * 40
    second = await store.enroll_additional_device(
        first.session.principal,
        current_credential=first.device_credential,
        enrollment_id="secondary-enrollment-" + "s" * 40,
        device_secret=second_secret,
        peer_key="peer-secondary",
    )
    assert second.session.principal.owner_id == first.session.principal.owner_id
    assert second.session.principal.device_id != first.session.principal.device_id

    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        token = bind_principal(second.session.principal)
        try:
            await repo.create_meeting("shared-by-user", started_at=clock[0])
        finally:
            reset_principal(token)

        token = bind_principal(first.session.principal)
        try:
            assert await repo.get_meeting("shared-by-user") is not None
        finally:
            reset_principal(token)

        assert (
            await store.revoke_device(
                second.session.principal,
                current_credential=second_secret,
            )
            is True
        )
        with pytest.raises(RevokedSessionError):
            await store.validate_public_token(second.session.token)
        with pytest.raises(RevokedDeviceCredentialError):
            await store.renew_public_session(second_secret)

        token = bind_principal(first.session.principal)
        try:
            assert await repo.get_meeting("shared-by-user") is not None
        finally:
            reset_principal(token)
    finally:
        await repo.aclose()


@pytest.mark.unit
def test_physical_paths_are_user_scoped_and_not_device_scoped(tmp_path: Path) -> None:
    settings = Settings(skill_executor_build_dir=tmp_path / "skill-build")
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_a2 = Principal("tenant-a", "device-a2", "owner-a", "session-a2", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token = bind_principal(principal_a)
    try:
        build_a = workflow_build_dir(settings, "same-run", "pdf")
        transcript_a = scoped_directory(tmp_path / "meetings") / (
            physical_resource_id("same-meeting", kind="meeting") + ".json"
        )
    finally:
        reset_principal(token)
    token = bind_principal(principal_a2)
    try:
        build_a2 = workflow_build_dir(settings, "same-run", "pdf")
    finally:
        reset_principal(token)
    token = bind_principal(principal_b)
    try:
        build_b = workflow_build_dir(settings, "same-run", "pdf")
        transcript_b = scoped_directory(tmp_path / "meetings") / (
            physical_resource_id("same-meeting", kind="meeting") + ".json"
        )
    finally:
        reset_principal(token)

    assert build_a == build_a2
    assert build_a != build_b
    assert transcript_a != transcript_b
    transcript_a.parent.mkdir(parents=True)
    transcript_b.parent.mkdir(parents=True)
    transcript_a.write_text("owner-a", encoding="utf-8")
    transcript_b.write_text("owner-b", encoding="utf-8")
    assert transcript_a.read_text(encoding="utf-8") == "owner-a"
    assert transcript_b.read_text(encoding="utf-8") == "owner-b"


@pytest.mark.unit
async def test_family_revoke_kills_access_and_device_credential(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()

    assert await store.revoke_session_family(identity.session.principal) is True
    assert await store.revoke_session_family(identity.session.principal) is False
    with pytest.raises(RevokedSessionError):
        await store.validate_public_token(identity.session.token)
    with pytest.raises(RevokedDeviceCredentialError):
        await store.renew_public_session(identity.device_credential)


@pytest.mark.unit
async def test_device_revoke_kills_every_future_renewal(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()

    assert (
        await store.revoke_device(
            identity.session.principal,
            current_credential=identity.device_credential,
        )
        is True
    )
    with pytest.raises(RevokedDeviceCredentialError):
        await store.renew_public_session(identity.device_credential)


@pytest.mark.unit
async def test_enroll_retry_with_same_pair_preserves_identity_and_rotates_session(
    tmp_path: Path,
) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    enrollment_id = "enrollment-retry-" + "e" * 40
    device_secret = "device-retry-" + "s" * 40

    first = await store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
    )
    retry = await store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret=device_secret,
        peer_key="peer-a",
    )

    assert retry.session.token != first.session.token
    assert retry.session.principal.tenant_id == first.session.principal.tenant_id
    assert retry.session.principal.owner_id == first.session.principal.owner_id
    assert retry.session.principal.device_id == first.session.principal.device_id
    with pytest.raises(RevokedSessionError):
        await store.validate_public_token(first.session.token)


@pytest.mark.unit
async def test_same_enrollment_id_with_another_secret_is_rejected(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)
    enrollment_id = "enrollment-conflict-" + "e" * 40
    await store.enroll_public_device(
        enrollment_id=enrollment_id,
        device_secret="device-secret-a-" + "a" * 40,
    )

    with pytest.raises(IdentityAlreadyEnrolledError):
        await store.enroll_public_device(
            enrollment_id=enrollment_id,
            device_secret="device-secret-b-" + "b" * 40,
        )


@pytest.mark.unit
async def test_concurrent_double_enroll_creates_exactly_one_identity(tmp_path: Path) -> None:
    store, db_path, _clock = await _store(tmp_path)
    enrollment_id = "enrollment-concurrent-" + "e" * 40
    device_secret = "device-concurrent-" + "s" * 40

    first, second = await asyncio.gather(
        store.enroll_public_device(
            enrollment_id=enrollment_id,
            device_secret=device_secret,
            peer_key="peer-a",
        ),
        store.enroll_public_device(
            enrollment_id=enrollment_id,
            device_secret=device_secret,
            peer_key="peer-a",
        ),
    )

    assert first.session.principal.tenant_id == second.session.principal.tenant_id
    assert first.session.principal.owner_id == second.session.principal.owner_id
    async with aiosqlite.connect(str(db_path)) as conn:
        assert await (await conn.execute("SELECT COUNT(*) FROM public_enrollments")).fetchone() == (
            1,
        )
        assert await (
            await conn.execute("SELECT COUNT(*) FROM tenants WHERE tenant_id != 'legacy-local'")
        ).fetchone() == (1,)


@pytest.mark.unit
async def test_concurrent_renewals_leave_exactly_one_active_session(tmp_path: Path) -> None:
    store, db_path, _clock = await _store(tmp_path)
    identity = await store.enroll_public_device()

    first, second = await asyncio.gather(
        store.renew_public_session(identity.device_credential),
        store.renew_public_session(identity.device_credential),
    )

    async with aiosqlite.connect(str(db_path)) as conn:
        active = await (
            await conn.execute(
                "SELECT COUNT(*) FROM principal_sessions "
                "WHERE family_id = ? AND revoked_at IS NULL",
                (identity.session.principal.family_id,),
            )
        ).fetchone()
    assert active == (1,)
    validation_results = await asyncio.gather(
        store.validate_public_token(first.token),
        store.validate_public_token(second.token),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in validation_results) == 1


@pytest.mark.unit
async def test_access_session_ttl_cannot_exceed_one_hour(tmp_path: Path) -> None:
    store, _db_path, _clock = await _store(tmp_path)

    with pytest.raises(ValueError, match="one hour"):
        await store.enroll_public_device(ttl=timedelta(hours=2))


@pytest.mark.unit
async def test_legacy_session_migration_can_be_claimed_only_once(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-session.db"
    await _run_through(db_path, tmp_path, 17)
    legacy_token = "legacy-public-bearer"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            """INSERT INTO principal_sessions
               (session_id, token_hash, tenant_id, device_id, owner_id, mode,
                issued_at, expires_at, revoked_at)
               VALUES ('legacy-session', ?, 'tenant-old', 'device-old', 'owner-old',
                       'public', '2026-07-11T11:00:00+00:00',
                       '2026-07-11T13:00:00+00:00', NULL)""",
            (hashlib.sha256(legacy_token.encode()).hexdigest(),),
        )
        await conn.commit()
    migrated = await run_migrations(db_path)
    assert migrated.errors == []
    store = SessionStore(
        db_path,
        now=lambda: datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )
    legacy_principal = await store.validate_public_token(legacy_token)

    claimed = await store.claim_legacy_identity(legacy_principal)

    assert claimed.session.principal.tenant_id == "tenant-old"
    assert claimed.session.principal.owner_id == "owner-old"
    assert claimed.session.principal.device_id == "device-old"
    with pytest.raises(DeviceIdentityAlreadyClaimedError):
        await store.claim_legacy_identity(claimed.session.principal)


@pytest.mark.unit
async def test_composite_key_migration_preserves_rows_and_allows_same_ids_per_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-resources.db"
    await _run_through(db_path, tmp_path, 18)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('same-id', 'ended', '2026-01-01', 'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO meeting_segments
               (meeting_id, text, start_ms, end_ms, captured_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'preserved', 0, 1, '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'test', 'test', 'succeeded', 'preserved',
                       '2026-01-01', '2026-01-01', 'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'txt', '/tmp/a', 'text/plain', '2026-01-01',
                       '2026-01-01', 'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.commit()

    migrated = await run_migrations(db_path)
    assert migrated.errors == []
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('same-id', 'ended', '2026-01-02', 'tenant-b', 'device-b', 'owner-b')"""
        )
        conn.execute(
            """INSERT INTO meeting_segments
               (meeting_id, text, start_ms, end_ms, captured_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'isolated', 0, 1, '2026-01-02',
                       'tenant-b', 'device-b', 'owner-b')"""
        )
        conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'test', 'test', 'succeeded', 'isolated',
                       '2026-01-02', '2026-01-02', 'tenant-b', 'device-b', 'owner-b')"""
        )
        conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('same-id', 'txt', '/tmp/b', 'text/plain', '2026-01-02',
                       '2026-01-02', 'tenant-b', 'device-b', 'owner-b')"""
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM meetings WHERE id='same-id'").fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE run_id='same-id'"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_id='same-id'"
        ).fetchone() == (2,)


@pytest.mark.unit
async def test_composite_child_foreign_key_rejects_cross_scope_parent(tmp_path: Path) -> None:
    _store_instance, db_path, _clock = await _store(tmp_path)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('meeting-x', 'ended', '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO meeting_segments
                   (meeting_id, text, start_ms, end_ms, captured_at,
                    tenant_id, device_id, owner_id)
                   VALUES ('meeting-x', 'forbidden', 0, 1, '2026-01-01',
                           'tenant-b', 'device-b', 'owner-b')"""
            )


@pytest.mark.unit
async def test_all_security_critical_composite_foreign_keys_reject_cross_scope(
    tmp_path: Path,
) -> None:
    _store_instance, db_path, _clock = await _store(tmp_path)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        for tenant, owner, device in (
            ("tenant-a", "owner-a", "device-a"),
            ("tenant-b", "owner-b", "device-b"),
        ):
            await conn.execute(
                "INSERT INTO tenants VALUES (?, 'active', '2026-01-01', '2026-01-01')",
                (tenant,),
            )
            await conn.execute(
                "INSERT INTO users VALUES (?, ?, 'active', '2026-01-01', '2026-01-01')",
                (tenant, owner),
            )
            await conn.execute(
                """INSERT INTO devices
                   (tenant_id, user_id, device_id, created_at, last_seen_at)
                   VALUES (?, ?, ?, '2026-01-01', '2026-01-01')""",
                (tenant, owner, device),
            )
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('meeting-shared', 'ended', '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('parent-run', 'test', 'test', 'succeeded', 'parent',
                       '2026-01-01', '2026-01-01', 'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('artifact-b', 'txt', '/tmp/b', 'text/plain',
                       '2026-01-01', '2026-01-01', 'tenant-b', 'device-b', 'owner-b')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO workflow_runs
                   (run_id, kind, source, state, intent_text, meeting_id,
                    created_at, updated_at, tenant_id, device_id, owner_id)
                   VALUES ('cross-meeting', 'test', 'test', 'failed', 'cross',
                           'meeting-shared', '2026-01-01', '2026-01-01',
                           'tenant-b', 'device-b', 'owner-b')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO workflow_runs
                   (run_id, kind, source, state, intent_text, parent_run_id,
                    created_at, updated_at, tenant_id, device_id, owner_id)
                   VALUES ('cross-parent', 'test', 'test', 'failed', 'cross',
                           'parent-run', '2026-01-01', '2026-01-01',
                           'tenant-b', 'device-b', 'owner-b')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO artifact_links
                   (link_id, artifact_id, source, meeting_id, created_at,
                    tenant_id, device_id, owner_id)
                   VALUES ('cross-link', 'artifact-b', 'test', 'meeting-shared',
                           '2026-01-01', 'tenant-b', 'device-b', 'owner-b')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO agent_runner_grants
                   (grant_id, device_id, runner, permission_profile,
                    permission_mode, granted_at, tenant_id, owner_id)
                   VALUES ('cross-grant', 'device-b', 'claude_code', 'full',
                           'bypass', '2026-01-01', 'tenant-a', 'owner-a')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                """INSERT INTO principal_quota_ledger
                   (tenant_id, owner_id, metric, window_key, used)
                   VALUES ('tenant-a', 'owner-b', 'requests', 'minute:test', 1)"""
            )


@pytest.mark.unit
async def test_019_quarantines_legacy_orphans_before_enforcing_new_foreign_keys(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orphaned-legacy.db"
    await _run_through(db_path, tmp_path, 18)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            """INSERT INTO meeting_segments
               (meeting_id, text, start_ms, end_ms, captured_at,
                tenant_id, device_id, owner_id)
               VALUES ('missing-meeting', 'orphan', 0, 1, '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO artifact_links
               (link_id, artifact_id, source, meeting_id, created_at,
                tenant_id, device_id, owner_id)
               VALUES ('orphan-meeting-link', 'orphan-artifact', 'test',
                       'missing-meeting', '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, meeting_id, parent_run_id,
                created_at, updated_at, tenant_id, device_id, owner_id)
               VALUES ('orphan-run', 'test', 'test', 'failed', 'orphan',
                       'missing-meeting', 'missing-parent', '2026-01-01', '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, run_id,
                created_at, updated_at, tenant_id, device_id, owner_id)
               VALUES ('orphan-artifact', 'txt', '/tmp/orphan', 'text/plain',
                       'missing-run', '2026-01-01', '2026-01-01',
                       'tenant-a', 'device-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO agent_tasks
               (task_id, device_id, title, intent_text, state, submitted_at,
                workflow_run_id, grant_id, tenant_id, owner_id)
               VALUES ('orphan-task', 'device-a', 'orphan', 'orphan', 'failed',
                       '2026-01-01', 'missing-run', 'missing-grant',
                       'tenant-a', 'owner-a')"""
        )
        await conn.execute(
            """INSERT INTO agent_runner_grants
               (grant_id, device_id, runner, permission_profile, permission_mode,
                granted_at, tenant_id, owner_id)
               VALUES ('invalid-device-grant', 'missing-device', 'claude_code',
                       'full', 'bypass', '2026-01-01',
                       'legacy-local', 'legacy-local')"""
        )
        await conn.execute(
            """INSERT INTO agent_runner_grants
               (grant_id, device_id, runner, permission_profile, permission_mode,
                granted_at, tenant_id, owner_id)
               VALUES ('valid-device-grant', 'legacy-local', 'claude_code',
                       'full', 'bypass', '2026-01-02',
                       'legacy-local', 'legacy-local')"""
        )
        await conn.execute(
            """INSERT INTO agent_tasks
               (task_id, device_id, title, intent_text, state, submitted_at,
                grant_id, tenant_id, owner_id)
               VALUES ('invalid-grant-task', 'legacy-local', 'invalid grant',
                       'invalid grant', 'failed', '2026-01-01',
                       'invalid-device-grant', 'legacy-local', 'legacy-local')"""
        )
        await conn.commit()

    migrated = await run_migrations(db_path)

    assert migrated.errors == []
    assert migrated.orphan_quarantined >= 7
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM meeting_segments WHERE text = 'orphan'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT meeting_id, parent_run_id FROM workflow_runs WHERE run_id='orphan-run'"
        ).fetchone() == (None, None)
        assert conn.execute(
            "SELECT run_id FROM artifacts WHERE artifact_id='orphan-artifact'"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT meeting_id FROM artifact_links WHERE link_id='orphan-meeting-link'"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT workflow_run_id, grant_id FROM agent_tasks WHERE task_id='orphan-task'"
        ).fetchone() == (None, None)
        assert conn.execute(
            "SELECT grant_id FROM agent_tasks WHERE task_id='invalid-grant-task'"
        ).fetchone() == (None,)
        assert (
            conn.execute(
                "SELECT 1 FROM agent_runner_grants WHERE grant_id='invalid-device-grant'"
            ).fetchone()
            is None
        )
        assert conn.execute(
            "SELECT device_id FROM agent_runner_grants WHERE grant_id='valid-device-grant'"
        ).fetchone() == ("legacy-local",)
        relations = {
            row[0]
            for row in conn.execute(
                "SELECT relation_name FROM migration_orphan_quarantine"
            ).fetchall()
        }
        assert {"meeting_id", "parent_run_id", "run_id", "workflow_run_id", "grant_id"} <= relations
