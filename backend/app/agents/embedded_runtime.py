"""Backend side of the private Electron↔embedded-worker runtime port.

The port is deliberately transport-only.  It accepts one inherited duplex file
descriptor and never discovers a socket, executable, user config, or credential
from the host.  AgentTaskService remains the durable authority; this module only
submits commands and returns raw runtime frames to the service bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from app.agents.base import AgentIntent, AgentSubmitResult

RUNTIME_PROTOCOL_VERSION = 1
MAX_RUNTIME_FRAME_BYTES = 16 * 1024 * 1024
AGENTOS_SUBMIT_MAX_WALL_S = 50.0


class RuntimeFrame(TypedDict, total=False):
    protocolVersion: int
    frameId: str
    type: str
    taskId: str
    operationKey: str
    sentAt: str
    payload: dict[str, Any]


class RuntimeTransport(Protocol):
    async def send(self, frame: RuntimeFrame) -> None: ...

    async def receive(self) -> RuntimeFrame: ...

    async def close(self) -> None: ...


class EmbeddedRuntimeError(RuntimeError):
    """Stable failure for an unavailable or malformed embedded runtime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EmbeddedRuntimeError("RUNTIME_EOF", "embedded runtime closed the inherited handle")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise EmbeddedRuntimeError("RUNTIME_WRITE_FAILED", "embedded runtime handle rejected a frame")
        view = view[written:]


@dataclass(slots=True)
class InheritedDuplexTransport:
    """Length-prefixed JSON transport over one Electron-inherited duplex fd."""

    read_fd: int
    write_fd: int
    _write_lock: asyncio.Lock

    @classmethod
    def from_environment(cls) -> InheritedDuplexTransport:
        raw_fd = os.environ.get("ECHODESK_RUNTIME_FD")
        if raw_fd is None:
            raise EmbeddedRuntimeError(
                "EMBEDDED_RUNTIME_UNAVAILABLE",
                "ECHODESK_RUNTIME_FD is required; no external runtime fallback is allowed",
            )
        try:
            fd = int(raw_fd, 10)
        except ValueError as exc:
            raise EmbeddedRuntimeError("RUNTIME_HANDLE_INVALID", "ECHODESK_RUNTIME_FD is not an integer") from exc
        if fd < 0:
            raise EmbeddedRuntimeError("RUNTIME_HANDLE_INVALID", "ECHODESK_RUNTIME_FD must be non-negative")
        # A duplicated descriptor lets the read and write lifecycle close cleanly
        # without assuming whether Electron passed a pipe or a socketpair.
        try:
            write_fd = os.dup(fd)
        except OSError as exc:
            raise EmbeddedRuntimeError("RUNTIME_HANDLE_INVALID", "inherited runtime handle cannot be duplicated") from exc
        return cls(read_fd=fd, write_fd=write_fd, _write_lock=asyncio.Lock())

    async def send(self, frame: RuntimeFrame) -> None:
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RUNTIME_FRAME_BYTES:
            raise EmbeddedRuntimeError("RUNTIME_FRAME_TOO_LARGE", "runtime frame exceeds the 16 MiB limit")
        prefix = len(encoded).to_bytes(4, "big", signed=False)
        async with self._write_lock:
            await asyncio.to_thread(_write_all, self.write_fd, prefix + encoded)

    async def receive(self) -> RuntimeFrame:
        prefix = await asyncio.to_thread(_read_exact, self.read_fd, 4)
        size = int.from_bytes(prefix, "big", signed=False)
        if size <= 0 or size > MAX_RUNTIME_FRAME_BYTES:
            raise EmbeddedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame length is outside the contract")
        raw = await asyncio.to_thread(_read_exact, self.read_fd, size)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmbeddedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise EmbeddedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame must be a JSON object")
        if value.get("protocolVersion") != RUNTIME_PROTOCOL_VERSION:
            raise EmbeddedRuntimeError("RUNTIME_PROTOCOL_MISMATCH", "runtime protocol version is unsupported")
        for field in ("frameId", "type", "sentAt", "payload"):
            if field not in value:
                raise EmbeddedRuntimeError("RUNTIME_FRAME_INVALID", f"runtime frame misses {field}")
        if not isinstance(value["payload"], dict):
            raise EmbeddedRuntimeError("RUNTIME_FRAME_INVALID", "runtime frame payload must be an object")
        return value  # type: ignore[return-value]

    async def close(self) -> None:
        for fd in {self.read_fd, self.write_fd}:
            with suppress(OSError):
                os.close(fd)


