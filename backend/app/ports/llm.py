"""LLM Port：屏蔽具体模型供应商差异。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from app.schemas.llm import ChatMessage, LLMResponse

LLMPriority = Literal["foreground", "background"]


@runtime_checkable
class LLMPort(Protocol):
    """主/快 通道统一接口。具体路由策略在 adapter 层完成。"""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        top_p: float | None = None,
        min_p: float | None = None,
        repetition_penalty: float | None = None,
        seed: int | None = None,
        timeout_s: float = 120.0,
        priority: LLMPriority = "foreground",
    ) -> LLMResponse: ...

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        top_p: float | None = None,
        min_p: float | None = None,
        repetition_penalty: float | None = None,
        seed: int | None = None,
        timeout_s: float = 600.0,
        priority: LLMPriority = "foreground",
    ) -> AsyncIterator[str]:
        """Async generator：调用方使用 `async for chunk in port.chat_stream(...)`."""
        ...
