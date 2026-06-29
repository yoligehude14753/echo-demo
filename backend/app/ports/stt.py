"""STT Port：当前唯一 backend 为 FireRedASR2-AED 兼容服务。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.meeting import TranscriptSegment


@runtime_checkable
class STTPort(Protocol):
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]: ...
