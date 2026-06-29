"""TTS Port：当前 adapter = faster-qwen3-tts 1.7B CustomVoice。

详见 docs/ARCH-AUDIT.md §1 §3。
"""

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
