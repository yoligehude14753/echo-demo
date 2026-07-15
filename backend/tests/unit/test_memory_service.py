"""Memory extraction timeout boundary regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.config import Settings
from app.memory.models import MemoryScope
from app.memory.service import MemoryService
from app.schemas.llm import LLMResponse


class _RecordingLLM:
    def __init__(self, response: str | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.timeout_s: float | None = None

    async def chat(self, _messages: list[object], **kwargs: object) -> LLMResponse:
        self.timeout_s = float(kwargs["timeout_s"])
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return LLMResponse(content=self.response, model="gpt-5.4-nano")


def _service(tmp_path, llm: _RecordingLLM) -> MemoryService:
    settings = Settings(
        db_path=tmp_path / "memory.db",
        storage_dir=tmp_path / "storage",
        memory_small_model_timeout_s=2.0,
        memory_extraction_timeout_s=8.0,
        _env_file=None,  # type: ignore[call-arg]
    )
    service = MemoryService(settings, llm)  # type: ignore[arg-type]
    service.repository.semantic_candidates = AsyncMock(return_value=[])
    service.repository.record_extraction_run = AsyncMock()
    return service


def _scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-test",
        owner_id="owner-test",
        device_id="device-test",
        session_id="session-test",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extraction_uses_its_own_bounded_timeout(tmp_path) -> None:
    llm = _RecordingLLM(response='{"memories": []}')
    service = _service(tmp_path, llm)

    result = await service.extract_text(
        _scope(),
        text="用户明确表示：每周一上午十点查看项目进度。",
        source_kind="user_explicit",
        source_id="explicit-timeout-boundary",
        occurred_at=datetime.now(UTC),
    )

    assert result.state == "succeeded"
    assert llm.timeout_s == 8.0
    service.repository.record_extraction_run.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extraction_timeout_keeps_failed_state_and_explicit_error(tmp_path) -> None:
    llm = _RecordingLLM(error=TimeoutError())
    service = _service(tmp_path, llm)

    result = await service.extract_text(
        _scope(),
        text="用户明确表示：每周一上午十点查看项目进度。",
        source_kind="user_explicit",
        source_id="explicit-timeout-error",
        occurred_at=datetime.now(UTC),
    )

    assert result.state == "failed"
    assert result.memories == []
    assert result.error == "TimeoutError: memory extraction timed out after 8.0s"
    calls = service.repository.record_extraction_run.await_args_list
    assert [call.kwargs["state"] for call in calls] == ["running", "failed"]
