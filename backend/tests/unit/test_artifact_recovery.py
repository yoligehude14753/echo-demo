from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.artifacts import staging as artifact_staging
from app.artifacts.recovery import (
    artifact_file_cleanup_target,
    recover_skill_build_artifacts,
    replay_artifact_file_cleanup_target,
    replay_succeeded_artifact_file_cleanups,
    validated_artifact_file_path,
)
from app.artifacts.repository import ArtifactFileUnavailableError, ArtifactRepository
from app.artifacts.staging import (
    WORKFLOW_BUILDING_DIR,
    cleanup_abandoned_builds,
    workflow_artifact_id,
    workflow_build_lease_marker,
)
from app.config import Settings
from app.runtime.execution_lease import ExecutionLeaseStore
from app.schemas.artifact import GeneratedArtifact
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import bind_principal, current_principal, reset_principal
from app.security.models import Principal
from app.security.scope import physical_resource_id_for, scoped_directory, scoped_directory_for
from app.workflows.service import WorkflowService


@pytest.mark.unit
async def test_artifact_locator_binds_skill_and_agent_storage_to_registered_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings(
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
    )
    tenant_id = "tenant-a"
    owner_id = "owner-a"

    skill_scope = scoped_directory_for(settings.skill_executor_build_dir, tenant_id, owner_id)
    own_output = skill_scope / "artifact-a" / "output.txt"
    sibling_output = skill_scope / "artifact-b" / "output.txt"
    own_output.parent.mkdir(parents=True)
    sibling_output.parent.mkdir(parents=True)
    own_output.write_text("artifact a", encoding="utf-8")
    sibling_output.write_text("artifact b", encoding="utf-8")
    assert (
        artifact_file_cleanup_target(
            settings,
            artifact_id="artifact-a",
            file_path=str(own_output),
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        is not None
    )
    assert (
        validated_artifact_file_path(
            settings,
            artifact_id="artifact-a",
            file_path=str(sibling_output),
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        is None
    )

    task_id = "task-agent-storage-binding"
    relpath = "out/report.pdf"
    artifact_id = f"agent-{hashlib.sha1(f'{task_id}:{relpath}'.encode()).hexdigest()[:24]}"
    task_dir = physical_resource_id_for(
        task_id,
        kind="agent-task",
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    storage_scope = scoped_directory_for(
        settings.storage_dir / "agent_artifacts",
        tenant_id,
        owner_id,
    )
    registered = storage_scope / task_dir / "out" / "report.pdf"
    sibling = storage_scope / task_dir / "out" / "other.pdf"
    registered.parent.mkdir(parents=True)
    registered.write_bytes(b"registered")
    sibling.write_bytes(b"same owner sibling")
    metadata = {"source": "agent", "agent_task_id": task_id, "relpath": relpath}
    assert (
        validated_artifact_file_path(
            settings,
            artifact_id=artifact_id,
            file_path=str(registered),
            tenant_id=tenant_id,
            owner_id=owner_id,
            metadata=metadata,
        )
        == registered
    )
    assert (
        validated_artifact_file_path(
            settings,
            artifact_id=artifact_id,
            file_path=str(sibling),
            tenant_id=tenant_id,
            owner_id=owner_id,
            metadata=metadata,
        )
        is None
    )
    target = artifact_file_cleanup_target(
        settings,
        artifact_id=artifact_id,
        file_path=str(registered),
        tenant_id=tenant_id,
        owner_id=owner_id,
        metadata=metadata,
    )
    assert target is not None and target["binding"] == "agent-storage-v1"
    tampered = {**target, "relative_path": sibling.relative_to(settings.storage_dir).as_posix()}
    assert (
        await replay_artifact_file_cleanup_target(
            settings,
            tampered,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        == "unsafe"
    )
    assert sibling.read_bytes() == b"same owner sibling"


@pytest.mark.unit
async def test_cleanup_replay_is_scoped_when_two_users_share_artifact_id(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    cleanup_output = json.dumps({"file_cleanup_artifact_ids": ["shared-artifact"]})
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        for tenant_id, owner_id in (("tenant-a", "owner-a"), ("tenant-b", "owner-b")):
            await conn.execute(
                """INSERT INTO workflow_runs
                   (run_id, kind, source, state, intent_text, output_json,
                    created_at, updated_at, tenant_id, device_id, owner_id)
                   VALUES ('cleanup-run', 'meeting.outputs.clear', 'test', 'succeeded',
                           'cleanup', ?, '2026-01-01', '2026-01-01', ?, 'device', ?)""",
                (cleanup_output, tenant_id, owner_id),
            )
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES ('shared-artifact', 'txt', '/tmp/b', 'text/plain',
                       '2026-01-01', '2026-01-01', 'tenant-b', 'device', 'owner-b')"""
        )
        await conn.commit()
    root = settings.skill_executor_build_dir
    directory_a = scoped_directory_for(root, "tenant-a", "owner-a") / "shared-artifact"
    directory_b = scoped_directory_for(root, "tenant-b", "owner-b") / "shared-artifact"
    for directory, content in ((directory_a, "a"), (directory_b, "b")):
        directory.mkdir(parents=True)
        (directory / "output.txt").write_text(content, encoding="utf-8")

    removed = await replay_succeeded_artifact_file_cleanups(settings)

    assert removed == 1
    assert not directory_a.exists()
    assert (directory_b / "output.txt").read_text(encoding="utf-8") == "b"


def _write_artifact(
    root: Path,
    artifact_id: str,
    *,
    suffix: str,
    meta: dict[str, object] | None,
) -> None:
    directory = root / artifact_id
    directory.mkdir(parents=True)
    (directory / f"output.{suffix}").write_bytes(b"historic artifact")
    if meta is not None:
        (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


async def _persist_public_scopes(
    settings: Settings,
    scopes: list[tuple[str, str]],
) -> None:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.executemany(
            """INSERT INTO tenants (tenant_id, status, created_at, updated_at)
               VALUES (?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(tenant_id) DO NOTHING""",
            [(tenant_id,) for tenant_id in sorted({item[0] for item in scopes})],
        )
        await conn.executemany(
            """INSERT INTO users
               (tenant_id, user_id, status, created_at, updated_at)
               VALUES (?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(tenant_id, user_id) DO NOTHING""",
            scopes,
        )
        await conn.commit()


def _public_principal(tenant_id: str, owner_id: str) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        device_id=f"device-{owner_id}",
        owner_id=owner_id,
        session_id=f"session-{owner_id}",
        mode="public",
        family_id=f"family-{owner_id}",
    )


@pytest.mark.unit
async def test_recovery_backfills_skill_build_and_links_historical_meeting(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    meeting_id = "m-history"
    artifact_id = "pptx-history-001"
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="历史会议")
        await repo.update_meeting_state(
            meeting_id,
            state="finalized",
            minutes_json=json.dumps(
                {"todos": [{"id": "todo-history", "artifact_id": artifact_id}]}
            ),
        )
        _write_artifact(
            settings.skill_executor_build_dir,
            artifact_id,
            suffix="pptx",
            meta={
                "title": "历史方案演示",
                "artifact_type": "pptx",
                "meeting_id": meeting_id,
            },
        )
        _write_artifact(
            settings.skill_executor_build_dir,
            "html-unlinked-001",
            suffix="html",
            meta=None,
        )
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        linked_dir = settings.skill_executor_build_dir / "txt-outside-001"
        linked_dir.mkdir(parents=True)
        with contextlib.suppress(OSError):
            (linked_dir / "output.txt").symlink_to(outside)

        artifact_repo = ArtifactRepository(settings)
        first = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )

        assert first.discovered == 2
        assert first.recovered == 2
        assert first.linked == 1
        assert {item.artifact_id for item in await artifact_repo.list_artifacts(limit=10)} == {
            "html-unlinked-001",
            artifact_id,
        }
        linked = await artifact_repo.list_meeting_artifacts(meeting_id)
        assert [item.artifact_id for item in linked] == [artifact_id]
        assert linked[0].metadata["recovered"] == "true"
        assert linked[0].metadata["meeting_id"] == meeting_id

        second = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )
        assert second.recovered == 0
        assert second.linked == 0
        assert second.already_recorded == 2
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_recovery_does_not_duplicate_existing_meeting_link(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    meeting_id = "m-existing-link"
    artifact_id = "html-existing-link"
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="已有关联")
        _write_artifact(
            settings.skill_executor_build_dir,
            artifact_id,
            suffix="html",
            meta={"title": "已有产物", "artifact_type": "html", "meeting_id": meeting_id},
        )
        output = settings.skill_executor_build_dir / artifact_id / "output.html"

        artifact_repo = ArtifactRepository(settings)
        await artifact_repo.save_artifact(
            GeneratedArtifact(
                artifact_id=artifact_id,
                artifact_type="html",
                title="已有产物",
                file_path=str(output),
                mime_type="text/html",
                size_bytes=output.stat().st_size,
                generation_latency_ms=1,
                model="test",
                metadata={},
            )
        )
        await artifact_repo.link_artifact(
            artifact_id=artifact_id,
            source="artifact_generate",
            meeting_id=meeting_id,
        )

        report = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )

        assert report.discovered == 1
        assert report.recovered == 0
        assert report.linked == 0
        assert report.already_recorded == 1
        assert len(await artifact_repo.list_links_for_artifact(artifact_id)) == 1
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_recovery_skips_workflow_outputs_and_cleans_abandoned_builds(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    assert (await run_migrations(db_path)).errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        artifact_id = workflow_artifact_id("run-crashed", "pdf")
        _write_artifact(
            settings.skill_executor_build_dir,
            artifact_id,
            suffix="pdf",
            meta={"title": "workflow output", "artifact_type": "pdf"},
        )
        abandoned = settings.skill_executor_build_dir / WORKFLOW_BUILDING_DIR / "partial"
        abandoned.mkdir(parents=True)
        (abandoned / "output.pdf").write_bytes(b"partial")
        old = datetime.now(UTC).timestamp() - settings.artifact_build_stale_grace_s - 1
        os.utime(abandoned, (old, old))

        artifact_repo = ArtifactRepository(settings)
        report = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )

        assert report.workflow_managed == 1
        assert report.abandoned_builds_cleaned == 1
        assert report.discovered == 0
        assert await artifact_repo.list_artifacts() == []
        assert not abandoned.exists()
        assert (settings.skill_executor_build_dir / artifact_id / "output.pdf").is_file()
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_startup_cleanup_does_not_delete_other_process_active_workflow_build(
    tmp_path: Path,
) -> None:
    """Instance B startup is fenced from instance A's paused generator bytes."""

    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    run_id = "run_active_other_process"
    leases = ExecutionLeaseStore(settings.db_path)
    lease = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="instance-a",
        ttl_seconds=300,
    )
    assert lease is not None
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = scoped_directory(settings.skill_executor_build_dir) / WORKFLOW_BUILDING_DIR
    build_dir = building_root / f"{artifact_id}-private"
    ready = asyncio.Event()
    resume = asyncio.Event()

    async def instance_a() -> None:
        with workflow_build_lease_marker(
            settings,
            run_id=run_id,
            artifact_type="pdf",
            fence_token=lease.fence_token,
        ):
            build_dir.mkdir(parents=True)
            partial = build_dir / "output.pdf"
            partial.write_bytes(b"%PDF-active")
            ready.set()
            await resume.wait()
            assert partial.read_bytes() == b"%PDF-active"

    producer = asyncio.create_task(instance_a())
    await ready.wait()
    try:
        assert await cleanup_abandoned_builds(settings) == 0
        assert (build_dir / "output.pdf").is_file()
    finally:
        resume.set()
        await producer
        await leases.release(lease)


@pytest.mark.unit
async def test_startup_cleanup_removes_build_after_durable_lease_is_released(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    run_id = "run_released_before_cleanup"
    leases = ExecutionLeaseStore(settings.db_path)
    lease = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="crashed-instance",
        ttl_seconds=300,
    )
    assert lease is not None
    artifact_id = workflow_artifact_id(run_id, "pdf")
    build_dir = (
        scoped_directory(settings.skill_executor_build_dir)
        / WORKFLOW_BUILDING_DIR
        / f"{artifact_id}-private"
    )
    with workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=lease.fence_token,
    ):
        build_dir.mkdir(parents=True)
        (build_dir / "output.pdf").write_bytes(b"partial")
        assert await leases.release(lease)
        assert await cleanup_abandoned_builds(settings) == 1
        assert not build_dir.exists()


@pytest.mark.unit
async def test_startup_cleanup_rechecks_marker_generation_before_deleting(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    run_id = "run_successor_generation_race"
    leases = ExecutionLeaseStore(settings.db_path)
    old_lease = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="expired-instance",
        ttl_seconds=300,
    )
    assert old_lease is not None
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = scoped_directory(settings.skill_executor_build_dir) / WORKFLOW_BUILDING_DIR
    old_build = building_root / f"{artifact_id}-old-private"
    new_build = building_root / f"{artifact_id}-new-private"
    marker_path = building_root / f".workflow-active-{artifact_id}.json"
    old_marker = workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=old_lease.fence_token,
    )
    old_marker.__enter__()
    cleanup_task: asyncio.Task[int] | None = None
    successor_marker: contextlib.AbstractContextManager[None] | None = None
    successor_lease = None
    resume_cleanup = asyncio.Event()
    snapshot_checked = asyncio.Event()
    original_check = artifact_staging._has_live_workflow_lease
    paused = False

    async def pause_after_expired_snapshot(
        conn: aiosqlite.Connection,
        marker: object,
        *,
        now: float,
    ) -> bool:
        nonlocal paused
        assert isinstance(marker, artifact_staging._ActiveBuildMarker)
        result = await original_check(conn, marker, now=now)
        if marker.fence_token == old_lease.fence_token and not paused:
            paused = True
            snapshot_checked.set()
            await resume_cleanup.wait()
        return result

    monkeypatch.setattr(
        artifact_staging,
        "_has_live_workflow_lease",
        pause_after_expired_snapshot,
    )
    try:
        old_build.mkdir(parents=True)
        (old_build / "output.pdf").write_bytes(b"expired-generation")
        assert await leases.release(old_lease)
        cleanup_task = asyncio.create_task(cleanup_abandoned_builds(settings))
        await asyncio.wait_for(snapshot_checked.wait(), timeout=1.0)

        successor_lease = await leases.acquire(
            tenant_id=principal.tenant_id,
            owner_id=principal.owner_id,
            resource_kind="workflow",
            resource_id=run_id,
            holder_id="successor-instance",
            ttl_seconds=300,
        )
        assert successor_lease is not None
        assert successor_lease.fence_token > old_lease.fence_token
        successor_marker = workflow_build_lease_marker(
            settings,
            run_id=run_id,
            artifact_type="pdf",
            fence_token=successor_lease.fence_token,
        )
        successor_marker.__enter__()
        new_build.mkdir(parents=True)
        (new_build / "output.pdf").write_bytes(b"active-successor")
        resume_cleanup.set()

        assert await cleanup_task == 0
        assert (old_build / "output.pdf").read_bytes() == b"expired-generation"
        assert (new_build / "output.pdf").read_bytes() == b"active-successor"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["fence_token"] == successor_lease.fence_token
    finally:
        resume_cleanup.set()
        if cleanup_task is not None and not cleanup_task.done():
            await asyncio.gather(cleanup_task, return_exceptions=True)
        if successor_marker is not None:
            successor_marker.__exit__(None, None, None)
        if successor_lease is not None:
            await leases.release(successor_lease)
        old_marker.__exit__(None, None, None)


@pytest.mark.unit
async def test_startup_cleanup_pages_to_expired_public_principal_build(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    scopes = [("tenant-public", f"owner-{index:03}") for index in range(140)]
    await _persist_public_scopes(settings, scopes)
    tenant_id, owner_id = scopes[-1]
    principal = _public_principal(tenant_id, owner_id)
    run_id = "run_public_expired_partial"
    leases = ExecutionLeaseStore(settings.db_path)
    lease = await leases.acquire(
        tenant_id=tenant_id,
        owner_id=owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="crashed-public-instance",
        ttl_seconds=300,
    )
    assert lease is not None
    marker_context = workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=lease.fence_token,
    )
    token = bind_principal(principal)
    try:
        marker_context.__enter__()
    finally:
        reset_principal(token)
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = (
        scoped_directory_for(settings.skill_executor_build_dir, tenant_id, owner_id)
        / WORKFLOW_BUILDING_DIR
    )
    partial = building_root / f"{artifact_id}-public-private"
    try:
        partial.mkdir(parents=True)
        (partial / "output.pdf").write_bytes(b"public-partial")
        assert await leases.release(lease)

        assert await cleanup_abandoned_builds(settings) == 1
        assert not partial.exists()
        assert not (building_root / f".workflow-active-{artifact_id}.json").exists()
    finally:
        marker_context.__exit__(None, None, None)


@pytest.mark.unit
async def test_startup_cleanup_preserves_live_public_successor_generation(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    tenant_id = "tenant-public-successor"
    owner_id = "owner-public-successor"
    await _persist_public_scopes(settings, [(tenant_id, owner_id)])
    principal = _public_principal(tenant_id, owner_id)
    run_id = "run_public_successor"
    leases = ExecutionLeaseStore(settings.db_path)
    old_lease = await leases.acquire(
        tenant_id=tenant_id,
        owner_id=owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="expired-public-instance",
        ttl_seconds=300,
    )
    assert old_lease is not None
    old_marker = workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=old_lease.fence_token,
    )
    token = bind_principal(principal)
    try:
        old_marker.__enter__()
    finally:
        reset_principal(token)
    assert await leases.release(old_lease)
    successor_lease = await leases.acquire(
        tenant_id=tenant_id,
        owner_id=owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="live-public-successor",
        ttl_seconds=300,
    )
    assert successor_lease is not None
    assert successor_lease.fence_token > old_lease.fence_token
    successor_marker = workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=successor_lease.fence_token,
    )
    token = bind_principal(principal)
    try:
        successor_marker.__enter__()
    finally:
        reset_principal(token)
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = (
        scoped_directory_for(settings.skill_executor_build_dir, tenant_id, owner_id)
        / WORKFLOW_BUILDING_DIR
    )
    old_build = building_root / f"{artifact_id}-old-private"
    successor_build = building_root / f"{artifact_id}-successor-private"
    try:
        old_build.mkdir(parents=True)
        successor_build.mkdir(parents=True)
        (old_build / "output.pdf").write_bytes(b"old")
        (successor_build / "output.pdf").write_bytes(b"successor")

        assert await cleanup_abandoned_builds(settings) == 0
        assert (old_build / "output.pdf").read_bytes() == b"old"
        assert (successor_build / "output.pdf").read_bytes() == b"successor"
    finally:
        successor_marker.__exit__(None, None, None)
        old_marker.__exit__(None, None, None)
        await leases.release(successor_lease)


@pytest.mark.unit
async def test_startup_cleanup_rejects_marker_whose_scope_disagrees_with_directory_hash(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        artifact_build_stale_grace_s=60,
    )
    assert (await run_migrations(settings.db_path)).errors == []
    scope_a = ("tenant-scope-a", "owner-scope-a")
    scope_b = ("tenant-scope-b", "owner-scope-b")
    await _persist_public_scopes(settings, [scope_a, scope_b])
    run_id = "run_mismatched_scope_marker"
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = (
        scoped_directory_for(settings.skill_executor_build_dir, *scope_a) / WORKFLOW_BUILDING_DIR
    )
    partial = building_root / f"{artifact_id}-private"
    partial.mkdir(parents=True)
    (partial / "output.pdf").write_bytes(b"must-survive")
    marker = building_root / f".workflow-active-{artifact_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "run_id": run_id,
                "tenant_id": scope_b[0],
                "owner_id": scope_b[1],
                "fence_token": 1,
            }
        ),
        encoding="utf-8",
    )
    old = datetime.now(UTC).timestamp() - 61
    os.utime(partial, (old, old))
    os.utime(marker, (old, old))

    assert await cleanup_abandoned_builds(settings) == 0
    assert (partial / "output.pdf").read_bytes() == b"must-survive"
    assert marker.is_file()
    assert not (building_root / ".workflow-quarantine").exists()


