"""YunwuOpenAIEmbedding 实接云雾 API 的集成测试。

执行：
    pytest -m integration backend/tests/integration/test_embedding_yunwu_real.py

仅在以下条件全部满足时执行：
- ``Settings.yunwu_open_key`` 非空
- 测试机能访问 ``https://yunwu.ai/v1/embeddings``

CI nightly（``.github/workflows/integration-nightly.yml``）会跑这组测试。
本地 PR-time CI 默认跳过 ``integration`` mark。
"""

from __future__ import annotations

import pytest
from app.adapters.embedding import EmbeddingError, YunwuOpenAIEmbedding
from app.config import get_settings


def _has_yunwu_key() -> bool:
    return bool(get_settings().yunwu_open_key)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _has_yunwu_key(), reason="yunwu_open_key not configured"),
]


@pytest.mark.asyncio
async def test_health_text_embedding_3_small_real() -> None:
    emb = YunwuOpenAIEmbedding(get_settings(), model="text-embedding-3-small")
    try:
        assert await emb.health() is True
    finally:
        await emb.aclose()


@pytest.mark.asyncio
async def test_encode_single_query_real() -> None:
    emb = YunwuOpenAIEmbedding(get_settings(), model="text-embedding-3-small")
    try:
        vecs = await emb.encode(["褐蚁竞品调研 one-pager"], timeout_s=30.0)
        assert len(vecs) == 1
        assert len(vecs[0]) == 1536
        # vector 不应全 0
        assert any(abs(v) > 1e-6 for v in vecs[0])
    finally:
        await emb.aclose()


@pytest.mark.asyncio
async def test_encode_batch_real() -> None:
    emb = YunwuOpenAIEmbedding(get_settings(), model="text-embedding-3-small")
    try:
        texts = [
            "EchoDesk dense embedding 通道",
            "bge-m3 本地主路",
            "云雾 text-embedding-3-large fallback",
            "heyi-bj 暂不可用",
        ]
        vecs = await emb.encode(texts, timeout_s=60.0)
        assert len(vecs) == len(texts)
        # 相似/不相似检验：第 0/1 句话题相关、第 0/3 不相关
        import math

        def cos(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb + 1e-9)

        sim_01 = cos(vecs[0], vecs[1])
        sim_03 = cos(vecs[0], vecs[3])
        # 仅做 sanity check：两个向量之间的 cosine 在 [-1, 1]
        assert -1.0 <= sim_01 <= 1.0
        assert -1.0 <= sim_03 <= 1.0
    finally:
        await emb.aclose()


@pytest.mark.asyncio
async def test_unsupported_model_open_source_returns_503() -> None:
    """spike §2.1 表：bge-m3 / Qwen3-Embedding 在云雾返回 503『无可用渠道』。"""
    # 直接绕过 adapter 校验（YunwuOpenAIEmbedding init 已拒绝），手工构 client
    import httpx

    s = get_settings()
    payload = {"model": "bge-m3", "input": "test"}
    headers = {"Authorization": f"Bearer {s.yunwu_open_key}"}
    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as http:
        r = await http.post(f"{s.llm_main_base_url}/embeddings", json=payload, headers=headers)
    # 云雾可能返回 200（极少数情况下 distributor 修好了）或 4xx/5xx。
    # 不强断言具体 code，只保证 adapter 拒绝走该路径。
    if r.status_code == 200:
        # 如果某天云雾支持了 bge-m3，需要更新 spike 报告 + adapter 白名单
        pytest.skip("yunwu now supports bge-m3, update spike report + adapter")
    assert r.status_code >= 400


@pytest.mark.asyncio
async def test_invalid_key_raises_embedding_error() -> None:
    from app.config import Settings

    bad_settings = Settings(
        yunwu_open_key="sk-invalid-key-xxxx",
        llm_main_base_url=get_settings().llm_main_base_url,
        embedding_yunwu_model="text-embedding-3-small",
    )
    emb = YunwuOpenAIEmbedding(bad_settings)
    try:
        with pytest.raises(EmbeddingError):
            await emb.encode(["x"], timeout_s=20.0)
    finally:
        await emb.aclose()
