"""LLM Port：屏蔽 Yunwu / heyi-local 等供应商差异。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.schemas.llm import ChatMessage, LLMResponse


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
        timeout_s: float = 120.0,
    ) -> LLMResponse: ...

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout_s: float = 600.0,
    ) -> AsyncIterator[str]:
        """Async generator：调用方使用 `async for chunk in port.chat_stream(...)`."""
        ...
