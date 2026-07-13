"""Bounded streaming primitives for AgentOS artifact transfers."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx


class ArtifactSizeLimitError(RuntimeError):
    """Raised when an artifact exceeds the configured transfer limit."""


class ArtifactContentLengthError(ValueError):
    """Raised when an upstream Content-Length header is malformed."""


@dataclass(frozen=True, slots=True)
class ArtifactDownloadResult:
    size_bytes: int
    content_type: str | None


def validated_content_length(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> int | None:
    """Validate Content-Length and reject declared oversize bodies preflight."""

    raw_content_length = response.headers.get("content-length")
    if raw_content_length is None:
        return None
    try:
        content_length = int(raw_content_length)
    except ValueError as exc:
        raise ArtifactContentLengthError("artifact upstream returned invalid size") from exc
    if content_length < 0:
        raise ArtifactContentLengthError("artifact upstream returned invalid size")
    if content_length > max_bytes:
        raise ArtifactSizeLimitError("agent artifact exceeded proxy size limit")
    return content_length


async def close_artifact_stream(
    upstream: httpx.Response | None,
    client: httpx.AsyncClient,
) -> None:
    """Close both halves of a streamed HTTP transfer."""

    try:
        if upstream is not None:
            await upstream.aclose()
    finally:
        await client.aclose()


async def bounded_artifact_body(
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    *,
    max_bytes: int,
    chunk_bytes: int,
) -> AsyncIterator[bytes]:
    """Yield a response body while enforcing a limit even without Content-Length."""

    transferred = 0
    try:
        async for chunk in upstream.aiter_bytes(chunk_bytes):
            transferred += len(chunk)
            if transferred > max_bytes:
                raise ArtifactSizeLimitError("agent artifact exceeded proxy size limit")
            yield chunk
    finally:
        await close_artifact_stream(upstream, client)


async def download_artifact_to_path(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    chunk_bytes: int,
    timeout_s: float = 30.0,
) -> ArtifactDownloadResult:
    """Stream an artifact to a same-directory temp file and atomically install it.

    The temporary file is removed on cancellation, HTTP errors, malformed sizes,
    oversize chunked bodies, and local I/O failures. An existing destination is
    only replaced after the complete body has been flushed and fsynced.
    """

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(timeout=timeout_s, trust_env=False)
    upstream: httpx.Response | None = None
    temp_path: Path | None = None
    try:
        request = client.build_request("GET", url)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
        validated_content_length(upstream, max_bytes=max_bytes)

        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
        )
        temp_path = Path(raw_temp_path)
        transferred = 0
        with os.fdopen(fd, "wb") as handle:
            async for chunk in upstream.aiter_bytes(chunk_bytes):
                transferred += len(chunk)
                if transferred > max_bytes:
                    raise ArtifactSizeLimitError("agent artifact exceeded proxy size limit")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, destination)
        temp_path = None
        return ArtifactDownloadResult(
            size_bytes=transferred,
            content_type=upstream.headers.get("content-type"),
        )
    finally:
        try:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        finally:
            await close_artifact_stream(upstream, client)
