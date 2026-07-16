from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.agents.base import AgentIntent
from app.agents.events import EchoTaskEvent
from app.agents.service import get_agent_task_service
from app.api import artifacts as artifacts_api
from app.api import capture as capture_api
from app.api import deps as deps_mod
from app.api import retrieval as retrieval_api
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.config_io import user_config_dir
from app.main import create_app
from app.schemas.artifact import GeneratedArtifact
from app.schemas.capture import CaptureChunkResult
from app.schemas.events import EchoEvent
from app.schemas.meeting import MeetingMinutes
from app.schemas.workflow import WorkflowRunCreate
from app.security.access import AccessPolicy
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    PUBLIC_CLIENT_VERSION_HEADER,
)
from app.security.context import bind_principal, reset_principal
from app.security.models import Principal
from app.security.public_projection import project_client_dict, server_private_roots
from app.security.scope import scoped_directory, scoped_directory_for
from app.workflows.service import WorkflowService
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def public_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "public.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        debug_token="test-admin",
        quota_storage_bytes=32 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = asyncio.run(run_migrations(settings.db_path))
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    deps_mod.reset_deps_for_test()
    with TestClient(
        app,
        headers={PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION},
    ) as client:
        yield client


def _enrollment_payload(label: str) -> dict[str, str]:
    return {
        "enrollment_id": f"enrollment-{label}-" + "e" * 40,
        "device_secret": f"device-{label}-" + "s" * 40,
    }


def _issue(client: TestClient, label: str) -> tuple[dict[str, object], str]:
    payload = _enrollment_payload(label)
    response = client.post("/session", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload["device_secret"]


def _assert_private_no_store(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.unit
def test_public_text_projection_redacts_configured_roots_but_preserves_user_paths() -> None:
    settings = Settings(
        storage_dir=Path("/Volumes/EchoServerData"),
        _env_file=None,  # type: ignore[call-arg]
    )
    public = Principal(
        tenant_id="tenant",
        device_id="device",
        owner_id="owner",
        session_id="session",
        mode="public",
    )
    local = Principal(
        tenant_id="legacy-local",
        device_id="legacy-local",
        owner_id="legacy-local",
        session_id="local",
        mode="local",
    )
    payload = {
        "state": "succeeded",
        "final_text": (
            "assistant saved /Volumes/EchoServerData/private/result.txt "
            "for user reference /user/request.md"
        ),
        "file_uri": "open file:///Volumes/EchoServerData/private/result.txt",
        "colon_path": "trace:/Users/server/private/result.txt",
        "web_url": "https://home.example/public/result.txt",
    }

    projected = project_client_dict(
        payload,
        public,
        private_roots=server_private_roots(settings),
    )

    assert projected["final_text"] == (
        "assistant saved [SERVER_PATH] for user reference /user/request.md"
    )
    assert projected["file_uri"] == "open [SERVER_PATH]"
    assert projected["colon_path"] == "trace:[SERVER_PATH]"
    assert projected["web_url"] == "https://home.example/public/result.txt"
    assert (
        project_client_dict(
            payload,
            local,
            private_roots=server_private_roots(settings),
        )
        == payload
    )


async def _seed_public_path_privacy_probe(
    settings: Settings,
    principal_payload: dict[str, object],
) -> tuple[str, str, str, str, int, str]:
    principal = Principal(
        tenant_id=str(principal_payload["tenant_id"]),
        device_id=str(principal_payload["device_id"]),
        owner_id=str(principal_payload["owner_id"]),
        session_id=str(principal_payload["session_id"]),
        family_id=str(principal_payload["family_id"]),
        mode="public",
    )
    context_token = bind_principal(principal)
    repository = SQLiteRepository(settings.db_path)
    workflow_service = WorkflowService(settings, deps_mod.get_event_bus())
    artifact_repo = ArtifactRepository(settings)
    artifact_path = str(settings.skill_executor_build_dir / "private" / "output.pdf")
    transcript_path = str(settings.storage_dir / "private" / "meeting-transcript.json")
    meeting_id = "meeting-path-privacy"
    try:
        await repository.init()
        now = datetime.now(UTC)
        await repository.create_meeting(meeting_id, started_at=now, title="路径隐私")
        minutes = MeetingMinutes(
            meeting_id=meeting_id,
            title="路径隐私",
            duration_sec=12,
            summary="public projection probe",
            raw_transcript_ref=transcript_path,
            created_at=now,
        )
        await repository.update_meeting_state(
            meeting_id,
            state="finalized",
            ended_at=now,
            finalized_at=now,
            minutes_json=minutes.model_dump_json(),
            raw_transcript_ref=transcript_path,
            minutes_status="ok",
            display_title=minutes.title,
        )
        Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
        Path(artifact_path).write_bytes(b"private-pdf\n")
        artifact = GeneratedArtifact(
            artifact_id="artifact-path-privacy",
            artifact_type="pdf",
            title="路径隐私",
            file_path=artifact_path,
            mime_type="application/pdf",
            size_bytes=12,
            generation_latency_ms=1,
            model="test",
            metadata={"original_build_dir": str(Path(artifact_path).parent)},
        )
        await artifact_repo.save_artifact(artifact)
        await artifact_repo.link_artifact(
            artifact_id=artifact.artifact_id,
            source="meeting",
            meeting_id=meeting_id,
        )

        run = await workflow_service.create_run(
            WorkflowRunCreate(
                kind="artifact.generate",
                source="privacy-test",
                intent_text="verify public path projection",
                meeting_id=meeting_id,
                input={"artifact": artifact.model_dump(mode="json")},
            )
        )
        await workflow_service.start_run(run.run_id)
        await workflow_service.record_event(
            run.run_id,
            "privacy.path.probe",
            message="正常进度：用户选择 /user/reference.md",
            payload={
                "artifact": artifact.model_dump(mode="json"),
                "minutes": minutes.model_dump(mode="json"),
                "source_path": artifact_path,
                "audio_ref": transcript_path,
            },
        )
        await workflow_service.complete_run(
            run.run_id,
            output={
                "artifact": artifact.model_dump(mode="json"),
                "minutes": minutes.model_dump(mode="json"),
            },
        )
        await workflow_service.merge_output(
            run.run_id,
            {
                "file_cleanup_errors": {artifact.artifact_id: "file cleanup failed"},
                "file_cleanup_targets": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "root": "skill_build",
                        "relative_path": "private/output.pdf",
                    }
                ],
            },
        )

        failed_meeting_id = "meeting-failure-path-privacy"
        await repository.create_meeting(
            failed_meeting_id,
            started_at=now + timedelta(seconds=1),
            title="失败路径隐私",
        )
        await repository.update_meeting_state(
            failed_meeting_id,
            state="ended",
            ended_at=now + timedelta(seconds=2),
            minutes_status="generation_failed",
            minutes_error=f"minutes provider failed for {transcript_path}",
        )
        failed_run = await workflow_service.create_run(
            WorkflowRunCreate(
                kind="artifact.generate",
                source="privacy-test",
                intent_text="verify public failure projection",
                meeting_id=failed_meeting_id,
            )
        )
        await workflow_service.start_run(failed_run.run_id)
        event_bus = deps_mod.get_event_bus()
        replay_after_seq = event_bus.stream_state_for_current_scope().max_seq
        # A replay window may legitimately include unrelated events from the
        # same principal.  Keep more than the former eight-frame test budget in
        # front of the probes so this contract cannot regress to frame counting.
        for noise_index in range(9):
            await event_bus.publish(
                EchoEvent(
                    type="rag.query",
                    payload={"privacy_noise_index": noise_index},
                )
            )
        failure_error = f"provider failed while opening {artifact_path}"
        await workflow_service.fail_run(
            failed_run.run_id,
            error=failure_error,
            payload={"error": failure_error},
        )
        await event_bus.publish(
            EchoEvent(
                type="artifact.ready",
                meeting_id=meeting_id,
                payload={
                    "privacy_probe": True,
                    "artifact": artifact.model_dump(mode="json"),
                    "minutes": minutes.model_dump(mode="json"),
                },
            )
        )
        await event_bus.publish(
            EchoEvent(
                type="minutes.ready",
                meeting_id=meeting_id,
                payload={
                    "privacy_probe": True,
                    **minutes.model_dump(mode="json"),
                },
            )
        )
        return (
            run.run_id,
            failed_run.run_id,
            artifact_path,
            transcript_path,
            replay_after_seq,
            "正常进度：用户选择 /user/reference.md",
        )
    finally:
        await workflow_service.aclose()
        await repository.aclose()
        reset_principal(context_token)


