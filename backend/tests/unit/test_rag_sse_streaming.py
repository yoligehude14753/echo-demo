"""True incremental /rag/ask SSE protocol, failure and cancellation tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from app.api.retrieval import _sse
from app.schemas.llm import ChatMessage, LLMResponse
from app.schemas.rag import RagChunk
from app.use_cases.retrieve_and_answer import retrieve_and_answer
from starlette.requests import Request

_SUPPORTED_LINE = "grounded evidence [doc:doc-1-chunk-1]"


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _Rag:
    async def query(self, _query: str, *, top_k: int = 5) -> list[RagChunk]:
        _ = top_k
        return [
            RagChunk(
                doc_id="doc-1",
                doc_title="Local source",
                chunk_id="chunk-1",
                text="grounded evidence",
                score=0.9,
                metadata={"page": "3"},
            )
        ]


class _Web:
    async def search(self, _query: str, *, top_n: int = 5) -> list[Any]:
        _ = top_n
        return []


class _BaseStreamingLLM:
    async def chat(self, _messages: list[ChatMessage], **_kwargs: Any) -> LLMResponse:
        return LLMResponse(content="rag", model="classifier")


class _GatedStreamingLLM(_BaseStreamingLLM):
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.completed = False
        self.closed = asyncio.Event()

    async def chat_stream(
        self,
        _messages: list[ChatMessage],
        **_kwargs: Any,
    ) -> AsyncIterator[str]:
        try:
            yield f"{_SUPPORTED_LINE}\n"
            await self.release.wait()
            yield _SUPPORTED_LINE
            self.completed = True
        finally:
            self.closed.set()


class _FailingStreamingLLM(_BaseStreamingLLM):
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def chat_stream(
        self,
        _messages: list[ChatMessage],
        **_kwargs: Any,
    ) -> AsyncIterator[str]:
        try:
            yield f"{_SUPPORTED_LINE}\n"
            raise RuntimeError(
                "answer stream exploded at https://provider.invalid/private /tmp/secret-key"
            )
        finally:
            self.closed.set()


class _BlockingStreamingLLM(_BaseStreamingLLM):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()
        self.never = asyncio.Event()

    async def chat_stream(
        self,
        _messages: list[ChatMessage],
        **_kwargs: Any,
    ) -> AsyncIterator[str]:
        try:
            yield f"{_SUPPORTED_LINE}\n"
            self.waiting.set()
            await self.never.wait()
            yield "unreachable"
        finally:
            self.closed.set()


def _decode_frame(frame: bytes) -> tuple[str, dict[str, Any]]:
    text = frame.decode()
    event = next(line[7:] for line in text.splitlines() if line.startswith("event: "))
    data = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
    return event, cast(dict[str, Any], json.loads(data))


async def _answer(llm: _BaseStreamingLLM):  # type: ignore[no-untyped-def]
    return await retrieve_and_answer(
        main_llm=llm,  # type: ignore[arg-type]
        fast_llm=llm,  # type: ignore[arg-type]
        fast_model="classifier",
        rag=_Rag(),  # type: ignore[arg-type]
        web=_Web(),  # type: ignore[arg-type]
        question="what is local?",
        stream=True,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_delta_arrives_before_full_answer_and_done_has_metadata() -> None:
    llm = _GatedStreamingLLM()
    body = _sse(cast(Request, _ConnectedRequest()), await _answer(llm))

    first_frame = await asyncio.wait_for(anext(body), timeout=0.5)
    event, payload = _decode_frame(first_frame)
    assert event == "delta"
    assert payload == {"type": "delta", "delta": f"{_SUPPORTED_LINE}\n"}
    assert llm.completed is False
    assert llm.release.is_set() is False

    llm.release.set()
    remaining = [_decode_frame(frame) async for frame in body]
    assert [item[0] for item in remaining] == ["delta", "done"]
    done = remaining[-1][1]
    assert done["type"] == "done"
    assert done["answer"] == f"{_SUPPORTED_LINE}\n{_SUPPORTED_LINE}"
    assert done["sources"][0]["doc_id"] == "doc-1"
    assert done["sources"][0]["page"] == "3"
    assert done["trace"]["chosen_source"] == "rag"
    assert done["meta"]["citations"][0]["chunk_id"] == "chunk-1"
    assert llm.completed is True
    assert llm.closed.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_midstream_failure_emits_error_without_done_and_rethrows() -> None:
    llm = _FailingStreamingLLM()
    body = _sse(cast(Request, _ConnectedRequest()), await _answer(llm))

    first = _decode_frame(await anext(body))
    error = _decode_frame(await anext(body))
    assert first[0] == "delta"
    assert error[0] == "error"
    assert error[1]["type"] == "error"
    assert error[1]["code"] == "answer_generation_failed"
    assert error[1]["error"] == "暂时无法生成回答，请稍后重试"
    assert error[1]["trace"] == {
        "phase": "generation",
        "partial_chars": len(_SUPPORTED_LINE) + 1,
    }
    assert "provider.invalid" not in json.dumps(error[1], ensure_ascii=False)
    assert "/tmp/secret-key" not in json.dumps(error[1], ensure_ascii=False)
    with pytest.raises(RuntimeError, match="exploded"):
        await anext(body)
    assert llm.closed.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_response_iteration_closes_blocked_upstream() -> None:
    llm = _BlockingStreamingLLM()
    body = _sse(cast(Request, _ConnectedRequest()), await _answer(llm))
    assert _decode_frame(await anext(body))[0] == "delta"

    pending = asyncio.create_task(anext(body))
    await asyncio.wait_for(llm.waiting.wait(), timeout=0.5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await asyncio.wait_for(llm.closed.wait(), timeout=0.5)
    assert pending.done()
