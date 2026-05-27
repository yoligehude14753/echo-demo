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
from app.ports.rag import RagPort
from app.ports.stt import STTPort
from app.schemas.capture import CaptureChunkResult
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError

logger = logging.getLogger("echo-demo.ambient")


class AmbientCapturePipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        stt: STTPort,
        rag: RagPort,
        meeting: MeetingPipeline,
    ) -> None:
        self._settings = settings
        self._stt = stt
        self._rag = rag
        self._meeting = meeting
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

        stt_segs = []
        try:
            stt_segs = await self._stt.transcribe(audio_bytes, sample_rate=sample_rate)
        except Exception as e:
            logger.warning("ambient STT failed (audio saved): %s", e)

        ambient_stored = False
        ambient_text: str | None = None
        texts = [s.text.strip() for s in stt_segs if s.text.strip()]
        if texts:
            ambient_text = " ".join(texts)
            captured_at = datetime.now(UTC).isoformat()
            try:
                await self._rag.ingest_ambient_segment(
                    ambient_text,
                    captured_at=captured_at,
                    audio_ref=audio_ref,
                )
                ambient_stored = True
            except Exception as e:
                logger.warning("ambient RAG ingest failed: %s", e)

        meeting_segments = []
        if meeting_id:
            try:
                meeting_segments = await self._meeting.ingest_from_stt(
                    meeting_id,
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
            meeting_segments=meeting_segments,
        )
