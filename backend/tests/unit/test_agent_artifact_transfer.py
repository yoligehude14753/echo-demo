from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.agents.artifact_transfer import (
    ArtifactSizeLimitError,
    download_artifact_to_path,
)


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
        yield b"first"
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.AsyncBaseTransport,
) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=handler)
    monkeypatch.setattr(
        "app.agents.artifact_transfer.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    return client


@pytest.mark.unit
async def test_download_artifact_streams_and_atomically_replaces_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([b"abc", b"def"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "6", "content-type": "text/plain"},
            stream=stream,
        )

    client = _install_client(monkeypatch, httpx.MockTransport(handler))
    destination = tmp_path / "artifact.txt"
    destination.write_bytes(b"old")

    result = await download_artifact_to_path(
        "http://agentos.test/artifact?secret=must-not-leak",
        destination,
        max_bytes=6,
        chunk_bytes=2,
    )

    assert result.size_bytes == 6
    assert result.content_type == "text/plain"
    assert destination.read_bytes() == b"abcdef"
    assert list(tmp_path.glob("*.part")) == []
    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_download_artifact_rejects_declared_oversize_without_touching_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([b"never-read"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "7"}, stream=stream)

    client = _install_client(monkeypatch, httpx.MockTransport(handler))
    destination = tmp_path / "artifact.bin"
    destination.write_bytes(b"old")

    with pytest.raises(ArtifactSizeLimitError):
        await download_artifact_to_path(
            "http://agentos.test/artifact",
            destination,
            max_bytes=6,
            chunk_bytes=2,
        )

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.part")) == []
    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_download_artifact_removes_chunked_oversize_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([b"abcd", b"efgh"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = _install_client(monkeypatch, httpx.MockTransport(handler))
    destination = tmp_path / "artifact.bin"
    destination.write_bytes(b"old")

    with pytest.raises(ArtifactSizeLimitError):
        await download_artifact_to_path(
            "http://agentos.test/artifact",
            destination,
            max_bytes=7,
            chunk_bytes=4,
        )

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.part")) == []
    assert stream.closed is True
    assert client.is_closed is True


@pytest.mark.unit
async def test_download_artifact_cancellation_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _BlockingStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = _install_client(monkeypatch, httpx.MockTransport(handler))
    destination = tmp_path / "artifact.bin"
    destination.write_bytes(b"old")
    download = asyncio.create_task(
        download_artifact_to_path(
            "http://agentos.test/artifact",
            destination,
            max_bytes=1024,
            chunk_bytes=64,
        )
    )

    await asyncio.wait_for(stream.waiting.wait(), timeout=1.0)
    download.cancel()
    with pytest.raises(asyncio.CancelledError):
        await download

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.part")) == []
    assert stream.closed is True
    assert client.is_closed is True
