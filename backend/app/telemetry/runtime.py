"""Production telemetry wiring kept outside the business database and HTTP API."""

from __future__ import annotations

import logging

from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.errors import (
    ASRAudioRejected,
    ASRDeadlineExceeded,
    ASRError,
    ASRIdempotencyConflict,
    ASRNoEligibleProvider,
    ASRRateLimited,
)
from app.config import Settings
from app.telemetry.adapters import NoopTelemetryAdapter
from app.telemetry.contracts import (
    APP_VERSION_PATTERN,
    FailureReason,
    TelemetryIdentityInput,
    TelemetryObservation,
    TelemetryOperation,
    TelemetryPlatform,
    TelemetryProvider,
)
from app.telemetry.ports import TelemetryPort
from app.telemetry.pseudonym import HmacPseudonymizer
from app.telemetry.sqlite import SQLiteTelemetryAdapter

logger = logging.getLogger("echodesk.telemetry")


def _platform(value: str | None) -> TelemetryPlatform:
    try:
        return TelemetryPlatform(value or "unknown")
    except ValueError:
        return TelemetryPlatform.UNKNOWN


def _app_version(value: str | None) -> str:
    candidate = (value or "unknown").strip()
    return candidate if APP_VERSION_PATTERN.fullmatch(candidate) else "unknown"


def _provider(value: str | None) -> TelemetryProvider:
    try:
        return TelemetryProvider(value or "unknown")
    except ValueError:
        return TelemetryProvider.STT


def _failure_reason(error: BaseException | None) -> FailureReason | None:
    if error is None:
        return None
    for error_type, reason in (
        (ASRAudioRejected, FailureReason.INVALID_INPUT),
        (ASRIdempotencyConflict, FailureReason.CONFLICT),
        (ASRRateLimited, FailureReason.RATE_LIMITED),
        (ASRDeadlineExceeded, FailureReason.TIMEOUT),
        (ASRNoEligibleProvider, FailureReason.PROVIDER_UNAVAILABLE),
    ):
        if isinstance(error, error_type):
            return reason
    if isinstance(error, ASRError):
        return FailureReason.PROVIDER_UNAVAILABLE
    return FailureReason.INTERNAL


class TelemetryRuntime:
    """Fail-soft recorder around a strict, privacy-preserving TelemetryPort."""

    def __init__(self, sink: TelemetryPort) -> None:
        self.sink = sink
        self._sink_failure_count = 0

    @property
    def sink_failure_count(self) -> int:
        return self._sink_failure_count

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
    ) -> None:
        """Record only materialized, typed ASR outcome fields."""

        try:
            observation = TelemetryObservation(
                event_id=f"asr:{context.idempotency_key or context.request_id}",
                identity=TelemetryIdentityInput(
                    tenant_id=context.tenant_id or "unknown-tenant",
                    user_id=context.principal_id or "unknown-principal",
                    device_id=context.device_id or "unknown-device",
                ),
                operation=TelemetryOperation.TRANSCRIBE,
                platform=_platform(context.platform),
                app_version=_app_version(context.app_version),
                provider=_provider(provider),
                success=success,
                failure_reason=_failure_reason(error),
                end_to_end_latency_ms=max(0, latency_ms),
                queue_wait_ms=max(0, queue_wait_ms),
                audio_duration_ms=max(0, audio_duration_ms),
            )
            await self.sink.record(observation)
        except Exception as sink_error:  # telemetry must never break ASR
            self._sink_failure_count += 1
            logger.warning(
                "telemetry sink failed: type=%s count=%d",
                type(sink_error).__name__,
                self._sink_failure_count,
            )


def build_telemetry_runtime(settings: Settings) -> TelemetryRuntime:
    if not settings.telemetry_enabled:
        return TelemetryRuntime(NoopTelemetryAdapter())
    if settings.telemetry_db_path is None or not str(settings.telemetry_db_path).strip():
        raise RuntimeError("telemetry is enabled but DB path is missing")
    key_ring = settings.telemetry_hmac_key_ring
    current_version = settings.telemetry_hmac_current_key_version.strip()
    if not key_ring or not current_version or current_version not in key_ring:
        raise RuntimeError("telemetry is enabled but HMAC key ring/current key is missing")
    keys = {
        version: value.encode("utf-8")
        for version, value in key_ring.items()
        if isinstance(version, str) and isinstance(value, str) and value
    }
    if current_version not in keys:
        raise RuntimeError("telemetry is enabled but current HMAC key is invalid")
    pseudonymizer = HmacPseudonymizer(
        keys,
        current_key_version=current_version,
        rotation_period_s=settings.telemetry_rotation_period_s,
    )
    sink = SQLiteTelemetryAdapter(
        settings.telemetry_db_path,
        pseudonymizer,
        retention_s=settings.telemetry_retention_s,
        k_threshold=settings.telemetry_k_threshold,
    )
    return TelemetryRuntime(sink)


__all__ = ["TelemetryRuntime", "build_telemetry_runtime"]
