from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus, SlowConsumerError
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations
from app.config import Settings
from app.schemas.events import EchoEvent
from app.schemas.workflow import WorkflowRunCreate
from app.security import Principal
from app.security.context import bind_principal, reset_principal
from app.workflows import service as workflow_service_module
from app.workflows.service import (
    InvalidWorkflowTransition,
    WorkflowConflictError,
    WorkflowService,
)


async def _service(
    tmp_path: Path,
    **settings_overrides: Any,
) -> tuple[WorkflowService, InMemoryEventBus]:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    bus = InMemoryEventBus()
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        **settings_overrides,
    )
    return WorkflowService(settings, bus), bus


def _migrations_through(tmp_path: Path, version: int) -> Path:
    target = tmp_path / f"migrations-through-{version}"
    target.mkdir()
    for source in _DEFAULT_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"):
        if int(source.name.split("_", 1)[0]) <= version:
            shutil.copy2(source, target / source.name)
    return target


async def _insert_outbox_rows(
    db_path: Path,
    count: int,
    *,
    start_index: int = 0,
    unpublished_indexes: set[int] | None = None,
    published_at: str = "2020-01-01T00:00:00+00:00",
) -> None:
    unpublished = unpublished_indexes or set()
    values = []
    for index in range(start_index, start_index + count):
        values.append(
            (
                f"row-{index}",
                json.dumps({"payload": {"index": index}}),
                None if index in unpublished else published_at,
            )
        )
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at, published_at)
               VALUES ('legacy-local', 'legacy-local', 'legacy-local',
                       'domain', ?, 'workflow.snapshot', ?,
                       '2020-01-01T00:00:00+00:00', ?)""",
            values,
        )
        await conn.commit()


@pytest.mark.unit
async def test_workflow_lifecycle_records_events_and_snapshots(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES ('mtg-1', 'ended', '2026-01-01',
                       'legacy-local', 'legacy-local', 'legacy-local')"""
        )
        await conn.commit()

    run = await service.create_run(
        WorkflowRunCreate(
            kind="artifact.generate",
            source="todo",
            title="生成会议 PDF",
            intent_text="把会议纪要生成 PDF",
            meeting_id="mtg-1",
            todo_id="todo-1",
            input={"artifact_type": "pdf"},
        )
    )
    assert run.state == "pending"

    started = await service.start_run(run.run_id)
    assert started is not None
    assert started.state == "running"
    done = await service.complete_run(run.run_id, output={"artifact_id": "pdf-1"})
    assert done is not None
    assert done.state == "succeeded"
    assert done.output["artifact_id"] == "pdf-1"

    events = await service.list_events(run.run_id)
    assert [event.event_type for event in events] == [
        "workflow.created",
        "workflow.started",
        "workflow.succeeded",
    ]
    assert bus.max_seq >= 6  # 每个状态至少有 event + snapshot 投影。


@pytest.mark.unit
async def test_workflow_retry_preserves_parent_reference(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    run = await service.create_run(
        WorkflowRunCreate(
            kind="artifact.generate",
            source="artifact_api",
            intent_text="生成失败后重试",
            input={"artifact_type": "html"},
        )
    )
    await service.start_run(run.run_id)
    failed = await service.fail_run(run.run_id, error="boom")
    assert failed is not None
    assert failed.state == "failed"

    retry = await service.retry_run(run.run_id, reason="user_retry")
    assert retry is not None
    assert retry.state == "pending"
    assert retry.input["retry_of"] == run.run_id
    assert retry.input["retry_reason"] == "user_retry"

    old_events = await service.list_events(run.run_id)
    assert old_events[-1].event_type == "workflow.retry_created"


@pytest.mark.unit
async def test_concurrent_retry_without_parent_idempotency_creates_one_child(
    tmp_path: Path,
) -> None:
    first, _bus = await _service(tmp_path)
    second = WorkflowService(first.settings, InMemoryEventBus())
    run = await first.create_run(
        WorkflowRunCreate(
            kind="artifact.generate",
            source="artifact_api",
            intent_text="没有客户端幂等键的失败任务",
        )
    )
    await first.start_run(run.run_id)
    await first.fail_run(run.run_id, error="boom")

    retries = await asyncio.gather(
        first.retry_run(run.run_id, reason="first caller"),
        second.retry_run(run.run_id, reason="second caller"),
    )

    assert retries[0] is not None and retries[1] is not None
    assert retries[0].run_id == retries[1].run_id
    assert retries[0].idempotency_key == f"workflow.retry:{run.run_id}:2"
    runs = await first.list_runs()
    assert len(runs) == 2
    parent_events = await first.list_events(run.run_id)
    assert [event.event_type for event in parent_events].count("workflow.retry_created") == 1


@pytest.mark.unit
async def test_fresh_active_run_wins_retry_race_without_lineage_impersonation(
    tmp_path: Path,
) -> None:
    service, _bus = await _service(tmp_path)
    active_key = "meeting.finalize:retry-race-fresh"
    parent = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="test",
            intent_text="failed generation",
            active_key=active_key,
        )
    )
    await service.start_run(parent.run_id)
    await service.fail_run(parent.run_id, error="provider down")
    fresh = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="explicit-fresh",
            intent_text="new explicit generation",
            idempotency_key="fresh-generation",
            active_key=active_key,
        )
    )

    with pytest.raises(WorkflowConflictError, match="won retry race"):
        await service.retry_run(parent.run_id, reason="late retry")

    assert fresh.parent_run_id is None
    assert fresh.state == "pending"
    assert len(await service.list_runs()) == 2
    assert all(
        event.event_type != "workflow.retry_created"
        for event in await service.list_events(parent.run_id)
    )


@pytest.mark.unit
async def test_retry_child_wins_active_key_and_fresh_request_joins_lineage(
    tmp_path: Path,
) -> None:
    service, _bus = await _service(tmp_path)
    active_key = "meeting.finalize:retry-race-child"
    parent = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="test",
            intent_text="failed generation",
            active_key=active_key,
        )
    )
    await service.start_run(parent.run_id)
    await service.fail_run(parent.run_id, error="provider down")

    child = await service.retry_run(parent.run_id, reason="retry wins")
    assert child is not None
    fresh = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="explicit-fresh",
            intent_text="concurrent fresh generation",
            active_key=active_key,
        )
    )

    assert fresh.run_id == child.run_id
    assert child.parent_run_id == parent.run_id
    assert child.active_key == active_key
    parent_events = await service.list_events(parent.run_id)
    assert [event.event_type for event in parent_events].count("workflow.retry_created") == 1


