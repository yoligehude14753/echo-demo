"""ASR-owned public readiness projection tests."""

from __future__ import annotations

import pytest
from app.adapters.stt import (
    build_asr_scheduler,
    reset_asr_scheduler_for_test,
    start_asr_scheduler,
    stop_asr_scheduler,
)
from app.api.asr import get_asr_readiness, router
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


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
    finally:
        await stop_asr_scheduler(grace_period_s=0.2)
