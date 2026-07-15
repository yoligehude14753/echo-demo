"""ASR-owned factory tests; existing meeting/capture callsites stay untouched."""

from __future__ import annotations

import pytest
from app.adapters.stt import build_asr_scheduler
from app.config import Settings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_construct_or_start_local_worker() -> None:
    settings = Settings(
        asr_scheduler_enabled=False,
        asr_local_enabled=True,
        asr_local_model_path="/models/unit",
        asr_eligible_providers=("local",),
        asr_provider_weights={"local": 1.0},
        asr_provider_concurrency={"local": 1},
    )
    scheduler = build_asr_scheduler(settings)
    try:
        await scheduler.start()
        assert scheduler.capability_transports() == {}
        assert scheduler.readiness().worker_count == 0
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse_one_shot", "websocket_stream"])
async def test_stepfun_factory_preserves_explicit_capability_transport(transport: str) -> None:
    settings = Settings(
        asr_scheduler_enabled=True,
        asr_stepfun_enabled=True,
        asr_stepfun_transport=transport,
        asr_stepfun_api_key="fixture-only",
        asr_eligible_providers=("stepfun",),
        asr_provider_weights={"stepfun": 1.0},
        asr_provider_concurrency={"stepfun": 1},
    )
    scheduler = build_asr_scheduler(settings)
    try:
        assert scheduler.capability_transports() == {"stepfun": transport}
        await scheduler.start()
        assert scheduler.readiness().worker_count == settings.asr_scheduler_max_concurrency
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_factory_is_single_worker_and_only_when_enabled_and_eligible() -> None:
    settings = Settings(
        asr_scheduler_enabled=True,
        asr_local_enabled=True,
        asr_local_model_path="/models/unit",
        asr_eligible_providers=("local",),
        asr_provider_weights={"local": 1.0},
        asr_provider_concurrency={"local": 1},
    )
    scheduler = build_asr_scheduler(settings)
    try:
        assert scheduler.capability_transports() == {"local": "local_worker"}
    finally:
        await scheduler.close(grace_period_s=0.2)
