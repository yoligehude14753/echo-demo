from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.base import AgentIntent
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentTaskRecord, AgentTaskService
from app.config import Settings
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseOwnershipError
from app.security import Principal
from app.security.context import bind_principal, reset_principal


class _EnabledBackend:
    enabled = True
    base_url = "http://127.0.0.1:9"


async def _settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "agent-bridge.db"
    assert (await run_migrations(db_path)).errors == []
    return Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        agent_os_enabled=False,
    )


def _service(
    settings: Settings,
    *,
    holder_id: str,
    heartbeat: float = 0.02,
    ttl: float = 0.2,
    recovery_interval: float = 0.1,
    retry_base: float = 0.1,
    retry_max: float = 0.5,
) -> AgentTaskService:
    service = AgentTaskService(
        settings,
        InMemoryEventBus(),
        holder_id=holder_id,
        bridge_heartbeat_seconds=heartbeat,
        bridge_lease_ttl_seconds=ttl,
        bridge_recovery_interval_seconds=recovery_interval,
        bridge_retry_base_seconds=retry_base,
        bridge_retry_max_seconds=retry_max,
    )
    service.backend = _EnabledBackend()  # type: ignore[assignment]
    return service


async def _runner_task(service: AgentTaskService, task_id: str = "task-bridge") -> AgentTaskRecord:
    await service.record_permission_required(
        AgentIntent(
            text="run a durable task",
            device_id="legacy-local",
            echo_task_id=task_id,
        ),
        workflow_run_id=None,
    )
    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET runner_task_id = ?, state = 'pending', progress_text = 'queued'
               WHERE tenant_id = 'legacy-local' AND owner_id = 'legacy-local'
                 AND task_id = ?""",
            (f"runner-{task_id}", task_id),
        )
        await conn.commit()
    rec = await service.get_task(task_id)
    assert rec is not None
    return rec


async def _wait_for_bridge_completion(
    service: AgentTaskService,
    task_id: str,
    *,
    timeout_s: float = 1.0,
) -> AgentTaskRecord:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        stored = await service.get_task(task_id)
        if stored is not None and stored.bridge_completed_at is not None:
            return stored
        await asyncio.sleep(0.01)
    raise AssertionError("bridge projection did not complete before timeout")


@pytest.mark.unit
async def test_two_service_instances_start_only_one_bridge_and_close_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    first = _service(settings, holder_id="instance-a")
    second = _service(settings, holder_id="instance-b")
    rec = await _runner_task(first)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    starts = 0

    class _BlockingBridge:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self) -> bool:
            nonlocal starts
            starts += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return False

    monkeypatch.setattr("app.agents.service.EchoTaskStreamBridge", _BlockingBridge)
    first.start_bridge_for_task(rec)
    second.start_bridge_for_task(rec)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert starts == 1

    await asyncio.gather(first.aclose(), second.aclose())
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    stored = await first.get_task(rec.task_id)
    assert stored is not None
    assert stored.state.value == "pending"
    assert stored.bridge_completed_at is None

    probe = ExecutionLeaseStore(settings.db_path)
    token = await probe.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="post-close",
        ttl_seconds=1.0,
    )
    assert token is not None
    assert await probe.release(token) is True


@pytest.mark.unit
async def test_heartbeat_loss_cancels_bridge_without_writing_a_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="instance-a")
    rec = await _runner_task(service, "task-lease-loss")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingBridge:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self) -> bool:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return False

    monkeypatch.setattr("app.agents.service.EchoTaskStreamBridge", _BlockingBridge)
    service.start_bridge_for_task(rec)
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'agent_task' AND resource_id = ?""",
            (rec.tenant_id, rec.owner_id, rec.task_id),
        )
        await conn.commit()
    intruder_store = ExecutionLeaseStore(settings.db_path)
    intruder = await intruder_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="instance-b",
        ttl_seconds=1.0,
    )
    assert intruder is not None

    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    await service.aclose()
    stored = await service.get_task(rec.task_id)
    assert stored is not None
    assert stored.state.value == "pending"
    assert stored.finished_at is None
    assert await intruder_store.release(intruder) is True


