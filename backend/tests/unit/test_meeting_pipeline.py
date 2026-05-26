"""会议 Pipeline 单测：mock STT/Diarizer/RAG/LLM，验证 happy path + 边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
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

    async def identify(self, audio_bytes: bytes, *, sample_rate: int = 16_000) -> str | None:
        if not self._q:
            return None
        return self._q.pop(0)

    async def reset(self) -> None:
        return None


class FakeRag:
    def __init__(self) -> None:
        self.ingested: list[tuple[str, str, str]] = []

    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        return "pdf-doc-id"

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        self.ingested.append((meeting_id, transcript, title))
        return f"meeting-doc-{meeting_id}"

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        return []

    async def delete(self, doc_id: str) -> None:
        return None


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
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
