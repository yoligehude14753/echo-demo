"""B10 C-owned deterministic vertical contract evidence.

This harness models only the embedded worker's typed event sink.  Production
runtime wiring remains owned by B10 A; the test deliberately enters the
authoritative backend through ``AgentTaskService.record_task_event``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentTaskService
from app.config import Settings


class _DeterministicEmbeddedBackend:
    """Minimal backend-side port used to create one task-owned runtime turn."""

    enabled = True
    base_url = "embedded://b10-test"

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str]] = []

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_b10",
            accepted=True,
            provider="embedded-worker",
            runner_task_id="embedded-runner-b10",
            runner_base_url=self.base_url,
        )

    async def cancel(self, runner_task_id: str, *, operation_key: str) -> bool:
        self.cancel_calls.append((runner_task_id, operation_key))
        return True


@dataclass(frozen=True, slots=True)
class _WorkerEvent:
    raw_identity: str
    event: EchoTaskEvent


class _DeterministicEmbeddedWorker:
    """Emit a stable trace, including an intentional raw duplicate and late terminal."""

    def __init__(self, events: list[_WorkerEvent]) -> None:
        self.events = events

    async def run(
        self,
        sink: Callable[..., Awaitable[EchoTaskEvent | None]],
    ) -> None:
        for item in self.events:
            await sink(
                item.event,
                raw_hash=item.raw_identity,
                raw_kind="embedded.worker.event",
            )


async def _make_service(tmp_path: Path) -> AgentTaskService:
    db_path = tmp_path / "b10-vertical.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        agent_os_enabled=False,
    )
    return AgentTaskService(settings, InMemoryEventBus())


@pytest.mark.unit
async def test_b10_vertical_worker_event_state_contract(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    backend = _DeterministicEmbeddedBackend()
    service.backend = backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-b10")

    task = await service.submit_task(
        AgentIntent(
            text="B10 deterministic turn",
            device_id="desktop-b10",
            title="B10 vertical",
        )
    )
    worker = _DeterministicEmbeddedWorker(
        [
            _WorkerEvent(
                "raw-start",
                EchoTaskEvent(
                    task_id=task.task_id,
                    runner_task_id=task.runner_task_id,
                    event="task.started",
                    state="running",
                    message="embedded worker started",
                ),
            ),
            _WorkerEvent(
                "raw-text",
                EchoTaskEvent(
                    task_id=task.task_id,
                    runner_task_id=task.runner_task_id,
                    event="task.text_delta",
                    state="running",
                    text_delta="hello",
                ),
            ),
            _WorkerEvent(
                "raw-text",
                EchoTaskEvent(
                    task_id=task.task_id,
                    runner_task_id=task.runner_task_id,
                    event="task.text_delta",
                    state="running",
                    text_delta="hello",
                ),
            ),
            _WorkerEvent(
                "raw-terminal",
                EchoTaskEvent(
                    task_id=task.task_id,
                    runner_task_id=task.runner_task_id,
                    event="task.completed",
                    state="succeeded",
                    message="embedded worker completed",
                ),
            ),
            _WorkerEvent(
                "raw-late-terminal",
                EchoTaskEvent(
                    task_id=task.task_id,
                    runner_task_id=task.runner_task_id,
                    event="task.cancelled",
                    state="cancelled",
                    message="late cancellation",
                ),
            ),
        ]
    )

    async def sink(
        event: EchoTaskEvent,
        *,
        raw_hash: str,
        raw_kind: str,
    ) -> EchoTaskEvent | None:
        return await service.record_task_event(
            event,
            raw_hash=raw_hash,
            raw_kind=raw_kind,
        )

    await worker.run(sink)

    stored = await service.get_task(task.task_id)
    assert stored is not None
    assert stored.state.value == "succeeded"
    assert stored.snapshot["text_buffer"] == "hello"
    assert stored.snapshot["final_text"] == "embedded worker completed"

    assert task.workflow_run_id is not None
    workflow = await service.workflow.get_run(task.workflow_run_id)
    assert workflow is not None
    assert workflow.state == "succeeded"

    events, _snapshot, last_seq = await service.list_events(task.task_id)
    assert [event.seq for event in events] == list(range(1, last_seq + 1))
    assert [event.event for event in events].count("task.text_delta") == 1
    assert events[-1].event == "task.terminal_ignored"
    assert events[-1].visibility == "debug"
    assert events[-1].state == "succeeded"

    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT raw_event_hash FROM agent_task_events
                   WHERE task_id = ? AND raw_event_hash IS NOT NULL
                   ORDER BY seq""",
                (task.task_id,),
            )
        ).fetchall()
    assert [row[0] for row in rows] == [
        "raw-start",
        "raw-text",
        "raw-terminal",
        "raw-late-terminal",
    ]


@pytest.mark.unit
async def test_b10_cancel_outbox_operation_key_is_idempotent(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    backend = _DeterministicEmbeddedBackend()
    service.backend = backend  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]
    await service.create_grant(device_id="desktop-b10")

    task = await service.submit_task(
        AgentIntent(text="B10 cancel", device_id="desktop-b10", title="B10 cancel")
    )
    cancelled = await service.cancel_task(task.task_id)
    repeated = await service.cancel_task(task.task_id)

    assert cancelled is not None and cancelled.state.value == "cancelled"
    assert repeated is not None and repeated.state.value == "cancelled"
    assert len(backend.cancel_calls) == 1
    assert backend.cancel_calls[0][1].startswith("agent-cancel-")

    assert task.workflow_run_id is not None
    workflow = await service.workflow.get_run(task.workflow_run_id)
    assert workflow is not None and workflow.state == "cancelled"

    async with aiosqlite.connect(str(service.settings.db_path)) as conn:
        rows = await (
            await conn.execute(
                """SELECT operation_key, outcome, completed_at
                   FROM agent_command_outbox
                   WHERE task_id = ?""",
                (task.task_id,),
            )
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == backend.cancel_calls[0][1]
    assert rows[0][1] == "cancelled"
    assert rows[0][2] is not None
