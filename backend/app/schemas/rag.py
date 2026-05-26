"""RAG / 检索 schema。"""

from __future__ import annotations

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
    chosen_source: str = "rag"  # rag / web / both
