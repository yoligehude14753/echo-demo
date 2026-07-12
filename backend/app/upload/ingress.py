"""ASGI-level upload guard that runs before multipart parsing."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.security.paths import route_scope_path

_UPLOAD_PATHS = (
    re.compile(r"^/capture/chunk$"),
    re.compile(r"^/meetings/[^/]+/chunk$"),
    re.compile(r"^/meetings/[^/]+/inject_segment$"),
    re.compile(r"^/rag/ingest$"),
)
_MEETING_INJECT_PATH = re.compile(r"^/meetings/[^/]+/inject_segment$")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class UploadIngressCapacityExceeded(RuntimeError):
    """The process-wide upload concurrency or in-flight byte cap is full."""


class RequestBodyTooLarge(RuntimeError):
    """The raw HTTP body exceeded the route's pre-parser ceiling."""


class RequestBodyTimeout(RuntimeError):
    """The complete upload body did not arrive before the ingress deadline."""


class _IngressLease:
    def __init__(self, limiter: UploadIngressLimiter, reserved: int) -> None:
        self._limiter = limiter
        self._reserved = reserved
        self._released = False

    async def ensure_bytes(self, observed: int) -> None:
        if observed <= self._reserved:
            return
        await self._limiter._grow(self, observed - self._reserved)
        self._reserved = observed

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._limiter._release(self._reserved)


class UploadIngressLimiter:
    """Process-wide in-flight upload count and byte reservations."""

    def __init__(self, *, max_requests: int, max_bytes: int) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self._requests = 0
        self._bytes = 0
        self._lock = asyncio.Lock()

    async def acquire(self, declared_bytes: int) -> _IngressLease:
        declared = max(0, declared_bytes)
        async with self._lock:
            if self._requests >= self.max_requests or self._bytes + declared > self.max_bytes:
                raise UploadIngressCapacityExceeded
            self._requests += 1
            self._bytes += declared
        return _IngressLease(self, declared)

    async def _grow(self, lease: _IngressLease, delta: int) -> None:
        del lease
        async with self._lock:
            if self._bytes + delta > self.max_bytes:
                raise UploadIngressCapacityExceeded
            self._bytes += delta

    async def _release(self, reserved: int) -> None:
        async with self._lock:
            self._requests = max(0, self._requests - 1)
            self._bytes = max(0, self._bytes - reserved)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _content_length(scope: Mapping[str, object]) -> int | None:
    raw = _header(scope.get("headers", []), b"content-length")  # type: ignore[arg-type]
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("invalid content-length") from exc
    if value < 0:
        raise ValueError("invalid content-length")
    return value


def upload_body_limit(settings: Settings, path: str) -> int | None:
    if not any(pattern.fullmatch(path) for pattern in _UPLOAD_PATHS):
        return None
    if _MEETING_INJECT_PATH.fullmatch(path):
        # JSON field names, timestamps and optional speaker metadata have a
        # small fixed envelope around the separately checked text payload.
        return settings.meeting_inject_segment_max_bytes + 8 * 1024
    file_mb = (
        max(settings.upload_max_file_mb, settings.workspace_max_file_mb)
        if path == "/rag/ingest" and not settings.public_demo_mode
        else settings.upload_max_file_mb
    )
    return int(file_mb * 1024 * 1024) + settings.upload_multipart_overhead_bytes


def request_body_limit(settings: Settings, path: str, method: str) -> int | None:
    """Return the pre-parser ceiling for every method that may carry a body."""

    if method.upper() not in _BODY_METHODS:
        return None
    return upload_body_limit(settings, path) or settings.request_body_max_bytes


def _request_body_timeout(settings: Settings, path: str) -> float:
    if upload_body_limit(settings, path) is not None:
        return settings.upload_body_timeout_s
    return settings.request_body_timeout_s


