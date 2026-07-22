from __future__ import annotations

from types import SimpleNamespace

from app.api.capture import router
from app.hub.runtime import HubRuntime
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(tmp_path, *, enabled: bool, base_url: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.hub_runtime = HubRuntime(
        SimpleNamespace(
            hub_enabled=enabled,
            hub_base_url=base_url,
            hub_state_file=tmp_path / "hub_state.json",
            hub_request_timeout_s=2.0,
            hub_sync_interval_s=60.0,
        )
    )
    return app


def test_capture_devices_returns_empty_when_hub_is_disabled(tmp_path) -> None:
    with TestClient(_app(tmp_path, enabled=False, base_url="")) as client:
        response = client.get("/capture/devices")

    assert response.status_code == 200
    assert response.json() == {"devices": []}


def test_capture_devices_exposes_unavailable_hub_as_service_error(tmp_path) -> None:
    with TestClient(_app(tmp_path, enabled=True, base_url="http://hub.test")) as client:
        response = client.get("/capture/devices")

    assert response.status_code == 503
    assert response.json() == {"detail": "设备列表暂不可用"}
