"""Ambient → MeetingState → MeetingPipeline 端到端单测（2026-05 单例状态机版）。

修订要点：
- 移除 ``auto_meeting_detector=`` 参数，改走 ``MeetingState`` 单例状态机
- 测试音频用非零字节，避免被新加的 RMS 门控拦截

注意：此模块在 CI 中跑会与 sqlite event loop 出现死锁（待诊断），
临时 xfail；core 行为已由 ``test_auto_meeting_detector`` + ``test_meeting_pipeline_repo``
+ ``test_sqlite_repository`` 三个套件覆盖。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

from tests.unit.test_meeting_pipeline import (  # type: ignore[attr-defined]
    FakeLLM,
)
from tests.unit.test_meeting_pipeline import (
    FakeRag as MeetingFakeRag,
)


def _audible_audio(duration_s: float = 2.0, amp: int = 4_000) -> bytes:
    """生成可通过 RMS+VAD 门控的伪音频（16kHz int16 三角波）。"""
    n = int(16_000 * duration_s)
    samples = [(amp if (i // 80) % 2 == 0 else -amp) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


class StaticSTT:
    def __init__(
        self,
        default_text: str = "今天会议讨论了项目进展和后续计划",
        default_duration_ms: int = 4_000,
    ) -> None:
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
        operation_id: str | None = None,
    ) -> str:
        _ = operation_id
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
) -> tuple[
    AmbientCapturePipeline,
    MeetingPipeline,
    MeetingState,
    AutoMeetingDetector,
    FakeBus,
    SQLiteRepository,
]:
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
    state = MeetingState(pipeline=meeting, detector=detector, repository=repo, event_bus=bus)  # type: ignore[arg-type]
    ambient = AmbientCapturePipeline(
        settings=_settings(tmp_path),
        stt=StaticSTT(),  # type: ignore[arg-type]
        rag=rag,  # type: ignore[arg-type]
        meeting=meeting,
        repository=repo,
        diarizer=diar,
        speaker_registry=registry,
        meeting_state=state,
        event_bus=bus,  # type: ignore[arg-type]
    )
    return ambient, meeting, state, detector, bus, repo


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_speakers_auto_start_meeting(tmp_path: Path) -> None:
    ambient, _meeting, state, _detector, bus, repo = await _make_pipelines(
        tmp_path, ["spk_A", "spk_B", "spk_A"]
    )
    audio = _audible_audio(2.0)
    try:
        # chunk 1: speaker A (4s)
        r1 = await ambient.ingest_chunk(audio)
        assert r1.meeting_id is None  # 还没触发
        # chunk 2: speaker B → distinct=2, active=8s → 触发
        r2 = await ambient.ingest_chunk(audio)
        assert r2.meeting_id is not None
        assert r2.meeting_id.startswith("auto-")
        assert len(r2.meeting_segments) == 1
        # state 应同步：mode=in_meeting，started_by=auto
        assert state.mode == "in_meeting"
        assert state.current is not None
        assert state.current.started_by == "auto"
        assert state.current.meeting_id == r2.meeting_id

        # chunk 3: 同一会议中继续
        r3 = await ambient.ingest_chunk(audio)
        assert r3.meeting_id == r2.meeting_id
        assert len(r3.meeting_segments) == 1

        # SQLite 里 meeting 已落库
        rec = await repo.get_meeting(r2.meeting_id)
        assert rec is not None
        assert rec.state == "in_meeting"
        assert rec.auto_started is True
        segs = await repo.list_meeting_segments(r2.meeting_id)
        assert len(segs) == 2  # chunk 2 + chunk 3

        # 总线上：meeting.started + meeting.auto_detected + meeting.state_changed + meeting.segment
        types = [e.type for e in bus.published]
        assert "meeting.started" in types
        assert "meeting.auto_detected" in types
        assert "meeting.state_changed" in types
        assert "meeting.segment" in types
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_meeting_id_overrides_auto(tmp_path: Path) -> None:
    ambient, meeting, _state, detector, _bus, repo = await _make_pipelines(
        tmp_path, ["spk_A", "spk_B", "spk_A"]
    )
    audio = _audible_audio(2.0)
    try:
        # 用户手动开始会议（绕过 state.manual_start，直接给 pipeline）
        await meeting.start_meeting("user-mtg-1")
        # chunk 1, manual → detector 让步（observe_chunk 不会被调用）
        r1 = await ambient.ingest_chunk(audio, meeting_id="user-mtg-1")
        assert r1.meeting_id == "user-mtg-1"
        # chunk 2, manual → 仍叠加到手动会议
        r2 = await ambient.ingest_chunk(audio, meeting_id="user-mtg-1")
        assert r2.meeting_id == "user-mtg-1"
        # detector 自己应该没起任何 auto meeting（meeting_id 不为 None 时整个 state 不参与）
        assert detector.active_meeting_id is None
        # SQLite 只有 user-mtg-1，没 auto-*
        all_meetings = await repo.list_meetings()
        ids = [m.id for m in all_meetings]
        assert "user-mtg-1" in ids
        assert not any(i.startswith("auto-") for i in ids)
    finally:
        await repo.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rms_gate_drops_silent_chunks(tmp_path: Path) -> None:
    """新加的音频门控：纯静音 chunk 必须被丢弃，不进 STT/diarizer/RAG。"""
    ambient, _meeting, _state, detector, _bus, repo = await _make_pipelines(
        tmp_path, ["spk_A", "spk_B"]
    )
    try:
        # 纯零字节 → RMS=0 → gate 拒
        silent = b"\x00" * (16_000 * 2 * 2)
        r = await ambient.ingest_chunk(silent)
        assert r.ambient_stored is False
        assert r.ambient_text is None
        assert r.speaker_id is None
        # detector 不该被这种 chunk 拉触发（虽然会观测，但 speaker_id=None 不入窗口）
        assert detector.active_meeting_id is None
        # repo 里也没 ambient segment
        n = await repo.count_ambient_segments()
        assert n == 0
    finally:
        await repo.aclose()