@pytest.mark.unit
async def test_two_instances_race_retry_against_fresh_active_run_atomically(
    tmp_path: Path,
) -> None:
    retry_service, _bus = await _service(tmp_path)
    fresh_service = WorkflowService(retry_service.settings, InMemoryEventBus())
    active_key = "meeting.finalize:true-concurrent-race"
    parent = await retry_service.create_run(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="test",
            intent_text="failed generation before a true concurrent race",
            active_key=active_key,
        )
    )
    await retry_service.start_run(parent.run_id)
    await retry_service.fail_run(parent.run_id, error="provider down")
    barrier = asyncio.Barrier(2)

    async def retry_at_barrier() -> object:
        await barrier.wait()
        return await retry_service.retry_run(parent.run_id, reason="concurrent retry")

    async def create_fresh_at_barrier() -> object:
        await barrier.wait()
        return await fresh_service.create_run(
            WorkflowRunCreate(
                kind="meeting.finalize",
                source="fresh-request",
                intent_text="concurrent fresh generation",
                idempotency_key="concurrent-fresh-generation",
                active_key=active_key,
            )
        )

    retry_result, fresh_result = await asyncio.gather(
        retry_at_barrier(),
        create_fresh_at_barrier(),
        return_exceptions=True,
    )
    assert not isinstance(fresh_result, BaseException)
    runs = await retry_service.list_runs()
    active = [run for run in runs if not run.is_terminal and run.active_key == active_key]
    assert len(active) == 1
    parent_events = await retry_service.list_events(parent.run_id)
    retry_event_count = [event.event_type for event in parent_events].count(
        "workflow.retry_created"
    )
    with sqlite3.connect(retry_service.settings.db_path) as conn:
        retry_outbox_count = conn.execute(
            """SELECT COUNT(*) FROM workflow_outbox
               WHERE aggregate_id = ? AND event_type = 'workflow.event'
                 AND payload_json LIKE '%workflow.retry_created%'""",
            (parent.run_id,),
        ).fetchone()[0]

    if isinstance(retry_result, WorkflowConflictError):
        assert fresh_result.run_id == active[0].run_id  # type: ignore[union-attr]
        assert fresh_result.parent_run_id is None  # type: ignore[union-attr]
        assert retry_event_count == 0
        assert retry_outbox_count == 0
    else:
        assert not isinstance(retry_result, BaseException)
        assert retry_result is not None
        assert retry_result.run_id == fresh_result.run_id  # type: ignore[union-attr]
        assert retry_result.run_id == active[0].run_id
        assert retry_result.parent_run_id == parent.run_id
        assert retry_event_count == 1
        assert retry_outbox_count == 1


@pytest.mark.unit
async def test_retry_child_and_parent_event_roll_back_in_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    run = await service.create_run(
        WorkflowRunCreate(kind="rag.query", source="test", intent_text="atomic retry")
    )
    await service.start_run(run.run_id)
    await service.fail_run(run.run_id, error="boom")
    original_append = service._append_event_tx

    async def crash_on_parent_retry(*args: object, **kwargs: object) -> object:
        event_type = str(args[2])
        if event_type == "workflow.retry_created":
            raise RuntimeError("crash before retry commit")
        return await original_append(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_append_event_tx", crash_on_parent_retry)

    with pytest.raises(RuntimeError, match="crash before retry commit"):
        await service.retry_run(run.run_id)

    assert len(await service.list_runs()) == 1
    assert all(
        event.event_type != "workflow.retry_created"
        for event in await service.list_events(run.run_id)
    )


@pytest.mark.unit
async def test_restore_unfinished_replays_non_terminal_runs(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)
    pending = await service.create_run(
        WorkflowRunCreate(kind="agent.task", source="agent", intent_text="继续任务")
    )
    finished = await service.create_run(
        WorkflowRunCreate(kind="artifact.generate", source="artifact_api", intent_text="完成任务")
    )
    await service.start_run(finished.run_id)
    await service.complete_run(finished.run_id)
    before = bus.max_seq

    restored = await service.restore_unfinished()

    assert restored == 1
    assert bus.max_seq > before
    events = await service.list_events(pending.run_id)
    assert events[-1].event_type == "workflow.restored"


@pytest.mark.unit
async def test_workflow_runs_events_and_mutations_are_principal_scoped(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token_a = bind_principal(principal_a)
    try:
        run = await service.create_run(
            WorkflowRunCreate(kind="meeting.finalize", source="meeting", intent_text="A secret")
        )
        await service.start_run(run.run_id)
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        assert await service.get_run(run.run_id) is None
        assert await service.list_runs() == []
        assert await service.list_events(run.run_id) == []
        assert await service.complete_run(run.run_id, output={"stolen": True}) is None
        assert await service.request_cancel(run.run_id) is None
    finally:
        reset_principal(token_b)

    token_a = bind_principal(principal_a)
    try:
        owned = await service.get_run(run.run_id)
        assert owned is not None
        assert owned.state == "running"
        assert [event.event_type for event in await service.list_events(run.run_id)] == [
            "workflow.created",
            "workflow.started",
        ]
    finally:
        reset_principal(token_a)


@pytest.mark.unit
async def test_workflow_rejects_illegal_transition_and_tracks_revision(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    run = await service.create_run(
        WorkflowRunCreate(kind="meeting.finalize", source="meeting", intent_text="finalize")
    )
    assert run.revision == 0
    with pytest.raises(InvalidWorkflowTransition):
        await service.complete_run(run.run_id)
    unchanged = await service.get_run(run.run_id)
    assert unchanged is not None
    assert unchanged.state == "pending"
    assert unchanged.revision == 0

    started = await service.start_run(run.run_id)
    assert started is not None
    assert started.state == "running"
    assert started.revision == 1
    done = await service.complete_run(run.run_id)
    assert done is not None
    assert done.state == "succeeded"
    assert done.revision == 2
    with pytest.raises(InvalidWorkflowTransition):
        await service.request_cancel(run.run_id)


@pytest.mark.unit
async def test_workflow_idempotency_survives_terminal_run_and_retry_links_parent(
    tmp_path: Path,
) -> None:
    service, _bus = await _service(tmp_path)
    body = WorkflowRunCreate(
        kind="rag.ingest",
        source="upload",
        intent_text="same upload",
        idempotency_key="upload:sha256:test",
        timeout_s=30,
    )
    first = await service.create_run(body)
    duplicate = await service.create_run(body)
    assert duplicate.run_id == first.run_id
    assert first.deadline_at is not None

    await service.start_run(first.run_id)
    await service.fail_run(first.run_id, error="boom")
    lost_response_retry = await service.create_run(body)
    assert lost_response_retry.run_id == first.run_id
    assert len(await service.list_runs()) == 1
    retry = await service.retry_run(first.run_id, reason="user_retry")
    assert retry is not None
    assert retry.run_id != first.run_id
    assert retry.parent_run_id == first.run_id
    assert retry.attempt == 2
    assert retry.input["retry_of"] == first.run_id


@pytest.mark.unit
async def test_workflow_retry_rejects_running_and_succeeded_runs(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    running = await service.create_run(
        WorkflowRunCreate(kind="rag.query", source="test", intent_text="running")
    )
    await service.start_run(running.run_id)
    with pytest.raises(InvalidWorkflowTransition, match="non-terminal"):
        await service.retry_run(running.run_id)
    await service.complete_run(running.run_id)
    with pytest.raises(InvalidWorkflowTransition, match="succeeded"):
        await service.retry_run(running.run_id)


@pytest.mark.unit
async def test_workflow_idempotency_is_atomic_under_concurrent_requests(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    body = WorkflowRunCreate(
        kind="workspace.scan",
        source="test",
        intent_text="one logical scan",
        active_key="workspace:scan:owner",
    )

    runs = await asyncio.gather(*(service.create_run(body) for _ in range(12)))

    assert len({run.run_id for run in runs}) == 1
    assert len(await service.list_runs()) == 1
    assert [event.event_type for event in await service.list_events(runs[0].run_id)] == [
        "workflow.created"
    ]
    await service.start_run(runs[0].run_id)
    await service.complete_run(runs[0].run_id)
    next_scan = await service.create_run(body)
    assert next_scan.run_id != runs[0].run_id


@pytest.mark.unit
async def test_workflow_transaction_rolls_back_run_when_event_insert_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)

    async def crash_before_commit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("crash before event/outbox")

    monkeypatch.setattr(service, "_append_event_tx", crash_before_commit)
    with pytest.raises(RuntimeError, match="crash before"):
        await service.create_run(
            WorkflowRunCreate(kind="artifact.generate", source="test", intent_text="atomic")
        )

    with sqlite3.connect(service.settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0


@pytest.mark.unit
@pytest.mark.parametrize("terminal_state", ["cancelled", "timeout"])
async def test_atomic_finalize_start_prevents_terminal_run_from_reviving_meeting(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    service, _bus = await _service(tmp_path)
    meeting_id = f"meeting-{terminal_state}"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'in_meeting', '2026-07-12T00:00:00+00:00',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id,),
        )
        await conn.commit()

    async def mark_generation_started(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            """UPDATE meetings
               SET state = 'ended', ended_at = '2026-07-12T00:01:00+00:00',
                   minutes_status = 'generating'
               WHERE id = ? AND tenant_id = 'legacy-local' AND owner_id = 'legacy-local'""",
            (meeting_id,),
        )

    run = await service.create_run_atomic(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="meeting_state",
            intent_text=f"Finalize {meeting_id}",
            meeting_id=meeting_id,
        ),
        domain_writer=mark_generation_started,
    )
    if terminal_state == "cancelled":
        await service.request_cancel(run.run_id)
        terminal = await service.mark_cancelled(run.run_id)
    else:
        terminal = await service.timeout_run(run.run_id)

    assert terminal is not None and terminal.state == terminal_state
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                "SELECT state, minutes_status FROM meetings WHERE id = ?",
                (meeting_id,),
            )
        ).fetchone()
    assert row == ("ended", "generating")


@pytest.mark.unit
async def test_atomic_finalize_start_rolls_back_run_event_and_meeting_on_crash(
    tmp_path: Path,
) -> None:
    service, _bus = await _service(tmp_path)
    meeting_id = "meeting-start-crash"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'in_meeting', '2026-07-12T00:00:00+00:00',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id,),
        )
        await conn.commit()

    async def crash_after_marker(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE meetings SET state = 'ended' WHERE id = ?",
            (meeting_id,),
        )
        raise RuntimeError("crash before workflow/domain commit")

    with pytest.raises(RuntimeError, match="crash before workflow/domain commit"):
        await service.create_run_atomic(
            WorkflowRunCreate(
                kind="meeting.finalize",
                source="meeting_state",
                intent_text="atomic crash",
                meeting_id=meeting_id,
            ),
            domain_writer=crash_after_marker,
        )

    assert await service.list_runs() == []
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute("SELECT state FROM meetings WHERE id = ?", (meeting_id,))
        ).fetchone()
    assert row == ("in_meeting",)


