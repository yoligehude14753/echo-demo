"""LLM adapter 单测（不接外部服务，验证路由 + kwargs 构造）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.llm import LLMError, OpenAICompatibleLLM
from app.adapters.llm.openai_compatible import _is_reasoning
from app.config import Settings
from app.schemas.llm import ChatMessage


@pytest.fixture
def settings() -> Settings:
    return Settings(
        yunwu_open_key="sk-test",
        llm_main_model="MiniMax-M2.7",
        llm_main_base_url="https://yunwu.ai/v1",
        llm_fast_model="Qwen3-1.7B",
        llm_fast_base_url="http://10.0.0.1:7860/v1",
        llm_local_api_key="EMPTY",
        llm_main_max_tokens=80_000,
        llm_fast_max_tokens=512,
    )


@pytest.mark.unit
def test_reasoning_model_detection() -> None:
    assert _is_reasoning("MiniMax-M2.7")
    assert _is_reasoning("Qwen3-1.7B")
    assert _is_reasoning("GLM-5.1")
    assert not _is_reasoning("gpt-4")
    assert not _is_reasoning("Kimi-K2.6")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pick_routes_to_main_or_fast(settings: Settings) -> None:
    llm = OpenAICompatibleLLM(settings)
    try:
        client_m, model_m, max_m = llm._pick(None)
        assert model_m == "MiniMax-M2.7"
        assert max_m == 80_000

        client_f, model_f, max_f = llm._pick("Qwen3-1.7B")
        assert model_f == "Qwen3-1.7B"
        assert max_f == 512
        assert client_f is not client_m

        client_fb, model_fb, max_fb = llm._pick("GLM-4.6")
        assert model_fb == "GLM-4.6"
        assert max_fb == 80_000
        assert client_fb is client_m
    finally:
        await llm.aclose()
        # 释放 asyncio loop 警告
        await asyncio.sleep(0)


@pytest.mark.unit
def test_build_kwargs_disables_thinking_for_reasoning(settings: Settings) -> None:
    kwargs = OpenAICompatibleLLM._build_kwargs(
        "MiniMax-M2.7",
        [ChatMessage(role="user", content="hi")],
        max_tokens=80_000,
        temperature=0.3,
        stream=False,
    )
    assert kwargs["model"] == "MiniMax-M2.7"
    assert kwargs["max_tokens"] == 80_000
    assert kwargs["stream"] is False
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.unit
def test_build_kwargs_no_thinking_flag_for_non_reasoning(settings: Settings) -> None:
    kwargs = OpenAICompatibleLLM._build_kwargs(
        "Kimi-K2.6",
        [ChatMessage(role="user", content="hi")],
        max_tokens=4096,
        temperature=0.3,
        stream=True,
    )
    assert "extra_body" not in kwargs


@pytest.mark.asyncio
@pytest.mark.unit
async def test_chat_returns_llm_response_with_mock(settings: Settings) -> None:
    llm = OpenAICompatibleLLM(settings)
    try:
        fake_resp = MagicMock()
        fake_resp.choices = [MagicMock(message=MagicMock(content="你好"), finish_reason="stop")]
        fake_resp.usage = MagicMock(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        llm._main.chat.completions.create = AsyncMock(return_value=fake_resp)

        r = await llm.chat([ChatMessage(role="user", content="你好")])
        assert r.content == "你好"
        assert r.model == "MiniMax-M2.7"
        assert r.usage.total_tokens == 5
        assert r.finish_reason == "stop"
        assert r.latency_ms >= 0
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_chat_stream_yields_chunks_with_mock(settings: Settings) -> None:
    llm = OpenAICompatibleLLM(settings)
    try:

        class _Delta:
            def __init__(self, content: str) -> None:
                self.content = content

        class _Choice:
            def __init__(self, content: str) -> None:
                self.delta = _Delta(content)

        class _Event:
            def __init__(self, content: str) -> None:
                self.choices = [_Choice(content)]

        async def _agen() -> object:
            for c in ["你", "好", "啊"]:
                yield _Event(c)

        fake_stream = MagicMock()
        fake_stream.__aiter__ = lambda self: _agen()
        fake_stream.close = AsyncMock()
        llm._main.chat.completions.create = AsyncMock(return_value=fake_stream)

        out: list[str] = []
        async for chunk in llm.chat_stream([ChatMessage(role="user", content="hi")]):
            out.append(chunk)
        assert out == ["你", "好", "啊"]
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_chat_wraps_openai_timeout_into_llmerror(settings: Settings) -> None:

    llm = OpenAICompatibleLLM(settings)
    try:
        llm._main.chat.completions.create = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(LLMError):
            await llm.chat([ChatMessage(role="user", content="x")], timeout_s=0.01)
    finally:
        await llm.aclose()
