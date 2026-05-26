"""TTS Port：cosyvoice(主) / yunwu openai-tts(备)。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TTSPort(Protocol):
    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        sample_rate: int = 16_000,
    ) -> bytes: ...
