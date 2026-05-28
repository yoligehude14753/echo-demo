"""本地 BAAI/bge-m3 embedding adapter（主路）。

2026-05-28 spike 决策（``docs/rag_embedding_spike_2026-05-28.md §4 §5``）：
- 模型尺寸 ~2.27 GB，HF 直连可达 200/2.2s（无需 mirror）
- M2 Pro CPU 估算：query encoding ~50ms、50k chunks 回填 ~7 min
- 同时输出 dense + sparse + ColBERT 三种向量（中期 hybrid 不用换模型）

依赖（``sentence-transformers / torch / hnswlib``）属重量级，
为不阻塞 backend 启动，本 adapter 走**懒加载**：
- 包未装 → ``__init__`` 阶段抛 ``ImportError``，``adapters/embedding/__init__.py``
  会捕获并把 ``BgeM3LocalEmbedding`` 设为 ``None``，调用方走 fallback。
- 包已装 → 模型按需在首次 ``encode`` 时加载（``asyncio.to_thread`` 不阻塞事件循环）。

测试：本 adapter 在 spike 阶段未实跑（``sentence-transformers`` 未装），
落地 PR 后第一次 CI 在 ``backend/requirements-extras-embedding.txt`` 装齐
依赖后由集成测试 ``test_embedding_bge_m3_real.py`` 覆盖。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Final

from app.adapters.embedding.errors import EmbeddingError
from app.config import Settings

_DEFAULT_MODEL_ID: Final[str] = "BAAI/bge-m3"
_BGE_M3_DIM: Final[int] = 1024
_BGE_M3_MAX_TOKENS: Final[int] = 8192


class BgeM3LocalEmbedding:
    """实现 ``ports.embedding.EmbeddingPort`` 的本地 bge-m3 adapter。

    - 包未装时本类直接 ImportError；``adapters/embedding/__init__.py`` 捕获后
      把 ``BgeM3LocalEmbedding`` 暴露为 ``None``，``EmbeddingRouter`` 自动走 fallback。
    - 首次 ``encode`` 触发模型加载（3-8s 冷启动），之后驻留进程。
    - 模型加载 + encode 均走 ``asyncio.to_thread`` 避免阻塞事件循环。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        model_id: str | None = None,
        device: str | None = None,
        cache_dir: str | None = None,
    ) -> None:
        try:
            import sentence_transformers as _st  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "sentence-transformers not installed; "
                "install via `pip install -r backend/requirements-extras-embedding.txt` "
                "or fall back to YunwuOpenAIEmbedding"
            ) from e

        self._settings = settings
        self._model_id = model_id or settings.embedding_bge_m3_model_id or _DEFAULT_MODEL_ID
        self._device = device or settings.embedding_bge_m3_device or "cpu"
        self._cache_dir = cache_dir or (
            str(settings.embedding_bge_m3_cache_dir)
            if settings.embedding_bge_m3_cache_dir
            else None
        )
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return f"local/{self._model_id}"

    @property
    def dim(self) -> int:
        return _BGE_M3_DIM

    @property
    def max_input_tokens(self) -> int:
        return _BGE_M3_MAX_TOKENS

    async def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            self._model = await asyncio.to_thread(self._load_model_sync)
            return self._model

    def _load_model_sync(self) -> Any:
        try:
            from sentence_transformers import SentenceTransformer

            return SentenceTransformer(
                self._model_id,
                device=self._device,
                cache_folder=self._cache_dir,
                trust_remote_code=False,
            )
        except Exception as e:
            raise EmbeddingError(f"bge-m3 load failed: {type(e).__name__}: {e}") from e

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
        model = await self._ensure_loaded()

        def _encode_sync() -> list[list[float]]:
            try:
                arr = model.encode(
                    list(texts),
                    batch_size=batch_size,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            except Exception as e:
                raise EmbeddingError(f"bge-m3 encode failed: {type(e).__name__}: {e}") from e
            return [row.tolist() for row in arr]

        try:
            return await asyncio.wait_for(asyncio.to_thread(_encode_sync), timeout=timeout_s)
        except TimeoutError as e:
            raise EmbeddingError(
                f"bge-m3 encode timeout after {timeout_s}s (n={len(texts)})"
            ) from e

    async def health(self) -> bool:
        """探活：尝试加载模型 + encode 一条短串。"""
        try:
            t0 = time.monotonic()
            vecs = await self.encode(["健康检查"], timeout_s=120.0)
        except EmbeddingError:
            return False
        if not vecs or len(vecs[0]) != self.dim:
            return False
        _ = (time.monotonic() - t0) * 1000
        return True
