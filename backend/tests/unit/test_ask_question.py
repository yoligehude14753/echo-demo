"""use_case ask_question 单测（mock LLMPort）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.schemas.llm import ChatMessage
from app.use_cases.ask_question import ask_question


class FakeLLM:
    """实现 LLMPort 协议的最小 fake。"""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.captured_messages: list[ChatMessage] | None = None
        self.captured_model: str | None = None

    async def chat(self, messages: list[ChatMessage], **_: Any) -> Any:
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        **_: Any,
    ) -> AsyncIterator[str]:
        self.captured_messages = list(messages)
        self.captured_model = model
        for c in self.chunks:
            yield c


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ask_question_streams_chunks() -> None:
    llm = FakeLLM(["你", "好", "！"])
    out: list[str] = []
    async for c in ask_question(llm, "你好"):
        out.append(c)
    assert out == ["你", "好", "！"]
    assert llm.captured_messages is not None
    assert llm.captured_messages[0].role == "system"
    assert "EchoDesk" in llm.captured_messages[0].content
    assert llm.captured_messages[-1].content == "你好"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ask_question_propagates_history() -> None:
    llm = FakeLLM(["ok"])
    history = [
        ChatMessage(role="user", content="先前问题"),
        ChatMessage(role="assistant", content="先前答复"),
    ]
    async for _ in ask_question(llm, "继续", history=history):
        pass
    assert llm.captured_messages is not None
    roles = [m.role for m in llm.captured_messages]
    assert roles == ["system", "user", "assistant", "user"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ask_question_forwards_model() -> None:
    llm = FakeLLM(["x"])
    async for _ in ask_question(llm, "hi", model="fast-test-model"):
        pass
    assert llm.captured_model == "fast-test-model"