async def _seed_public_agent_failure_probe(
    settings: Settings,
    principal_payload: dict[str, object],
) -> tuple[str, str, str, str, int, str, str]:
    principal = Principal(
        tenant_id=str(principal_payload["tenant_id"]),
        device_id=str(principal_payload["device_id"]),
        owner_id=str(principal_payload["owner_id"]),
        session_id=str(principal_payload["session_id"]),
        family_id=str(principal_payload["family_id"]),
        mode="public",
    )
    context_token = bind_principal(principal)
    service = get_agent_task_service(settings, deps_mod.get_event_bus())
    settings.workspace_dirs = "/Volumes/Corp"
    server_path = "/Volumes/Corp/private-agent-workspace/result.txt"
    user_text = "请处理用户引用 /user/request.md"
    final_text = "成功结果保留 /user/final.md"
    try:
        failed = await service.submit_task(
            AgentIntent(text=user_text, device_id=principal.device_id, title="失败隐私探针")
        )
        assert failed.workflow_run_id is not None
        replay_after_seq = deps_mod.get_event_bus().stream_state_for_current_scope().max_seq
        failure = f"runner failed while reading {server_path}"
        artifact = {
            "name": "result.txt",
            "relpath": "out/result.txt",
            "file_path": server_path,
        }
        await service.record_task_event(
            EchoTaskEvent(
                task_id=failed.task_id,
                runner_task_id=failed.runner_task_id,
                event="task.message",
                state="running",
                message=f"assistant draft saved at {server_path}",
            )
        )
        await service.record_task_event(
            EchoTaskEvent(
                task_id=failed.task_id,
                runner_task_id=failed.runner_task_id,
                event="task.artifact_updated",
                state="running",
                message="产物已更新",
                artifacts=[artifact],
                raw_ref=server_path,
            )
        )
        await service.record_task_event(
            EchoTaskEvent(
                task_id=failed.task_id,
                runner_task_id=failed.runner_task_id,
                event="task.failed",
                state="failed",
                message=failure,
                artifacts=[artifact],
                snapshot={
                    "status": "failed",
                    "progress_text": failure,
                    "error": failure,
                    "artifacts": [artifact],
                },
                raw_ref=server_path,
            )
        )

        succeeded = await service.submit_task(
            AgentIntent(text=user_text, device_id=principal.device_id, title="成功文本探针")
        )
        await service.record_task_event(
            EchoTaskEvent(
                task_id=succeeded.task_id,
                runner_task_id=succeeded.runner_task_id,
                event="task.completed",
                state="succeeded",
                message=final_text,
            )
        )
        return (
            failed.task_id,
            failed.workflow_run_id,
            succeeded.task_id,
            server_path,
            replay_after_seq,
            user_text,
            final_text,
        )
    finally:
        reset_principal(context_token)


async def _seed_public_corrupt_artifact_paths(
    settings: Settings,
    owner_payload: dict[str, object],
    other_payload: dict[str, object],
) -> tuple[str, str, str | None, str, str]:
    principal = Principal(
        tenant_id=str(owner_payload["tenant_id"]),
        device_id=str(owner_payload["device_id"]),
        owner_id=str(owner_payload["owner_id"]),
        session_id=str(owner_payload["session_id"]),
        family_id=str(owner_payload["family_id"]),
        mode="public",
    )
    context_token = bind_principal(principal)
    repository = SQLiteRepository(settings.db_path)
    artifact_repo = ArtifactRepository(settings)
    meeting_id = "meeting-corrupt-artifact-paths"
    cross_artifact_id = "artifact-cross-owner-path"
    symlink_artifact_id: str | None = "artifact-symlink-path"
    other_file = (
        scoped_directory_for(
            settings.storage_dir,
            str(other_payload["tenant_id"]),
            str(other_payload["owner_id"]),
        )
        / "private-other-owner.txt"
    )
    owner_real_file = (
        scoped_directory_for(settings.storage_dir, principal.tenant_id, principal.owner_id)
        / "real-owner-file.txt"
    )
    try:
        await repository.init()
        await repository.create_meeting(
            meeting_id,
            started_at=datetime.now(UTC),
            title="损坏产物路径",
        )
        other_file.parent.mkdir(parents=True, exist_ok=True)
        other_file.write_text("OTHER_OWNER_PRIVATE_BYTES", encoding="utf-8")
        owner_real_file.parent.mkdir(parents=True, exist_ok=True)
        owner_real_file.write_text("SYMLINK_TARGET_PRIVATE_BYTES", encoding="utf-8")
        symlink = owner_real_file.with_name("linked-owner-file.txt")
        try:
            symlink.symlink_to(owner_real_file)
        except OSError:
            symlink_artifact_id = None

        artifacts = [
            GeneratedArtifact(
                artifact_id=cross_artifact_id,
                artifact_type="txt",
                title="cross owner",
                file_path=str(other_file),
                mime_type="text/plain",
                size_bytes=other_file.stat().st_size,
                generation_latency_ms=0,
                model="test",
            )
        ]
        if symlink_artifact_id is not None:
            artifacts.append(
                GeneratedArtifact(
                    artifact_id=symlink_artifact_id,
                    artifact_type="txt",
                    title="symlink",
                    file_path=str(symlink),
                    mime_type="text/plain",
                    size_bytes=owner_real_file.stat().st_size,
                    generation_latency_ms=0,
                    model="test",
                )
            )
        for artifact in artifacts:
            await artifact_repo.save_artifact(artifact)
            await artifact_repo.link_artifact(
                artifact_id=artifact.artifact_id,
                source="meeting",
                meeting_id=meeting_id,
            )
        return (
            meeting_id,
            cross_artifact_id,
            symlink_artifact_id,
            str(other_file),
            str(owner_real_file),
        )
    finally:
        await repository.aclose()
        reset_principal(context_token)


