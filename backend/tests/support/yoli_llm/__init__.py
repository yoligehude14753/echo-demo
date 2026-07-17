"""Deterministic CI contract for the separately shipped ``yoli_llm`` package.

The model-gateway tests inject their own transport and never make an external
request.  This module therefore exposes only the immutable public shapes that
those tests and the Echo adapter import during collection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import StreamCancelledError


@dataclass(frozen=True, slots=True)
class SSEFrame:
    data: Mapping[str, Any] | None = None
    done: bool = False


@dataclass(frozen=True, slots=True)
class StreamingRequest:
    endpoint: str
    protocol: str
    body: Mapping[str, Any]
    credential_handle: str
    timeout_s: float
    max_retries: int
    cancel_event: Any = None


async def stream_sse(
    request: StreamingRequest,
    resolver: Callable[[str], str | Awaitable[str]],
) -> AsyncIterator[SSEFrame]:
    del request, resolver
    if False:  # pragma: no cover - keeps this a typed async generator
        yield SSEFrame(done=True)
    raise RuntimeError("deterministic yoli_llm contract has no external transport")


__all__ = [
    "SSEFrame",
    "StreamCancelledError",
    "StreamingRequest",
    "stream_sse",
]