@pytest.mark.unit
async def test_atomic_finalize_marker_survives_post_commit_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    meeting_id = "meeting-start-flush-failure"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'in_meeting', '2026-07-13T00:00:00+00:00',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id,),
        )
        await conn.commit()

    async def write_marker(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE meetings SET minutes_status = 'generating' WHERE id = ?",
            (meeting_id,),
        )

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated create atomic publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    run = await service.create_run_atomic(
        WorkflowRunCreate(
            kind="meeting.finalize",
            source="test",
            intent_text="atomic marker survives publish failure",
            meeting_id=meeting_id,
        ),
        domain_writer=write_marker,
    )

    assert run.state == "pending"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        marker = await (
            await conn.execute("SELECT minutes_status FROM meetings WHERE id = ?", (meeting_id,))
        ).fetchone()
    assert marker == ("generating",)
    assert [event.event_type for event in await service.list_events(run.run_id)] == [
        "workflow.created"
    ]


@pytest.mark.unit
async def test_domain_write_and_terminal_outbox_roll_back_together_on_crash(tmp_path: Path) -> None:
    service, _bus = await _service(tmp_path)
    run = await service.create_run(
        WorkflowRunCreate(kind="artifact.generate", source="test", intent_text="atomic domain")
    )
    await service.start_run(run.run_id)

    async def crash_after_domain_write(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, file_path, mime_type, created_at, updated_at)
               VALUES ('half-artifact', 'txt', '/tmp/half.txt', 'text/plain', 'now', 'now')"""
        )
        raise RuntimeError("power loss after domain write")

    with pytest.raises(RuntimeError, match="power loss"):
        await service.complete_run_atomic(
            run.run_id,
            output={"artifact_id": "half-artifact"},
            domain_writer=crash_after_domain_write,
            domain_events=[
                EchoEvent(type="artifact.ready", payload={"artifact_id": "half-artifact"})
            ],
        )

    current = await service.get_run(run.run_id)
    assert current is not None and current.state == "running"
    assert [event.event_type for event in await service.list_events(run.run_id)] == [
        "workflow.created",
        "workflow.started",
    ]
    with sqlite3.connect(service.settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_outbox WHERE event_type = 'artifact.ready'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.unit
async def test_active_progress_commit_survives_eager_outbox_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    meeting_id = "meeting-progress-flush-failure"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'ended', '2026-07-13T00:00:00+00:00',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id,),
        )
        await conn.commit()
    run = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.outputs.clear",
            source="test",
            intent_text="commit before eager publish",
            meeting_id=meeting_id,
        )
    )
    await service.start_run(run.run_id)

    async def write_domain(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE meetings SET rag_projection_generation = 1 WHERE id = ?",
            (meeting_id,),
        )

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated post-commit publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    committed = await service.commit_run_progress_atomic(
        run.run_id,
        output={"domain_commit": {"kind": "meeting.outputs.clear", "generation": 1}},
        domain_writer=write_domain,
        domain_events=[],
    )

    assert committed is not None and committed.state == "running"
    assert committed.output["domain_commit"]["generation"] == 1
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        meeting_row = await (
            await conn.execute(
                "SELECT rag_projection_generation FROM meetings WHERE id = ?",
                (meeting_id,),
            )
        ).fetchone()
        unpublished = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL")
        ).fetchone()
    assert meeting_row == (1,)
    assert unpublished is not None and int(unpublished[0]) > 0


@pytest.mark.unit
async def test_inline_completion_returns_committed_run_when_eager_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    meeting_id = "meeting-inline-flush-failure"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'ended', '2026-07-13T00:00:00+00:00',
                       'legacy-local', 'legacy-local', 'legacy-local')""",
            (meeting_id,),
        )
        await conn.commit()

    async def write_domain(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE meetings SET title = 'inline committed' WHERE id = ?",
            (meeting_id,),
        )

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated inline post-commit publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    done = await service.complete_new_run_atomic(
        WorkflowRunCreate(
            kind="share.prepare",
            source="test",
            intent_text="inline commit before eager publish",
            meeting_id=meeting_id,
        ),
        output={"resource_type": "meeting", "resource_id": meeting_id},
        domain_writer=write_domain,
    )

    assert done.state == "succeeded"
    assert [event.event_type for event in await service.list_events(done.run_id)] == [
        "workflow.created",
        "workflow.succeeded",
    ]
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        meeting_row = await (
            await conn.execute("SELECT title FROM meetings WHERE id = ?", (meeting_id,))
        ).fetchone()
    assert meeting_row == ("inline committed",)


@pytest.mark.unit
async def test_merge_output_returns_committed_patch_when_eager_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    run = await service.create_run(
        WorkflowRunCreate(
            kind="meeting.outputs.clear",
            source="test",
            intent_text="persist tail marker before eager publish",
        )
    )
    await service.start_run(run.run_id)

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated tail marker publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    updated = await service.merge_output(
        run.run_id,
        {"post_commit_complete": True},
        event_type="workflow.tail_committed",
    )

    assert updated is not None and updated.state == "running"
    assert updated.output["post_commit_complete"] is True
    persisted = await service.get_run(run.run_id)
    assert persisted is not None and persisted.output["post_commit_complete"] is True
    assert [event.event_type for event in await service.list_events(run.run_id)][-1] == (
        "workflow.tail_committed"
    )


