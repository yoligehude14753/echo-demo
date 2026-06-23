"""Public demo mode must not expose local-admin endpoints."""

from __future__ import annotations

import pytest
from app.api import deps as deps_mod
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: Settings(
        public_demo_mode=True,
        debug_token="admin-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    return TestClient(app)


@pytest.mark.unit
def test_public_demo_blocks_admin_without_token(client: TestClient) -> None:
    r = client.get("/admin/data-dir")
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


@pytest.mark.unit
def test_public_demo_blocks_diagnostics_without_token(client: TestClient) -> None:
    r = client.get("/admin/diagnostics/export")
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


@pytest.mark.unit
def test_public_demo_allows_admin_with_bearer_token(client: TestClient) -> None:
    r = client.get(
        "/admin/settings/remote",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.unit
def test_public_demo_allows_admin_with_echo_admin_token(client: TestClient) -> None:
    r = client.get(
        "/admin/settings/remote",
        headers={"X-Echo-Admin-Token": "admin-secret"},
    )
    assert r.status_code == 200, r.text
