"""Telemetry privacy contracts.

Only the HMAC adapter may turn ``TelemetryIdentityInput`` into a stored event.
Materialized events and query results contain pseudonyms, never raw identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DURATION_MS: Final[int] = 86_400_000
DEFAULT_K_THRESHOLD: Final[int] = 5
PSEUDONYM_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
OPAQUE_TOKEN_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
KEY_VERSION_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,31}$"
APP_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:unknown|(?:0|[1-9][0-9]*)\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)$"
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for defaults and audit receipts."""

    return datetime.now(UTC)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def _validate_app_version(value: str) -> str:
    normalized = value.strip()
    if not APP_VERSION_PATTERN.fullmatch(normalized):
        raise ValueError("app_version must be a semantic version or unknown")
    return normalized


class TelemetryOperation(StrEnum):
    REQUEST = "request"
    MEETING_FINALIZE = "meeting_finalize"
    TRANSCRIBE = "transcribe"
    SYNTHESIZE = "synthesize"
    WORKFLOW = "workflow"
    RAG = "rag"
    ARTIFACT = "artifact"
    UNKNOWN = "unknown"


class TelemetryPlatform(StrEnum):
    DESKTOP = "desktop"
    ANDROID = "android"
    TV = "tv"
    WEB = "web"
    UNKNOWN = "unknown"


class TelemetryProvider(StrEnum):
    LOCAL = "local"
    MAIN = "main"
    FAST = "fast"
    STT = "stt"
    TTS = "tts"
    YUNWU = "yunwu"
    FIRERED = "firered"
    QWEN3_TTS = "qwen3_tts"
    TAVILY = "tavily"
    GLM = "glm"
    KIMI = "kimi"
    UNKNOWN = "unknown"


class FailureReason(StrEnum):
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_INPUT = "invalid_input"
    QUOTA_EXCEEDED = "quota_exceeded"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class FailureReasonCount(BaseModel):
    """Typed count for one stable failure reason in an aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: FailureReason
    event_count: int = Field(ge=1)


class DeletionReason(StrEnum):
    USER_REQUEST = "user_request"
    RETENTION = "retention"
    KEY_ERASURE = "key_erasure"


PROVIDER_REGISTRY: Final[frozenset[TelemetryProvider]] = frozenset(TelemetryProvider)


class TelemetryIdentityInput(BaseModel):
    """Server-validated identity material accepted only at the adapter boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=256)


