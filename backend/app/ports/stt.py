"""STT Port：当前唯一 backend 为 Model Gateway 中的 Qwen3-ASR。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.schemas.meeting import TranscriptSegment


@dataclass(frozen=True, slots=True)
class TranscriptStreamEvent:
    """Transient ASR projection; canonical persistence remains final-only."""

    text: str
    state: Literal["partial", "completed", "failed"]


TranscriptStreamHandler = Callable[[TranscriptStreamEvent], Awaitable[None]]


@runtime_checkable
class STTPort(Protocol):
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
        on_partial: TranscriptStreamHandler | None = None,
    ) -> list[TranscriptSegment]: ...


__all__ = ["STTPort", "TranscriptStreamEvent", "TranscriptStreamHandler"]
