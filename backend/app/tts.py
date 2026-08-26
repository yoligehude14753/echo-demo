"""Legacy TTS facade routed exclusively through Qwen3TTS."""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.adapters.tts import Qwen3TTS
from app.config import get_settings

def build_ssml(text: str) -> str:
    """Compatibility identity helper; prosody is no longer a legacy-provider option."""

    return text


async def synthesize(
    text: str,
    *,
    voice: str | None = None,
    sample_rate: int = 16_000,
    speed: float | None = None,
    response_format: str | None = None,
    language: str | None = None,
    ref_audio: str | None = None,
    max_new_tokens: int | None = None,
) -> bytes:
    if not text.strip():
        return b""
    adapter = Qwen3TTS(get_settings())
    try:
        return await adapter.synthesize(
            text,
            voice=voice,
            sample_rate=sample_rate,
            speed=speed,
            response_format=response_format,
            language=language,
            ref_audio=ref_audio,
            max_new_tokens=max_new_tokens,
        )
    finally:
        await adapter.aclose()


async def synthesize_stream(
    text: str,
    *,
    voice: str | None = None,
    sample_rate: int = 16_000,
    speed: float | None = None,
    response_format: str | None = None,
    language: str | None = None,
    ref_audio: str | None = None,
    max_new_tokens: int | None = None,
) -> AsyncIterator[bytes]:
    audio = await synthesize(
        text,
        voice=voice,
        sample_rate=sample_rate,
        speed=speed,
        response_format=response_format,
        language=language,
        ref_audio=ref_audio,
        max_new_tokens=max_new_tokens,
    )
    if audio:
        yield audio
