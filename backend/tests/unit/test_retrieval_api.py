"""HTTP /rag/ask SSE 元数据单测。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.api import retrieval as retrieval_api
from app.api.deps import get_llm_singleton as get_llm
from app.main import create_app
from app.schemas.rag import RagChunk, RetrievalResult
from app.use_cases.retrieve_and_answer import AnswerStream
from fastapi.testclient import TestClient


class DummyLLM:
    async def chat(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError

    async def chat_stream(self, *_: Any, **__: Any) -> AsyncIterator[str]:
        yield "unused"


class DummyRag:
    pass


class DummyWeb:
    pass


def dummy_llm() -> DummyLLM:
    return DummyLLM()


def dummy_rag() -> DummyRag:
    return DummyRag()


def dummy_web() -> DummyWeb:
    return DummyWeb()


@pytest.mark.unit
def test_rag_ask_sse_meta_includes_human_citation_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve_and_answer(**_: Any) -> AnswerStream:
        async def chunks() -> AsyncIterator[str]:
            yield "HY100"

        return AnswerStream(
            retrieval=RetrievalResult(
                query="HY100 是什么",
                rag_chunks=[
                    RagChunk(
                        doc_id="pdf-aa9c2de77e3e",
                        doc_title="褐蚁产品手册",
                        chunk_id="pdf-aa9c2de77e3e-p013-c0000",
                        text="HY100 是褐蚁硬件产品线的核心型号，用于会议记录场景。",
                        score=12.34,
                        metadata={"page": "13", "kind": "pdf", "source": "upload"},
                    )
                ],
                web_hits=[],
                chosen_source="rag",
            ),
            chunks=chunks(),
        )

    monkeypatch.setattr(retrieval_api, "retrieve_and_answer", fake_retrieve_and_answer)
    app = create_app()
    app.dependency_overrides[get_llm] = dummy_llm
    app.dependency_overrides[retrieval_api.get_rag] = dummy_rag
    app.dependency_overrides[retrieval_api.get_web] = dummy_web

    client = TestClient(app)
    with client.stream("POST", "/rag/ask", json={"question": "HY100 是什么"}) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode()

    first_data = next(line for line in body.splitlines() if line.startswith("data: "))
    meta = json.loads(first_data[len("data: ") :])["meta"]
    citation = meta["citations"][0]
    assert citation["doc_id"] == "pdf-aa9c2de77e3e"
    assert citation["chunk_id"] == "pdf-aa9c2de77e3e-p013-c0000"
    assert citation["doc_title"] == "褐蚁产品手册"
    assert citation["title"] == "褐蚁产品手册"
    assert citation["page"] == "13"
    assert citation["source"] == "upload"
    assert citation["score"] == 12.34
    assert "HY100 是褐蚁硬件产品线" in citation["text"]
