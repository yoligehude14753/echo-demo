"""ASR-owned public readiness projection tests."""

from __future__ import annotations

import asyncio

import app.adapters.stt as stt_adapters
import pytest
from app.adapters.stt import (
    build_asr_scheduler,
    reset_asr_scheduler_for_test,
    start_asr_scheduler,
    stop_asr_scheduler,
)
from app.adapters.stt.errors import ASRProviderTransientError
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.api.asr import get_asr_readiness, router
from app.config import Settings
from app.main import create_app
from app.schemas.meeting import TranscriptSegment
from fastapi.testclient import TestClient


class StartupProbeSTT:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def transcribe(
        self,
        _audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        del sample_rate, language
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return []


def startup_probe_scheduler(provider: StartupProbeSTT) -> ASRScheduler:
    return ASRScheduler(
        {
            "firered": ASRProviderBinding(
                name="firered",
                adapter=provider,
                max_concurrency=1,
            )
        },
        ASRSchedulerConfig(
            enabled=True,
            eligible_providers=("firered",),
            max_concurrency=1,
            queue_size=1,
            job_deadline_s=0.2,
            max_attempts=1,
            circuit_failure_threshold=3,
            readiness_stale_after_s=1.0,
        ),
    )


async def wait_for_probe(scheduler: ASRScheduler) -> None:
    for _ in range(100):
        if scheduler.readiness().last_controlled_probe_at is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("startup controlled probe did not finish")


async def wait_for_probe_calls(provider: StartupProbeSTT, expected: int) -> None:
    for _ in range(100):
        if provider.calls >= expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"controlled probe call count did not reach {expected}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_readiness_is_safe_and_unknown_is_not_ready() -> None:
    settings = Settings()
    scheduler = build_asr_scheduler(settings)
    try:
        response = await get_asr_readiness(settings=settings, scheduler=scheduler)
        assert response.schema_version == 1
        assert response.status == "unavailable"
        assert response.accepting is False
        assert set(response.model_dump()) == {
            "schema_version",
            "status",
            "accepting",
            "checked_at",
            "ttl_s",
            "reason_code",
            "retry_after_s",
        }
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
def test_asr_readiness_route_is_owned_by_asr_module() -> None:
    assert any(route.path == "/asr/readiness" for route in router.routes)


@pytest.mark.unit
def test_main_registers_asr_readiness_router_without_sync_or_capture_route_copy() -> None:
    app = create_app()
    paths = app.openapi()["paths"]
    assert "/asr/readiness" in paths
    assert "/capture/readiness" not in paths

    with TestClient(app) as client:
        readiness = client.get("/asr/readiness")
        capture_readiness = client.get("/capture/readiness")

    assert readiness.status_code == 200
    assert readiness.json()["schema_version"] == 1
    assert capture_readiness.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_scheduler_lifecycle_has_no_workers() -> None:
    reset_asr_scheduler_for_test()
    settings = Settings(asr_scheduler_enabled=False)
    try:
        scheduler = await start_asr_scheduler(settings)
        assert scheduler.readiness().worker_count == 0
        assert scheduler.readiness().scheduler_accepting is False
        assert scheduler.readiness().last_controlled_probe_at is None
    finally:
        await stop_asr_scheduler(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_startup_probe_is_background_and_promotes_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_asr_scheduler_for_test()
    provider = StartupProbeSTT()
    scheduler = startup_probe_scheduler(provider)
    monkeypatch.setattr(stt_adapters, "_scheduler", scheduler)
    settings = Settings(asr_scheduler_enabled=True, asr_job_deadline_s=0.2)
    try:
        started_scheduler = await asyncio.wait_for(
            start_asr_scheduler(settings),
            timeout=0.05,
        )
        await asyncio.wait_for(provider.started.wait(), timeout=0.05)

        assert started_scheduler is scheduler
        assert scheduler.readiness().last_controlled_probe_at is None

        provider.release.set()
        await wait_for_probe(scheduler)
        readiness = scheduler.readiness().to_public(ttl_s=1.0)
        assert provider.calls == 1
        assert readiness.status == "ready"
        assert readiness.accepting is True
        assert readiness.reason_code == "asr_ready"
    finally:
        provider.release.set()
        await stop_asr_scheduler(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_startup_probe_is_degraded_without_blocking_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_asr_scheduler_for_test()
    provider = StartupProbeSTT(error=ASRProviderTransientError())
    provider.release.set()
    scheduler = startup_probe_scheduler(provider)
    monkeypatch.setattr(stt_adapters, "_scheduler", scheduler)
    settings = Settings(asr_scheduler_enabled=True, asr_job_deadline_s=0.2)
    try:
        await start_asr_scheduler(settings)
        await wait_for_probe(scheduler)
        readiness = scheduler.readiness().to_public(ttl_s=1.0)
        assert provider.calls == 1
        assert readiness.status == "degraded"
        assert readiness.accepting is True
        assert readiness.reason_code == "asr_controlled_probe_degraded"
    finally:
        await stop_asr_scheduler(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_controlled_probe_refreshes_before_ttl_and_stops_with_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_asr_scheduler_for_test()
    provider = StartupProbeSTT()
    provider.release.set()
    scheduler = startup_probe_scheduler(provider)
    monkeypatch.setattr(stt_adapters, "_scheduler", scheduler)
    settings = Settings(
        asr_scheduler_enabled=True,
        asr_job_deadline_s=0.2,
        asr_readiness_stale_after_s=0.04,
    )
    try:
        await start_asr_scheduler(settings)
        await wait_for_probe_calls(provider, 1)
        await wait_for_probe(scheduler)
        first_checked_at = scheduler.readiness().last_controlled_probe_at

        await wait_for_probe_calls(provider, 2)
        refreshed_at = first_checked_at
        for _ in range(100):
            refreshed_at = scheduler.readiness().last_controlled_probe_at
            if refreshed_at != first_checked_at:
                break
            await asyncio.sleep(0)
        assert first_checked_at is not None
        assert refreshed_at is not None
        assert refreshed_at > first_checked_at

        await stop_asr_scheduler(grace_period_s=0.2)
        calls_after_stop = provider.calls
        await asyncio.sleep(0.05)
        assert provider.calls == calls_after_stop
    finally:
        await stop_asr_scheduler(grace_period_s=0.2)