@pytest.mark.unit
async def test_startup_cleanup_rejects_marker_with_wrong_run_for_live_artifact_id(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        artifact_build_stale_grace_s=60,
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    correct_run_id = "run_live_correct_generation"
    wrong_run_id = "run_marker_spoofed_generation"
    leases = ExecutionLeaseStore(settings.db_path)
    live = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id=correct_run_id,
        holder_id="live-correct-instance",
        ttl_seconds=300,
    )
    assert live is not None
    artifact_id = workflow_artifact_id(correct_run_id, "pdf")
    building_root = scoped_directory(settings.skill_executor_build_dir) / WORKFLOW_BUILDING_DIR
    partial = building_root / f"{artifact_id}-private"
    partial.mkdir(parents=True)
    (partial / "output.pdf").write_bytes(b"live-correct-bytes")
    marker = building_root / f".workflow-active-{artifact_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "run_id": wrong_run_id,
                "tenant_id": principal.tenant_id,
                "owner_id": principal.owner_id,
                "fence_token": live.fence_token,
            }
        ),
        encoding="utf-8",
    )
    old = datetime.now(UTC).timestamp() - 61
    os.utime(partial, (old, old))
    os.utime(marker, (old, old))
    try:
        assert await cleanup_abandoned_builds(settings) == 0
        assert await cleanup_abandoned_builds(settings) == 0
        assert (partial / "output.pdf").read_bytes() == b"live-correct-bytes"
        assert marker.is_file()
        assert not (building_root / ".workflow-quarantine").exists()
    finally:
        await leases.release(live)


