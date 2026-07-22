"""SQLite Repository 单测：CRUD + hydrate 行为。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.schemas.meeting import TranscriptSegment


@pytest.fixture
async def repo(tmp_path: Path) -> SQLiteRepository:
    r = SQLiteRepository(tmp_path / "test.db")
    await r.init()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_init_creates_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "echo.db"
    r = SQLiteRepository(db_path)
    await r.init()
    try:
        assert db_path.exists()
    finally:
        await r.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_and_get_meeting(repo: SQLiteRepository) -> None:
    started = datetime.now(UTC)
    await repo.create_meeting("m1", started_at=started, title="Q3 销售")

    got = await repo.get_meeting("m1")
    assert got is not None
    assert got.id == "m1"
    assert got.title == "Q3 销售"
    assert got.state == "in_meeting"
    assert got.started_at.replace(microsecond=0) == started.replace(microsecond=0)
    assert got.auto_started is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_meeting_is_idempotent(repo: SQLiteRepository) -> None:
    started = datetime.now(UTC)
    await repo.create_meeting("m1", started_at=started)
    later = started + timedelta(minutes=5)
    # 第二次 create 应被 OR IGNORE 静默丢弃，保留首次 started_at
    await repo.create_meeting("m1", started_at=later, title="new title")
    got = await repo.get_meeting("m1")
    assert got is not None
    assert got.started_at.replace(microsecond=0) == started.replace(microsecond=0)
    assert got.title is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_meeting_state_progression(repo: SQLiteRepository) -> None:
    started = datetime.now(UTC)
    await repo.create_meeting("m1", started_at=started)

    ended = started + timedelta(minutes=10)
    await repo.update_meeting_state("m1", state="ended", ended_at=ended)
    got = await repo.get_meeting("m1")
    assert got is not None
    assert got.state == "ended"
    assert got.ended_at is not None

    finalized = started + timedelta(minutes=12)
    await repo.update_meeting_state(
        "m1",
        state="finalized",
        title="Final Q3",
        finalized_at=finalized,
        minutes_json='{"summary": "ok"}',
        raw_transcript_ref="/tmp/m1.json",
    )
    got = await repo.get_meeting("m1")
    assert got is not None
    assert got.state == "finalized"
    assert got.title == "Final Q3"
    assert got.minutes_json == '{"summary": "ok"}'
    assert got.raw_transcript_ref == "/tmp/m1.json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_meetings_filters_state(repo: SQLiteRepository) -> None:
    base = datetime.now(UTC)
    await repo.create_meeting("a", started_at=base - timedelta(minutes=2))
    await repo.update_meeting_state("a", state="finalized", finalized_at=base)
    await repo.create_meeting("b", started_at=base - timedelta(minutes=1))
    await repo.update_meeting_state("b", state="finalized", finalized_at=base)
    await repo.create_meeting("c", started_at=base)

    in_meeting = await repo.list_meetings(state="in_meeting")
    assert [m.id for m in in_meeting] == ["c"]
    final = await repo.list_meetings(state="finalized")
    assert [m.id for m in final] == ["b", "a"]

    all_meetings = await repo.list_meetings()
    assert {m.id for m in all_meetings} == {"a", "b", "c"}
    # ORDER BY started_at DESC
    assert all_meetings[0].id == "c"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_repositories_return_one_authoritative_active_meeting(
    tmp_path: Path,
) -> None:
    """Two connections racing from idle must converge on the same DB row."""

    db_path = tmp_path / "concurrent-start.db"
    repo_a = SQLiteRepository(db_path)
    repo_b = SQLiteRepository(db_path)
    await repo_a.init()
    await repo_b.init()
    release = asyncio.Event()

    async def create(repo: SQLiteRepository, meeting_id: str):  # type: ignore[no-untyped-def]
        await release.wait()
        return await repo.create_meeting(meeting_id, started_at=datetime.now(UTC))

    try:
        task_a = asyncio.create_task(create(repo_a, "meeting-a"))
        task_b = asyncio.create_task(create(repo_b, "meeting-b"))
        release.set()
        active_a, active_b = await asyncio.gather(task_a, task_b)

        assert active_a.id == active_b.id
        assert active_a.id in {"meeting-a", "meeting-b"}
        persisted = await repo_a.list_meetings(state="in_meeting", limit=10)
        assert [meeting.id for meeting in persisted] == [active_a.id]
    finally:
        await asyncio.gather(repo_a.aclose(), repo_b.aclose())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_and_list_meeting_segments(repo: SQLiteRepository) -> None:
    await repo.create_meeting("m1", started_at=datetime.now(UTC))
    seg1 = TranscriptSegment(text="hi", start_ms=0, end_ms=500, speaker_id="spk_A")
    seg2 = TranscriptSegment(
        text="world", start_ms=600, end_ms=1100, speaker_id="spk_B", speaker_label="说话人2"
    )
    now = datetime.now(UTC)
    await repo.append_meeting_segment("m1", seg1, captured_at=now)
    await repo.append_meeting_segment("m1", seg2, captured_at=now + timedelta(seconds=1))

    got = await repo.list_meeting_segments("m1")
    assert len(got) == 2
    assert got[0].text == "hi"
    assert got[0].speaker_id == "spk_A"
    assert got[1].text == "world"
    assert got[1].speaker_label == "说话人2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meeting_speaker_label_upsert(repo: SQLiteRepository) -> None:
    await repo.create_meeting("m1", started_at=datetime.now(UTC))
    await repo.upsert_meeting_speaker_label("m1", "spk_A", "说话人1")
    await repo.upsert_meeting_speaker_label("m1", "spk_B", "说话人2")
    labels = await repo.get_meeting_speaker_labels("m1")
    assert labels == {"spk_A": "说话人1", "spk_B": "说话人2"}

    # update：相同 (meeting, speaker) 改 label
    await repo.upsert_meeting_speaker_label("m1", "spk_A", "李雷")
    labels = await repo.get_meeting_speaker_labels("m1")
    assert labels["spk_A"] == "李雷"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambient_segment_persist_and_query(repo: SQLiteRepository) -> None:
    t0 = datetime.now(UTC)
    aid = await repo.append_ambient_segment(
        audio_ref="/x/1.wav",
        text="今天天气不错",
        captured_at=t0,
        speaker_id="spk_A",
        speaker_label="说话人1",
        duration_ms=8_000,
        client_segment_id="device:native:segment-1",
    )
    assert aid > 0
    rows = await repo.list_ambient_segments(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r.audio_ref == "/x/1.wav"
    assert r.speaker_id == "spk_A"
    assert r.duration_ms == 8_000
    assert r.client_segment_id == "device:native:segment-1"

    # 时间窗口过滤
    later = t0 + timedelta(minutes=5)
    await repo.append_ambient_segment(audio_ref="/x/2.wav", text="另一段", captured_at=later)
    only_recent = await repo.list_ambient_segments(since=t0 + timedelta(minutes=1))
    assert len(only_recent) == 1
    assert only_recent[0].audio_ref == "/x/2.wav"

    count = await repo.count_ambient_segments()
    assert count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rag_projection_backoff_prevents_old_failures_from_starving_pending_work(
    repo: SQLiteRepository,
) -> None:
    started_at = datetime.now(UTC) - timedelta(days=1)
    for index in range(100):
        meeting_id = f"poison-{index:03d}"
        await repo.create_meeting(meeting_id, started_at=started_at + timedelta(seconds=index))
        await repo.update_meeting_state(
            meeting_id,
            state="ended",
            rag_projection_state="index_pending",
        )
        await repo.set_meeting_rag_projection(
            meeting_id,
            state="index_failed",
            error="permanent malformed minutes",
            retry_backoff=True,
        )

    pending_id = "pending-after-poison"
    await repo.create_meeting(pending_id, started_at=datetime.now(UTC))
    await repo.update_meeting_state(
        pending_id,
        state="ended",
        rag_projection_state="index_pending",
    )

    due = await repo.list_meetings_needing_rag_projection(limit=100)

    assert [meeting.id for meeting in due] == [pending_id]
    poison = await repo.get_meeting("poison-000")
    assert poison is not None
    assert poison.rag_projection_attempts == 1
    assert poison.rag_projection_next_retry_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meeting_rag_projection_state_uses_generation_compare_and_swap(
    repo: SQLiteRepository,
) -> None:
    meeting_id = "projection-cas"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC))
    await repo.update_meeting_state(
        meeting_id,
        state="ended",
        rag_projection_state="index_pending",
    )
    first = await repo.get_meeting(meeting_id)
    assert first is not None

    await repo.clear_meeting_outputs(meeting_id)
    second = await repo.get_meeting(meeting_id)
    assert second is not None
    assert second.rag_projection_generation == first.rag_projection_generation + 1
    assert second.rag_projection_state == "delete_pending"

    assert not await repo.set_meeting_rag_projection(
        meeting_id,
        state="indexed",
        expected_generation=first.rag_projection_generation,
    )
    unchanged = await repo.get_meeting(meeting_id)
    assert unchanged is not None
    assert unchanged.rag_projection_state == "delete_pending"
    assert unchanged.rag_projection_attempts == 0

    assert await repo.set_meeting_rag_projection(
        meeting_id,
        state="deleted",
        projected_at=datetime.now(UTC),
        expected_generation=second.rag_projection_generation,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending_state", "success_state", "failure_state"),
    [
        ("index_pending", "indexed", "index_failed"),
        ("delete_pending", "deleted", "delete_failed"),
    ],
)
async def test_meeting_rag_success_rejects_late_failure_from_another_repository(
    tmp_path: Path,
    pending_state: str,
    success_state: str,
    failure_state: str,
) -> None:
    db_path = tmp_path / f"meeting-{success_state}-monotonic.db"
    repo_a = SQLiteRepository(db_path)
    repo_b = SQLiteRepository(db_path)
    await repo_a.init()
    await repo_b.init()
    try:
        meeting_id = f"meeting-{success_state}-monotonic"
        await repo_a.create_meeting(meeting_id, started_at=datetime.now(UTC))
        generation = await repo_a.update_meeting_state(
            meeting_id,
            state="ended",
            rag_projection_state=pending_state,  # type: ignore[arg-type]
        )
        assert generation is not None

        success_applied = await repo_a.set_meeting_rag_projection(
            meeting_id,
            state=success_state,  # type: ignore[arg-type]
            projected_at=datetime.now(UTC),
            expected_generation=generation,
        )
        late_failure_applied = await repo_b.set_meeting_rag_projection(
            meeting_id,
            state=failure_state,  # type: ignore[arg-type]
            error="late worker failure",
            retry_backoff=True,
            expected_generation=generation,
        )
        persisted = await repo_b.get_meeting(meeting_id)
        assert persisted is not None
        assert (
            success_applied,
            late_failure_applied,
            persisted.rag_projection_state,
            persisted.rag_projection_attempts,
            persisted.rag_projection_error,
        ) == (True, False, success_state, 0, None)
    finally:
        await asyncio.gather(repo_a.aclose(), repo_b.aclose())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambient_rag_success_rejects_late_failure_from_another_repository(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ambient-indexed-monotonic.db"
    repo_a = SQLiteRepository(db_path)
    repo_b = SQLiteRepository(db_path)
    await repo_a.init()
    await repo_b.init()
    try:
        segment_id = await repo_a.append_ambient_segment(
            audio_ref="/tmp/ambient-monotonic.wav",
            text="先成功后迟到失败",
            captured_at=datetime.now(UTC),
        )
        success_applied = await repo_a.set_ambient_rag_projection(
            segment_id,
            state="indexed",
            projected_at=datetime.now(UTC),
        )
        late_failure_applied = await repo_b.set_ambient_rag_projection(
            segment_id,
            state="index_failed",
            error="late worker failure",
            retry_backoff=True,
        )
        persisted = (await repo_b.list_ambient_segments(limit=10))[0]
        assert (
            success_applied,
            late_failure_applied,
            persisted.rag_projection_state,
            persisted.rag_projection_attempts,
            persisted.rag_projection_error,
        ) == (True, False, "indexed", 0, None)
    finally:
        await asyncio.gather(repo_a.aclose(), repo_b.aclose())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_speaker_registry_upsert(repo: SQLiteRepository) -> None:
    t0 = datetime.now(UTC)
    await repo.upsert_speaker("spk_A", captured_at=t0, label="老板")
    s = await repo.get_speaker("spk_A")
    assert s is not None
    assert s.n_samples == 1
    assert s.label == "老板"
    assert s.first_seen_at.replace(microsecond=0) == t0.replace(microsecond=0)

    # 再次出现，n_samples + 1，last_seen_at 更新
    t1 = t0 + timedelta(seconds=30)
    await repo.upsert_speaker("spk_A", captured_at=t1)
    s = await repo.get_speaker("spk_A")
    assert s is not None
    assert s.n_samples == 2
    assert s.last_seen_at.replace(microsecond=0) == t1.replace(microsecond=0)

    await repo.upsert_speaker("spk_B", captured_at=t1)
    all_speakers = await repo.list_speakers()
    assert {s.speaker_id for s in all_speakers} == {"spk_A", "spk_B"}
