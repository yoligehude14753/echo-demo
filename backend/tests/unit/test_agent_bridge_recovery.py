from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.artifact_transfer import ArtifactDownloadResult
from app.agents.base import AgentIntent, AgentTaskState
from app.agents.embedded_runtime import EmbeddedRuntimeBackend
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentTaskRecord, AgentTaskService
from app.config import Settings
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseOwnershipError
from app.security import Principal
from app.security.context import bind_principal, reset_principal


class _EnabledBackend(EmbeddedRuntimeBackend):
    enabled = True
    is_embedded = True
    base_url = "http://127.0.0.1:9"

    async def get_task(self, _runner_task_id: str) -> dict[str, Any] | None:
        return None


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
    timeout_s: float = 5.0,
) -> AgentTaskRecord:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        stored = await service.get_task(task_id)
        if stored is not None and stored.bridge_completed_at is not None:
            return stored
        await asyncio.sleep(0.01)
    raise AssertionError("bridge projection did not complete before timeout")


@pytest.mark.unit
async def test_periodic_recovery_replays_projection_only_task_without_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="projection-only-reaper")
    original_project = service._project_event

    async def crash_before_projection(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("crash after Agent event commit")

    monkeypatch.setattr(service, "_project_event", crash_before_projection)
    with pytest.raises(RuntimeError, match="Agent event commit"):
        await service.record_permission_required(
            AgentIntent(
                text="wait for permission without a runner",
                device_id="legacy-local",
                echo_task_id="task-projection-only-recovery",
            ),
            workflow_run_id=None,
        )
    monkeypatch.setattr(service, "_project_event", original_project)

    await service._recover_agent_bridges_once()

    async with aiosqlite.connect(str(settings.db_path)) as conn:
        row = await (
            await conn.execute(
                """SELECT projected_at FROM agent_task_events
                   WHERE task_id = 'task-projection-only-recovery' AND seq = 1"""
            )
        ).fetchone()
    assert row is not None and row[0] is not None
    recovered = await service.get_task("task-projection-only-recovery")
    assert recovered is not None
    assert recovered.runner_task_id is None
    assert service._bridge_tasks == {}
    await service.aclose()


@pytest.mark.unit
async def test_bridge_recovers_completed_task_from_agentos_http_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="http-snapshot-recovery")
    rec, _created = await service._create_task_with_run(
        AgentIntent(
            text="produce a report",
            device_id="legacy-local",
            echo_task_id="task-http-snapshot-recovery",
        ),
        state=AgentTaskState.PENDING,
    )
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET runner_task_id = 'runner-http-snapshot', state = 'pending'
               WHERE task_id = ?""",
            (rec.task_id,),
        )
        await conn.commit()
    rec = await service.get_task(rec.task_id)
    assert rec is not None
    assert rec.workflow_run_id is not None

    async def http_snapshot(_runner_task_id: str) -> dict[str, Any]:
        return {
            "id": "runner-http-snapshot",
            "status": "succeeded",
            "final_text": "报告已完成",
            "finished_at": "2026-07-13T06:00:00+00:00",
            "duration_ms": 1234,
            "artifacts": [
                {
                    "relpath": "reports/output.md",
                    "name": "output.md",
                    "kind": "text",
                    "size_bytes": 42,
                }
            ],
        }

    monkeypatch.setattr(service.backend, "get_task", http_snapshot)

    async def fake_download(*_args: Any, **kwargs: Any) -> ArtifactDownloadResult:
        cache_path = kwargs["cache_path"]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("done", encoding="utf-8")
        return ArtifactDownloadResult(
            path=cache_path,
            size_bytes=4,
            content_type="text/markdown",
        )

    monkeypatch.setattr("app.agents.service.download_artifact_to_path", fake_download)

    service.start_bridge_for_task(rec)
    recovered = await _wait_for_bridge_completion(service, rec.task_id)
    assert recovered.state == AgentTaskState.SUCCEEDED
    assert recovered.final_text == "报告已完成"
    assert recovered.artifacts[0]["relpath"] == "reports/output.md"
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert run is not None
    assert run.state == "succeeded"
    assert run.output["artifacts"][0]["relpath"] == "reports/output.md"
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT raw_kind FROM agent_task_events
                   WHERE task_id = ? ORDER BY seq""",
                (rec.task_id,),
            )
        ).fetchall()
    assert [row[0] for row in rows] == ["artifact_change", "result", "task_state"]
    await service.aclose()


