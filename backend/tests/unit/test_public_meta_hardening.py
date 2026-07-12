from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.repo.migrator import run_migrations
from app.adapters.skill import SkillError, SkillExecutor
from app.api import deps as deps_mod
from app.config import Settings
from app.main import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response


def _enroll(client: TestClient, label: str) -> dict[str, object]:
    response = client.post(
        "/session/enroll",
        json={
            "enrollment_id": f"meta-{label}-" + "e" * 40,
            "device_secret": f"meta-{label}-" + "s" * 40,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def public_meta_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "public-meta.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        debug_token="host-admin",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    deps_mod.reset_deps_for_test()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client
    finally:
        deps_mod.reset_deps_for_test()


@pytest.fixture
def local_meta_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "local-meta.db",
        storage_dir=tmp_path / "storage-local",
        rag_index_dir=tmp_path / "rag-local",
        skill_executor_build_dir=tmp_path / "skills-local",
        public_demo_mode=False,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    deps_mod.reset_deps_for_test()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client
    finally:
        deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_public_docs_and_openapi_are_not_mounted(public_meta_client: TestClient) -> None:
    session = _enroll(public_meta_client, "docs")
    headers = {"Authorization": f"Bearer {session['token']}"}
    assert public_meta_client.get("/docs", headers=headers).status_code == 404
    assert public_meta_client.get("/redoc", headers=headers).status_code == 404
    assert public_meta_client.get("/openapi.json", headers=headers).status_code == 404


@pytest.mark.unit
def test_public_anonymous_meta_is_minimal(public_meta_client: TestClient) -> None:
    assert public_meta_client.get("/healthz").json() == {"status": "ok"}
    assert public_meta_client.get("/readyz").json() == {"status": "ready"}
    bootstrap = public_meta_client.get("/bootstrap").json()
    assert bootstrap["ws_path"] == "/ws/echo"
    assert bootstrap["session_required"] is True
    assert bootstrap["capabilities"]["ws_stream_epoch"] is True
    assert bootstrap["capabilities"]["ws_hello_bearer"] is True
    for sensitive in ("backend_version", "app_version", "ws_url", "http_url"):
        assert sensitive not in bootstrap


@pytest.mark.unit
def test_full_health_and_admin_require_host_admin(public_meta_client: TestClient) -> None:
    session = _enroll(public_meta_client, "admin")
    session_headers = {"Authorization": f"Bearer {session['token']}"}
    assert public_meta_client.get("/healthz/full", headers=session_headers).status_code == 403
    assert public_meta_client.get("/admin/data-dir", headers=session_headers).status_code == 403

    admin_headers = {"X-Echo-Admin-Token": "host-admin"}
    assert public_meta_client.get("/healthz/full", headers=admin_headers).status_code == 200
    assert public_meta_client.get("/admin/data-dir", headers=admin_headers).status_code == 200


@pytest.mark.unit
def test_public_server_errors_hide_internal_detail(public_meta_client: TestClient) -> None:
    async def fail_with_internal_detail() -> None:
        raise HTTPException(
            status_code=500,
            detail=(
                "failed at /Users/private/workspace; "
                "provider=https://llm.example/v1?api_key=secret-token"
            ),
        )

    public_meta_client.app.add_api_route(
        "/test-public-server-error",
        fail_with_internal_detail,
        methods=["GET"],
    )
    session = _enroll(public_meta_client, "server-error")
    response = public_meta_client.get(
        "/test-public-server-error",
        headers={"Authorization": f"Bearer {session['token']}"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "请求未能完成，请稍后重试",
        }
    }
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "/Users/private" not in response.text
    assert "secret-token" not in response.text
    assert "llm.example" not in response.text


@pytest.mark.unit
def test_public_validation_error_does_not_echo_secret_input(
    public_meta_client: TestClient,
) -> None:
    secret = "TOPSECRET_SENTINEL"
    response = public_meta_client.post(
        "/session/enroll",
        json={"enrollment_id": "e" * 40, "device_secret": secret},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "请求参数无效，请检查后重试",
        }
    }
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert secret not in response.text
    assert "input" not in response.text
    assert "ctx" not in response.text


@pytest.mark.unit
def test_local_validation_error_keeps_fastapi_diagnostics(
    local_meta_client: TestClient,
) -> None:
    secret = "TOPSECRET_SENTINEL"
    response = local_meta_client.post(
        "/session/enroll",
        json={"enrollment_id": "e" * 40, "device_secret": secret},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert secret in response.text
    assert "input" in response.text
    assert "ctx" in response.text


async def _fail_rag_ingest_with_private_detail(
    _self: BM25Rag,
    *_args: object,
    **_kwargs: object,
) -> str:
    raise RuntimeError("parser failed at /Users/private/workspace; api_key=TOPSECRET_SENTINEL")


async def _fail_artifact_with_private_detail(
    _self: SkillExecutor,
    **_kwargs: object,
) -> object:
    raise SkillError("generator failed at /Users/private/build; api_key=TOPSECRET_SENTINEL")


def _assert_private_detail_is_suppressed(response: Response) -> None:
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "request_failed",
            "message": "请求未能完成，请稍后重试",
        }
    }
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "/Users/private" not in response.text
    assert "TOPSECRET_SENTINEL" not in response.text


@pytest.mark.unit
def test_public_real_rag_and_artifact_routes_hide_workflow_internal_detail(
    public_meta_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BM25Rag, "ingest_file", _fail_rag_ingest_with_private_detail)
    monkeypatch.setattr(SkillExecutor, "generate", _fail_artifact_with_private_detail)
    session = _enroll(public_meta_client, "internal-routes")

    rag_response = public_meta_client.post(
        "/rag/ingest",
        headers={"Authorization": f"Bearer {session['token']}"},
        files={"file": ("private.txt", b"trigger parser", "text/plain")},
    )
    artifact_response = public_meta_client.post(
        "/artifacts/generate",
        headers={"X-Echo-Admin-Token": "host-admin"},
        json={"artifact_type": "pdf", "brief": "trigger generator"},
    )

    _assert_private_detail_is_suppressed(rag_response)
    _assert_private_detail_is_suppressed(artifact_response)


@pytest.mark.unit
def test_local_real_rag_and_artifact_routes_keep_internal_diagnostics(
    local_meta_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BM25Rag, "ingest_file", _fail_rag_ingest_with_private_detail)
    monkeypatch.setattr(SkillExecutor, "generate", _fail_artifact_with_private_detail)

    responses = (
        local_meta_client.post(
            "/rag/ingest",
            files={"file": ("private.txt", b"trigger parser", "text/plain")},
        ),
        local_meta_client.post(
            "/artifacts/generate",
            json={"artifact_type": "pdf", "brief": "trigger generator"},
        ),
    )

    for response in responses:
        assert response.status_code == 400
        assert "/Users/private" in response.text
        assert "TOPSECRET_SENTINEL" in response.text
        assert "detail" in response.json()
