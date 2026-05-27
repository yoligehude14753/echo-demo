"""P2.3：远程降级链路单测。

覆盖三个关键 graceful-failure 路径：
1. artifacts API 在 LLMError 时 emit artifact.failed 事件（含 reason=remote_llm）
   并返回 502 而非 500 → 前端能复用 P2.2 的失败卡片渲染
2. retrieve_and_answer._classify 在 fast LLM 失败时 fallback 到 "either"
   而非 raise → 整条 RAG/web 链路不挂
3. _classify 成功路径不受影响
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.adapters.llm import LLMError
from app.api.artifacts import generate as artifacts_generate
from app.schemas.artifact import ArtifactRequest
from app.schemas.events import EchoEvent
from app.use_cases.retrieve_and_answer import _classify


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[EchoEvent] = []

    async def publish(self, event: EchoEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_artifacts_emits_failed_on_llm_error() -> None:
    """artifacts.generate 调用 LLM 失败 → emit artifact.failed + 502。"""
    bus = _FakeBus()
    fake_llm = AsyncMock()
    fake_skill = AsyncMock()

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
                llm=fake_llm,
                runner=fake_skill,
                event_bus=bus,  # type: ignore[arg-type]
            )
        assert ei.value.status_code == 502, "LLM 不可达应映射到 502（gateway 类）"
    finally:
        artifacts_module.generate_artifact = original  # type: ignore[assignment]

    assert len(bus.events) == 2, "应该 emit generating + failed 两条事件"
    assert bus.events[0].type == "artifact.generating"
    assert bus.events[1].type == "artifact.failed"
    payload = bus.events[1].payload
    assert payload["artifact_type"] == "html"
    assert payload["reason"] == "remote_llm", "前端用 reason 字段区分 LLM 失败 vs Skill 失败"
    assert "Yunwu" in payload["error"] or "timeout" in payload["error"]


@pytest.mark.asyncio
async def test_classify_falls_back_on_llm_error() -> None:
    """_classify 在 fast LLM 失败时返回 'either' 而非 raise。"""
    fake_llm = AsyncMock()
    fake_llm.chat.side_effect = LLMError("heyi-bj :7860 connect refused")
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
