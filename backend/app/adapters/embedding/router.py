"""Embedding Router：一主一备的运行时切换。

策略：
- 主路（``BgeM3LocalEmbedding`` 本地 bge-m3）就绪 → 全部走主路。
- 主路未就绪（依赖未装 / 模型未下载 / GPU 不可用）→ 回退 fallback
  （``YunwuOpenAIEmbedding`` 云雾 ``text-embedding-3-large``）。
- 主路本次调用临时失败 → 单次 fallback，但**不**长期切换；下一次 encode
  仍优先尝试主路（避免 transient 故障引起永久降级）。

注意：
- ``dim`` / ``model_name`` 暴露的是**当前使用通道**的元数据。Router 实例化时
  二者维度可能不一致（bge-m3=1024 vs text-embedding-3-large=3072），调用方
  在 vector store header 必须以**真实写入 vector 的 adapter** 为准（router
  暴露的 ``model_name`` 在每次 encode 后会更新到最近一次使用的 adapter）。
- spike 报告 §5.3 风险 #3：模型版本漂移 → vector store 检测到不一致后台
  分批重 embed。这层由 ``HybridRag`` 的 ingestion pipeline 处理，router
  本身只负责"挑哪个 adapter 出 vector"。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.adapters.embedding.errors import EmbeddingError
from app.ports.embedding import EmbeddingPort

logger = logging.getLogger(__name__)


class EmbeddingRouter:
    """主备双 adapter 组合，对外仍实现 ``EmbeddingPort``。

    typical 装配（``main.py`` lifespan）：

        primary = BgeM3LocalEmbedding(settings) if has_st else None
        fallback = YunwuOpenAIEmbedding(settings)
        embedding = EmbeddingRouter(primary=primary, fallback=fallback)
    """

    def __init__(
        self,
        *,
        primary: EmbeddingPort | None,
        fallback: EmbeddingPort,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_healthy: bool | None = None  # None=未探测；True/False=已探测
        self._last_used: EmbeddingPort = fallback if primary is None else primary

    @property
    def model_name(self) -> str:
        return self._last_used.model_name

    @property
    def dim(self) -> int:
        return self._last_used.dim

    @property
    def max_input_tokens(self) -> int:
        return self._last_used.max_input_tokens

    @property
    def has_primary(self) -> bool:
        return self._primary is not None

    @property
    def active_provider(self) -> str:
        """运行时实际指向哪个 adapter（debug / metrics 用）。"""
        if self._primary is None:
            return self._fallback.model_name
        if self._primary_healthy is False:
            return self._fallback.model_name
        return self._primary.model_name

    async def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        if not texts:
            return []
        primary = self._primary
        # 主路 None 或已知 unhealthy → 直接 fallback
        if primary is None or self._primary_healthy is False:
            return await self._encode_with(
                self._fallback,
                texts,
                batch_size=batch_size,
                timeout_s=timeout_s,
                is_query=is_query,
            )

        try:
            out = await self._encode_with(
                primary,
                texts,
                batch_size=batch_size,
                timeout_s=timeout_s,
                is_query=is_query,
            )
            self._primary_healthy = True
            return out
        except EmbeddingError as e:
            # 单次失败 → fallback；不把 primary_healthy 设 False（避免 transient 故障引起永久降级）
            logger.warning(
                "embedding primary %s failed, falling back to %s: %s",
                primary.model_name,
                self._fallback.model_name,
                e,
            )
            return await self._encode_with(
                self._fallback,
                texts,
                batch_size=batch_size,
                timeout_s=timeout_s,
                is_query=is_query,
            )

    async def _encode_with(
        self,
        adapter: EmbeddingPort,
        texts: Sequence[str],
        *,
        batch_size: int,
        timeout_s: float,
        is_query: bool,
    ) -> list[list[float]]:
        out = await adapter.encode(
            texts, batch_size=batch_size, timeout_s=timeout_s, is_query=is_query
        )
        self._last_used = adapter
        return out

    async def health(self) -> bool:
        """两路任意一个健康即返回 True；同时把 ``primary_healthy`` 缓存下来。"""
        if self._primary is not None:
            try:
                ok = await self._primary.health()
            except EmbeddingError:
                ok = False
            self._primary_healthy = ok
            if ok:
                return True
        return await self._fallback.health()
