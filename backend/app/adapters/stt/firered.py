"""STT adapter: FireRedASR2-AED HTTP。

接口：POST {base}/v1/audio/transcriptions（OpenAI 兼容，与 SenseVoice GPU 同构）
- 上传 multipart：file=WAV(16k/16bit/mono)
- form 字段：model, language, response_format
- 返回 JSON: { text: str }

选型背景（docs/ARCH-AUDIT.md §2）：
- echo 实战路径：Deepgram → FireRed → faster-whisper → SenseVoice
- echo-demo 之前默认 SenseVoice，但实测 6s ambient 上 73% < 30 字 + 日英乱码
- 用户决策：切回 FireRed —— FireRed 判别式无幻觉，中文强（echo §6.30.8 RTF 0.18）
- 中英混合差是 FireRed 已知短板，但 EchoDesk 主用 中文，可接受

稳定性策略：不在 adapter 层做本地熔断。语音识别服务偶发慢/空/断连时，调用方
按单次失败处理；ambient pipeline 负责并发闸，避免慢请求堆积。
"""

from __future__ import annotations

import logging
import time

import httpx

from app.adapters.audio import normalize_audio_bytes, pcm_to_wav
from app.config import Settings
from app.schemas.meeting import TranscriptSegment

logger = logging.getLogger("echodesk.stt.firered")


class STTError(RuntimeError):
    pass


def _error_detail(e: Exception) -> str:
    text = str(e).strip()
    if not text:
        text = repr(e)
    return f"{type(e).__name__}: {text}"


class FireRedSTT:
    """实现 ports.stt.STTPort。"""

    def __init__(self, settings: Settings, *, timeout_s: float = 60.0) -> None:
        self._settings = settings
        self._base = settings.stt_firered_url.rstrip("/")
        self._default_language = settings.stt_language
        self._timeout = timeout_s
        self._api_key = settings.stt_firered_api_key or settings.heyi_gateway_token or "x"
        self._fail_count = 0

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> list[TranscriptSegment]:
        if not audio_bytes:
            return []

        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        audio_bytes = normalized.pcm
        sample_rate = normalized.sample_rate
        lang = language or self._default_language
        wav = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        url = f"{self._base}/v1/audio/transcriptions"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                resp = await client.post(
                    url,
                    # FireRed server schema 默认 model="whisper-1" 也接受，固定写 firered-asr-aed 更清晰
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data={
                        "model": "firered-asr-aed",
                        "language": lang,
                        "response_format": "json",
                    },
                    files={"file": ("audio.wav", wav, "audio/wav")},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            self._fail_count += 1
            raise STTError(f"firered transcribe failed: {_error_detail(e)}") from e

        self._fail_count = 0
        text = (data.get("text") or "").strip()
        if not text:
            return []

        # ``end_ms`` 是音频时间轴，不能混入 HTTP/推理 wall time。旧实现取
        # max(audio_duration, elapsed)，上游慢 20~60s 时会把一个 6s chunk
        # 伪装成 20~60s 的有效语音，进而错误触发或续命 auto meeting。
        duration_ms = int(len(audio_bytes) / (sample_rate * 2) * 1000)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.debug(
            "firered transcription completed audio_duration_ms=%d elapsed_ms=%d chars=%d",
            duration_ms,
            elapsed_ms,
            len(text),
        )
        return [
            TranscriptSegment(
                text=text,
                start_ms=0,
                end_ms=duration_ms,
                speaker_id=None,
                speaker_label=None,
            )
        ]
