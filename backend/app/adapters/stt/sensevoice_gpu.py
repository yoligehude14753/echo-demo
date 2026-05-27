"""STT adapter: sensevoice_gpu HTTP（heyi-bj :8093）。

接口：POST {base}/v1/audio/transcriptions（OpenAI 兼容）
- model: "sensevoice-small"
- 输入 WAV(16k/16bit/mono)
- 输出 JSON: { text: str }

熔断：连续 3 次失败冷却 60s。
"""

from __future__ import annotations

import time

import httpx

from app.adapters.audio import pcm_to_wav
from app.config import Settings
from app.schemas.meeting import TranscriptSegment


class STTError(RuntimeError):
    pass


class SenseVoiceGPUSTT:
    """实现 ports.stt.STTPort。"""

    def __init__(self, settings: Settings, *, timeout_s: float = 60.0) -> None:
        self._settings = settings
        self._base = settings.stt_sensevoice_gpu_url.rstrip("/")
        # 默认语言从 settings 读取（修复 2026-05-27 发现的 silent config bug：
        # 之前 transcribe(language="zh") 是方法签名硬编码，settings.stt_language 静默失效）
        self._default_language = settings.stt_language
        self._timeout = timeout_s
        self._fail_count = 0
        self._last_fail: float = 0.0
        self._max_failures = 3
        self._cooldown_s = 60.0

    def _circuit_open(self) -> bool:
        if self._fail_count < self._max_failures:
            return False
        if time.monotonic() - self._last_fail < self._cooldown_s:
            return True
        self._fail_count = 0
        return False

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> list[TranscriptSegment]:
        # 调用方不传 language 时用 settings 默认（避免历史硬编码 "zh" 让配置失效）
        lang = language or self._default_language
        if self._circuit_open():
            raise STTError("sensevoice_gpu circuit open (3 consecutive failures)")
        if not audio_bytes:
            return []

        wav = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        url = f"{self._base}/v1/audio/transcriptions"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": "Bearer x"},
                    data={
                        "model": "sensevoice-small",
                        "language": lang,
                        "response_format": "json",
                    },
                    files={"file": ("audio.wav", wav, "audio/wav")},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            self._fail_count += 1
            self._last_fail = time.monotonic()
            raise STTError(f"sensevoice_gpu transcribe failed: {e}") from e

        self._fail_count = 0
        text = (data.get("text") or "").strip()
        if not text:
            return []

        # 把整段音频长度估算为单一 segment（无字级时间轴）
        duration_ms = int(len(audio_bytes) / (sample_rate * 2) * 1000)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return [
            TranscriptSegment(
                text=text,
                start_ms=0,
                end_ms=max(duration_ms, elapsed_ms),
                speaker_id=None,
                speaker_label=None,
            )
        ]
