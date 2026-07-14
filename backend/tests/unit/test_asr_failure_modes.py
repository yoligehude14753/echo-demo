"""ASR provider fault and failover contract tests."""

from __future__ import annotations

import asyncio

import pytest
from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.errors import (
    ASRDeadlineExceeded,
    ASRProviderRateLimited,
    ASRProviderTransientError,
    ASRRateLimited,
    as_http_error,
)
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.schemas.meeting import TranscriptSegment

VALID_AUDIO = b"\x01\x00" * 80


class FaultProvider:
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.error = error
        self.delay_s = delay_s
        self.calls = 0
        self.cancelled = 0

    async def transcribe(
        self,
        _audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        del sample_rate, language
        self.calls += 1
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.error is not None:
                raise self.error
            return [TranscriptSegment(text="ok", start_ms=0, end_ms=100)]
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def ctx(key: str) -> ASRRequestContext:
    return ASRRequestContext(
        request_id=key,
        idempotency_key=key,
        tenant_id="tenant-fault",
        principal_id="principal-fault",
    )


def make_scheduler(
    primary: FaultProvider,
    fallback: FaultProvider,
    *,
    deadline_s: float = 0.2,
) -> ASRScheduler:
    return ASRScheduler(
        {
            "primary": ASRProviderBinding(name="primary", adapter=primary),
            "fallback": ASRProviderBinding(name="fallback", adapter=fallback),
        },
        ASRSchedulerConfig(
            enabled=True,
            eligible_providers=("primary", "fallback"),
            max_concurrency=1,
            queue_size=1,
            job_deadline_s=deadline_s,
            max_attempts=2,
            scope_max_concurrency=2,
            scope_rate_limit_per_minute=20,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error",
    [
        ASRRateLimited(retry_after_s=1.0),
        ASRProviderRateLimited(retry_after_s=2.0),
        ASRProviderTransientError(),
    ],
)
async def test_provider_429_or_5xx_transient_failure_falls_back_once(
    provider_error: Exception,
) -> None:
    primary = FaultProvider(error=provider_error)
    fallback = FaultProvider()
    scheduler = make_scheduler(primary, fallback)
    try:
        result = await scheduler.transcribe(VALID_AUDIO, context=ctx("fault-fallback"))
        assert result[0].text == "ok"
        assert primary.calls == 1
        assert fallback.calls == 1
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_rate_limit_without_fallback_is_external_503_with_bounded_retry_after() -> (
    None
):
    primary = FaultProvider(error=ASRProviderRateLimited(retry_after_s=120.0))
    scheduler = ASRScheduler(
        {"primary": ASRProviderBinding(name="primary", adapter=primary)},
        ASRSchedulerConfig(
            enabled=True,
            eligible_providers=("primary",),
            max_concurrency=1,
            queue_size=0,
            job_deadline_s=0.2,
            max_attempts=1,
            scope_max_concurrency=2,
            scope_rate_limit_per_minute=20,
        ),
    )
    try:
        with pytest.raises(ASRProviderRateLimited) as error:
            await scheduler.transcribe(VALID_AUDIO, context=ctx("fault-no-fallback"))
        status, payload, headers = as_http_error(error.value)
        assert status == 503
        assert payload["error"]["code"] == "provider_rate_limited"
        assert headers["Retry-After"] == "60"
        assert primary.calls == 1
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_timeout_is_504_and_task_is_cancelled() -> None:
    primary = FaultProvider(delay_s=1.0)
    fallback = FaultProvider()
    scheduler = make_scheduler(primary, fallback, deadline_s=0.03)
    try:
        with pytest.raises(ASRDeadlineExceeded):
            await scheduler.transcribe(VALID_AUDIO, context=ctx("fault-timeout"))
        assert primary.cancelled == 1
        assert fallback.calls == 0
    finally:
        await scheduler.close(grace_period_s=0.2)