class _FailoverBridgeHarness:
    def __init__(self, *, task_id: str, runner_task_id: str | None) -> None:
        self.task_id = task_id
        self.runner_task_id = runner_task_id
        self.initial_bridge_started = asyncio.Event()
        self.takeover_completed = asyncio.Event()
        self.starts = 0

    def build(self, **kwargs: Any) -> _FailoverBridgeAttempt:
        return _FailoverBridgeAttempt(self, kwargs["recorder"])


class _FailoverBridgeAttempt:
    def __init__(self, harness: _FailoverBridgeHarness, recorder: Any) -> None:
        self.harness = harness
        self.recorder = recorder

    async def run(self) -> bool:
        self.harness.starts += 1
        if self.harness.starts == 1:
            self.harness.initial_bridge_started.set()
            await asyncio.Event().wait()
        await self.recorder(
            EchoTaskEvent(
                task_id=self.harness.task_id,
                runner_task_id=self.harness.runner_task_id,
                event="task.result",
                state="running",
                message="runner result",
            ),
            raw_hash="failover-result",
            raw_kind="result",
        )
        await self.recorder(
            EchoTaskEvent(
                task_id=self.harness.task_id,
                runner_task_id=self.harness.runner_task_id,
                event="task.completed",
                state="succeeded",
                message="runner completed",
            ),
            raw_hash="failover-terminal",
            raw_kind="task_state",
        )
        self.harness.takeover_completed.set()
        return True


class _AgentTaskAcquireProbe:
    def __init__(self) -> None:
        self.attempts = 0
        self.both_instances_attempted = asyncio.Event()

    def wrap(self, acquire: Any) -> Any:
        async def counted(**kwargs: Any) -> Any:
            result = await acquire(**kwargs)
            if kwargs.get("resource_kind") == "agent_task":
                self.attempts += 1
                if self.attempts >= 2:
                    self.both_instances_attempted.set()
            return result

        return counted


async def _current_bridge_holder(
    settings: Settings,
    rec: AgentTaskRecord,
) -> str:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        cur = await conn.execute(
            """SELECT holder_id FROM execution_leases
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'agent_task' AND resource_id = ?""",
            (rec.tenant_id, rec.owner_id, rec.task_id),
        )
        lease_row = await cur.fetchone()
        await cur.close()
    assert lease_row is not None
    return str(lease_row[0])


