"""最小可运行性单测：FastAPI 启动 + /healthz + /bootstrap。"""

from __future__ import annotations

import pytest
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.unit
def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.unit
def test_bootstrap_payload(client: TestClient) -> None:
    r = client.get("/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert body["ws_url"].startswith("ws://")
    assert body["http_url"].startswith("http://")
    assert "app_version" in body
    assert body["stt_enabled"] is True


@pytest.mark.unit
def test_settings_singleton_idempotent() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