def _frame(
    frame_type: str,
    *,
    task_id: str | None = None,
    operation_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RuntimeFrame:
    result: RuntimeFrame = {
        "protocolVersion": RUNTIME_PROTOCOL_VERSION,
        "frameId": f"runtime_{secrets.token_hex(16)}",
        "type": frame_type,
        "sentAt": _now(),
        "payload": payload or {},
    }
    if task_id is not None:
        result["taskId"] = task_id
    if operation_key is not None:
        result["operationKey"] = operation_key
    return result


def submit_operation_key(*, tenant_id: str, owner_id: str, task_id: str) -> str:
    material = f"v1\0{tenant_id}\0{owner_id}\0{task_id}\0submit".encode()
    return f"agent-submit-{hashlib.sha256(material).hexdigest()}"


def _deadline(timeout_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + timeout_s))


def _runtime_event_to_runner_event(frame: RuntimeFrame) -> dict[str, Any]:
    """Adapt KernelEventEnvelope to the existing Echo event adapter shape."""

    payload = frame.get("payload") or {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    event_type = str(event.get("type") or "")
    event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw: dict[str, Any] = {
        "task_id": frame.get("taskId"),
        "runtime_event_id": event.get("runtimeEventId") or frame.get("frameId"),
        "ts": event.get("occurredAt") or frame.get("sentAt") or _now(),
    }
    if event_type == "agent.turn.started":
        raw.update({"kind": "system", "payload": {"payload": {"subtype": "init"}}})
    elif event_type in {"agent.message.delta", "agent.message.completed"}:
        raw.update({"kind": "assistant_text", "payload": {"text": str(event_payload.get("text") or ""), "stream": event_type.endswith("delta")}})
    elif event_type == "agent.tool.requested":
        raw.update({"kind": "tool_use", "payload": {"tool_use_id": event_payload.get("toolUseId"), "name": event_payload.get("name")}})
    elif event_type in {"agent.tool.completed", "agent.tool.failed", "agent.tool.denied"}:
        raw.update({"kind": "tool_result", "payload": {"tool_use_id": event_payload.get("toolUseId"), "is_error": event_type != "agent.tool.completed"}})
    elif event_type in {"agent.turn.completed", "agent.turn.failed"}:
        raw.update({"kind": "result", "payload": {"result_text": str(event_payload.get("text") or event_payload.get("message") or ""), "is_error": event_type.endswith("failed")}})
    elif event_type == "agent.turn.cancelled":
        raw.update({"kind": "task_state", "payload": {"status": "cancelled"}})
    elif event_type == "agent.turn.timeout":
        raw.update({"kind": "task_state", "payload": {"status": "timeout"}})
    else:
        raw.update({"kind": "system", "payload": {"payload": {"type": event_type or "runtime_event"}}})
    return raw


class EmbeddedRuntimeBackend:
    """AgentTaskService adapter backed only by the embedded Electron runtime."""

    name = "embedded"
    base_url = ""

    def __init__(self, transport: RuntimeTransport | object | None = None) -> None:
        # AgentTaskService historically passed Settings to the runner
        # constructor.  Consume that private compatibility shape only to select
        # the inherited runtime handle; never derive a URL or executable from it.
        if transport is not None and hasattr(transport, "send") and hasattr(transport, "receive"):
            self._transport = transport  # type: ignore[assignment]
        else:
            try:
                self._transport = InheritedDuplexTransport.from_environment()
            except EmbeddedRuntimeError:
                self._transport = None
        self._ready = False
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[RuntimeFrame]] = {}
        self._events: dict[str, asyncio.Queue[RuntimeFrame | None]] = {}
        self._closed = False

    @classmethod
    def from_environment(cls) -> EmbeddedRuntimeBackend:
        try:
            transport: RuntimeTransport | None = InheritedDuplexTransport.from_environment()
        except EmbeddedRuntimeError:
            transport = None
        return cls(transport)

    @property
    def enabled(self) -> bool:
        return self._transport is not None and not self._closed

    @property
    def is_embedded(self) -> bool:
        return True

    async def _ensure_ready(self) -> None:
        if not self.enabled:
            raise EmbeddedRuntimeError("EMBEDDED_RUNTIME_UNAVAILABLE", "embedded runtime handle is unavailable")
        if self._ready:
            return
        assert self._transport is not None
        nonce = os.environ.get("ECHODESK_RUNTIME_NONCE")
        if not nonce:
            raise EmbeddedRuntimeError("RUNTIME_HANDSHAKE_FAILED", "ECHODESK_RUNTIME_NONCE is required")
        await self._transport.send(
            _frame(
                "runtime.hello",
                payload={
                    "nonceProof": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                    "buildId": "echodesk-backend",
                    "protocolVersion": RUNTIME_PROTOCOL_VERSION,
                },
            )
        )
        ready = await asyncio.wait_for(self._transport.receive(), timeout=10.0)
        if ready.get("type") != "runtime.ready":
            raise EmbeddedRuntimeError("RUNTIME_HANDSHAKE_FAILED", "embedded runtime did not become ready")
        self._ready = True
        self._reader_task = asyncio.create_task(self._read_frames(), name="embedded-runtime-reader")

    async def _read_frames(self) -> None:
        assert self._transport is not None
        try:
            while not self._closed:
                frame = await self._transport.receive()
                task_id = str(frame.get("taskId") or "")
                if frame.get("type") == "task.event" and task_id:
                    await self._events.setdefault(task_id, asyncio.Queue()).put(frame)
                    continue
                frame_id = str(frame.get("frameId") or "")
                future = self._pending.pop(frame_id, None)
                if future is not None and not future.done():
                    future.set_result(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, EmbeddedRuntimeError) else EmbeddedRuntimeError("RUNTIME_READ_FAILED", str(exc))
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            for queue in self._events.values():
                await queue.put(None)

    async def _request(
        self,
        frame_type: str,
        *,
        task_id: str,
        operation_key: str,
        payload: dict[str, Any],
        timeout_s: float = 30.0,
    ) -> RuntimeFrame:
        await self._ensure_ready()
        assert self._transport is not None
        frame = _frame(frame_type, task_id=task_id, operation_key=operation_key, payload=payload)
        frame_id = str(frame["frameId"])
        future: asyncio.Future[RuntimeFrame] = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = future
        try:
            await self._transport.send(frame)
            result = await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(frame_id, None)
        if result.get("type") == "runtime.degraded":
            raise EmbeddedRuntimeError("EMBEDDED_RUNTIME_DEGRADED", "embedded runtime rejected the command")
        return result

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        if not intent.echo_task_id:
            return AgentSubmitResult(task_id="", accepted=False, provider=self.name, error="embedded submit requires a stable Echo task id")
        if not intent.runner_operation_key:
            return AgentSubmitResult(task_id=intent.echo_task_id, accepted=False, provider=self.name, error="embedded submit requires an operation key")
        try:
            response = await self._request(
                "task.submit",
                task_id=intent.echo_task_id,
                operation_key=intent.runner_operation_key,
                payload={
                    "taskId": intent.echo_task_id,
                    "operationKey": intent.runner_operation_key,
                    "text": intent.text,
                    "title": intent.title or "EchoDesk 任务",
                    "context": intent.context,
                    "outputContract": intent.output_contract,
                    "conversationId": intent.conversation_id,
                    "messageId": intent.message_id,
                    "taskKind": intent.task_kind,
                    "grantId": intent.grant_id,
                    "permissionProfile": intent.permission_profile,
                    "deadlineAt": _deadline(intent.timeout_s),
                },
                timeout_s=min(max(intent.timeout_s, 5.0), AGENTOS_SUBMIT_MAX_WALL_S),
            )
        except EmbeddedRuntimeError as exc:
            return AgentSubmitResult(task_id=intent.echo_task_id, accepted=False, provider=self.name, error=f"{exc.code}: {exc}")
        accepted = response.get("type") == "task.accepted"
        return AgentSubmitResult(
            task_id=intent.echo_task_id,
            accepted=accepted,
            provider=self.name,
            runner_task_id=intent.echo_task_id if accepted else None,
            error=None if accepted else str((response.get("payload") or {}).get("error") or "embedded runtime rejected task"),
        )

    async def cancel(self, runner_task_id: str, *, operation_key: str) -> bool:
        try:
            response = await self._request(
                "task.cancel",
                task_id=runner_task_id,
                operation_key=operation_key,
                payload={"operationKey": operation_key},
                timeout_s=10.0,
            )
        except EmbeddedRuntimeError:
            return False
        return response.get("type") == "task.cancelled"

    async def get_task(self, runner_task_id: str) -> dict[str, object] | None:
        # Snapshot transport is intentionally only a typed embedded request; no
        # HTTP/WS reconciliation path exists in this adapter.
        try:
            response = await self._request(
                "task.snapshot.request",
                task_id=runner_task_id,
                operation_key=f"snapshot:{runner_task_id}",
                payload={},
            )
        except EmbeddedRuntimeError:
            return None
        payload = response.get("payload")
        return payload if isinstance(payload, dict) else None

    async def events(self, task_id: str, *, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        del after_seq
        await self._ensure_ready()
        queue = self._events.setdefault(task_id, asyncio.Queue())
        while True:
            frame = await queue.get()
            if frame is None:
                return
            raw = _runtime_event_to_runner_event(frame)
            yield raw
            if raw.get("kind") == "result" or (
                raw.get("kind") == "task_state"
                and str((raw.get("payload") or {}).get("status")) in {"cancelled", "timeout", "failed"}
            ):
                return

    async def aclose(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._transport is not None:
            await self._transport.close()