class PseudonymousIdentity(BaseModel):
    """The only identity representation allowed in stored events and results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    user_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    device_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    key_version: str = Field(pattern=KEY_VERSION_PATTERN)
    epoch: int = Field(ge=0)


class _TelemetryOutcome(BaseModel):
    """Shared allowlisted metrics and outcome fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    operation: TelemetryOperation = TelemetryOperation.UNKNOWN
    platform: TelemetryPlatform = TelemetryPlatform.UNKNOWN
    app_version: str = "unknown"
    provider: TelemetryProvider = TelemetryProvider.UNKNOWN
    success: bool
    failure_reason: FailureReason | None = None
    end_to_end_latency_ms: int = Field(default=0, ge=0, le=MAX_DURATION_MS)
    queue_wait_ms: int = Field(default=0, ge=0, le=MAX_DURATION_MS)
    audio_duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)

    @field_validator("app_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_app_version(value)

    @model_validator(mode="after")
    def validate_failure_reason(self) -> _TelemetryOutcome:
        if self.success and self.failure_reason is not None:
            raise ValueError("successful telemetry cannot include failure_reason")
        if not self.success and self.failure_reason is None:
            object.__setattr__(self, "failure_reason", FailureReason.UNKNOWN)
        return self


class TelemetryObservation(_TelemetryOutcome):
    """Typed input for ``TelemetryPort.record``.

    The identity is consumed only by the server-side pseudonymization adapter and
    is never retained in the materialized event.
    """

    event_id: str = Field(pattern=OPAQUE_TOKEN_PATTERN)
    occurred_at: datetime = Field(default_factory=utc_now)
    identity: TelemetryIdentityInput

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class TelemetryEvent(_TelemetryOutcome):
    """Pseudonymized event retained by an adapter."""

    event_id: str = Field(pattern=OPAQUE_TOKEN_PATTERN)
    occurred_at: datetime
    identity: PseudonymousIdentity

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class TelemetryAggregate(BaseModel):
    """Cohort aggregate returned by a privacy-filtered query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch: int = Field(ge=0)
    key_version: str = Field(pattern=KEY_VERSION_PATTERN)
    tenant_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    user_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    device_pseudonym: str = Field(pattern=PSEUDONYM_PATTERN)
    operation: TelemetryOperation
    platform: TelemetryPlatform
    app_version: str
    provider: TelemetryProvider
    failure_reason_counts: tuple[FailureReasonCount, ...] = ()
    request_count: int = Field(ge=1)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    latency_sum_ms: int = Field(ge=0)
    queue_wait_sum_ms: int = Field(ge=0)
    audio_duration_sum_ms: int = Field(ge=0)
    audio_duration_event_count: int = Field(ge=0)

    @field_validator("app_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_app_version(value)


class TelemetryQuery(BaseModel):
    """Typed query; filters are pseudonyms or allowlisted enums only."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    start_at: datetime | None = None
    end_at: datetime | None = None
    epoch: int | None = Field(default=None, ge=0)
    key_version: str | None = Field(default=None, pattern=KEY_VERSION_PATTERN)
    tenant_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    user_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    device_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    operation: TelemetryOperation | None = None
    platform: TelemetryPlatform | None = None
    app_version: str | None = None
    provider: TelemetryProvider | None = None
    failure_reason: FailureReason | None = None
    k_threshold: int = Field(default=DEFAULT_K_THRESHOLD, ge=1, le=100_000)

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_query_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware_utc(value)

    @field_validator("app_version")
    @classmethod
    def validate_query_version(cls, value: str | None) -> str | None:
        return None if value is None else _validate_app_version(value)

    @model_validator(mode="after")
    def validate_query_window(self) -> TelemetryQuery:
        if self.start_at is not None and self.end_at is not None and self.start_at >= self.end_at:
            raise ValueError("query start_at must be before end_at")
        return self


class TelemetryDeleteRequest(BaseModel):
    """Typed deletion hook addressed only by pseudonymous identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    user_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    device_pseudonym: str | None = Field(default=None, pattern=PSEUDONYM_PATTERN)
    key_version: str | None = Field(default=None, pattern=KEY_VERSION_PATTERN)
    epoch: int | None = Field(default=None, ge=0)
    reason: DeletionReason = DeletionReason.USER_REQUEST

    @model_validator(mode="after")
    def require_identity_filter(self) -> TelemetryDeleteRequest:
        if not any(
            value is not None
            for value in (
                self.tenant_pseudonym,
                self.user_pseudonym,
                self.device_pseudonym,
            )
        ):
            raise ValueError("delete requires at least one pseudonymous identity filter")
        return self


class DeletionReceipt(BaseModel):
    """Deletion audit result without identity or free-text fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(pattern=OPAQUE_TOKEN_PATTERN)
    deleted_event_count: int = Field(ge=0)
    deleted_at: datetime = Field(default_factory=utc_now)
    reason: DeletionReason

    @field_validator("deleted_at")
    @classmethod
    def normalize_deleted_at(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


@dataclass(frozen=True, slots=True)
class TelemetryRuntimeConfig:
    """Dependency-injected runtime settings; disabled and keyless by default."""

    enabled: bool = False
    key_version: str = "v1"
    hmac_secret: bytes = field(default=b"", repr=False)
    retention_s: int = 30 * 24 * 60 * 60
    k_threshold: int = DEFAULT_K_THRESHOLD
    rotation_period_s: int = 30 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if not re.fullmatch(KEY_VERSION_PATTERN, self.key_version):
            raise ValueError("invalid telemetry key_version")
        if self.retention_s <= 0 or self.rotation_period_s <= 0:
            raise ValueError("telemetry durations must be positive")
        if self.k_threshold < 1:
            raise ValueError("telemetry k_threshold must be positive")


__all__ = [
    "APP_VERSION_PATTERN",
    "DEFAULT_K_THRESHOLD",
    "KEY_VERSION_PATTERN",
    "MAX_DURATION_MS",
    "OPAQUE_TOKEN_PATTERN",
    "PROVIDER_REGISTRY",
    "PSEUDONYM_PATTERN",
    "DeletionReason",
    "DeletionReceipt",
    "FailureReason",
    "FailureReasonCount",
    "PseudonymousIdentity",
    "TelemetryAggregate",
    "TelemetryDeleteRequest",
    "TelemetryEvent",
    "TelemetryIdentityInput",
    "TelemetryObservation",
    "TelemetryOperation",
    "TelemetryPlatform",
    "TelemetryProvider",
    "TelemetryQuery",
    "TelemetryRuntimeConfig",
    "utc_now",
]
