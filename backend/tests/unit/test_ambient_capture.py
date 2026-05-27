"""AmbientCapturePipeline 单测。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline


@pytest.fixture
def ambient_pipeline(tmp_path: Path) -> AmbientCapturePipeline:
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
    )
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[
            TranscriptSegment(text="ambient hello", start_ms=0, end_ms=1000),
        ]
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-20260527")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])
    return AmbientCapturePipeline(
        settings=settings,
        stt=stt,
        rag=rag,
        meeting=meeting,
    )


@pytest.mark.asyncio
async def test_ambient_chunk_always_persisted_and_ingested(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    result = await ambient_pipeline.ingest_chunk(b"\x00" * 1000, sample_rate=16_000)
    assert result.audio_ref
    assert Path(result.audio_ref).exists()
    assert result.ambient_stored is True
    assert result.ambient_text == "ambient hello"
    ambient_pipeline._rag.ingest_ambient_segment.assert_awaited_once()  # type: ignore[attr-defined]
    ambient_pipeline._meeting.ingest_from_stt.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambient_with_meeting_overlay(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    seg = TranscriptSegment(text="hi", start_ms=0, end_ms=500, speaker_label="说话人1")
    ambient_pipeline._meeting.ingest_from_stt = AsyncMock(return_value=[seg])  # type: ignore[method-assign]
    result = await ambient_pipeline.ingest_chunk(
        b"\x00" * 1000,
        sample_rate=16_000,
        meeting_id="m-test",
    )
    assert result.ambient_stored is True
    assert len(result.meeting_segments) == 1
    ambient_pipeline._meeting.ingest_from_stt.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambient_stt_fail_still_saves_audio(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    ambient_pipeline._stt.transcribe = AsyncMock(side_effect=RuntimeError("stt down"))  # type: ignore[method-assign]
    result = await ambient_pipeline.ingest_chunk(b"\x01" * 500)
    assert Path(result.audio_ref).exists()
    assert result.ambient_stored is False
    ambient_pipeline._rag.ingest_ambient_segment.assert_not_awaited()  # type: ignore[attr-defined]
