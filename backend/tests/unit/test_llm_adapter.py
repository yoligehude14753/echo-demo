"""LLM adapter 单测（不接外部服务，验证路由 + kwargs 构造）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.llm import LLMError, OpenAICompatibleLLM
from app.adapters.llm.openai_compatible import (
    _is_reasoning,
    _strip_thinking,
    _ThinkStripper,
)
from app.config import Settings
from app.schemas.llm import ChatMessage


@pytest.fixture
def settings() -> Settings:
    return Settings(
        yunwu_open_key="sk-test",
        llm_main_model="MiniMax-M2.7",
        llm_main_base_url="https://yunwu.ai/v1",
        llm_fast_model="qwen3-local",
        llm_fast_base_url="http://100.76.3.59:7905/v1",
        llm_local_api_key="EMPTY",
        llm_main_max_tokens=80_000,
        llm_fast_max_tokens=512,
    )


@pytest.mark.unit
def test_reasoning_model_detection() -> None:
    assert _is_reasoning("MiniMax-M2.7")
    assert _is_reasoning("qwen3-local")
    assert _is_reasoning("GLM-5.1")
    assert not _is_reasoning("gpt-4")
    assert not _is_reasoning("Kimi-K2.6")


@pytest.mark.unit
def test_fast_api_key_uses_gateway_token_when_local_key_is_placeholder() -> None:
    settings = Settings(
        llm_main_base_url="https://main.example/v1",
        llm_fast_base_url="https://fast.example/v1",
        llm_local_api_key="EMPTY",
        heyi_gateway_token="gateway-token",
    )
    assert settings.llm_fast_api_key == "gateway-token"


@pytest.mark.unit
def test_fast_api_key_prefers_dedicated_key_and_main_endpoint_key() -> None:
    dedicated = Settings(
        llm_main_base_url="https://main.example/v1",
        llm_fast_base_url="https://fast.example/v1",
        llm_local_api_key="dedicated-token",
        heyi_gateway_token="gateway-token",
    )
    shared = Settings(
        llm_main_base_url="https://shared.example/v1",
        llm_fast_base_url="https://shared.example/v1/",
        yunwu_open_key="main-token",
        heyi_gateway_token="gateway-token",
    )
    assert dedicated.llm_fast_api_key == "dedicated-token"
    assert shared.llm_fast_api_key == "main-token"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pick_routes_to_main_or_fast(settings: Settings) -> None:
    llm = OpenAICompatibleLLM(settings)
    try:
        client_m, model_m, max_m = llm._pick(None)
        assert model_m == "MiniMax-M2.7"
        assert max_m == 80_000

        client_f, model_f, max_f = llm._pick("qwen3-local")
        assert model_f == "qwen3-local"
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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pick_explicit_fast_reuses_main_when_main_and_fast_share_endpoint() -> None:
    settings = Settings(
        yunwu_open_key="sk-test",
        llm_main_model="MiniMax-M2.7",
        llm_main_base_url="https://yunwu.ai/v1",
        llm_fast_model="MiniMax-M2.7",
        llm_fast_base_url="https://yunwu.ai/v1",
        llm_main_max_tokens=4096,
        llm_fast_max_tokens=512,
    )
    llm = OpenAICompatibleLLM(settings)
    try:
        client_m, model_m, max_m = llm._pick(None)
        client_f, model_f, max_f = llm._pick("MiniMax-M2.7")
        assert model_m == model_f == "MiniMax-M2.7"
        assert max_m == 4096
        assert max_f == 512
        assert client_f is client_m
    finally:
        await llm.aclose()
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


@pytest.mark.unit
def test_strip_thinking_complete_block() -> None:
    assert _strip_thinking("<think>let me think</think>final answer") == "final answer"


@pytest.mark.unit
def test_strip_thinking_only_close_tag() -> None:
    """Yunwu 代理 minimax 时，``<think>`` 标签可能被截断，只剩 ``</think>``。"""
    assert _strip_thinking("just thinking</think>final") == "final"


@pytest.mark.unit
def test_strip_thinking_no_tag() -> None:
    assert _strip_thinking("plain content") == "plain content"


@pytest.mark.unit
def test_think_stripper_swallows_until_close() -> None:
    s = _ThinkStripper()
    parts = ["<think>", "long ", "reason", "ing</think>", "real ", "answer"]
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert out == "real answer"


@pytest.mark.unit
def test_think_stripper_no_think_passes_through() -> None:
    s = _ThinkStripper()
    parts = ["hello ", "world"]
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert out == "hello world"
