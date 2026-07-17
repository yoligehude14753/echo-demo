"""Model protocol boundary types.

This module deliberately contains no provider SDK, HTTP client, credential, or
tool-host dependency.  Provider adapters use these immutable values as the
only identity and event boundary.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from app.model_runtime.types import RequestIdentity as RuntimeRequestIdentity

MODEL_SCHEMA_VERSION = 1

# Compatibility alias only: model_runtime.types.RequestIdentity is the sole
# identity schema and remains the handoff type for compiler/snapshot callers.
RequestIdentity = RuntimeRequestIdentity


class ProtocolAdapterError(ValueError):
    """A deterministic, provider-neutral protocol rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        # Provider payloads are intentionally not retained.  The message is
        # already sanitized by the adapter before this exception is created.
        self.message = message
        super().__init__(f"{code}: {message}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolAdapterError("MODEL_SCHEMA_VERSION_MISMATCH", f"{field} must be non-empty")
    return value


def _identity_dict(identity: RequestIdentity) -> dict[str, Any]:
    return identity.model_dump(by_alias=True)


def _identity_matches(identity: RequestIdentity, fields: Mapping[str, Any]) -> bool:
    expected = _identity_dict(identity)
    return all(fields.get(key) == value for key, value in expected.items())


def _validated_identity(fields: Mapping[str, Any]) -> RequestIdentity:
    try:
        return RuntimeRequestIdentity.model_validate(fields)
    except Exception as exc:  # Pydantic error text may contain caller input.
        raise ProtocolAdapterError(
            "MODEL_SCHEMA_VERSION_MISMATCH", "request identity is incomplete or invalid"
        ) from exc


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    """Provider-neutral message with ordered Claude-shaped content blocks."""

    role: str
    content: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CanonicalToolDefinition:
    name: str
    description: str | None
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelToolRequest:
    """A complete typed tool request; it is never invoked by this package."""

    identity: RequestIdentity
    tool_use_id: str
    name: str
    input: Mapping[str, Any]
    index: int

    @property
    def toolUseId(self) -> str:
        return self.tool_use_id

    def as_dict(self) -> dict[str, Any]:
        identity = _identity_dict(self.identity)
        return {
            "schemaVersion": MODEL_SCHEMA_VERSION,
            **identity,
            "toolUseId": self.tool_use_id,
            "name": self.name,
            "input": _thaw(self.input),
            "index": self.index,
        }


@dataclass(frozen=True, slots=True)
class ModelRequestEnvelope:
    """Explicit request envelope; all five identity fields are serialized."""

    identity: RequestIdentity | None = None
    schema_version: Literal[1] | None = None
    task_id: str = ""
    operation_key: str = ""
    request_id: str = ""
    config_revision: int = 0
    route_id: str = ""
    body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fields = {
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "requestId": self.request_id,
            "configRevision": self.config_revision,
            "routeId": self.route_id,
        }
        if (
            self.identity is None
            or self.schema_version != MODEL_SCHEMA_VERSION
            or not all(fields.values())
            or not isinstance(self.config_revision, int)
        ):
            raise ProtocolAdapterError(
                "MODEL_SCHEMA_VERSION_MISMATCH", "request envelope is incomplete"
            )
        if not _identity_matches(self.identity, fields):
            raise ProtocolAdapterError(
                "MODEL_REQUEST_IDENTITY_MISMATCH", "request envelope identity mismatch"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "requestId": self.request_id,
            "configRevision": self.config_revision,
            "routeId": self.route_id,
            "body": _thaw(self.body),
        }


@dataclass(frozen=True, slots=True)
class ModelEventEnvelope:
    """Explicit event envelope with no inferred identity fields."""

    identity: RequestIdentity | None = None
    schema_version: Literal[1] | None = None
    task_id: str = ""
    operation_key: str = ""
    request_id: str = ""
    config_revision: int = 0
    route_id: str = ""
    type: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fields = {
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "requestId": self.request_id,
            "configRevision": self.config_revision,
            "routeId": self.route_id,
        }
        if (
            self.identity is None
            or self.schema_version != MODEL_SCHEMA_VERSION
            or not all(fields.values())
            or not self.type
            or not isinstance(self.config_revision, int)
        ):
            raise ProtocolAdapterError(
                "MODEL_SCHEMA_VERSION_MISMATCH", "event envelope is incomplete"
            )
        if not _identity_matches(self.identity, fields):
            raise ProtocolAdapterError(
                "MODEL_REQUEST_IDENTITY_MISMATCH", "event envelope identity mismatch"
            )

    @property
    def request_id_value(self) -> str:
        return self.request_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "requestId": self.request_id,
            "configRevision": self.config_revision,
            "routeId": self.route_id,
            "type": self.type,
            **_thaw(self.payload),
        }


# Existing adapter callers use NormalizedEvent; the envelope is the one
# implementation, not a second event contract.
NormalizedEvent = ModelEventEnvelope


def event(
    identity: RequestIdentity, event_type: str, payload: Mapping[str, Any] | None = None
) -> ModelEventEnvelope:
    identity_fields = _identity_dict(identity)
    return ModelEventEnvelope(
        identity=identity,
        schema_version=MODEL_SCHEMA_VERSION,
        task_id=identity_fields["taskId"],
        operation_key=identity_fields["operationKey"],
        request_id=identity_fields["requestId"],
        config_revision=identity_fields["configRevision"],
        route_id=identity_fields["routeId"],
        type=event_type,
        payload=MappingProxyType(_freeze(dict(payload or {}))),
    )


def freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a recursively immutable mapping for adapter-owned typed data."""

    return MappingProxyType(_freeze(dict(value)))


def json_object(value: str, *, field: str = "tool input") -> Mapping[str, Any]:
    """Decode a complete JSON object and reject truncated/non-object values."""

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolAdapterError(
            "MODEL_TOOL_ARGUMENTS_INVALID", f"{field} is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProtocolAdapterError("MODEL_TOOL_ARGUMENTS_INVALID", f"{field} must be a JSON object")
    return freeze_mapping(parsed)


_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(?:api[_ -]?key|token|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
)


def sanitize_provider_message(value: Any) -> str:
    """Keep provider diagnostics short and prevent accidental credential echo."""

    message = str(value) if value is not None else "provider error"
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[redacted]", message)
    return message[:512]
