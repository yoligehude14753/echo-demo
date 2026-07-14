"""Stable ASR admission/provider errors and HTTP mapping."""

from __future__ import annotations

import math
from typing import Any


class ASRError(RuntimeError):
    """Base class for errors that may cross an ASR HTTP boundary."""

    status_code = 503
    machine_code = "asr_provider_unavailable"
    default_detail = "ASR is temporarily unavailable."
    retryable = False

    def __init__(self, detail: str | None = None, *, retry_after_s: float | None = None) -> None:
        self.retry_after_s = retry_after_s
        self.safe_detail = detail or self.default_detail
        super().__init__(self.safe_detail)


class ASRAudioRejected(ASRError):
    status_code = 422
    machine_code = "asr_audio_rejected"
    default_detail = "Audio was rejected by ASR admission."


class ASRIdempotencyConflict(ASRError):
    status_code = 409
    machine_code = "asr_idempotency_conflict"
    default_detail = "ASR idempotency key conflicts with an existing request."


class ASRRateLimited(ASRError):
    status_code = 429
    machine_code = "asr_rate_limited"
    default_detail = "ASR caller or tenant quota is temporarily exhausted."
    retryable = True


class ASRQueueFull(ASRError):
    status_code = 503
    machine_code = "asr_queue_full"
    default_detail = "ASR queue capacity is temporarily exhausted."
    retryable = True


class ASRNoEligibleProvider(ASRError):
    status_code = 503
    machine_code = "asr_no_eligible_provider"
    default_detail = "No eligible ASR capability is available."
    retryable = True


class ASRDeadlineExceeded(ASRError):
    status_code = 504
    machine_code = "asr_deadline_exceeded"
    default_detail = "ASR deadline was exceeded."


class ASRSchedulerDisabled(ASRError):
    status_code = 503
    machine_code = "asr_scheduler_disabled"
    default_detail = "ASR scheduler is disabled."


class ASRSchedulerShutdown(ASRError):
    status_code = 503
    machine_code = "asr_scheduler_shutdown"
    default_detail = "ASR scheduler is shutting down."
    retryable = True


class ASRProviderTransientError(ASRError):
    """Adapter-facing safe transient error used by retry/failover logic."""

    status_code = 503
    machine_code = "asr_provider_transient"
    default_detail = "ASR provider returned a transient failure."
    retryable = True


class ASRProviderRateLimited(ASRError):
    """Provider-side rate limiting, distinct from caller admission quota."""

    status_code = 503
    machine_code = "provider_rate_limited"
    default_detail = "ASR provider rate limit is temporarily exhausted."
    retryable = True


class ASRProviderPermanentError(ASRError):
    status_code = 503
    machine_code = "asr_provider_unavailable"
    default_detail = "ASR provider could not complete the operation."


class ASRProviderAuthError(ASRError):
    status_code = 503
    machine_code = "asr_provider_auth_unavailable"
    default_detail = "ASR provider authentication is not ready."


class ASRProviderProtocolError(ASRError):
    status_code = 503
    machine_code = "asr_provider_protocol_error"
    default_detail = "ASR provider protocol response was invalid."


class ASRProviderMidstreamError(ASRError):
    """A stream failed after audio crossed the provider boundary.

    It is intentionally not retryable: replaying the same unfinished payload
    on another provider could duplicate transcription.
    """

    status_code = 503
    machine_code = "asr_provider_midstream_failed"
    default_detail = "ASR stream failed after audio admission."


class ASRLocalUnavailable(ASRError):
    status_code = 503
    machine_code = "asr_local_unavailable"
    default_detail = "Local ASR capability is not ready."


class ASRProviderSessionCapacity(ASRError):
    status_code = 503
    machine_code = "asr_provider_session_capacity"
    default_detail = "ASR stream session capacity is temporarily exhausted."
    retryable = True


def as_http_error(error: ASRError) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Return a stable status/payload/headers tuple for an ASR HTTP handler."""

    headers: dict[str, str] = {}
    if error.retry_after_s is not None:
        bounded_retry_after = min(60.0, max(0.0, error.retry_after_s))
        headers["Retry-After"] = str(max(1, math.ceil(bounded_retry_after)))
    return (
        error.status_code,
        {
            "error": {
                "code": error.machine_code,
                "message": error.safe_detail,
            }
        },
        headers,
    )


__all__ = [
    "ASRAudioRejected",
    "ASRDeadlineExceeded",
    "ASRError",
    "ASRIdempotencyConflict",
    "ASRLocalUnavailable",
    "ASRNoEligibleProvider",
    "ASRProviderAuthError",
    "ASRProviderMidstreamError",
    "ASRProviderPermanentError",
    "ASRProviderProtocolError",
    "ASRProviderRateLimited",
    "ASRProviderSessionCapacity",
    "ASRProviderTransientError",
    "ASRQueueFull",
    "ASRRateLimited",
    "ASRSchedulerDisabled",
    "ASRSchedulerShutdown",
    "as_http_error",
]
