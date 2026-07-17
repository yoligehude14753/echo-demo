"""P2.3：远程降级链路单测。

覆盖三个关键 graceful-failure 路径：
1. artifacts API 在 LLMError 时 emit artifact.failed 事件（含 reason=remote_llm）
   并返回 502 而非 500 → 前端能复用 P2.2 的失败卡片渲染
2. retrieve_and_answer._classify 在 fast LLM 失败时 fallback 到 "either"
   而非 raise → 整条 RAG/web 链路不挂
3. _classify 成功路径不受影响
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import LLMError
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.artifacts import generate as artifacts_generate
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.schemas.artifact import ArtifactRequest
from app.security import local_principal
from app.use_cases.retrieve_and_answer import _classify
from app.workflows.kernel import WorkflowDispatcher
from app.workflows.service import WorkflowService


@pytest.mark.asyncio
async def test_artifacts_emits_failed_on_llm_error(tmp_path: Path) -> None:
    """artifacts.generate 调用 LLM 失败 → emit artifact.failed + 502。"""
    settings = Settings(db_path=tmp_path / "echo.db", storage_dir=tmp_path / "storage")
    assert (await run_migrations(settings.db_path)).errors == []
    bus = InMemoryEventBus()
    workflow = WorkflowService(settings, bus)
    dispatcher = WorkflowDispatcher(workflow)
    fake_llm = AsyncMock()
    fake_skill = AsyncMock()
    artifact_repo = ArtifactRepository(settings)
    repository = SQLiteRepository(settings.db_path)
    await repository.init()

    async def boom(*_: Any, **__: Any) -> Any:
        raise LLMError("Yunwu connect timeout")

    # patch use_case 调用：让它内部 LLM 调用 raise LLMError
    import app.api.artifacts as artifacts_module

    original = artifacts_module.generate_artifact
    artifacts_module.generate_artifact = boom  # type: ignore[assignment]
    try:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await artifacts_generate(
                body=ArtifactRequest(artifact_type="html", brief="test brief"),
                principal=local_principal(),
                repository=repository,
                llm=fake_llm,
                runner=fake_skill,
                event_bus=bus,
                dispatcher=dispatcher,
                artifact_repo=artifact_repo,
            )
        assert ei.value.status_code == 502, "LLM 不可达应映射到 502（gateway 类）"
    finally:
        artifacts_module.generate_artifact = original  # type: ignore[assignment]

    domain_events = [
        event
        for event in bus.recent_events_for_current_scope()
        if event.type in {"artifact.generating", "artifact.failed"}
    ]
    assert [event.type for event in domain_events] == ["artifact.generating", "artifact.failed"]
    payload = domain_events[1].payload
    assert payload["artifact_type"] == "html"
    assert payload["reason"] == "remote_llm", "前端用 reason 字段区分 LLM 失败 vs Skill 失败"
    assert "Yunwu" in payload["error"] or "timeout" in payload["error"]
    runs = await workflow.list_runs()
    assert len(runs) == 1 and runs[0].state == "failed"


@pytest.mark.asyncio
async def test_classify_falls_back_on_llm_error() -> None:
    """_classify 在 fast LLM 失败时返回 'either' 而非 raise。"""
    fake_llm = AsyncMock()
    fake_llm.chat.side_effect = LLMError("fast LLM connect refused")
    result = await _classify(fake_llm, "qwen3-1.7b", "什么是 SDXL？")
    assert result == "either", "fallback 必须是 either（让 RAG 与 web 都跑）"


@pytest.mark.asyncio
async def test_classify_falls_back_on_timeout() -> None:
    """_classify 在 timeout 时也 fallback（不止 LLMError，所有 Exception 都 catch）。"""
    fake_llm = AsyncMock()
    fake_llm.chat.side_effect = TimeoutError("read timed out")
    result = await _classify(fake_llm, "qwen3-1.7b", "test")
    assert result == "either"


@pytest.mark.asyncio
async def test_classify_returns_label_on_success() -> None:
    """正常路径：LLM 返回 'rag' → _classify 返回 'rag'。"""
    fake_llm = AsyncMock()
    fake_resp = type("R", (), {"content": "rag"})()
    fake_llm.chat.return_value = fake_resp
    result = await _classify(fake_llm, "qwen3-1.7b", "什么是 SDXL？")
    assert result == "rag"


@pytest.mark.asyncio
async def test_classify_unknown_label_defaults_either() -> None:
    """LLM 返回非 rag/web/either → 默认 either。"""
    fake_llm = AsyncMock()
    fake_resp = type("R", (), {"content": "garbage"})()
    fake_llm.chat.return_value = fake_resp
    result = await _classify(fake_llm, "qwen3-1.7b", "test")
    assert result == "either"
