from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.api.artifacts import bind_artifact_workflow_handler
from app.artifacts.repository import ArtifactRepository
from app.artifacts.staging import load_workflow_artifact, workflow_artifact_id
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import current_principal
from app.security.scope import scoped_directory
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher
from app.workflows.service import WorkflowService


class _ProcessCrash(BaseException):
    pass


class _CountingSkill:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    async def generate(
        self,
        *,
        llm: Any,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
        artifact_id: str | None = None,
    ) -> GeneratedArtifact:
        _ = llm, extra_instructions
        self.calls += 1
        assert artifact_id is not None
        directory = scoped_directory(self.root) / artifact_id
        directory.mkdir(parents=True)
        output = directory / "output.pdf"
        output.write_bytes(b"%PDF-1.4\ncrash-safe")
        (directory / "meta.json").write_text(
            json.dumps({"title": brief, "artifact_type": artifact_type}),
            encoding="utf-8",
        )
        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            title=brief,
            file_path=str(output),
            mime_type="application/pdf",
            size_bytes=output.stat().st_size,
            generation_latency_ms=1,
            model="fake",
        )


@pytest.mark.unit
async def test_artifact_replay_reuses_deterministic_output_after_precommit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    assert (await run_migrations(settings.db_path)).errors == []
    bus = InMemoryEventBus()
    service = WorkflowService(settings, bus)
    dispatcher = WorkflowDispatcher(service)
    runner = _CountingSkill(settings.skill_executor_build_dir)
    artifact_repo = ArtifactRepository(settings)
    bind_artifact_workflow_handler(
        dispatcher,
        settings=settings,
        llm=object(),  # type: ignore[arg-type]
        runner=runner,
        event_bus=bus,
        artifact_repo=artifact_repo,
    )
    payload = {"artifact_type": "pdf", "brief": "可恢复报告"}
    run = await service.create_run(
        WorkflowRunCreate(
            kind="artifact.generate",
            source="test",
            intent_text="crash point",
            input=payload,
        )
    )
    started = await service.start_run(run.run_id)
    assert started is not None
    principal = current_principal()
    handler = dispatcher.registry.resolve(
        "artifact.generate", (principal.tenant_id, principal.owner_id)
    )
    assert handler is not None
    complete_run_atomic = service.complete_run_atomic

    async def crash_before_sqlite(*_args: object, **_kwargs: object) -> None:
        raise _ProcessCrash

    monkeypatch.setattr(service, "complete_run_atomic", crash_before_sqlite)
    with pytest.raises(_ProcessCrash):
        await handler(
            WorkflowContext(run_id=run.run_id, attempt=1, cancel_event=asyncio.Event()),
            payload,
        )

    deterministic_id = workflow_artifact_id(run.run_id, "pdf")
    staged = load_workflow_artifact(
        settings,
        run_id=run.run_id,
        artifact_type="pdf",
    )
    assert staged is not None
    assert staged.artifact_id == deterministic_id
    assert runner.calls == 1
    still_running = await service.get_run(run.run_id)
    assert still_running is not None and still_running.state == "running"
    assert await artifact_repo.get_artifact(deterministic_id) is None

    monkeypatch.setattr(service, "complete_run_atomic", complete_run_atomic)
    output = await handler(
        WorkflowContext(run_id=run.run_id, attempt=1, cancel_event=asyncio.Event()),
        payload,
    )

    assert runner.calls == 1
    assert output["artifact_id"] == deterministic_id
    recovered = await artifact_repo.get_artifact(deterministic_id)
    assert recovered is not None
    done = await service.get_run(run.run_id)
    assert done is not None and done.state == "succeeded"
