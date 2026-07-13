from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.artifacts import get_skill
from app.api.deps import (
    get_llm_singleton,
    get_repository,
    get_workflow_dispatcher,
    get_workflow_service,
    reset_deps_for_test,
)
from app.api.retrieval import get_rag, get_web
from app.api.workflows import _agent_service
from app.api.workflows import router as workflows_router
from app.config import Settings, get_settings
from app.main import create_app
from app.schemas.artifact import GeneratedArtifact
from app.schemas.llm import ChatMessage, LLMResponse
from app.schemas.rag import RagChunk
from app.schemas.workflow import WorkflowRunCreate
from app.security.models import local_principal
from app.security.scope import scoped_directory
from app.upload.ownership import claim_rag_content, stage_rag_content_blob
from app.workflows.kernel import WorkflowDispatcher
from app.workflows.service import WorkflowService
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeLLM:
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 120.0,
    ) -> LLMResponse:
        return LLMResponse(content="ok", model=model or "fake", latency_ms=1)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 600.0,
    ) -> AsyncIterator[str]:
        yield "ok"


class _FakeSkill:
    def __init__(self, build_dir: Path) -> None:
        self.build_dir = build_dir

    async def generate(
        self,
        *,
        llm: Any,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
        artifact_id: str | None = None,
    ) -> GeneratedArtifact:
        artifact_id = artifact_id or "pdf-http-scenario"
        out_dir = scoped_directory(self.build_dir) / artifact_id
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / "output.pdf"
        output.write_bytes(b"%PDF-1.4\nfake")
        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type="pdf",
            title="HTTP 场景 PDF",
            file_path=str(output),
            mime_type="application/pdf",
            size_bytes=output.stat().st_size,
            generation_latency_ms=2,
            model="fake",
            metadata={"kind": "pdf", "brief": brief[:20]},
        )


class _QueryRag:
    async def query(self, _query: str, *, top_k: int = 5) -> list[RagChunk]:
        _ = top_k
        return [RagChunk(doc_id="doc-1", doc_title="Local", chunk_id="chunk-1", text="evidence")]


class _QueryWeb:
    async def search(self, _query: str, *, top_n: int = 5) -> list[object]:
        _ = top_n
        return []


