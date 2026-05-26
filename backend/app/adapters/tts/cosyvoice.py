"""TTS adapter: cosyvoice HTTP（heyi-bj :8094）。

OpenAI 兼容接口（参考 echo backend/app/tts.py 的 _cosyvoice_tts 实现）：
  POST {base}/v1/audio/speech
  Authorization: Bearer x
  json: { model: "tts-1", input: text, voice: str, stream: false }
  resp: audio/wav（或 audio/pcm，按服务返回的 content-type 决定）

输出统一为 raw 16kHz 16-bit mono PCM（与 ESP32/前端 AudioContext 一致）。
"""

from __future__ import annotations

import time

import httpx

from app.adapters.audio import wav_to_float_mono16k
from app.config import Settings


class TTSError(RuntimeError):
    pass


class CosyVoiceTTS:
    """实现 ports.tts.TTSPort。"""

    def __init__(self, settings: Settings, *, timeout_s: float = 30.0) -> None:
        self._settings = settings
        self._base = settings.tts_cosyvoice_url.rstrip("/")
        self._default_voice = settings.tts_cosyvoice_voice
        self._timeout = timeout_s

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        sample_rate: int = 16_000,
    ) -> bytes:
        if not text.strip():
            return b""
        use_voice = voice or self._default_voice
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                resp = await client.post(
                    f"{self._base}/v1/audio/speech",
                    json={
                        "model": "tts-1",
                        "input": text,
                        "voice": use_voice,
                        "stream": False,
                    },
                    headers={"Authorization": "Bearer x"},
                )
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                audio = resp.content
        except Exception as e:
            raise TTSError(
                f"cosyvoice synthesize failed ({time.monotonic() - t0:.2f}s): {e}"
            ) from e

        is_wav = "wav" in ct or (len(audio) >= 4 and audio[:4] == b"RIFF")
        if is_wav:
            arr = wav_to_float_mono16k(audio)
            if arr is None:
                return audio
            import numpy as np

            pcm16 = (arr * 32767.0).clip(-32768, 32767).astype(np.int16)
            return bytes(pcm16.tobytes())
        return audio
