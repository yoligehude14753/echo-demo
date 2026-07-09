"""AgentTaskService 单测：授权、事件持久化、去重与 replay。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.events import EchoTaskEvent
from app.agents.service import AgentRunnerGrant, AgentTaskService
from app.api.agents import _encode_agentos_artifact_path
from app.config import Settings
from fastapi import HTTPException


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

    async def cancel(self, _runner_task_id: str) -> bool:
        return True


async def _make_service(tmp_path: Path, **settings_overrides: Any) -> tuple[AgentTaskService, InMemoryEventBus]:
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
        published = await agen.__anext__()
    finally:
        await agen.aclose()
    assert published.type == "agent.task.event"
    assert published.payload["event"] == "task.permission_required"


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
