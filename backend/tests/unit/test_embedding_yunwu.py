"""YunwuOpenAIEmbedding adapter 单测（不接外部服务）。

验证：
- model_name / dim / max_input_tokens 与 spike 表对齐
- encode 单串 & 多串走 client.embeddings.create
- batch > 32 自动切片
- APIError / Timeout 包装为 EmbeddingError
- 不支持的模型在 __init__ 即报错
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.embedding import EmbeddingError, YunwuOpenAIEmbedding
from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        yunwu_open_key="sk-test",
        llm_main_base_url="https://yunwu.ai/v1",
        embedding_yunwu_model="text-embedding-3-large",
    )


def _fake_resp(n: int, dim: int) -> MagicMock:
    resp = MagicMock()
    resp.data = [MagicMock(embedding=[0.1] * dim) for _ in range(n)]
    return resp


@pytest.mark.unit
def test_default_model_metadata(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings)
    try:
        assert emb.model_name == "yunwu/text-embedding-3-large"
        assert emb.dim == 3072
        assert emb.max_input_tokens == 8191
    finally:
        # 直接构造 + 不发请求，httpx client 在 GC 时关闭即可
        pass


@pytest.mark.unit
def test_override_model_small(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    assert emb.dim == 1536
    assert emb.model_name == "yunwu/text-embedding-3-small"


@pytest.mark.unit
def test_unsupported_model_raises(settings: Settings) -> None:
    with pytest.raises(EmbeddingError, match="unsupported"):
        YunwuOpenAIEmbedding(settings, model="bge-m3")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_single_string(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(return_value=_fake_resp(1, 1536))
        out = await emb.encode(["hello"])
        assert len(out) == 1
        assert len(out[0]) == 1536
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_empty_returns_empty(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(return_value=_fake_resp(0, 1536))
        out = await emb.encode([])
        assert out == []
        emb._client.embeddings.create.assert_not_called()
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_large_batch_is_chunked(settings: Settings) -> None:
    """batch > 32 时按 _MAX_BATCH_SAFE=32 自动切片（spike §2.2 实测尾延迟）。"""
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        # 模拟每次调用返回的 vector 数 = 实际入参 n
        async def _create(model: str, input: list[str]) -> MagicMock:
            return _fake_resp(len(input), 1536)

        emb._client.embeddings.create = AsyncMock(side_effect=_create)
        texts = [f"text {i}" for i in range(100)]
        out = await emb.encode(texts, batch_size=32)
        assert len(out) == 100
        # 100 / 32 → 32+32+32+4 = 4 次
        assert emb._client.embeddings.create.await_count == 4
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_user_batch_capped_at_max_safe(settings: Settings) -> None:
    """即使调用方传 batch_size=128，内部也按 _MAX_BATCH_SAFE=32 切。"""
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:

        async def _create(model: str, input: list[str]) -> MagicMock:
            assert len(input) <= 32, "batch must be capped at 32"
            return _fake_resp(len(input), 1536)

        emb._client.embeddings.create = AsyncMock(side_effect=_create)
        out = await emb.encode(["x"] * 50, batch_size=128)
        assert len(out) == 50
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_timeout_wraps_to_embedding_error(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(EmbeddingError, match="timeout"):
            await emb.encode(["x"], timeout_s=0.01)
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_encode_count_mismatch_raises(settings: Settings) -> None:
    """上游返回 vector 数和入参不对齐时（理论不会发生但兜底） → EmbeddingError。"""
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(return_value=_fake_resp(1, 1536))
        with pytest.raises(EmbeddingError, match="vectors but"):
            await emb.encode(["a", "b"])
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_health_returns_true_on_normal_response(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(return_value=_fake_resp(1, 1536))
        assert await emb.health() is True
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_health_returns_false_on_dim_mismatch(settings: Settings) -> None:
    """上游返回错位维度 → health=False（不抛错）。"""
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        # 返回 8 维 vector，但 model 声明 1536
        emb._client.embeddings.create = AsyncMock(return_value=_fake_resp(1, 8))
        assert await emb.health() is False
    finally:
        await emb.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_health_returns_false_on_timeout(settings: Settings) -> None:
    emb = YunwuOpenAIEmbedding(settings, model="text-embedding-3-small")
    try:
        emb._client.embeddings.create = AsyncMock(side_effect=TimeoutError())
        assert await emb.health() is False
    finally:
        await emb.aclose()
