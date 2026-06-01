"""RAG 工厂：根据 settings.embedding_enabled 决定 HybridRag 或 BM25Rag。

落地策略（rag_redesign_2026-05-28 §C.3 phase5-hybrid-rag）：

- ``embedding_enabled=False`` → 纯 BM25（向后兼容/灰度回退）
- ``embedding_enabled=True`` + 依赖 OK → ``HybridRag(BM25, EmbeddingRouter, VectorStore)``
- ``embedding_enabled=True`` 但 ``BgeM3LocalEmbedding`` 导入失败 / 模型未下载
  → 主路 None → ``EmbeddingRouter`` 跑 fallback（yunwu）。
- 任何异常（vector store 初始化、router 构造）→ log warning + 回退 BM25。
  graceful degradation 硬要求：HybridRag 启动失败绝不能让 backend 起不来。

返回 ``RagPort`` 协议实例；上层 api / use_case 都按 ``RagPort`` 用，
看不见底层差异。
"""

from __future__ import annotations

import logging

from app.adapters.embedding import (
    BgeM3LocalEmbedding,
    EmbeddingRouter,
    YunwuOpenAIEmbedding,
)
from app.adapters.rag.bm25 import BM25Rag
from app.adapters.rag.hybrid import HybridRag
from app.adapters.rag.vector_store import VectorStore, VectorStoreError
from app.config import Settings
from app.ports.rag import RagPort

logger = logging.getLogger(__name__)


def build_rag(settings: Settings) -> RagPort:
    bm25 = BM25Rag(settings)
    if not settings.embedding_enabled:
        return bm25

    try:
        primary = None
        if BgeM3LocalEmbedding is not None:
            try:
                primary = BgeM3LocalEmbedding(settings)
            except Exception as e:  # ImportError 已在模块加载阶段捕获
                logger.warning("bge-m3 local embedding init failed; falling back to yunwu: %s", e)
                primary = None
        fallback = YunwuOpenAIEmbedding(settings)
        router = EmbeddingRouter(primary=primary, fallback=fallback)

        # 主路 dim 与 fallback dim 可能不同；vector store 跟随实际 active provider
        # 的 dim。primary 不为 None 时按 primary（bge-m3=1024）；否则按 fallback。
        dim = router.dim
        vector_store = VectorStore(settings, dim=dim)
        logger.info(
            "HybridRag enabled: embedding=%s dim=%d vector_dir=%s",
            router.active_provider,
            dim,
            vector_store.index_dir,
        )
        return HybridRag(bm25, router, vector_store, settings)
    except VectorStoreError as e:
        logger.warning("vector store init failed → BM25-only: %s", e)
        return bm25
    except Exception as e:  # 启动期任何意外都不能挂掉 backend
        logger.warning("HybridRag bootstrap failed → BM25-only: %s", e)
        return bm25
