"""ASR scheduler contract tests.

These tests intentionally use fake STT ports. They prove scheduler semantics,
not live provider availability or real transcription quality.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.errors import (
    ASRAudioRejected,
    ASRDeadlineExceeded,
    ASRNoEligibleProvider,
    ASRProviderTransientError,
    ASRQueueFull,
    ASRRateLimited,
    ASRSchedulerShutdown,
    as_http_error,
)
from app.adapters.stt.scheduler import (
    ASRProviderBinding,
    ASRScheduler,
    ASRSchedulerConfig,
)
from app.schemas.meeting import TranscriptSegment

VALID_AUDIO = b"\x01\x00" * 80
SILENT_AUDIO = b"\x00" * 160


class FakeSTT:
    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        failures: int = 0,
        failure_error: Exception | None = None,
        text: str = "fake-result",
    ) -> None:
        self.delay_s = delay_s
        self.failures = failures
        self.failure_error = failure_error or ASRProviderTransientError()
        self.text = text
        self.calls = 0
        self.active = 0
        self.max_active = 0
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
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.failures:
                self.failures -= 1
                raise self.failure_error
            return [TranscriptSegment(text=self.text, start_ms=0, end_ms=100)]
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


def binding(
    name: str,
    provider: FakeSTT,
    *,
    weight: float = 1.0,
    max_concurrency: int = 1,
    transport: str = "sse_one_shot",
) -> ASRProviderBinding:
    return ASRProviderBinding(
        name=name,
        adapter=provider,
        weight=weight,
        max_concurrency=max_concurrency,
        transport=transport,
    )


def config(**overrides: object) -> ASRSchedulerConfig:
    values: dict[str, object] = {
        "enabled": True,
        "eligible_providers": ("primary", "fallback"),
        "max_concurrency": 2,
        "queue_size": 2,
        "job_deadline_s": 0.5,
        "max_attempts": 2,
        "circuit_failure_threshold": 2,
        "circuit_cooldown_s": 0.05,
        "scope_max_concurrency": 2,
        "scope_rate_limit_per_minute": 100,
    }
    values.update(overrides)
    return ASRSchedulerConfig(**values)


def context(
    *,
    key: str | None = None,
    tenant: str | None = "tenant-a",
    deadline_s: float | None = None,
) -> ASRRequestContext:
    return ASRRequestContext(
        request_id="request-1",
        idempotency_key=key,
        tenant_id=tenant,
        principal_id="principal-a",
        device_id=None,
        deadline_s=deadline_s,
    )


async def close_scheduler(scheduler: ASRScheduler) -> None:
    await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_and_silent_audio_are_rejected_before_provider_queue() -> None:
    provider = FakeSTT()
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",)),
    )
    try:
        with pytest.raises(ASRAudioRejected) as empty_error:
            await scheduler.transcribe(b"", context=context())
        with pytest.raises(ASRAudioRejected) as silent_error:
            await scheduler.transcribe(SILENT_AUDIO, context=context())
        assert empty_error.value.status_code == 422
        assert silent_error.value.machine_code == "asr_audio_rejected"
        assert provider.calls == 0
        assert scheduler.metrics_snapshot()["accepted_total"] == 0
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_queue_is_bounded_and_overflow_returns_503() -> None:
    provider = FakeSTT(delay_s=0.15)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider, max_concurrency=1)},
        config(
            eligible_providers=("primary",),
            max_concurrency=1,
            queue_size=1,
            job_deadline_s=1.0,
            scope_max_concurrency=10,
        ),
    )
    try:
        first = asyncio.create_task(scheduler.transcribe(VALID_AUDIO, context=context(key="q-1")))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(scheduler.transcribe(VALID_AUDIO, context=context(key="q-2")))
        await asyncio.sleep(0.02)
        with pytest.raises(ASRQueueFull) as overflow:
            await scheduler.transcribe(VALID_AUDIO, context=context(key="q-3"))
        assert overflow.value.status_code == 503
        assert overflow.value.retry_after_s is not None
        await asyncio.gather(first, second)
        assert scheduler.metrics_snapshot()["accepted_total"] == 2
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_quota_returns_429_without_consuming_global_queue() -> None:
    provider = FakeSTT(delay_s=0.1)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(
            eligible_providers=("primary",),
            max_concurrency=2,
            queue_size=2,
            scope_max_concurrency=1,
            scope_rate_limit_per_minute=100,
        ),
    )
    try:
        first = asyncio.create_task(scheduler.transcribe(VALID_AUDIO, context=context(key="r-1")))
        await asyncio.sleep(0.02)
        with pytest.raises(ASRRateLimited) as limited:
            await scheduler.transcribe(VALID_AUDIO, context=context(key="r-2"))
        assert limited.value.status_code == 429
        assert limited.value.machine_code == "asr_rate_limited"
        assert scheduler.metrics_snapshot()["accepted_total"] == 1
        await first
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deadline_cancels_provider_and_returns_504() -> None:
    provider = FakeSTT(delay_s=1.0)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(
            eligible_providers=("primary",),
            max_concurrency=1,
            queue_size=1,
            job_deadline_s=0.04,
            max_attempts=1,
        ),
    )
    try:
        with pytest.raises(ASRDeadlineExceeded) as deadline:
            await scheduler.transcribe(VALID_AUDIO, context=context(deadline_s=0.04))
        assert deadline.value.status_code == 504
        assert deadline.value.machine_code == "asr_deadline_exceeded"
        assert provider.cancelled == 1
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weighted_least_loaded_selection_uses_current_load() -> None:
    primary = FakeSTT(delay_s=0.12, text="primary")
    fallback = FakeSTT(text="fallback")
    scheduler = ASRScheduler(
        {
            "primary": binding("primary", primary, weight=3.0),
            "fallback": binding("fallback", fallback, weight=1.0),
        },
        config(max_concurrency=2, queue_size=1, scope_max_concurrency=3),
    )
    try:
        first = asyncio.create_task(
            scheduler.transcribe(VALID_AUDIO, context=context(key="load-1"))
        )
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            scheduler.transcribe(VALID_AUDIO, context=context(key="load-2"))
        )
        await asyncio.gather(first, second)
        assert primary.calls == 1
        assert fallback.calls == 1
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_transient_failure_falls_back_once_within_budget() -> None:
    primary = FakeSTT(failures=1)
    fallback = FakeSTT(text="fallback")
    scheduler = ASRScheduler(
        {
            "primary": binding("primary", primary),
            "fallback": binding("fallback", fallback),
        },
        config(max_concurrency=1, queue_size=1),
    )
    try:
        result = await scheduler.transcribe(VALID_AUDIO, context=context(key="fallback-1"))
        assert result[0].text == "fallback"
        assert primary.calls == 1
        assert fallback.calls == 1
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_circuit_opens_then_allows_one_half_open_probe_and_recovers() -> None:
    provider = FakeSTT(failures=2)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(
            eligible_providers=("primary",),
            max_concurrency=1,
            queue_size=1,
            max_attempts=1,
            circuit_failure_threshold=2,
            circuit_cooldown_s=0.04,
        ),
    )
    try:
        for key in ("cb-1", "cb-2"):
            with pytest.raises(Exception) as error:
                await scheduler.transcribe(VALID_AUDIO, context=context(key=key))
            assert error.value.status_code == 503
        with pytest.raises(ASRNoEligibleProvider):
            await scheduler.transcribe(VALID_AUDIO, context=context(key="cb-open"))
        await asyncio.sleep(0.05)
        result = await scheduler.transcribe(VALID_AUDIO, context=context(key="cb-probe"))
        assert result[0].text == "fake-result"
        assert scheduler.readiness().providers["primary"].circuit_state == "closed"
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotency_key_deduplicates_concurrent_success() -> None:
    provider = FakeSTT(delay_s=0.06)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",), max_concurrency=2),
    )
    try:
        one, two = await asyncio.gather(
            scheduler.transcribe(VALID_AUDIO, context=context(key="same-job")),
            scheduler.transcribe(VALID_AUDIO, context=context(key="same-job")),
        )
        assert one == two
        assert provider.calls == 1
        assert scheduler.metrics_snapshot()["idempotency_hits_total"] == 1
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caller_cancellation_cancels_accepted_provider_task() -> None:
    provider = FakeSTT(delay_s=1.0)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",), max_concurrency=1),
    )
    try:
        task = asyncio.create_task(scheduler.transcribe(VALID_AUDIO, context=context(key="cancel")))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
        assert provider.cancelled == 1
        assert scheduler.readiness().active_jobs == 0
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_stops_admission_and_does_not_leave_workers() -> None:
    provider = FakeSTT(delay_s=0.02)
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",), max_concurrency=1, queue_size=1),
    )
    await scheduler.start()
    await scheduler.close(grace_period_s=0.2)
    with pytest.raises(ASRSchedulerShutdown):
        await scheduler.transcribe(VALID_AUDIO, context=context(key="after-close"))
    assert scheduler.readiness().worker_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_and_metrics_expose_state_without_transcript_text() -> None:
    provider = FakeSTT(text="must-not-be-a-metric")
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",)),
    )
    try:
        await scheduler.transcribe(VALID_AUDIO, context=context(key="metrics"))
        readiness = scheduler.readiness()
        metrics = scheduler.metrics_snapshot()
        assert readiness.scheduler_accepting is True
        assert readiness.eligible_provider_count == 1
        assert readiness.last_controlled_probe_at is None
        assert metrics["completed_total"] == 1
        assert all("must-not-be-a-metric" not in str(value) for value in metrics.values())
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_internal_and_public_projections_are_distinct_and_truthful() -> None:
    provider = FakeSTT()
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(eligible_providers=("primary",)),
    )
    now = datetime.now(UTC)
    try:
        unknown = scheduler.readiness().to_public(now=now, ttl_s=30.0)
        assert unknown.schema_version == 1
        assert unknown.status == "unknown"
        assert unknown.accepting is False
        assert set(unknown.model_dump()) == {
            "schema_version",
            "status",
            "accepting",
            "checked_at",
            "ttl_s",
            "reason_code",
            "retry_after_s",
        }

        scheduler.record_controlled_probe(True, checked_at=now)
        snapshot = scheduler.readiness()
        internal = snapshot.to_internal(now=now, ttl_s=30.0)
        public = snapshot.to_public(now=now, ttl_s=30.0)
        assert internal.status == "ready"
        assert internal.accepting is True
        assert internal.queue_capacity >= internal.queue_available
        assert internal.eligible_provider_count == 1
        assert public.status == "ready"
        assert public.accepting is True
        assert "eligible_provider_count" not in public.model_dump()
        assert "queue_capacity" not in public.model_dump()

        scheduler.record_controlled_probe(False, checked_at=now)
        degraded = scheduler.readiness().to_public(now=now, ttl_s=30.0)
        assert degraded.status == "degraded"
        assert degraded.accepting is True

        stale = scheduler.readiness().to_public(
            now=now + timedelta(seconds=31),
            ttl_s=30.0,
        )
        assert stale.status == "unknown"
        assert stale.accepting is False

        unavailable_scheduler = ASRScheduler({}, config(eligible_providers=()))
        unavailable = unavailable_scheduler.readiness().to_public(now=now, ttl_s=30.0)
        assert unavailable.status == "unavailable"
        assert unavailable.accepting is False
    finally:
        await close_scheduler(scheduler)


@pytest.mark.unit
def test_asr_errors_map_to_stable_http_status_and_retry_after() -> None:
    cases: list[tuple[Exception, int, str, bool]] = [
        (ASRAudioRejected(), 422, "asr_audio_rejected", False),
        (ASRRateLimited(retry_after_s=2.0), 429, "asr_rate_limited", True),
        (ASRQueueFull(retry_after_s=1.0), 503, "asr_queue_full", True),
        (ASRDeadlineExceeded(), 504, "asr_deadline_exceeded", False),
    ]
    for error, status_code, machine_code, has_retry_after in cases:
        status, payload, headers = as_http_error(error)
        assert status == status_code
        assert payload["error"]["code"] == machine_code
        assert ("Retry-After" in headers) is has_retry_after


@pytest.mark.unit
def test_scheduler_config_rejects_unbounded_or_invalid_values() -> None:
    with pytest.raises(ValueError):
        ASRSchedulerConfig(queue_size=-1)
    with pytest.raises(ValueError):
        ASRSchedulerConfig(max_concurrency=0)
    with pytest.raises(ValueError):
        ASRSchedulerConfig(job_deadline_s=0)
    with pytest.raises(ValueError):
        ASRSchedulerConfig(eligible_providers=("",))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_contract_has_explicit_sample_count_and_raw_percentiles() -> None:
    provider = FakeSTT()
    scheduler = ASRScheduler(
        {"primary": binding("primary", provider)},
        config(
            eligible_providers=("primary",),
            max_concurrency=4,
            queue_size=8,
            scope_max_concurrency=20,
            scope_rate_limit_per_minute=200,
        ),
    )
    samples: list[float] = []
    try:
        for index in range(20):
            started = time.perf_counter()
            await scheduler.transcribe(VALID_AUDIO, context=context(key=f"load-{index}"))
            samples.append((time.perf_counter() - started) * 1000)
        report = scheduler.latency_report(samples)
        assert report["sample_count"] == 20
        assert report["p50_ms"] <= report["p95_ms"] <= report["p99_ms"]
    finally:
        await close_scheduler(scheduler)


ProviderFactory = Callable[[str], ASRProviderBinding]
