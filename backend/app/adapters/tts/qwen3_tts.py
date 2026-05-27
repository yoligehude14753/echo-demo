"""TTS adapter: faster-qwen3-tts 1.7B CustomVoice（heyi-bj :8094）。

历史命名校正（2026-05-27）：之前这个 adapter 叫 `CosyVoiceTTS`，文件名
`cosyvoice.py`，所有 settings 字段都以 `tts_cosyvoice_*` 命名。但
heyi-bj :8094 端点上**实际跑的服务**是 `faster-qwen3-tts CustomVoice
OpenAI-compatible API`（openapi swagger title 实测确认），不是 CosyVoice。
echo 历史也已经在 commit b065547 把 TTS 从 CosyVoice2-0.5B :8092 切到
faster-qwen3-tts :8094（TTFB 5ms，比 cosyvoice 200×）。

详见 `docs/ARCH-AUDIT.md` §3。

OpenAI 兼容接口（参考 echo `backend/app/tts.py` 的 _fasterqwen3_tts 实现）：
  POST {base}/v1/audio/speech
  Authorization: Bearer x
  json: { model: "tts-1", input: text, voice: str, stream: false }
  resp: audio/wav 或 audio/pcm（按 content-type 决定）

输出统一为 raw 16kHz 16-bit mono PCM（与 ESP32/前端 AudioContext 一致）。
faster-qwen3-tts 内部 24k → 16k 重采样已修（echo commit f465fe4）。
"""

from __future__ import annotations

import time

import httpx

from app.adapters.audio import wav_to_float_mono16k
from app.config import Settings


class TTSError(RuntimeError):
    pass


class Qwen3TTS:
    """faster-qwen3-tts CustomVoice OpenAI-compatible 客户端。

    实现 ports.tts.TTSPort。
    """

    def __init__(self, settings: Settings, *, timeout_s: float = 30.0) -> None:
        self._settings = settings
        self._base = settings.tts_qwen3_url.rstrip("/")
        self._default_voice = settings.tts_qwen3_voice
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
                audio: bytes = bytes(resp.content)
        except Exception as e:
            raise TTSError(
                f"qwen3_tts synthesize failed ({time.monotonic() - t0:.2f}s): {e}"
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


# 历史兼容别名：仍允许 from app.adapters.tts import CosyVoiceTTS。
# Deprecated since 2026-05-27. 新代码请用 Qwen3TTS。
CosyVoiceTTS = Qwen3TTS

__all__ = ["Qwen3TTS", "CosyVoiceTTS", "TTSError"]
