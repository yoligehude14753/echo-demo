"""AgentTaskService 单测：授权、事件持久化、去重与 replay。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentRunnerGrant, AgentTaskService
from app.api.agents import _encode_agentos_artifact_path
from app.config import Settings
from app.security import Principal
from app.security.context import bind_principal, reset_principal
from fastapi import HTTPException

from tests.unit._principal_identity import seed_principal_identity


class _FakeBackend:
    enabled = True
    base_url = "http://127.0.0.1:9"

    def __init__(self) -> None:
        self.submissions: list[AgentIntent] = []

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        self.submissions.append(intent)
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_fake",
            accepted=True,
            provider="claude_code",
            runner_task_id="runner_fake",
            runner_base_url=self.base_url,
        )

    async def cancel(self, _runner_task_id: str, *, operation_key: str) -> bool:
        assert operation_key.startswith("agent-cancel-")
        return True


class _CrashOnceBackend(_FakeBackend):
    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        self.submissions.append(intent)
        if len(self.submissions) == 1:
            raise RuntimeError("process died around AgentOS submit")
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_fake",
            accepted=True,
            provider="claude_code",
            runner_task_id="runner_recovered",
            runner_base_url=self.base_url,
        )


async def _make_service(
    tmp_path: Path, **settings_overrides: Any
) -> tuple[AgentTaskService, InMemoryEventBus]:
    db_path = tmp_path / "agent.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        agent_os_enabled=False,
        **settings_overrides,
    )
    bus = InMemoryEventBus()
    return AgentTaskService(settings, bus), bus


@pytest.mark.unit
def test_agent_artifact_proxy_path_sanitizer() -> None:
    assert _encode_agentos_artifact_path("out/报告 1.pdf") == "out/%E6%8A%A5%E5%91%8A%201.pdf"
    with pytest.raises(HTTPException):
        _encode_agentos_artifact_path("../secret.txt")


@pytest.mark.unit
async def test_submit_without_grant_records_permission_event_and_broadcasts(tmp_path: Path) -> None:
    service, bus = await _make_service(tmp_path)

    rec = await service.submit_task(
        AgentIntent(text="帮我生成一份调研报告", device_id="desktop-test", title="调研报告")
    )

    assert rec.state.value == "waiting_permission"
    assert rec.workflow_run_id is not None
    events, snapshot, last_seq = await service.list_events(rec.task_id)
    assert last_seq == 1
    assert events[0].event == "task.permission_required"
    assert events[0].actions == [
        {"id": "grant_and_start", "label": "允许并开始"},
        {"id": "cancel", "label": "取消"},
    ]
    assert snapshot["permission"]["permission_profile"] == "claude_code_full_access"

    agen = bus.subscribe()
    try:
        published = [await agen.__anext__() for _ in range(bus.max_seq)]
    finally:
        await agen.aclose()
    agent_events = [event for event in published if event.type == "agent.task.event"]
    workflow_events = [event for event in published if event.type == "workflow.event"]
    assert agent_events[-1].payload["event"] == "task.permission_required"
    assert workflow_events


@pytest.mark.unit
async def test_record_task_event_dedupes_by_raw_hash_and_replays_after_seq(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    rec = await service.submit_task(AgentIntent(text="写一个文件", device_id="desktop-test"))

    first = await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            title=rec.title,
            event="task.text_delta",
            state="running",
            text_delta="hello",
        ),
        raw_hash="same-raw-event",
    )
    duplicate = await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            title=rec.title,
            event="task.text_delta",
            state="running",
            text_delta="hello",
        ),
        raw_hash="same-raw-event",
    )

    assert first is not None
    assert duplicate is None
    events, snapshot, last_seq = await service.list_events(rec.task_id, after_seq=1)
    assert [event.event for event in events] == ["task.text_delta"]
    assert last_seq == 2
    assert snapshot["text_buffer"] == "hello"
    assert events[0].snapshot["text_buffer"] == "hello"


@pytest.mark.unit
async def test_agent_events_project_to_workflow_run(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")

    rec = await service.submit_task(AgentIntent(text="写一个文件", device_id="desktop-test"))
    assert rec.workflow_run_id is not None
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            title=rec.title,
            event="task.started",
            state="running",
            message="任务开始执行",
        )
    )
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            title=rec.title,
            event="task.completed",
            state="succeeded",
            message="完成",
        )
    )

    run = await service.workflow.get_run(rec.workflow_run_id)
    assert run is not None
    assert run.state == "succeeded"
    assert run.output["agent_task_id"] == rec.task_id
    workflow_events = await service.workflow.list_events(rec.workflow_run_id)
    assert "agent.task.completed" in [event.event_type for event in workflow_events]


@pytest.mark.unit
async def test_terminal_agent_event_is_published_after_workflow_projection(
    tmp_path: Path,
) -> None:
    service, bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")

    rec = await service.submit_task(AgentIntent(text="取消一致性", device_id="desktop-test"))
    assert rec.workflow_run_id is not None

    observed_workflow_states: list[str | None] = []
    publish_to = bus.publish_to

    async def observe_then_publish(scope: tuple[str, str], event: Any) -> None:
        if event.type == "agent.task.event" and event.payload.get("state") == "cancelled":
            run = await service.workflow.get_run(rec.workflow_run_id or "")
            observed_workflow_states.append(run.state if run else None)
        await publish_to(scope, event)

    bus.publish_to = observe_then_publish  # type: ignore[method-assign]
    cancelled = await service.cancel_task(rec.task_id)

    assert cancelled is not None
    assert cancelled.state.value == "cancelled"
    assert observed_workflow_states == ["cancelled"]
    workflow_run = await service.workflow.get_run(rec.workflow_run_id)
    assert workflow_run is not None
    assert workflow_run.state == "cancelled"


@pytest.mark.unit
async def test_first_terminal_wins_when_cancel_arrives_after_success(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    service.backend = _FakeBackend()  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")
    rec = await service.submit_task(AgentIntent(text="成功优先", device_id="desktop-test"))
    assert rec.workflow_run_id is not None

    await service.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
    )
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.completed",
            state="succeeded",
            message="任务完成",
        )
    )
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.started",
            state="running",
            message="迟到的执行中状态",
        )
    )
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.cancelled",
            state="cancelled",
            message="迟到的取消",
        )
    )

    stored = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    events, snapshot, _last_seq = await service.list_events(rec.task_id)
    assert stored is not None and stored.state.value == "succeeded"
    assert run is not None and run.state == "succeeded"
    assert events[-1].event == "task.terminal_ignored"
    assert events[-1].visibility == "debug"
    assert events[-1].state == "succeeded"
    assert events[-2].event == "task.terminal_ignored"
    assert snapshot["progress_text"] == "任务完成"


@pytest.mark.unit
async def test_first_terminal_wins_when_success_arrives_after_cancel(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    service.backend = _FakeBackend()  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")
    rec = await service.submit_task(AgentIntent(text="取消优先", device_id="desktop-test"))
    assert rec.workflow_run_id is not None
    await service.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
    )

    cancelled = await service.cancel_task(rec.task_id)
    assert cancelled is not None and cancelled.state.value == "cancelled"
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.completed",
            state="succeeded",
            message="迟到的成功",
        )
    )

    stored = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    events, snapshot, _last_seq = await service.list_events(rec.task_id)
    assert stored is not None and stored.state.value == "cancelled"
    assert run is not None and run.state == "cancelled"
    assert events[-1].event == "task.terminal_ignored"
    assert events[-1].state == "cancelled"
    assert snapshot["progress_text"] == "任务已取消"


@pytest.mark.unit
async def test_success_cancel_race_keeps_agent_workflow_and_projection_consistent(
    tmp_path: Path,
) -> None:
    first, bus = await _make_service(tmp_path)
    second = AgentTaskService(first.settings, bus, holder_id="race-instance-b")
    for service in (first, second):
        service.backend = _FakeBackend()  # type: ignore[assignment]
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await first.create_grant(device_id="desktop-test")

    for index in range(20):
        rec = await first.submit_task(
            AgentIntent(text=f"并发终态 {index}", device_id="desktop-test")
        )
        assert rec.workflow_run_id is not None
        await first.record_task_event(
            EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
        )

        await asyncio.gather(
            first.record_task_event(
                EchoTaskEvent(
                    task_id=rec.task_id,
                    event="task.completed",
                    state="succeeded",
                    message="并发成功",
                )
            ),
            second.cancel_task(rec.task_id),
        )

        stored = await first.get_task(rec.task_id)
        run = await first.workflow.get_run(rec.workflow_run_id)
        assert stored is not None
        assert run is not None
        assert stored.state.value in {"succeeded", "cancelled"}
        assert run.state == stored.state.value
        async with aiosqlite.connect(first.settings.db_path) as conn:
            cur = await conn.execute(
                """SELECT COUNT(*) FROM agent_task_events
                   WHERE task_id = ? AND projected_at IS NULL""",
                (rec.task_id,),
            )
            assert await cur.fetchone() == (0,)
            await cur.close()


@pytest.mark.unit
async def test_cancel_stale_read_returns_concurrent_success_without_runner_cancel(
    tmp_path: Path,
) -> None:
    first, bus = await _make_service(tmp_path)
    second = AgentTaskService(first.settings, bus, holder_id="cancel-stale-reader")
    for service in (first, second):
        service.backend = _FakeBackend()  # type: ignore[assignment]
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await first.create_grant(device_id="desktop-test")
    rec = await first.submit_task(AgentIntent(text="取消读取竞态", device_id="desktop-test"))
    assert rec.workflow_run_id is not None
    await first.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
    )

    original_get = second.get_task
    stale_read = asyncio.Event()
    allow_stale_return = asyncio.Event()
    get_calls = 0

    async def controlled_get(task_id: str) -> Any:
        nonlocal get_calls
        get_calls += 1
        current = await original_get(task_id)
        if get_calls == 1:
            stale_read.set()
            await allow_stale_return.wait()
        return current

    cancel_calls = 0

    async def count_runner_cancel(_runner_task_id: str, *, operation_key: str) -> bool:
        nonlocal cancel_calls
        assert operation_key.startswith("agent-cancel-")
        cancel_calls += 1
        return True

    second.get_task = controlled_get  # type: ignore[method-assign]
    second.backend.cancel = count_runner_cancel  # type: ignore[method-assign]
    cancelling = asyncio.create_task(second.cancel_task(rec.task_id))
    await asyncio.wait_for(stale_read.wait(), timeout=1.0)
    await first.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.completed",
            state="succeeded",
            message="并发完成",
        )
    )
    allow_stale_return.set()
    cancelled = await asyncio.wait_for(cancelling, timeout=1.0)

    stored = await first.get_task(rec.task_id)
    run = await first.workflow.get_run(rec.workflow_run_id)
    assert cancelled is not None and cancelled.state.value == "succeeded"
    assert stored is not None and stored.state.value == "succeeded"
    assert run is not None and run.state == "succeeded"
    assert cancel_calls == 0
    async with aiosqlite.connect(first.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT COUNT(*) FROM agent_task_events
               WHERE task_id = ? AND projected_at IS NULL""",
            (rec.task_id,),
        )
        assert await cur.fetchone() == (0,)
        await cur.close()


