"""Application-facing ASR contracts and runtime ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.schemas.meeting import TranscriptSegment


@dataclass(frozen=True, slots=True)
class ASRRequestContext:
    """Authenticated server context for one ASR operation."""

    request_id: str
    idempotency_key: str | None = None
    tenant_id: str | None = None
    principal_id: str | None = None
    device_id: str | None = None
    deadline_s: float | None = None
    capability: str | None = None
    platform: str | None = None
    app_version: str | None = None
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if self.idempotency_key is not None and len(self.idempotency_key) > 256:
            raise ValueError("idempotency_key is too long")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")
        if self.capability is not None and not self.capability.strip():
            raise ValueError("capability must not be blank")
        if self.platform is not None and not self.platform.strip():
            raise ValueError("platform must not be blank")
        if self.app_version is not None and not self.app_version.strip():
            raise ValueError("app_version must not be blank")
        if any(not str(key).strip() for key in self.options):
            raise ValueError("ASR option names must not be blank")

    @property
    def scope_key(self) -> tuple[str, str, str]:
        """Return the scheduler quota key for the authenticated principal."""

        return (
            self.tenant_id.strip() if self.tenant_id else "unknown-tenant",
            self.principal_id.strip() if self.principal_id else "unknown-principal",
            self.device_id.strip() if self.device_id else "unknown-device",
        )


class ASRErrorBase(Exception):
    """Stable exception base for typed ASR failures crossing the port boundary."""


class ASRSchedulerPort(Protocol):
    """Bounded ASR admission and transcription boundary."""

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        context: ASRRequestContext,
    ) -> list[TranscriptSegment]: ...


class ASRTelemetryPort(Protocol):
    """Fail-soft telemetry boundary for legacy ASR calls."""

    async def record_asr(
        self,
        *,
        context: ASRRequestContext,
        provider: str | None,
        success: bool,
        error: BaseException | None,
        latency_ms: int,
        queue_wait_ms: int,
        audio_duration_ms: int,
    ) -> None: ...


__all__ = [
    "ASRErrorBase",
    "ASRRequestContext",
    "ASRSchedulerPort",
    "ASRTelemetryPort",
]
