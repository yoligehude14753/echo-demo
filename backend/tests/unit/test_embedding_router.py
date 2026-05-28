"""EmbeddingRouter 单测：主备切换、failure transient、metadata 传播。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from app.adapters.embedding import EmbeddingError, EmbeddingRouter


class _FakeEmbedding:
    """testing helper：实现 EmbeddingPort 协议。"""

    def __init__(
        self,
        *,
        model_name: str,
        dim: int,
        healthy: bool = True,
        raise_on_encode: bool = False,
        encode_calls: list[int] | None = None,
    ) -> None:
        self._name = model_name
        self._dim = dim
        self._healthy = healthy
        self._raise = raise_on_encode
        self.encode_calls = encode_calls if encode_calls is not None else []

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def max_input_tokens(self) -> int:
        return 8192

    async def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        del batch_size, timeout_s, is_query
        self.encode_calls.append(len(texts))
        if self._raise:
            raise EmbeddingError(f"{self._name} forced failure")
        return [[0.1] * self._dim for _ in texts]

    async def health(self) -> bool:
        return self._healthy

    def set_raise(self, v: bool) -> None:
        self._raise = v


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_uses_primary_when_present() -> None:
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    out = await router.encode(["a", "b"])
    assert len(out) == 2
    assert len(out[0]) == 1024
    assert primary.encode_calls == [2]
    assert fallback.encode_calls == []
    assert router.active_provider == "local/bge-m3"
    assert router.dim == 1024


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_uses_fallback_when_no_primary() -> None:
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072)
    router = EmbeddingRouter(primary=None, fallback=fallback)

    out = await router.encode(["x"])
    assert len(out[0]) == 3072
    assert router.active_provider == "yunwu/3-large"
    assert router.has_primary is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_falls_back_on_primary_error() -> None:
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024, raise_on_encode=True)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    out = await router.encode(["x", "y"])
    assert len(out) == 2
    assert len(out[0]) == 3072
    assert primary.encode_calls == [2]
    assert fallback.encode_calls == [2]
    # transient 失败不长期切换
    assert router._primary_healthy is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_recovers_after_transient_failure() -> None:
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024, raise_on_encode=True)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    out1 = await router.encode(["x"])
    assert len(out1[0]) == 3072

    primary.set_raise(False)
    out2 = await router.encode(["y", "z"])
    assert len(out2[0]) == 1024
    assert primary.encode_calls == [1, 2]
    assert fallback.encode_calls == [1]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_health_caches_primary_unhealthy() -> None:
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024, healthy=False)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072, healthy=True)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    assert await router.health() is True
    assert router._primary_healthy is False
    # 此后 encode 直接走 fallback 不再尝试 primary
    await router.encode(["x"])
    assert primary.encode_calls == []
    assert fallback.encode_calls == [1]
    assert router.active_provider == "yunwu/3-large"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_health_both_dead_returns_false() -> None:
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024, healthy=False)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072, healthy=False)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    assert await router.health() is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_router_encode_empty_short_circuits() -> None:
    """空入参时 router 应直接返回 []，不调任何 adapter。"""
    primary = _FakeEmbedding(model_name="local/bge-m3", dim=1024)
    fallback = _FakeEmbedding(model_name="yunwu/3-large", dim=3072)
    router = EmbeddingRouter(primary=primary, fallback=fallback)

    out = await router.encode([])
    assert out == []
    assert primary.encode_calls == []
    assert fallback.encode_calls == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_checkable_protocol_compatibility() -> None:
    """fake adapter 必须能通过 EmbeddingPort 的 runtime isinstance 检查。"""
    from app.ports.embedding import EmbeddingPort

    fake: Any = _FakeEmbedding(model_name="x", dim=8)
    assert isinstance(fake, EmbeddingPort)