@pytest.mark.unit
async def test_cancel_request_loses_to_success_before_runner_cancel(
    tmp_path: Path,
) -> None:
    first, bus = await _make_service(tmp_path)
    second = AgentTaskService(first.settings, bus, holder_id="cancel-request-race")
    for service in (first, second):
        service.backend = _FakeBackend()  # type: ignore[assignment]
        service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await first.create_grant(device_id="desktop-test")
    rec = await first.submit_task(AgentIntent(text="取消请求线性化", device_id="desktop-test"))
    assert rec.workflow_run_id is not None
    await first.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
    )

    original_record = second.record_task_event
    before_cancel_event = asyncio.Event()
    allow_cancel_event = asyncio.Event()

    async def controlled_record(event: EchoTaskEvent, **kwargs: Any) -> Any:
        if event.event == "task.cancel_requested":
            before_cancel_event.set()
            await allow_cancel_event.wait()
        return await original_record(event, **kwargs)

    cancel_calls = 0

    async def count_runner_cancel(_runner_task_id: str, *, operation_key: str) -> bool:
        nonlocal cancel_calls
        assert operation_key.startswith("agent-cancel-")
        cancel_calls += 1
        return True

    second.record_task_event = controlled_record  # type: ignore[method-assign]
    second.backend.cancel = count_runner_cancel  # type: ignore[method-assign]
    cancelling = asyncio.create_task(second.cancel_task(rec.task_id))
    await asyncio.wait_for(before_cancel_event.wait(), timeout=1.0)
    await first.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            event="task.completed",
            state="succeeded",
            message="取消请求后并发完成",
        )
    )
    allow_cancel_event.set()
    result = await asyncio.wait_for(cancelling, timeout=1.0)

    stored = await first.get_task(rec.task_id)
    run = await first.workflow.get_run(rec.workflow_run_id)
    events, snapshot, _last_seq = await first.list_events(rec.task_id)
    assert result is not None and result.state.value == "succeeded"
    assert stored is not None and stored.state.value == "succeeded"
    assert run is not None and run.state == "succeeded"
    assert cancel_calls == 0
    assert events[-1].event == "task.terminal_ignored"
    assert events[-1].visibility == "debug"
    assert snapshot["progress_text"] == "任务完成"
    async with aiosqlite.connect(first.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT COUNT(*) FROM agent_task_events
               WHERE task_id = ? AND projected_at IS NULL""",
            (rec.task_id,),
        )
        assert await cur.fetchone() == (0,)
        await cur.close()


@pytest.mark.unit
async def test_runner_cancel_without_local_request_projects_legal_workflow_path(
    tmp_path: Path,
) -> None:
    service, _bus = await _make_service(tmp_path)
    service.backend = _FakeBackend()  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")
    rec = await service.submit_task(AgentIntent(text="Runner 主动取消", device_id="desktop-test"))
    assert rec.workflow_run_id is not None
    await service.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.started", state="running")
    )
    await service.record_task_event(
        EchoTaskEvent(task_id=rec.task_id, event="task.cancelled", state="cancelled")
    )

    stored = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert stored is not None and stored.state.value == "cancelled"
    assert run is not None and run.state == "cancelled"
    assert run.cancel_requested_at is not None


@pytest.mark.unit
async def test_agent_timeout_projects_to_timeout_workflow_run(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")

    rec = await service.submit_task(
        AgentIntent(text="执行一个限时任务", device_id="desktop-test", timeout_s=0.05)
    )
    assert rec.workflow_run_id is not None
    await service.record_task_event(
        EchoTaskEvent(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            title=rec.title,
            event="task.timeout",
            state="timeout",
            message="任务超时",
        )
    )

    stored = await service.get_task(rec.task_id)
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert stored is not None
    assert stored.state.value == "timeout"
    assert run is not None
    assert run.state == "timeout"
    assert run.error == "任务超时"


@pytest.mark.unit
async def test_restore_repairs_terminal_agent_workflow_projection(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    rec = await service.submit_task(
        AgentIntent(text="恢复投影", device_id="desktop-test", title="恢复投影")
    )
    assert rec.workflow_run_id is not None
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET state = 'failed', error = 'runner crashed after durable event',
                   finished_at = 'now'
               WHERE task_id = ?""",
            (rec.task_id,),
        )
        await conn.commit()

    assert await service.restore_unfinished() == 0
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert run is not None
    assert run.state == "failed"
    assert run.error == "runner crashed after durable event"


