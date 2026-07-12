"""Agent 取消命令 outbox 的线性一致性、崩溃恢复与并发契约。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import aiosqlite
import httpx
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.agentos import AgentOSBackend, submit_operation_key
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentTaskRecord, AgentTaskService
from app.config import Settings
from app.runtime.execution_lease import LeaseOwnershipError


class _CancelBackend:
    enabled = True
    base_url = "http://127.0.0.1:9"

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str]] = []
        self.raise_after_remote_side_effect = False

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_cancel",
            accepted=True,
            provider="claude_code",
            runner_task_id=f"runner_{intent.echo_task_id}",
            runner_base_url=self.base_url,
        )

    async def cancel(self, runner_task_id: str, *, operation_key: str) -> bool:
        self.cancel_calls.append((runner_task_id, operation_key))
        if self.raise_after_remote_side_effect:
            self.raise_after_remote_side_effect = False
            raise RuntimeError("process died after remote cancel side effect")
        return True


class _BlockingSubmitBackend(_CancelBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.first_submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        self.submit_calls += 1
        self.first_submit_started.set()
        await self.release_submit.wait()
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_late_submit",
            accepted=True,
            provider="claude_code",
            runner_task_id="runner_late_submit",
            runner_base_url=self.base_url,
        )


class _BlockingRejectedSubmitBackend(_CancelBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        self.submit_started.set()
        await self.release_submit.wait()
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_stale_submit",
            accepted=False,
            provider="claude_code",
            error="stale submit failed",
        )


async def _make_service(
    tmp_path: Path,
    *,
    holder_id: str = "cancel-test-a",
    submit_lease_ttl_seconds: float | None = None,
    submit_heartbeat_seconds: float | None = None,
) -> tuple[AgentTaskService, InMemoryEventBus, _CancelBackend]:
    db_path = tmp_path / "agent-cancel.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        agent_os_enabled=False,
    )
    bus = InMemoryEventBus()
    service = AgentTaskService(
        settings,
        bus,
        holder_id=holder_id,
        submit_lease_ttl_seconds=submit_lease_ttl_seconds,
        submit_heartbeat_seconds=submit_heartbeat_seconds,
    )
    backend = _CancelBackend()
    service.backend = backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    return service, bus, backend


async def _running_task(service: AgentTaskService) -> AgentTaskRecord:
    await service.create_grant(device_id="desktop-test")
    task = await service.submit_task(AgentIntent(text="执行可取消任务", device_id="desktop-test"))
    await service.record_task_event(
        EchoTaskEvent(
            task_id=task.task_id,
            runner_task_id=task.runner_task_id,
            event="task.started",
            state="running",
        )
    )
    current = await service.get_task(task.task_id)
    assert current is not None
    return current


async def _leave_cancel_command_pending(
    service: AgentTaskService,
    task_id: str,
) -> None:
    async def crash_before_dispatch(**_kwargs: Any) -> int:
        raise RuntimeError("process died after cancel transaction commit")

    service.recover_cancel_commands_once = crash_before_dispatch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cancel transaction commit"):
        await service.cancel_task(task_id)


@pytest.mark.unit
async def test_terminal_agent_read_repairs_workflow_before_return(tmp_path: Path) -> None:
    service, _bus, _backend = await _make_service(tmp_path)
    task = await _running_task(service)
    assert task.workflow_run_id is not None
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET state = 'succeeded', final_text = 'durable result', finished_at = 'now'
               WHERE task_id = ?""",
            (task.task_id,),
        )
        await conn.commit()

    visible = await service.get_task(task.task_id)
    workflow = await service.workflow.get_run(task.workflow_run_id)

    assert visible is not None and visible.state.value == "succeeded"
    assert workflow is not None and workflow.state == "succeeded"


