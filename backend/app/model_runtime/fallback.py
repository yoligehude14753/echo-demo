"""Explicit, revision-pinned model fallback decisions and events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.model_runtime.errors import MODEL_FALLBACK_EXHAUSTED
from app.model_runtime.health import RouteHealthReport
from app.model_runtime.revision import TaskModelRevisionRegistry
from app.model_runtime.snapshot import ModelRuntimeSnapshot, validate_request_identity
from app.model_runtime.types import RequestIdentity

FallbackReason = Literal[
    "health_unhealthy",
    "capability_missing",
    "credential_unavailable",
    "timeout",
    "upstream_error",
    "cancelled",
]
FallbackOutcome = Literal["selected", "exhausted"]


@dataclass(frozen=True, slots=True)
class ModelFallbackEvent:
    """Typed event that makes fallback visible to the caller and UI layer."""

    schema_version: Literal[1]
    event_type: Literal["model.fallback"]
    identity: RequestIdentity
    from_route_id: str
    to_route_id: str | None
    reason: FallbackReason
    outcome: FallbackOutcome
    error_code: str | None
    emitted_at: datetime

    def public_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.event_type,
            **self.identity.model_dump(mode="json", by_alias=True),
            "fromRouteId": self.from_route_id,
            "toRouteId": self.to_route_id,
            "reason": self.reason,
            "outcome": self.outcome,
            "errorCode": self.error_code,
            "emittedAt": self.emitted_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    snapshot: ModelRuntimeSnapshot | None
    event: ModelFallbackEvent


class ExplicitFallbackRouter:
    """Select only an explicitly declared fallback on the task's config."""

    def __init__(self, registry: TaskModelRevisionRegistry) -> None:
        self._registry = registry

    def select(
        self,
        task_id: str,
        identity: RequestIdentity,
        reason: FallbackReason,
        health: Mapping[str, RouteHealthReport],
        *,
        required_capabilities: tuple[str, ...] = (),
    ) -> FallbackDecision:
        binding = self._registry.binding(task_id)
        validate_request_identity(identity, binding.snapshot)
        current = binding.route(binding.snapshot.route_id)
        pending = list(current.fallback_route_ids)
        visited: set[str] = {current.route_id}
        while pending:
            route_id = pending.pop(0)
            if route_id in visited:
                continue
            visited.add(route_id)
            route = binding.route(route_id)
            report = health.get(route_id)
            if report is not None and report.status == "healthy":
                candidate = binding.snapshot_for_route(route_id)
                if all(
                    getattr(candidate.capabilities, capability, False)
                    for capability in required_capabilities
                ) and (report.capability_probe is None or not report.capability_probe.missing):
                    return FallbackDecision(
                        snapshot=candidate,
                        event=_event(
                            identity,
                            from_route_id=current.route_id,
                            to_route_id=route_id,
                            reason=reason,
                            outcome="selected",
                            error_code=None,
                        ),
                    )
            pending.extend(route.fallback_route_ids)
        return FallbackDecision(
            snapshot=None,
            event=_event(
                identity,
                from_route_id=current.route_id,
                to_route_id=None,
                reason=reason,
                outcome="exhausted",
                error_code=MODEL_FALLBACK_EXHAUSTED,
            ),
        )

    choose = select


def _event(
    identity: RequestIdentity,
    *,
    from_route_id: str,
    to_route_id: str | None,
    reason: FallbackReason,
    outcome: FallbackOutcome,
    error_code: str | None,
) -> ModelFallbackEvent:
    return ModelFallbackEvent(
        schema_version=1,
        event_type="model.fallback",
        identity=identity,
        from_route_id=from_route_id,
        to_route_id=to_route_id,
        reason=reason,
        outcome=outcome,
        error_code=error_code,
        emitted_at=datetime.now(UTC),
    )


__all__ = [
    "ExplicitFallbackRouter",
    "FallbackDecision",
    "FallbackOutcome",
    "FallbackReason",
    "ModelFallbackEvent",
]
