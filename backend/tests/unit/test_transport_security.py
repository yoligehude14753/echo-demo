from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import OFFICIAL_ELECTRON_ORIGIN, Settings, get_settings
from app.main import create_app
from app.security.access import (
    AccessPolicy,
    AccessPolicyError,
    PreAuthAdmission,
    PreAuthAdmissionError,
)
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    PUBLIC_CLIENT_VERSION_HEADER,
)
from app.security.paths import route_scope_path
from app.security.sessions import SessionStore
from app.upload.ingress import UploadIngressMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SUPPORTED_CLIENT_HEADERS = {
    PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION,
}


class _SlowJsonBody(httpx.AsyncByteStream):
    def __init__(
        self,
        payload: bytes,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._payload = payload
        self._entered = entered
        self._release = release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        midpoint = max(1, len(self._payload) // 2)
        self._entered.set()
        yield self._payload[:midpoint]
        await self._release.wait()
        yield self._payload[midpoint:]


def _enrollment_body(label: str) -> bytes:
    return json.dumps(
        {
            "enrollment_id": f"{label}-enrollment-" + "e" * 40,
            "device_secret": f"{label}-device-" + "s" * 40,
        }
    ).encode()


@pytest.mark.unit
def test_policy_path_uses_scope_and_matches_router_root_path() -> None:
    poisoned_host_scope = {
        "path": "/meetings",
        "root_path": "",
        "headers": [(b"host", b"echodesk.example/healthz?mask=")],
    }
    assert route_scope_path(poisoned_host_scope) == "/meetings"
    assert (
        route_scope_path(
            {
                "path": "/echo/healthz/full",
                "root_path": "/echo",
                "headers": [(b"host", b"echodesk.example")],
            }
        )
        == "/healthz/full"
    )
    assert route_scope_path({"path": "/echoes/healthz/full", "root_path": "/echo"}) == (
        "/echoes/healthz/full"
    )


@pytest.mark.unit
def test_cors_wraps_identity_policy_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "http://localhost:5173"
    settings = Settings(
        db_path=tmp_path / "cors.db",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    response = TestClient(app, headers=SUPPORTED_CLIENT_HEADERS).get(
        "/meetings", headers={"Origin": origin}
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == origin

    upgrade = TestClient(app).get("/meetings", headers={"Origin": origin})
    assert upgrade.status_code == 426
    assert upgrade.headers["x-echodesk-minimum-client-version"] == MINIMUM_PUBLIC_CLIENT_VERSION
    exposed = upgrade.headers["access-control-expose-headers"].lower()
    assert "x-echodesk-minimum-client-version" in exposed


@pytest.mark.unit
def test_public_remote_peer_bypasses_lan_share_gate_but_not_session_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "public-remote-peer.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        lan_full_api_enabled=False,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(
        app,
        headers=SUPPORTED_CLIENT_HEADERS,
        client=("203.0.113.42", 43100),
    ) as client:
        unauthenticated = client.get("/meetings")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "session_required"

        enrolled = client.post(
            "/session/enroll",
            json={
                "enrollment_id": "remote-proxy-enrollment-" + "e" * 40,
                "device_secret": "remote-proxy-device-" + "s" * 40,
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        authorized = client.get(
            "/meetings",
            headers={"Authorization": f"Bearer {enrolled.json()['token']}"},
        )
        assert authorized.status_code == 200, authorized.text
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trusted_host_rejects_before_reading_upload_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "host.db",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    received = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        received = True
        raise AssertionError("invalid Host must be rejected before request body IO")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "server": ("testserver", 80),
            "client": ("192.168.50.20", 50000),
            "root_path": "",
            "path": "/rag/ingest",
            "raw_path": b"/rag/ingest",
            "query_string": b"",
            "headers": [
                (b"host", b"echodesk.yoliyoli.uk/healthz?mask="),
                (b"content-length", b"999999999"),
            ],
            "state": {},
        },
        receive,
        send,
    )

    assert received is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upload_guard_uses_router_path_under_root_path() -> None:
    settings = Settings(
        upload_max_file_mb=0.000001,
        upload_multipart_overhead_bytes=64 * 1024,
        public_demo_mode=True,
        _env_file=None,  # type: ignore[call-arg]
    )
    called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal called
        called = True

    middleware = UploadIngressMiddleware(downstream, settings=settings)  # type: ignore[arg-type]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("oversized body must not be read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(  # type: ignore[arg-type]
        {
            "type": "http",
            "method": "POST",
            "path": "/echo/rag/ingest",
            "root_path": "/echo",
            "headers": [(b"content-length", b"70000")],
        },
        receive,
        send,
    )
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remote_lan_websocket_is_rejected_before_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "lan-ws.db",
        public_demo_mode=False,
        lan_full_api_enabled=False,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    received = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        received = True
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await asyncio.wait_for(
        app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "scheme": "ws",
                "server": ("testserver", 80),
                "client": ("192.168.50.20", 50000),
                "root_path": "",
                "path": "/ws/echo",
                "raw_path": b"/ws/echo",
                "query_string": b"",
                "headers": [(b"host", b"lan-device.local")],
                "subprotocols": [],
                "state": {},
            },
            receive,
            send,
        ),
        timeout=1,
    )

    assert received is False
    assert sent == [
        {
            "type": "websocket.close",
            "code": 4403,
            "reason": "LAN websocket access disabled",
        }
    ]