@pytest.mark.unit
async def test_record_event_and_retry_survive_post_commit_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bus = await _service(tmp_path)
    parent = await service.create_run(
        WorkflowRunCreate(kind="artifact.generate", source="test", intent_text="retry durable")
    )
    await service.start_run(parent.run_id)
    await service.fail_run(parent.run_id, error="provider down")

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated event/retry publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    restored = await service.record_event(
        parent.run_id,
        "workflow.restored",
        message="durable before eager publish",
    )
    retry = await service.retry_run(parent.run_id, reason="post-commit publish failed")

    assert restored is not None and restored.event_type == "workflow.restored"
    assert retry is not None and retry.parent_run_id == parent.run_id
    assert retry.state == "pending"
    parent_events = [event.event_type for event in await service.list_events(parent.run_id)]
    assert parent_events[-2:] == ["workflow.restored", "workflow.retry_created"]
    assert [event.event_type for event in await service.list_events(retry.run_id)] == [
        "workflow.created"
    ]


@pytest.mark.unit
async def test_committed_outbox_replays_after_publish_crash_and_restart(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)

    async def fail_publish(_scope: object, _event: object) -> None:
        raise RuntimeError("bus unavailable")

    bus.publish_to = fail_publish  # type: ignore[method-assign]
    run = await service.create_run(
        WorkflowRunCreate(kind="rag.ingest", source="test", intent_text="recover outbox")
    )
    assert await service.get_run(run.run_id) is not None
    with sqlite3.connect(service.settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL"
            ).fetchone()[0]
            == 2
        )

    healthy_bus = InMemoryEventBus()
    restarted = WorkflowService(service.settings, healthy_bus)
    assert await restarted.flush_outbox() == 2
    assert healthy_bus.max_seq == 2
    with sqlite3.connect(service.settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.unit
async def test_background_outbox_routes_from_row_not_active_context(tmp_path: Path) -> None:
    service, failed_bus = await _service(tmp_path)
    target = Principal("tenant-target", "device-target", "owner-target", "s-target", "public")
    wrong = Principal("tenant-wrong", "device-wrong", "owner-wrong", "s-wrong", "public")

    async def fail_publish(_scope: object, _event: object) -> None:
        raise RuntimeError("defer until background poll")

    failed_bus.publish_to = fail_publish  # type: ignore[method-assign]
    target_token = bind_principal(target)
    try:
        await service.create_run(
            WorkflowRunCreate(kind="rag.query", source="test", intent_text="scoped outbox")
        )
    finally:
        reset_principal(target_token)

    healthy_bus = InMemoryEventBus()
    restarted = WorkflowService(service.settings, healthy_bus)
    wrong_token = bind_principal(wrong)
    try:
        assert await restarted.flush_outbox() == 2
        assert healthy_bus.max_seq == 0
    finally:
        reset_principal(wrong_token)

    target_token = bind_principal(target)
    try:
        stream = healthy_bus.subscribe(since_seq=0)
        received = [await anext(stream), await anext(stream)]
        await stream.aclose()
    finally:
        reset_principal(target_token)
    assert [event.type for event in received] == ["workflow.event", "workflow.snapshot"]


@pytest.mark.unit
async def test_startup_outbox_drain_has_no_500_row_ceiling(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('legacy-local', 'legacy-local', 'legacy-local',
                       'domain', ?, 'error', '{"payload": {}}', 'now')""",
            [(f"event-{index}",) for index in range(501)],
        )
        await conn.commit()

    assert await service.drain_outbox() == 501
    assert bus.max_seq == 501
    async with aiosqlite.connect(service.settings.db_path) as conn:
        row = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL")
        ).fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.unit
async def test_each_backend_instance_consumes_shared_sqlite_outbox(tmp_path: Path) -> None:
    """Instance B must project commits written and already published by instance A."""

    service_a, bus_a = await _service(tmp_path)
    bus_b = InMemoryEventBus()
    service_b = WorkflowService(service_a.settings, bus_b)
    consumer = bus_b.subscribe()
    first_event = asyncio.create_task(anext(consumer))
    await asyncio.sleep(0)
    service_b.start_outbox_poller(interval_s=0.01)
    try:
        run = await service_a.create_run(
            WorkflowRunCreate(kind="rag.ingest", source="instance-a", intent_text="cross instance")
        )
        assert bus_a.max_seq == 2
        assert (await asyncio.wait_for(first_event, timeout=1.0)).type == "workflow.event"
        assert (await asyncio.wait_for(anext(consumer), timeout=1.0)).type == "workflow.snapshot"
        assert bus_b.max_seq == 2
        assert await service_b.get_run(run.run_id) is not None
    finally:
        await consumer.aclose()
        await service_b.aclose()


@pytest.mark.unit
async def test_restarted_instance_replays_committed_outbox_even_when_globally_published(
    tmp_path: Path,
) -> None:
    service, first_bus = await _service(tmp_path)
    await service.create_run(
        WorkflowRunCreate(kind="meeting.finalize", source="first", intent_text="restart replay")
    )
    assert first_bus.max_seq == 2
    with sqlite3.connect(service.settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL"
            ).fetchone()[0]
            == 0
        )

    restarted_bus = InMemoryEventBus()
    restarted = WorkflowService(service.settings, restarted_bus)
    assert await restarted.drain_outbox() == 2
    assert restarted_bus.max_seq == 2


@pytest.mark.unit
async def test_outbox_fanout_marks_slow_consumer_for_reconnect(tmp_path: Path) -> None:
    writer, _bus = await _service(tmp_path)
    await writer.create_run(
        WorkflowRunCreate(kind="rag.ingest", source="writer", intent_text="slow consumer")
    )
    slow_bus = InMemoryEventBus(per_subscriber_queue=1)
    reader = WorkflowService(writer.settings, slow_bus)
    consumer = slow_bus.subscribe()
    first_wait = asyncio.create_task(anext(consumer))
    await asyncio.sleep(0)

    await slow_bus.publish(EchoEvent(type="meeting.started", meeting_id="slow"))
    assert (await first_wait).type == "meeting.started"
    assert await reader.drain_outbox() == 2

    signal = await asyncio.wait_for(anext(consumer), timeout=1.0)
    assert signal.type == "error"
    assert signal.payload["reason"] == "slow_consumer"
    assert signal.payload["reconnect"] is True
    assert signal.payload["close_code"] == 4409
    assert signal.payload["fence_seq"] == 3
    with pytest.raises(SlowConsumerError):
        await anext(consumer)
    await consumer.aclose()


@pytest.mark.unit
async def test_new_consumer_replays_only_configured_window_from_large_history(
    tmp_path: Path,
) -> None:
    service, bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=500,
        workflow_outbox_max_rows=20_000,
        workflow_outbox_retention_s=1_000_000_000,
    )
    await _insert_outbox_rows(service.settings.db_path, 10_000)

    assert await service.drain_outbox() == 500
    assert bus.max_seq == 500
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT cursor_outbox_id FROM workflow_outbox_consumers
                   WHERE consumer_id = ?""",
                (service._outbox_consumer_id,),
            )
        ).fetchone()
    assert row is not None and row[0] == 10_000
    await service.aclose()


