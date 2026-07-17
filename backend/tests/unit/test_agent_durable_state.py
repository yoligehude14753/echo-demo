"""B10 durable event/state-machine focused contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.adapters.repo.migrator import run_migrations
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.durable_state import DurableEventStateMachine, raw_event_hash
from app.agents.service import AgentTaskService
from app.config import Settings


def _raw_event(
    *,
    event_id: str = "runtime-event-1",
    occurred_at: str = "2026-07-15T00:00:00Z",
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "taskId": "task-durable",
        "operationKey": "operation-durable",
        "runtimeEventId": event_id,
        "occurredAt": occurred_at,
        "type": "agent.message.delta",
        "payload": {"text": "hello"},
    }


@pytest.mark.unit
def test_raw_event_hash_excludes_observation_time_but_keeps_identity() -> None:
    first = raw_event_hash(_raw_event(occurred_at="2026-07-15T00:00:00Z"))
    replay = raw_event_hash(_raw_event(occurred_at="2026-07-15T00:00:02Z"))
    distinct = raw_event_hash(_raw_event(event_id="runtime-event-2"))

    assert first == replay
    assert first != distinct


@pytest.mark.unit
def test_durable_state_machine_dedupes_without_allocating_second_seq() -> None:
    machine = DurableEventStateMachine(
        last_seq=4,
        current_state="running",
    )

    first = machine.admit(_raw_event(), incoming_state="running")
    duplicate = machine.admit(
        _raw_event(occurred_at="2026-07-15T00:00:03Z"),
        incoming_state="running",
    )

    assert (first.durable_seq, first.duplicate, first.audit_only) == (5, False, False)
    assert (duplicate.durable_seq, duplicate.duplicate, duplicate.audit_only) == (5, True, False)
    assert machine.last_seq == 5


@pytest.mark.unit
def test_first_terminal_wins_and_late_terminals_are_audit_only() -> None:
    machine = DurableEventStateMachine(last_seq=0, current_state="running")

    success = machine.admit(
        _raw_event(event_id="terminal-success"),
        incoming_state="succeeded",
    )
    late_failure = machine.admit(
        _raw_event(event_id="terminal-failure"),
        incoming_state="failed",
    )
    repeated_success = machine.admit(
        _raw_event(event_id="terminal-success-2"),
        incoming_state="succeeded",
    )

    assert success.effective_state == "succeeded"
    assert success.audit_only is False
    assert late_failure.audit_only is True
    assert repeated_success.audit_only is True
    assert late_failure.effective_state == repeated_success.effective_state == "succeeded"
    assert machine.last_seq == 3


class _SubmitBackend:
    enabled = True
    base_url = "embedded://test"

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        return AgentSubmitResult(
            task_id=intent.echo_task_id or "echo_task_durable",
            accepted=True,
            provider="embedded",
            runner_task_id="runner-durable",
            runner_base_url=self.base_url,
        )


@pytest.mark.unit
async def test_completed_cancel_outbox_rejects_late_submit_requeue(tmp_path: Path) -> None:
    db_path = tmp_path / "durable-state.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    service = AgentTaskService(
        Settings(
            db_path=db_path,
            storage_dir=tmp_path / "storage",
            skill_executor_build_dir=tmp_path / "skill-build",
            agent_os_enabled=False,
        ),
        InMemoryEventBus(),
        holder_id="durable-state-test",
    )
    service.backend = _SubmitBackend()  # type: ignore[assignment]
    service.start_bridge_for_task = lambda _rec: None  # type: ignore[method-assign]

    await service.create_grant(device_id="desktop-test")
    task = await service.submit_task(AgentIntent(text="durable cancel", device_id="desktop-test"))

    async def no_dispatch(**_kwargs: Any) -> int:
        return 0

    service.recover_cancel_commands_once = no_dispatch  # type: ignore[method-assign]
    await service.cancel_task(task.task_id)
    commands = await service._command_outbox.list_due(
        tenant_id=task.tenant_id,
        owner_id=task.owner_id,
        task_id=task.task_id,
    )
    assert len(commands) == 1
    command = commands[0]
    lease = await service._lease_store.acquire(
        tenant_id=command.tenant_id,
        owner_id=command.owner_id,
        resource_kind="agent_command",
        resource_id=command.command_id,
        holder_id=service._holder_id,
        ttl_seconds=5.0,
    )
    assert lease is not None
    try:
        assert await service._command_outbox.mark_completed(
            command,
            lease,
            outcome="terminal_won",
        ) is True
        async with open_aiosqlite_connection(db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            assert await service._command_outbox.attach_runner_and_requeue_cancel_in_transaction(
                conn,
                tenant_id=command.tenant_id,
                owner_id=command.owner_id,
                task_id=command.task_id,
                runner_task_id="late-runner",
            ) is False
            await conn.commit()
    finally:
        await service._lease_store.release(lease)

    assert await service._command_outbox.list_due(
        tenant_id=command.tenant_id,
        owner_id=command.owner_id,
        task_id=command.task_id,
    ) == []