@pytest.mark.unit
def test_non_public_sync_gateway_uses_claimed_device_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "sync-principal.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=False,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = asyncio.run(run_migrations(settings.db_path))
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        pairing = client.post("/hub/v1/pairings")
        assert pairing.status_code == 201, pairing.text
        claim = client.post(
            "/hub/v1/pairings/claim",
            json={
                "pairing_code": pairing.json()["pairing_code"],
                "device_id": "non-public-paired-device",
                "device_name": "Paired device",
                "platform": "test",
            },
        )
        assert claim.status_code == 200, claim.text
        sync_headers = {"X-Echo-Sync-Token": claim.json()["sync_token"]}

        devices = client.get("/hub/v1/devices", headers=sync_headers)
        assert devices.status_code == 200, devices.text
        assert [item["device_id"] for item in devices.json()] == [
            "non-public-paired-device"
        ]

        push = client.post(
            "/hub/v1/sync/push",
            headers=sync_headers,
            json={
                "operation_id": "sync:non-public:1",
                "device_id": "non-public-paired-device",
                "entity_type": "memory",
                "entity_id": "memory-1",
                "base_revision": 0,
                "updated_at": "2026-07-14T10:00:00Z",
                "payload": {"content": "paired identity"},
            },
        )
        assert push.status_code == 200, push.text

        invalid = client.get(
            "/hub/v1/sync/changes",
            headers={"X-Echo-Sync-Token": "sync_invalid"},
        )
        assert invalid.status_code == 401, invalid.text

    local = AccessPolicy(settings, SessionStore(settings.db_path))
    local_principal = asyncio.run(
        local.resolve_http_principal(
            method="GET",
            path="/meetings",
            client_host="127.0.0.1",
        )
    )
    assert local_principal.device_id == "legacy-local"
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_explicit_http_origin_requires_allowlist_while_missing_origin_remains_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_origin = "http://localhost:5173"
    settings = Settings(
        db_path=tmp_path / "origin-http.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=False,
        allowed_origins=allowed_origin,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    calls = 0

    @app.post("/transport-origin-probe")
    async def transport_origin_probe() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    with TestClient(app, headers=SUPPORTED_CLIENT_HEADERS) as client:
        for origin in ("https://evil.example", "", "null", f"{allowed_origin},https://evil"):
            denied = client.post("/transport-origin-probe", headers={"Origin": origin})
            assert denied.status_code == 403
            assert denied.json()["detail"] == "origin not allowed"
        assert calls == 0

        allowed = client.post(
            "/transport-origin-probe",
            headers={"Origin": allowed_origin},
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == allowed_origin

        no_origin = client.post("/transport-origin-probe")
        assert no_origin.status_code == 200
        assert calls == 2
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_public_allowed_origin_still_receives_cors_and_can_enroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "https://app.example.test"
    settings = Settings(
        db_path=tmp_path / "origin-public.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        allowed_origins=origin,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app, headers=SUPPORTED_CLIENT_HEADERS) as client:
        response = client.post(
            "/session/enroll",
            headers={"Origin": origin},
            json={
                "enrollment_id": "origin-enrollment-" + "e" * 40,
                "device_secret": "origin-device-" + "s" * 40,
            },
        )
        assert response.status_code == 201
        assert response.headers["access-control-allow-origin"] == origin
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_official_electron_origin_keeps_public_session_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "electron-origin-public.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        allowed_origins="https://browser.example.test",
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    origin_headers = {"Origin": OFFICIAL_ELECTRON_ORIGIN}

    with TestClient(app, headers=SUPPORTED_CLIENT_HEADERS) as client:
        unauthenticated_http = client.get("/meetings", headers=origin_headers)
        assert unauthenticated_http.status_code == 401
        assert (
            unauthenticated_http.headers["access-control-allow-origin"] == OFFICIAL_ELECTRON_ORIGIN
        )

        with client.websocket_connect("/ws/echo", headers=origin_headers) as websocket:
            websocket.send_json({"type": "client_hello", "last_seq": 0, "client_version": "0.3.2"})
            with pytest.raises(WebSocketDisconnect) as unauthenticated_ws:
                websocket.receive_json()
        assert unauthenticated_ws.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect) as forged_origin,
            client.websocket_connect(
                "/ws/echo",
                headers={"Origin": "echodesk://app.evil"},
            ),
        ):
            pass
        assert forged_origin.value.code == 4403

        enrolled = client.post(
            "/session/enroll",
            headers=origin_headers,
            json={
                "enrollment_id": "electron-origin-enrollment-" + "e" * 40,
                "device_secret": "electron-origin-device-" + "s" * 40,
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        assert enrolled.headers["access-control-allow-origin"] == OFFICIAL_ELECTRON_ORIGIN

        with client.websocket_connect("/ws/echo", headers=origin_headers) as websocket:
            websocket.send_json(
                {
                    "type": "client_hello",
                    "last_seq": 0,
                    "client_version": "0.3.2",
                    "auth": {"type": "bearer", "token": enrolled.json()["token"]},
                }
            )
            assert websocket.receive_json()["type"] == "server_hello"
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preauth_admission_bounds_global_and_peer_concurrency_and_rate() -> None:
    concurrency_gate = PreAuthAdmission(
        channel="http",
        global_concurrent=2,
        peer_concurrent=1,
        global_attempts=20,
        peer_attempts=10,
        window_s=60,
        max_peers=4,
    )
    peer_a = await concurrency_gate.acquire("peer-a")
    with pytest.raises(PreAuthAdmissionError) as peer_capacity:
        await concurrency_gate.acquire("peer-a")
    assert peer_capacity.value.reason == "capacity"

    peer_b = await concurrency_gate.acquire("peer-b")
    with pytest.raises(PreAuthAdmissionError) as global_capacity:
        await concurrency_gate.acquire("peer-c")
    assert global_capacity.value.reason == "capacity"
    await peer_a.release()
    await peer_b.release()

    now = [0.0]
    rate_gate = PreAuthAdmission(
        channel="websocket",
        global_concurrent=2,
        peer_concurrent=2,
        global_attempts=3,
        peer_attempts=2,
        window_s=60,
        max_peers=4,
        clock=lambda: now[0],
    )
    for _ in range(2):
        lease = await rate_gate.acquire("peer-a")
        await lease.release()
    with pytest.raises(PreAuthAdmissionError) as peer_rate:
        await rate_gate.acquire("peer-a")
    assert peer_rate.value.reason == "rate limit"

    peer_b = await rate_gate.acquire("peer-b")
    await peer_b.release()
    with pytest.raises(PreAuthAdmissionError) as global_rate:
        await rate_gate.acquire("peer-c")
    assert global_rate.value.reason == "rate limit"

    now[0] = 61.0
    recovered = await rate_gate.acquire("peer-a")
    await recovered.release()


@pytest.mark.unit
def test_session_body_admission_is_limited_to_credential_post_routes() -> None:
    for path in (
        "/session",
        "/session/enroll",
        "/session/renew",
        "/session/credential/rotate",
    ):
        assert AccessPolicy.is_session_body_admission_route("POST", path) is True

    for method, path in (
        ("GET", "/session/enroll"),
        ("POST", "/session/claim"),
        ("POST", "/meetings"),
    ):
        assert AccessPolicy.is_session_body_admission_route(method, path) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_body_peer_cap_reserves_global_slot_after_pool_is_reduced(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "session-body-reduced-global.db",
        public_demo_mode=True,
        preauth_http_global_concurrent=2,
        preauth_http_peer_concurrent=4,
        preauth_http_global_requests_per_window=100,
        preauth_http_peer_requests_per_window=100,
        upload_global_concurrent_requests=4,
        _env_file=None,  # type: ignore[call-arg]
    )
    policy = AccessPolicy(settings, SessionStore(settings.db_path))

    first_peer_lease = await policy.admit_session_body(
        method="POST",
        path="/session/enroll",
        client_key="peer-a",
    )
    assert first_peer_lease is not None
    with pytest.raises(PreAuthAdmissionError) as same_peer:
        await policy.admit_session_body(
            method="POST",
            path="/session/enroll",
            client_key="peer-a",
        )
    assert same_peer.value.reason == "capacity"

    other_peer_lease = await policy.admit_session_body(
        method="POST",
        path="/session/enroll",
        client_key="peer-b",
    )
    assert other_peer_lease is not None
    await first_peer_lease.release()
    await other_peer_lease.release()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("session_path", ["/session/enroll", "/session"])
async def test_anonymous_session_slow_body_is_fair_per_peer_without_blocking_authenticated_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_path: str,
) -> None:
    settings = Settings(
        db_path=tmp_path / "session-body-fairness.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        preauth_http_global_concurrent=4,
        preauth_http_peer_concurrent=1,
        preauth_http_global_requests_per_window=100,
        preauth_http_peer_requests_per_window=100,
        upload_global_concurrent_requests=2,
        request_body_timeout_s=2,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = await run_migrations(settings.db_path)
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/authenticated-peer-probe")
    async def authenticated_peer_probe() -> dict[str, bool]:
        return {"ok": True}

    same_peer_transport = httpx.ASGITransport(
        app=app,
        client=("203.0.113.7", 50000),
    )
    other_peer_transport = httpx.ASGITransport(
        app=app,
        client=("203.0.113.8", 50001),
    )
    request_headers = {
        **SUPPORTED_CLIENT_HEADERS,
        "Content-Type": "application/json",
    }
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    other_entered = asyncio.Event()
    other_release = asyncio.Event()

    try:
        async with (
            httpx.AsyncClient(
                transport=same_peer_transport,
                base_url="http://testserver",
                headers=SUPPORTED_CLIENT_HEADERS,
            ) as same_peer,
            httpx.AsyncClient(
                transport=other_peer_transport,
                base_url="http://testserver",
                headers=SUPPORTED_CLIENT_HEADERS,
            ) as other_peer,
        ):
            enrolled = await same_peer.post(
                "/session/enroll",
                content=_enrollment_body("authenticated-probe"),
                headers=request_headers,
            )
            assert enrolled.status_code == 201, enrolled.text
            bearer = enrolled.json()["token"]

            first_task = asyncio.create_task(
                same_peer.post(
                    session_path,
                    content=_SlowJsonBody(
                        _enrollment_body("slow-first"),
                        entered=first_entered,
                        release=first_release,
                    ),
                    headers=request_headers,
                )
            )
            await asyncio.wait_for(first_entered.wait(), timeout=1)

            same_peer_second = await same_peer.post(
                session_path,
                content=_enrollment_body("same-peer-second"),
                headers=request_headers,
            )

            other_task = asyncio.create_task(
                other_peer.post(
                    session_path,
                    content=_SlowJsonBody(
                        _enrollment_body("slow-other"),
                        entered=other_entered,
                        release=other_release,
                    ),
                    headers=request_headers,
                )
            )
            await asyncio.wait_for(other_entered.wait(), timeout=1)

            authenticated = await same_peer.get(
                "/authenticated-peer-probe",
                headers={"Authorization": f"Bearer {bearer}"},
            )

            first_release.set()
            other_release.set()
            first_response, other_response = await asyncio.gather(first_task, other_task)

        assert same_peer_second.status_code == 429, same_peer_second.text
        assert same_peer_second.json()["detail"] == ("pre-auth session body capacity exceeded")
        assert authenticated.status_code == 200, authenticated.text
        assert first_response.status_code == 201, first_response.text
        assert other_response.status_code == 201, other_response.text
    finally:
        first_release.set()
        other_release.set()
        deps_mod.reset_deps_for_test()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_anonymous_session_body_releases_peer_fairness_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "session-body-cancel.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        preauth_http_global_concurrent=2,
        preauth_http_peer_concurrent=1,
        preauth_http_global_requests_per_window=100,
        preauth_http_peer_requests_per_window=100,
        upload_global_concurrent_requests=2,
        request_body_timeout_s=2,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = await run_migrations(settings.db_path)
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50002))
    request_headers = {
        **SUPPORTED_CLIENT_HEADERS,
        "Content-Type": "application/json",
    }
    entered = asyncio.Event()
    release = asyncio.Event()

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=SUPPORTED_CLIENT_HEADERS,
        ) as client:
            blocked_task = asyncio.create_task(
                client.post(
                    "/session/enroll",
                    content=_SlowJsonBody(
                        _enrollment_body("cancelled"),
                        entered=entered,
                        release=release,
                    ),
                    headers=request_headers,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            blocked_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked_task

            recovered = await client.post(
                "/session/enroll",
                content=_enrollment_body("after-cancel"),
                headers=request_headers,
            )

        assert recovered.status_code == 201, recovered.text
    finally:
        release.set()
        deps_mod.reset_deps_for_test()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_after_session_body_parse_releases_peer_fairness_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "session-body-post-parse-cancel.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        preauth_http_global_concurrent=2,
        preauth_http_peer_concurrent=1,
        preauth_http_global_requests_per_window=100,
        preauth_http_peer_requests_per_window=100,
        upload_global_concurrent_requests=2,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = await run_migrations(settings.db_path)
    assert result.errors == []
    original_enroll = AccessPolicy.enroll_public_device
    handler_entered = asyncio.Event()
    keep_handler_open = asyncio.Event()

    async def stalled_enroll(
        _policy: AccessPolicy,
        **_kwargs: object,
    ) -> object:
        # Reaching the endpoint proves Starlette already consumed and parsed
        # the complete JSON body.  Cancellation here exercises the ownership
        # window after body parsing but before any response exists.
        handler_entered.set()
        await keep_handler_open.wait()
        raise AssertionError("cancelled enrollment handler resumed unexpectedly")

    monkeypatch.setattr(AccessPolicy, "enroll_public_device", stalled_enroll)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50003))
    request_headers = {
        **SUPPORTED_CLIENT_HEADERS,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=SUPPORTED_CLIENT_HEADERS,
        ) as client:
            cancelled = asyncio.create_task(
                client.post(
                    "/session",
                    content=_enrollment_body("post-parse-cancelled"),
                    headers=request_headers,
                )
            )
            await asyncio.wait_for(handler_entered.wait(), timeout=1)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

            monkeypatch.setattr(
                AccessPolicy,
                "enroll_public_device",
                original_enroll,
            )
            recovered = await client.post(
                "/session",
                content=_enrollment_body("post-parse-recovered"),
                headers=request_headers,
            )

        assert recovered.status_code == 201, recovered.text
    finally:
        keep_handler_open.set()
        deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_forged_bearer_failures_are_limited_before_principal_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "preauth-http.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        preauth_http_global_requests_per_window=10,
        preauth_http_peer_requests_per_window=2,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    forged = {"Authorization": "Bearer forged-access-token"}

    with TestClient(app, headers=SUPPORTED_CLIENT_HEADERS) as client:
        assert client.get("/meetings", headers=forged).status_code == 401
        assert client.get("/meetings", headers=forged).status_code == 401
        limited = client.get("/meetings", headers=forged)
        assert limited.status_code == 429
        assert limited.json()["detail"] == "pre-auth http rate limit exceeded"
        assert int(limited.headers["Retry-After"]) >= 1
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [b"https://evil.example", b"", b"null", b"file://", b"file:///tmp/untrusted.html"],
)
async def test_local_websocket_rejects_untrusted_origin_before_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: bytes,
) -> None:
    settings = Settings(
        db_path=tmp_path / "origin-ws.db",
        public_demo_mode=False,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    received = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        received = True
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await asyncio.wait_for(
        app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "scheme": "ws",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 50000),
                "root_path": "",
                "path": "/ws/echo",
                "raw_path": b"/ws/echo",
                "query_string": b"",
                "headers": [(b"host", b"testserver"), (b"origin", origin)],
                "subprotocols": [],
                "state": {},
            },
            receive,
            send,
        ),
        timeout=1,
    )

    assert received is False
    assert sent == [
        {
            "type": "websocket.close",
            "code": 4403,
            "reason": "origin not allowed",
        }
    ]