async def _close_without_releasing_bridge_lease(
    service: AgentTaskService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def abandon_lease(_token: Any) -> bool:
        return False

    monkeypatch.setattr(service._lease_store, "release", abandon_lease)
    await service.aclose()


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

    monkeypatch.setattr("app.agents.service.EmbeddedTaskStreamBridge", _BlockingBridge)
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

    monkeypatch.setattr("app.agents.service.EmbeddedTaskStreamBridge", _BlockingBridge)
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

    monkeypatch.setattr("app.agents.service.EmbeddedTaskStreamBridge", _RetryBridge)
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
async def test_surviving_instance_reaps_expired_bridge_and_completes_projection(
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
    harness = _FailoverBridgeHarness(
        task_id=rec.task_id,
        runner_task_id=rec.runner_task_id,
    )
    monkeypatch.setattr("app.agents.service.EmbeddedTaskStreamBridge", harness.build)
    acquire_probe = _AgentTaskAcquireProbe()
    monkeypatch.setattr(
        first._lease_store,
        "acquire",
        acquire_probe.wrap(first._lease_store.acquire),
    )
    monkeypatch.setattr(
        second._lease_store,
        "acquire",
        acquire_probe.wrap(second._lease_store.acquire),
    )
    try:
        first.start_bridge_for_task(rec)
        second.start_bridge_for_task(rec)
        await asyncio.wait_for(harness.initial_bridge_started.wait(), timeout=1.0)
        await asyncio.wait_for(acquire_probe.both_instances_attempted.wait(), timeout=1.0)
        assert harness.starts == 1
        assert 2 <= acquire_probe.attempts <= 4

        holder_id = await _current_bridge_holder(settings, rec)
        assert holder_id in {"instance-a", "instance-b"}
        lease_holder = first if holder_id == "instance-a" else second
        survivor = second if lease_holder is first else first

        await _close_without_releasing_bridge_lease(lease_holder, monkeypatch)
        await asyncio.wait_for(harness.takeover_completed.wait(), timeout=2.0)
        stored = await _wait_for_bridge_completion(survivor, rec.task_id)
        run = await survivor.workflow.get_run(waiting.workflow_run_id)
        assert stored.state.value == "succeeded"
        assert stored.bridge_completed_at is not None
        assert run is not None
        assert run.state == "succeeded"
        assert harness.starts == 2
        assert acquire_probe.attempts <= 12
        await asyncio.sleep(0.1)
        assert harness.starts == 2
    finally:
        await asyncio.gather(first.aclose(), second.aclose())

    assert first._recovery_task is None
    assert second._recovery_task is None
    assert first._bridge_tasks == {}
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

    async def crash_once(
        _rec: AgentTaskRecord,
        artifacts: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> None:
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
        workflow_counts = await (
            await conn.execute(
                """SELECT event_type, COUNT(*) FROM workflow_events
                   WHERE tenant_id = ? AND owner_id = ? AND run_id = ?
                     AND event_type IN ('agent.task.artifact_updated',
                                        'agent.artifacts_projected')
                   GROUP BY event_type ORDER BY event_type""",
                (rec.tenant_id, rec.owner_id, rec.workflow_run_id),
            )
        ).fetchall()
    assert row is not None
    count, projected_at, raw_kind = row
    assert count == 1
    assert projected_at is not None
    assert raw_kind == "artifact_change"
    assert workflow_counts == [
        ("agent.artifacts_projected", 1),
        ("agent.task.artifact_updated", 1),
    ]
    stored_task = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert stored_task is not None
    assert stored_task.artifacts[0]["relpath"] == "out/report.pdf"
    assert run is not None
    assert run.output["artifacts"][0]["relpath"] == "out/report.pdf"
    assert await service._lease_store.release(lease) is True


@pytest.mark.unit
async def test_artifact_projection_retry_is_idempotent_after_import_commit_crash(  # noqa: PLR0915 - keep crash-boundary assertions together
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    service = _service(settings, holder_id="artifact-crash-instance")
    rec = await service.submit_task(
        AgentIntent(
            text="persist one artifact exactly once",
            device_id="legacy-local",
            echo_task_id="task-artifact-import-commit-crash",
        )
    )
    assert rec.workflow_run_id is not None
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            """UPDATE agent_tasks SET runner_task_id = ?, state = 'pending'
               WHERE tenant_id = ? AND owner_id = ? AND task_id = ?""",
            ("runner-artifact-crash", rec.tenant_id, rec.owner_id, rec.task_id),
        )
        await conn.commit()
    rec = await service.get_task(rec.task_id)
    assert rec is not None

    lease = await service._lease_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id=service._holder_id,
        ttl_seconds=5.0,
    )
    assert lease is not None

    download_calls = 0

    async def fake_download(*_args: Any, **kwargs: Any) -> ArtifactDownloadResult:
        nonlocal download_calls
        download_calls += 1
        cache_path = Path(kwargs["cache_path"])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"pdf-payload")
        return ArtifactDownloadResult(size_bytes=11, content_type="application/pdf")

    import_attempts = 0

    original_import = service._import_agent_artifacts

    async def count_import_attempts(*args: Any, **kwargs: Any) -> None:
        nonlocal import_attempts
        import_attempts += 1
        await original_import(*args, **kwargs)
        if import_attempts == 1:
            raise RuntimeError("crash after artifact import commit")

    published_types: list[str] = []

    async def count_publish(_scope: tuple[str, str], event: Any) -> None:
        if (
            event.type != "agent.task.event"
            or event.payload.get("event") == "task.artifact_updated"
        ):
            published_types.append(str(event.type))

    monkeypatch.setattr(service, "_download_agent_artifact", fake_download)
    monkeypatch.setattr(service, "_import_agent_artifacts", count_import_attempts)
    monkeypatch.setattr(service.event_bus, "publish_to", count_publish)

    event = EchoTaskEvent(
        task_id=rec.task_id,
        runner_task_id=rec.runner_task_id,
        event="task.artifact_updated",
        state="running",
        message="artifact imported",
        artifacts=[{"name": "report.pdf", "relpath": "out/report.pdf", "kind": "pdf"}],
    )
    with pytest.raises(RuntimeError, match="artifact import commit"):
        await service.record_task_event(
            event,
            raw_hash="artifact-import-commit-crash",
            raw_kind="artifact_change",
            lease_token=lease,
        )

    async def upstream_must_not_be_retried(*_args: Any, **_kwargs: Any) -> ArtifactDownloadResult:
        raise AssertionError("durably imported artifact must not be downloaded again")

    monkeypatch.setattr(service, "_download_agent_artifact", upstream_must_not_be_retried)
    duplicate = await service.record_task_event(
        event,
        raw_hash="artifact-import-commit-crash",
        raw_kind="artifact_change",
        lease_token=lease,
    )
    assert duplicate is None
    await service.workflow.flush_outbox()

    async with aiosqlite.connect(str(settings.db_path)) as conn:
        artifact_count = int(
            (
                await (
                    await conn.execute(
                        """SELECT COUNT(*) FROM artifacts
                           WHERE tenant_id = ? AND owner_id = ? AND run_id = ?""",
                        (rec.tenant_id, rec.owner_id, rec.workflow_run_id),
                    )
                ).fetchone()
            )[0]
        )
        link_count = int(
            (
                await (
                    await conn.execute(
                        """SELECT COUNT(*) FROM artifact_links
                           WHERE tenant_id = ? AND owner_id = ? AND run_id = ?""",
                        (rec.tenant_id, rec.owner_id, rec.workflow_run_id),
                    )
                ).fetchone()
            )[0]
        )
        workflow_counts = await (
            await conn.execute(
                """SELECT event_type, COUNT(*) FROM workflow_events
                   WHERE tenant_id = ? AND owner_id = ? AND run_id = ?
                     AND event_type IN ('agent.task.artifact_updated',
                                        'agent.artifact_imported',
                                        'agent.artifacts_projected')
                   GROUP BY event_type ORDER BY event_type""",
                (rec.tenant_id, rec.owner_id, rec.workflow_run_id),
            )
        ).fetchall()
        event_row = await (
            await conn.execute(
                """SELECT projected_at FROM agent_task_events
                   WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                     AND raw_event_hash = ?""",
                (rec.tenant_id, rec.owner_id, rec.task_id, "artifact-import-commit-crash"),
            )
        ).fetchone()
    assert artifact_count == 1
    assert link_count == 1
    assert workflow_counts == [
        ("agent.artifact_imported", 1),
        ("agent.artifacts_projected", 1),
        ("agent.task.artifact_updated", 1),
    ]
    assert event_row is not None and event_row[0] is not None
    assert import_attempts == 2
    assert download_calls == 1
    assert published_types.count("artifact.ready") == 1
    assert published_types.count("agent.task.event") == 1
    assert await service._lease_store.release(lease) is True
    await service.aclose()


@pytest.mark.unit
async def test_projection_fence_blocks_stale_side_effects_after_mid_projection_takeover(  # noqa: PLR0915 - explicit two-fence orchestration
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _settings(tmp_path)
    first = _service(
        settings,
        holder_id="projection-instance-a",
        heartbeat=0.02,
        ttl=0.08,
    )
    second = _service(
        settings,
        holder_id="projection-instance-b",
        heartbeat=0.1,
        ttl=1.0,
    )
    rec = await first.submit_task(
        AgentIntent(
            text="project one durable artifact event",
            device_id="legacy-local",
            echo_task_id="task-projection-fence-takeover",
        )
    )
    assert rec.workflow_run_id is not None
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        await conn.execute(
            """UPDATE agent_tasks SET runner_task_id = ?, state = 'pending'
               WHERE tenant_id = ? AND owner_id = ? AND task_id = ?""",
            ("runner-projection-fence", rec.tenant_id, rec.owner_id, rec.task_id),
        )
        await conn.commit()
    rec = await first.get_task(rec.task_id)
    assert rec is not None

    stale_task_lease = await first._lease_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="projection-instance-a",
        ttl_seconds=0.08,
    )
    assert stale_task_lease is not None

    projection_paused = asyncio.Event()
    resume_stale_projection = asyncio.Event()
    original_project = first._project_workflow_event

    async def pause_stale_projection(
        task: AgentTaskRecord,
        event: EchoTaskEvent,
        **kwargs: Any,
    ) -> None:
        projection_paused.set()
        await resume_stale_projection.wait()
        await original_project(task, event, **kwargs)

    async def abandon_projection_heartbeat(_lease: Any) -> None:
        await asyncio.Event().wait()

    artifact_imports: list[tuple[str, int]] = []

    async def count_artifact_import(
        task: AgentTaskRecord,
        _artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        projection_lease = kwargs["projection_lease"]
        projection_seq = int(kwargs["projection_seq"])
        await second._assert_record_projection(projection_lease, task, projection_seq)
        artifact_imports.append((projection_lease.holder_id, projection_lease.fence_token))

    published_agent_events: list[EchoTaskEvent] = []

    async def count_publish(_scope: tuple[str, str], event: Any) -> None:
        if event.type == "agent.task.event":
            projected = EchoTaskEvent.model_validate(event.payload)
            if projected.seq == projection_seq:
                published_agent_events.append(projected)

    monkeypatch.setattr(first, "_project_workflow_event", pause_stale_projection)
    monkeypatch.setattr(first, "_heartbeat_projection_lease", abandon_projection_heartbeat)
    monkeypatch.setattr(first, "_import_agent_artifacts", count_artifact_import)
    monkeypatch.setattr(second, "_import_agent_artifacts", count_artifact_import)
    monkeypatch.setattr(first.event_bus, "publish_to", count_publish)
    monkeypatch.setattr(second.event_bus, "publish_to", count_publish)

    event = EchoTaskEvent(
        task_id=rec.task_id,
        runner_task_id=rec.runner_task_id,
        event="task.artifact_updated",
        state="running",
        message="artifact ready",
        artifacts=[{"name": "report.pdf", "relpath": "out/report.pdf", "kind": "pdf"}],
    )
    stale_projection = asyncio.create_task(
        first.record_task_event(
            event,
            raw_hash="projection-fence-artifact",
            raw_kind="artifact_change",
            lease_token=stale_task_lease,
        )
    )
    await asyncio.wait_for(projection_paused.wait(), timeout=1.0)

    pending = await first.get_task(rec.task_id)
    assert pending is not None
    projection_seq = pending.last_seq
    projection_resource_id = first._projection_resource_id(rec.task_id, projection_seq)
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        first_claim = await (
            await conn.execute(
                """SELECT holder_id, fence_token FROM execution_leases
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = 'agent_projection' AND resource_id = ?""",
                (rec.tenant_id, rec.owner_id, projection_resource_id),
            )
        ).fetchone()
    assert first_claim == (f"{first._holder_id}:projection:{projection_seq}", 1)

    await asyncio.sleep(0.1)
    current_task_lease = await second._lease_store.acquire(
        tenant_id=rec.tenant_id,
        owner_id=rec.owner_id,
        resource_kind="agent_task",
        resource_id=rec.task_id,
        holder_id="projection-instance-b",
        ttl_seconds=1.0,
    )
    assert current_task_lease is not None
    assert current_task_lease.fence_token == stale_task_lease.fence_token + 1

    await second._project_event(
        rec.task_id,
        projection_seq,
        lease_token=current_task_lease,
    )
    resume_stale_projection.set()
    with pytest.raises(LeaseOwnershipError):
        await stale_projection
    await second.workflow.flush_outbox()

    async with aiosqlite.connect(str(settings.db_path)) as conn:
        projection_row = await (
            await conn.execute(
                """SELECT projected_at FROM agent_task_events
                   WHERE tenant_id = ? AND owner_id = ? AND task_id = ? AND seq = ?""",
                (rec.tenant_id, rec.owner_id, rec.task_id, projection_seq),
            )
        ).fetchone()
        current_claim = await (
            await conn.execute(
                """SELECT holder_id, fence_token FROM execution_leases
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = 'agent_projection' AND resource_id = ?""",
                (rec.tenant_id, rec.owner_id, projection_resource_id),
            )
        ).fetchone()
        workflow_counts = await (
            await conn.execute(
                """SELECT event_type, COUNT(*) FROM workflow_events
                   WHERE tenant_id = ? AND owner_id = ? AND run_id = ?
                     AND event_type IN ('agent.task.artifact_updated',
                                        'agent.artifacts_projected')
                   GROUP BY event_type ORDER BY event_type""",
                (rec.tenant_id, rec.owner_id, rec.workflow_run_id),
            )
        ).fetchall()
    assert projection_row is not None and projection_row[0] is not None
    assert current_claim == (f"{second._holder_id}:projection:{projection_seq}", 2)
    assert artifact_imports == [(f"{second._holder_id}:projection:{projection_seq}", 2)]
    assert len(published_agent_events) == 1
    assert published_agent_events[0].seq == projection_seq
    assert workflow_counts == [
        ("agent.artifacts_projected", 1),
        ("agent.task.artifact_updated", 1),
    ]
    assert await second._lease_store.release(current_task_lease) is True
    await asyncio.gather(first.aclose(), second.aclose())


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

    monkeypatch.setattr("app.agents.service.EmbeddedTaskStreamBridge", _MustNotReconnectBridge)
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