@pytest.mark.unit
async def test_cancel_requested_and_command_are_one_transaction(tmp_path: Path) -> None:
    service, _bus, _backend = await _make_service(tmp_path)
    task = await _running_task(service)
    assert task.workflow_run_id is not None

    await _leave_cancel_command_pending(service, task.task_id)

    async with aiosqlite.connect(service.settings.db_path) as conn:
        row = await (
            await conn.execute(
                """SELECT task.state, run.state, command.command_type,
                          command.operation_key, command.completed_at
                   FROM agent_tasks AS task
                   JOIN workflow_runs AS run
                     ON run.tenant_id = task.tenant_id
                    AND run.owner_id = task.owner_id
                    AND run.run_id = task.workflow_run_id
                   JOIN agent_command_outbox AS command
                     ON command.tenant_id = task.tenant_id
                    AND command.owner_id = task.owner_id
                    AND command.task_id = task.task_id
                   WHERE task.task_id = ?""",
                (task.task_id,),
            )
        ).fetchone()

    assert row is not None
    assert row[0:3] == ("cancel_requested", "cancel_requested", "cancel")
    assert str(row[3]).startswith("agent-cancel-")
    assert row[4] is None


@pytest.mark.unit
async def test_crash_after_remote_side_effect_replays_same_operation_key(
    tmp_path: Path,
) -> None:
    service, bus, _backend = await _make_service(tmp_path)
    task = await _running_task(service)
    await _leave_cancel_command_pending(service, task.task_id)

    recovering = AgentTaskService(service.settings, bus, holder_id="cancel-replay-a")
    backend = _CancelBackend()
    backend.raise_after_remote_side_effect = True
    recovering.backend = backend  # type: ignore[assignment]
    recovering.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    assert await recovering.recover_cancel_commands_once() == 0
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.execute(
            "UPDATE agent_command_outbox SET next_attempt_at = 0 WHERE task_id = ?",
            (task.task_id,),
        )
        await conn.commit()
    assert await recovering.recover_cancel_commands_once() == 1

    visible = await recovering.get_task(task.task_id)
    assert visible is not None and visible.state.value == "cancelled"
    assert len(backend.cancel_calls) == 2
    assert backend.cancel_calls[0][1] == backend.cancel_calls[1][1]


@pytest.mark.unit
async def test_two_instances_dispatch_one_cancel_lease(tmp_path: Path) -> None:
    first, bus, _backend = await _make_service(tmp_path)
    task = await _running_task(first)
    await _leave_cancel_command_pending(first, task.task_id)

    backend = _CancelBackend()
    first_worker = AgentTaskService(first.settings, bus, holder_id="cancel-worker-a")
    second_worker = AgentTaskService(first.settings, bus, holder_id="cancel-worker-b")
    for worker in (first_worker, second_worker):
        worker.backend = backend  # type: ignore[assignment]
        worker.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    results = await asyncio.gather(
        first_worker.recover_cancel_commands_once(),
        second_worker.recover_cancel_commands_once(),
    )

    assert sum(results) == 1
    assert len(backend.cancel_calls) == 1
    visible = await first_worker.get_task(task.task_id)
    assert visible is not None and visible.state.value == "cancelled"


@pytest.mark.unit
async def test_first_terminal_success_supersedes_pending_cancel_without_remote_call(
    tmp_path: Path,
) -> None:
    service, bus, _backend = await _make_service(tmp_path)
    task = await _running_task(service)
    await _leave_cancel_command_pending(service, task.task_id)

    await service.record_task_event(
        EchoTaskEvent(
            task_id=task.task_id,
            runner_task_id=task.runner_task_id,
            event="task.completed",
            state="succeeded",
            message="完成先于取消命令恢复",
        )
    )
    worker = AgentTaskService(service.settings, bus, holder_id="cancel-terminal-winner")
    backend = _CancelBackend()
    worker.backend = backend  # type: ignore[assignment]

    assert await worker.recover_cancel_commands_once() == 1
    visible = await worker.get_task(task.task_id)
    assert visible is not None and visible.state.value == "succeeded"
    assert backend.cancel_calls == []
    async with aiosqlite.connect(service.settings.db_path) as conn:
        row = await (
            await conn.execute(
                """SELECT outcome, completed_at FROM agent_command_outbox
                   WHERE task_id = ?""",
                (task.task_id,),
            )
        ).fetchone()
    assert row is not None and row[0] == "terminal_won" and row[1] is not None


@pytest.mark.unit
async def test_agentos_cancel_forwards_operation_key_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Idempotency-Key")))
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agents.agentos.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    backend = AgentOSBackend(
        Settings(
            db_path=tmp_path / "unused.db",
            storage_dir=tmp_path / "storage",
            agent_os_enabled=True,
            agent_os_url="http://agentos.test",
        )
    )

    assert await backend.cancel("runner-1", operation_key="agent-cancel-stable")
    assert seen == [("/api/v1/tasks/runner-1/cancel", "agent-cancel-stable")]


