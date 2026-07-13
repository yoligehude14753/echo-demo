"""MeetingPipeline ↔ SQLiteRepository 整合：持久化 + hydrate（断电恢复）。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError

# 复用 test_meeting_pipeline 里的 fakes
from tests.unit.test_meeting_pipeline import (  # type: ignore[attr-defined]
    FakeDiarizer,
    FakeLLM,
    FakeRag,
    FakeSTT,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(storage_dir=tmp_path, rag_index_dir=tmp_path / "rag")


def _build_pipeline(tmp_path: Path, repo: SQLiteRepository) -> MeetingPipeline:
    return MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="hello", start_ms=0, end_ms=400)]]),
        diarizer=FakeDiarizer(["spk_A"]),
        rag=FakeRag(),
        llm=FakeLLM(
            json.dumps({"summary": "ok", "sections": [{"heading": "x", "bullets": ["a"]}]})
        ),
        repository=repo,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_meeting_persists_record(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipe = _build_pipeline(tmp_path, repo)
        await pipe.start_meeting("m1", title="Q3 review")
        rec = await repo.get_meeting("m1")
        assert rec is not None
        assert rec.state == "in_meeting"
        assert rec.title == "Q3 review"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_chunk_persists_segments_and_labels(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipe = _build_pipeline(tmp_path, repo)
        await pipe.start_meeting("m1")
        out = await pipe.add_audio_chunk("m1", b"\x00\x00" * 16_000)
        assert len(out) == 1

        rows = await repo.list_meeting_segments("m1")
        assert len(rows) == 1
        assert rows[0].text == "hello"
        assert rows[0].speaker_id == "spk_A"
        assert rows[0].speaker_label == "说话人1"

        labels = await repo.get_meeting_speaker_labels("m1")
        assert labels == {"spk_A": "说话人1"}
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_end_meeting_updates_state(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipe = _build_pipeline(tmp_path, repo)
        await pipe.start_meeting("m1")
        await pipe.end_meeting("m1")
        rec = await repo.get_meeting("m1")
        assert rec is not None
        assert rec.state == "ended"
        assert rec.ended_at is not None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_persists_minutes(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipe = _build_pipeline(tmp_path, repo)
        await pipe.start_meeting("m1")
        await pipe.add_audio_chunk("m1", b"\x00\x00" * 16_000)
        minutes = await pipe.finalize_meeting("m1", title="Final")
        assert minutes.summary == "ok"

        rec = await repo.get_meeting("m1")
        assert rec is not None
        assert rec.state == "finalized"
        assert rec.title == "Final"
        assert rec.finalized_at is not None
        assert rec.minutes_json is not None
        loaded = json.loads(rec.minutes_json)
        assert loaded["summary"] == "ok"
        assert rec.raw_transcript_ref is not None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_emits_tts_suggested(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    events: list = []

    class FakeBus:
        async def publish(self, ev) -> None:  # type: ignore[no-untyped-def]
            events.append(ev)

    try:
        pipe = MeetingPipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT([[TranscriptSegment(text="hi", start_ms=0, end_ms=400)]]),
            diarizer=FakeDiarizer(["spk_A"]),
            rag=FakeRag(),
            llm=FakeLLM(
                json.dumps(
                    {"summary": "Q3 销售上调", "sections": [{"heading": "x", "bullets": ["a"]}]}
                )
            ),
            event_bus=FakeBus(),  # type: ignore[arg-type]
            repository=repo,
        )
        await pipe.start_meeting("m1")
        await pipe.add_audio_chunk("m1", b"\x00\x00" * 16_000)
        await pipe.finalize_meeting("m1", title="Q3 例会")

        tts_evs = [e for e in events if e.type == "tts.suggested"]
        assert len(tts_evs) == 1
        assert tts_evs[0].meeting_id == "m1"
        assert "Q3 例会" in tts_evs[0].payload["text"]
        assert "Q3 销售上调" in tts_evs[0].payload["text"]
        assert tts_evs[0].payload["kind"] == "minutes"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_resumes_in_progress_meetings(tmp_path: Path) -> None:
    """模拟断电：进程 1 写入，进程 2 hydrate 后能 finalize。"""
    db_path = tmp_path / "echo.db"

    # 进程 1
    repo1 = SQLiteRepository(db_path)
    await repo1.init()
    try:
        pipe1 = _build_pipeline(tmp_path, repo1)
        await pipe1.start_meeting("m1", title="Will resume")
        await pipe1.add_audio_chunk("m1", b"\x00\x00" * 16_000)
        # 模拟"还没 end"就崩了
    finally:
        await repo1.aclose()

    # 进程 2：新的 repo + pipeline 实例
    repo2 = SQLiteRepository(db_path)
    await repo2.init()
    try:
        pipe2 = MeetingPipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT([[TranscriptSegment(text="resumed", start_ms=0, end_ms=400)]]),
            diarizer=FakeDiarizer(["spk_A"]),
            rag=FakeRag(),
            llm=FakeLLM(
                json.dumps(
                    {"summary": "after restart", "sections": [{"heading": "x", "bullets": ["a"]}]}
                )
            ),
            repository=repo2,
        )
        n = await pipe2.hydrate_from_repo()
        assert n == 1
        assert pipe2.get_segments("m1")[0].text == "hello"
        assert pipe2._speaker_labels["m1"] == {"spk_A": "说话人1"}

        # 继续追加，labels 不重新编号
        await pipe2.add_audio_chunk("m1", b"\x00\x00" * 16_000)
        segs = pipe2.get_segments("m1")
        assert len(segs) == 2
        assert segs[1].speaker_label == "说话人1"

        # finalize 也能完成
        m = await pipe2.finalize_meeting("m1", title="Resumed")
        assert m.summary == "after restart"
    finally:
        await repo2.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_ignores_finalized_meetings(tmp_path: Path) -> None:
    db_path = tmp_path / "echo.db"
    repo1 = SQLiteRepository(db_path)
    await repo1.init()
    try:
        await repo1.create_meeting("done", started_at=datetime.now(UTC))
        await repo1.update_meeting_state("done", state="finalized", finalized_at=datetime.now(UTC))
    finally:
        await repo1.aclose()

    repo2 = SQLiteRepository(db_path)
    await repo2.init()
    try:
        pipe = _build_pipeline(tmp_path, repo2)
        n = await pipe.hydrate_from_repo()
        assert n == 0
    finally:
        await repo2.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_refreshes_cross_instance_segments_with_stable_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment committed while B refreshes is included once; later appends fail."""

    db_path = tmp_path / "cross-instance-finalize.db"
    repo_a = SQLiteRepository(db_path)
    repo_b = SQLiteRepository(db_path)
    await repo_a.init()
    await repo_b.init()
    llm = FakeLLM(
        json.dumps(
            {
                "summary": "完整",
                "sections": [{"heading": "内容", "bullets": ["A", "B"]}],
            },
            ensure_ascii=False,
        )
    )
    pipe_a = _build_pipeline(tmp_path, repo_a)
    pipe_b = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=FakeRag(),
        llm=llm,
        repository=repo_b,
    )
    snapshot_entered = asyncio.Event()
    release_snapshot = asyncio.Event()
    original_snapshot = repo_b.snapshot_meeting_segments_for_finalize

    async def latched_snapshot(
        meeting_id: str,
        *,
        ended_at: datetime,
    ) -> list[TranscriptSegment]:
        snapshot_entered.set()
        await release_snapshot.wait()
        return await original_snapshot(meeting_id, ended_at=ended_at)

    monkeypatch.setattr(repo_b, "snapshot_meeting_segments_for_finalize", latched_snapshot)
    try:
        await pipe_a.start_meeting("shared", title="并发会议")
        assert await pipe_b.hydrate_from_repo() == 1
        await pipe_b.append_segment(
            "shared",
            TranscriptSegment(text="B 本地段", start_ms=0, end_ms=500, speaker_id="b"),
        )

        finalize_task = asyncio.create_task(
            pipe_b.finalize_meeting("shared", title="并发会议", commit=False)
        )
        await snapshot_entered.wait()
        await pipe_a.append_segment(
            "shared",
            TranscriptSegment(text="A 并发段", start_ms=600, end_ms=1000, speaker_id="a"),
        )
        release_snapshot.set()
        await finalize_task

        assert len(llm.calls) == 1
        prompt = "\n".join(str(message.content) for message in llm.calls[0]["messages"])
        assert prompt.count("B 本地段") == 1
        assert prompt.count("A 并发段") == 1
        assert [segment.text for segment in pipe_b.get_segments("shared")] == [
            "B 本地段",
            "A 并发段",
        ]

        with pytest.raises(MeetingPipelineError, match="not active"):
            await pipe_a.append_segment(
                "shared",
                TranscriptSegment(text="fence 后", start_ms=1100, end_ms=1200),
            )
        assert [segment.text for segment in await repo_a.list_meeting_segments("shared")] == [
            "B 本地段",
            "A 并发段",
        ]
    finally:
        release_snapshot.set()
        await asyncio.gather(repo_a.aclose(), repo_b.aclose())
