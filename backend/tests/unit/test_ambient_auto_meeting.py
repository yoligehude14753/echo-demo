"""Ambient → AutoMeetingDetector → MeetingPipeline 端到端单测。"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.speaker_registry import SpeakerRegistry

# 复用 test_meeting_pipeline fakes
from tests.unit.test_meeting_pipeline import (  # type: ignore[attr-defined]
    FakeLLM,
    FakeRag as MeetingFakeRag,
)


class StaticSTT:
    """每 chunk 都返回一段对应 duration_ms 的 segment。"""

    def __init__(self, default_text: str = "spoke", default_duration_ms: int = 4_000) -> None:
        self._text = default_text
        self._dur = default_duration_ms

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        return [TranscriptSegment(text=self._text, start_ms=0, end_ms=self._dur)]


class ScriptedDiarizer:
    def __init__(self, ids: list[str | None]) -> None:
        self._q = list(ids)

    async def identify(self, *_a: Any, **_kw: Any) -> str | None:
        if not self._q:
            return None
        return self._q.pop(0)

    async def reset(self) -> None:
        return None


class AmbientFakeRag(MeetingFakeRag):
    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> str:
        return "ambient-doc"


class FakeBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, ev: Any) -> None:
        self.published.append(ev)


def _settings(tmp_path: Path) -> Settings:
    return Settings(storage_dir=tmp_path / "storage", rag_index_dir=tmp_path / "rag")


async def _make_pipelines(
    tmp_path: Path,
    diarizer_ids: list[str | None],
) -> tuple[AmbientCapturePipeline, MeetingPipeline, AutoMeetingDetector, FakeBus, SQLiteRepository]:
    repo = SQLiteRepository(tmp_path / "echo.db")
    await repo.init()
    bus = FakeBus()
    diar = ScriptedDiarizer(list(diarizer_ids))
    registry = SpeakerRegistry(repo)
    detector = AutoMeetingDetector(min_active_seconds=4.0, window_s=30.0, cooldown_s=5.0)
    rag = AmbientFakeRag()
    meeting = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=StaticSTT(),  # type: ignore[arg-type]
        diarizer=diar,
        rag=rag,  # type: ignore[arg-type]
        llm=FakeLLM(json.dumps({"summary": "x", "sections": [{"heading": "x", "bullets": ["a"]}]})),
        event_bus=bus,  # type: ignore[arg-type]
        repository=repo,
    )
    ambient = AmbientCapturePipeline(
        settings=_settings(tmp_path),
        stt=StaticSTT(),  # type: ignore[arg-type]
        rag=rag,  # type: ignore[arg-type]
        meeting=meeting,
        repository=repo,
        diarizer=diar,
        speaker_registry=registry,
        auto_meeting_detector=detector,
        event_bus=bus,  # type: ignore[arg-type]
    )
    return ambient, meeting, detector, bus, repo


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_speakers_auto_start_meeting(tmp_path: Path) -> None:
    ambient, meeting, detector, bus, repo = await _make_pipelines(
        tmp_path, ["spk_A", "spk_B", "spk_A"]
    )
    try:
        # chunk 1: speaker A (4s)
        r1 = await ambient.ingest_chunk(b"\x00" * 32_000)
        assert r1.meeting_id is None  # 还没触发
        # chunk 2: speaker B (4s) → distinct=2, active=8s → 触发
        r2 = await ambient.ingest_chunk(b"\x00" * 32_000)
        assert r2.meeting_id is not None
        assert r2.meeting_id.startswith("auto-")
        # 该 chunk 的 STT segs 已经叠加进 auto meeting
        assert len(r2.meeting_segments) == 1

        # chunk 3: 同一会议中继续
        r3 = await ambient.ingest_chunk(b"\x00" * 32_000)
        assert r3.meeting_id == r2.meeting_id
        assert len(r3.meeting_segments) == 1

        # SQLite 里 meeting 已落库
        rec = await repo.get_meeting(r2.meeting_id)
        assert rec is not None
        assert rec.state == "in_meeting"
        assert rec.auto_started is True
        segs = await repo.list_meeting_segments(r2.meeting_id)
        assert len(segs) == 2  # chunk 2 + chunk 3

        # 总线上有 meeting.started + meeting.auto_detected + meeting.segment 事件
        types = [e.type for e in bus.published]
        assert "meeting.started" in types
        assert "meeting.auto_detected" in types
        assert "meeting.segment" in types
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_meeting_id_overrides_auto(tmp_path: Path) -> None:
    ambient, meeting, detector, bus, repo = await _make_pipelines(
        tmp_path, ["spk_A", "spk_B", "spk_A"]
    )
    try:
        # 用户手动开始会议
        await meeting.start_meeting("user-mtg-1")
        # chunk 1, manual → detector idle
        r1 = await ambient.ingest_chunk(b"\x00" * 32_000, meeting_id="user-mtg-1")
        assert r1.meeting_id == "user-mtg-1"
        # chunk 2, manual → 仍叠加到手动会议
        r2 = await ambient.ingest_chunk(b"\x00" * 32_000, meeting_id="user-mtg-1")
        assert r2.meeting_id == "user-mtg-1"
        # detector 自己应该没起任何 auto meeting
        assert detector.active_meeting_id is None
        # SQLite 只有 user-mtg-1，没 auto-*
        all_meetings = await repo.list_meetings()
        ids = [m.id for m in all_meetings]
        assert "user-mtg-1" in ids
        assert not any(i.startswith("auto-") for i in ids)
    finally:
        await repo.aclose()
