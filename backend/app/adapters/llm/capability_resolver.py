"""Resolve one strict binding declaration into EchoDesk's effective capability."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from yoli_llm.model_gateway import (
    GatewayOutputPolicy,
    GatewayRequestPolicy,
    GatewayRetryPolicy,
    ModelGatewayClient,
    ModelGatewayError,
)

_REQUIREMENTS_PATH = Path(__file__).resolve().parents[2] / "model_gateway_requirements.json"
_CANONICAL_FIELDS = {
    "chat": frozenset({"model", "messages", "max_tokens", "stream"}),
    "transcription": frozenset({"model", "file", "stream"}),
    "speech": frozenset({"model", "input", "voice", "response_format"}),
}


def _copy(value: object) -> Any:
    return json.loads(json.dumps(value))


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelGatewayError("capability contract unavailable")
    return _copy(value)


def _string_list(value: object, *, nonempty: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ModelGatewayError("capability contract unavailable")
    return tuple(value)


def _load_requirements(capability: str) -> dict[str, Any]:
    try:
        payload = json.loads(_REQUIREMENTS_PATH.read_text(encoding="utf-8"))
        if payload.get("requirements_schema_version") != 1:
            raise ValueError
        requirement = payload["requirements"][capability]
        protocol = requirement["protocol"]
        required_fields = _string_list(requirement["required_fields"], nonempty=True)
        output = _mapping(requirement["output"])
        stream_output = requirement.get("stream_output")
        if stream_output is not None:
            stream_output = _mapping(stream_output)
        overrides = _mapping(requirement.get("request_overrides", {}))
        constraints = _mapping(requirement.get("constraints", {}))
        if not isinstance(protocol, str) or not protocol:
            raise ValueError
        for value in (output, stream_output):
            if value is not None and (
                value.get("boundary") not in {"json", "sse", "binary", "multipart"}
                or not isinstance(value.get("shape"), str)
                or not value["shape"]
            ):
                raise ValueError
        return {
            "protocol": protocol,
            "required_fields": required_fields,
            "output": output,
            "stream_output": stream_output,
            "request_overrides": overrides,
            "constraints": constraints,
        }
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise ModelGatewayError("capability contract unavailable") from None


def _output_policy(raw: object) -> GatewayOutputPolicy:
    value = _mapping(raw)
    statuses = value.get("status")
    content_type = value.get("content_type")
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(isinstance(item, bool) or not isinstance(item, int) for item in statuses)
        or not isinstance(content_type, str)
        or not content_type
        or value.get("boundary") not in {"json", "sse", "binary", "multipart"}
        or not isinstance(value.get("shape"), str)
        or not value["shape"]
    ):
        raise ModelGatewayError("capability contract unavailable")
    return GatewayOutputPolicy(
        status=frozenset(statuses),
        content_type=content_type,
        boundary=value["boundary"],
        shape=value["shape"],
    )


def _retry_policy(raw: object) -> GatewayRetryPolicy:
    value = _mapping(raw)
    max_retries = value.get("max_retries")
    statuses = value.get("statuses")
    idempotency = value.get("idempotency")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 3
        or not isinstance(statuses, list)
        or any(status not in {429, 502, 503, 504} for status in statuses)
        or idempotency not in {"none", "owner_declared"}
        or (max_retries and idempotency == "none")
    ):
        raise ModelGatewayError("capability contract unavailable")
    return GatewayRetryPolicy(
        max_retries=max_retries,
        statuses=frozenset(statuses),
        idempotency="idempotent" if idempotency == "owner_declared" else "none",
        idempotency_key=value.get("idempotency_key"),
    )


def _matching_output(actual: GatewayOutputPolicy, expected: Mapping[str, Any]) -> bool:
    return actual.boundary == expected["boundary"] and actual.shape == expected["shape"]


@dataclass(frozen=True, slots=True)
class EffectiveCapability:
    """The only capability object callers may use to construct gateway requests."""

    name: str
    model: str
    limits: dict[str, Any]
    defaults: dict[str, Any]
    request_overrides: dict[str, Any]
    policy: GatewayRequestPolicy

    @property
    def endpoint(self) -> str:
        return self.policy.endpoint

    @property
    def context_window_tokens(self) -> int:
        return int(self.limits.get("context_window_tokens", 0))

    @property
    def max_output_tokens(self) -> int:
        return int(self.limits.get("max_output_tokens", 0))

    def options(self, supplied: Mapping[str, Any] | None = None) -> dict[str, Any]:
        supplied_values = dict(supplied or {})
        if any(key in _CANONICAL_FIELDS[self.name] for key in supplied_values):
            raise ModelGatewayError("capability contract conflict")
        values = {**self.defaults, **self.request_overrides, **supplied_values}
        if any(key not in self.policy.supported_fields for key in values):
            raise ModelGatewayError("request error")
        return _copy({
            key: value
            for key, value in values.items()
            if key not in _CANONICAL_FIELDS[self.name]
        })


def _effective(entry: Mapping[str, Any], capability: str) -> EffectiveCapability:
    requirement = _load_requirements(capability)
    if entry.get("capability") != capability:
        raise ModelGatewayError("capability contract conflict")
    model = entry.get("id")
    request = _mapping(entry.get("request"))
    supported = frozenset(_string_list(request.get("supported_fields"), nonempty=True))
    required = frozenset(_string_list(request.get("required_fields"), nonempty=True))
    expected_required = frozenset(requirement["required_fields"])
    if (
        not isinstance(model, str)
        or not model.strip()
        or model != model.strip()
        or request.get("method") != "POST"
        or not isinstance(request.get("endpoint"), str)
        or not request["endpoint"].startswith("/")
        or request.get("protocol") != requirement["protocol"]
        or not required.issubset(supported)
        or not expected_required.issubset(supported)
        or any(key not in supported for key in requirement["request_overrides"])
    ):
        raise ModelGatewayError("capability contract unavailable")
    limits = _mapping(entry.get("limits"))
    defaults = _mapping(entry.get("defaults"))
    if any(key not in supported for key in defaults):
        raise ModelGatewayError("capability contract unavailable")
    output = _output_policy(entry.get("output"))
    if not _matching_output(output, requirement["output"]):
        raise ModelGatewayError("capability contract unavailable")
    stream_raw = entry.get("stream_output")
    stream_output = _output_policy(stream_raw) if stream_raw is not None else None
    expected_stream = requirement["stream_output"]
    if expected_stream is not None and (
        stream_output is None or not _matching_output(stream_output, expected_stream)
    ):
        raise ModelGatewayError("capability contract unavailable")
    if capability == "chat" and (
        not isinstance(limits.get("context_window_tokens"), int)
        or isinstance(limits["context_window_tokens"], bool)
        or limits["context_window_tokens"] < 1
        or not isinstance(limits.get("max_output_tokens"), int)
        or isinstance(limits["max_output_tokens"], bool)
        or not 1 <= limits["max_output_tokens"] <= limits["context_window_tokens"]
    ):
        raise ModelGatewayError("capability contract unavailable")
    merged_limits = {**limits, **requirement["constraints"]}
    policy = GatewayRequestPolicy(
        model=model,
        method="POST",
        endpoint=request["endpoint"],
        protocol=request["protocol"],
        supported_fields=supported,
        defaults={},
        limits=merged_limits,
        output=output,
        stream_output=stream_output,
        retry=_retry_policy(entry.get("retry")),
    )
    return EffectiveCapability(
        name=capability,
        model=model,
        limits=merged_limits,
        defaults=defaults,
        request_overrides=requirement["request_overrides"],
        policy=policy,
    )


async def resolve_gateway_capability(
    gateway: ModelGatewayClient,
    capability_name: str,
    *,
    requested_model: str | None,
    timeout_s: float | None = None,
) -> EffectiveCapability:
    model = (requested_model or "").strip()
    if not model:
        raise ModelGatewayError("capability contract conflict")
    catalog = await gateway.discover_model_profiles(timeout_s=timeout_s)
    if not isinstance(catalog, (tuple, list)) or any(not isinstance(item, Mapping) for item in catalog):
        raise ModelGatewayError("capability contract conflict")
    selected = next((item for item in catalog if item.get("id") == model), None)
    if selected is None:
        raise ModelGatewayError("capability contract conflict")
    return _effective(selected, capability_name)


async def resolve_chat_capability(
    gateway: ModelGatewayClient,
    *,
    requested_model: str | None,
    timeout_s: float | None = None,
) -> EffectiveCapability:
    return await resolve_gateway_capability(
        gateway,
        "chat",
        requested_model=requested_model,
        timeout_s=timeout_s,
    )


__all__ = ["EffectiveCapability", "resolve_chat_capability", "resolve_gateway_capability"]
