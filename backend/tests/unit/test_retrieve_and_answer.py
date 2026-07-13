"""retrieve_and_answer use_case 单测：mock LLM/RAG/Web。"""

from __future__ import annotations

from typing import Any

import pytest
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.schemas.rag import RagChunk, WebHit
from app.use_cases.retrieve_and_answer import retrieve_and_answer


class FakeLLM:
    def __init__(
        self, classify_label: str = "either", answer_chunks: list[str] | None = None
    ) -> None:
        self.classify_label = classify_label
        self.answer_chunks = answer_chunks or ["答", "复"]
        self.classify_calls: list[str] = []
        self.answer_messages: list[ChatMessage] | None = None
        self.answer_kwargs: dict[str, Any] | None = None

    async def chat(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        if messages and "只能输出三个标签之一" in messages[0].content:
            self.classify_calls.append(messages[-1].content)
            content = self.classify_label
        else:
            self.answer_messages = list(messages)
            self.answer_kwargs = dict(kw)
            content = "".join(self.answer_chunks)
        return LLMResponse(
            content=content,
            model="qwen3",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=12.0,
        )


class FakeRag:
    def __init__(self, chunks: list[RagChunk]) -> None:
        self.chunks = chunks
        self.query_count = 0

    async def query(self, q: str, *, top_k: int = 5) -> list[RagChunk]:
        self.query_count += 1
        return list(self.chunks)

    async def ingest_pdf(self, path: str, doc_title: str | None = None) -> str:
        return "fake"

    async def ingest_meeting(
        self,
        meeting_id: str,
        transcript: str,
        title: str,
        *,
        projection_generation: int | None = None,
    ) -> str:
        _ = projection_generation
        return f"meeting-{meeting_id}"

    async def delete(
        self,
        doc_id: str,
        *,
        projection_generation: int | None = None,
    ) -> None:
        _ = doc_id, projection_generation


class FakeWeb:
    def __init__(self, hits: list[WebHit]) -> None:
        self.hits = hits
        self.search_count = 0

    async def search(self, q: str, *, top_n: int = 5) -> list[WebHit]:
        self.search_count += 1
        return list(self.hits)


class FailingAnswerLLM(FakeLLM):
    async def chat(self, messages: list[ChatMessage], **kw: Any) -> LLMResponse:
        if messages and "只能输出三个标签之一" in messages[0].content:
            return await super().chat(messages, **kw)
        raise RuntimeError("answer backend unavailable")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rag_only_branch_skips_web() -> None:
    llm = FakeLLM(classify_label="rag")
    rag = FakeRag(
        [RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="本地证据 1", score=0.8)]
    )
    web = FakeWeb([WebHit(title="x", url="u", snippet="y")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="本地 PDF 提到什么?",
    )
    chunks = [c async for c in out.chunks]
    assert "".join(chunks) == "- 本地证据 1 [doc:d-c1]"
    assert rag.query_count == 1
    assert web.search_count == 0
    assert out.retrieval.chosen_source == "rag"
    assert len(out.retrieval.rag_chunks) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_web_only_branch_skips_rag() -> None:
    llm = FakeLLM(classify_label="web")
    rag = FakeRag([RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="本地", score=0.5)])
    web = FakeWeb([WebHit(title="今日新闻", url="https://news/", snippet="...", source="tavily")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="今天有什么新闻?",
    )
    _ = [c async for c in out.chunks]
    assert web.search_count == 1
    assert rag.query_count == 0
    assert out.retrieval.chosen_source == "web"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_either_branch_runs_both() -> None:
    llm = FakeLLM(classify_label="either")
    rag = FakeRag([RagChunk(doc_id="d", doc_title="t", chunk_id="c1", text="local")])
    web = FakeWeb([WebHit(title="x", url="u", snippet="y")])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="ambiguous query",
    )
    _ = [c async for c in out.chunks]
    assert rag.query_count == 1
    assert web.search_count == 1
    assert out.retrieval.chosen_source == "both"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_classifier_garbage_falls_back_to_either() -> None:
    llm = FakeLLM(classify_label="garbage output 我不懂")
    rag = FakeRag([])
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="x",
    )
    _ = [c async for c in out.chunks]
    assert rag.query_count == 1
    assert web.search_count == 1
    assert out.retrieval.chosen_source == "none"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_prompt_includes_citations() -> None:
    llm = FakeLLM(classify_label="rag")
    rag = FakeRag(
        [
            RagChunk(
                doc_id="pdf-1",
                doc_title="财报",
                chunk_id="c1",
                text="2024 年营收 100 亿",
                score=0.9,
            )
        ]
    )
    web = FakeWeb([])
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=rag,
        web=web,
        question="财报数据?",
    )
    _ = [c async for c in out.chunks]
    assert llm.answer_messages is not None
    body = llm.answer_messages[0].content
    assert "2024 年营收 100 亿" in body
    assert "[doc:pdf-1-c1]" in body
    assert "财报" in body
    assert llm.answer_kwargs is not None
    assert llm.answer_kwargs["max_tokens"] == 768
    assert llm.answer_kwargs["timeout_s"] == 60.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_zero_evidence_skips_answer_generation_and_returns_short_notice() -> None:
    llm = FailingAnswerLLM(classify_label="rag")
    out = await retrieve_and_answer(
        main_llm=llm,
        fast_llm=llm,
        fast_model="qwen3",
        rag=FakeRag([]),
        web=FakeWeb([]),
        question="must fail",
    )
    chunks = [chunk async for chunk in out.chunks]
    assert "".join(chunks) == "当前没有足够的可用证据。"
    assert llm.answer_messages is None