@pytest.mark.unit
def test_packaged_electron_file_origin_is_loopback_local_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = Settings(
        db_path=tmp_path / "electron-file-local.db",
        storage_dir=tmp_path / "local-storage",
        rag_index_dir=tmp_path / "local-rag",
        skill_executor_build_dir=tmp_path / "local-skills",
        public_demo_mode=False,
        electron_file_origin_enabled=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: local_settings)
    deps_mod.reset_deps_for_test()
    local_app = create_app()
    local_app.dependency_overrides[get_settings] = lambda: local_settings
    with (
        TestClient(local_app) as client,
        client.websocket_connect(
            "/ws/echo",
            headers={"Origin": "file://"},
        ) as websocket,
    ):
        websocket.send_json({"type": "client_hello", "last_seq": 0})
        assert websocket.receive_json()["type"] == "server_hello"

    policy = AccessPolicy(local_settings, SessionStore(local_settings.db_path))
    policy.require_allowed_origin(["file://"], client_host="127.0.0.1")
    with pytest.raises(AccessPolicyError, match="origin not allowed"):
        policy.require_allowed_origin(["file://"], client_host="192.168.50.20")

    public_settings = local_settings.model_copy(
        update={
            "db_path": tmp_path / "electron-file-public.db",
            "public_demo_mode": True,
        }
    )
    monkeypatch.setattr("app.main.get_settings", lambda: public_settings)
    deps_mod.reset_deps_for_test()
    public_app = create_app()
    public_app.dependency_overrides[get_settings] = lambda: public_settings
    with (
        TestClient(public_app) as client,
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect(
            "/ws/echo",
            headers={"Origin": "file://"},
        ),
    ):
        pass
    assert rejected.value.code == 4403
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_allowed_websocket_origin_connects_and_public_failures_are_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_origin = "http://localhost:5173"
    local_settings = Settings(
        db_path=tmp_path / "allowed-ws.db",
        storage_dir=tmp_path / "local-storage",
        rag_index_dir=tmp_path / "local-rag",
        skill_executor_build_dir=tmp_path / "local-skills",
        public_demo_mode=False,
        allowed_origins=allowed_origin,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: local_settings)
    deps_mod.reset_deps_for_test()
    local_app = create_app()
    local_app.dependency_overrides[get_settings] = lambda: local_settings
    with (
        TestClient(local_app) as client,
        client.websocket_connect(
            "/ws/echo",
            headers={"Origin": allowed_origin},
        ) as websocket,
    ):
        websocket.send_json({"type": "client_hello", "last_seq": 0})
        assert websocket.receive_json()["type"] == "server_hello"

    public_settings = Settings(
        db_path=tmp_path / "limited-ws.db",
        storage_dir=tmp_path / "public-storage",
        rag_index_dir=tmp_path / "public-rag",
        skill_executor_build_dir=tmp_path / "public-skills",
        public_demo_mode=True,
        preauth_ws_global_attempts_per_window=10,
        preauth_ws_peer_attempts_per_window=1,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    monkeypatch.setattr("app.main.get_settings", lambda: public_settings)
    deps_mod.reset_deps_for_test()
    public_app = create_app()
    public_app.dependency_overrides[get_settings] = lambda: public_settings
    with TestClient(public_app) as client:
        with client.websocket_connect("/ws/echo") as websocket:
            websocket.send_json({"type": "client_hello", "last_seq": 0, "client_version": "0.3.2"})
            with pytest.raises(WebSocketDisconnect) as unauthorized:
                websocket.receive_json()
        assert unauthorized.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect) as limited,
            client.websocket_connect("/ws/echo"),
        ):
            pass
        assert limited.value.code == 4429
    deps_mod.reset_deps_for_test()
