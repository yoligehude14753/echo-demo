"""Speak UseCase：TTS 主链路。

把"文本 → 语音"封装成一个 use case，业务层调 ``speak(text, kind)`` 即可：
- 出 PCM bytes（16kHz 16-bit mono；对接前端 AudioContext / ESP32 通用格式）
- 同时推 ``tts.suggested`` WS 事件，告诉前端"这段文字应该被念出来"
- 调用方负责决定要不要走 TTS（如：用户已关 tts → 跳过本 use case）

设计上仍是 Ports & Adapters 友好：只 import TTSPort + EventBusPort。
"""

from __future__ import annotations

import logging
from typing import Literal

from app.ports.event_bus import EventBusPort
from app.ports.tts import TTSPort
from app.schemas.events import EchoEvent

logger = logging.getLogger("echodesk.tts")

TtsKind = Literal["chat", "minutes", "ack", "alert"]


class SpeakUseCase:
    def __init__(
        self,
        *,
        tts: TTSPort,
        event_bus: EventBusPort | None = None,
    ) -> None:
        self._tts = tts
        self._event_bus = event_bus

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
    ) -> bytes:
        """直接走 TTS 合成；返回 PCM bytes，不触发事件。"""
        clean = text.strip()
        if not clean:
            return b""
        return await self._tts.synthesize(clean, voice=voice)

    async def suggest(
        self,
        text: str,
        *,
        kind: TtsKind = "chat",
        meeting_id: str | None = None,
    ) -> None:
        """推 ``tts.suggested`` 事件，让前端按需 fetch /tts/speak 播放。

        不在后端直接产生音频，避免 WS 传大 payload；前端拿到事件后自己控制是否播。
        """
        clean = text.strip()
        if not clean or self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                EchoEvent(
                    type="tts.suggested",
                    meeting_id=meeting_id,
                    payload={"text": clean, "kind": kind},
                )
            )
        except Exception as e:
            logger.warning("publish tts.suggested failed: %s", e)


__all__ = ["SpeakUseCase", "TtsKind"]