@pytest.mark.unit
async def test_startup_cleanup_retries_detached_quarantine_after_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    run_id = "run_quarantine_retry"
    leases = ExecutionLeaseStore(settings.db_path)
    lease = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id=run_id,
        holder_id="expired-instance",
        ttl_seconds=300,
    )
    assert lease is not None
    artifact_id = workflow_artifact_id(run_id, "pdf")
    building_root = scoped_directory(settings.skill_executor_build_dir) / WORKFLOW_BUILDING_DIR
    build = building_root / f"{artifact_id}-private"
    original_rmtree = shutil.rmtree
    with workflow_build_lease_marker(
        settings,
        run_id=run_id,
        artifact_type="pdf",
        fence_token=lease.fence_token,
    ):
        build.mkdir(parents=True)
        (build / "output.pdf").write_bytes(b"detached")
        assert await leases.release(lease)

        def fail_delete(_path: Path) -> None:
            raise OSError("injected delete failure")

        monkeypatch.setattr(shutil, "rmtree", fail_delete)
        assert await cleanup_abandoned_builds(settings) == 0
        quarantine = building_root / ".workflow-quarantine"
        assert not build.exists()
        assert len(list(quarantine.iterdir())) == 1

        monkeypatch.setattr(shutil, "rmtree", original_rmtree)
        assert await cleanup_abandoned_builds(settings) == 1
        assert not quarantine.exists()


