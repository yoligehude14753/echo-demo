"""STT adapter: FireRedASR2-AED HTTP（heyi-bj :8090）。

接口：POST {base}/v1/audio/transcriptions（OpenAI 兼容，与 SenseVoice GPU 同构）
- 上传 multipart：file=WAV(16k/16bit/mono)
- form 字段：model, language, response_format
- 返回 JSON: { text: str }

选型背景（docs/ARCH-AUDIT.md §2）：
- echo 实战路径：Deepgram → FireRed → faster-whisper → SenseVoice
- echo-demo 之前默认 SenseVoice，但实测 6s ambient 上 73% < 30 字 + 日英乱码
- 用户决策：切回 FireRed —— FireRed 判别式无幻觉，中文强（echo §6.30.8 RTF 0.18）
- 中英混合差是 FireRed 已知短板，但 EchoDesk 主用 中文，可接受

熔断：连续 3 次失败冷却 60s（与 SenseVoice adapter 行为一致）。
"""

from __future__ import annotations

import time

import httpx

from app.adapters.audio import pcm_to_wav
from app.config import Settings
from app.schemas.meeting import TranscriptSegment


class STTError(RuntimeError):
    pass


class FireRedSTT:
    """实现 ports.stt.STTPort。"""

    def __init__(self, settings: Settings, *, timeout_s: float = 60.0) -> None:
        self._settings = settings
        self._base = settings.stt_firered_url.rstrip("/")
        self._default_language = settings.stt_language
        # 直连 heyi 时为 "x"（服务端忽略）；网关模式为客户端 token（网关校验）。
        self._auth_token = settings.upstream_audio_token
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
        if self._circuit_open():
            raise STTError("firered circuit open (3 consecutive failures)")
        if not audio_bytes:
            return []

        lang = language or self._default_language
        wav = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        url = f"{self._base}/v1/audio/transcriptions"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                resp = await client.post(
                    url,
                    # FireRed server schema 默认 model="whisper-1" 也接受，固定写 firered-asr-aed 更清晰
                    headers={"Authorization": f"Bearer {self._auth_token}"},
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
            self._last_fail = time.monotonic()
            raise STTError(f"firered transcribe failed: {e}") from e

        self._fail_count = 0
        text = (data.get("text") or "").strip()
        if not text:
            return []

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
