"""Ambient 主链路 UseCase：落盘 + STT + RAG；Meeting 为可选叠加层。

设计（方案 2 · 数字分身）：
- 每个 chunk **必**走 ambient（会议外音频不丢弃）
- meeting_id 可选：仅当会议 in_meeting 时叠加 MeetingPipeline（复用同一次 STT）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.ports.diarizer import DiarizerPort
from app.ports.event_bus import EventBusPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.ports.stt import STTPort
from app.schemas.capture import CaptureChunkResult
from app.schemas.events import EchoEvent
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.speaker_registry import SpeakerRegistry

logger = logging.getLogger("echodesk.ambient")


class AmbientCapturePipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        stt: STTPort,
        rag: RagPort,
        meeting: MeetingPipeline,
        repository: RepositoryPort | None = None,
        diarizer: DiarizerPort | None = None,
        speaker_registry: SpeakerRegistry | None = None,
        auto_meeting_detector: AutoMeetingDetector | None = None,
        event_bus: EventBusPort | None = None,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._rag = rag
        self._meeting = meeting
        self._repo = repository
        self._diarizer = diarizer
        self._registry = speaker_registry
        self._detector = auto_meeting_detector
        self._event_bus = event_bus
        self._ambient_dir = Path(settings.storage_dir).expanduser() / "ambient"
        self._ambient_dir.mkdir(parents=True, exist_ok=True)

    def _persist_wav(self, audio_bytes: bytes, sample_rate: int) -> str:
        now = datetime.now(UTC)
        day_dir = self._ambient_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        name = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.wav"
        path = day_dir / name
        path.write_bytes(audio_bytes)
        return str(path)

    async def ingest_chunk(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        meeting_id: str | None = None,
    ) -> CaptureChunkResult:
        audio_ref = await asyncio.to_thread(self._persist_wav, audio_bytes, sample_rate)

        # STT 与 Diarizer 并发（节省 ambient 全天候链路的延迟）
        stt_task = asyncio.create_task(self._safe_stt(audio_bytes, sample_rate))
        diar_task: asyncio.Task[str | None] | None = None
        if self._diarizer is not None:
            diar_task = asyncio.create_task(self._safe_diarize(audio_bytes, sample_rate))
        stt_segs = await stt_task
        speaker_id: str | None = await diar_task if diar_task is not None else None

        captured_dt = datetime.now(UTC)
        captured_at = captured_dt.isoformat()

        speaker_label: str | None = None
        if self._registry is not None:
            speaker_label = await self._registry.label_for(speaker_id, captured_at=captured_dt)

        ambient_stored = False
        ambient_text: str | None = None
        texts = [s.text.strip() for s in stt_segs if s.text.strip()]
        if texts:
            ambient_text = " ".join(texts)
            duration_ms = max(0, max((s.end_ms for s in stt_segs), default=0))
            try:
                await self._rag.ingest_ambient_segment(
                    ambient_text,
                    captured_at=captured_at,
                    audio_ref=audio_ref,
                    speaker_id=speaker_id,
                    speaker_label=speaker_label,
                )
                ambient_stored = True
            except Exception as e:
                logger.warning("ambient RAG ingest failed: %s", e)
            if self._repo is not None:
                try:
                    await self._repo.append_ambient_segment(
                        audio_ref=audio_ref,
                        text=ambient_text,
                        captured_at=captured_dt,
                        speaker_id=speaker_id,
                        speaker_label=speaker_label,
                        duration_ms=duration_ms,
                    )
                except Exception as e:
                    logger.warning("ambient repo persist failed: %s", e)

        # 自动会议检测（手动 meeting_id 优先；detector 让步）
        effective_meeting_id = meeting_id
        if self._detector is not None:
            duration_ms = max((s.end_ms for s in stt_segs), default=0) if stt_segs else 0
            events = self._detector.observe(
                speaker_id=speaker_id,
                duration_ms=duration_ms,
                now=captured_dt,
                manual_meeting_id=meeting_id,
            )
            for ev in events:
                if ev.kind == "start":
                    try:
                        await self._meeting.start_meeting(
                            ev.meeting_id, auto_started=True
                        )
                    except Exception as e:
                        logger.warning("auto-start meeting failed: %s", e)
                        continue
                    await self._publish_event(
                        "meeting.auto_detected",
                        ev.meeting_id,
                        {"reason": ev.reason},
                    )
                elif ev.kind == "end":
                    try:
                        await self._meeting.end_meeting(ev.meeting_id)
                    except Exception as e:
                        logger.warning("auto-end meeting failed: %s", e)
                    await self._publish_event(
                        "meeting.auto_ended",
                        ev.meeting_id,
                        {"reason": ev.reason},
                    )
            # 手动 mid 优先；否则用 detector 当前的 auto meeting
            if meeting_id is None:
                effective_meeting_id = self._detector.active_meeting_id

        meeting_segments = []
        if effective_meeting_id:
            try:
                meeting_segments = await self._meeting.ingest_from_stt(
                    effective_meeting_id,
                    audio_bytes,
                    stt_segs,
                    sample_rate=sample_rate,
                )
            except MeetingPipelineError as e:
                logger.debug("meeting overlay skipped: %s", e)

        return CaptureChunkResult(
            ambient_stored=ambient_stored,
            ambient_text=ambient_text,
            audio_ref=audio_ref,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
            meeting_id=effective_meeting_id,
            meeting_segments=meeting_segments,
        )

    async def _publish_event(
        self, event_type: str, meeting_id: str, payload: dict[str, str]
    ) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                EchoEvent(type=event_type, meeting_id=meeting_id, payload=payload)  # type: ignore[arg-type]
            )
        except Exception as e:
            logger.warning("publish %s failed: %s", event_type, e)

    async def _safe_stt(
        self, audio_bytes: bytes, sample_rate: int
    ) -> list:  # type: ignore[type-arg]
        try:
            return await self._stt.transcribe(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            logger.warning("ambient STT failed (audio saved): %s", e)
            return []

    async def _safe_diarize(self, audio_bytes: bytes, sample_rate: int) -> str | None:
        if self._diarizer is None:
            return None
        try:
            return await self._diarizer.identify(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            logger.warning("ambient diarizer failed: %s", e)
            return None
