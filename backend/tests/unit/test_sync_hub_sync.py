from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import run_migrations
from app.api import deps as deps_mod
from app.config import Settings
from app.main import create_app
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    PUBLIC_CLIENT_VERSION_HEADER,
)
from fastapi.testclient import TestClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "sync.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skills",
        public_demo_mode=True,
        workspace_scan_on_startup=False,
        tts_enabled=False,
        diarizer_enabled=False,
        web_search_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    settings = _settings(tmp_path)
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[deps_mod.get_settings] = lambda: settings
    deps_mod.reset_deps_for_test()
    with TestClient(
        app,
        headers={PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION},
    ) as client:
        yield client
    deps_mod.reset_deps_for_test()


def _issue(client: TestClient, label: str) -> str:
    response = client.post(
        "/session",
        json={
            "enrollment_id": f"sync-enrollment-{label}-" + "e" * 40,
            "device_secret": f"sync-secret-{label}-" + "s" * 40,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _claim_device(
    client: TestClient,
    session_token: str,
    device_id: str,
) -> str:
    pairing = client.post(
        "/hub/v1/pairings",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert pairing.status_code == 201, pairing.text
    claim = client.post(
        "/hub/v1/pairings/claim",
        json={
            "pairing_code": pairing.json()["pairing_code"],
            "device_id": device_id,
            "device_name": device_id,
            "platform": "test",
        },
    )
    assert claim.status_code == 200, claim.text
    return claim.json()["sync_token"]


def _push(
    client: TestClient,
    token: str,
    *,
    operation_id: str,
    device_id: str,
    entity_type: str,
    entity_id: str,
    base_revision: int,
    payload: dict[str, object],
) -> dict[str, object]:
    response = client.post(
        "/hub/v1/sync/push",
        headers={"X-Echo-Sync-Token": token},
        json={
            "operation_id": operation_id,
            "device_id": device_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "base_revision": base_revision,
            "updated_at": "2026-07-14T10:00:00Z",
            "payload": payload,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.unit
def test_sync_push_duplicate_conflict_changes_snapshot_and_repository_adapters(
    public_client: TestClient,
    tmp_path: Path,
) -> None:
    session = _issue(public_client, "adapter")
    token = _claim_device(public_client, session, "sync-adapter-device")
    headers = {"X-Echo-Sync-Token": token}
    public_fallback = public_client.get(
        "/hub/v1/sync/changes?cursor=0",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert public_fallback.status_code == 401, public_fallback.text

    transcript_payload = {
        "meeting_id": "meeting-sync-adapter",
        "segment_id": 101,
        "text": "first transcript",
        "start_ms": 0,
        "end_ms": 1200,
        "speaker_label": "Speaker A",
        "tenant_id": "attacker-tenant",
        "owner_id": "attacker-owner",
        "device_id": "attacker-device",
    }
    reserved_namespace = public_client.post(
        "/hub/v1/sync/push",
        headers=headers,
        json={
            "operation_id": "capture:1:1",
            "device_id": "sync-adapter-device",
            "entity_type": "transcript_segment",
            "entity_id": "101",
            "base_revision": 0,
            "updated_at": "2026-07-14T10:00:00Z",
            "payload": transcript_payload,
        },
    )
    assert reserved_namespace.status_code == 400, reserved_namespace.text

    applied = _push(
        public_client,
        token,
        operation_id="op-transcript-1",
        device_id="sync-adapter-device",
        entity_type="transcript_segment",
        entity_id="101",
        base_revision=0,
        payload=transcript_payload,
    )
    assert applied == {"status": "applied", "revision": 1, "cursor": 1}

    duplicate = _push(
        public_client,
        token,
        operation_id="op-transcript-1",
        device_id="sync-adapter-device",
        entity_type="transcript_segment",
        entity_id="101",
        base_revision=0,
        payload=transcript_payload,
    )
    assert duplicate["status"] == "duplicate"
    assert duplicate["revision"] == 1
    assert duplicate["cursor"] == 1

    conflict = _push(
        public_client,
        token,
        operation_id="op-transcript-conflict",
        device_id="sync-adapter-device",
        entity_type="transcript_segment",
        entity_id="101",
        base_revision=0,
        payload={**transcript_payload, "text": "stale transcript"},
    )
    assert conflict["status"] == "conflict"
    assert conflict["revision"] == 1
    assert conflict["current"]["text"] == "first transcript"

    updated = _push(
        public_client,
        token,
        operation_id="op-transcript-2",
        device_id="sync-adapter-device",
        entity_type="transcript_segment",
        entity_id="101",
        base_revision=1,
        payload={**transcript_payload, "text": "updated transcript"},
    )
    assert updated == {"status": "applied", "revision": 2, "cursor": 2}

    summary = _push(
        public_client,
        token,
        operation_id="op-summary-1",
        device_id="sync-adapter-device",
        entity_type="meeting_summary",
        entity_id="meeting-sync-adapter",
        base_revision=0,
        payload={
            "meeting_id": "meeting-sync-adapter",
            "title": "Adapter meeting",
            "summary": "A synchronized summary",
        },
    )
    assert summary["status"] == "applied"
    assert summary["revision"] == 1
    assert summary["cursor"] == 3

    memory = _push(
        public_client,
        token,
        operation_id="op-memory-1",
        device_id="sync-adapter-device",
        entity_type="memory",
        entity_id="memory-adapter-1",
        base_revision=0,
        payload={
            "memory_id": "memory-adapter-1",
            "kind": "fact",
            "content": "The adapter test uses SQLite",
            "canonical_key": "adapter-test",
            "confidence": 0.9,
            "salience": 0.8,
        },
    )
    assert memory["status"] == "applied"
    assert memory["cursor"] == 4

    changes = public_client.get(
        "/hub/v1/sync/changes?cursor=0&limit=20",
        headers=headers,
    )
    assert changes.status_code == 200, changes.text
    changes_body = changes.json()
    assert changes_body["cursor"] == 4
    assert [item["entity_type"] for item in changes_body["changes"]] == [
        "transcript_segment",
        "transcript_segment",
        "meeting_summary",
        "memory",
    ]

    snapshot = public_client.get("/hub/v1/sync/snapshot", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    snapshot_body = snapshot.json()
    assert snapshot_body["cursor"] == 4
    assert snapshot_body["transcript_segments"][0]["payload"]["text"] == "updated transcript"
    assert snapshot_body["meeting_summaries"][0]["payload"]["summary"] == (
        "A synchronized summary"
    )
    assert snapshot_body["memories"][0]["payload"]["content"] == (
        "The adapter test uses SQLite"
    )

    async def read_business_rows() -> tuple[tuple[object, ...], tuple[object, ...]]:
        async with aiosqlite.connect(str(tmp_path / "sync.db")) as conn:
            segment = await (
                await conn.execute(
                    "SELECT meeting_id, text, owner_id FROM meeting_segments WHERE id = 101"
                )
            ).fetchone()
            memory_row = await (
                await conn.execute(
                    "SELECT memory_id, content, revision FROM memory_nodes "
                    "WHERE memory_id = 'memory-adapter-1'"
                )
            ).fetchone()
        assert segment is not None
        assert memory_row is not None
        return tuple(segment), tuple(memory_row)

    segment_row, memory_row = asyncio.run(read_business_rows())
    assert segment_row[0:2] == ("meeting-sync-adapter", "updated transcript")
    assert segment_row[2] != "attacker-owner"
    assert memory_row == ("memory-adapter-1", "The adapter test uses SQLite", 1)


@pytest.mark.unit
def test_sync_websocket_notifies_and_user_scopes_are_isolated(
    public_client: TestClient,
) -> None:
    session_a = _issue(public_client, "ws-a")
    token_a = _claim_device(public_client, session_a, "sync-ws-a")
    session_b = _issue(public_client, "ws-b")
    token_b = _claim_device(public_client, session_b, "sync-ws-b")

    with public_client.websocket_connect(
        f"/hub/v1/sync/events?sync_token={token_a}&cursor=0"
    ) as websocket:
        result = _push(
            public_client,
            token_a,
            operation_id="op-ws-1",
            device_id="sync-ws-a",
            entity_type="transcript_segment",
            entity_id="201",
            base_revision=0,
            payload={
                "meeting_id": "meeting-ws",
                "segment_id": 201,
                "text": "websocket event",
                "start_ms": 0,
                "end_ms": 500,
            },
        )
        assert result["status"] == "applied"
        event = websocket.receive_json()
        assert event["cursor"] == 1
        assert event["entity_type"] == "transcript_segment"
        assert event["payload"]["text"] == "websocket event"

    isolated_changes = public_client.get(
        "/hub/v1/sync/changes?cursor=0",
        headers={"X-Echo-Sync-Token": token_b},
    )
    assert isolated_changes.status_code == 200, isolated_changes.text
    assert isolated_changes.json() == {"cursor": 0, "changes": []}
    isolated_snapshot = public_client.get(
        "/hub/v1/sync/snapshot",
        headers={"X-Echo-Sync-Token": token_b},
    )
    assert isolated_snapshot.status_code == 200, isolated_snapshot.text
    assert isolated_snapshot.json()["cursor"] == 0
    assert isolated_snapshot.json()["transcript_segments"] == []


@pytest.mark.unit
def test_sync_data_survives_app_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    def make_client() -> TestClient:
        app = create_app()
        app.dependency_overrides[deps_mod.get_settings] = lambda: settings
        deps_mod.reset_deps_for_test()
        return TestClient(
            app,
            headers={PUBLIC_CLIENT_VERSION_HEADER: MINIMUM_PUBLIC_CLIENT_VERSION},
        )

    with make_client() as first_client:
        session = _issue(first_client, "restart")
        token = _claim_device(first_client, session, "sync-restart")
        result = _push(
            first_client,
            token,
            operation_id="op-restart-1",
            device_id="sync-restart",
            entity_type="memory",
            entity_id="memory-restart",
            base_revision=0,
            payload={
                "memory_id": "memory-restart",
                "kind": "decision",
                "content": "Persist this across restart",
            },
        )
        assert result["status"] == "applied"
    deps_mod.reset_deps_for_test()

    with make_client() as second_client:
        changes = second_client.get(
            "/hub/v1/sync/changes?cursor=0",
            headers={"X-Echo-Sync-Token": token},
        )
        assert changes.status_code == 200, changes.text
        assert len(changes.json()["changes"]) == 1
        snapshot = second_client.get(
            "/hub/v1/sync/snapshot",
            headers={"X-Echo-Sync-Token": token},
        )
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["memories"][0]["payload"]["content"] == (
            "Persist this across restart"
        )
    deps_mod.reset_deps_for_test()


@pytest.mark.unit
def test_gateway_snapshot_deduplicates_remote_transcript_side_effect(
    public_client: TestClient,
    tmp_path: Path,
) -> None:
    session = _issue(public_client, "gateway-snapshot")
    device_id = "android-gateway-device"
    token = _claim_device(public_client, session, device_id)
    headers = {"X-Echo-Sync-Token": token}
    first_entity_id = "meeting-gateway:0:1200"
    first_payload = {
        "meeting_id": "meeting-gateway",
        "text": "remote canonical transcript",
        "start_ms": 0,
        "end_ms": 1200,
        "captured_at": "2026-07-14T10:00:00Z",
    }

    applied = _push(
        public_client,
        token,
        operation_id="op-gateway-canonical-1",
        device_id=device_id,
        entity_type="transcript_segment",
        entity_id=first_entity_id,
        base_revision=0,
        payload=first_payload,
    )
    assert applied == {"status": "applied", "revision": 1, "cursor": 1}

    changes = public_client.get("/hub/v1/sync/changes?cursor=0", headers=headers)
    assert changes.status_code == 200, changes.text
    change = changes.json()["changes"]
    assert len(change) == 1
    assert change[0]["entity_id"] == first_entity_id
    assert change[0]["source_device_id"] == device_id
    assert change[0]["revision"] == 1

    initial_snapshot = public_client.get("/hub/v1/sync/snapshot", headers=headers)
    assert initial_snapshot.status_code == 200, initial_snapshot.text
    initial_transcript = initial_snapshot.json()["transcript_segments"]
    assert len(initial_transcript) == 1
    assert initial_transcript[0]["entity_id"] == first_entity_id
    assert initial_transcript[0]["source_device_id"] == device_id
    assert initial_transcript[0]["revision"] == 1

    async def insert_legacy_side_effect() -> None:
        async with aiosqlite.connect(str(tmp_path / "sync.db")) as conn:
            scope = await (
                await conn.execute(
                    "SELECT tenant_id, user_id FROM devices WHERE device_id = ?",
                    (device_id,),
                )
            ).fetchone()
            assert scope is not None
            await conn.execute(
                """INSERT INTO meeting_segments (
                       meeting_id, text, start_ms, end_ms, captured_at,
                       tenant_id, device_id, owner_id
                   ) VALUES (?, ?, ?, ?, ?, ?, 'legacy-local', ?)""",
                (
                    first_payload["meeting_id"],
                    first_payload["text"],
                    first_payload["start_ms"],
                    first_payload["end_ms"],
                    "2026-07-14T10:00:01Z",
                    scope[0],
                    scope[1],
                ),
            )
            await conn.commit()

    asyncio.run(insert_legacy_side_effect())
    repeated_snapshot = public_client.get("/hub/v1/sync/snapshot", headers=headers)
    assert repeated_snapshot.status_code == 200, repeated_snapshot.text
    repeated_transcript = repeated_snapshot.json()["transcript_segments"]
    assert len(repeated_transcript) == 1
    assert repeated_transcript[0]["entity_id"] == first_entity_id
    assert repeated_transcript[0]["source_device_id"] == device_id

    second_entity_id = "meeting-gateway:1200:2400"
    second = _push(
        public_client,
        token,
        operation_id="op-gateway-canonical-2",
        device_id=device_id,
        entity_type="transcript_segment",
        entity_id=second_entity_id,
        base_revision=0,
        payload={
            **first_payload,
            "start_ms": 1200,
            "end_ms": 2400,
            "text": "second remote transcript",
        },
    )
    assert second == {"status": "applied", "revision": 1, "cursor": 2}

    final_snapshot = public_client.get("/hub/v1/sync/snapshot", headers=headers)
    assert final_snapshot.status_code == 200, final_snapshot.text
    final_transcript = final_snapshot.json()["transcript_segments"]
    assert len(final_transcript) == 2
    assert {item["entity_id"] for item in final_transcript} == {
        first_entity_id,
        second_entity_id,
    }
    assert {item["source_device_id"] for item in final_transcript} == {device_id}