@pytest.mark.unit
async def test_restore_repairs_cancelled_agent_workflow_projection(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    rec = await service.submit_task(
        AgentIntent(text="恢复取消投影", device_id="desktop-test", title="恢复取消投影")
    )
    assert rec.workflow_run_id is not None
    async with aiosqlite.connect(service.settings.db_path) as conn:
        await conn.execute(
            """UPDATE agent_tasks
               SET state = 'cancelled', finished_at = 'now'
               WHERE task_id = ?""",
            (rec.task_id,),
        )
        await conn.commit()

    assert await service.restore_unfinished() == 0
    run = await service.workflow.get_run(rec.workflow_run_id)
    assert run is not None
    assert run.state == "cancelled"
    assert run.cancel_requested_at is not None


@pytest.mark.unit
async def test_restore_resubmits_durable_agent_command_with_same_id(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    backend = _CrashOnceBackend()
    service.backend = backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")

    with pytest.raises(RuntimeError, match="AgentOS submit"):
        await service.submit_task(AgentIntent(text="幂等提交", device_id="desktop-test"))
    pending = (await service.list_tasks())[0]
    assert pending.state.value == "pending"
    assert pending.runner_task_id is None

    assert await service.restore_unfinished() == 1
    recovered = await service.get_task(pending.task_id)
    assert recovered is not None
    assert recovered.runner_task_id == "runner_recovered"
    assert [item.echo_task_id for item in backend.submissions] == [pending.task_id, pending.task_id]
    operation_keys = [item.runner_operation_key for item in backend.submissions]
    assert operation_keys[0] is not None
    assert operation_keys == [operation_keys[0], operation_keys[0]]
    assert operation_keys[0].startswith("agent-submit-")

    async with aiosqlite.connect(service.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT COUNT(*)
               FROM workflow_runs AS w
               LEFT JOIN agent_tasks AS a ON a.workflow_run_id = w.run_id
               WHERE w.kind = 'agent_task' AND a.task_id IS NULL"""
        )
        assert (await cur.fetchone())[0] == 0
        await cur.close()


@pytest.mark.unit
async def test_agent_task_and_workflow_rollback_together_before_commit(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    original_insert = service._insert_task_tx

    async def crash_after_both_rows(*args: Any, **kwargs: Any) -> Any:
        await original_insert(*args, **kwargs)
        raise RuntimeError("crash before Unit of Work commit")

    service._insert_task_tx = crash_after_both_rows  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Unit of Work commit"):
        await service.submit_task(
            AgentIntent(
                text="原子创建",
                device_id="desktop-test",
                echo_task_id="task-atomic-rollback",
            )
        )

    async with aiosqlite.connect(service.settings.db_path) as conn:
        for table in ("agent_tasks", "workflow_runs", "workflow_events", "workflow_outbox"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cur.fetchone())[0] == 0, table
            await cur.close()


@pytest.mark.unit
async def test_agent_submit_recovers_after_commit_before_outbox_flush(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    original_flush = service.workflow.flush_outbox
    flush_calls = 0

    async def fail_first_flush(*, limit: int = 500) -> int:
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == 1:
            raise RuntimeError("response lost after durable commit")
        return await original_flush(limit=limit)

    service.workflow.flush_outbox = fail_first_flush  # type: ignore[method-assign]
    intent = AgentIntent(
        text="恢复已提交创建",
        device_id="desktop-test",
        echo_task_id="task-commit-recovery",
    )
    with pytest.raises(RuntimeError, match="response lost"):
        await service.submit_task(intent)

    async with aiosqlite.connect(service.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM agent_tasks),
                   (SELECT COUNT(*) FROM workflow_runs),
                   (SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL)"""
        )
        assert await cur.fetchone() == (1, 1, 2)
        await cur.close()

    recovered = await service.submit_task(
        AgentIntent(
            text="恢复已提交创建",
            device_id="desktop-test",
            echo_task_id="task-commit-recovery",
        )
    )
    assert recovered.last_seq == 1
    assert recovered.workflow_run_id is not None
    async with aiosqlite.connect(service.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM agent_tasks),
                   (SELECT COUNT(*) FROM workflow_runs),
                   (SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL)"""
        )
        assert await cur.fetchone() == (1, 1, 0)
        await cur.close()


@pytest.mark.unit
async def test_agent_submit_is_idempotent_for_same_principal_and_task_id(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)

    first = await service.submit_task(
        AgentIntent(text="只创建一次", device_id="desktop-test", echo_task_id="task-replay")
    )
    second = await service.submit_task(
        AgentIntent(text="只创建一次", device_id="desktop-test", echo_task_id="task-replay")
    )

    assert second.task_id == first.task_id
    assert second.workflow_run_id == first.workflow_run_id
    assert second.last_seq == 1
    async with aiosqlite.connect(service.settings.db_path) as conn:
        cur = await conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM agent_tasks),
                   (SELECT COUNT(*) FROM workflow_runs),
                   (SELECT COUNT(*) FROM agent_task_events)"""
        )
        assert await cur.fetchone() == (1, 1, 1)
        await cur.close()


@pytest.mark.unit
async def test_same_task_id_is_isolated_across_principals(
    tmp_path: Path,
) -> None:
    service, _bus = await _make_service(tmp_path)
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token_a = bind_principal(principal_a)
    try:
        task_a = await service.submit_task(
            AgentIntent(text="A secret", device_id="forged", echo_task_id="task-collision")
        )
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        task_b = await service.submit_task(
            AgentIntent(text="B isolated", device_id="forged", echo_task_id="task-collision")
        )
        assert task_b.task_id == task_a.task_id
        assert task_b.device_id == "device-b"
        assert (await service.get_task(task_b.task_id)).intent_text == "B isolated"  # type: ignore[union-attr]
        assert len(await service.workflow.list_runs()) == 1
    finally:
        reset_principal(token_b)

    async with aiosqlite.connect(service.settings.db_path) as conn:
        cur = await conn.execute(
            "SELECT tenant_id, owner_id FROM workflow_runs ORDER BY tenant_id, owner_id"
        )
        assert await cur.fetchall() == [("tenant-a", "owner-a"), ("tenant-b", "owner-b")]
        await cur.close()
        cur = await conn.execute(
            "SELECT tenant_id, owner_id FROM agent_tasks ORDER BY tenant_id, owner_id"
        )
        assert await cur.fetchall() == [("tenant-a", "owner-a"), ("tenant-b", "owner-b")]
        await cur.close()


@pytest.mark.unit
async def test_agent_artifact_event_imports_unified_artifact(tmp_path: Path) -> None:
    async def handle_http(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read(4096)
        body = b"%PDF-1.4\nagent"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/pdf\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_http, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    service, _bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    fake_backend.base_url = f"http://127.0.0.1:{port}"
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")
    try:
        rec = await service.submit_task(AgentIntent(text="生成报告", device_id="desktop-test"))
        assert rec.workflow_run_id is not None

        await service.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.completed",
                state="succeeded",
                message="任务完成",
            )
        )
        await service.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.artifact_updated",
                state="running",
                message="产物已更新",
                artifacts=[
                    {
                        "name": "report.pdf",
                        "relpath": "out/report.pdf",
                        "kind": "pdf",
                        "url": f"/agents/tasks/{rec.task_id}/artifacts/out/report.pdf",
                    }
                ],
            )
        )

        artifacts = await service.artifact_repo.list_artifacts(limit=10)
        assert len(artifacts) == 1
        assert artifacts[0].metadata["source"] == "agent"
        assert artifacts[0].metadata["agent_task_id"] == rec.task_id
        assert Path(artifacts[0].file_path).read_bytes().startswith(b"%PDF")
        links = await service.artifact_repo.list_links_for_artifact(artifacts[0].artifact_id)
        assert links[0].source == "agent"
        assert links[0].run_id == rec.workflow_run_id
        stored = await service.get_task(rec.task_id)
        run = await service.workflow.get_run(rec.workflow_run_id)
        assert stored is not None
        assert stored.state.value == "succeeded"
        assert stored.artifacts[0]["relpath"] == "out/report.pdf"
        assert run is not None
        assert run.output["artifacts"][0]["relpath"] == "out/report.pdf"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.unit