@pytest.mark.unit
def test_submit_operation_key_is_stable_and_scope_bound() -> None:
    baseline = submit_operation_key(
        tenant_id="tenant-a",
        owner_id="owner-a",
        task_id="task-a",
    )

    assert baseline == submit_operation_key(
        tenant_id="tenant-a",
        owner_id="owner-a",
        task_id="task-a",
    )
    assert (
        len(
            {
                baseline,
                submit_operation_key(
                    tenant_id="tenant-b",
                    owner_id="owner-a",
                    task_id="task-a",
                ),
                submit_operation_key(
                    tenant_id="tenant-a",
                    owner_id="owner-b",
                    task_id="task-a",
                ),
                submit_operation_key(
                    tenant_id="tenant-a",
                    owner_id="owner-a",
                    task_id="task-b",
                ),
            }
        )
        == 4
    )


@pytest.mark.unit
async def test_resume_submit_race_never_revives_cancelled_task(tmp_path: Path) -> None:
    service, _bus, _backend = await _make_service(tmp_path)
    task = await service.submit_task(AgentIntent(text="授权后立即取消", device_id="desktop-test"))
    grant = await service.create_grant(device_id="desktop-test")
    backend = _BlockingSubmitBackend()
    service.backend = backend  # type: ignore[assignment]

    resuming = asyncio.create_task(service.resume_with_grant(task.task_id, grant))
    await backend.first_submit_started.wait()
    cancelled = await service.cancel_task(task.task_id)
    backend.release_submit.set()
    resumed = await resuming

    assert cancelled is not None and cancelled.state.value == "cancelled"
    assert resumed.state.value == "cancelled"
    assert resumed.runner_task_id == "runner_late_submit"
    assert backend.cancel_calls == [
        ("runner_late_submit", ANY),
    ]
    async with aiosqlite.connect(service.settings.db_path) as conn:
        row = await (
            await conn.execute(
                """SELECT task.state, command.runner_task_id, command.outcome,
                          command.completed_at
                   FROM agent_tasks AS task
                   JOIN agent_command_outbox AS command
                     ON command.tenant_id = task.tenant_id
                    AND command.owner_id = task.owner_id
                    AND command.task_id = task.task_id
                   WHERE task.task_id = ?""",
                (task.task_id,),
            )
        ).fetchone()
    assert row is not None and row[0:3] == (
        "cancelled",
        "runner_late_submit",
        "cancelled",
    )
    assert row[3] is not None


@pytest.mark.unit
async def test_two_resume_requests_share_one_durable_submit_lease(tmp_path: Path) -> None:
    first, bus, _backend = await _make_service(tmp_path, holder_id="submit-worker-a")
    task = await first.submit_task(AgentIntent(text="只提交一次", device_id="desktop-test"))
    grant = await first.create_grant(device_id="desktop-test")
    second = AgentTaskService(first.settings, bus, holder_id="submit-worker-b")
    backend = _BlockingSubmitBackend()
    for service in (first, second):
        service.backend = backend  # type: ignore[assignment]
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    first_call = asyncio.create_task(first.resume_with_grant(task.task_id, grant))
    await backend.first_submit_started.wait()
    second_call = asyncio.create_task(second.resume_with_grant(task.task_id, grant))
    second_completed_while_first_held_lease = False
    try:
        await asyncio.wait_for(asyncio.shield(second_call), timeout=0.5)
        second_completed_while_first_held_lease = True
    except TimeoutError:
        pass
    finally:
        backend.release_submit.set()
        await asyncio.gather(first_call, second_call)

    assert second_completed_while_first_held_lease
    assert backend.submit_calls == 1
    stored = await first.get_task(task.task_id)
    assert stored is not None and stored.runner_task_id == "runner_late_submit"