@pytest.mark.unit
async def test_transient_heartbeat_exception_is_rescheduled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(
        settings,
        holder_id="instance-a",
        heartbeat=0.02,
        ttl=0.15,
        recovery_interval=0.01,
        retry_base=0.02,
        retry_max=0.04,
    )
    rec = await _runner_task(service, "task-heartbeat-retry")
    first_cancelled = asyncio.Event()
    completed = asyncio.Event()
    bridge_starts = 0

    class _RetryBridge:
        def __init__(self, **kwargs: Any) -> None:
            self.recorder = kwargs["recorder"]

        async def run(self) -> bool:
            nonlocal bridge_starts
            bridge_starts += 1
            if bridge_starts == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            await self.recorder(
                EchoTaskEvent(
                    task_id=rec.task_id,
                    runner_task_id=rec.runner_task_id,
                    event="task.result",
                    state="running",
                ),
                raw_hash="heartbeat-retry-result",
                raw_kind="result",
            )
            await self.recorder(
                EchoTaskEvent(
                    task_id=rec.task_id,
                    runner_task_id=rec.runner_task_id,
                    event="task.completed",
                    state="succeeded",
                ),
                raw_hash="heartbeat-retry-terminal",
                raw_kind="task_state",
            )
            completed.set()
            return True

    monkeypatch.setattr("app.agents.service.EchoTaskStreamBridge", _RetryBridge)
    original_renew = service._lease_store.renew
    renew_calls = 0

    async def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 1:
            raise OSError("transient sqlite failure")
        return await original_renew(*args, **kwargs)

    monkeypatch.setattr(service._lease_store, "renew", fail_once)
    try:
        service.start_bridge_for_task(rec)
        await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        stored = await _wait_for_bridge_completion(service, rec.task_id)
        assert stored.state.value == "succeeded"
        assert bridge_starts == 2
        assert renew_calls >= 1
    finally:
        await service.aclose()