async def test_agent_artifact_import_failure_isolated_from_later_item_and_terminal(
    tmp_path: Path,
) -> None:
    async def handle_http(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request = await reader.read(4096)
        if b"/bad.bin" in request:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n")
        else:
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_http, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    service, _bus = await _make_service(
        tmp_path,
        agent_artifact_proxy_max_bytes=1024 * 1024,
    )
    fake_backend = _FakeBackend()
    fake_backend.base_url = f"http://127.0.0.1:{port}"
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-test")
    try:
        rec = await service.submit_task(AgentIntent(text="生成两个产物", device_id="desktop-test"))
        assert rec.workflow_run_id is not None
        await service.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.artifact_updated",
                state="running",
                artifacts=[
                    {
                        "name": "bad.bin",
                        "relpath": "out/bad.bin",
                        "url": "https://example.invalid/?token=super-secret",
                    },
                    {
                        "name": "good.txt",
                        "relpath": "out/good.txt",
                    },
                ],
            )
        )
        await service.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.completed",
                state="succeeded",
                message="任务完成",
            )
        )

        artifacts = await service.artifact_repo.list_artifacts(limit=10)
        assert [item.title for item in artifacts] == ["good.txt"]
        assert Path(artifacts[0].file_path).read_bytes() == b"ok"
        assert list(service.settings.storage_dir.rglob("*.part")) == []
        workflow_events = await service.workflow.list_events(rec.workflow_run_id)
        failures = [
            event for event in workflow_events if event.event_type == "agent.artifact_import_failed"
        ]
        assert len(failures) == 1
        assert failures[0].payload == {
            "relpath": "out/bad.bin",
            "reason": "size_limit_exceeded",
        }
        assert "secret" not in json.dumps(failures[0].payload)
        assert "http" not in json.dumps(failures[0].payload)
        run = await service.workflow.get_run(rec.workflow_run_id)
        assert run is not None
        assert run.state == "succeeded"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.unit
