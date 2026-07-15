"""Deterministic ASR scheduler load evidence with a fake STT port."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.api.asr import get_asr_readiness
from app.config import Settings
from app.schemas.meeting import TranscriptSegment

VALID_AUDIO = b"\x01\x00" * 80


class LoadProvider:
    def __init__(self, delay_s: float = 0.002) -> None:
        self.delay_s = delay_s
        self.calls = 0
        self.active = 0
        self.max_active = 0

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
            await asyncio.sleep(self.delay_s)
            return [TranscriptSegment(text="ok", start_ms=0, end_ms=100)]
        finally:
            self.active -= 1


def scheduler(provider: LoadProvider) -> ASRScheduler:
    return ASRScheduler(
        {"fake": ASRProviderBinding(name="fake", adapter=provider, max_concurrency=4)},
        ASRSchedulerConfig(
            enabled=True,
            eligible_providers=("fake",),
            max_concurrency=4,
            queue_size=100,
            job_deadline_s=2.0,
            max_attempts=1,
            scope_max_concurrency=20,
            scope_rate_limit_per_minute=1_000,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_tenant_load_reports_raw_latency_percentiles_and_health_latency() -> None:
    provider = LoadProvider()
    asr = scheduler(provider)
    settings = Settings(
        asr_scheduler_enabled=True,
        asr_eligible_providers=("firered",),
        asr_provider_weights={"firered": 1.0},
        asr_provider_concurrency={"firered": 1},
    )
    try:

        async def one(index: int) -> float:
            started = time.perf_counter()
            await asr.transcribe(
                VALID_AUDIO,
                context=ASRRequestContext(
                    request_id=f"load-{index}",
                    idempotency_key=f"load-{index}",
                    tenant_id=f"tenant-{index % 10}",
                    principal_id=f"principal-{index % 10}",
                ),
            )
            return (time.perf_counter() - started) * 1000

        tasks = [asyncio.create_task(one(index)) for index in range(100)]
        await asyncio.sleep(0)
        readiness_samples: list[float] = []
        for _ in range(20):
            started = time.perf_counter()
            response = await get_asr_readiness(settings=settings, scheduler=asr)
            readiness_samples.append((time.perf_counter() - started) * 1000)
            assert response.status in {"unknown", "unavailable", "ready", "degraded"}
            assert response.accepting is False or response.status in {"ready", "degraded"}
        samples = await asyncio.gather(*tasks)
        report = asr.latency_report(samples)
        readiness_report = asr.latency_report(readiness_samples)
        print(
            "ASR_LOAD_RAW "
            + json.dumps(
                {
                    "sample_count": report["sample_count"],
                    "p50_ms": report["p50_ms"],
                    "p95_ms": report["p95_ms"],
                    "p99_ms": report["p99_ms"],
                    "provider_calls": provider.calls,
                    "provider_max_active": provider.max_active,
                    "tenant_count": 10,
                },
                sort_keys=True,
            )
        )
        print(
            "ASR_READINESS_RAW " + json.dumps(readiness_report, sort_keys=True),
        )
        assert report["sample_count"] == 100
        assert provider.calls == 100
        assert provider.max_active <= 4
        assert report["p50_ms"] <= report["p95_ms"] <= report["p99_ms"]
        assert readiness_report["sample_count"] == 20
        assert (
            readiness_report["p50_ms"] <= readiness_report["p95_ms"] <= readiness_report["p99_ms"]
        )
    finally:
        await asr.close(grace_period_s=0.5)
