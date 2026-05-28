"""Embedding adapter 异常类型。

与 ``app.adapters.llm.openai_compatible.LLMError`` 同形：所有 adapter 内部
错误都包装为 ``EmbeddingError`` 抛出；调用方（``EmbeddingRouter`` /
``HybridRag``）决定是否走 fallback / 降级到 BM25-only。
"""

from __future__ import annotations


class EmbeddingError(RuntimeError):
    """Embedding 调用失败（网络/超时/上游 5xx/模型加载失败）。"""
