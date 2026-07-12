from __future__ import annotations

import asyncio
import gc
import weakref
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api import deps as deps_mod
from app.config import Settings
from app.main import _bind_workflow_handlers_for_current_principal
from app.schemas.workflow import WorkflowRunCreate
from app.security import Principal
from app.security.context import bind_principal, current_principal, reset_principal
from app.workflows.kernel import (
    WorkflowContext,
    WorkflowDispatcher,
    WorkflowExecutionError,
    WorkflowHandlerRegistry,
)
from app.workflows.service import WorkflowRunRecord, WorkflowService


async def _kernel(tmp_path: Path) -> tuple[WorkflowDispatcher, WorkflowHandlerRegistry]:
    db_path = tmp_path / "kernel.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    service = WorkflowService(
        Settings(db_path=db_path, storage_dir=tmp_path / "storage", _env_file=None),  # type: ignore[call-arg]
        InMemoryEventBus(),
    )
    registry = WorkflowHandlerRegistry()
    return WorkflowDispatcher(service, registry), registry


async def _shared_dispatchers(
    tmp_path: Path,
    *,
    heartbeat_s: float = 60.0,
) -> tuple[
    WorkflowDispatcher,
    WorkflowHandlerRegistry,
    WorkflowDispatcher,
    WorkflowHandlerRegistry,
]:
    db_path = tmp_path / "shared-kernel.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        execution_lease_ttl_s=300.0,
        execution_lease_heartbeat_s=heartbeat_s,
        _env_file=None,  # type: ignore[call-arg]
    )
    first_registry = WorkflowHandlerRegistry()
    second_registry = WorkflowHandlerRegistry()
    first = WorkflowDispatcher(
        WorkflowService(settings, InMemoryEventBus()),
        first_registry,
        worker_id="worker-a",
    )
    second = WorkflowDispatcher(
        WorkflowService(settings, InMemoryEventBus()),
        second_registry,
        worker_id="worker-b",
    )
    return first, first_registry, second, second_registry


async def _wait_for_state(
    service: WorkflowService,
    run_id: str,
    *states: str,
) -> WorkflowRunRecord:
    async with asyncio.timeout(3):
        while True:
            run = await service.get_run(run_id)
            if run is not None and run.state in states:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.unit
