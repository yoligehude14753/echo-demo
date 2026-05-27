"""TTS API：POST /tts/speak → 直接返回 PCM bytes（16kHz 16-bit mono）。

前端在用户开了 TTS 开关后调本接口：
- chat 答完 / @总结会议 完 → fetch /tts/speak → AudioContext 播放
- 后端不主动推 WS 音频，避免 base64 大 payload；只推 ``tts.suggested`` 文字事件
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.tts.qwen3_tts import Qwen3TTS, TTSError
from app.api.deps import get_event_bus
from app.config import Settings, get_settings
from app.use_cases.speak import SpeakUseCase, TtsKind

router = APIRouter(prefix="/tts", tags=["tts"])

_tts_singleton: Qwen3TTS | None = None


def get_tts_singleton(
    settings: Settings = Depends(get_settings),
) -> Qwen3TTS:
    """faster-qwen3-tts adapter 单例（详见 docs/ARCH-AUDIT.md §3）。"""
    global _tts_singleton  # noqa: PLW0603
    if _tts_singleton is None:
        _tts_singleton = Qwen3TTS(settings)
    return _tts_singleton


def get_speak_use_case(
    tts: Qwen3TTS = Depends(get_tts_singleton),
    bus: InMemoryEventBus = Depends(get_event_bus),
) -> SpeakUseCase:
    return SpeakUseCase(tts=tts, event_bus=bus)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    voice: str | None = None
    kind: TtsKind = "chat"


@router.post(
    "/speak",
    responses={
        200: {
            "content": {"audio/pcm": {}},
            "description": "PCM 16kHz 16-bit mono",
        }
    },
)
async def tts_speak(
    body: Annotated[SpeakRequest, Body(...)],
    settings: Annotated[Settings, Depends(get_settings)],
    speak: Annotated[SpeakUseCase, Depends(get_speak_use_case)],
) -> Response:
    """合成语音并返回 PCM bytes。"""
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="tts disabled in settings")
    try:
        pcm = await speak.synthesize(body.text, voice=body.voice)
    except TTSError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not pcm:
        raise HTTPException(status_code=400, detail="empty text")
    return Response(content=pcm, media_type="audio/pcm")


class SuggestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: TtsKind = "chat"
    meeting_id: str | None = None


@router.post("/suggest")
async def tts_suggest(
    body: Annotated[SuggestRequest, Body(...)],
    speak: Annotated[SpeakUseCase, Depends(get_speak_use_case)],
) -> dict[str, str]:
    """只推事件，不合成（让前端控制是否真的播）。多用于服务端主动触发场景。"""
    await speak.suggest(body.text, kind=body.kind, meeting_id=body.meeting_id)
    return {"status": "queued"}