class _FailingAnswerLLM(_FakeLLM):
    async def chat(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        if messages and "只能输出三个标签之一" in messages[0].content:
            return LLMResponse(content="rag", model="fake", latency_ms=1)
        raise RuntimeError("answer stream exploded")

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        _ = messages, kwargs
        yield "partial"
        raise RuntimeError("answer stream exploded")


@pytest.mark.unit
async def test_workflow_retry_active_key_conflict_is_http_409(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "workflow-retry-409.db",
        storage_dir=tmp_path / "storage",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    service = WorkflowService(settings, InMemoryEventBus())
    dispatcher = WorkflowDispatcher(service)
    active_key = "workflow:http-retry-conflict"
    parent = await service.create_run(
        WorkflowRunCreate(
            kind="rag.query",
            source="test",
            intent_text="failed parent",
            active_key=active_key,
        )
    )
    await service.start_run(parent.run_id)
    await service.fail_run(parent.run_id, error="failed")
    fresh = await service.create_run(
        WorkflowRunCreate(
            kind="rag.query",
            source="fresh",
            intent_text="fresh active winner",
            active_key=active_key,
        )
    )
    app = FastAPI()
    app.include_router(workflows_router)
    dummy_agents = object()
    app.dependency_overrides[get_workflow_service] = lambda: service
    app.dependency_overrides[get_workflow_dispatcher] = lambda: dispatcher
    app.dependency_overrides[_agent_service] = lambda: dummy_agents

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/workflows/runs/{parent.run_id}/retry",
        json={"reason": "late retry"},
    )

    assert response.status_code == 409
    assert "won retry race" in response.json()["detail"]
    assert (await service.get_active_by_active_key(active_key)).run_id == fresh.run_id  # type: ignore[union-attr]


@pytest.mark.unit
async def test_artifact_generate_http_writes_workflow_and_meeting_link(tmp_path: Path) -> None:
    reset_deps_for_test()
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    repo = SQLiteRepository(db_path)
    await repo.init()
    await repo.create_meeting(
        "mtg-http",
        started_at=datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
        title="HTTP 场景",
    )
    await repo.update_meeting_state(
        "mtg-http",
        state="in_meeting",
        minutes_json=json.dumps(
            {
                "meeting_id": "mtg-http",
                "title": "HTTP 场景",
                "duration_sec": 1,
                "summary": "atomic todo",
                "sections": [],
                "decisions": [],
                "todos": [{"id": "todo-http", "text": "生成 PDF", "status": "pending"}],
                "action_items": [],
            }
        ),
    )
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    app = create_app()
    fake_llm = _FakeLLM()
    fake_skill = _FakeSkill(settings.skill_executor_build_dir)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_llm_singleton] = lambda: fake_llm
    app.dependency_overrides[get_skill] = lambda: fake_skill
    client = TestClient(app)
    try:
        r = client.post(
            "/artifacts/generate",
            json={
                "artifact_type": "pdf",
                "brief": "请把会议纪要整理成 PDF",
                "meeting_id": "mtg-http",
                "todo_id": "todo-http",
            },
        )
        assert r.status_code == 200, r.text
        artifact_id = r.json()["artifact_id"]
        local_file_path = r.json()["file_path"]
        assert isinstance(local_file_path, str)
        assert Path(local_file_path).is_file()

        runs = client.get("/workflows/runs?meeting_id=mtg-http").json()
        assert len(runs) == 1
        assert runs[0]["state"] == "succeeded"
        assert runs[0]["output"]["artifact_id"] == artifact_id

        artifacts = client.get("/meetings/mtg-http/artifacts").json()
        assert [item["artifact_id"] for item in artifacts] == [artifact_id]
        assert artifacts[0]["file_path"] == local_file_path
        minutes = client.get("/meetings/mtg-http/minutes").json()
        assert minutes["todos"][0]["status"] == "done"
        assert minutes["todos"][0]["artifact_id"] == artifact_id

        download = client.get(f"/artifacts/{artifact_id}/download")
        assert download.status_code == 200
        assert download.content.startswith(b"%PDF")
    finally:
        await repo.aclose()
        reset_deps_for_test()


@pytest.mark.unit
async def test_rag_ingest_and_delete_execute_through_dispatcher(tmp_path: Path) -> None:
    reset_deps_for_test()
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        workspace_scan_on_startup=False,
    )
    assert (await run_migrations(settings.db_path)).errors == []
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        ingested = client.post(
            "/rag/ingest",
            files={"file": ("notes.txt", b"dispatcher owned knowledge", "text/plain")},
        )
        assert ingested.status_code == 200, ingested.text
        doc_id = ingested.json()["doc_id"]

        runs = client.get("/workflows/runs").json()
        ingest_run = next(run for run in runs if run["kind"] == "rag.ingest")
        assert ingest_run["state"] == "succeeded"
        assert ingest_run["output"]["doc_id"] == doc_id
        events = client.get(f"/workflows/runs/{ingest_run['run_id']}/events").json()["events"]
        assert [event["event_type"] for event in events] == [
            "workflow.created",
            "workflow.started",
            "workflow.succeeded",
        ]

        deleted = client.delete(f"/rag/docs/{doc_id}")
        assert deleted.status_code == 200
        delete_run = next(
            run for run in client.get("/workflows/runs").json() if run["kind"] == "rag.delete"
        )
        assert delete_run["state"] == "succeeded"
        assert client.get("/rag/docs").json()["total"] == 0

        reingested = client.post(
            "/rag/ingest",
            files={"file": ("notes.txt", b"dispatcher owned knowledge", "text/plain")},
        )
        assert reingested.status_code == 200, reingested.text
        assert reingested.json()["doc_id"] != doc_id
        ingest_runs = [
            run for run in client.get("/workflows/runs").json() if run["kind"] == "rag.ingest"
        ]
        assert len(ingest_runs) == 2
        assert all(run["state"] == "succeeded" for run in ingest_runs)
    finally:
        reset_deps_for_test()


