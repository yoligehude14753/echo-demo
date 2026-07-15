from __future__ import annotations

import json
import sys

import httpx
import pytest
from app.hub.client import HubClient, HubError


@pytest.mark.asyncio
async def test_hub_client_covers_pairing_devices_and_revoke_endpoints():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/hub/v1/pairings":
            return httpx.Response(
                200,
                json={
                    "pairing_code": "ABCD-1234",
                    "expires_at": "2026-07-14T12:00:00Z",
                },
                request=request,
            )
        if request.method == "POST" and request.url.path == "/hub/v1/pairings/claim":
            return httpx.Response(200, json={"sync_token": "sync-secret"}, request=request)
        if request.method == "GET" and request.url.path == "/hub/v1/devices":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"device_id": "desktop-1", "name": "PC", "platform": "darwin"},
                        {"device_id": "phone-1", "name": "Android", "platform": "android"},
                    ]
                },
                request=request,
            )
        if request.method == "DELETE" and request.url.path == "/hub/v1/devices/phone-1":
            return httpx.Response(204, request=request)
        return httpx.Response(404, request=request)

    client = HubClient(
        "http://hub.test",
        device_id="desktop-1",
        transport=httpx.MockTransport(handler),
    )
    try:
        pairing = await client.create_pairing()
        assert pairing.code == "ABCD-1234"
        assert pairing.expires_at == "2026-07-14T12:00:00Z"

        claim = await client.claim_pairing("ABCD-1234")
        assert claim.sync_token == "sync-secret"
        client.set_sync_token(claim.sync_token)
        devices = await client.list_devices()
        assert [device.device_id for device in devices] == ["desktop-1", "phone-1"]
        assert devices[0].is_current is True
        await client.revoke_device("phone-1")
    finally:
        await client.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/hub/v1/pairings"),
        ("POST", "/hub/v1/pairings/claim"),
        ("GET", "/hub/v1/devices"),
        ("DELETE", "/hub/v1/devices/phone-1"),
    ]
    assert json.loads(requests[0].content)["device_id"] == "desktop-1"
    assert json.loads(requests[1].content) == {
        "pairing_code": "ABCD-1234",
        "device_id": "desktop-1",
        "device_name": "EchoDesk Desktop",
        "platform": sys.platform,
    }
    assert requests[2].headers["X-Echo-Sync-Token"] == "sync-secret"
    assert "Authorization" not in requests[2].headers


@pytest.mark.asyncio
async def test_hub_client_hides_http_body_from_public_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            content=b"provider-internal-secret",
            request=request,
        )

    client = HubClient(
        "http://hub.test",
        device_id="desktop-1",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(HubError) as error:
            await client.list_devices()
    finally:
        await client.close()

    assert error.value.code == "request_failed"
    assert str(error.value) == "request_failed"
    assert "provider-internal-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_hub_client_sync_push_changes_and_snapshot_contract():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/base/hub/v1/sync/push":
            operation = json.loads(request.content)
            operation_id = operation["operation_id"]
            status = {
                "sync:desktop:1": "applied",
                "sync:desktop:2": "duplicate",
                "sync:desktop:3": "conflict",
            }[operation_id]
            return httpx.Response(
                200,
                json={"operation_id": operation_id, "status": status, "revision": 1, "cursor": 1},
                request=request,
            )
        if request.method == "GET" and request.url.path == "/base/hub/v1/sync/changes":
            return httpx.Response(
                200,
                json={"changes": [{"operation_id": "remote:1"}], "cursor": 0},
                request=request,
            )
        if request.method == "GET" and request.url.path == "/base/hub/v1/sync/snapshot":
            return httpx.Response(
                200,
                json={
                    "cursor": 0,
                    "transcript_segments": [{"operation_id": "remote:transcript"}],
                    "meeting_summaries": [{"operation_id": "remote:summary"}],
                    "memories": [{"operation_id": "remote:memory"}],
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    client = HubClient(
        "https://hub.test/base",
        device_id="desktop",
        sync_token="sync-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        pushed = await client.push(
            [
                {"operation_id": "sync:desktop:1"},
                {"operation_id": "sync:desktop:2"},
                {"operation_id": "sync:desktop:3"},
            ]
        )
        changes, changes_cursor = await client.changes(cursor=None, limit=20)
        snapshot, snapshot_cursor = await client.snapshot()
    finally:
        await client.close()

    assert pushed.applied == ["sync:desktop:1"]
    assert pushed.duplicate == ["sync:desktop:2"]
    assert pushed.conflict == ["sync:desktop:3"]
    assert changes == [{"operation_id": "remote:1"}]
    assert changes_cursor == "0"
    assert snapshot == [
        {"operation_id": "remote:transcript"},
        {"operation_id": "remote:summary"},
        {"operation_id": "remote:memory"},
    ]
    assert snapshot_cursor == "0"
    assert client._events_url("0") == ("wss://hub.test/base/hub/v1/sync/events?cursor=0")
    changes_request = next(
        request for request in requests if request.url.path.endswith("/sync/changes")
    )
    assert "cursor" not in changes_request.url.params
    sync_requests = [request for request in requests if "/sync/" in request.url.path]
    assert sync_requests
    assert all(request.headers["X-Echo-Sync-Token"] == "sync-secret" for request in sync_requests)
    assert all("Authorization" not in request.headers for request in sync_requests)
