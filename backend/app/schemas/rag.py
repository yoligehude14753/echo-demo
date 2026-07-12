"""RAG / 检索 schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RagChunk(BaseModel):
    doc_id: str
    doc_title: str
    chunk_id: str
    text: str
    score: float = 0.0
    metadata: dict[str, str] = Field(default_factory=dict)


class WebHit(BaseModel):
    title: str
    url: str
    snippet: str
    score: float = 0.0
    source: str = ""  # tavily / ddg


class RetrievalResult(BaseModel):
    query: str
    rag_chunks: list[RagChunk] = Field(default_factory=list)
    web_hits: list[WebHit] = Field(default_factory=list)
    arbitration: dict[str, float] = Field(default_factory=dict)
    chosen_source: str = "rag"  # rag / web / both / none


class RagAnswerSource(BaseModel):
    """One final citation exposed by the RAG answer stream."""

    kind: Literal["rag", "web"]
    doc_id: str | None = None
    chunk_id: str | None = None
    title: str | None = None
    page: str | None = None
    url: str | None = None
    source: str | None = None
    score: float = 0.0


class RagAnswerTrace(BaseModel):
    """Retrieval/arbitration trace emitted only after generation succeeds."""

    query: str
    chosen_source: str
    arbitration: dict[str, float] = Field(default_factory=dict)
    rag_count: int = 0
    web_count: int = 0


class RagAnswerMeta(BaseModel):
    """Legacy-compatible metadata view consumed by the current desktop parser."""

    chosen_source: str
    rag_count: int = 0
    web_count: int = 0
    citations: list[RagAnswerSource] = Field(default_factory=list)


class RagAnswerDeltaEvent(BaseModel):
    type: Literal["delta"] = "delta"
    delta: str


class RagAnswerDoneEvent(BaseModel):
    type: Literal["done"] = "done"
    answer: str
    sources: list[RagAnswerSource] = Field(default_factory=list)
    trace: RagAnswerTrace
    meta: RagAnswerMeta


class RagAnswerErrorTrace(BaseModel):
    phase: Literal["generation"] = "generation"
    partial_chars: int = 0


class RagAnswerErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: Literal["answer_generation_failed"] = "answer_generation_failed"
    error: Literal["暂时无法生成回答，请稍后重试"] = "暂时无法生成回答，请稍后重试"
    trace: RagAnswerErrorTrace
