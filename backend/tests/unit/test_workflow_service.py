from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.schemas.workflow import WorkflowRunCreate
from app.workflows.service import WorkflowService


async def _service(tmp_path: Path) -> tuple[WorkflowService, InMemoryEventBus]:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    bus = InMemoryEventBus()
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    return WorkflowService(settings, bus), bus


@pytest.mark.unit
async def test_workflow_lifecycle_records_events_and_snapshots(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)

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
async def test_restore_unfinished_replays_non_terminal_runs(tmp_path: Path) -> None:
    service, bus = await _service(tmp_path)
    pending = await service.create_run(
        WorkflowRunCreate(kind="agent.task", source="agent", intent_text="继续任务")
    )
    finished = await service.create_run(
        WorkflowRunCreate(kind="artifact.generate", source="artifact_api", intent_text="完成任务")
    )
    await service.complete_run(finished.run_id)
    before = bus.max_seq

    restored = await service.restore_unfinished()

    assert restored == 1
    assert bus.max_seq > before
    events = await service.list_events(pending.run_id)
    assert events[-1].event_type == "workflow.restored"