@pytest.mark.unit
@pytest.mark.timeout(30)
def test_public_http_workflow_and_websocket_redact_server_paths(  # noqa: PLR0915 - one cross-transport privacy contract
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "path-privacy")
    principal = session["principal"]
    assert isinstance(principal, dict)
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    assert public_client.portal is not None
    (
        run_id,
        failed_run_id,
        artifact_path,
        transcript_path,
        replay_after_seq,
        normal_progress_message,
    ) = public_client.portal.call(_seed_public_path_privacy_probe, settings, principal)
    headers = {"Authorization": f"Bearer {session['token']}"}

    artifacts = public_client.get("/artifacts", headers=headers)
    meeting_artifacts = public_client.get(
        "/meetings/meeting-path-privacy/artifacts",
        headers=headers,
    )
    minutes = public_client.get(
        "/meetings/meeting-path-privacy/minutes",
        headers=headers,
    )
    workflow = public_client.get(f"/workflows/runs/{run_id}", headers=headers)
    workflow_events = public_client.get(
        f"/workflows/runs/{run_id}/events",
        headers=headers,
    )
    failed_workflow = public_client.get(
        f"/workflows/runs/{failed_run_id}",
        headers=headers,
    )
    failed_workflow_events = public_client.get(
        f"/workflows/runs/{failed_run_id}/events",
        headers=headers,
    )
    current_meeting = public_client.get("/meetings/current", headers=headers)
    responses = (
        artifacts,
        meeting_artifacts,
        minutes,
        workflow,
        workflow_events,
        failed_workflow,
        failed_workflow_events,
        current_meeting,
    )
    assert [response.status_code for response in responses] == [200] * len(responses)
    for response in responses:
        serialized = json.dumps(response.json(), ensure_ascii=False)
        assert artifact_path not in serialized
        assert transcript_path not in serialized

    assert artifacts.json()[0]["file_path"] is None
    assert "original_build_dir" not in artifacts.json()[0]["metadata"]
    assert meeting_artifacts.json()[0]["file_path"] is None
    assert minutes.json()["raw_transcript_ref"] is None
    assert workflow.json()["output"]["artifact"]["file_path"] is None
    assert "original_build_dir" not in workflow.json()["output"]["artifact"]["metadata"]
    assert workflow.json()["output"]["file_cleanup_errors"] == {
        "artifact-path-privacy": "产物文件清理失败"
    }
    assert workflow.json()["output"]["file_cleanup_targets"][0]["relative_path"] is None
    probe = next(
        event
        for event in workflow_events.json()["events"]
        if event["event_type"] == "privacy.path.probe"
    )
    assert probe["payload"]["minutes"]["raw_transcript_ref"] is None
    assert probe["payload"]["source_path"] is None
    assert probe["payload"]["audio_ref"] is None
    assert probe["message"] == normal_progress_message
    assert failed_workflow.json()["error"] == "操作失败，请重试"
    failed_event = next(
        event
        for event in failed_workflow_events.json()["events"]
        if event["event_type"] == "workflow.failed"
    )
    assert failed_event["message"] == "操作失败，请重试"
    assert failed_event["payload"]["error"] == "操作失败，请重试"
    assert current_meeting.json()["minutes_status"] == "generation_failed"
    assert current_meeting.json()["minutes_error"] == "操作失败，请重试"

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": replay_after_seq,
                "client_version": "0.3.3",
                "auth": {"type": "bearer", "token": session["token"]},
            }
        )
        server_hello = ws.receive_json()
        assert server_hello["type"] == "server_hello"
        replay_fence = server_hello["payload"]["max_seq"]
        assert isinstance(replay_fence, int)
        expected_replay = replay_fence - replay_after_seq
        assert 8 < expected_replay <= settings.ws_replay_buffer_size
        probes: dict[str, dict[str, object]] = {}
        last_replayed_seq = replay_after_seq
        while last_replayed_seq < replay_fence:
            event = ws.receive_json()
            event_seq = event.get("seq")
            if event_seq is None:
                assert event["type"] == "server_ping"
                continue
            assert isinstance(event_seq, int)
            assert event_seq == last_replayed_seq + 1
            last_replayed_seq = event_seq
            if event["type"] in {"artifact.ready", "minutes.ready"} and event["payload"].get(
                "privacy_probe"
            ):
                probes[event["type"]] = event
            if (
                event["type"] in {"workflow.event", "workflow.snapshot"}
                and event["payload"].get("run_id") == failed_run_id
            ):
                probes[event["type"]] = event
        assert probes.keys() == {
            "artifact.ready",
            "minutes.ready",
            "workflow.event",
            "workflow.snapshot",
        }, "public path/failure privacy probes were not replayed through the fenced seq window"
    serialized_events = json.dumps(probes, ensure_ascii=False)
    assert artifact_path not in serialized_events
    assert transcript_path not in serialized_events
    assert probes["artifact.ready"]["payload"]["artifact"]["file_path"] is None
    assert "original_build_dir" not in probes["artifact.ready"]["payload"]["artifact"]["metadata"]
    assert probes["minutes.ready"]["payload"]["raw_transcript_ref"] is None
    assert probes["workflow.event"]["payload"]["message"] == "操作失败，请重试"
    assert probes["workflow.event"]["payload"]["payload"]["error"] == "操作失败，请重试"
    assert probes["workflow.snapshot"]["payload"]["error"] == "操作失败，请重试"


