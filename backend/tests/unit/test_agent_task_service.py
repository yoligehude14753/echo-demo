"""AgentTaskService 单测：授权、事件持久化、去重与 replay。"""

from __future__ import annotations

import asyncio
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
