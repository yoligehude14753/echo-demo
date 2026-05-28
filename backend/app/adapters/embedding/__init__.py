"""Embedding adapter 集合。

主路 + fallback 路由组合：
- ``BgeM3LocalEmbedding``：sentence-transformers + BAAI/bge-m3，主路、本地 CPU
- ``YunwuOpenAIEmbedding``：云雾 OpenAI 兼容 ``/v1/embeddings``，fallback
- ``EmbeddingRouter``：包装一主一备，health 失败自动切

见 ``docs/rag_embedding_spike_2026-05-28.md`` 选型论证。
"""

from app.adapters.embedding.errors import EmbeddingError
from app.adapters.embedding.router import EmbeddingRouter
from app.adapters.embedding.yunwu import YunwuOpenAIEmbedding

__all__ = [
    "EmbeddingError",
    "EmbeddingRouter",
    "YunwuOpenAIEmbedding",
]


def _try_import_bge_m3_local() -> type | None:
    """懒导出 ``BgeM3LocalEmbedding``：依赖 ``sentence-transformers`` 重包，
    包未装时不阻塞 backend 启动（fallback 走 yunwu）。
    """
    try:
        from app.adapters.embedding.bge_m3_local import BgeM3LocalEmbedding

        return BgeM3LocalEmbedding
    except ImportError:
        return None


BgeM3LocalEmbedding = _try_import_bge_m3_local()
if BgeM3LocalEmbedding is not None:
    __all__.append("BgeM3LocalEmbedding")