@pytest.mark.unit
async def test_submit_heartbeat_prevents_takeover_after_initial_ttl(tmp_path: Path) -> None:
    first, bus, _backend = await _make_service(
        tmp_path,
        holder_id="submit-heartbeat-a",
        submit_lease_ttl_seconds=0.12,
        submit_heartbeat_seconds=0.02,
    )
    task = await first.submit_task(AgentIntent(text="续租后仍只提交一次", device_id="desktop-test"))
    grant = await first.create_grant(device_id="desktop-test")
    second = AgentTaskService(
        first.settings,
        bus,
        holder_id="submit-heartbeat-b",
        submit_lease_ttl_seconds=0.12,
        submit_heartbeat_seconds=0.02,
    )
    backend = _BlockingSubmitBackend()
    for service in (first, second):
        service.backend = backend  # type: ignore[assignment]
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    original_renew = first._lease_store.renew
    renew_count = 0
    renewed_beyond_initial_term = asyncio.Event()

    async def counted_renew(*args: Any, **kwargs: Any) -> Any:
        nonlocal renew_count
        renewed = await original_renew(*args, **kwargs)
        renew_count += 1
        if renew_count >= 8:
            renewed_beyond_initial_term.set()
        return renewed

    first._lease_store.renew = counted_renew  # type: ignore[method-assign]
    first_call = asyncio.create_task(first.resume_with_grant(task.task_id, grant))
    await backend.first_submit_started.wait()
    await asyncio.wait_for(renewed_beyond_initial_term.wait(), timeout=2)

    second_result = await asyncio.wait_for(
        second.resume_with_grant(task.task_id, grant),
        timeout=0.5,
    )
    backend.release_submit.set()
    await first_call

    assert renew_count >= 8
    assert backend.submit_calls == 1
    assert second_result.runner_task_id is None
    stored = await first.get_task(task.task_id)
    assert stored is not None and stored.runner_task_id == "runner_late_submit"


