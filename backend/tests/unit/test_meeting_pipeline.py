"""会议 Pipeline 单测：mock STT/Diarizer/RAG/LLM，验证 happy path + 边界。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.rag import BM25Rag
from app.adapters.repo.migrator import _DEFAULT_MIGRATIONS_DIR, run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.schemas.meeting import TranscriptSegment
from app.schemas.rag import RagChunk
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError


class FakeSTT:
    def __init__(self, scripted: list[list[TranscriptSegment]]) -> None:
        self._q = list(scripted)

    async def transcribe(
        self, audio_bytes: bytes, *, sample_rate: int = 16_000, language: str = "zh"
    ) -> list[TranscriptSegment]:
        if not self._q:
            return []
        return self._q.pop(0)


class FakeDiarizer:
    def __init__(self, ids: list[str | None]) -> None:
        self._q = list(ids)

    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> str | None:
        if not self._q:
            return None
        return self._q.pop(0)

    async def reset(self) -> None:
        return None


class FakeRag:
    def __init__(self) -> None:
        self.ingested: list[tuple[str, str, str]] = []
        self.ingest_generations: list[int | None] = []
        self.ambient_ingested: list[tuple[str, str | None]] = []
        self.deleted: list[str] = []
        self.delete_generations: list[int | None] = []
        self.fail_ingest = False
        self.fail_ambient_ingest = False
        self.fail_delete = False

    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        return "pdf-doc-id"

    async def ingest_meeting(
        self,
        meeting_id: str,
        transcript: str,
        title: str,
        *,
        projection_generation: int | None = None,
    ) -> str:
        if self.fail_ingest:
            raise RuntimeError("temporary RAG ingest failure")
        self.ingested.append((meeting_id, transcript, title))
        self.ingest_generations.append(projection_generation)
        return f"meeting-doc-{meeting_id}"

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        _ = (captured_at, audio_ref, speaker_id, speaker_label)
        if self.fail_ambient_ingest:
            raise RuntimeError("temporary ambient RAG ingest failure")
        self.ambient_ingested.append((text, operation_id))
        return "ambient-test"

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        return []

    async def delete(
        self,
        doc_id: str,
        *,
        projection_generation: int | None = None,
    ) -> None:
        if self.fail_delete:
            raise RuntimeError("temporary RAG delete failure")
        self.deleted.append(doc_id)
        self.delete_generations.append(projection_generation)


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> LLMResponse:
        self.calls.append({"messages": messages, **kwargs})
        return LLMResponse(
            content=self.content,
            model="MiniMax-M2.7",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=12.0,
        )

    async def chat_stream(self, _messages: list[ChatMessage], **_: Any):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


def _settings(tmp_path: Path) -> Settings:
    return Settings(storage_dir=tmp_path / "storage")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_chunk_assigns_speaker_labels(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT(
            [
                [TranscriptSegment(text="大家好", start_ms=0, end_ms=1500)],
                [TranscriptSegment(text="今天讨论 Q3 预算", start_ms=0, end_ms=2000)],
                [TranscriptSegment(text="我建议砍 30%", start_ms=0, end_ms=1800)],
            ]
        ),
        diarizer=FakeDiarizer(["spk-A", "spk-B", "spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM('{"summary":"x","sections":[{"heading":"a","bullets":["1","2"]}]}'),
    )
    await pipe.start_meeting("m1")
    s1 = await pipe.add_audio_chunk("m1", b"\x00" * 16_000)
    s2 = await pipe.add_audio_chunk("m1", b"\x00" * 16_000)
    s3 = await pipe.add_audio_chunk("m1", b"\x00" * 16_000)
    assert s1[0].speaker_label == "说话人1"
    assert s2[0].speaker_label == "说话人2"
    # 第三段又是 spk-A，应该回到说话人1
    assert s3[0].speaker_label == "说话人1"
    assert len(pipe.get_segments("m1")) == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_stt_does_not_block(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([[]]),
        diarizer=FakeDiarizer([None]),
        rag=FakeRag(),
        llm=FakeLLM("{}"),
    )
    out = await pipe.add_audio_chunk("m2", b"\x00" * 16_000)
    assert out == []
    assert pipe.get_segments("m2") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_generates_minutes_and_ingests_rag(tmp_path: Path) -> None:
    minutes_json = json.dumps(
        {
            "summary": "讨论 Q3 预算，决议砍 30%。",
            "sections": [
                {
                    "heading": "预算方案",
                    "bullets": ["原始方案 100", "建议砍 30%"],
                }
            ],
            "decisions": ["Q3 预算下调 30%"],
            "action_items": ["Alice 周五前出具修订版"],
        },
        ensure_ascii=False,
    )
    rag = FakeRag()
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT(
            [
                [TranscriptSegment(text="今天讨论 Q3 预算", start_ms=0, end_ms=2000)],
                [TranscriptSegment(text="我建议砍 30%", start_ms=0, end_ms=1800)],
            ]
        ),
        diarizer=FakeDiarizer(["spk-A", "spk-B"]),
        rag=rag,
        llm=FakeLLM(minutes_json),
    )
    await pipe.add_audio_chunk("m3", b"\x00" * 16_000)
    await pipe.add_audio_chunk("m3", b"\x00" * 16_000)

    minutes = await pipe.finalize_meeting("m3", title="预算评审")

    assert minutes.title == "预算评审"
    assert "Q3" in minutes.summary
    assert minutes.decisions == ["Q3 预算下调 30%"]
    assert minutes.action_items == ["Alice 周五前出具修订版"]
    assert "说话人1" in minutes.speakers and "说话人2" in minutes.speakers
    assert minutes.raw_transcript_ref is not None
    assert Path(minutes.raw_transcript_ref).exists()

    assert len(rag.ingested) == 1
    meeting_id, payload, title = rag.ingested[0]
    assert meeting_id == "m3"
    assert title == "预算评审"
    assert "Q3" in payload and "说话人1" in payload


@pytest.mark.asyncio
@pytest.mark.unit
async def test_meeting_rag_projection_failure_is_durable_and_repaired_after_restart(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "projection.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    migration = await run_migrations(settings.db_path)
    assert migration.errors == []
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    failing_rag = FakeRag()
    failing_rag.fail_ingest = True
    minutes_json = json.dumps(
        {
            "title": "可修复投影",
            "summary": "会议纪要已提交，但索引暂时失败。",
            "sections": [{"heading": "状态", "bullets": ["纪要成功", "稍后补索引"]}],
        },
        ensure_ascii=False,
    )
    first = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([[TranscriptSegment(text="持久纪要内容", start_ms=0, end_ms=1500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=failing_rag,
        llm=FakeLLM(minutes_json),
        repository=repo,
    )
    await first.start_meeting("m-rag-repair", title="投影修复")
    await first.add_audio_chunk("m-rag-repair", b"\x00" * 16_000)
    minutes = await first.finalize_meeting("m-rag-repair", title="投影修复")
    assert minutes.summary
    failed = await repo.get_meeting("m-rag-repair")
    assert failed is not None
    assert failed.minutes_status == "ok"
    assert failed.rag_projection_state == "index_failed"
    assert "temporary RAG ingest failure" in (failed.rag_projection_error or "")

    recovered_rag = FakeRag()
    restarted = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=recovered_rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    assert await restarted.repair_rag_projections() == (1, 1)
    repaired = await repo.get_meeting("m-rag-repair")
    assert repaired is not None
    assert repaired.rag_projection_state == "indexed"
    assert repaired.rag_projection_error is None
    assert repaired.rag_projected_at is not None
    assert recovered_rag.ingested[0][0] == "m-rag-repair"

    await repo.clear_meeting_outputs("m-rag-repair")
    recovered_rag.fail_delete = True
    assert await restarted.repair_rag_projections() == (1, 0)
    delete_failed = await repo.get_meeting("m-rag-repair")
    assert delete_failed is not None
    assert delete_failed.rag_projection_state == "delete_failed"
    assert delete_failed.rag_projection_attempts == 1
    assert delete_failed.rag_projection_next_retry_at is not None
    recovered_rag.fail_delete = False
    await repo.set_meeting_rag_projection("m-rag-repair", state="delete_pending")
    assert await restarted.repair_rag_projections() == (1, 1)
    deleted = await repo.get_meeting("m-rag-repair")
    assert deleted is not None
    assert deleted.rag_projection_state == "deleted"
    assert recovered_rag.deleted == ["meeting-m-rag-repair"]
    await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ambient_rag_projection_is_durable_backed_off_and_repairable(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "ambient-projection.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    captured_at = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    segment_id = await repo.append_ambient_segment(
        audio_ref="/tmp/ambient-repair.wav",
        text="需要恢复的环境记忆",
        captured_at=captured_at,
    )
    pending = await repo.list_ambient_segments(limit=10)
    assert pending[0].rag_projection_state == "index_pending"

    failing_rag = FakeRag()
    failing_rag.fail_ambient_ingest = True
    pipeline = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=failing_rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    assert await pipeline.repair_rag_projections() == (1, 0)
    failed = (await repo.list_ambient_segments(limit=10))[0]
    assert failed.id == segment_id
    assert failed.rag_projection_state == "index_failed"
    assert failed.rag_projection_attempts == 1
    assert failed.rag_projection_next_retry_at is not None

    await repo.set_ambient_rag_projection(segment_id, state="index_pending")
    recovered_rag = FakeRag()
    restarted = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=recovered_rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    assert await restarted.repair_rag_projections() == (1, 1)
    repaired = (await repo.list_ambient_segments(limit=10))[0]
    assert repaired.rag_projection_state == "indexed"
    assert repaired.rag_projection_error is None
    assert repaired.rag_projected_at is not None
    assert recovered_rag.ambient_ingested == [
        ("需要恢复的环境记忆", f"ambient-segment:{segment_id}")
    ]
    await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("side_effect_fails", [False, True])
async def test_stale_index_repair_cannot_overwrite_newer_delete_intent(
    tmp_path: Path,
    side_effect_fails: bool,
) -> None:
    settings = Settings(
        db_path=tmp_path / "stale-index.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "stale-index-repair"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="旧索引")
    await repo.append_meeting_segment(
        meeting_id,
        TranscriptSegment(text="旧纪要内容", start_ms=0, end_ms=800),
        captured_at=datetime.now(UTC),
    )
    minutes_json = json.dumps(
        {
            "meeting_id": meeting_id,
            "title": "旧索引",
            "duration_sec": 1,
            "summary": "旧纪要内容",
            "sections": [],
            "decisions": [],
            "action_items": [],
        },
        ensure_ascii=False,
    )
    await repo.update_meeting_state(
        meeting_id,
        state="finalized",
        minutes_json=minutes_json,
        minutes_status="ok",
        rag_projection_state="index_pending",
    )
    loaded = await repo.get_meeting(meeting_id)
    assert loaded is not None
    stale_generation = loaded.rag_projection_generation
    rag = FakeRag()

    async def race_ingest(
        target_id: str,
        transcript: str,
        title: str,
        *,
        projection_generation: int | None = None,
    ) -> str:
        _ = transcript, title
        assert target_id == meeting_id
        assert projection_generation == stale_generation
        await repo.clear_meeting_outputs(meeting_id)
        if side_effect_fails:
            raise RuntimeError("stale index failed after clear")
        return f"meeting-{meeting_id}"

    rag.ingest_meeting = race_ingest  # type: ignore[method-assign]
    pipeline = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    try:
        assert await pipeline.repair_rag_projections() == (1, 0)
        current = await repo.get_meeting(meeting_id)
        assert current is not None
        assert current.rag_projection_generation == stale_generation + 1
        assert current.rag_projection_state == "delete_pending"
        assert current.rag_projection_error is None
    finally:
        await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("side_effect_fails", [False, True])
async def test_stale_delete_repair_cannot_overwrite_newer_finalize_intent(
    tmp_path: Path,
    side_effect_fails: bool,
) -> None:
    settings = Settings(
        db_path=tmp_path / "stale-delete.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    repo = SQLiteRepository(settings.db_path)
    await repo.init()
    meeting_id = "stale-delete-repair"
    await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="清理后重生")
    await repo.clear_meeting_outputs(meeting_id)
    loaded = await repo.get_meeting(meeting_id)
    assert loaded is not None
    stale_generation = loaded.rag_projection_generation
    rag = FakeRag()

    async def race_delete(
        doc_id: str,
        *,
        projection_generation: int | None = None,
    ) -> None:
        assert doc_id == f"meeting-{meeting_id}"
        assert projection_generation == stale_generation
        await repo.update_meeting_state(
            meeting_id,
            state="finalized",
            minutes_json=json.dumps(
                {
                    "meeting_id": meeting_id,
                    "title": "新纪要",
                    "duration_sec": 1,
                    "summary": "新一代内容",
                    "sections": [],
                    "decisions": [],
                    "action_items": [],
                },
                ensure_ascii=False,
            ),
            minutes_status="ok",
            rag_projection_state="index_pending",
        )
        if side_effect_fails:
            raise RuntimeError("stale delete failed after finalize")

    rag.delete = race_delete  # type: ignore[method-assign]
    pipeline = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    try:
        assert await pipeline.repair_rag_projections() == (1, 0)
        current = await repo.get_meeting(meeting_id)
        assert current is not None
        assert current.rag_projection_generation == stale_generation + 1
        assert current.rag_projection_state == "index_pending"
        assert current.rag_projection_error is None
        assert current.minutes_status == "ok"
    finally:
        await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_v37_ambient_reconciliation_repairs_crash_gap_without_legacy_duplicate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v37-ambient.db"
    v37_catalog = tmp_path / "migrations-v37"
    v37_catalog.mkdir()
    for source in _DEFAULT_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"):
        if int(source.name.split("_", 1)[0]) <= 37:
            shutil.copy2(source, v37_catalog / source.name)
    assert (await run_migrations(db_path, migrations_dir=v37_catalog)).current_version == 37
    captured_at = "2026-07-12T09:00:00+00:00"
    async with aiosqlite.connect(db_path) as conn:
        indexed_cur = await conn.execute(
            """INSERT INTO ambient_segments
               (audio_ref, text, captured_at, tenant_id, device_id, owner_id)
               VALUES (?, ?, ?, 'legacy-local', 'legacy-local', 'legacy-local')""",
            ("/legacy/already-indexed.wav", "legacy evidence already indexed", captured_at),
        )
        indexed_id = int(indexed_cur.lastrowid or 0)
        missing_cur = await conn.execute(
            """INSERT INTO ambient_segments
               (audio_ref, text, captured_at, tenant_id, device_id, owner_id)
               VALUES (?, ?, ?, 'legacy-local', 'legacy-local', 'legacy-local')""",
            ("/legacy/crash-gap.wav", "legacy evidence missing projection", captured_at),
        )
        missing_id = int(missing_cur.lastrowid or 0)
        await conn.commit()

    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    rag = BM25Rag(settings)
    await rag.ingest_ambient_segment(
        "legacy evidence already indexed",
        captured_at=captured_at,
        audio_ref="/legacy/already-indexed.wav",
    )
    # v37 audio retention can clear the DB path after indexing. Reconciliation
    # must still use captured_at + normalized text and avoid a duplicate.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE ambient_segments SET audio_ref = '' WHERE id = ?",
            (indexed_id,),
        )
        await conn.commit()

    migration = await run_migrations(db_path)
    assert migration.errors == [] and migration.current_version == 38
    repo = SQLiteRepository(db_path)
    await repo.init()
    pipeline = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=rag,
        llm=FakeLLM("{}"),
        repository=repo,
    )
    try:
        pending = await repo.list_ambient_segments(limit=10)
        assert {row.rag_projection_state for row in pending} == {"reconcile_pending"}
        assert await pipeline.repair_rag_projections() == (2, 2)
        reconciled = await repo.list_ambient_segments(limit=10)
        assert {row.rag_projection_state for row in reconciled} == {"indexed"}

        snapshot = rag._snapshot_for_scope(("legacy-local", "legacy-local"), force=True)
        ambient_chunks = [
            chunk for chunk in snapshot.chunks if dict(chunk.metadata).get("kind") == "ambient"
        ]
        assert [chunk.text for chunk in ambient_chunks].count(
            "legacy evidence already indexed"
        ) == 1
        assert [chunk.text for chunk in ambient_chunks].count(
            "legacy evidence missing projection"
        ) == 1
        repaired = next(
            chunk for chunk in ambient_chunks if chunk.text == "legacy evidence missing projection"
        )
        assert dict(repaired.metadata)["operation_id"] == f"ambient-segment:{missing_id}"
    finally:
        await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_uses_minutes_max_tokens(tmp_path: Path) -> None:
    minutes_json = json.dumps(
        {
            "summary": "讨论 TV 会议录音与纪要生成。",
            "sections": [{"heading": "修复", "bullets": ["限制 token", "恢复纪要"]}],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(minutes_json)
    pipe = MeetingPipeline(
        settings=Settings(storage_dir=tmp_path / "storage", minutes_max_tokens=12_000),
        stt=FakeSTT([[TranscriptSegment(text="修复电视会议", start_ms=0, end_ms=2000)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=llm,
    )
    await pipe.add_audio_chunk("m-token", b"\x00" * 16_000)

    await pipe.finalize_meeting("m-token", title="TV 会议修复")

    assert llm.calls
    assert llm.calls[0]["max_tokens"] == 12_000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_without_segments_raises(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([]),
        diarizer=FakeDiarizer([]),
        rag=FakeRag(),
        llm=FakeLLM("{}"),
    )
    await pipe.start_meeting("m4")
    with pytest.raises(MeetingPipelineError, match="no segments"):
        await pipe.finalize_meeting("m4", title="空会议")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_invalid_json_raises(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="hi", start_ms=0, end_ms=500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM("not json at all"),
    )
    await pipe.add_audio_chunk("m5", b"\x00" * 16_000)
    with pytest.raises(MeetingPipelineError, match="JSON parse"):
        await pipe.finalize_meeting("m5", title="坏 JSON")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_missing_summary_raises(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="hi", start_ms=0, end_ms=500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM('{"sections": []}'),
    )
    await pipe.add_audio_chunk("m6", b"\x00" * 16_000)
    with pytest.raises(MeetingPipelineError, match="missing key"):
        await pipe.finalize_meeting("m6", title="缺字段")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pipeline_isolates_meetings(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT(
            [
                [TranscriptSegment(text="m7-1", start_ms=0, end_ms=500)],
                [TranscriptSegment(text="m8-1", start_ms=0, end_ms=500)],
            ]
        ),
        diarizer=FakeDiarizer(["A", "B"]),
        rag=FakeRag(),
        llm=FakeLLM("{}"),
    )
    await pipe.add_audio_chunk("m7", b"\x00" * 16_000)
    await pipe.add_audio_chunk("m8", b"\x00" * 16_000)
    assert len(pipe.get_segments("m7")) == 1
    assert len(pipe.get_segments("m8")) == 1
    assert pipe.get_segments("m7")[0].text == "m7-1"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_end_meeting_blocks_further_chunks(tmp_path: Path) -> None:
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="before", start_ms=0, end_ms=500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM("{}"),
    )
    await pipe.start_meeting("m-end")
    await pipe.add_audio_chunk("m-end", b"\x00" * 16_000)
    await pipe.end_meeting("m-end")
    with pytest.raises(MeetingPipelineError, match="already ended"):
        await pipe.add_audio_chunk("m-end", b"\x00" * 16_000)
