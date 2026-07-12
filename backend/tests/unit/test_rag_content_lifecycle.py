from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import bind_principal, reset_principal
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.models import Principal
from app.upload import ownership as rag_ownership
from app.upload.ownership import (
    RagContentOwnershipError,
    bind_rag_content_doc,
    claim_rag_content,
    get_rag_content_claim,
    open_rag_parser_input,
    rag_blob_path,
    rag_staging_root,
    reconcile_rag_content_storage,
    release_rag_content_claim,
    stage_rag_content_blob,
)
from app.workflows.service import WorkflowService

from tests.unit._principal_identity import seed_principal_identity


async def _settings(tmp_path: Path, *, storage_limit: int = 1024) -> Settings:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        quota_storage_bytes=storage_limit,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    return settings


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_claim_is_single_charge_and_enforces_actual_acl_quota(
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path, storage_limit=20)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    second = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")
    await seed_principal_identity(settings.db_path, principal, second)
    digest = _digest(b"shared")

    claims = await asyncio.gather(
        *(
            claim_rag_content(
                settings.db_path,
                principal,
                content_hash=digest,
                size_bytes=6,
                workflow_run_id=f"run-{index}",
                file_suffix=".md",
                storage_limit=settings.quota_storage_bytes,
            )
            for index in range(16)
        )
    )

    assert sum(claim.created for claim in claims) == 1
    assert len({claim.workflow_run_id for claim in claims}) == 1
    governor = PrincipalGovernor(settings)
    assert await governor.usage(principal, "storage_bytes") == 6
    with pytest.raises(QuotaExceeded, match="storage_bytes"):
        await claim_rag_content(
            settings.db_path,
            principal,
            content_hash=_digest(b"second-content"),
            size_bytes=15,
            workflow_run_id="run-over-quota",
            file_suffix=".md",
            storage_limit=settings.quota_storage_bytes,
        )
    with pytest.raises(RagContentOwnershipError, match="global size"):
        await claim_rag_content(
            settings.db_path,
            second,
            content_hash=digest,
            size_bytes=7,
            workflow_run_id="run-wrong-size",
            file_suffix=".md",
            storage_limit=settings.quota_storage_bytes,
        )
    assert await governor.usage(principal, "storage_bytes") == 6

    async with aiosqlite.connect(settings.db_path) as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM rag_content_owners")).fetchone()
    assert row == (1,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stage_release_race_never_leaves_an_unowned_blob(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)

    for index in range(8):
        content = f"race-{index}".encode()
        digest = _digest(content)
        run_id = f"run-race-{index}"
        await claim_rag_content(
            settings.db_path,
            principal,
            content_hash=digest,
            size_bytes=len(content),
            workflow_run_id=run_id,
            file_suffix=".md",
            storage_limit=settings.quota_storage_bytes,
        )
        staged, released = await asyncio.gather(
            stage_rag_content_blob(
                settings.db_path,
                settings.storage_dir,
                principal,
                content_hash=digest,
                workflow_run_id=run_id,
                content=content,
            ),
            release_rag_content_claim(
                settings.db_path,
                settings.storage_dir,
                principal,
                content_hash=digest,
            ),
            return_exceptions=True,
        )
        assert not isinstance(released, BaseException)
        if isinstance(staged, BaseException):
            assert isinstance(staged, RagContentOwnershipError)
        assert not rag_blob_path(settings.storage_dir, digest).exists()

    governor = PrincipalGovernor(settings)
    assert await governor.usage(principal, "storage_bytes") == 0
    root = rag_staging_root(settings.storage_dir)
    assert not list(root.glob(".rag-upload-*.tmp"))
    async with aiosqlite.connect(settings.db_path) as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM rag_content_owners")).fetchone()
    assert row == (0,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_reconcile_repairs_crash_windows_and_is_idempotent(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    service = WorkflowService(settings, InMemoryEventBus())
    token = bind_principal(principal)
    try:
        succeeded = await service.create_run(
            WorkflowRunCreate(kind="rag.ingest", intent_text="success"),
            run_id="run-success",
        )
        await service.start_run(succeeded.run_id)
        await service.complete_run(succeeded.run_id, output={"doc_id": "md-success"})
        failed = await service.create_run(
            WorkflowRunCreate(kind="rag.ingest", intent_text="failed"),
            run_id="run-failed",
        )
        await service.fail_run(failed.run_id, error="parser failed")
    finally:
        reset_principal(token)

    claimed_content = b"claimed"
    claimed_hash = _digest(claimed_content)
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=claimed_hash,
        size_bytes=len(claimed_content),
        workflow_run_id="run-never-created",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )

    succeeded_content = b"success"
    succeeded_hash = _digest(succeeded_content)
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=succeeded_hash,
        size_bytes=len(succeeded_content),
        workflow_run_id="run-success",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=succeeded_hash,
        workflow_run_id="run-success",
        content=succeeded_content,
    )

    failed_content = b"failed"
    failed_hash = _digest(failed_content)
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=failed_hash,
        size_bytes=len(failed_content),
        workflow_run_id="run-failed",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=failed_hash,
        workflow_run_id="run-failed",
        content=failed_content,
    )

    legacy_content = b"legacy"
    legacy_hash = _digest(legacy_content)
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=legacy_hash,
        size_bytes=len(legacy_content),
        workflow_run_id="run-legacy",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    root = rag_staging_root(settings.storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    legacy_path = root / f"{legacy_hash}.md"
    legacy_path.write_bytes(legacy_content)
    orphan_path = root / _digest(b"orphan")
    orphan_path.write_bytes(b"orphan")
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.execute(
            """UPDATE rag_content_owners
               SET state = 'ready', doc_id = 'legacy-doc', file_suffix = ''
               WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
            (principal.tenant_id, principal.owner_id, legacy_hash),
        )
        await conn.execute(
            """UPDATE principal_quota_ledger SET used = 999
               WHERE tenant_id = ? AND owner_id = ?
                 AND metric = 'storage_bytes' AND window_key = 'lifetime'""",
            (principal.tenant_id, principal.owner_id),
        )
        await conn.execute(
            """UPDATE execution_leases
               SET expires_at = 0, heartbeat_at = 0
               WHERE resource_kind IN ('rag-upload', 'rag-view')"""
        )
        await conn.commit()

    report = await reconcile_rag_content_storage(
        settings.db_path,
        settings.storage_dir,
    )

    assert report.released_acls == 2
    assert report.ready_acls_repaired == 1
    assert report.canonicalized_blobs == 1
    assert report.orphan_blobs_deleted >= 2
    assert report.quota_scopes_rebuilt == 1
    assert not rag_blob_path(settings.storage_dir, claimed_hash).exists()
    assert rag_blob_path(settings.storage_dir, succeeded_hash).exists()
    assert not rag_blob_path(settings.storage_dir, failed_hash).exists()
    assert rag_blob_path(settings.storage_dir, legacy_hash).exists()
    assert not legacy_path.exists()
    assert not orphan_path.exists()

    governor = PrincipalGovernor(settings)
    expected_usage = len(succeeded_content) + len(legacy_content)
    assert await governor.usage(principal, "storage_bytes") == expected_usage
    async with aiosqlite.connect(settings.db_path) as conn:
        rows = await (
            await conn.execute(
                """SELECT content_hash, state, doc_id FROM rag_content_owners
                   ORDER BY content_hash"""
            )
        ).fetchall()
    actual_rows = [(str(row[0]), str(row[1]), str(row[2])) for row in rows]
    assert actual_rows == sorted(
        [
            (legacy_hash, "ready", "legacy-doc"),
            (succeeded_hash, "ready", "md-success"),
        ]
    )

    second = await reconcile_rag_content_storage(
        settings.db_path,
        settings.storage_dir,
    )
    assert second == type(second)()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_instance_startup_does_not_delete_active_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    content = b"instance-a-active-upload"
    digest = _digest(content)
    run_id = "run-active-upload"
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=len(content),
        workflow_run_id=run_id,
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )

    entered = threading.Event()
    release = threading.Event()
    original_write = rag_ownership._write_upload_temp

    def latched_write(path: Path, payload: bytes) -> None:
        original_write(path, payload)
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("upload latch was not released")

    monkeypatch.setattr(rag_ownership, "_write_upload_temp", latched_write)
    instance_a = asyncio.create_task(
        stage_rag_content_blob(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
            workflow_run_id=run_id,
            content=content,
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
        root = rag_staging_root(settings.storage_dir)
        upload_temps = list(root.glob(f".rag-upload-{digest}-*.tmp"))
        assert len(upload_temps) == 1

        instance_b_report = await reconcile_rag_content_storage(
            settings.db_path,
            settings.storage_dir,
        )
        assert instance_b_report.released_acls == 0
        assert instance_b_report.temp_files_deleted == 0
        assert upload_temps[0].exists()
        assert (
            await get_rag_content_claim(
                settings.db_path,
                principal,
                content_hash=digest,
            )
            is not None
        )
    finally:
        release.set()
    staged = await instance_a
    assert staged.exists()
    handoff_report = await reconcile_rag_content_storage(
        settings.db_path,
        settings.storage_dir,
    )
    assert handoff_report.released_acls == 0
    assert staged.exists()
    handoff_claim = await get_rag_content_claim(
        settings.db_path,
        principal,
        content_hash=digest,
    )
    assert handoff_claim is not None and handoff_claim.state == "staged"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_instance_startup_does_not_delete_active_parser_view(
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    content = b"instance-a-active-view"
    digest = _digest(content)
    run_id = "run-active-view"
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=len(content),
        workflow_run_id=run_id,
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=digest,
        workflow_run_id=run_id,
        content=content,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    parser_paths: list[Path] = []

    async def instance_a_parser() -> None:
        async with open_rag_parser_input(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
            workflow_run_id=run_id,
        ) as parser_path:
            parser_paths.append(parser_path)
            entered.set()
            await release.wait()
            assert parser_path.exists()

    instance_a = asyncio.create_task(instance_a_parser())
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        assert len(parser_paths) == 1
        assert parser_paths[0].exists()
        instance_b_report = await reconcile_rag_content_storage(
            settings.db_path,
            settings.storage_dir,
        )
        assert instance_b_report.released_acls == 0
        assert instance_b_report.temp_files_deleted == 0
        assert parser_paths[0].exists()
        assert (
            await get_rag_content_claim(
                settings.db_path,
                principal,
                content_hash=digest,
            )
            is not None
        )
    finally:
        release.set()
    await instance_a
    assert not parser_paths[0].exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_reconcile_recovers_only_explicitly_expired_activity_files(
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    root = rag_staging_root(settings.storage_dir)
    root.mkdir(parents=True, exist_ok=True)

    stale_content = b"stale-upload"
    stale_digest = _digest(stale_content)
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=stale_digest,
        size_bytes=len(stale_content),
        workflow_run_id="run-stale-upload",
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )

    ready_content = b"stale-view"
    ready_digest = _digest(ready_content)
    ready_run_id = "run-stale-view"
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=ready_digest,
        size_bytes=len(ready_content),
        workflow_run_id=ready_run_id,
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=ready_digest,
        workflow_run_id=ready_run_id,
        content=ready_content,
    )
    await bind_rag_content_doc(
        settings.db_path,
        principal,
        content_hash=ready_digest,
        workflow_run_id=ready_run_id,
        doc_id="ready-doc",
    )

    stale_view_activity = "a" * 32
    stale_view = root / f".rag-view-{ready_digest}-{stale_view_activity}-1.md"
    stale_view.write_bytes(ready_content)
    async with aiosqlite.connect(settings.db_path) as conn:
        row = await (
            await conn.execute(
                """SELECT resource_id, holder_id, fence_token
                   FROM execution_leases
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = 'rag-upload'
                     AND substr(resource_id, 1, 65) = ?""",
                (principal.tenant_id, principal.owner_id, f"{stale_digest}:"),
            )
        ).fetchone()
        assert row is not None
        stale_upload = root / (f".rag-upload-{stale_digest}-{row[1]}-{int(row[2])}.tmp")
        stale_upload.write_bytes(stale_content)
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0, heartbeat_at = 0
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'rag-upload'
                 AND resource_id = ?""",
            (principal.tenant_id, principal.owner_id, str(row[0])),
        )
        await conn.execute(
            """INSERT INTO execution_leases
               (tenant_id, owner_id, resource_kind, resource_id, holder_id,
                fence_token, expires_at, heartbeat_at)
               VALUES (?, ?, 'rag-view', ?, ?, 1, 0, 0)""",
            (
                principal.tenant_id,
                principal.owner_id,
                f"{ready_digest}:{stale_view_activity}",
                stale_view_activity,
            ),
        )
        await conn.commit()

    decoy = root / f".rag-upload-{stale_digest}-{'f' * 32}-999.tmp"
    decoy.write_bytes(b"not-registered")
    report = await reconcile_rag_content_storage(
        settings.db_path,
        settings.storage_dir,
    )

    assert report.released_acls == 1
    assert report.temp_files_deleted == 2
    assert not stale_upload.exists()
    assert not stale_view.exists()
    assert decoy.exists()
    assert (
        await get_rag_content_claim(
            settings.db_path,
            principal,
            content_hash=stale_digest,
        )
        is None
    )
    ready_claim = await get_rag_content_claim(
        settings.db_path,
        principal,
        content_hash=ready_digest,
    )
    assert ready_claim is not None and ready_claim.state == "ready"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_cleanup_cannot_delete_a_reacquired_upload_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    content = b"reacquired-upload"
    digest = _digest(content)
    run_id = "run-reacquired-upload"
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=len(content),
        workflow_run_id=run_id,
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    root = rag_staging_root(settings.storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as conn:
        stale_row = await (
            await conn.execute(
                """SELECT resource_id, holder_id, fence_token
                   FROM execution_leases
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = 'rag-upload'
                     AND substr(resource_id, 1, 65) = ?""",
                (principal.tenant_id, principal.owner_id, f"{digest}:"),
            )
        ).fetchone()
        assert stale_row is not None
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0, heartbeat_at = 0
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'rag-upload' AND resource_id = ?""",
            (principal.tenant_id, principal.owner_id, str(stale_row[0])),
        )
        await conn.commit()
    stale_temp = root / (f".rag-upload-{digest}-{stale_row[1]}-{int(stale_row[2])}.tmp")
    stale_temp.write_bytes(content)

    entered = threading.Event()
    release = threading.Event()
    original_cleanup = rag_ownership._delete_stale_activity_files

    def latched_cleanup(
        cleanup_root: Path,
        rows: list[tuple[str, str, str, int]],
    ) -> int:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("stale cleanup latch was not released")
        return original_cleanup(cleanup_root, rows)

    monkeypatch.setattr(
        rag_ownership,
        "_delete_stale_activity_files",
        latched_cleanup,
    )
    instance_b = asyncio.create_task(
        reconcile_rag_content_storage(settings.db_path, settings.storage_dir)
    )
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
    try:
        await claim_rag_content(
            settings.db_path,
            principal,
            content_hash=digest,
            size_bytes=len(content),
            workflow_run_id=run_id,
            file_suffix=".md",
            storage_limit=settings.quota_storage_bytes,
        )
        async with aiosqlite.connect(settings.db_path) as conn:
            live_row = await (
                await conn.execute(
                    """SELECT holder_id, fence_token FROM execution_leases
                       WHERE tenant_id = ? AND owner_id = ?
                         AND resource_kind = 'rag-upload'
                         AND substr(resource_id, 1, 65) = ?""",
                    (principal.tenant_id, principal.owner_id, f"{digest}:"),
                )
            ).fetchone()
        assert live_row is not None
        assert str(live_row[0]) != str(stale_row[1])
        live_temp = root / (f".rag-upload-{digest}-{live_row[0]}-{int(live_row[1])}.tmp")
        live_temp.write_bytes(content)
    finally:
        release.set()

    report = await instance_b
    assert report.released_acls == 0
    assert report.temp_files_deleted == 1
    assert not stale_temp.exists()
    assert live_temp.exists()
    assert (
        await get_rag_content_claim(
            settings.db_path,
            principal,
            content_hash=digest,
        )
        is not None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_hashing_does_not_hold_sqlite_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    await seed_principal_identity(settings.db_path, principal)
    content = b"hash-latch"
    digest = _digest(content)
    run_id = "run-hash-latch"
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=len(content),
        workflow_run_id=run_id,
        file_suffix=".md",
        storage_limit=settings.quota_storage_bytes,
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=digest,
        workflow_run_id=run_id,
        content=content,
    )
    await bind_rag_content_doc(
        settings.db_path,
        principal,
        content_hash=digest,
        workflow_run_id=run_id,
        doc_id="hash-latch-doc",
    )

    entered = threading.Event()
    release = threading.Event()
    original_size = rag_ownership._blob_content_size

    def latched_size(path: Path, expected_digest: str) -> int | None:
        if expected_digest == digest and path.name == digest and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("hash latch was not released")
        return original_size(path, expected_digest)

    monkeypatch.setattr(rag_ownership, "_blob_content_size", latched_size)
    reconcile_task = asyncio.create_task(
        reconcile_rag_content_storage(
            settings.db_path,
            settings.storage_dir,
        )
    )
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)

    other_content = b"other-instance-write"
    other_claim_task = asyncio.create_task(
        claim_rag_content(
            settings.db_path,
            principal,
            content_hash=_digest(other_content),
            size_bytes=len(other_content),
            workflow_run_id="run-other-instance",
            file_suffix=".md",
            storage_limit=settings.quota_storage_bytes,
        )
    )
    done, _pending = await asyncio.wait({other_claim_task}, timeout=1)
    completed_before_hash_release = other_claim_task in done
    release.set()
    await reconcile_task
    await other_claim_task
    assert completed_before_hash_release is True
