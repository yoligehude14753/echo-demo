"""ASR-owned typed contracts.

This module deliberately stays below HTTP and application use cases. It
contains no provider SDK types and no sync-outbox concepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.meeting import TranscriptSegment

ASRReadinessStatus = Literal["ready", "degraded", "unavailable", "unknown"]
ASRReadinessReasonCode = Literal[
    "asr_ready",
    "asr_provider_capacity_degraded",
    "asr_controlled_probe_degraded",
    "asr_scheduler_not_accepting",
    "asr_no_eligible_provider",
    "asr_probe_stale",
    "asr_queue_saturated",
]
ASR_READINESS_SCHEMA_VERSION: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class ASRRequestContext:
    """Authenticated server context for one ASR operation.

    ``device_id`` is optional because a caller-provided device header is not a
    trusted binding. Integrators must populate these fields from the verified
    principal/session; the scheduler never promotes client claims to identity.
    """

    request_id: str
    idempotency_key: str | None = None
    tenant_id: str | None = None
    principal_id: str | None = None
    device_id: str | None = None
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if self.idempotency_key is not None and len(self.idempotency_key) > 256:
            raise ValueError("idempotency_key is too long")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")

    @property
    def scope_key(self) -> tuple[str, str, str]:
        """Return a stable quota key without trusting an unbound device claim."""

        return (
            self.tenant_id.strip() if self.tenant_id else "unknown-tenant",
            self.principal_id.strip() if self.principal_id else "unknown-principal",
            self.device_id.strip() if self.device_id else "unknown-device",
        )


@dataclass(frozen=True, slots=True)
class ASRPartialEvent:
    """Typed partial/delta event; never included in scheduler metrics/logs."""

    request_id: str
    sequence: int
    text: str
    correction_tail: str = ""
    finalized: bool = False


@dataclass(frozen=True, slots=True)
class ASRFinalTranscript:
    """Unified final result boundary returned by an ASR capability."""

    segments: tuple[TranscriptSegment, ...]
    provider_operation_key: str | None = None


@dataclass(frozen=True, slots=True)
class ASRProviderReadiness:
    """Internal provider state; never serialized to user-facing readiness."""

    circuit_state: Literal["closed", "open", "half_open"]
    in_flight: int
    max_concurrency: int
    eligible: bool
    auth_ready: bool


@dataclass(frozen=True, slots=True)
class ASRReadinessSnapshot:
    """Internal readiness snapshot with provider names retained for operations."""

    scheduler_accepting: bool
    queue_capacity: int
    queue_available: int
    eligible_provider_count: int
    active_jobs: int
    worker_count: int
    checked_at: datetime | None = None
    probe_ok: bool | None = None
    providers: dict[str, ASRProviderReadiness] = field(default_factory=dict)
    reason_code: str = "asr_probe_not_run"
    retry_after_s: float | None = None

    @property
    def accepting(self) -> bool:
        return self.scheduler_accepting

    @property
    def queue_saturation(self) -> float:
        if self.queue_capacity <= 0:
            return 1.0
        return round((self.queue_capacity - self.queue_available) / self.queue_capacity, 6)

    @property
    def last_controlled_probe_at(self) -> datetime | None:
        return self.checked_at

    def _classify(
        self,
        *,
        now: datetime | None,
        ttl_s: float,
    ) -> tuple[ASRReadinessStatus, bool, ASRReadinessReasonCode]:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        current = now or datetime.now(tz=self.checked_at.tzinfo if self.checked_at else None)
        age_s: float | None = None
        if self.checked_at is not None:
            checked_at = self.checked_at
            if checked_at.tzinfo is None and current.tzinfo is not None:
                checked_at = checked_at.replace(tzinfo=current.tzinfo)
            age_s = max(0.0, (current - checked_at).total_seconds())
        status: ASRReadinessStatus = "ready"
        accepting = True
        reason: ASRReadinessReasonCode = "asr_ready"
        if not self.scheduler_accepting:
            status, accepting, reason = "unavailable", False, "asr_scheduler_not_accepting"
        elif self.eligible_provider_count == 0:
            status, accepting, reason = "unavailable", False, "asr_no_eligible_provider"
        elif self.checked_at is None or age_s is None or age_s > ttl_s:
            status, accepting, reason = "unknown", False, "asr_probe_stale"
        elif self.queue_available <= 0:
            status, accepting, reason = "unavailable", False, "asr_queue_saturated"
        elif self.probe_ok is False:
            status, reason = "degraded", "asr_controlled_probe_degraded"
        elif self.eligible_provider_count < len(self.providers):
            status, reason = "degraded", "asr_provider_capacity_degraded"
        return status, accepting, reason

    def to_internal(
        self,
        *,
        now: datetime | None = None,
        ttl_s: float = 30.0,
    ) -> ASRReadinessInternal:
        """Return authenticated internal/admin aggregate projection."""

        status, accepting, reason = self._classify(now=now, ttl_s=ttl_s)
        return ASRReadinessInternal(
            schema_version=ASR_READINESS_SCHEMA_VERSION,
            status=status,
            accepting=accepting,
            queue_capacity=self.queue_capacity,
            queue_available=self.queue_available,
            queue_saturation=self.queue_saturation,
            eligible_provider_count=self.eligible_provider_count,
            checked_at=self.checked_at,
            ttl_s=ttl_s,
            reason_code=reason,
            retry_after_s=self.retry_after_s,
        )

    def to_public(
        self,
        *,
        now: datetime | None = None,
        ttl_s: float = 30.0,
    ) -> ASRReadinessPublic:
        """Return the default user-safe projection without provider aggregates."""

        internal = self.to_internal(now=now, ttl_s=ttl_s)
        return ASRReadinessPublic(
            schema_version=internal.schema_version,
            status=internal.status,
            accepting=internal.accepting,
            checked_at=internal.checked_at,
            ttl_s=internal.ttl_s,
            reason_code=internal.reason_code,
            retry_after_s=internal.retry_after_s,
        )

    def to_wire(
        self,
        *,
        now: datetime | None = None,
        ttl_s: float = 30.0,
    ) -> ASRReadinessPublic:
        """Backward-compatible alias for the safe public projection."""

        return self.to_public(now=now, ttl_s=ttl_s)


class _ReadinessBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = ASR_READINESS_SCHEMA_VERSION
    status: ASRReadinessStatus
    accepting: bool
    checked_at: datetime | None
    ttl_s: float = Field(gt=0.0)
    reason_code: ASRReadinessReasonCode
    retry_after_s: float | None = Field(default=None, ge=0.0)


class ASRReadinessInternal(_ReadinessBase):
    """Authenticated internal/admin projection with aggregate capacity."""

    queue_capacity: int = Field(ge=0)
    queue_available: int = Field(ge=0)
    queue_saturation: float = Field(ge=0.0, le=1.0)
    eligible_provider_count: int = Field(ge=0)


class ASRReadinessPublic(_ReadinessBase):
    """Default client projection; intentionally contains no capacity/vendor data."""


# Existing callers may still import this name; its public shape is now safe by
# construction. Internal consumers must use ASRReadinessInternal explicitly.
ASRReadinessWire = ASRReadinessPublic


__all__ = [
    "ASR_READINESS_SCHEMA_VERSION",
    "ASRFinalTranscript",
    "ASRPartialEvent",
    "ASRProviderReadiness",
    "ASRReadinessInternal",
    "ASRReadinessPublic",
    "ASRReadinessSnapshot",
    "ASRReadinessStatus",
    "ASRReadinessWire",
    "ASRRequestContext",
]