@pytest.mark.unit
async def test_second_instance_reaps_expired_bridge_and_completes_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    first = _service(
        settings,
        holder_id="instance-a",
        heartbeat=0.03,
        ttl=0.15,
        recovery_interval=0.01,
        retry_base=0.03,
        retry_max=0.08,
    )
    second = _service(
        settings,
        holder_id="instance-b",
        heartbeat=0.03,
        ttl=0.15,
        recovery_interval=0.01,
        retry_base=0.03,
        retry_max=0.08,
    )
    waiting = await first.submit_task(
        AgentIntent(
            text="recover this bridge",
            device_id="legacy-local",
            echo_task_id="task-auto-failover",
        )
    )
    assert waiting.workflow_run_id is not None
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET runner_task_id = 'runner-auto-failover', state = 'pending'
               WHERE task_id = ?""",
            (waiting.task_id,),
        )
        await conn.commit()
    rec = await first.get_task(waiting.task_id)
    assert rec is not None
    task_id = rec.task_id
    runner_task_id = rec.runner_task_id

    first_started = asyncio.Event()
    second_completed = asyncio.Event()
    bridge_starts = 0

    class _FailoverBridge:
        def __init__(self, **kwargs: Any) -> None:
            self.recorder = kwargs["recorder"]

        async def run(self) -> bool:
            nonlocal bridge_starts
            bridge_starts += 1
            if bridge_starts == 1:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise
            await self.recorder(
                EchoTaskEvent(
                    task_id=task_id,
                    runner_task_id=runner_task_id,
                    event="task.result",
                    state="running",
                    message="runner result",
                ),
                raw_hash="failover-result",
                raw_kind="result",
            )
            await self.recorder(
                EchoTaskEvent(
                    task_id=task_id,
                    runner_task_id=runner_task_id,
                    event="task.completed",
                    state="succeeded",
                    message="runner completed",
                ),
                raw_hash="failover-terminal",
                raw_kind="task_state",
            )
            second_completed.set()
            return True

    monkeypatch.setattr("app.agents.service.EchoTaskStreamBridge", _FailoverBridge)
    acquire_attempts = 0
    original_acquire = second._lease_store.acquire

    async def counted_acquire(**kwargs: Any) -> Any:
        nonlocal acquire_attempts
        acquire_attempts += 1
        return await original_acquire(**kwargs)

    monkeypatch.setattr(second._lease_store, "acquire", counted_acquire)
    try:
        first.start_bridge_for_task(rec)
        second.start_bridge_for_task(rec)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await asyncio.sleep(0.12)
        assert bridge_starts == 1
        assert 1 <= acquire_attempts <= 4

        async def abandon_lease(_token: Any) -> bool:
            return False

        monkeypatch.setattr(first._lease_store, "release", abandon_lease)
        await first.aclose()
        await asyncio.wait_for(second_completed.wait(), timeout=2.0)
        stored = await _wait_for_bridge_completion(second, rec.task_id)
        run = await second.workflow.get_run(waiting.workflow_run_id)
        assert stored.state.value == "succeeded"
        assert stored.bridge_completed_at is not None
        assert run is not None
        assert run.state == "succeeded"
        assert bridge_starts == 2
        assert acquire_attempts <= 10
        await asyncio.sleep(0.1)
        assert bridge_starts == 2
    finally:
        await asyncio.gather(first.aclose(), second.aclose())

    assert second._recovery_task is None
    assert second._bridge_tasks == {}


@pytest.mark.unit
async def test_cross_instance_event_sequences_are_atomic(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    first = _service(settings, holder_id="instance-a")
    second = _service(settings, holder_id="instance-b")
    rec = await first.record_permission_required(
        AgentIntent(
            text="sequence events",
            device_id="legacy-local",
            echo_task_id="task-sequence",
        ),
        workflow_run_id=None,
    )

    async def append(service: AgentTaskService, index: int) -> None:
        stored = await service.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                event="task.text_delta",
                state="running",
                text_delta=f"{index},",
            )
        )
        assert stored is not None

    await asyncio.gather(*(append(first if index % 2 else second, index) for index in range(20)))
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        cur = await conn.execute(
            """SELECT seq FROM agent_task_events
               WHERE tenant_id = 'legacy-local' AND owner_id = 'legacy-local'
                 AND task_id = ? ORDER BY seq""",
            (rec.task_id,),
        )
        assert [row[0] for row in await cur.fetchall()] == list(range(1, 22))
        await cur.close()


@pytest.mark.unit
async def test_stale_fence_cannot_record_a_runner_event(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="instance-a")
    rec = await service.record_permission_required(
        AgentIntent(
            text="fence events",
            device_id="legacy-local",
            echo_task_id="task-fence",
        ),
        workflow_run_id=None,
    )
    store = ExecutionLeaseStore(settings.db_path)
    stale = await store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="instance-a",
        ttl_seconds=5.0,
    )
    assert stale is not None
    assert await store.release(stale) is True
    current = await store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="instance-b",
        ttl_seconds=5.0,
    )
    assert current is not None

    event = EchoTaskEvent(
        task_id=rec.task_id,
        event="task.started",
        state="running",
    )
    with pytest.raises(LeaseOwnershipError):
        await service.record_task_event(
            event,
            raw_hash="stale-event",
            raw_kind="task_state",
            lease_token=stale,
        )
    stored = await service.record_task_event(
        event,
        raw_hash="current-event",
        raw_kind="task_state",
        lease_token=current,
    )
    assert stored is not None
    assert stored.seq == 2
    assert await store.release(current) is True


@pytest.mark.unit
async def test_same_raw_hash_replays_projection_after_post_commit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="instance-a")
    rec = await service.submit_task(
        AgentIntent(
            text="produce an artifact",
            device_id="legacy-local",
            echo_task_id="task-projection-replay",
        )
    )
    assert rec.workflow_run_id is not None
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            "UPDATE agent_tasks SET runner_task_id = 'runner-projection' WHERE task_id = ?",
            (rec.task_id,),
        )
        await conn.commit()

    import_calls: list[list[dict[str, Any]]] = []

    async def crash_once(_rec: AgentTaskRecord, artifacts: list[dict[str, Any]]) -> None:
        import_calls.append(artifacts)
        if len(import_calls) == 1:
            raise RuntimeError("crash after event commit before artifact projection")

    monkeypatch.setattr(service, "_import_agent_artifacts", crash_once)
    lease = await service._lease_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id=service._holder_id,
        ttl_seconds=5.0,
    )
    assert lease is not None
    event = EchoTaskEvent(
        task_id=rec.task_id,
        runner_task_id="runner-projection",
        event="task.artifact_updated",
        state="running",
        artifacts=[{"name": "report.pdf", "relpath": "out/report.pdf", "kind": "pdf"}],
    )
    with pytest.raises(RuntimeError, match="artifact projection"):
        await service.record_task_event(
            event,
            raw_hash="artifact-raw-hash",
            raw_kind="artifact_change",
            lease_token=lease,
        )
    duplicate = await service.record_task_event(
        event,
        raw_hash="artifact-raw-hash",
        raw_kind="artifact_change",
        lease_token=lease,
    )
    assert duplicate is None
    assert len(import_calls) == 2

    async with aiosqlite.connect(str(settings.db_path)) as conn:
        cur = await conn.execute(
            """SELECT COUNT(*), MIN(projected_at), MAX(raw_kind)
               FROM agent_task_events WHERE task_id = ? AND raw_event_hash = ?""",
            (rec.task_id, "artifact-raw-hash"),
        )
        row = await cur.fetchone()
        await cur.close()
    assert row is not None
    count, projected_at, raw_kind = row
    assert count == 1
    assert projected_at is not None
    assert raw_kind == "artifact_change"
    stored_task = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert stored_task is not None
    assert stored_task.artifacts[0]["relpath"] == "out/report.pdf"
    assert run is not None
    assert run.output["artifacts"][0]["relpath"] == "out/report.pdf"
    assert await service._lease_store.release(lease) is True


@pytest.mark.unit
async def test_durable_terminal_tail_completes_bridge_after_crash_before_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="instance-a")
    rec = await _runner_task(service, "task-tail-marker-crash")
    lease = await service._lease_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="pre-crash-instance",
        ttl_seconds=5.0,
    )
    assert lease is not None
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            event="task.completed",
            state="succeeded",
            message="result committed",
        ),
        raw_hash="durable-result",
        raw_kind="result",
        lease_token=lease,
    )
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            event="task.completed",
            state="succeeded",
            message="tail committed",
        ),
        raw_hash="durable-tail",
        raw_kind="task_state",
        lease_token=lease,
    )
    assert await service._lease_store.release(lease) is True

    class _MustNotReconnectBridge:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("durable terminal tail should complete without reconnect")

    monkeypatch.setattr("app.agents.service.EchoTaskStreamBridge", _MustNotReconnectBridge)
    current = await service.get_task(rec.task_id)
    assert current is not None
    service.start_bridge_for_task(current)
    bridge_task = service._bridge_tasks[service._bridge_key(current)]
    await asyncio.wait_for(bridge_task, timeout=1.0)

    recovered = await service.get_task(rec.task_id)
    assert recovered is not None
    assert recovered.bridge_completed_at is not None
    assert recovered.state.value == "succeeded"
    await service.aclose()


@pytest.mark.unit
async def test_unfinished_public_agent_principals_are_enumerated(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="instance-a")
    principals = (
        Principal("tenant-a", "device-a", "owner-a", "session-a", "public"),
        Principal("tenant-b", "device-b", "owner-b", "session-b", "public"),
    )
    for index, principal in enumerate(principals):
        token = bind_principal(principal)
        try:
            rec = await service.record_permission_required(
                AgentIntent(
                    text=f"public task {index}",
                    device_id="forged-device",
                    echo_task_id=f"public-task-{index}",
                ),
                workflow_run_id=None,
            )
            async with aiosqlite.connect(str(settings.db_path)) as conn:
                await conn.execute(
                    """UPDATE agent_tasks SET state = 'pending', runner_task_id = ?
                       WHERE tenant_id = ? AND owner_id = ? AND task_id = ?""",
                    (f"runner-{index}", principal.tenant_id, principal.owner_id, rec.task_id),
                )
                await conn.commit()
        finally:
            reset_principal(token)

    restored = await service.list_unfinished_principals()
    assert {(item.tenant_id, item.device_id, item.owner_id, item.mode) for item in restored} == {
        ("tenant-a", "device-a", "owner-a", "public"),
        ("tenant-b", "device-b", "owner-b", "public"),
    }
