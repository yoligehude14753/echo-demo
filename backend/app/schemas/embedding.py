"""Embedding 请求/响应共用 schema。

dense retrieval 通道在 2026-05-28 spike 决策（见
``docs/rag_embedding_spike_2026-05-28.md``）后落地：

- **主路**：本地 ``BAAI/bge-m3`` (sentence-transformers + hnswlib)
- **fallback**：云雾 ``text-embedding-3-large`` （OpenAI 兼容）
- **预留**：heyi-bj 远端，等运维起 ``sglang --is-embedding`` 后接入

本 schema 仅记录 adapter 出参的结构化元数据，便于 vector store 写 header /
RAG eval 时定位模型版本漂移。在线 query/encode 走 ``EmbeddingPort.encode``
直接返回 ``list[list[float]]``，不强求构造此对象（避免对热路径加 dict 开销）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmbeddingResult(BaseModel):
    """Embedding 调用的结构化结果。"""

    model: str
    dim: int
    vectors: list[list[float]] = Field(default_factory=list)
    prompt_tokens: int = 0
    latency_ms: float = 0.0