@pytest.mark.unit
async def test_slow_seven_second_quarantine_delete_does_not_lock_unrelated_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = current_principal()
    leases = ExecutionLeaseStore(settings.db_path)
    expired = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id="run_slow_cleanup",
        holder_id="expired-instance",
        ttl_seconds=300,
    )
    unrelated = await leases.acquire(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind="workflow",
        resource_id="run_unrelated_renew",
        holder_id="healthy-instance",
        ttl_seconds=300,
    )
    assert expired is not None
    assert unrelated is not None
    artifact_id = workflow_artifact_id("run_slow_cleanup", "pdf")
    building_root = scoped_directory(settings.skill_executor_build_dir) / WORKFLOW_BUILDING_DIR
    build = building_root / f"{artifact_id}-private"
    delete_entered = threading.Event()
    release_delete = threading.Event()
    original_rmtree = shutil.rmtree
    cleanup_task: asyncio.Task[int] | None = None

    def slow_rmtree(path: Path) -> None:
        delete_entered.set()
        if not release_delete.wait(timeout=7.0):
            raise TimeoutError("test did not release seven-second deletion latch")
        original_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", slow_rmtree)
    try:
        with workflow_build_lease_marker(
            settings,
            run_id="run_slow_cleanup",
            artifact_type="pdf",
            fence_token=expired.fence_token,
        ):
            build.mkdir(parents=True)
            (build / "output.pdf").write_bytes(b"slow-delete")
            assert await leases.release(expired)
            cleanup_task = asyncio.create_task(cleanup_abandoned_builds(settings))
            assert await asyncio.to_thread(delete_entered.wait, 2.0)

            renewed = await asyncio.wait_for(
                leases.renew(unrelated, ttl_seconds=300),
                timeout=1.0,
            )
            assert renewed is not None
            unrelated = renewed
            release_delete.set()
            assert await cleanup_task == 1
            assert not build.exists()
    finally:
        release_delete.set()
        if cleanup_task is not None and not cleanup_task.done():
            await asyncio.gather(cleanup_task, return_exceptions=True)
        await leases.release(unrelated)


