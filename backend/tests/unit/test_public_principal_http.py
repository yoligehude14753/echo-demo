from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def public_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "public.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        debug_token="test-admin",
        quota_storage_bytes=32 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    result = asyncio.run(run_migrations(settings.db_path))
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    deps_mod.reset_deps_for_test()
    with TestClient(app) as client:
        yield client


def _enrollment_payload(label: str) -> dict[str, str]:
    return {
        "enrollment_id": f"enrollment-{label}-" + "e" * 40,
        "device_secret": f"device-{label}-" + "s" * 40,
    }


def _issue(client: TestClient, label: str) -> tuple[dict[str, object], str]:
    payload = _enrollment_payload(label)
    response = client.post("/session", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload["device_secret"]


@pytest.mark.unit
def test_public_routes_require_server_issued_session(public_client: TestClient) -> None:
    assert public_client.get("/healthz").status_code == 200
    assert public_client.get("/meetings").status_code == 401

    assert public_client.post("/session").status_code == 422
    payload = _enrollment_payload("retry")
    first = public_client.post("/session", json=payload)
    second = public_client.post("/session", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["token"] != second.json()["token"]
    assert first.json()["principal"]["owner_id"] == second.json()["principal"]["owner_id"]
    conflict_payload = {**payload, "device_secret": "conflict-" + "x" * 40}
    assert public_client.post("/session", json=conflict_payload).status_code == 409

    authorized = public_client.get(
        "/meetings",
        headers={"Authorization": f"Bearer {second.json()['token']}"},
    )
    assert authorized.status_code == 200


@pytest.mark.unit
def test_public_json_body_limit_runs_before_auth_and_validation(
    public_client: TestClient,
) -> None:
    payload = b'{"question":"' + (b"x" * (2 * 1024 * 1024)) + b'"}'

    response = public_client.post(
        "/chat",
        content=payload,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


@pytest.mark.unit
def test_malformed_host_cannot_poison_identity_or_lan_policy_path(
    public_client: TestClient,
) -> None:
    poisoned = public_client.get(
        "/meetings",
        headers={"Host": "echodesk.yoliyoli.uk/healthz?mask="},
    )
    assert poisoned.status_code == 400
    assert poisoned.text == "Invalid host header"

    canonical = public_client.get(
        "/meetings",
        headers={"Host": "echodesk.yoliyoli.uk"},
    )
    assert canonical.status_code == 401
    assert canonical.json()["detail"] == "session required"


@pytest.mark.unit
def test_public_session_http_renew_rotate_and_revoke_preserve_identity(
    public_client: TestClient,
) -> None:
    payload = {**_enrollment_payload("lifecycle"), "display_name": "test"}
    enrolled = public_client.post("/session/enroll", json=payload)
    assert enrolled.status_code == 201
    first = enrolled.json()
    assert first["device_credential"] is None
    stable_scope = {key: first["principal"][key] for key in ("tenant_id", "owner_id", "device_id")}

    renewed = public_client.post(
        "/session/renew",
        json={"device_credential": payload["device_secret"]},
    )
    assert renewed.status_code == 200
    second = renewed.json()
    assert {
        key: second["principal"][key] for key in ("tenant_id", "owner_id", "device_id")
    } == stable_scope
    assert (
        public_client.get(
            "/meetings", headers={"Authorization": f"Bearer {first['token']}"}
        ).status_code
        == 401
    )

    next_credential = "rotated-lifecycle-" + "r" * 40
    rotated = public_client.post(
        "/session/credential/rotate",
        headers={"Authorization": f"Bearer {second['token']}"},
        json={
            "current_device_credential": payload["device_secret"],
            "new_device_credential": next_credential,
        },
    )
    assert rotated.status_code == 200
    assert "device_credential" not in rotated.json()
    assert (
        public_client.post(
            "/session/renew",
            json={"device_credential": payload["device_secret"]},
        ).status_code
        == 401
    )

    third_response = public_client.post(
        "/session/renew",
        json={"device_credential": next_credential},
    )
    assert third_response.status_code == 200
    third = third_response.json()
    revoked = public_client.post(
        "/session/revoke",
        json={"scope": "family"},
        headers={"Authorization": f"Bearer {third['token']}"},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True, "scope": "family"}
    assert (
        public_client.get(
            "/meetings", headers={"Authorization": f"Bearer {third['token']}"}
        ).status_code
        == 401
    )
    assert (
        public_client.post(
            "/session/renew",
            json={"device_credential": next_credential},
        ).status_code
        == 401
    )


@pytest.mark.unit
def test_credential_rotate_reauth_has_dedicated_rate_limit(public_client: TestClient) -> None:
    session, _credential = _issue(public_client, "rotate-limit")
    headers = {"Authorization": f"Bearer {session['token']}"}
    denied = [
        public_client.post(
            "/session/credential/rotate",
            headers=headers,
            json={
                "current_device_credential": "wrong-current-" + "x" * 40,
                "new_device_credential": f"new-{index}-" + "n" * 40,
            },
        )
        for index in range(7)
    ]

    assert [response.status_code for response in denied[:6]] == [401] * 6
    assert denied[6].status_code == 429
    assert int(denied[6].headers["Retry-After"]) >= 1


@pytest.mark.unit
def test_anonymous_session_issuance_is_rate_limited_per_peer(
    public_client: TestClient,
) -> None:
    issued = [
        public_client.post("/session", json=_enrollment_payload(f"rate-{index}"))
        for index in range(12)
    ]
    blocked = public_client.post("/session", json=_enrollment_payload("rate-blocked"))

    assert all(response.status_code == 201 for response in issued)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "session issuance rate limit exceeded"
    assert int(blocked.headers["Retry-After"]) >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remote_lan_caller_cannot_use_host_capabilities_in_local_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        db_path=tmp_path / "local-lan.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=False,
        lan_full_api_enabled=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        debug_token="host-admin-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    result = await run_migrations(settings.db_path)
    assert result.errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    deps_mod.reset_deps_for_test()
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings

    transport = httpx.ASGITransport(app=app, client=("192.168.50.20", 50000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        denied_requests = (
            await client.get("/admin/data-dir"),
            await client.get("/workspace/status"),
            await client.post(
                "/artifacts/generate",
                json={"artifact_type": "html", "brief": "execute on host"},
            ),
            await client.post(
                "/agents/tasks",
                json={"device_id": "lan", "text": "read host"},
            ),
            await client.post(
                "/agents/grants/claude_code",
                json={"device_id": "lan", "workspace_ids": []},
            ),
        )
        assert [response.status_code for response in denied_requests] == [403] * 5

        trusted = {"X-Echo-Admin-Token": "host-admin-secret"}
        assert (await client.get("/workspace/status", headers=trusted)).status_code == 200


@pytest.mark.unit
def test_public_session_cannot_reach_host_runtime_capabilities(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "host-capability")
    headers = {"Authorization": f"Bearer {session['token']}"}

    assert public_client.get("/workspace/status", headers=headers).status_code == 403
    assert (
        public_client.post(
            "/artifacts/generate",
            headers=headers,
            json={"artifact_type": "html", "brief": "run generated host code"},
        ).status_code
        == 403
    )
    assert (
        public_client.post(
            "/agents/grants/claude_code",
            headers=headers,
            json={"device_id": "attacker", "workspace_ids": []},
        ).status_code
        == 403
    )
    assert (
        public_client.post(
            "/agents/tasks",
            headers=headers,
            json={"device_id": "attacker", "text": "read the server"},
        ).status_code
        == 403
    )

    trusted = {"X-Echo-Admin-Token": "test-admin"}
    assert public_client.get("/workspace/status", headers=trusted).status_code == 200


@pytest.mark.unit
def test_public_meetings_are_isolated_between_issued_sessions(
    public_client: TestClient,
) -> None:
    session_a, _credential_a = _issue(public_client, "meeting-a")
    session_b, _credential_b = _issue(public_client, "meeting-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    assert public_client.post("/meetings/meeting-a/start", headers=headers_a).status_code == 200
    injected = public_client.post(
        "/meetings/meeting-a/inject_segment",
        headers=headers_a,
        json={"text": "tenant A secret", "start_ms": 0, "end_ms": 1000},
    )
    assert injected.status_code == 200

    assert [m["meeting_id"] for m in public_client.get("/meetings", headers=headers_a).json()] == [
        "meeting-a"
    ]
    assert public_client.get("/meetings", headers=headers_b).json() == []
    assert public_client.get("/meetings/meeting-a/transcript", headers=headers_b).status_code == 404
    assert public_client.get("/meetings/meeting-a/segments", headers=headers_b).status_code == 404

    assert (
        public_client.post(
            "/meetings/meeting-a/inject_segment",
            headers=headers_b,
            json={"text": "tenant B injection", "start_ms": 1001, "end_ms": 2000},
        ).status_code
        == 404
    )
    assert public_client.post("/meetings/meeting-a/end", headers=headers_b).status_code == 404
    assert (
        public_client.request(
            "DELETE",
            "/meetings/meeting-a/outputs",
            headers=headers_b,
            json={"artifact_ids": [], "clear_minutes": True},
        ).status_code
        == 404
    )

    transcript_a = public_client.get("/meetings/meeting-a/transcript", headers=headers_a).json()
    assert [segment["text"] for segment in transcript_a] == ["tenant A secret"]


@pytest.mark.unit
def test_public_segment_injection_is_byte_bounded_and_storage_governed(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "meeting-bounds")
    headers = {"Authorization": f"Bearer {session['token']}"}
    assert public_client.post("/meetings/bounded/start", headers=headers).status_code == 200

    oversized = public_client.post(
        "/meetings/bounded/inject_segment",
        headers=headers,
        json={"text": "界" * 6_000, "start_ms": 0, "end_ms": 1_000},
    )
    assert oversized.status_code == 413

    payload = {"text": "x" * 12_000, "start_ms": 0, "end_ms": 1_000}
    assert (
        public_client.post(
            "/meetings/bounded/inject_segment", headers=headers, json=payload
        ).status_code
        == 200
    )
    assert (
        public_client.post(
            "/meetings/bounded/inject_segment", headers=headers, json=payload
        ).status_code
        == 200
    )
    exhausted = public_client.post(
        "/meetings/bounded/inject_segment", headers=headers, json=payload
    )
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["metric"] == "storage_bytes"

    transcript = public_client.get("/meetings/bounded/transcript", headers=headers).json()
    assert len(transcript) == 2


@pytest.mark.unit
def test_public_manual_meeting_state_is_principal_scoped(public_client: TestClient) -> None:
    session_a, _credential_a = _issue(public_client, "manual-a")
    session_b, _credential_b = _issue(public_client, "manual-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}

    started_a = public_client.post("/meetings/manual_start", headers=headers_a).json()
    assert started_a["mode"] == "in_meeting"
    assert public_client.get("/meetings/current", headers=headers_b).json()["mode"] == "idle"

    started_b = public_client.post("/meetings/manual_start", headers=headers_b).json()
    assert started_b["mode"] == "in_meeting"
    assert started_b["meeting_id"] != started_a["meeting_id"]
    assert (
        public_client.get("/meetings/current", headers=headers_a).json()["meeting_id"]
        == started_a["meeting_id"]
    )


@pytest.mark.unit
def test_public_share_uses_narrow_resource_ticket(public_client: TestClient) -> None:
    session_a, _credential_a = _issue(public_client, "share-a")
    session_b, _credential_b = _issue(public_client, "share-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}
    assert public_client.post("/meetings/share-a/start", headers=headers_a).status_code == 200

    issued = public_client.post("/meetings/share-a/share-ticket", headers=headers_a)
    assert issued.status_code == 200
    share_path = issued.json()["path"]
    assert "share=" in share_path
    assert session_a["token"] not in share_path
    assert public_client.get(share_path).status_code == 200
    shared = public_client.get(share_path)
    assert shared.headers["cache-control"] == "private, no-store, max-age=0"
    assert shared.headers["referrer-policy"] == "no-referrer"
    assert shared.headers["x-frame-options"] == "DENY"
    assert issued.headers["cache-control"] == "private, no-store, max-age=0"
    assert issued.headers["referrer-policy"] == "no-referrer"
    assert issued.json()["expires_in_s"] == 600
    share_runs = public_client.get("/workflows/runs", headers=headers_a).json()
    share_run = next(run for run in share_runs if run["kind"] == "share.prepare")
    assert share_run["state"] == "succeeded"
    assert share_run["output"]["resource_id"] == "share-a"
    assert "token" not in share_run["output"]

    assert public_client.get("/meetings/share-a/share").status_code == 401
    assert (
        public_client.post("/meetings/share-a/share-ticket", headers=headers_b).status_code == 404
    )
    ticket = share_path.split("share=", 1)[1]
    assert public_client.get(f"/meetings/other/share?share={ticket}").status_code == 401
    assert public_client.get(f"/artifacts/share-a/download?share={ticket}").status_code == 401


@pytest.mark.unit
def test_public_websocket_requires_session_and_replays_only_owner_events(
    public_client: TestClient,
) -> None:
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json({"type": "client_hello", "last_seq": 0})
        with pytest.raises(WebSocketDisconnect) as anonymous:
            ws.receive_json()
    assert anonymous.value.code == 4401

    session_a, _credential_a = _issue(public_client, "ws-a")
    session_b, _credential_b = _issue(public_client, "ws-b")
    headers_a = {"Authorization": f"Bearer {session_a['token']}"}
    headers_b = {"Authorization": f"Bearer {session_b['token']}"}
    assert public_client.post("/meetings/meeting-a/start", headers=headers_a).status_code == 200
    assert public_client.post("/meetings/meeting-b/start", headers=headers_b).status_code == 200

    for session, expected_meeting in (
        (session_a, "meeting-a"),
        (session_b, "meeting-b"),
    ):
        with public_client.websocket_connect("/ws/echo") as ws:
            ws.send_json(
                {
                    "type": "client_hello",
                    "last_seq": 0,
                    "auth": {"type": "bearer", "token": session["token"]},
                }
            )
            assert ws.receive_json()["type"] == "server_hello"
            event = ws.receive_json()
            assert event["type"] == "meeting.started"
            assert event["meeting_id"] == expected_meeting


@pytest.mark.unit
def test_public_websocket_rejects_query_bearer_and_oversized_first_frame(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-query")
    with public_client.websocket_connect(f"/ws/echo?session={session['token']}") as ws:
        ws.send_json({"type": "client_hello", "last_seq": 0})
        with pytest.raises(WebSocketDisconnect) as query_rejected:
            ws.receive_json()
    assert query_rejected.value.code == 4401

    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_text("x" * 4097)
        with pytest.raises(WebSocketDisconnect) as oversized:
            ws.receive_json()
    assert oversized.value.code == 4408


@pytest.mark.unit
def test_public_websocket_revalidates_revoke_and_releases_subscriber(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-revoke")
    token = str(session["token"])
    bus = deps_mod.get_event_bus()
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        revoked = public_client.post(
            "/session/revoke",
            json={"scope": "family"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoked.status_code == 200
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401
    for _ in range(50):
        if bus.subscriber_count() == 0:
            break
        time.sleep(0.01)
    assert bus.subscriber_count() == 0


@pytest.mark.unit
def test_public_websocket_revalidates_expired_connected_session(
    public_client: TestClient,
) -> None:
    session, _credential = _issue(public_client, "ws-expired")
    token = str(session["token"])
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        settings = public_client.app.dependency_overrides[deps_mod.get_settings]()  # type: ignore[union-attr]
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE principal_sessions SET expires_at = ? WHERE session_id = ?",
                ("2000-01-01T00:00:00+00:00", session["principal"]["session_id"]),
            )
            conn.commit()
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401


@pytest.mark.unit
def test_public_websocket_revalidates_session_generation_after_renew(
    public_client: TestClient,
) -> None:
    session, credential = _issue(public_client, "ws-generation")
    token = str(session["token"])
    with public_client.websocket_connect("/ws/echo") as ws:
        ws.send_json(
            {
                "type": "client_hello",
                "last_seq": 0,
                "auth": {"type": "bearer", "token": token},
            }
        )
        assert ws.receive_json()["type"] == "server_hello"
        renewed = public_client.post(
            "/session/renew",
            json={"device_credential": credential},
        )
        assert renewed.status_code == 200
        ws.send_json({"type": "client_ping"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()
        assert disconnected.value.code == 4401
