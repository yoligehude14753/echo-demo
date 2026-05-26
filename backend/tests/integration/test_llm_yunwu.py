"""Integration: 真实访问 Yunwu M2.7（需 YUNWU_OPEN_KEY 环境变量）。

跳过条件：未配置 YUNWU_OPEN_KEY 或 .env 不存在 → 自动 skip。
"""

from __future__ import annotations

import os

import pytest
from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings
from app.schemas.llm import ChatMessage

pytestmark = pytest.mark.integration


def _has_yunwu_key() -> bool:
    return bool(os.getenv("YUNWU_OPEN_KEY") or Settings().yunwu_open_key)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_yunwu_key(), reason="YUNWU_OPEN_KEY not set")
async def test_yunwu_m27_short_chat_returns_content() -> None:
    s = Settings()
    llm = OpenAICompatibleLLM(s)
    try:
        r = await llm.chat(
            [ChatMessage(role="user", content="用一个词回答:北京是哪个国家的首都?直接给国家名。")],
            max_tokens=2000,
            timeout_s=120.0,
        )
        assert r.content.strip(), f"empty content from {s.llm_main_model}"
        assert "中国" in r.content or "China" in r.content
        assert r.usage.total_tokens > 0
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_yunwu_key(), reason="YUNWU_OPEN_KEY not set")
async def test_yunwu_m27_stream_yields_chunks() -> None:
    s = Settings()
    llm = OpenAICompatibleLLM(s)
    try:
        chunks: list[str] = []
        async for c in llm.chat_stream(
            [ChatMessage(role="user", content="把'你好'重复输出三次,用空格分隔。")],
            max_tokens=2000,
            timeout_s=120.0,
        ):
            chunks.append(c)
        joined = "".join(chunks)
        assert joined.strip(), "no streamed content"
        assert joined.count("你好") >= 2
    finally:
        await llm.aclose()
