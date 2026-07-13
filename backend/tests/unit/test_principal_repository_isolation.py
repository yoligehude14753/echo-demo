from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.schemas.meeting import TranscriptSegment
from app.security import Principal
from app.security.context import bind_principal, reset_principal


def _principal(suffix: str) -> Principal:
    return Principal(
        tenant_id=f"tenant-{suffix}",
        device_id=f"device-{suffix}",
        owner_id=f"owner-{suffix}",
        session_id=f"session-{suffix}",
        mode="public",
    )


@pytest.mark.unit
async def test_repository_enforces_owner_for_read_write_and_clear(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "isolation.db")
    await repo.init()
    now = datetime.now(UTC)
    principal_a = _principal("a")
    principal_b = _principal("b")
    token_a = bind_principal(principal_a)
    try:
        await repo.create_meeting("meeting-a", started_at=now, title="owner A")
        await repo.append_meeting_segment(
            "meeting-a",
            TranscriptSegment(text="A secret", start_ms=0, end_ms=1000),
            captured_at=now,
        )
        await repo.append_ambient_segment(
            audio_ref="a.wav",
            text="A ambient",
            captured_at=now,
        )
        await repo.upsert_speaker("speaker-shared", captured_at=now, label="Alice")
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        assert await repo.get_meeting("meeting-a") is None
        assert await repo.list_meetings() == []
        assert await repo.list_meeting_segments("meeting-a") == []
        assert await repo.list_ambient_segments() == []
        assert await repo.count_ambient_segments() == 0

        await repo.update_meeting_state("meeting-a", state="ended", title="stolen")
        await repo.append_meeting_segment(
            "meeting-a",
            TranscriptSegment(text="B injected", start_ms=1001, end_ms=2000),
            captured_at=now,
        )
        await repo.clear_meeting_outputs("meeting-a")
        await repo.upsert_speaker("speaker-shared", captured_at=now, label="Bob")
        speaker_b = await repo.get_speaker("speaker-shared")
        assert speaker_b is not None
        assert speaker_b.label == "Bob"
    finally:
        reset_principal(token_b)

    token_a = bind_principal(principal_a)
    try:
        meeting = await repo.get_meeting("meeting-a")
        assert meeting is not None
        assert meeting.title == "owner A"
        assert meeting.state == "in_meeting"
        segments = await repo.list_meeting_segments("meeting-a")
        assert [segment.text for segment in segments] == ["A secret"]
        assert [segment.text for segment in await repo.list_ambient_segments()] == ["A ambient"]
        speaker_a = await repo.get_speaker("speaker-shared")
        assert speaker_a is not None
        assert speaker_a.label == "Alice"
    finally:
        reset_principal(token_a)
        await repo.aclose()
