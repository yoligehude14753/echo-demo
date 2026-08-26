"""Legacy STT facade routed exclusively through the canonical FireRed adapter."""

from __future__ import annotations

from app.adapters.stt import FireRedSTT
from app.config import get_settings


async def _api_transcribe(
    audio_bytes: bytes,
    prompt: str = "",
    *,
    language: str | None = None,
    response_format: str | None = None,
) -> str:
    segments = await FireRedSTT(get_settings()).transcribe(
        audio_bytes,
        prompt=prompt,
        language=language,
        response_format=response_format,
    )
    return segments[0].text if segments else ""


_funasr_transcribe = _api_transcribe
_faster_whisper_transcribe = _api_transcribe
_stepfun_transcribe = _api_transcribe


def _is_hallucination(text: str) -> bool:
    normalized = text.strip().lower()
    return not normalized or len(normalized) <= 1


def get_asr_router_snapshot() -> dict[str, object]:
    configured_model = get_settings().stt_model or "profile.default"
    return {
        "backend": "firered",
        "model": configured_model,
        "logical_model": configured_model,
        "provider_count": 1,
    }


async def transcribe(
    audio_bytes: bytes,
    prompt: str = "",
    *,
    language: str | None = None,
    response_format: str | None = None,
) -> str:
    if not audio_bytes:
        return ""
    text = await _api_transcribe(
        audio_bytes,
        prompt,
        language=language,
        response_format=response_format,
    )
    return "" if _is_hallucination(text) else text
