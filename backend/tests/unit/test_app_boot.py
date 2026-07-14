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
    assert body["ws_url"].startswith(("ws://", "wss://"))
    assert body["http_url"].startswith(("http://", "https://"))
    assert "app_version" in body
    assert body["stt_enabled"] is True
    assert body["schema_version"] == 1
    assert body["api_version"] == "0.3"
    assert body["session_required"] is False
    assert "minimum_client_version" not in body
    assert body["capabilities"]["principal_sessions"] is True
    assert body["capabilities"]["owner_isolation"] is True
    assert body["capabilities"]["workflow_kernel"] == "dispatcher-v1"
    assert body["capabilities"]["ws_owner_filtering"] is True
    assert body["capabilities"]["ws_stream_epoch"] is True
    assert body["capabilities"]["ws_hello_bearer"] is False
    assert body["capabilities"]["server_resync_rehydrate_required"] is True
    assert body["capabilities"]["host_runtime_requires_admin"] is False
    readiness = body["capabilities"]["transcription_readiness"]
    assert readiness["schema_version"] == 1
    assert set(readiness) == {
        "schema_version",
        "status",
        "accepting",
        "checked_at",
        "ttl_s",
        "reason_code",
        "retry_after_s",
    }


@pytest.mark.unit
def test_settings_singleton_idempotent() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
