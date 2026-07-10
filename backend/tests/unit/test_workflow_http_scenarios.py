from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.artifacts import get_skill
from app.api.deps import get_llm_singleton, get_repository, reset_deps_for_test
from app.config import Settings, get_settings
from app.main import create_app
from app.schemas.artifact import GeneratedArtifact
from app.schemas.llm import ChatMessage, LLMResponse
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
    ) -> GeneratedArtifact:
        artifact_id = "pdf-http-scenario"
        out_dir = self.build_dir / artifact_id
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

        runs = client.get("/workflows/runs?meeting_id=mtg-http").json()
        assert len(runs) == 1
        assert runs[0]["state"] == "succeeded"
        assert runs[0]["output"]["artifact_id"] == artifact_id

        artifacts = client.get("/meetings/mtg-http/artifacts").json()
        assert [item["artifact_id"] for item in artifacts] == [artifact_id]

        download = client.get(f"/artifacts/{artifact_id}/download")
        assert download.status_code == 200
        assert download.content.startswith(b"%PDF")
    finally:
        await repo.aclose()
        reset_deps_for_test()