@pytest.mark.unit
async def test_old_unpublished_crash_row_is_recovered_outside_recent_window(
    tmp_path: Path,
) -> None:
    service, bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=10,
        workflow_outbox_max_rows=5_000,
        workflow_outbox_retention_s=1_000_000_000,
    )
    await _insert_outbox_rows(
        service.settings.db_path,
        1_001,
        unpublished_indexes={0},
    )

    assert await service.drain_outbox() == 11
    stream = bus.subscribe(since_seq=0)
    received = [await anext(stream) for _ in range(11)]
    await stream.aclose()
    assert {event.payload["index"] for event in received} == {
        0,
        *range(991, 1_001),
    }
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                "SELECT published_at FROM workflow_outbox WHERE aggregate_id = 'row-0'"
            )
        ).fetchone()
    assert row is not None and row[0] is not None
    await service.aclose()


@pytest.mark.unit
async def test_active_slow_consumer_blocks_prune_until_its_ttl_expires(tmp_path: Path) -> None:
    writer, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=10,
        workflow_outbox_max_rows=10,
        workflow_outbox_retention_s=1.0,
        workflow_outbox_consumer_ttl_s=60.0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    await _insert_outbox_rows(writer.settings.db_path, 100)
    slow = WorkflowService(writer.settings, InMemoryEventBus(), consumer_id="slow-instance")
    assert await slow.flush_outbox(limit=0) == 0
    await _insert_outbox_rows(writer.settings.db_path, 20, start_index=100)

    fast = WorkflowService(writer.settings, InMemoryEventBus(), consumer_id="fast-instance")
    assert await fast.drain_outbox() == 10
    await fast.prune_outbox()
    async with aiosqlite.connect(str(writer.settings.db_path)) as conn:
        protected = await (
            await conn.execute("SELECT 1 FROM workflow_outbox WHERE outbox_id = 101")
        ).fetchone()
        await conn.execute(
            """UPDATE workflow_outbox_consumers SET heartbeat_at = 0
               WHERE consumer_id = 'slow-instance'"""
        )
        await conn.commit()
    assert protected is not None

    await fast.prune_outbox()
    async with aiosqlite.connect(str(writer.settings.db_path)) as conn:
        count = await (await conn.execute("SELECT COUNT(*) FROM workflow_outbox")).fetchone()
        expired = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumers
                   WHERE consumer_id = 'slow-instance'"""
            )
        ).fetchone()
    assert count is not None and count[0] == 10
    assert expired is not None and expired[0] == 0
    await asyncio.gather(slow.aclose(), fast.aclose(), writer.aclose())


@pytest.mark.unit
async def test_prune_bounds_published_rows_but_never_deletes_unpublished(
    tmp_path: Path,
) -> None:
    service, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=5,
        workflow_outbox_max_rows=50,
        workflow_outbox_retention_s=1_000_000_000,
    )
    await _insert_outbox_rows(
        service.settings.db_path,
        1_003,
        unpublished_indexes={0, 1, 2},
    )

    assert await service.prune_outbox() == 950
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT COUNT(*),
                          SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END)
                   FROM workflow_outbox"""
            )
        ).fetchone()
    assert row == (53, 3)
    await service.aclose()


@pytest.mark.unit
async def test_persisted_consumer_cursor_resumes_after_crash_without_window_replay(
    tmp_path: Path,
) -> None:
    first, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=3,
        workflow_outbox_max_rows=1_000,
        workflow_outbox_retention_s=1_000_000_000,
    )
    first = WorkflowService(first.settings, InMemoryEventBus(), consumer_id="stable-consumer")
    await _insert_outbox_rows(first.settings.db_path, 5)
    assert await first.drain_outbox() == 3

    # Simulate a process crash: do not call first.aclose(), so its durable cursor remains.
    await _insert_outbox_rows(first.settings.db_path, 2, start_index=5)
    restarted_bus = InMemoryEventBus()
    restarted = WorkflowService(
        first.settings,
        restarted_bus,
        consumer_id="stable-consumer",
    )
    assert await restarted.drain_outbox() == 2
    assert restarted_bus.max_seq == 2

    await restarted.aclose()
    async with aiosqlite.connect(str(first.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumers
                   WHERE consumer_id = 'stable-consumer'"""
            )
        ).fetchone()
    assert row is not None and row[0] == 0
    await first.aclose()


@pytest.mark.unit
async def test_outbox_scope_failure_does_not_block_other_scope_and_recovers_in_order(
    tmp_path: Path,
) -> None:
    service, _unused_bus = await _service(tmp_path)
    bus = InMemoryEventBus(
        max_scope_streams=1,
        per_subscriber_queue=8,
        replay_buffer=8,
    )
    service = WorkflowService(service.settings, bus, consumer_id="scope-lanes")
    scope_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    scope_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token = bind_principal(scope_a)
    try:
        subscription_a = await bus.open_fenced_subscription(last_seq=0, stream_epoch=None)
    finally:
        reset_principal(token)

    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES (?, ?, ?, 'domain', ?, 'workflow.snapshot', ?, 'now')""",
            [
                (
                    "tenant-b",
                    "device-b",
                    "owner-b",
                    "b-1",
                    json.dumps({"payload": {"label": "b-1"}}),
                ),
                (
                    "tenant-b",
                    "device-b",
                    "owner-b",
                    "b-2",
                    json.dumps({"payload": {"label": "b-2"}}),
                ),
                (
                    "tenant-a",
                    "device-a",
                    "owner-a",
                    "a-1",
                    json.dumps({"payload": {"label": "a-1"}}),
                ),
            ],
        )
        await conn.commit()

    assert await service.flush_outbox(limit=10) == 1
    delivered_a = await asyncio.wait_for(anext(subscription_a), timeout=1.0)
    assert delivered_a.payload["label"] == "a-1"
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT outbox.aggregate_id, recovery.attempts,
                          recovery.next_retry_at
                   FROM workflow_outbox_consumer_scope_recovery AS recovery
                   JOIN workflow_outbox AS outbox
                     ON outbox.outbox_id = recovery.next_outbox_id
                   WHERE recovery.consumer_id = 'scope-lanes'
                   ORDER BY recovery.next_outbox_id"""
            )
        ).fetchall()
    assert [row[0] for row in rows] == ["b-1"]
    assert rows[0][1] == 1 and rows[0][2] > 0

    # A retry before the durable deadline must neither spin nor overtake b-1.
    assert await service.flush_outbox(limit=10) == 0
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        attempts = await (
            await conn.execute(
                """SELECT attempts FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = 'scope-lanes' ORDER BY next_outbox_id"""
            )
        ).fetchall()
    assert [row[0] for row in attempts] == [1]

    await subscription_a.aclose()
    token = bind_principal(scope_b)
    try:
        subscription_b = await bus.open_fenced_subscription(last_seq=0, stream_epoch=None)
    finally:
        reset_principal(token)
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """UPDATE workflow_outbox_consumer_scope_recovery SET next_retry_at = 0
               WHERE consumer_id = 'scope-lanes'"""
        )
        await conn.commit()

    assert await service.drain_outbox(batch_size=10) == 2
    delivered_b = [
        await asyncio.wait_for(anext(subscription_b), timeout=1.0),
        await asyncio.wait_for(anext(subscription_b), timeout=1.0),
    ]
    assert [event.payload["label"] for event in delivered_b] == ["b-1", "b-2"]
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        remaining = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = 'scope-lanes'"""
            )
        ).fetchone()
    assert remaining is not None and remaining[0] == 0
    await subscription_b.aclose()
    await service.aclose()