@pytest.mark.unit
def test_public_agent_history_and_websocket_project_private_failure_details(  # noqa: PLR0915 - cross-transport privacy contract
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "agent-path-privacy")
    principal = session["principal"]
    assert isinstance(principal, dict)
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    assert public_client.portal is not None
    (
        failed_task_id,
        workflow_run_id,
        succeeded_task_id,
        server_path,
        replay_after_seq,
        user_text,
        final_text,
    ) = public_client.portal.call(_seed_public_agent_failure_probe, settings, principal)
    headers = {"Authorization": f"Bearer {session['token']}"}

    failed_response = public_client.get(f"/agents/tasks/{failed_task_id}", headers=headers)
    succeeded_response = public_client.get(f"/agents/tasks/{succeeded_task_id}", headers=headers)
    list_response = public_client.get("/agents/tasks", headers=headers)
    events_response = public_client.get(
        f"/agents/tasks/{failed_task_id}/events",
        headers=headers,
    )
    workflow_events_response = public_client.get(
        f"/workflows/runs/{workflow_run_id}/events",
        headers=headers,
    )
    responses = (
        failed_response,
        succeeded_response,
        list_response,
        events_response,
        workflow_events_response,
    )
    assert [response.status_code for response in responses] == [200] * len(responses)
    for response in responses:
        assert server_path not in json.dumps(response.json(), ensure_ascii=False)

    failed = failed_response.json()
    assert failed["intent_text"] == user_text
    assert failed["error"] == "操作失败，请重试"
    assert failed["progress_text"] == "操作失败，请重试"
    assert failed["final_text"] == "assistant draft saved at [SERVER_PATH]"
    assert failed["artifacts"][0]["file_path"] is None
    assert failed["artifacts"][0]["relpath"] == "out/result.txt"
    assert failed["snapshot"]["error"] == "操作失败，请重试"
    assert failed["snapshot"]["progress_text"] == "操作失败，请重试"
    assert failed["snapshot"]["final_text"] == "assistant draft saved at [SERVER_PATH]"
    assert failed["snapshot"]["artifacts"][0]["file_path"] is None
    succeeded = succeeded_response.json()
    assert succeeded["intent_text"] == user_text
    assert succeeded["final_text"] == final_text

    draft_event = next(
        event for event in events_response.json()["events"] if event["event"] == "task.message"
    )
    assert draft_event["message"] == "assistant draft saved at [SERVER_PATH]"
    workflow_draft_event = next(
        event
        for event in workflow_events_response.json()["events"]
        if event["event_type"] == "agent.task.message"
    )
    assert workflow_draft_event["message"] == "assistant draft saved at [SERVER_PATH]"
    assert workflow_draft_event["payload"]["message"] == ("assistant draft saved at [SERVER_PATH]")
    failed_event = next(
        event for event in events_response.json()["events"] if event["event"] == "task.failed"
    )
    assert failed_event["message"] == "操作失败，请重试"
    assert failed_event["raw_ref"] is None
    assert failed_event["snapshot"]["error"] == "操作失败，请重试"
    artifact_event = next(
        event
        for event in events_response.json()["events"]
        if event["event"] == "task.artifact_updated"
    )
    assert artifact_event["artifacts"][0]["file_path"] is None
    assert artifact_event["raw_ref"] is None

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": replay_after_seq,
                "client_version": "0.3.3",
                "auth": {"type": "bearer", "token": session["token"]},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        agent_ws_events: dict[str, dict[str, object]] = {}
        for _ in range(24):
            event = ws.receive_json()
            if (
                event["type"] == "agent.task.event"
                and event["payload"].get("task_id") == failed_task_id
            ):
                event_name = str(event["payload"].get("event") or "")
                if event_name in {"task.message", "task.failed"}:
                    agent_ws_events[event_name] = event
            if agent_ws_events.keys() == {"task.message", "task.failed"}:
                break
        else:
            pytest.fail("draft/failed agent privacy probes were not replayed")
    draft_ws_event = agent_ws_events["task.message"]
    failed_ws_event = agent_ws_events["task.failed"]
    serialized = json.dumps(agent_ws_events, ensure_ascii=False)
    assert server_path not in serialized
    assert draft_ws_event["payload"]["message"] == "assistant draft saved at [SERVER_PATH]"
    assert failed_ws_event["payload"]["message"] == "操作失败，请重试"
    assert failed_ws_event["payload"]["raw_ref"] is None
    assert failed_ws_event["payload"]["snapshot"]["error"] == "操作失败，请重试"


@pytest.mark.unit
def test_public_artifact_download_and_meeting_share_reject_corrupt_scoped_paths(
    public_client: TestClient,
) -> None:
    session_a, _credential_a = _issue(public_client, "artifact-path-owner-a")
    session_b, _credential_b = _issue(public_client, "artifact-path-owner-b")
    principal_a = session_a["principal"]
    principal_b = session_b["principal"]
    assert isinstance(principal_a, dict) and isinstance(principal_b, dict)
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    assert public_client.portal is not None
    (
        meeting_id,
        cross_artifact_id,
        symlink_artifact_id,
        other_file,
        owner_real_file,
    ) = public_client.portal.call(
        _seed_public_corrupt_artifact_paths,
        settings,
        principal_a,
        principal_b,
    )
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    assert (
        public_client.get(
            f"/artifacts/{cross_artifact_id}/download",
            headers=headers_a,
        ).status_code
        == 404
    )
    assert (
        public_client.get(
            f"/artifacts/{cross_artifact_id}/download",
            headers=headers_b,
        ).status_code
        == 404
    )
    if symlink_artifact_id is not None:
        assert (
            public_client.get(
                f"/artifacts/{symlink_artifact_id}/download",
                headers=headers_a,
            ).status_code
            == 404
        )

    issued = public_client.post(f"/meetings/{meeting_id}/share-ticket", headers=headers_a)
    assert issued.status_code == 200
    shared = public_client.get(issued.json()["path"])
    assert shared.status_code == 200
    assert cross_artifact_id not in shared.text
    if symlink_artifact_id is not None:
        assert symlink_artifact_id not in shared.text
    assert "OTHER_OWNER_PRIVATE_BYTES" not in shared.text
    assert "SYMLINK_TARGET_PRIVATE_BYTES" not in shared.text
    assert Path(other_file).read_text(encoding="utf-8") == "OTHER_OWNER_PRIVATE_BYTES"
    assert Path(owner_real_file).read_text(encoding="utf-8") == "SYMLINK_TARGET_PRIVATE_BYTES"


@pytest.mark.unit
def test_public_capture_and_rag_docs_redact_server_file_references(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "path-projection-endpoints")
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    audio_ref = str(settings.storage_dir / "private" / "ambient.wav")
    source_path = str(settings.storage_dir / "private" / "workspace.txt")

    class _AmbientPathProbe:
        async def ingest_chunk(self, *_args: object, **_kwargs: object) -> CaptureChunkResult:
            return CaptureChunkResult(
                ambient_stored=True,
                ambient_text="privacy probe",
                audio_ref=audio_ref,
            )

    class _RagPathProbe:
        async def list_docs(self) -> list[dict[str, object]]:
            return [
                {
                    "doc_id": "private-source-doc",
                    "title": "privacy probe",
                    "source": "workspace",
                    "source_path": source_path,
                    "kind": "txt",
                    "n_chunks": 1,
                }
            ]

    public_client.app.dependency_overrides[capture_api.get_ambient_pipeline] = _AmbientPathProbe
    public_client.app.dependency_overrides[retrieval_api.get_rag] = _RagPathProbe
    headers = {"Authorization": f"Bearer {session['token']}"}
    device_id = str(session["principal"]["device_id"])
    try:
        control = public_client.get("/capture/control", headers=headers)
        assert control.status_code == 200, control.text
        selected = public_client.put(
            "/capture/control",
            headers=headers,
            json={
                "mode": "single",
                "selectedDeviceIds": [device_id],
                "expectedRevision": control.json()["revision"],
            },
        )
        assert selected.status_code == 200, selected.text
        capture = public_client.post(
            "/capture/chunk",
            headers=headers,
            files={"audio": ("probe.wav", b"safe-audio-probe", "audio/wav")},
            data={
                "deviceId": device_id,
                "segmentId": "path-projection-probe",
            },
        )
        rag_docs = public_client.get("/rag/docs", headers=headers)
    finally:
        public_client.app.dependency_overrides.pop(capture_api.get_ambient_pipeline, None)
        public_client.app.dependency_overrides.pop(retrieval_api.get_rag, None)

    assert capture.status_code == 200, capture.text
    assert capture.json()["audio_ref"] is None
    assert audio_ref not in capture.text
    assert rag_docs.status_code == 200, rag_docs.text
    assert rag_docs.json()["docs"][0]["source_path"] is None
    assert rag_docs.json()["by_source"]["workspace"][0]["source_path"] is None
    assert source_path not in rag_docs.text