@pytest.mark.unit
async def test_recovery_replays_succeeded_artifact_file_cleanup_intent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    assert (await run_migrations(db_path)).errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    artifact_id = "pdf-cleanup-after-crash"
    symlink_id = "pdf-cleanup-symlink"
    _write_artifact(
        settings.skill_executor_build_dir,
        artifact_id,
        suffix="pdf",
        meta={"title": "delete me", "artifact_type": "pdf"},
    )
    outside = tmp_path / "outside-artifact"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (settings.skill_executor_build_dir / symlink_id).symlink_to(outside, target_is_directory=True)
    service = WorkflowService(settings, InMemoryEventBus())
    run = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.outputs.clear",
            source="test",
            intent_text="durable file cleanup",
        )
    )
    await service.start_run(run.run_id)
    await service.complete_run(
        run.run_id,
        output={"file_cleanup_artifact_ids": [artifact_id, symlink_id]},
    )

    assert await replay_succeeded_artifact_file_cleanups(settings) == 1
    assert not (settings.skill_executor_build_dir / artifact_id).exists()
    assert (outside / "keep.txt").is_file()
    assert await replay_succeeded_artifact_file_cleanups(settings) == 0

    _write_artifact(
        settings.skill_executor_build_dir,
        artifact_id,
        suffix="pdf",
        meta={"title": "new registered output", "artifact_type": "pdf"},
    )
    output = settings.skill_executor_build_dir / artifact_id / "output.pdf"
    await ArtifactRepository(settings).save_artifact(
        GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="pdf",
            title="new registered output",
            file_path=str(output),
            mime_type="application/pdf",
            size_bytes=output.stat().st_size,
            generation_latency_ms=0,
            model="test",
        )
    )
    assert await replay_succeeded_artifact_file_cleanups(settings) == 0
    assert output.is_file()


