from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.llm import LLMError, OpenAICompatibleLLM
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.schemas.llm import ChatMessage
from app.security.context import bind_principal, reset_principal
from app.security.governor import PrincipalGovernor
from app.security.models import Principal

from tests.unit._principal_identity import seed_principal_identity


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_adapter_settles_actual_tokens_and_releases_failed_reservation(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "llm-quota.db",
        llm_main_model="test-model",
        llm_main_base_url="http://model.invalid/v1",
        llm_main_max_tokens=10,
        quota_llm_tokens_per_day=20,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    governor = PrincipalGovernor(settings)
    principal = Principal("tenant", "device", "owner", "session", "public")
    await seed_principal_identity(settings.db_path, principal)
    context_token = bind_principal(principal)
    llm = OpenAICompatibleLLM(settings, governor=governor)
    try:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="ok"), finish_reason="stop")]
        response.usage = MagicMock(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        llm._main.chat.completions.create = AsyncMock(return_value=response)
        result = await llm.chat(
            [ChatMessage(role="user", content="hello")],
            max_tokens=10,
        )
        assert result.usage.total_tokens == 5
        assert await governor.usage(principal, "llm_tokens") == 5

        llm._main.chat.completions.create = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(LLMError):
            await llm.chat(
                [ChatMessage(role="user", content="timeout")],
                max_tokens=10,
                timeout_s=0.01,
            )
        assert await governor.usage(principal, "llm_tokens") == 5
    finally:
        await llm.aclose()
        reset_principal(context_token)
