"""Provider-neutral live contract for the configured OpenAI-compatible MAIN model.

This gate intentionally has no skip conditions.  CI runs it only in the
explicit live workflow; a missing or unreachable configured provider is a
product-contract failure, while provider-specific diagnostics remain separate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.skill.llm_skill import SkillExecutor
from app.config import Settings
from app.schemas.llm import ChatMessage

pytestmark = [pytest.mark.integration, pytest.mark.live]


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        skill_executor_timeout_s=180,
        skill_executor_max_tokens=4096,
        llm_main_max_tokens=4096,
        llm_fast_max_tokens=4096,
    )
    if not settings.llm_main_model.strip() or not settings.llm_main_base_url.strip():
        raise AssertionError("live contract requires LLM_MAIN_MODEL and LLM_MAIN_BASE_URL")
    if settings.resolved_llm_main_api_key == "EMPTY":
        raise AssertionError("live contract requires LLM_MAIN_API_KEY")
    return settings


@pytest.mark.asyncio
async def test_configured_main_chat_and_stream_return_content(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    llm = OpenAICompatibleLLM(settings)
    try:
        response = await llm.chat(
            [ChatMessage(role="user", content="只回复 ECHODESK_LIVE_CHAT_OK")],
            max_tokens=128,
            timeout_s=60,
        )
        assert "ECHODESK_LIVE_CHAT_OK" in response.content
        assert response.usage.total_tokens > 0

        chunks: list[str] = []
        async for chunk in llm.chat_stream(
            [ChatMessage(role="user", content="只回复 ECHODESK_LIVE_STREAM_OK")],
            max_tokens=128,
            timeout_s=60,
        ):
            chunks.append(chunk)
        assert "ECHODESK_LIVE_STREAM_OK" in "".join(chunks)
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_configured_main_generates_real_txt_artifact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    llm = OpenAICompatibleLLM(settings)
    try:
        artifact = await SkillExecutor(settings).generate(
            llm=llm,
            artifact_type="txt",
            brief=(
                "生成纯文本验收记录，第一行必须原样写 ECHODESK_LIVE_ARTIFACT_OK，"
                "第二行写 provider-neutral OpenAI-compatible contract passed。"
            ),
        )
        output = Path(artifact.file_path)
        assert output.is_file()
        assert artifact.size_bytes == output.stat().st_size
        assert "ECHODESK_LIVE_ARTIFACT_OK" in output.read_text(encoding="utf-8")
    finally:
        await llm.aclose()