@pytest.mark.unit
async def test_outbox_recovery_backoff_survives_consumer_restart(tmp_path: Path) -> None:
    service, failed_bus = await _service(tmp_path)
    service = WorkflowService(service.settings, failed_bus, consumer_id="stable-retry")
    publish_calls = 0

    async def fail_publish(_scope: object, _event: object) -> None:
        nonlocal publish_calls
        publish_calls += 1
        raise RuntimeError("temporary event bus failure")

    failed_bus.publish_to = fail_publish  # type: ignore[method-assign]
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('legacy-local', 'legacy-local', 'legacy-local',
                       'domain', 'retry-me', 'workflow.snapshot',
                       '{"payload":{"label":"retry-me"}}', 'now')"""
        )
        await conn.commit()

    assert await service.flush_outbox() == 0
    assert publish_calls == 1

    # Simulate a crash by constructing the same durable consumer without
    # calling aclose() on the first object (which would intentionally unregister).
    healthy_bus = InMemoryEventBus()
    restarted = WorkflowService(service.settings, healthy_bus, consumer_id="stable-retry")
    assert await restarted.flush_outbox() == 0
    assert healthy_bus.max_seq == 0
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT attempts, next_retry_at
                   FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = 'stable-retry'"""
            )
        ).fetchone()
        assert row is not None and row[0] == 1 and row[1] > 0
        await conn.execute(
            """UPDATE workflow_outbox_consumer_scope_recovery SET next_retry_at = 0
               WHERE consumer_id = 'stable-retry'"""
        )
        await conn.commit()

    assert await restarted.flush_outbox() == 1
    assert healthy_bus.max_seq == 1
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        remaining = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = 'stable-retry'"""
            )
        ).fetchone()
    assert remaining is not None and remaining[0] == 0
    await restarted.aclose()


@pytest.mark.unit
async def test_compact_scope_lane_ignores_row_cap_and_survives_restart_and_prune(
    tmp_path: Path,
) -> None:
    """A1/A2/A3 cannot consume the cap or prevent healthy B1 delivery."""

    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=1,
        workflow_outbox_max_rows=2,
        workflow_outbox_retention_s=1.0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    failing_bus = InMemoryEventBus()
    delivered: list[str] = []

    async def fail_only_scope_a(scope: tuple[str, str], event: EchoEvent) -> None:
        if scope == ("tenant-a", "owner-a"):
            raise RuntimeError("scope A unavailable")
        delivered.append(str(event.payload["label"]))

    failing_bus.publish_to = fail_only_scope_a  # type: ignore[method-assign]
    service = WorkflowService(seed.settings, failing_bus, consumer_id="compact-cap")
    # Register at an empty cursor first so A1/A2/A3 exercise the runtime compact
    # lane rather than the exact sparse snapshot used for pre-registration rows.
    assert await service.flush_outbox(limit=0) == 0
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES (?, ?, ?, 'domain', ?, 'workflow.snapshot', ?, 'now')""",
            [
                (
                    tenant,
                    f"device-{owner}",
                    owner,
                    label,
                    json.dumps({"payload": {"label": label}}),
                )
                for tenant, owner, label in (
                    ("tenant-a", "owner-a", "a-1"),
                    ("tenant-a", "owner-a", "a-2"),
                    ("tenant-a", "owner-a", "a-3"),
                    ("tenant-b", "owner-b", "b-1"),
                )
            ],
        )
        await conn.commit()

    assert await service.flush_outbox(limit=10) == 1
    assert delivered == ["b-1"]
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        cursor = await (
            await conn.execute(
                """SELECT cursor_outbox_id FROM workflow_outbox_consumers
                   WHERE consumer_id = 'compact-cap'"""
            )
        ).fetchone()
        compact = await (
            await conn.execute(
                """SELECT outbox.aggregate_id, recovery.attempts
                   FROM workflow_outbox_consumer_scope_recovery AS recovery
                   JOIN workflow_outbox AS outbox
                     ON outbox.outbox_id = recovery.next_outbox_id
                   WHERE recovery.consumer_id = 'compact-cap'"""
            )
        ).fetchall()
        legacy = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumer_recovery
                   WHERE consumer_id = 'compact-cap'"""
            )
        ).fetchone()
        # Simulate another process having globally projected A.  This consumer's
        # scope lane must still protect and replay its own missing delivery.
        await conn.execute(
            """UPDATE workflow_outbox SET published_at = '2020-01-01T00:00:00+00:00'
               WHERE tenant_id = 'tenant-a'"""
        )
        await conn.commit()
    assert cursor is not None and cursor[0] == 4
    assert [tuple(row) for row in compact] == [("a-1", 1)]
    assert legacy is not None and legacy[0] == 0

    assert await service.prune_outbox() == 0
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        protected = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE tenant_id = 'tenant-a'")
        ).fetchone()
    assert protected is not None and protected[0] == 3

    recovered: list[str] = []
    healthy_bus = InMemoryEventBus()

    async def collect(_scope: tuple[str, str], event: EchoEvent) -> None:
        recovered.append(str(event.payload["label"]))

    healthy_bus.publish_to = collect  # type: ignore[method-assign]
    restarted = WorkflowService(seed.settings, healthy_bus, consumer_id="compact-cap")
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """UPDATE workflow_outbox_consumer_scope_recovery SET next_retry_at = 0
               WHERE consumer_id = 'compact-cap'"""
        )
        await conn.commit()

    assert await restarted.drain_outbox(batch_size=2) == 3
    assert recovered == ["a-1", "a-2", "a-3"]
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        remaining_lane = await (
            await conn.execute(
                """SELECT COUNT(*) FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = 'compact-cap'"""
            )
        ).fetchone()
    assert remaining_lane is not None and remaining_lane[0] == 0
    # The restarted consumer's first flush may already prune A1/A2 after they
    # are delivered; the explicit pass removes the final now-unprotected row.
    assert await restarted.prune_outbox() >= 1
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        remaining_a = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox WHERE tenant_id = 'tenant-a'")
        ).fetchone()
    assert remaining_a is not None and remaining_a[0] == 0
    await restarted.aclose()


@pytest.mark.unit
async def test_25k_ancient_rows_register_random_consumers_without_event_linear_metadata_or_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=1,
        workflow_outbox_max_rows=2,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    await _insert_outbox_rows(
        seed.settings.db_path,
        25_000,
        unpublished_indexes=set(range(25_000)),
    )

    def footprint() -> int:
        return sum(
            path.stat().st_size
            for path in seed.settings.db_path.parent.glob(f"{seed.settings.db_path.name}*")
        )

    size_before = footprint()
    latched = WorkflowService(seed.settings, InMemoryEventBus(), consumer_id="random-latched")
    original_floor = latched._outbox_replay_floor
    floor_read = asyncio.Event()
    release_registration = asyncio.Event()

    async def paused_floor(conn: aiosqlite.Connection, rows: int) -> int:
        floor = await original_floor(conn, rows)
        floor_read.set()
        await release_registration.wait()
        return floor

    monkeypatch.setattr(latched, "_outbox_replay_floor", paused_floor)
    registration = asyncio.create_task(latched.flush_outbox(limit=0))
    await floor_read.wait()

    async def concurrent_writer() -> None:
        async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=1000")
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                """INSERT INTO workflow_outbox
                   (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                    event_type, payload_json, created_at)
                   VALUES ('writer-tenant', 'writer-device', 'writer-owner',
                           'domain', 'writer-during-registration', 'workflow.snapshot',
                           '{"payload":{"label":"writer"}}', 'now')"""
            )
            await conn.commit()

    try:
        # The replay-floor read is paused, yet no BEGIN IMMEDIATE is held by
        # registration, so an unrelated writer must commit immediately.
        await asyncio.wait_for(concurrent_writer(), timeout=1.0)
    finally:
        release_registration.set()
    assert await registration == 0

    consumers = [
        WorkflowService(seed.settings, InMemoryEventBus(), consumer_id=f"random-{index}")
        for index in range(5)
    ]
    started = time.perf_counter()
    assert await asyncio.gather(*(consumer.flush_outbox(limit=0) for consumer in consumers)) == [
        0
    ] * len(consumers)
    registration_elapsed_s = time.perf_counter() - started
    size_after = footprint()

    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        consumer_count = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumers")
        ).fetchone()
        sparse_count = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumer_recovery")
        ).fetchone()
        compact_count = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumer_scope_recovery")
        ).fetchone()
        global_lane_count = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_global_scope_recovery")
        ).fetchone()
        global_state = await (
            await conn.execute(
                """SELECT recovery_through_outbox_id, scan_cursor_outbox_id
                   FROM workflow_outbox_global_recovery_state"""
            )
        ).fetchone()
    assert consumer_count is not None and consumer_count[0] == 6
    assert sparse_count is not None and sparse_count[0] == 0
    assert compact_count is not None and compact_count[0] == 0
    assert global_lane_count is not None and global_lane_count[0] == 0
    assert global_state is not None and global_state[0] == 25_000 and global_state[1] == 0
    assert registration_elapsed_s < 5.0
    assert size_after - size_before < 1_000_000
    await asyncio.gather(latched.aclose(), *(consumer.aclose() for consumer in consumers))


@pytest.mark.unit
async def test_global_recovery_tolerates_concurrent_published_at_change_and_orders_recent_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=1,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', ?,
                       'workflow.snapshot', ?, 'now')""",
            [
                ("ancient-1", json.dumps({"payload": {"label": "ancient-1"}})),
                ("ancient-2", json.dumps({"payload": {"label": "ancient-2"}})),
                ("recent-3", json.dumps({"payload": {"label": "recent-3"}})),
            ],
        )
        await conn.commit()
    delivered: list[str] = []
    bus = InMemoryEventBus()

    async def collect(_scope: tuple[str, str], event: EchoEvent) -> None:
        delivered.append(str(event.payload["label"]))

    bus.publish_to = collect  # type: ignore[method-assign]
    service = WorkflowService(seed.settings, bus, consumer_id="published-race")
    assert await service.flush_outbox(limit=0) == 0
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.execute(
            """UPDATE workflow_outbox SET published_at = '2020-01-01T00:00:00+00:00'
               WHERE aggregate_id = 'ancient-1'"""
        )
        await conn.commit()

    original_publish = service._publish_outbox_row
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    async def paused_publish(row: aiosqlite.Row) -> None:
        body = json.loads(str(row["payload_json"]))
        if body["payload"]["label"] == "ancient-2":
            publish_started.set()
            await release_publish.wait()
        await original_publish(row)

    monkeypatch.setattr(service, "_publish_outbox_row", paused_publish)
    flushing = asyncio.create_task(service.flush_outbox(limit=10))
    await publish_started.wait()
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        # Another consumer wins the global published_at write while this lease
        # holder is outside SQLite publishing to its local bus.
        await conn.execute(
            """UPDATE workflow_outbox SET published_at = '2020-01-01T00:00:01+00:00'
               WHERE aggregate_id = 'ancient-2'"""
        )
        await conn.commit()
    release_publish.set()

    assert await flushing == 2
    assert delivered == ["ancient-2", "recent-3"]
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        state = await (
            await conn.execute(
                """SELECT scan_cursor_outbox_id, recovery_through_outbox_id
                   FROM workflow_outbox_global_recovery_state"""
            )
        ).fetchone()
    assert state is not None and tuple(state) == (2, 2)
    await service.aclose()