def _body_failure_response(error: RuntimeError) -> JSONResponse:
    if isinstance(error, RequestBodyTooLarge):
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    if isinstance(error, RequestBodyTimeout):
        return JSONResponse({"detail": "request body timeout"}, status_code=408)
    return JSONResponse(
        {"detail": "request body capacity temporarily full"},
        status_code=503,
        headers={"Retry-After": "1"},
    )


class _BodyGuard:
    """Own one request body's accounting, failure mapping and capacity lease."""

    def __init__(
        self,
        *,
        scope: Scope,
        receive: Receive,
        send: Send,
        lease: _IngressLease,
        limit: int,
        timeout_s: float,
    ) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self._lease = lease
        self._limit = limit
        self._deadline = asyncio.get_running_loop().time() + timeout_s
        self._observed = 0
        self._failure: RuntimeError | None = None
        self._response_started = False

    async def receive(self) -> Message:
        remaining_s = self._deadline - asyncio.get_running_loop().time()
        if remaining_s <= 0:
            self._failure = RequestBodyTimeout()
            raise self._failure
        try:
            message = await asyncio.wait_for(self._receive(), timeout=remaining_s)
        except TimeoutError:
            self._failure = RequestBodyTimeout()
            raise self._failure from None
        if message["type"] != "http.request":
            return message
        self._observed += len(message.get("body", b""))
        self.scope.setdefault("state", {})["upload_body_bytes"] = self._observed
        if self._observed > self._limit:
            self._failure = RequestBodyTooLarge()
            raise self._failure
        try:
            await self._lease.ensure_bytes(self._observed)
        except UploadIngressCapacityExceeded as exc:
            self._failure = exc
            raise
        return message

    async def send(self, message: Message) -> None:
        # FastAPI 会把 receive 侧解析异常改写成通用 400。守卫已经完成错误分类后，
        # 丢弃该内部响应，统一由最外层 ASGI 边界返回稳定的 413/408/503。
        if self._failure is not None:
            return
        if message["type"] == "http.response.start":
            self._response_started = True
        await self._send(message)

    async def run(self, app: ASGIApp) -> None:
        try:
            try:
                await app(self.scope, self.receive, self.send)
            except (
                RequestBodyTooLarge,
                RequestBodyTimeout,
                UploadIngressCapacityExceeded,
            ) as exc:
                self._failure = exc
            if self._failure is not None:
                if self._response_started:
                    raise self._failure
                await _body_failure_response(self._failure)(
                    self.scope,
                    self._receive,
                    self._send,
                )
        finally:
            await self._lease.release()


class UploadIngressMiddleware:
    """Reject and reserve request bodies before Starlette parses them."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.limiter = UploadIngressLimiter(
            max_requests=settings.upload_global_concurrent_requests,
            max_bytes=settings.upload_global_inflight_bytes,
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = route_scope_path(scope)
        limit = request_body_limit(self.settings, path, str(scope.get("method", "")))
        if limit is None:
            await self.app(scope, receive, send)
            return
        try:
            declared = _content_length(scope)
        except ValueError:
            await JSONResponse({"detail": "invalid content-length"}, status_code=400)(
                scope, receive, send
            )
            return
        if declared is not None and declared > limit:
            await _body_failure_response(RequestBodyTooLarge())(scope, receive, send)
            return
        try:
            lease = await self.limiter.acquire(declared or 0)
        except UploadIngressCapacityExceeded:
            await _body_failure_response(UploadIngressCapacityExceeded())(scope, receive, send)
            return

        guard = _BodyGuard(
            scope=scope,
            receive=receive,
            send=send,
            lease=lease,
            limit=limit,
            timeout_s=_request_body_timeout(self.settings, path),
        )
        await guard.run(self.app)


__all__ = [
    "RequestBodyTimeout",
    "RequestBodyTooLarge",
    "UploadIngressCapacityExceeded",
    "UploadIngressLimiter",
    "UploadIngressMiddleware",
    "request_body_limit",
    "upload_body_limit",
]
