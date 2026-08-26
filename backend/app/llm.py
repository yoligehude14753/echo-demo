"""Legacy LLM facade retained for old pipeline call sites.

All model I/O is delegated to the canonical Echo adapter; historical provider
and fallback selection is intentionally no longer available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, NoReturn

from app.adapters.llm import OpenAICompatibleLLM
from app.config import get_settings
from app.schemas.llm import ChatMessage

def register_tool(_schema: dict[str, Any]) -> NoReturn:
    """Reject obsolete registration instead of silently discarding tools."""

    raise ValueError("legacy LLM facade does not support tool calling")


def _messages(messages: list[dict[str, Any]]) -> list[ChatMessage]:
    return [ChatMessage(role=str(item.get("role", "user")), content=str(item.get("content", ""))) for item in messages]


def _reject_tools(tools: list[dict] | None) -> None:
    if tools:
        raise ValueError("legacy LLM facade does not support tool calling")


async def complete(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    stream: bool | None = None,
    model: str | None = None,
    *,
    temperature: float = 0.3,
    top_p: float | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    seed: int | None = None,
) -> str:
    _reject_tools(tools)
    adapter = OpenAICompatibleLLM(get_settings())
    try:
        options = {
            "model": model,
            "temperature": temperature,
            "top_p": top_p,
            "min_p": min_p,
            "repetition_penalty": repetition_penalty,
            "seed": seed,
        }
        if stream:
            return "".join([chunk async for chunk in adapter.chat_stream(_messages(messages), **options)])
        return (await adapter.chat(_messages(messages), **options)).content
    finally:
        await adapter.aclose()


async def complete_nano(messages: list[dict[str, Any]]) -> str:
    return await complete(messages)


async def complete_with_fallback(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    *,
    temperature: float = 0.3,
    top_p: float | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    seed: int | None = None,
) -> str:
    return await complete(
        messages,
        tools=tools,
        model=model,
        temperature=temperature,
        top_p=top_p,
        min_p=min_p,
        repetition_penalty=repetition_penalty,
        seed=seed,
    )


async def stream_complete(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    *,
    temperature: float = 0.3,
    top_p: float | None = None,
    min_p: float | None = None,
    repetition_penalty: float | None = None,
    seed: int | None = None,
) -> AsyncIterator[str]:
    _reject_tools(tools)
    adapter = OpenAICompatibleLLM(get_settings())
    try:
        async for chunk in adapter.chat_stream(
            _messages(messages),
            model=model,
            temperature=temperature,
            top_p=top_p,
            min_p=min_p,
            repetition_penalty=repetition_penalty,
            seed=seed,
        ):
            yield chunk
    finally:
        await adapter.aclose()


async def complete_with_tools(
    messages: list[dict[str, Any]],
    tool_handlers: dict[str, Any],
    tools: list[dict] | None = None,
    max_rounds: int = 5,
    model: str | None = None,
) -> str:
    if tool_handlers or tools or max_rounds != 5:
        raise ValueError("legacy LLM facade does not support tool calling")
    return await complete(messages, model=model)