@pytest.mark.unit
async def test_fresh_but_stuck_legacy_consumer_cannot_block_global_at_least_once(
    tmp_path: Path,
) -> None:
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', 'old-1',
                       'workflow.snapshot', '{"payload":{"label":"old-1"}}', 'now')"""
        )
        await conn.execute(
            """INSERT INTO workflow_outbox_consumers
               (consumer_id, cursor_outbox_id, started_at, heartbeat_at)
               VALUES ('stuck-legacy', 1, 'now', ?)""",
            (time.time(),),
        )
        await conn.execute(
            """INSERT INTO workflow_outbox_consumer_recovery
               (consumer_id, outbox_id, attempts, next_retry_at, last_error)
               VALUES ('stuck-legacy', 1, 99, 0, 'local bus permanently failed')"""
        )
        await conn.execute(
            """UPDATE workflow_outbox_global_recovery_state
               SET recovery_through_outbox_id = 1, scan_cursor_outbox_id = 0"""
        )
        await conn.commit()

    delivered: list[str] = []
    healthy_bus = InMemoryEventBus()

    async def collect(_scope: tuple[str, str], event: EchoEvent) -> None:
        delivered.append(str(event.payload["label"]))

    healthy_bus.publish_to = collect  # type: ignore[method-assign]
    healthy = WorkflowService(seed.settings, healthy_bus, consumer_id="healthy-global")
    assert await healthy.flush_outbox(limit=10) == 1
    assert delivered == ["old-1"]
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        stuck_consumer = await (
            await conn.execute(
                "SELECT heartbeat_at FROM workflow_outbox_consumers WHERE consumer_id = 'stuck-legacy'"
            )
        ).fetchone()
        sparse_after_global = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumer_recovery")
        ).fetchone()
        published = await (
            await conn.execute(
                "SELECT published_at FROM workflow_outbox WHERE aggregate_id = 'old-1'"
            )
        ).fetchone()
    assert stuck_consumer is not None
    # G's global-at-least-once projection must not erase active C's local
    # delivery pointer: event-only UI state is not fully REST-rehydratable.
    assert sparse_after_global is not None and sparse_after_global[0] == 1
    assert published is not None and published[0] is not None

    recovered_local: list[str] = []
    recovered_bus = InMemoryEventBus()

    async def collect_recovered(_scope: tuple[str, str], event: EchoEvent) -> None:
        recovered_local.append(str(event.payload["label"]))

    recovered_bus.publish_to = collect_recovered  # type: ignore[method-assign]
    recovered_consumer = WorkflowService(
        seed.settings,
        recovered_bus,
        consumer_id="stuck-legacy",
    )
    assert await recovered_consumer.flush_outbox(limit=10) == 1
    assert recovered_local == ["old-1"]
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        sparse_after_local = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumer_recovery")
        ).fetchone()
    assert sparse_after_local is not None and sparse_after_local[0] == 0
    await recovered_consumer.aclose()
    await healthy.aclose()


@pytest.mark.unit
async def test_bad_global_owner_releases_batch_and_healthy_restart_takes_over_in_order(
    tmp_path: Path,
) -> None:
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', ?,
                       'workflow.snapshot', ?, 'now')""",
            [(label, json.dumps({"payload": {"label": label}})) for label in ("a-1", "a-2", "a-3")],
        )
        await conn.commit()

    bad_bus = InMemoryEventBus()

    async def always_fail(_scope: tuple[str, str], _event: EchoEvent) -> None:
        raise RuntimeError("bad process-local bus")

    bad_bus.publish_to = always_fail  # type: ignore[method-assign]
    recovered: list[str] = []
    healthy_bus = InMemoryEventBus()

    async def collect(_scope: tuple[str, str], event: EchoEvent) -> None:
        recovered.append(str(event.payload["label"]))

    healthy_bus.publish_to = collect  # type: ignore[method-assign]
    bad = WorkflowService(seed.settings, bad_bus, consumer_id="stable-global")
    healthy_restart = WorkflowService(seed.settings, healthy_bus, consumer_id="stable-global")
    assert await bad.flush_outbox(limit=0) == 0
    assert await healthy_restart.flush_outbox(limit=0) == 0
    assert await bad.flush_outbox(limit=10) == 0

    for expected in ("a-1", "a-2", "a-3"):
        async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
            await conn.execute("UPDATE workflow_outbox_global_scope_recovery SET next_retry_at = 0")
            await conn.commit()
        # The failed owner is either denied by its cooldown or fails one batch;
        # in both cases finally releases so the healthy peer can acquire.
        assert await bad.flush_outbox(limit=1) == 0
        async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
            await conn.execute("UPDATE workflow_outbox_global_scope_recovery SET next_retry_at = 0")
            lease = await (
                await conn.execute("SELECT lease_owner FROM workflow_outbox_global_recovery_state")
            ).fetchone()
            await conn.commit()
        assert lease is not None and lease[0] is None
        assert await healthy_restart.flush_outbox(limit=1) == 1
        assert recovered[-1] == expected

    assert recovered == ["a-1", "a-2", "a-3"]
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        lane = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_global_scope_recovery")
        ).fetchone()
        lease = await (
            await conn.execute("SELECT lease_owner FROM workflow_outbox_global_recovery_state")
        ).fetchone()
    assert lane is not None and lane[0] == 0
    assert lease is not None and lease[0] is None
    await healthy_restart.aclose()