@pytest.mark.unit
async def test_stale_submit_failure_cannot_overwrite_new_owner(tmp_path: Path) -> None:
    first, bus, _backend = await _make_service(tmp_path, holder_id="stale-submit-a")
    task = await first.submit_task(
        AgentIntent(text="旧失败不能覆盖新 owner", device_id="desktop-test")
    )
    grant = await first.create_grant(device_id="desktop-test")
    second = AgentTaskService(first.settings, bus, holder_id="stale-submit-b")
    stale_backend = _BlockingRejectedSubmitBackend()
    first.backend = stale_backend  # type: ignore[assignment]
    second_backend = _CancelBackend()
    second.backend = second_backend  # type: ignore[assignment]
    for service in (first, second):
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    stale_call = asyncio.create_task(first.resume_with_grant(task.task_id, grant))
    await stale_backend.submit_started.wait()
    async with aiosqlite.connect(first.settings.db_path) as conn:
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0
               WHERE resource_kind = 'agent_submit' AND resource_id = ?""",
            (task.task_id,),
        )
        await conn.commit()

    current = await second.resume_with_grant(task.task_id, grant)
    stale_backend.release_submit.set()
    stale_result = await stale_call

    assert current.state.value == "pending"
    assert current.runner_task_id == f"runner_{task.task_id}"
    assert stale_result.state.value == "pending"
    assert stale_result.runner_task_id == current.runner_task_id


@pytest.mark.unit
async def test_agentos_cancel_reconciles_lost_or_duplicate_cancel_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(200, json={"id": "runner-1", "status": "cancelled"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.agents.agentos.httpx.AsyncClient", lambda **_kwargs: client)
    backend = AgentOSBackend(
        Settings(
            db_path=tmp_path / "unused.db",
            storage_dir=tmp_path / "storage",
            agent_os_enabled=True,
            agent_os_url="http://agentos.test",
        )
    )

    assert await backend.cancel("runner-1", operation_key="agent-cancel-stable")
    assert requests == [
        ("POST", "/api/v1/tasks/runner-1/cancel", "agent-cancel-stable"),
        ("GET", "/api/v1/tasks/runner-1", None),
    ]


@pytest.mark.unit
async def test_agentos_submit_reuses_remote_task_after_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[dict[str, Any], str | None]] = []
    logical_creations = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logical_creations
        body = json.loads(request.content)
        submitted.append((body, request.headers.get("Idempotency-Key")))
        if len(submitted) == 1:
            logical_creations += 1
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(202, json={"id": "runner-recovered", "status": "running"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.agents.agentos.httpx.AsyncClient", lambda **_kwargs: client)
    backend = AgentOSBackend(
        Settings(
            db_path=tmp_path / "unused.db",
            storage_dir=tmp_path / "storage",
            agent_os_enabled=True,
            agent_os_url="http://agentos.test",
        )
    )
    intent = AgentIntent(
        text="响应丢失也不能重复执行",
        device_id="desktop-test",
        echo_task_id="echo_task_stable_submit",
        runner_operation_key=submit_operation_key(
            tenant_id="tenant-test",
            owner_id="owner-test",
            task_id="echo_task_stable_submit",
        ),
    )

    result = await backend.submit(intent)

    assert result.accepted and result.runner_task_id == "runner-recovered"
    assert len(submitted) == 2 and logical_creations == 1
    assert submitted[0] == submitted[1]
    body, operation_key = submitted[0]
    assert body["operation_key"] == operation_key == intent.runner_operation_key
    assert operation_key.startswith("agent-submit-")
    assert "EchoDesk-Operation-Key" not in body["text"]
    agentos_root = os.environ.get("ECHODESK_AGENTOS_ROOT")
    if agentos_root:
        contract = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "from agentos.server.integrations import EchoIntent; "
                "intent=EchoIntent.model_validate(json.loads(sys.argv[1])); "
                "assert intent.operation_key and intent.text"
            ),
            json.dumps(body),
            cwd=agentos_root,
            env={**os.environ, "PYTHONPATH": agentos_root},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await contract.communicate()
        assert contract.returncode == 0, stderr.decode()


@pytest.mark.unit
async def test_stale_cancel_command_lease_cannot_write_agent_terminal(
    tmp_path: Path,
) -> None:
    service, _bus, _backend = await _make_service(tmp_path, holder_id="stale-worker")
    task = await _running_task(service)
    await _leave_cancel_command_pending(service, task.task_id)
    command = (
        await service._command_outbox.list_due(
            tenant_id=task.tenant_id,
            owner_id=task.owner_id,
            task_id=task.task_id,
        )
    )[0]
    stale = await service._lease_store.acquire(
        tenant_id=task.tenant_id,
        owner_id=task.owner_id,
        resource_kind="agent_command",
        resource_id=command.command_id,
        holder_id="stale-worker",
        ttl_seconds=30,
    )
    assert stale is not None
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.execute(
            """UPDATE execution_leases SET expires_at = 0
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'agent_command' AND resource_id = ?""",
            (task.tenant_id, task.owner_id, command.command_id),
        )
        await conn.commit()
    current = await service._lease_store.acquire(
        tenant_id=task.tenant_id,
        owner_id=task.owner_id,
        resource_kind="agent_command",
        resource_id=command.command_id,
        holder_id="current-worker",
        ttl_seconds=30,
    )
    assert current is not None

    with pytest.raises(LeaseOwnershipError):
        await service.record_task_event(
            EchoTaskEvent(
                task_id=task.task_id,
                runner_task_id=task.runner_task_id,
                event="task.cancelled",
                state="cancelled",
            ),
            cancel_command=command,
            cancel_command_lease=stale,
        )

    raw = await service._read_task(task.task_id)
    assert raw is not None and raw.state.value == "cancel_requested"
    await service._lease_store.release(current)


@pytest.mark.unit
async def test_workflow_timeout_wins_before_late_agent_completion(tmp_path: Path) -> None:
    service, _bus, _backend = await _make_service(tmp_path)
    task = await _running_task(service)
    assert task.workflow_run_id is not None
    timed_out = await service.workflow.timeout_run(
        task.workflow_run_id,
        error="workflow deadline won",
    )
    assert timed_out is not None and timed_out.state == "timeout"

    stored = await service.record_task_event(
        EchoTaskEvent(
            task_id=task.task_id,
            runner_task_id=task.runner_task_id,
            event="task.completed",
            state="succeeded",
            message="迟到成功",
        )
    )

    assert stored is not None and stored.event == "task.terminal_ignored"
    visible = await service.get_task(task.task_id)
    workflow = await service.workflow.get_run(task.workflow_run_id)
    assert visible is not None and visible.state.value == "timeout"
    assert workflow is not None and workflow.state == "timeout"
