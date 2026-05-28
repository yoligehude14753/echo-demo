"""Ambient 主链路接入 Diarizer + SpeakerRegistry 后的端到端单测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline
from app.use_cases.speaker_registry import SpeakerRegistry


class FakeSTT:
    def __init__(self, scripted: list[list[TranscriptSegment]]) -> None:
        self._q = list(scripted)

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
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
        self.ingested: list[dict[str, Any]] = []

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> str:
        self.ingested.append(
            {
                "text": text,
                "captured_at": captured_at,
                "audio_ref": audio_ref,
                "speaker_id": speaker_id,
                "speaker_label": speaker_label,
            }
        )
        return f"ambient-doc-{len(self.ingested)}"


class FakeMeeting:
    """空 meeting overlay：本测试只关心 ambient 主链路。"""

    async def ingest_from_stt(
        self, meeting_id: str, audio_bytes: bytes, stt_segs: list[TranscriptSegment], **kw: Any
    ) -> list[TranscriptSegment]:
        return []


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=0,
        ambient_min_speech_frame_ratio=0.0,
        ambient_min_stt_chars=0,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambient_chunk_records_speaker(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        registry = SpeakerRegistry(repo)
        rag = FakeRag()
        pipeline = AmbientCapturePipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT([[TranscriptSegment(text="hello", start_ms=0, end_ms=500)]]),
            rag=rag,  # type: ignore[arg-type]
            meeting=FakeMeeting(),  # type: ignore[arg-type]
            repository=repo,
            diarizer=FakeDiarizer(["spk_A"]),
            speaker_registry=registry,
        )
        result = await pipeline.ingest_chunk(b"\x00\x00" * 16_000)
        assert result.ambient_stored is True
        assert result.ambient_text == "hello"
        assert result.speaker_id == "spk_A"
        assert result.speaker_label == "说话人1"

        # RAG metadata 含 speaker
        assert rag.ingested[0]["speaker_id"] == "spk_A"
        assert rag.ingested[0]["speaker_label"] == "说话人1"

        # SQLite ambient_segments 行
        rows = await repo.list_ambient_segments(limit=10)
        assert len(rows) == 1
        assert rows[0].speaker_id == "spk_A"
        assert rows[0].speaker_label == "说话人1"

        # speakers 表
        s = await repo.get_speaker("spk_A")
        assert s is not None
        assert s.n_samples == 1
        assert s.label == "说话人1"
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_chunks_same_speaker_increments(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        registry = SpeakerRegistry(repo)
        pipeline = AmbientCapturePipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT(
                [
                    [TranscriptSegment(text="hi", start_ms=0, end_ms=500)],
                    [TranscriptSegment(text="again", start_ms=0, end_ms=500)],
                    [TranscriptSegment(text="bonjour", start_ms=0, end_ms=500)],
                ]
            ),
            rag=FakeRag(),  # type: ignore[arg-type]
            meeting=FakeMeeting(),  # type: ignore[arg-type]
            repository=repo,
            diarizer=FakeDiarizer(["spk_A", "spk_A", "spk_B"]),
            speaker_registry=registry,
        )
        for _ in range(3):
            await pipeline.ingest_chunk(b"\x00" * 32_000)

        a = await repo.get_speaker("spk_A")
        b = await repo.get_speaker("spk_B")
        assert a is not None and a.n_samples == 2 and a.label == "说话人1"
        assert b is not None and b.n_samples == 1 and b.label == "说话人2"

        rows = await repo.list_ambient_segments(limit=10)
        assert len(rows) == 3
        # ORDER BY captured_at DESC → 最新（spk_B）在前
        labels = {r.speaker_label for r in rows}
        assert labels == {"说话人1", "说话人2"}
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambient_without_diarizer_still_works(tmp_path: Path) -> None:
    """diarizer/registry 为 None 时 ambient 主链路仍应正常落盘 + STT + RAG。"""
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipeline = AmbientCapturePipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT([[TranscriptSegment(text="standalone", start_ms=0, end_ms=400)]]),
            rag=FakeRag(),  # type: ignore[arg-type]
            meeting=FakeMeeting(),  # type: ignore[arg-type]
            repository=repo,
            diarizer=None,
            speaker_registry=None,
        )
        result = await pipeline.ingest_chunk(b"\x00\x00" * 16_000)
        assert result.ambient_stored is True
        assert result.speaker_id is None
        assert result.speaker_label is None

        rows = await repo.list_ambient_segments()
        assert len(rows) == 1
        assert rows[0].speaker_id is None
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_diarizer_failure_does_not_block_ambient(tmp_path: Path) -> None:
    """diarizer 抛错时不能阻断 ambient → STT/RAG 仍持久化。"""

    class BrokenDiarizer:
        async def identify(self, *_a: Any, **_kw: Any) -> str | None:
            raise RuntimeError("upstream is down")

        async def reset(self) -> None:
            return None

    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    try:
        pipeline = AmbientCapturePipeline(
            settings=_settings(tmp_path),
            stt=FakeSTT([[TranscriptSegment(text="text", start_ms=0, end_ms=400)]]),
            rag=FakeRag(),  # type: ignore[arg-type]
            meeting=FakeMeeting(),  # type: ignore[arg-type]
            repository=repo,
            diarizer=BrokenDiarizer(),  # type: ignore[arg-type]
            speaker_registry=SpeakerRegistry(repo),
        )
        result = await pipeline.ingest_chunk(b"\x00" * 32_000)
        assert result.ambient_stored is True
        assert result.speaker_id is None
        # registry 把 None 翻成"未识别"
        assert result.speaker_label == "未识别"
    finally:
        await repo.aclose()
