from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import require_admin_access
from app.api.hub import router
from app.hub.runtime import HubRuntime


def _app(tmp_path, *, enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin_access] = lambda: None
    app.state.hub_runtime = HubRuntime(
        SimpleNamespace(
            hub_enabled=enabled,
            hub_base_url="http://hub.test" if enabled else "",
            hub_state_file=tmp_path / "hub_state.json",
            hub_request_timeout_s=2.0,
            hub_sync_interval_s=60.0,
        )
    )
    app.state.hub_runtime.state.sync_token = "sync-secret"
    return app


def test_hub_status_is_host_admin_route_and_redacts_token(tmp_path):
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/hub/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["device_id"]
    assert payload["paired"] is True
    assert "sync_token" not in response.text


def test_hub_pairing_failure_is_generic(tmp_path):
    app = _app(tmp_path, enabled=False)

    with TestClient(app) as client:
        response = client.post("/hub/pairings")

    assert response.status_code == 503
    assert response.json()["detail"] == "配对失败，请重试"
    assert "hub.test" not in response.text
