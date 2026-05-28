"""云雾 OpenAI 兼容 ``/v1/embeddings`` adapter。

2026-05-28 spike 实测（``docs/rag_embedding_spike_2026-05-28.md §2``）：
- 可用模型：``text-embedding-3-small`` (1536d)、``text-embedding-3-large`` (3072d)、
  ``text-embedding-ada-002`` (1536d)。
- 不可用：``bge-m3``、``Qwen3-Embedding-0.6B`` → 503 「无可用渠道」。
- batch=32 sweet spot：~244ms/string；batch=64+ 出现 24-57s 尾延迟阶跃。
- 单串 P50=3.5s（**不适合在线 query**），仅作离线回填 + cold-start fallback。

定位：``EmbeddingRouter`` 的 fallback；不直接接入在线 RAG query 路径。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Final

import httpx
from openai import APIError, APITimeoutError, AsyncOpenAI

from app.adapters.embedding.errors import EmbeddingError
from app.config import Settings

_DEFAULT_MODEL: Final[str] = "text-embedding-3-large"
_MODEL_DIMS: Final[dict[str, int]] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
_MAX_INPUT_TOKENS: Final[int] = 8191  # OpenAI v3 系硬上限
_MAX_BATCH_SAFE: Final[int] = 32  # spike 实测 64+ 尾延迟爆炸


class YunwuOpenAIEmbedding:
    """实现 ``ports.embedding.EmbeddingPort`` 的云雾 OpenAI 兼容客户端。

    - 走 ``Settings.llm_main_base_url`` (https://yunwu.ai/v1) + ``yunwu_open_key``
    - 大 batch 自动切 ``_MAX_BATCH_SAFE`` 分片（spike 实测 batch=64+ 尾延迟阶跃）
    - 任何错误抛 ``EmbeddingError``；调用方决定 fallback
    """

    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._model = model or settings.embedding_yunwu_model or _DEFAULT_MODEL
        if self._model not in _MODEL_DIMS:
            raise EmbeddingError(
                f"unsupported yunwu embedding model {self._model!r}; "
                f"supported: {sorted(_MODEL_DIMS)}"
            )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(trust_env=False, timeout=120.0)
        self._client = AsyncOpenAI(
            api_key=settings.yunwu_open_key or "EMPTY",
            base_url=settings.llm_main_base_url,
            http_client=self._http,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ── EmbeddingPort 接口 ───────────────────────────────────────
    @property
    def model_name(self) -> str:
        return f"yunwu/{self._model}"

    @property
    def dim(self) -> int:
        return _MODEL_DIMS[self._model]

    @property
    def max_input_tokens(self) -> int:
        return _MAX_INPUT_TOKENS

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
        effective_batch = min(batch_size, _MAX_BATCH_SAFE)
        out: list[list[float]] = []
        for start in range(0, len(texts), effective_batch):
            chunk = list(texts[start : start + effective_batch])
            vectors = await self._encode_one_batch(chunk, timeout_s=timeout_s)
            out.extend(vectors)
        return out

    async def _encode_one_batch(self, texts: list[str], *, timeout_s: float) -> list[list[float]]:
        try:
            resp = await asyncio.wait_for(
                self._client.embeddings.create(model=self._model, input=texts),
                timeout=timeout_s,
            )
        except (TimeoutError, APITimeoutError) as e:
            raise EmbeddingError(
                f"yunwu {self._model} embedding timeout after {timeout_s}s (batch={len(texts)})"
            ) from e
        except APIError as e:
            raise EmbeddingError(f"yunwu {self._model} embedding api error: {e}") from e

        if len(resp.data) != len(texts):
            raise EmbeddingError(
                f"yunwu returned {len(resp.data)} vectors but {len(texts)} were requested"
            )
        return [list(item.embedding) for item in resp.data]

    async def health(self) -> bool:
        """单串 ping："hi" → 是否能拿到合规 vector。失败返回 False（不抛错）。"""
        try:
            t0 = time.monotonic()
            vecs = await self.encode(["hi"], timeout_s=10.0)
        except EmbeddingError:
            return False
        if not vecs or len(vecs[0]) != self.dim:
            return False
        # latency 仅做 debug 观测，不参与判定
        _ = (time.monotonic() - t0) * 1000
        return True
