"""Provider-neutral health and capability probes for model routes."""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from app.model_runtime.credentials import (
    CredentialHandleError,
    CredentialResolver,
    ResolvedCredential,
    validate_credential_handle,
)
from app.model_runtime.errors import MODEL_CAPABILITY_UNSUPPORTED, MODEL_HEALTH_PROBE_FAILED
from app.model_runtime.types import ModelCapabilities, ModelRoute

HealthStatus = Literal["healthy", "degraded", "unhealthy", "unavailable"]
CapabilityProbeStatus = Literal["supported", "partial", "unsupported", "unavailable"]
CapabilityProbeTransport = Callable[
    [ModelRoute, ResolvedCredential, float],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]

_TOKEN_RE = re.compile(r"(?i)\b(?:sk|key|tok|secret)[-_][A-Za-z0-9._~-]{4,}\b")
_URL_RE = re.compile(r"(?P<url>https?://[^\s\"'<>]+)", re.IGNORECASE)
_QUERY_RE = re.compile(r"(?i)(?P<prefix>[?&](?:api[_-]?key|password|secret|token)=)[^&\s\"']+")
_AUTH_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)\s+)[^\s,;]+"
)


def _safe_url(match: re.Match[str]) -> str:
    raw = match.group("url")
    suffix = ""
    while raw and raw[-1] in ".,;)]}":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        if parts.hostname is None:
            return "[REDACTED]" + suffix
        host = parts.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        query = "redacted" if parts.query else ""
        return urlunsplit((parts.scheme, f"{host}{port}", parts.path, query, "")) + suffix
    except ValueError:
        return "[REDACTED]" + suffix


def _sanitize_text(value: str) -> str:
    text = _URL_RE.sub(_safe_url, value)
    text = _QUERY_RE.sub(lambda match: f"{match.group('prefix')}[REDACTED]", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group('prefix')}[REDACTED]", text)
    return _TOKEN_RE.sub("[REDACTED]", text)
_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "streaming": ("streaming",),
    "tool_use": ("tool_use", "toolUse"),
    "parallel_tool_use": ("parallel_tool_use", "parallelToolUse"),
    "tool_choice": ("tool_choice", "toolChoice"),
    "system_messages": ("system_messages", "systemMessages"),
    "usage_in_stream": ("usage_in_stream", "usageInStream"),
    "prompt_cache": ("prompt_cache", "promptCache"),
    "multimodal_images": ("multimodal_images", "multimodalImages"),
    "multimodal_documents": ("multimodal_documents", "multimodalDocuments"),
}


def safe_model_diagnostic(value: object) -> str:
    """Return a bounded diagnostic with secrets and URL query values removed."""

    sanitized = _sanitize_text(str(value) if value is not None else "model probe failed")
    return sanitized[:256]


@dataclass(frozen=True, slots=True)
class CapabilityProbeResult:
    route_id: str
    status: CapabilityProbeStatus
    capabilities: ModelCapabilities | None
    missing: tuple[str, ...]
    error_code: str | None
    checked_at: datetime
    latency_ms: float

    def public_dict(self) -> dict[str, object]:
        return {
            "routeId": self.route_id,
            "status": self.status,
            "capabilities": (
                self.capabilities.model_dump(mode="json", by_alias=True)
                if self.capabilities is not None
                else None
            ),
            "missing": list(self.missing),
            "errorCode": self.error_code,
            "checkedAt": self.checked_at.isoformat(),
            "latencyMs": round(self.latency_ms, 3),
        }


@dataclass(frozen=True, slots=True)
class RouteHealthReport:
    route_id: str
    status: HealthStatus
    capability_probe: CapabilityProbeResult | None
    error_code: str | None
    diagnostic: str | None
    checked_at: datetime
    latency_ms: float

    def public_dict(self) -> dict[str, object]:
        return {
            "routeId": self.route_id,
            "status": self.status,
            "capabilityProbe": (
                self.capability_probe.public_dict() if self.capability_probe is not None else None
            ),
            "errorCode": self.error_code,
            "diagnostic": self.diagnostic,
            "checkedAt": self.checked_at.isoformat(),
            "latencyMs": round(self.latency_ms, 3),
        }


