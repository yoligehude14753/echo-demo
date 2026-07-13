from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.api.agents import _bounded_artifact_body, proxy_task_artifact
from app.config import Settings
from fastapi import HTTPException


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x" * 64
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class _AgentServiceStub:
    def __init__(self) -> None:
        self.backend = SimpleNamespace(base_url="http://agentos.test")

    async def get_task(self, task_id: str) -> Any:
        return SimpleNamespace(runner_task_id=f"runner-{task_id}")


@pytest.mark.unit
async def test_agent_artifact_proxy_streams_without_buffering_whole_body() -> None:
    stream = _ChunkStream([b"abc", b"def"])
    upstream = httpx.Response(200, stream=stream)
    client = httpx.AsyncClient()

    body = b"".join(
        [
            chunk
            async for chunk in _bounded_artifact_body(
                upstream,
                client,
                max_bytes=6,
                chunk_bytes=2,
            )
        ]
    )

    assert body == b"abcdef"
    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_agent_artifact_proxy_aborts_chunked_body_over_limit() -> None:
    stream = _ChunkStream([b"abcd", b"efgh"])
    upstream = httpx.Response(200, stream=stream)
    client = httpx.AsyncClient()

    with pytest.raises(RuntimeError, match="proxy size limit"):
        _ = [
            chunk
            async for chunk in _bounded_artifact_body(
                upstream,
                client,
                max_bytes=7,
                chunk_bytes=4,
            )
        ]

    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_agent_artifact_proxy_closes_upstream_when_consumer_cancels() -> None:
    stream = _BlockingStream()
    upstream = httpx.Response(200, stream=stream)
    client = httpx.AsyncClient()
    body = _bounded_artifact_body(
        upstream,
        client,
        max_bytes=1024,
        chunk_bytes=64,
    )

    assert await anext(body) == b"x" * 64
    pending = asyncio.create_task(anext(body))
    await asyncio.wait_for(stream.waiting.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_agent_artifact_proxy_rejects_declared_oversize_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([b"never-read"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1048577"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.api.agents.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    settings = Settings(
        agent_artifact_proxy_max_bytes=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )

    with pytest.raises(HTTPException) as error:
        await proxy_task_artifact(
            "task-1",
            "out/report.bin",
            service=_AgentServiceStub(),  # type: ignore[arg-type]
            settings=settings,
        )

    assert error.value.status_code == 413
    assert stream.closed is True
    assert client.is_closed is True