@pytest.mark.unit
async def test_cleanup_replay_deletes_storage_target_but_rejects_escape_and_path_reuse(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    stale = settings.storage_dir / "agent_artifacts" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("delete after crash", encoding="utf-8")
    reused = settings.storage_dir / "agent_artifacts" / "reused.txt"
    reused.write_text("current artifact owns this path", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("never delete outside controlled roots", encoding="utf-8")
    assert (
        artifact_file_cleanup_target(
            settings,
            artifact_id="outside-artifact",
            file_path=str(outside),
            tenant_id="legacy-local",
            owner_id="legacy-local",
        )
        is None
    )
    assert (
        artifact_file_cleanup_target(
            settings,
            artifact_id="cross-scope-artifact",
            file_path=str(reused),
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        is None
    )

    stale_target = artifact_file_cleanup_target(
        settings,
        artifact_id="stale-storage-artifact",
        file_path=str(stale),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    reused_target = artifact_file_cleanup_target(
        settings,
        artifact_id="old-artifact-id",
        file_path=str(reused),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    assert stale_target is not None and reused_target is not None

    service = WorkflowService(settings, InMemoryEventBus())
    run = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.outputs.clear",
            source="test",
            intent_text="durable standalone cleanup",
        )
    )
    await service.start_run(run.run_id)
    await service.complete_run(
        run.run_id,
        output={
            "file_cleanup_artifact_ids": [
                "stale-storage-artifact",
                "old-artifact-id",
                "escape-artifact",
            ],
            "file_cleanup_targets": [
                stale_target,
                reused_target,
                {
                    "artifact_id": "escape-artifact",
                    "root": "storage",
                    "relative_path": "../outside.txt",
                },
                {
                    "artifact_id": "absolute-artifact",
                    "root": "storage",
                    "relative_path": str(outside),
                },
            ],
        },
    )
    await ArtifactRepository(settings).save_artifact(
        GeneratedArtifact(
            artifact_id="new-artifact-id",
            artifact_type="txt",
            title="current path owner",
            file_path=str(reused),
            mime_type="text/plain",
            size_bytes=reused.stat().st_size,
            generation_latency_ms=0,
            model="test",
        )
    )

    assert await replay_succeeded_artifact_file_cleanups(settings) == 1
    assert not stale.exists()
    assert reused.read_text(encoding="utf-8") == "current artifact owns this path"
    assert outside.read_text(encoding="utf-8") == "never delete outside controlled roots"
    assert await replay_succeeded_artifact_file_cleanups(settings) == 0
    await service.aclose()


@pytest.mark.unit
async def test_cleanup_replay_expands_tilde_db_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    settings = Settings(
        db_path=Path("~/.echodesk/echo.db"),
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    expanded_db = Path(settings.db_path).expanduser()
    assert (await run_migrations(expanded_db)).errors == []
    output = settings.storage_dir / "tilde-replay.txt"
    output.parent.mkdir(parents=True)
    output.write_text("delete from expanded database", encoding="utf-8")
    target = artifact_file_cleanup_target(
        settings,
        artifact_id="tilde-replay-artifact",
        file_path=str(output),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    assert target is not None
    async with aiosqlite.connect(expanded_db) as conn:
        await conn.execute(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, output_json,
                created_at, updated_at, tenant_id, device_id, owner_id)
               VALUES ('tilde-cleanup', 'meeting.outputs.clear', 'test', 'succeeded',
                       'tilde cleanup', ?, '2026-01-01', '2026-01-01',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (json.dumps({"file_cleanup_targets": [target]}),),
        )
        await conn.commit()

    assert await replay_succeeded_artifact_file_cleanups(settings) == 1
    assert not output.exists()


@pytest.mark.unit
async def test_online_cleanup_rejects_public_cross_scope_and_symlink_targets(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    scope_b = scoped_directory_for(settings.storage_dir, "tenant-b", "owner-b")
    cross_scope_file = scope_b / "private.txt"
    cross_scope_file.parent.mkdir(parents=True)
    cross_scope_file.write_text("owner B", encoding="utf-8")
    cross_scope_target = artifact_file_cleanup_target(
        settings,
        artifact_id="cross-scope",
        file_path=str(cross_scope_file),
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert cross_scope_target is None
    assert (
        await replay_artifact_file_cleanup_target(
            settings,
            cross_scope_target,
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        == "unsafe"
    )
    assert cross_scope_file.read_text(encoding="utf-8") == "owner B"

    scope_a = scoped_directory_for(settings.storage_dir, "tenant-a", "owner-a")
    real_file = scope_a / "real.txt"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("do not follow links", encoding="utf-8")
    symlink = scope_a / "linked.txt"
    try:
        symlink.symlink_to(real_file)
    except OSError:
        return
    symlink_target = artifact_file_cleanup_target(
        settings,
        artifact_id="symlink-artifact",
        file_path=str(symlink),
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    assert symlink_target is None
    assert (
        await replay_artifact_file_cleanup_target(
            settings,
            symlink_target,
            tenant_id="tenant-a",
            owner_id="owner-a",
        )
        == "unsafe"
    )
    assert symlink.is_symlink()
    assert real_file.read_text(encoding="utf-8") == "do not follow links"


@pytest.mark.unit
async def test_online_cleanup_retry_rejects_path_reuse_and_symlink_replacement(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    reused = settings.storage_dir / "agent_artifacts" / "reused-online.txt"
    reused.parent.mkdir(parents=True)
    reused.write_text("new owner data", encoding="utf-8")
    reused_target = artifact_file_cleanup_target(
        settings,
        artifact_id="old-online-artifact",
        file_path=str(reused),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    assert reused_target is not None
    await ArtifactRepository(settings).save_artifact(
        GeneratedArtifact(
            artifact_id="new-online-artifact",
            artifact_type="txt",
            title="new path owner",
            file_path=str(reused),
            mime_type="text/plain",
            size_bytes=reused.stat().st_size,
            generation_latency_ms=0,
            model="test",
        )
    )
    assert (
        await replay_artifact_file_cleanup_target(
            settings,
            reused_target,
            tenant_id="legacy-local",
            owner_id="legacy-local",
        )
        == "protected"
    )
    assert reused.read_text(encoding="utf-8") == "new owner data"

    linked = settings.storage_dir / "agent_artifacts" / "linked-online.txt"
    linked.write_text("old", encoding="utf-8")
    linked_target = artifact_file_cleanup_target(
        settings,
        artifact_id="linked-online-artifact",
        file_path=str(linked),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    assert linked_target is not None
    linked.unlink()
    outside = tmp_path / "outside-online.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        linked.symlink_to(outside)
    except OSError:
        return
    assert (
        await replay_artifact_file_cleanup_target(
            settings,
            linked_target,
            tenant_id="legacy-local",
            owner_id="legacy-local",
        )
        == "unsafe"
    )
    assert linked.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.unit
async def test_online_cleanup_lock_rejects_file_before_db_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    old_artifact_id = "cleanup-window-old"
    build_dir = settings.skill_executor_build_dir / old_artifact_id
    build_dir.mkdir(parents=True)
    output = build_dir / "output.txt"
    output.write_bytes(b"old bytes")
    target = artifact_file_cleanup_target(
        settings,
        artifact_id=old_artifact_id,
        file_path=str(output),
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    assert target is not None

    deletion_entered = threading.Event()
    registration_started = threading.Event()
    writer_errors: list[BaseException] = []
    replacement = GeneratedArtifact(
        artifact_id="cleanup-window-new",
        artifact_type="txt",
        title="new writer",
        file_path=str(output),
        mime_type="text/plain",
        size_bytes=16,
        generation_latency_ms=0,
        model="test",
    )
    original_rmtree = shutil.rmtree

    def wait_for_file_before_db(path: Path) -> None:
        deletion_entered.set()
        assert registration_started.wait(timeout=2)
        original_rmtree(path)

    monkeypatch.setattr("app.artifacts.recovery.shutil.rmtree", wait_for_file_before_db)

    def register_after_writing_file() -> None:
        assert deletion_entered.wait(timeout=2)
        output.write_bytes(b"new before db")
        registration_started.set()
        try:
            asyncio.run(ArtifactRepository(settings).save_artifact(replacement))
        except BaseException as exc:  # captured for assertion in the pytest thread
            writer_errors.append(exc)

    writer = threading.Thread(target=register_after_writing_file, daemon=True)
    writer.start()
    outcome = await replay_artifact_file_cleanup_target(
        settings,
        target,
        tenant_id="legacy-local",
        owner_id="legacy-local",
    )
    await asyncio.to_thread(writer.join, 3)

    assert outcome == "deleted"
    assert not writer.is_alive()
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], ArtifactFileUnavailableError)
    assert not output.exists()
    assert await ArtifactRepository(settings).get_artifact(replacement.artifact_id) is None