async def test_dispatcher_runs_registered_handler_and_dedupes_active_request(
    tmp_path: Path,
) -> None:
    dispatcher, registry = await _kernel(tmp_path)
    release = asyncio.Event()
    calls = 0

    async def handle(_context: WorkflowContext, payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        await release.wait()
        return {"echo": payload["value"]}

    registry.register("test.echo", handle)
    body = WorkflowRunCreate(
        kind="test.echo",
        source="test",
        intent_text="echo",
        input={"value": 42},
        idempotency_key="echo:42",
    )
    first = await dispatcher.dispatch(body)
    duplicate = await dispatcher.dispatch(body)
    assert duplicate.run_id == first.run_id
    release.set()
    done = await dispatcher.wait(first.run_id)
    assert done is not None
    assert done.state == "succeeded"
    assert done.output == {"echo": 42}
    assert calls == 1


@pytest.mark.unit
async def test_execute_returns_only_successful_terminal_record(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)

    async def handle(_context: WorkflowContext, _payload: dict[str, object]) -> dict[str, object]:
        return {"value": 42}

    registry.register("test.execute", handle)
    done = await dispatcher.execute(
        WorkflowRunCreate(kind="test.execute", source="test", intent_text="execute")
    )

    assert done.state == "succeeded"
    assert done.output == {"value": 42}


@pytest.mark.unit
async def test_execute_raises_typed_error_with_durable_failed_record(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)

    async def handle(_context: WorkflowContext, _payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("handler exploded")

    registry.register("test.execute.failure", handle)
    with pytest.raises(WorkflowExecutionError, match="handler exploded") as caught:
        await dispatcher.execute(
            WorkflowRunCreate(
                kind="test.execute.failure",
                source="test",
                intent_text="execute failure",
            )
        )

    assert caught.value.state == "failed"
    assert caught.value.run is not None
    assert caught.value.run.error == "handler exploded"


@pytest.mark.unit
async def test_dispatcher_timeout_is_terminal_and_sets_cancel_event(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)
    cancelled = asyncio.Event()

    async def handle(context: WorkflowContext, _payload: dict[str, object]) -> dict[str, object]:
        try:
            await asyncio.sleep(60)
        finally:
            if context.cancel_event.is_set():
                cancelled.set()
        return {}

    registry.register("test.timeout", handle)
    run = await dispatcher.dispatch(
        WorkflowRunCreate(
            kind="test.timeout",
            source="test",
            intent_text="timeout",
            timeout_s=1,
        )
    )
    done = await dispatcher.wait(run.run_id)
    assert done is not None
    assert done.state == "timeout"
    assert cancelled.is_set()


@pytest.mark.unit
async def test_dispatcher_cancel_stops_live_handler_and_persists_cancelled(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)
    started = asyncio.Event()

    async def handle(context: WorkflowContext, _payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await context.cancel_event.wait()
        return {"should_not_complete": True}

    registry.register("test.cancel", handle)
    run = await dispatcher.dispatch(
        WorkflowRunCreate(kind="test.cancel", source="test", intent_text="cancel")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    cancelled = await dispatcher.cancel(run.run_id, reason="user")
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.cancel_requested_at is not None

    repeated = await dispatcher.cancel(run.run_id, reason="duplicate click")
    assert repeated is not None
    assert repeated.state == "cancelled"
    assert repeated.revision == cancelled.revision


@pytest.mark.unit
async def test_heartbeat_renew_exception_cancels_handler_and_cannot_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher, registry = await _kernel(tmp_path)
    dispatcher.service.settings.execution_lease_heartbeat_s = 0.01
    started = asyncio.Event()
    allow_renew_failure = asyncio.Event()
    renew_failed = asyncio.Event()
    handler_stopped = asyncio.Event()

    async def handle(
        context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            assert context.cancel_event.is_set()
            handler_stopped.set()
            # Even a cancellation-suppressing handler must not be allowed to
            # commit after the heartbeat outcome became uncertain.
            return {"must_not_complete": True}
        raise AssertionError("heartbeat test handler unexpectedly resumed")

    async def fail_renew(_lease: object) -> None:
        await allow_renew_failure.wait()
        renew_failed.set()
        raise RuntimeError("renew database unavailable")

    monkeypatch.setattr(dispatcher.service, "renew_run_lease", fail_renew)
    registry.register("test.heartbeat-failure", handle)
    caplog.set_level("WARNING", logger="echodesk.workflow.kernel")
    run = await dispatcher.dispatch(
        WorkflowRunCreate(
            kind="test.heartbeat-failure",
            source="test",
            intent_text="heartbeat failure",
        )
    )
    execution_task = dispatcher._tasks[run.run_id]
    await asyncio.wait_for(started.wait(), timeout=1)
    allow_renew_failure.set()
    await asyncio.wait_for(renew_failed.wait(), timeout=1)
    await asyncio.wait_for(handler_stopped.wait(), timeout=1)
    await asyncio.wait_for(
        asyncio.gather(execution_task, return_exceptions=True),
        timeout=1,
    )

    current = await dispatcher.service.get_run(run.run_id)
    assert current is not None
    assert current.state == "running"
    assert current.output == {}
    assert "renew database unavailable" in caplog.text
    await dispatcher.aclose()


@pytest.mark.unit
async def test_dispatcher_fails_unknown_handler_instead_of_leaving_pending(tmp_path: Path) -> None:
    dispatcher, _registry = await _kernel(tmp_path)
    run = await dispatcher.dispatch(
        WorkflowRunCreate(kind="unknown", source="test", intent_text="unknown")
    )
    done = await dispatcher.wait(run.run_id)
    assert done is not None
    assert done.state == "failed"
    assert done.error == "workflow handler not registered: unknown"


@pytest.mark.unit
def test_handler_registry_prefers_owner_scoped_handler() -> None:
    registry = WorkflowHandlerRegistry()

    async def global_handler(
        _context: WorkflowContext, _payload: dict[str, object]
    ) -> dict[str, object]:
        return {"scope": "global"}

    async def owner_handler(
        _context: WorkflowContext, _payload: dict[str, object]
    ) -> dict[str, object]:
        return {"scope": "owner-a"}

    registry.register("meeting.finalize", global_handler)
    registry.register(
        "meeting.finalize",
        owner_handler,
        scope=("tenant-a", "owner-a"),
    )
    assert registry.resolve("meeting.finalize", ("tenant-a", "owner-a")) is owner_handler
    assert registry.resolve("meeting.finalize", ("tenant-b", "owner-b")) is global_handler


@pytest.mark.unit
def test_handler_registry_bounds_scopes_and_releases_evicted_closures() -> None:
    registry = WorkflowHandlerRegistry(max_scopes=2)

    class CapturedPipeline:
        pass

    refs: list[weakref.ReferenceType[CapturedPipeline]] = []
    for index in range(10):
        pipeline = CapturedPipeline()
        refs.append(weakref.ref(pipeline))

        async def handler(
            _context: WorkflowContext,
            _payload: dict[str, object],
            captured: CapturedPipeline = pipeline,
        ) -> dict[str, object]:
            return {"pipeline": id(captured)}

        registry.register(
            "meeting.finalize",
            handler,
            scope=("tenant", f"owner-{index}"),
            replace=True,
        )
    del handler, pipeline
    gc.collect()

    assert registry.scope_count == 2
    assert registry.handler_count == 2
    assert sum(ref() is not None for ref in refs) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduled_scoped_handler_survives_eviction_and_holds_runtime_lease(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scoped-handler.db"
    assert (await run_migrations(db_path)).errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        runtime_scope_max_entries=1,
        _env_file=None,  # type: ignore[call-arg]
    )
    registry = WorkflowHandlerRegistry(max_scopes=1)
    acquired: list[tuple[str, str]] = []
    released: list[tuple[str, str]] = []

    class Lease:
        def __init__(self, scope: tuple[str, str]) -> None:
            self.scope = scope

        def release(self) -> None:
            released.append(self.scope)

    def acquire(scope: tuple[str, str]) -> Lease:
        acquired.append(scope)
        return Lease(scope)

    dispatcher = WorkflowDispatcher(
        WorkflowService(settings, InMemoryEventBus()),
        registry,
        scope_lease_factory=acquire,
    )
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler_a(
        _context: WorkflowContext, _payload: dict[str, object]
    ) -> dict[str, object]:
        started.set()
        await finish.wait()
        return {"scope": "a"}

    async def handler_b(
        _context: WorkflowContext, _payload: dict[str, object]
    ) -> dict[str, object]:
        return {"scope": "b"}

    principal_a = Principal("tenant", "device-a", "owner-a", "session-a", "public")
    token = bind_principal(principal_a)
    try:
        registry.register("meeting.finalize", handler_a, scope=("tenant", "owner-a"))
        run = await dispatcher.dispatch(
            WorkflowRunCreate(kind="meeting.finalize", source="test", intent_text="a")
        )
    finally:
        reset_principal(token)

    registry.register("meeting.finalize", handler_b, scope=("tenant", "owner-b"))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert acquired == [("tenant", "owner-a")]
    assert released == []
    finish.set()
    token = bind_principal(principal_a)
    try:
        done = await dispatcher.wait_succeeded(run.run_id)
    finally:
        reset_principal(token)
    assert done.output == {"scope": "a"}
    assert released == [("tenant", "owner-a")]
    await dispatcher.aclose()


@pytest.mark.unit
async def test_restore_unfinished_replays_each_principal_in_its_own_scope(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)
    seen: list[tuple[str, str]] = []

    async def handle(_context: WorkflowContext, payload: dict[str, object]) -> dict[str, object]:
        seen.append((str(payload["owner"]), str(payload["value"])))
        return {"owner": payload["owner"]}

    registry.register("test.restore", handle)
    principals = [
        Principal("tenant-a", "device-a", "owner-a", "session-a", "public"),
        Principal("tenant-b", "device-b", "owner-b", "session-b", "public"),
    ]
    run_ids: list[str] = []
    for index, principal in enumerate(principals):
        token = bind_principal(principal)
        try:
            run = await dispatcher.service.create_run(
                WorkflowRunCreate(
                    kind="test.restore",
                    source="test",
                    intent_text="restore",
                    input={"owner": principal.owner_id, "value": index},
                )
            )
            run_ids.append(run.run_id)
        finally:
            reset_principal(token)

    restored_principals = await dispatcher.service.list_unfinished_principals()
    assert {(item.tenant_id, item.owner_id) for item in restored_principals} == {
        ("tenant-a", "owner-a"),
        ("tenant-b", "owner-b"),
    }
    for principal in restored_principals:
        token = bind_principal(principal)
        try:
            assert await dispatcher.restore_unfinished() == 1
        finally:
            reset_principal(token)

    for principal, run_id in zip(principals, run_ids, strict=True):
        token = bind_principal(principal)
        try:
            done = await dispatcher.wait(run_id)
            assert done is not None
            assert done.state == "succeeded"
            assert done.output == {"owner": principal.owner_id}
        finally:
            reset_principal(token)
    assert sorted(seen) == [("owner-a", "0"), ("owner-b", "1")]


@pytest.mark.unit
async def test_restore_uses_original_deadline_instead_of_resetting_timeout(tmp_path: Path) -> None:
    dispatcher, registry = await _kernel(tmp_path)

    async def handle(_context: WorkflowContext, _payload: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.1)
        return {}

    registry.register("test.deadline", handle)
    run = await dispatcher.service.create_run(
        WorkflowRunCreate(
            kind="test.deadline",
            source="test",
            intent_text="deadline",
            timeout_s=0.01,
        )
    )
    await asyncio.sleep(0.02)
    assert await dispatcher.restore_unfinished() == 1
    done = await dispatcher.wait(run.run_id)
    assert done is not None
    assert done.state == "timeout"


@pytest.mark.unit
async def test_restore_has_no_500_run_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _registry = await _kernel(tmp_path)
    async with aiosqlite.connect(dispatcher.service.settings.db_path) as conn:
        await conn.executemany(
            """INSERT INTO workflow_runs
               (run_id, kind, source, state, intent_text, created_at, updated_at)
               VALUES (?, 'test.bulk-restore', 'test', 'pending', 'restore', 'now', 'now')""",
            [(f"run-bulk-{index}",) for index in range(501)],
        )
        await conn.commit()

    scheduled: set[str] = set()

    async def remember(
        run: WorkflowRunRecord,
        *,
        restored: bool = False,
    ) -> bool:
        assert restored is True
        scheduled.add(run.run_id)
        return True

    async def ignore_event(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(dispatcher, "_ensure_scheduled", remember)
    monkeypatch.setattr(dispatcher.service, "record_event", ignore_event)

    assert await dispatcher.restore_unfinished() == 501
    assert len(scheduled) == 501


@pytest.mark.unit
async def test_two_dispatchers_restore_one_run_exactly_once_and_serialize_event_sequences(
    tmp_path: Path,
) -> None:
    first, first_registry, second, second_registry = await _shared_dispatchers(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(
        _context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"worker_calls": calls}

    first_registry.register("test.multi-process", handle)
    second_registry.register("test.multi-process", handle)
    run = await first.service.create_run(
        WorkflowRunCreate(
            kind="test.multi-process",
            source="test",
            intent_text="restore concurrently",
        )
    )

    restored = await asyncio.gather(
        first.restore_unfinished(),
        second.restore_unfinished(),
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert sum(restored) == 1
    assert calls == 1

    release.set()
    done = await _wait_for_state(first.service, run.run_id, "succeeded")
    assert done.output == {"worker_calls": 1}

    # Per-process asyncio locks are insufficient here.  BEGIN IMMEDIATE must
    # serialize sequence allocation across two independent service instances.
    await asyncio.gather(
        *(
            (first.service if index % 2 == 0 else second.service).record_event(
                run.run_id,
                "test.concurrent-event",
                payload={"index": index},
                visibility="debug",
            )
            for index in range(20)
        )
    )
    events = await first.service.list_events(run.run_id)
    sequences = [event.seq for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len([event for event in events if event.event_type == "test.concurrent-event"]) == 20
    await first.aclose()
    await second.aclose()


@pytest.mark.unit
async def test_expired_workflow_is_taken_over_and_stale_fence_cannot_project(
    tmp_path: Path,
) -> None:
    first, first_registry, second, second_registry = await _shared_dispatchers(tmp_path)
    stale_started = asyncio.Event()
    release_stale = asyncio.Event()
    fences: dict[str, int] = {}

    async with aiosqlite.connect(first.service.settings.db_path) as conn:
        await conn.execute("CREATE TABLE stale_projection (value TEXT NOT NULL)")
        await conn.commit()

    async def stale_handler(
        context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        fences["stale"] = context.fence_token
        stale_started.set()
        await release_stale.wait()

        async def write_stale(conn: aiosqlite.Connection) -> None:
            await conn.execute("INSERT INTO stale_projection (value) VALUES ('stale')")

        await first.service.complete_run_atomic(
            context.run_id,
            output={"winner": "stale"},
            domain_writer=write_stale,
            domain_events=[],
        )
        return {"winner": "stale"}

    async def takeover_handler(
        context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        fences["takeover"] = context.fence_token
        return {"winner": "takeover"}

    first_registry.register("test.fenced-takeover", stale_handler)
    second_registry.register("test.fenced-takeover", takeover_handler)
    run = await first.dispatch(
        WorkflowRunCreate(
            kind="test.fenced-takeover",
            source="test",
            intent_text="take over expired execution",
        )
    )
    await asyncio.wait_for(stale_started.wait(), timeout=1)
    await _wait_for_state(first.service, run.run_id, "running")
    stale_task = first._tasks[run.run_id]

    async with aiosqlite.connect(first.service.settings.db_path) as conn:
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0
               WHERE resource_kind = 'workflow' AND resource_id = ?""",
            (run.run_id,),
        )
        await conn.commit()

    assert await second.restore_unfinished() == 1
    done = await _wait_for_state(second.service, run.run_id, "succeeded")
    assert done.output == {"winner": "takeover"}
    assert fences["takeover"] > fences["stale"]

    release_stale.set()
    await asyncio.wait_for(asyncio.gather(stale_task, return_exceptions=True), timeout=1)
    final = await first.service.get_run(run.run_id)
    assert final is not None
    assert final.state == "succeeded"
    assert final.output == {"winner": "takeover"}
    async with aiosqlite.connect(first.service.settings.db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM stale_projection")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
        await cur.close()
    await first.aclose()
    await second.aclose()


@pytest.mark.unit
async def test_live_second_instance_reaps_expired_run_without_restart_or_duplicate_tasks(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    first, first_registry, second, second_registry = await _shared_dispatchers(tmp_path)
    # Simulate worker A becoming unreachable immediately after starting the
    # handler: its heartbeat cannot run before this short term expires.
    first.service.settings.execution_lease_ttl_s = 0.15
    first.service.settings.execution_lease_heartbeat_s = 60.0
    principal = Principal(
        "tenant-reaper",
        "device-reaper",
        "owner-reaper",
        "session-reaper",
        "public",
    )
    stale_started = asyncio.Event()
    release_stale = asyncio.Event()
    takeover_calls = 0
    takeover_scope: tuple[str, str] | None = None
    prepared_scopes: set[tuple[str, str]] = set()

    async with aiosqlite.connect(first.service.settings.db_path) as conn:
        await conn.execute("CREATE TABLE reaper_stale_projection (value TEXT NOT NULL)")
        await conn.commit()

    async def stale_handler(
        context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        stale_started.set()
        await release_stale.wait()

        async def write_stale(conn: aiosqlite.Connection) -> None:
            await conn.execute("INSERT INTO reaper_stale_projection (value) VALUES ('stale')")

        await first.service.complete_run_atomic(
            context.run_id,
            output={"winner": "stale"},
            domain_writer=write_stale,
            domain_events=[],
        )
        return {"winner": "stale"}

    async def takeover_handler(
        _context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal takeover_calls, takeover_scope
        takeover_calls += 1
        active = current_principal()
        takeover_scope = (active.tenant_id, active.owner_id)
        return {"winner": "takeover"}

    first_registry.register("test.periodic-takeover", stale_handler)
    second_registry.register("test.periodic-takeover", takeover_handler)

    def prepare_current_scope() -> None:
        active = current_principal()
        prepared_scopes.add((active.tenant_id, active.owner_id))

    principal_token = bind_principal(principal)
    try:
        run = await first.dispatch(
            WorkflowRunCreate(
                kind="test.periodic-takeover",
                source="test",
                intent_text="periodic takeover",
            )
        )
        await asyncio.wait_for(stale_started.wait(), timeout=1)
        await _wait_for_state(first.service, run.run_id, "running")
    finally:
        reset_principal(principal_token)
    stale_task = first._tasks[run.run_id]

    second.start_recovery_reaper(
        prepare_current_scope=prepare_current_scope,
        interval_s=0.01,
        max_interval_s=0.03,
    )
    reaper_task = second._recovery_reaper_task
    assert reaper_task is not None
    # Starting lifecycle recovery twice must not create two polling loops.
    second.start_recovery_reaper(interval_s=0.01, max_interval_s=0.03)
    assert second._recovery_reaper_task is reaper_task

    principal_token = bind_principal(principal)
    try:
        done = await _wait_for_state(second.service, run.run_id, "succeeded")
    finally:
        reset_principal(principal_token)
    assert done.output == {"winner": "takeover"}
    assert takeover_calls == 1
    assert takeover_scope == (principal.tenant_id, principal.owner_id)
    assert (principal.tenant_id, principal.owner_id) in prepared_scopes

    # Let several fast reaper periods pass: terminal filtering plus the local
    # task map must prevent a second dispatcher invocation.
    await asyncio.sleep(0.1)
    assert takeover_calls == 1
    release_stale.set()
    await asyncio.wait_for(asyncio.gather(stale_task, return_exceptions=True), timeout=1)
    async with aiosqlite.connect(first.service.settings.db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM reaper_stale_projection")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
        await cur.close()

    await second.aclose()
    assert reaper_task.done()
    assert second._recovery_reaper_task is None
    assert second._tasks == {}
    assert second._cancel_events == {}
    assert not any(
        task.get_name() == "workflow-recovery-reaper:worker-b" and not task.done()
        for task in asyncio.all_tasks()
    )
    await first.aclose()


@pytest.mark.unit
async def test_graceful_dispatcher_close_releases_execution_for_restart_recovery(
    tmp_path: Path,
) -> None:
    first, first_registry, second, second_registry = await _shared_dispatchers(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def interrupted_handler(
        context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        started.set()
        try:
            await asyncio.Future()
        finally:
            assert context.cancel_event.is_set()
            stopped.set()
        raise AssertionError("interrupted handler unexpectedly resumed")

    async def recovered_handler(
        _context: WorkflowContext,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        return {"recovered": True}

    first_registry.register("test.restart", interrupted_handler)
    second_registry.register("test.restart", recovered_handler)
    run = await first.dispatch(
        WorkflowRunCreate(kind="test.restart", source="test", intent_text="restart")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await _wait_for_state(first.service, run.run_id, "running")

    await first.aclose()
    await asyncio.wait_for(stopped.wait(), timeout=1)
    interrupted = await first.service.get_run(run.run_id)
    assert interrupted is not None
    assert interrupted.state == "running"

    assert await second.restore_unfinished() == 1
    recovered = await _wait_for_state(second.service, run.run_id, "succeeded")
    assert recovered.output == {"recovered": True}
    await second.aclose()


@pytest.mark.unit
async def test_builtin_workflow_registry_is_an_exact_contract(tmp_path: Path) -> None:
    deps_mod.reset_deps_for_test()
    settings = Settings(
        db_path=tmp_path / "registry.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skill",
        workspace_scan_on_startup=False,
        diarizer_enabled=False,
        tts_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    repository = SQLiteRepository(settings.db_path)
    await repository.init()
    try:
        _bind_workflow_handlers_for_current_principal(settings, repository)
        dispatcher = deps_mod.get_workflow_dispatcher(
            deps_mod.get_workflow_service(settings, deps_mod.get_event_bus())
        )
        assert dispatcher.registry.kinds() == {
            "artifact.generate",
            "diagnostics.export",
            "meeting.export",
            "meeting.finalize",
            "meeting.outputs.clear",
            "rag.delete",
            "rag.ingest",
            "rag.query",
            "share.prepare",
            "workspace.clear",
            "workspace.config.add",
            "workspace.config.remove",
            "workspace.scan",
        }
    finally:
        await repository.aclose()
        deps_mod.reset_deps_for_test()