@pytest.mark.unit
def test_session_success_failure_and_transport_responses_are_private_no_store(
    public_client: TestClient,
) -> None:
    enrollment = _enrollment_payload("no-store")
    invalid_request = public_client.post("/session")
    enrolled = public_client.post(
        "/session/enroll",
        json=enrollment,
    )
    assert enrolled.status_code == 201
    renewed = public_client.post(
        "/session/renew",
        json={"device_credential": enrollment["device_secret"]},
    )
    invalid_credential = public_client.post(
        "/session/renew",
        json={"device_credential": "invalid-" + "x" * 40},
    )
    missing_authorization = public_client.post(
        "/session/credential/rotate",
        json={
            "current_device_credential": "current-" + "c" * 40,
            "new_device_credential": "replacement-" + "r" * 40,
        },
    )
    malformed_host = public_client.post(
        "/session",
        headers={"Host": "echodesk.yoliyoli.uk/invalid"},
        json=_enrollment_payload("bad-host"),
    )

    assert [
        invalid_request.status_code,
        renewed.status_code,
        invalid_credential.status_code,
        missing_authorization.status_code,
        malformed_host.status_code,
    ] == [422, 200, 401, 401, 400]
    for response in (
        invalid_request,
        enrolled,
        renewed,
        invalid_credential,
        missing_authorization,
        malformed_host,
    ):
        _assert_private_no_store(response)


@pytest.mark.unit
def test_unhandled_session_exception_is_private_no_store(
    public_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected secret-bearing session failure")

    monkeypatch.setattr(AccessPolicy, "enroll_public_device", explode)

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=public_client.app,
            raise_app_exceptions=False,
            client=("127.0.0.1", 55000),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://echodesk.yoliyoli.uk",
            headers={PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION},
        ) as client:
            return await client.post(
                "/session/enroll",
                json=_enrollment_payload("unhandled-no-store"),
            )

    response = asyncio.run(request())
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "请求未能完成，请稍后重试",
        }
    }
    assert "injected secret" not in response.text
    _assert_private_no_store(response)