@pytest.mark.unit
async def test_expired_global_lease_fence_prevents_slow_old_owner_from_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_service_module, "_OUTBOX_GLOBAL_RECOVERY_LEASE_S", 60.0)
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', 'fenced-1',
                       'workflow.snapshot', '{"payload":{"label":"fenced-1"}}', 'now')"""
        )
        await conn.commit()

    slow_bus = InMemoryEventBus()
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    async def slow_publish(_scope: tuple[str, str], _event: EchoEvent) -> None:
        publish_started.set()
        await release_publish.wait()

    slow_bus.publish_to = slow_publish  # type: ignore[method-assign]
    healthy_labels: list[str] = []
    healthy_bus = InMemoryEventBus()

    async def healthy_publish(_scope: tuple[str, str], event: EchoEvent) -> None:
        healthy_labels.append(str(event.payload["label"]))

    healthy_bus.publish_to = healthy_publish  # type: ignore[method-assign]
    slow = WorkflowService(seed.settings, slow_bus, consumer_id="fence-consumer")
    healthy = WorkflowService(seed.settings, healthy_bus, consumer_id="fence-consumer")
    assert await slow.flush_outbox(limit=0) == 0
    assert await healthy.flush_outbox(limit=0) == 0
    slow_flush = asyncio.create_task(slow.flush_outbox(limit=10))
    await publish_started.wait()
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.execute(
            """UPDATE workflow_outbox_global_recovery_state
               SET lease_expires_at = 0
               WHERE singleton = 1 AND lease_owner IS NOT NULL"""
        )
        await conn.commit()

    assert await healthy.flush_outbox(limit=10) == 1
    release_publish.set()
    assert await slow_flush == 0
    assert healthy_labels == ["fenced-1"]
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT published_at, attempts FROM workflow_outbox
                   WHERE aggregate_id = 'fenced-1'"""
            )
        ).fetchone()
        state = await (
            await conn.execute(
                """SELECT scan_cursor_outbox_id, recovery_through_outbox_id,
                          lease_fence, lease_owner
                   FROM workflow_outbox_global_recovery_state"""
            )
        ).fetchone()
    assert row is not None and row[0] is not None and row[1] == 1
    assert state is not None and state[0] == state[1] == 1
    assert state[2] >= 2 and state[3] is None
    await healthy.aclose()


@pytest.mark.unit
async def test_poller_cancellation_rolls_back_open_global_transaction_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _bus = await _service(
        tmp_path,
        workflow_outbox_replay_window_rows=0,
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        await conn.execute(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', 'cancel-open-tx',
                       'workflow.snapshot', '{"payload":{"label":"cancel"}}', 'now')"""
        )
        await conn.commit()

    service = WorkflowService(seed.settings, InMemoryEventBus(), consumer_id="cancel-open-tx")
    mutation_open = asyncio.Event()
    never_release = asyncio.Event()

    async def pause_after_mark(_conn: aiosqlite.Connection, _outbox_id: int) -> None:
        # _mark_global_outbox_published has already opened BEGIN IMMEDIATE and
        # updated the row; cancellation here used to make finally issue a nested
        # BEGIN, mask CancelledError, and leave aclose waiting forever.
        mutation_open.set()
        await never_release.wait()

    monkeypatch.setattr(service, "_advance_global_scan_cursor", pause_after_mark)
    service.start_outbox_poller(interval_s=0.01)
    await asyncio.wait_for(mutation_open.wait(), timeout=1.0)
    await asyncio.wait_for(service.aclose(), timeout=1.0)

    async with aiosqlite.connect(str(seed.settings.db_path)) as conn:
        row = await (
            await conn.execute(
                "SELECT published_at FROM workflow_outbox WHERE aggregate_id = 'cancel-open-tx'"
            )
        ).fetchone()
        state = await (
            await conn.execute(
                "SELECT lease_owner, lease_expires_at FROM workflow_outbox_global_recovery_state"
            )
        ).fetchone()
    assert row is not None and row[0] is None
    assert state is not None and tuple(state) == (None, 0.0)


@pytest.mark.unit
async def test_v35_upgrade_drains_existing_sparse_rows_in_order(tmp_path: Path) -> None:
    db_path = tmp_path / "v34-sparse-upgrade.db"
    through_34 = _migrations_through(tmp_path, 34)
    assert (await run_migrations(db_path, migrations_dir=through_34)).errors == []
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executemany(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES ('tenant-a', 'device-a', 'owner-a', 'domain', ?,
                       'workflow.snapshot', ?, 'now')""",
            [
                ("legacy-1", json.dumps({"payload": {"label": "legacy-1"}})),
                ("legacy-2", json.dumps({"payload": {"label": "legacy-2"}})),
            ],
        )
        await conn.execute(
            """INSERT INTO workflow_outbox_consumers
               (consumer_id, cursor_outbox_id, started_at, heartbeat_at)
               VALUES ('v34-consumer', 2, 'now', ?)""",
            (time.time(),),
        )
        await conn.executemany(
            """INSERT INTO workflow_outbox_consumer_recovery
               (consumer_id, outbox_id, attempts, next_retry_at, last_error)
               VALUES ('v34-consumer', ?, 0, 0, NULL)""",
            [(1,), (2,)],
        )
        await conn.commit()
    upgraded = await run_migrations(db_path)
    assert upgraded.errors == [] and upgraded.applied == [35, 36, 37, 38, 39, 40, 41, 42, 43, 44]

    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage-upgrade",
        skill_executor_build_dir=tmp_path / "skill-upgrade",
        workflow_outbox_cleanup_interval_s=3_600.0,
    )
    labels: list[str] = []
    bus = InMemoryEventBus()

    async def collect(_scope: tuple[str, str], event: EchoEvent) -> None:
        labels.append(str(event.payload["label"]))

    bus.publish_to = collect  # type: ignore[method-assign]
    service = WorkflowService(settings, bus, consumer_id="v34-consumer")
    assert await service.drain_outbox(batch_size=10) == 2
    assert labels == ["legacy-1", "legacy-2"]
    async with aiosqlite.connect(str(db_path)) as conn:
        sparse = await (
            await conn.execute("SELECT COUNT(*) FROM workflow_outbox_consumer_recovery")
        ).fetchone()
        state = await (
            await conn.execute(
                """SELECT scan_cursor_outbox_id, recovery_through_outbox_id
                   FROM workflow_outbox_global_recovery_state"""
            )
        ).fetchone()
    assert sparse is not None and sparse[0] == 0
    assert state is not None and tuple(state) == (2, 2)
    await service.aclose()
