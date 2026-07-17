"""Production Echo model port backed by the public yoli_llm SSE transport.

This adapter owns request assembly and event projection only.  Runtime config
and credentials remain upstream authorities: the gateway receives an immutable
snapshot, a route endpoint binding, and an injected credential resolver.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from yoli_llm import (
    SSEFrame,
    StreamCancelledError,
    StreamingRequest,
    stream_sse,
)
from yoli_llm.errors import (
    AuthError,
    BadRequestError,
    ProviderError,
    RateLimitError,
    TimeoutError_,
    YoliExternalError,
)

from app.model_runtime.protocols import (
    MODEL_SCHEMA_VERSION,
    ProtocolAdapterError,
    RequestIdentity,
    build_anthropic_request,
    build_openai_compatible_request,
    normalize_anthropic_stream,
    normalize_openai_compatible_stream,
)
from app.model_runtime.snapshot import ModelRuntimeSnapshot, validate_request_identity

CredentialResolver = Callable[[str], str | Awaitable[str]]
Transport = Callable[[StreamingRequest, CredentialResolver], AsyncIterable[SSEFrame]]


@dataclass(frozen=True, slots=True)
class AgentModelRequest:
    """Frozen v1 request shape accepted from the kernel model port."""

    request_id: str
    task_id: str
    operation_key: str
    purpose: str
    config_revision: int
    route_id: str
    model: str
    system: str | Sequence[Mapping[str, Any] | str] | None
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]] = ()
    tool_choice: Any = None
    max_output_tokens: int = 256
    temperature: float | None = None
    stop_sequences: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class AgentModelEvent:
    """Common event envelope projected from the B02 normalized event."""

    type: str
    request_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def schema_version(self) -> int:
        return MODEL_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "requestId": self.request_id,
            "type": self.type,
            **dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class TokenCountResult:
    tokens: int
    estimated: bool
    tokenizer: str


class EchoModelPort(Protocol):
    def stream(self, request: AgentModelRequest, signal: asyncio.Event | None = None) -> AsyncIterator[AgentModelEvent]: ...

    async def count_tokens(self, request: AgentModelRequest) -> TokenCountResult: ...

    def snapshot(self) -> ModelRuntimeSnapshot: ...


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field_name} is required")


def _identity(request: AgentModelRequest, snapshot: ModelRuntimeSnapshot) -> RequestIdentity:
    for value, field_name in (
        (request.request_id, "requestId"),
        (request.task_id, "taskId"),
        (request.operation_key, "operationKey"),
        (request.route_id, "routeId"),
        (request.model, "model"),
    ):
        _non_empty(value, field_name)
    if request.purpose != snapshot.purpose or request.model != snapshot.model:
        raise ProtocolAdapterError("MODEL_REQUEST_IDENTITY_MISMATCH", "request model identity does not match snapshot")
    identity = snapshot.identity(
        request_id=request.request_id,
        task_id=request.task_id,
        operation_key=request.operation_key,
    )
    if request.config_revision != snapshot.revision or request.route_id != snapshot.route_id:
        raise ProtocolAdapterError("MODEL_REQUEST_IDENTITY_MISMATCH", "request revision or route does not match snapshot")
    return validate_request_identity(identity, snapshot)


def _error_event(request_id: str, code: str, *, retryable: bool, message: str) -> AgentModelEvent:
    return AgentModelEvent(
        type="error",
        request_id=request_id,
        payload={"code": code, "retryable": retryable, "message": message[:512]},
    )


def _external_error(error: YoliExternalError) -> tuple[str, bool, str]:
    if isinstance(error, StreamCancelledError):
        return "MODEL_CANCELLED", False, "model stream cancelled"
    if isinstance(error, AuthError):
        code = "MODEL_CREDENTIAL_REVOKED" if error.status == 403 else "MODEL_CREDENTIAL_MISSING"
        return code, False, "model credential was rejected"
    if isinstance(error, TimeoutError_):
        return "MODEL_TIMEOUT", error.retryable, "model provider timed out"
    if isinstance(error, (RateLimitError, ProviderError)):
        return "MODEL_UPSTREAM_ERROR", error.retryable, "model provider returned an error"
    if isinstance(error, BadRequestError):
        return "MODEL_UPSTREAM_ERROR", False, "model provider rejected the request"
    return "MODEL_UPSTREAM_ERROR", error.retryable, "model provider request failed"


def _project(request_id: str, normalized: Any) -> AgentModelEvent:
    return AgentModelEvent(
        type=normalized.type,
        request_id=request_id,
        payload=dict(normalized.payload),
    )


def _openai_finish_seen(frame: SSEFrame) -> bool:
    if frame.done or not isinstance(frame.data, Mapping):
        return False
    choices = frame.data.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, Mapping) and choice.get("finish_reason") is not None
        for choice in choices
    )


class AgentModelGateway:
    """EchoModelPort adapter for Anthropic Messages and OpenAI Chat SSE."""

    def __init__(
        self,
        snapshot: ModelRuntimeSnapshot,
        *,
        endpoint: str,
        credential_resolver: CredentialResolver,
        transport: Transport = stream_sse,
    ) -> None:
        self._snapshot = snapshot
        self._endpoint = endpoint
        self._credential_resolver = credential_resolver
        self._transport = transport

    def snapshot(self) -> ModelRuntimeSnapshot:
        return self._snapshot

    async def count_tokens(self, request: AgentModelRequest) -> TokenCountResult:
        _identity(request, self._snapshot)
        chars = len(request.system or "")
        chars += sum(len(str(message.get("content", ""))) for message in request.messages)
        chars += sum(len(str(tool)) for tool in request.tools)
        estimate = max(1, (chars + 3) // 4) + self._snapshot.tokenizer.safety_margin_tokens
        return TokenCountResult(
            tokens=estimate,
            estimated=True,
            tokenizer=self._snapshot.tokenizer.identifier,
        )

    def stream(
        self,
        request: AgentModelRequest,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[AgentModelEvent]:
        return self._stream(request, signal)

    async def _stream(
        self,
        request: AgentModelRequest,
        signal: asyncio.Event | None,
    ) -> AsyncIterator[AgentModelEvent]:
        try:
            identity = _identity(request, self._snapshot)
            if self._snapshot.protocol == "anthropic_messages":
                provider_request = build_anthropic_request(
                    identity,
                    model=request.model,
                    system=request.system,
                    messages=request.messages,
                    tools=request.tools,
                    max_output_tokens=request.max_output_tokens,
                    tool_choice=request.tool_choice,
                    temperature=request.temperature,
                    stop_sequences=request.stop_sequences,
                )
                normalizer = normalize_anthropic_stream
                protocol = "anthropic_messages"
            else:
                provider_request = build_openai_compatible_request(
                    identity,
                    model=request.model,
                    system=request.system,
                    messages=request.messages,
                    tools=request.tools,
                    max_output_tokens=request.max_output_tokens,
                    tool_choice=request.tool_choice,
                    temperature=request.temperature,
                    stop_sequences=request.stop_sequences,
                )
                normalizer = normalize_openai_compatible_stream
                protocol = "openai_chat"
            transport_request = StreamingRequest(
                endpoint=self._endpoint,
                protocol=protocol,
                body=provider_request.body,
                credential_handle=self._snapshot.credential_handle,
                timeout_s=self._snapshot.limits.request_timeout_s,
                max_retries=self._snapshot.limits.max_retries,
                cancel_event=signal,
            )
            raw_frames: list[Mapping[str, Any] | str] = []
            emitted = 0
            terminal_pending = False
            async for frame in self._transport(transport_request, self._credential_resolver):
                raw_frames.append("[DONE]" if frame.done else dict(frame.data or {}))
                if protocol == "openai_chat" and _openai_finish_seen(frame):
                    terminal_pending = True
                if terminal_pending and not frame.done:
                    continue
                normalized = normalizer(raw_frames, identity)
                if normalized and normalized[-1].type == "error":
                    error = normalized[-1]
                    if error.payload.get("code") == "MODEL_STREAM_INCOMPLETE":
                        normalized = normalized[:-1]
                    else:
                        for item in normalized[emitted:]:
                            yield _project(request.request_id, item)
                        return
                for item in normalized[emitted:]:
                    yield _project(request.request_id, item)
                emitted = len(normalized)
            if terminal_pending and raw_frames and raw_frames[-1] != "[DONE]":
                normalized = normalizer(raw_frames, identity)
                if normalized and normalized[-1].type == "error":
                    error = normalized[-1]
                    for item in normalized[emitted:]:
                        yield _project(request.request_id, item)
                    if error.payload.get("code") != "MODEL_STREAM_INCOMPLETE":
                        return
                else:
                    for item in normalized[emitted:]:
                        yield _project(request.request_id, item)
        except ProtocolAdapterError as error:
            yield _error_event(
                request.request_id,
                error.code,
                retryable=error.retryable,
                message=error.message,
            )
        except YoliExternalError as error:
            code, retryable, message = _external_error(error)
            yield _error_event(request.request_id, code, retryable=retryable, message=message)


__all__ = [
    "AgentModelEvent",
    "AgentModelGateway",
    "AgentModelRequest",
    "EchoModelPort",
    "TokenCountResult",
]