@pytest.mark.unit
def test_public_routes_require_server_issued_session(public_client: TestClient) -> None:
    assert public_client.get("/healthz").status_code == 200
    assert public_client.get("/meetings").status_code == 401

    assert public_client.post("/session").status_code == 422
    payload = _enrollment_payload("retry")
    first = public_client.post("/session", json=payload)
    second = public_client.post("/session", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["token"] != second.json()["token"]
    assert first.json()["principal"]["owner_id"] == second.json()["principal"]["owner_id"]
    conflict_payload = {**payload, "device_secret": "conflict-" + "x" * 40}
    assert public_client.post("/session", json=conflict_payload).status_code == 409

    authorized = public_client.get(
        "/meetings",
        headers={"Authorization": f"Bearer {second.json()['token']}"},
    )
    assert authorized.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize(
    "version",
    [
        "",
        "0.2.50",
        "0.3.0",
        "0.3.1",
        "0.3.1-rc.1",
        "0.3.2",
        "0.3.3-preview.1",
        "invalid",
        "0.3",
    ],
)
def test_public_routes_reject_missing_invalid_or_old_client_versions(
    public_client: TestClient,
    version: str,
) -> None:
    response = public_client.get(
        "/meetings",
        headers={PUBLIC_CLIENT_VERSION_HEADER: version},
    )

    assert response.status_code == 426
    assert response.json()["error"] == {
        "code": "client_upgrade_required",
        "minimum_client_version": MINIMUM_PUBLIC_CLIENT_VERSION,
        "upgrade_url": "https://github.com/yoligehude14753/echo-demo/releases",
    }
    assert response.headers["x-echodesk-minimum-client-version"] == MINIMUM_PUBLIC_CLIENT_VERSION
    assert response.headers["cache-control"] == "private, no-store, max-age=0"


@pytest.mark.unit
@pytest.mark.parametrize(
    "version",
    [
        "0.3.3-preview.2",
        "v0.3.3-preview.2",
        "0.3.3",
        "v0.3.3",
        "echodesk-0.3.3",
        "0.3.10",
    ],
)
def test_supported_public_client_versions_continue_to_session_auth(
    public_client: TestClient,
    version: str,
) -> None:
    response = public_client.get(
        "/meetings",
        headers={PUBLIC_CLIENT_VERSION_HEADER: version},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_required"


@pytest.mark.unit
def test_public_json_body_limit_runs_before_auth_and_validation(
    public_client: TestClient,
) -> None:
    payload = b'{"question":"' + (b"x" * (2 * 1024 * 1024)) + b'"}'

    response = public_client.post(
        "/chat",
        content=payload,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


@pytest.mark.unit
def test_malformed_host_cannot_poison_identity_or_lan_policy_path(
    public_client: TestClient,
) -> None:
    poisoned = public_client.get(
        "/meetings",
        headers={"Host": "echodesk.yoliyoli.uk/healthz?mask="},
    )
    assert poisoned.status_code == 400
    assert poisoned.text == "Invalid host header"

    canonical = public_client.get(
        "/meetings",
        headers={"Host": "echodesk.yoliyoli.uk"},
    )
    assert canonical.status_code == 401
    assert canonical.json()["detail"] == "session required"
    assert canonical.json()["error"] == {
        "code": "session_required",
        "minimum_client_version": MINIMUM_PUBLIC_CLIENT_VERSION,
        "upgrade_url": "https://github.com/yoligehude14753/echo-demo/releases",
    }
    assert canonical.headers["x-echodesk-minimum-client-version"] == MINIMUM_PUBLIC_CLIENT_VERSION
    assert canonical.headers["cache-control"] == "private, no-store, max-age=0"
    assert canonical.headers["link"].endswith('; rel="upgrade"')


@pytest.mark.unit
def test_public_session_http_renew_rotate_and_revoke_preserve_identity(
    public_client: TestClient,
) -> None:
    payload = {**_enrollment_payload("lifecycle"), "display_name": "test"}
    enrolled = public_client.post("/session/enroll", json=payload)
    assert enrolled.status_code == 201
    first = enrolled.json()
    assert first["device_credential"] is None
    stable_scope = {key: first["principal"][key] for key in ("tenant_id", "owner_id", "device_id")}

    renewed = public_client.post(
        "/session/renew",
        json={"device_credential": payload["device_secret"]},
    )
    assert renewed.status_code == 200
    second = renewed.json()
    assert {
        key: second["principal"][key] for key in ("tenant_id", "owner_id", "device_id")
    } == stable_scope
    assert (
        public_client.get(
            "/meetings", headers={"Authorization": f"Bearer {first['token']}"}
        ).status_code
        == 401
    )

    next_credential = "rotated-lifecycle-" + "r" * 40
    rotated = public_client.post(
        "/session/credential/rotate",
        headers={"Authorization": f"Bearer {second['token']}"},
        json={
            "current_device_credential": payload["device_secret"],
            "new_device_credential": next_credential,
        },
    )
    assert rotated.status_code == 200
    assert "device_credential" not in rotated.json()
    assert (
        public_client.post(
            "/session/renew",
            json={"device_credential": payload["device_secret"]},
        ).status_code
        == 401
    )

    third_response = public_client.post(
        "/session/renew",
        json={"device_credential": next_credential},
    )
    assert third_response.status_code == 200
    third = third_response.json()
    revoked = public_client.post(
        "/session/revoke",
        json={"scope": "family"},
        headers={"Authorization": f"Bearer {third['token']}"},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True, "scope": "family"}
    assert (
        public_client.get(
            "/meetings", headers={"Authorization": f"Bearer {third['token']}"}
        ).status_code
        == 401
    )
    assert (
        public_client.post(
            "/session/renew",
            json={"device_credential": next_credential},
        ).status_code
        == 401
    )


@pytest.mark.unit
def test_credential_rotate_reauth_has_dedicated_rate_limit(public_client: TestClient) -> None:
    session, _credential = _issue(public_client, "rotate-limit")
    headers = {"Authorization": f"Bearer {session['token']}"}
    denied = [
        public_client.post(
            "/session/credential/rotate",
            headers=headers,
            json={
                "current_device_credential": "wrong-current-" + "x" * 40,
                "new_device_credential": f"new-{index}-" + "n" * 40,
            },
        )
        for index in range(7)
    ]

    assert [response.status_code for response in denied[:6]] == [401] * 6
    assert denied[6].status_code == 429
    assert int(denied[6].headers["Retry-After"]) >= 1


@pytest.mark.unit
def test_anonymous_session_issuance_is_rate_limited_per_peer(
    public_client: TestClient,
) -> None:
    issued = [
        public_client.post("/session", json=_enrollment_payload(f"rate-{index}"))
        for index in range(12)
    ]
    blocked = public_client.post("/session", json=_enrollment_payload("rate-blocked"))

    assert all(response.status_code == 201 for response in issued)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "session issuance rate limit exceeded"
    assert int(blocked.headers["Retry-After"]) >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remote_lan_caller_cannot_use_host_capabilities_in_local_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "local-lan.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=False,
        lan_full_api_enabled=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        debug_token="host-admin-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    result = await run_migrations(settings.db_path)
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app, client=("192.168.50.20", 50000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        denied_requests = (
            await client.get("/admin/data-dir"),
            await client.get("/workspace/status"),
            await client.post(
                "/artifacts/generate",
                json={"artifact_type": "html", "brief": "execute on host"},
            ),
            await client.post(
                "/agents/tasks",
                json={"device_id": "lan", "text": "read host"},
            ),
            await client.post(
                "/agents/grants/claude_code",
                json={"device_id": "lan", "workspace_ids": []},
            ),
        )
        assert [response.status_code for response in denied_requests] == [403] * 5

        trusted = {"X-Echo-Admin-Token": "host-admin-secret"}
        assert (await client.get("/workspace/status", headers=trusted)).status_code == 200


@pytest.mark.unit
def test_public_session_cannot_reach_host_runtime_capabilities(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "host-capability")
    headers = {"Authorization": f"Bearer {session['token']}"}

    assert public_client.get("/workspace/status", headers=headers).status_code == 403
    assert (
        public_client.post(
            "/agents/grants/claude_code",
            headers=headers,
            json={"device_id": "attacker", "workspace_ids": []},
        ).status_code
        == 403
    )
    assert (
        public_client.post(
            "/agents/tasks",
            headers=headers,
            json={"device_id": "attacker", "text": "read the server"},
        ).status_code
        == 403
    )

    trusted = {"X-Echo-Admin-Token": "test-admin"}
    assert public_client.get("/workspace/status", headers=trusted).status_code == 200


class _OwnerScopedPptSkill:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    async def generate(
        self,
        *,
        llm: object,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
        artifact_id: str | None = None,
    ) -> GeneratedArtifact:
        _ = llm, extra_instructions
        assert artifact_type in {"ppt", "pptx"}
        assert artifact_id is not None
        self.calls += 1
        directory = scoped_directory(self.root) / artifact_id
        directory.mkdir(parents=True)
        output = directory / "output.pptx"
        output.write_bytes(b"PK\x03\x04" + b"owner-scoped-ppt" * 512)
        (directory / "meta.json").write_text(
            json.dumps({"title": brief, "artifact_type": "pptx"}),
            encoding="utf-8",
        )
        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="pptx",
            title=brief,
            file_path=str(output),
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            size_bytes=output.stat().st_size,
            generation_latency_ms=1,
            model="test-owner-ppt",
        )


@pytest.mark.unit
def test_public_owner_can_generate_safe_artifact_without_cross_owner_access(
    public_client: TestClient,
) -> None:
    settings = public_client.app.dependency_overrides[deps_mod.get_settings]()
    runner = _OwnerScopedPptSkill(settings.skill_executor_build_dir)
    public_client.app.dependency_overrides[artifacts_api.get_skill] = lambda: runner
    session_a, _credential_a = _issue(public_client, "artifact-owner-a")
    session_b, _credential_b = _issue(public_client, "artifact-owner-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    assert (
        public_client.post(
            "/artifacts/generate",
            json={"artifact_type": "pptx", "brief": "anonymous deck"},
        ).status_code
        == 401
    )
    denied_kind = public_client.post(
        "/artifacts/generate",
        headers=headers_a,
        json={"artifact_type": "word", "brief": "generated code is not owner safe"},
    )
    assert denied_kind.status_code == 403
    assert denied_kind.json()["detail"] == "artifact type is not available to owner sessions"

    assert public_client.post(
        "/meetings/owner-a-meeting/start",
        headers=headers_a,
    ).status_code == 200
    cross_owner = public_client.post(
        "/artifacts/generate",
        headers=headers_b,
        json={
            "artifact_type": "pptx",
            "brief": "cross owner deck",
            "meeting_id": "owner-a-meeting",
        },
    )
    assert cross_owner.status_code == 404

    generated = public_client.post(
        "/artifacts/generate",
        headers=headers_a,
        json={
            "artifact_type": "pptx",
            "brief": "owner A quarterly deck",
            "meeting_id": "owner-a-meeting",
        },
    )
    assert generated.status_code == 200, generated.text
    payload = generated.json()
    assert payload["artifact_type"] == "pptx"
    assert payload["file_path"] is None
    assert runner.calls == 1

    assert [item["artifact_id"] for item in public_client.get(
        "/artifacts",
        headers=headers_a,
    ).json()] == [payload["artifact_id"]]
    assert public_client.get("/artifacts", headers=headers_b).json() == []
    assert public_client.get(
        f"/artifacts/{payload['artifact_id']}/download",
        headers=headers_b,
    ).status_code == 404


@pytest.mark.unit
def test_public_meetings_are_isolated_between_issued_sessions(
    public_client: TestClient,
) -> None:
    session_a, _credential_a = _issue(public_client, "meeting-a")
    session_b, _credential_b = _issue(public_client, "meeting-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    assert public_client.post("/meetings/meeting-a/start", headers=headers_a).status_code == 200
    injected = public_client.post(
        "/meetings/meeting-a/inject_segment",
        headers=headers_a,
        json={"text": "tenant A secret", "start_ms": 0, "end_ms": 1000},
    )
    assert injected.status_code == 200

    assert [m["meeting_id"] for m in public_client.get("/meetings", headers=headers_a).json()] == [
        "meeting-a"
    ]
    assert public_client.get("/meetings", headers=headers_b).json() == []
    assert public_client.get("/meetings/meeting-a/transcript", headers=headers_b).status_code == 404
    assert public_client.get("/meetings/meeting-a/segments", headers=headers_b).status_code == 404

    assert (
        public_client.post(
            "/meetings/meeting-a/inject_segment",
            headers=headers_b,
            json={"text": "tenant B injection", "start_ms": 1001, "end_ms": 2000},
        ).status_code
        == 404
    )
    assert public_client.post("/meetings/meeting-a/end", headers=headers_b).status_code == 404
    assert (
        public_client.request(
            "DELETE",
            "/meetings/meeting-a/outputs",
            headers=headers_b,
            json={"artifact_ids": [], "clear_minutes": True},
        ).status_code
        == 404
    )

    transcript_a = public_client.get("/meetings/meeting-a/transcript", headers=headers_a).json()
    assert [segment["text"] for segment in transcript_a] == ["tenant A secret"]


@pytest.mark.unit
def test_public_segment_injection_is_byte_bounded_and_storage_governed(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "meeting-bounds")
    headers = {"Authorization": f"Bearer {session['token']}"}
    assert public_client.post("/meetings/bounded/start", headers=headers).status_code == 200

    oversized = public_client.post(
        "/meetings/bounded/inject_segment",
        headers=headers,
        json={"text": "界" * 6_000, "start_ms": 0, "end_ms": 1_000},
    )
    assert oversized.status_code == 413

    payload = {"text": "x" * 12_000, "start_ms": 0, "end_ms": 1_000}
    assert (
        public_client.post(
            "/meetings/bounded/inject_segment", headers=headers, json=payload
        ).status_code
        == 200
    )
    assert (
        public_client.post(
            "/meetings/bounded/inject_segment", headers=headers, json=payload
        ).status_code
        == 200
    )
    exhausted = public_client.post(
        "/meetings/bounded/inject_segment", headers=headers, json=payload
    )
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["metric"] == "storage_bytes"

    transcript = public_client.get("/meetings/bounded/transcript", headers=headers).json()
    assert len(transcript) == 2


@pytest.mark.unit
def test_public_manual_meeting_state_is_principal_scoped(public_client: TestClient) -> None:
    session_a, _credential_a = _issue(public_client, "manual-a")
    session_b, _credential_b = _issue(public_client, "manual-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    started_a = public_client.post("/meetings/manual_start", headers=headers_a).json()
    assert started_a["mode"] == "in_meeting"
    assert public_client.get("/meetings/current", headers=headers_b).json()["mode"] == "idle"

    started_b = public_client.post("/meetings/manual_start", headers=headers_b).json()
    assert started_b["mode"] == "in_meeting"
    assert started_b["meeting_id"] != started_a["meeting_id"]
    assert (
        public_client.get("/meetings/current", headers=headers_a).json()["meeting_id"]
        == started_a["meeting_id"]
    )


@pytest.mark.unit
def test_public_share_uses_narrow_resource_ticket(
    public_client: TestClient,
) -> None:
    session_a, _credential_a = _issue(public_client, "share-a")
    session_b, _credential_b = _issue(public_client, "share-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}
    assert public_client.post("/meetings/share-a/start", headers=headers_a).status_code == 200

    issued = public_client.post("/meetings/share-a/share-ticket", headers=headers_a)
    assert issued.status_code == 200
    share_path = issued.json()["path"]
    assert "share=" in share_path
    assert session_a["token"] not in share_path
    assert public_client.get(share_path).status_code == 200
    shared = public_client.get(share_path)
    assert shared.headers["cache-control"] == "private, no-store, max-age=0"
    assert shared.headers["referrer-policy"] == "no-referrer"
    assert shared.headers["x-frame-options"] == "DENY"
    assert issued.headers["cache-control"] == "private, no-store, max-age=0"
    assert issued.headers["referrer-policy"] == "no-referrer"
    assert issued.json()["expires_in_s"] == 600
    share_runs = public_client.get("/workflows/runs", headers=headers_a).json()
    share_run = next(run for run in share_runs if run["kind"] == "share.prepare")
    assert share_run["state"] == "succeeded"
    assert share_run["output"]["resource_id"] == "share-a"
    assert "token" not in share_run["output"]
    share_events = public_client.get(
        f"/workflows/runs/{share_run['run_id']}/events",
        headers=headers_a,
    ).json()["events"]
    assert [event["event_type"] for event in share_events] == [
        "workflow.created",
        "workflow.succeeded",
    ]
    ticket = unquote(share_path.split("share=", 1)[1])
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    with sqlite3.connect(settings.db_path) as conn:
        durable_dump = "\n".join(conn.iterdump())
    assert ticket not in durable_dump

    assert public_client.get("/meetings/share-a/share").status_code == 401
    assert (
        public_client.post("/meetings/share-a/share-ticket", headers=headers_b).status_code == 404
    )
    assert public_client.get(f"/meetings/other/share?share={ticket}").status_code == 401
    assert public_client.get(f"/artifacts/share-a/download?share={ticket}").status_code == 401
    persisted_log = (user_config_dir() / "logs" / "backend.log").read_text(encoding="utf-8")
    assert ticket not in persisted_log
    assert "?redacted" in persisted_log


@pytest.mark.unit
def test_public_share_ticket_and_workflow_audit_roll_back_together(
    public_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _credential = _issue(public_client, "share-atomic-rollback")
    headers = {"Authorization": f"Bearer {session['token']}"}
    meeting_id = "share-atomic-rollback"
    assert public_client.post(f"/meetings/{meeting_id}/start", headers=headers).status_code == 200
    original_append = WorkflowService._append_event_tx

    async def fail_terminal_event(*args: object, **kwargs: object) -> object:
        if str(args[3]) == "workflow.succeeded":
            raise RuntimeError("injected audit failure after ticket insert")
        return await original_append(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkflowService, "_append_event_tx", fail_terminal_event)
    with pytest.raises(RuntimeError, match="injected audit failure after ticket insert"):
        public_client.post(f"/meetings/{meeting_id}/share-ticket", headers=headers)

    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resource_tickets").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE kind = 'share.prepare'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM workflow_events
                   WHERE run_id IN (
                       SELECT run_id FROM workflow_runs WHERE kind = 'share.prepare'
                   )"""
            ).fetchone()[0]
            == 0
        )


@pytest.mark.unit
def test_public_share_returns_token_when_post_commit_outbox_flush_fails(
    public_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _credential = _issue(public_client, "share-post-commit-flush")
    headers = {"Authorization": f"Bearer {session['token']}"}
    meeting_id = "share-post-commit-flush"
    assert public_client.post(f"/meetings/{meeting_id}/start", headers=headers).status_code == 200
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    service = deps_mod.get_workflow_service(settings, deps_mod.get_event_bus())

    async def fail_flush(*, limit: int = 500) -> int:
        _ = limit
        raise RuntimeError("simulated committed share publish failure")

    monkeypatch.setattr(service, "flush_outbox", fail_flush)
    issued = public_client.post(f"/meetings/{meeting_id}/share-ticket", headers=headers)
    assert issued.status_code == 200
    share_path = issued.json()["path"]
    ticket = unquote(share_path.split("share=", 1)[1])
    assert public_client.get(share_path).status_code == 200
    runs = public_client.get("/workflows/runs", headers=headers).json()
    share_run = next(item for item in runs if item["kind"] == "share.prepare")
    assert share_run["state"] == "succeeded"
    assert "token" not in share_run["output"]
    with sqlite3.connect(settings.db_path) as conn:
        durable_dump = "\n".join(conn.iterdump())
        unpublished = conn.execute(
            "SELECT COUNT(*) FROM workflow_outbox WHERE published_at IS NULL"
        ).fetchone()[0]
    assert ticket not in durable_dump
    assert unpublished > 0


@pytest.mark.unit
def test_public_websocket_requires_session_and_replays_only_owner_events(
    public_client: TestClient,
) -> None:
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json({"type": "client_hello", "last_seq": 0, "client_version": "0.3.3"})
        with pytest.raises(WebSocketDisconnect) as anonymous:
            ws.receive_json()
    assert anonymous.value.code == 4401

    session_a, _credential_a = _issue(public_client, "ws-a")
    session_b, _credential_b = _issue(public_client, "ws-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}
    assert public_client.post("/meetings/meeting-a/start", headers=headers_a).status_code == 200
    assert public_client.post("/meetings/meeting-b/start", headers=headers_b).status_code == 200

    for session, expected_meeting in (
        (session_a, "meeting-a"),
        (session_b, "meeting-b"),
    ):
        with public_client.websocket_connect("/ws/echo") as ws:
            ws.send_json(
                {
                    "type": "client_hello",
                    "last_seq": 0,
                    "client_version": "0.3.3",
                    "auth": {"type": "bearer", "token": session["token"]},
                }
            )
            assert ws.receive_json()["type"] == "server_hello"
            event = ws.receive_json()
            assert event["type"] == "meeting.started"
            assert event["meeting_id"] == expected_meeting


@pytest.mark.unit
@pytest.mark.parametrize(
    "version",
    [None, "0.2.50", "0.3.0", "0.3.1", "invalid", f"0.3.2-{'x' * 80}"],
)
def test_public_websocket_rejects_missing_or_old_client_version(
    public_client: TestClient,
    version: str | None,
) -> None:
    session, _credential = _issue(public_client, f"ws-version-{version or 'missing'}")
    hello: dict[str, object] = {
        "type": "client_hello",
        "last_seq": 0,
        "auth": {"type": "bearer", "token": session["token"]},
    }
    if version is not None:
        hello["client_version"] = version

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(hello)
        with pytest.raises(WebSocketDisconnect) as rejected:
            ws.receive_json()
    assert rejected.value.code == 4426
    assert rejected.value.reason == f"client upgrade required:{MINIMUM_PUBLIC_CLIENT_VERSION}"


@pytest.mark.unit
def test_public_websocket_rejects_query_bearer_and_oversized_first_frame(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-query")
    with public_client.websocket_connect(f"/ws/echo?session={session['token']}") as ws:
        ws.send_json({"type": "client_hello", "last_seq": 0, "client_version": "0.3.3"})
        with pytest.raises(WebSocketDisconnect) as query_rejected:
            ws.receive_json()
    assert query_rejected.value.code == 4401

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_text("x" * 4097)
        with pytest.raises(WebSocketDisconnect) as oversized:
            ws.receive_json()
    assert oversized.value.code == 4408


@pytest.mark.unit
def test_public_websocket_bounds_authenticated_client_frames(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-authenticated-frame-bounds")
    hello = {
        "type": "client_hello",
        "last_seq": 0,
        "client_version": "0.3.3",
        "auth": {"type": "bearer", "token": session["token"]},
    }

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "server_hello"
        ws.send_bytes(json.dumps({"type": "client_ping"}).encode())
        assert ws.receive_json()["type"] == "server_ping"
        ws.send_text("x" * 4097)
        with pytest.raises(WebSocketDisconnect) as oversized:
            ws.receive_json()
    assert oversized.value.code == 4408
    assert oversized.value.reason == "invalid client frame"

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "server_hello"
        ws.send_bytes(b"\xff\xfe")
        with pytest.raises(WebSocketDisconnect) as invalid_utf8:
            ws.receive_json()
    assert invalid_utf8.value.code == 4408
    assert invalid_utf8.value.reason == "invalid client frame"


@pytest.mark.unit
def test_public_websocket_rate_limits_authenticated_frames_per_principal(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-authenticated-frame-rate")
    settings_override = public_client.app.dependency_overrides[deps_mod.get_settings]
    settings = settings_override()
    assert isinstance(settings, Settings)
    settings.ws_client_frames_per_second = 2
    hello = {
        "type": "client_hello",
        "last_seq": 0,
        "client_version": "0.3.3",
        "auth": {"type": "bearer", "token": session["token"]},
    }

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "server_hello"
        for _ in range(2):
            ws.send_json({"type": "client_ping"})
            assert ws.receive_json()["type"] == "server_ping"
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as limited:
            ws.receive_json()
    assert limited.value.code == 4429
    assert limited.value.reason == "websocket frame rate exceeded"


@pytest.mark.unit
def test_public_websocket_revalidates_revoke_and_releases_subscriber(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-revoke")
    token = str(session["token"])
    bus = deps_mod.get_event_bus()
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "client_version": "0.3.3",
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        revoked = public_client.post(
            "/session/revoke",
            json={"scope": "family"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoked.status_code == 200
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401
    for _ in range(50):
        if bus.subscriber_count() == 0:
            break
        time.sleep(0.01)
    assert bus.subscriber_count() == 0


@pytest.mark.unit
def test_public_websocket_revalidates_expired_connected_session(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-expired")
    token = str(session["token"])
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "client_version": "0.3.3",
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        settings = public_client.app.dependency_overrides[deps_mod.get_settings]()  # type: ignore[union-attr]
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE principal_sessions SET expires_at = ? WHERE session_id = ?",
                ("2000-01-01T00:00:00+00:00", session["principal"]["session_id"]),
            )
            conn.commit()
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401


@pytest.mark.unit
def test_public_websocket_revalidates_session_generation_after_renew(
    public_client: TestClient,
) -> None:
    session, credential = _issue(public_client, "ws-generation")
    token = str(session["token"])
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "client_version": "0.3.3",
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        renewed = public_client.post(
            "/session/renew",
            json={"device_credential": credential},
        )
        assert renewed.status_code == 200
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401