class ModelHealthChecker:
    """Run one bounded route probe through an injected transport."""

    def __init__(self, resolver: CredentialResolver, transport: CapabilityProbeTransport) -> None:
        self._resolver = resolver
        self._transport = transport

    async def probe(
        self,
        route: ModelRoute,
        *,
        required_capabilities: tuple[str, ...] = (),
        timeout_s: float | None = None,
    ) -> RouteHealthReport:
        started = time.perf_counter()
        checked_at = datetime.now(UTC)
        timeout = timeout_s if timeout_s is not None else min(route.limits.request_timeout_s, 30.0)
        try:
            handle = validate_credential_handle(route.credential_handle)
            credential = self._resolver.resolve(handle)
            raw = self._transport(route, credential, timeout)
            if inspect.isawaitable(raw):
                raw = await raw
            if not isinstance(raw, Mapping):
                raise TypeError("probe response is not an object")
            capabilities = _parse_capabilities(raw.get("capabilities"))
            result = build_capability_probe_result(
                route,
                capabilities,
                required_capabilities=required_capabilities,
                checked_at=checked_at,
                latency_ms=(time.perf_counter() - started) * 1000,
                ok=raw.get("ok") is True,
            )
            status: HealthStatus = "healthy" if result.status == "supported" else "degraded"
            if raw.get("ok") is not True:
                status = "unhealthy"
            return RouteHealthReport(
                route_id=route.route_id,
                status=status,
                capability_probe=result,
                error_code=result.error_code,
                diagnostic=None,
                checked_at=checked_at,
                latency_ms=result.latency_ms,
            )
        except CredentialHandleError as error:
            latency = (time.perf_counter() - started) * 1000
            return RouteHealthReport(
                route_id=route.route_id,
                status="unavailable",
                capability_probe=None,
                error_code=error.code,
                diagnostic=None,
                checked_at=checked_at,
                latency_ms=latency,
            )
        except Exception as error:
            latency = (time.perf_counter() - started) * 1000
            return RouteHealthReport(
                route_id=route.route_id,
                status="unhealthy",
                capability_probe=None,
                error_code=MODEL_HEALTH_PROBE_FAILED,
                diagnostic=safe_model_diagnostic(error),
                checked_at=checked_at,
                latency_ms=latency,
            )

    def probe_sync(
        self,
        route: ModelRoute,
        *,
        required_capabilities: tuple[str, ...] = (),
        timeout_s: float | None = None,
    ) -> RouteHealthReport:
        import asyncio

        return asyncio.run(
            self.probe(
                route,
                required_capabilities=required_capabilities,
                timeout_s=timeout_s,
            )
        )


def build_capability_probe_result(
    route: ModelRoute,
    capabilities: ModelCapabilities | None,
    *,
    required_capabilities: tuple[str, ...],
    checked_at: datetime,
    latency_ms: float,
    ok: bool,
) -> CapabilityProbeResult:
    if capabilities is None:
        return CapabilityProbeResult(
            route_id=route.route_id,
            status="unavailable",
            capabilities=None,
            missing=tuple(required_capabilities),
            error_code=MODEL_HEALTH_PROBE_FAILED,
            checked_at=checked_at,
            latency_ms=latency_ms,
        )
    missing = tuple(
        capability
        for capability in required_capabilities
        if not getattr(capabilities, capability, False)
    )
    if missing:
        status: CapabilityProbeStatus = "partial" if len(missing) < len(required_capabilities) else "unsupported"
        error_code: str | None = MODEL_CAPABILITY_UNSUPPORTED
    else:
        status = "supported" if ok else "unavailable"
        error_code = None if ok else MODEL_HEALTH_PROBE_FAILED
    return CapabilityProbeResult(
        route_id=route.route_id,
        status=status,
        capabilities=capabilities,
        missing=missing,
        error_code=error_code,
        checked_at=checked_at,
        latency_ms=latency_ms,
    )


def _parse_capabilities(value: object) -> ModelCapabilities | None:
    if not isinstance(value, Mapping):
        return None
    canonical: dict[str, object] = {}
    for field, aliases in _CAPABILITY_ALIASES.items():
        candidate = next((value[name] for name in aliases if name in value), None)
        if not isinstance(candidate, bool):
            return None
        canonical[field] = candidate
    try:
        return ModelCapabilities.model_validate(canonical)
    except Exception:
        return None


__all__ = [
    "CapabilityProbeResult",
    "CapabilityProbeStatus",
    "HealthStatus",
    "ModelHealthChecker",
    "RouteHealthReport",
    "build_capability_probe_result",
    "safe_model_diagnostic",
]