async def test_resume_with_grant_submits_existing_waiting_task(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    service.backend = fake_backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    waiting = await service.submit_task(
        AgentIntent(text="创建一个 markdown 文件", device_id="desktop-test", title="创建文件")
    )
    grant: AgentRunnerGrant = await service.create_grant(device_id="desktop-test")
    resumed = await service.resume_with_grant(waiting.task_id, grant)

    assert resumed.state.value == "pending"
    assert resumed.runner_task_id == "runner_fake"
    assert resumed.grant_id == grant.grant_id
    assert fake_backend.submissions[0].echo_task_id == waiting.task_id
    events, snapshot, last_seq = await service.list_events(waiting.task_id)
    assert last_seq == 2
    assert [event.event for event in events] == ["task.permission_required", "task.queued"]
    assert snapshot["progress_text"] == "任务已提交，等待执行"


@pytest.mark.unit
async def test_resume_with_grant_rejects_other_device(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    fake_backend = _FakeBackend()
    service.backend = fake_backend  # type: ignore[assignment]

    waiting = await service.submit_task(
        AgentIntent(text="创建一个 markdown 文件", device_id="desktop-a", title="创建文件")
    )
    grant: AgentRunnerGrant = await service.create_grant(device_id="desktop-b")

    with pytest.raises(PermissionError):
        await service.resume_with_grant(waiting.task_id, grant)

    assert fake_backend.submissions == []
    unchanged = await service.get_task(waiting.task_id)
    assert unchanged is not None
    assert unchanged.state.value == "waiting_permission"


@pytest.mark.unit
async def test_agent_grants_tasks_and_events_are_principal_scoped(tmp_path: Path) -> None:
    service, _bus = await _make_service(tmp_path)
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")
    await seed_principal_identity(service.settings.db_path, principal_a, principal_b)

    token_a = bind_principal(principal_a)
    try:
        grant = await service.create_grant(device_id="forged-device")
        assert grant.device_id == "device-a"
        task = await service.record_permission_required(
            AgentIntent(
                text="A secret agent task",
                device_id="forged-device",
                echo_task_id="task-shared",
            ),
            workflow_run_id=None,
        )
        assert task.device_id == "device-a"
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        assert await service.get_active_grant(device_id="device-a") is None
        assert await service.revoke_grant(grant.grant_id) is False
        assert await service.get_task(task.task_id) is None
        assert await service.list_tasks() == []
        assert await service.list_events(task.task_id) == ([], {}, 0)
        assert await service.cancel_task(task.task_id) is None
        task_b = await service.record_permission_required(
            AgentIntent(
                text="B isolated",
                device_id="forged-device",
                echo_task_id="task-shared",
            ),
            workflow_run_id=None,
        )
        assert task_b.device_id == "device-b"
        assert (await service.get_task(task_b.task_id)).intent_text == "B isolated"  # type: ignore[union-attr]
    finally:
        reset_principal(token_b)

    token_a = bind_principal(principal_a)
    try:
        assert await service.get_task(task.task_id) is not None
        events, _snapshot, last_seq = await service.list_events(task.task_id)
        assert last_seq == 1
        assert [event.event for event in events] == ["task.permission_required"]
    finally:
        reset_principal(token_a)
