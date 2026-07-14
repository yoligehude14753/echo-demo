from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.hub.sync import HubSyncStore
from app.schemas.meeting import TranscriptSegment


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_hub_sync_reconciles_transcript_and_summary_into_durable_outbox(tmp_path):
    db_path = tmp_path / "echo.db"
    repository = SQLiteRepository(db_path)
    await repository.init()
    started_at = datetime.now(UTC)
    await repository.create_meeting("meeting-1", started_at=started_at, title="Local meeting")
    assert await repository.append_meeting_segment(
        "meeting-1",
        TranscriptSegment(text="local transcript", start_ms=0, end_ms=900),
        captured_at=started_at,
    )
    await repository.update_meeting_state(
        "meeting-1",
        state="finalized",
        ended_at=started_at,
        finalized_at=started_at,
        minutes_json='{"summary":"local summary"}',
        minutes_status="ok",
        display_title="Local summary",
    )

    store = HubSyncStore(db_path, device_id="desktop-device")
    await store.init()
    try:
        queued = await store.reconcile_local_changes()
        outbox = await store.list_outbox()
    finally:
        await store.aclose()
        await repository.aclose()

    assert queued == 2
    assert {item["entity_type"] for item in outbox} == {
        "transcript_segment",
        "meeting_summary",
    }
    assert all(item["operation_id"].startswith("sync:desktop-device:") for item in outbox)
    assert all(item["state"] == "pending" for item in outbox)


@pytest.mark.asyncio
async def test_hub_sync_applies_android_transcript_and_memory_idempotently(tmp_path):
    db_path = tmp_path / "echo.db"
    store = HubSyncStore(db_path, device_id="desktop-device")
    await store.init()
    transcript_change = {
        "operation_id": "android-device:segment-op-1",
        "device_id": "android-device",
        "entity_type": "transcript_segment",
        "entity_id": "android-device:segment-1",
        "base_revision": 0,
        "updated_at": _timestamp(),
        "payload": {
            "meeting_id": "android-meeting-1",
            "meeting_title": "Android meeting",
            "meeting_state": "in_meeting",
            "segment_id": "segment-1",
            "text": "Android transcript",
            "start_ms": 0,
            "end_ms": 1_200,
            "speaker_id": None,
            "speaker_label": "Speaker 1",
            "captured_at": _timestamp(),
        },
    }
    memory_change = {
        "operation_id": "android-device:memory-op-1",
        "device_id": "android-device",
        "entity_type": "memory",
        "entity_id": "android-device:memory-1",
        "base_revision": 0,
        "updated_at": _timestamp(),
        "payload": {
            "memory_id": "memory-1",
            "kind": "fact",
            "content": "Android memory",
            "normalized_content": "android memory",
            "canonical_key": "android-memory",
            "subject": "EchoDesk",
            "confidence": 0.9,
            "salience": 0.8,
            "scope": "owner",
            "status": "active",
            "hit_count": 1,
            "source_count": 1,
            "user_confirmed": True,
            "created_at": _timestamp(),
            "last_seen_at": _timestamp(),
            "updated_at": _timestamp(),
            "confirmed_at": _timestamp(),
            "superseded_at": None,
            "superseded_by": None,
            "deleted_at": None,
            "revision": 1,
            "metadata": {"source": "android"},
        },
    }
    try:
        first = await store.apply_changes([transcript_change, memory_change])
        duplicate = await store.apply_changes([transcript_change, memory_change])
        await store.reconcile_local_changes()
        outbox = await store.list_outbox()
        conn = store._require_conn()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM meeting_segments WHERE meeting_id = ?",
            ("android-meeting-1",),
        )
        segment_count = int((await cursor.fetchone())[0])
        await cursor.close()
        cursor = await conn.execute(
            "SELECT content FROM memory_nodes WHERE memory_id = ?",
            ("memory-1",),
        )
        memory_row = await cursor.fetchone()
        await cursor.close()
    finally:
        await store.aclose()

    assert first.applied == 2
    assert duplicate.duplicate == 2
    assert segment_count == 1
    assert memory_row[0] == "Android memory"
    assert outbox == []


@pytest.mark.asyncio
async def test_hub_sync_conflict_and_snapshot_recovery(tmp_path):
    db_path = tmp_path / "echo.db"
    store = HubSyncStore(db_path, device_id="desktop-device")
    await store.init()
    base = {
        "operation_id": "android-device:summary-1",
        "device_id": "android-device",
        "entity_type": "meeting_summary",
        "entity_id": "android-device:meeting-1",
        "base_revision": 0,
        "updated_at": _timestamp(),
        "payload": {
            "meeting_id": "meeting-1",
            "title": "Remote meeting",
            "display_title": "Remote summary",
            "started_at": _timestamp(),
            "ended_at": _timestamp(),
            "finalized_at": _timestamp(),
            "minutes_json": '{"summary":"one"}',
            "minutes_status": "ok",
            "minutes_error": None,
            "minutes_cleared_at": None,
            "deleted": False,
        },
    }
    conflict = {
        **base,
        "operation_id": "android-device:summary-2",
        "payload": {**base["payload"], "minutes_json": '{"summary":"two"}'},
    }
    try:
        applied = await store.apply_changes([base])
        conflicted = await store.apply_changes([conflict])
        snapshot = await store.apply_changes([conflict], snapshot=True)
    finally:
        await store.aclose()

    assert applied.applied == 1
    assert conflicted.conflict == 1
    assert snapshot.applied == 1