@pytest.mark.unit
async def test_rag_ingest_reuses_pending_acl_run_across_source_labels(tmp_path: Path) -> None:
    reset_deps_for_test()
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        workspace_scan_on_startup=False,
    )
    assert (await run_migrations(settings.db_path)).errors == []
    content = b"same bytes from another source"
    digest = hashlib.sha256(content).hexdigest()
    run_id = "run-existing-upload"
    service = WorkflowService(settings, InMemoryEventBus())
    await service.create_run(
        WorkflowRunCreate(
            kind="rag.ingest",
            source="upload",
            title="Existing upload",
            intent_text="Ingest Existing upload",
            input={
                "title": "Existing upload",
                "source": "upload",
                "source_path": None,
                "content_hash": digest,
            },
            timeout_s=120,
            active_key=f"rag.ingest:upload:{digest}",
        ),
        run_id=run_id,
    )
    principal = local_principal()
    await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=len(content),
        workflow_run_id=run_id,
        file_suffix=".txt",
    )
    await stage_rag_content_blob(
        settings.db_path,
        settings.storage_dir,
        principal,
        content_hash=digest,
        workflow_run_id=run_id,
        content=content,
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        ingested = client.post(
            "/rag/ingest",
            data={"source": "workspace", "source_path": "/tmp/example.txt"},
            files={"file": ("example.txt", content, "text/plain")},
        )
        assert ingested.status_code == 200, ingested.text
        runs = [run for run in client.get("/workflows/runs").json() if run["kind"] == "rag.ingest"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["state"] == "succeeded"
        assert runs[0]["active_key"] == f"rag.ingest:upload:{digest}"
    finally:
        reset_deps_for_test()


@pytest.mark.unit
async def test_rag_query_sse_streams_without_waiting_for_workflow(tmp_path: Path) -> None:
    reset_deps_for_test()
    settings = Settings(
        db_path=tmp_path / "query.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        workspace_scan_on_startup=False,
    )
    assert (await run_migrations(settings.db_path)).errors == []
    app = create_app()
    fake_llm = _FakeLLM()
    query_rag = _QueryRag()
    query_web = _QueryWeb()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_singleton] = lambda: fake_llm
    app.dependency_overrides[get_rag] = lambda: query_rag
    app.dependency_overrides[get_web] = lambda: query_web
    client = TestClient(app, raise_server_exceptions=False)
    try:
        success = client.post("/rag/ask", json={"question": "what is local?"})
        assert success.status_code == 200, success.text
        assert success.headers["content-type"].startswith("text/event-stream")
        assert success.headers["cache-control"] == "no-cache, no-transform"
        assert success.headers["x-accel-buffering"] == "no"
        assert "event: delta" in success.text
        assert '"delta":"- evidence [doc:doc-1-chunk-1]"' in success.text
        assert "event: done" in success.text
        assert '"chosen_source":"rag"' in success.text
        assert "data: [DONE]" not in success.text
        runs = client.get("/workflows/runs").json()
        assert all(run["kind"] != "rag.query" for run in runs)

        reset_deps_for_test()
        failing_llm = _FailingAnswerLLM()
        app.dependency_overrides[get_llm_singleton] = lambda: failing_llm
        failed = client.post("/rag/ask", json={"question": "make this fail"})
        assert failed.status_code == 200
        assert "event: delta" not in failed.text
        assert "event: error" in failed.text
        assert "暂时无法生成回答，请稍后重试" in failed.text
        assert "answer stream exploded" not in failed.text
        assert "event: done" not in failed.text
    finally:
        reset_deps_for_test()
