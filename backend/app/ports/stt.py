"""STT Port：sensevoice_gpu(主) / sensevoice in-process(备)。"""

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
