"""TTS adapter: faster-qwen3-tts 1.7B CustomVoice。

历史命名校正（2026-05-27）：之前这个 adapter 叫 `CosyVoiceTTS`，文件名
`cosyvoice.py`，所有 settings 字段都以 `tts_cosyvoice_*` 命名。但
当前端点上**实际跑的服务**是 `faster-qwen3-tts CustomVoice
OpenAI-compatible API`（openapi swagger title 实测确认），不是 CosyVoice。
echo 历史也已经在 commit b065547 把 TTS 从 CosyVoice2-0.5B :8092 切到
faster-qwen3-tts :8094（TTFB 5ms，比 cosyvoice 200×）。

详见 `docs/ARCH-AUDIT.md` §3。

OpenAI 兼容接口（参考 echo `backend/app/tts.py` 的 _fasterqwen3_tts 实现）：
  POST {base}/v1/audio/speech
  Authorization: Bearer {configured_token}
  json: { model: "tts-1", input: text, voice: str, stream: false }
  resp: audio/wav 或 audio/pcm（按 content-type 决定）

输出统一为 raw 16kHz 16-bit mono PCM（与 ESP32/前端 AudioContext 一致）。
faster-qwen3-tts 内部 24k → 16k 重采样已修（echo commit f465fe4）。

注（phase4-tts 2026-05-28）：部分 `audio/wav` 响应的 RIFF/data 长度字段是
``0xFFFFFFFF``（流式 placeholder，没有 fix-up）；Python ``wave`` 模块虽然能
读出全部数据，但任何"按 nframes 解析"的链路都很脆弱。adapter 现在多回
``SynthesisResult`` 携带原始字节、PCM、能量和 latency，让 API 层做诚实的
silence/empty 检测——cold-start 静音输出不再被悄悄当成正常响应返回。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.adapters.audio import wav_to_float_mono16k
from app.config import Settings

logger = logging.getLogger("echodesk.tts")


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthesisResult:
    """合成一次的完整产物 + 质量指标。

    - ``pcm``：返回给客户端的 raw 16kHz 16-bit mono bytes
    - ``raw_bytes`` / ``raw_content_type``：上游原始响应（wav 或 pcm）
    - ``rms``：PCM 的 RMS（int16 量纲，0–32767），用于 silence 检测
    - ``max_abs``：PCM 绝对值峰值
    - ``latency_s``：从发请求到拿到 bytes 的总耗时
    """

    pcm: bytes
    raw_bytes: bytes
    raw_content_type: str
    rms: float
    max_abs: int
    latency_s: float


# 静音阈值：RMS < SILENCE_RMS_FLOOR 视为静音。
# 真实人声 RMS 通常 ≥ 2000，背景噪声 ~50–200；50 是一个保守 floor，
# 既能挡住"全 0 cold-start"输出，又不会误伤极短/静默句首。
SILENCE_RMS_FLOOR = 50.0


class Qwen3TTS:
    """faster-qwen3-tts CustomVoice OpenAI-compatible 客户端。

    实现 ports.tts.TTSPort。
    """

    def __init__(self, settings: Settings, *, timeout_s: float | None = None) -> None:
        self._settings = settings
        self._base = settings.tts_qwen3_url.rstrip("/")
        self._default_voice = settings.tts_qwen3_voice
        self._timeout = settings.tts_qwen3_timeout_s if timeout_s is None else timeout_s
        self._api_key = settings.tts_qwen3_api_key or settings.heyi_gateway_token or "x"
        self._macos_fallback_enabled = settings.tts_macos_fallback_enabled

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def default_voice(self) -> str:
        return self._default_voice

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        sample_rate: int = 16_000,
    ) -> bytes:
        """TTSPort 兼容入口，仅返回 PCM bytes（含静音/空保留——上层决定怎么处理）。"""
        result = await self.synthesize_detailed(text, voice=voice, sample_rate=sample_rate)
        return result.pcm

    async def synthesize_detailed(
        self,
        text: str,
        *,
        voice: str | None = None,
        sample_rate: int = 16_000,  # 服务固定 16k；保留参数仅为 Port 兼容
    ) -> SynthesisResult:
        _ = sample_rate  # 显式标记未用，避免 ARG002 误报
        """详细版：返回 PCM 与原始字节、质量指标。供 /tts/diag 与 /tts/speak 共用。"""
        if not text.strip():
            return SynthesisResult(
                pcm=b"",
                raw_bytes=b"",
                raw_content_type="",
                rms=0.0,
                max_abs=0,
                latency_s=0.0,
            )
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
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                audio: bytes = bytes(resp.content)
        except Exception as e:
            if not self._macos_fallback_enabled:
                raise TTSError(
                    f"qwen3_tts synthesize failed ({time.monotonic() - t0:.2f}s): {e}"
                ) from e
            try:
                fallback = await _macos_say_fallback(text)
                logger.warning(
                    "qwen3_tts unavailable; fallback→macOS say ok: text_len=%d pcm_bytes=%d latency=%.2fs",
                    len(text),
                    len(fallback.pcm),
                    fallback.latency_s,
                )
                return fallback
            except Exception as fallback_exc:
                raise TTSError(
                    f"qwen3_tts synthesize failed ({time.monotonic() - t0:.2f}s): {e}; "
                    f"macOS say fallback failed: {fallback_exc}"
                ) from e

        elapsed = time.monotonic() - t0
        pcm = _decode_to_pcm16k(audio, ct)
        rms, max_abs = _pcm16_quality(pcm)
        return SynthesisResult(
            pcm=pcm,
            raw_bytes=audio,
            raw_content_type=ct,
            rms=rms,
            max_abs=max_abs,
            latency_s=elapsed,
        )


def _decode_to_pcm16k(audio: bytes, content_type: str) -> bytes:
    """把上游响应（wav 或 raw pcm）统一成 16kHz 16-bit mono PCM。"""
    is_wav = "wav" in content_type or (len(audio) >= 4 and audio[:4] == b"RIFF")
    if not is_wav:
        return audio
    arr = wav_to_float_mono16k(audio)
    if arr is None:
        return audio

    import numpy as np

    pcm16 = (arr * 32767.0).clip(-32768, 32767).astype(np.int16)
    return bytes(pcm16.tobytes())


async def _macos_say_fallback(text: str) -> SynthesisResult:
    """本机 macOS TTS 兜底：heyi/eight TTS 掉线时仍返回 16k PCM。"""
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="echodesk-tts-") as tmp:
        tmpdir = Path(tmp)
        aiff_path = tmpdir / "speech.aiff"
        wav_path = tmpdir / "speech.wav"
        say_proc = await asyncio.create_subprocess_exec(
            "say",
            "-v",
            "Ting-Ting",
            "-o",
            str(aiff_path),
            text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, say_err = await say_proc.communicate()
        if say_proc.returncode != 0:
            raise TTSError(say_err.decode("utf-8", errors="ignore") or "say failed")

        convert_proc = await asyncio.create_subprocess_exec(
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            str(aiff_path),
            str(wav_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, convert_err = await convert_proc.communicate()
        if convert_proc.returncode != 0:
            raise TTSError(
                convert_err.decode("utf-8", errors="ignore") or "afconvert failed"
            )
        audio = wav_path.read_bytes()

    pcm = _decode_to_pcm16k(audio, "audio/wav")
    rms, max_abs = _pcm16_quality(pcm)
    return SynthesisResult(
        pcm=pcm,
        raw_bytes=audio,
        raw_content_type="audio/wav; fallback=macos-say",
        rms=rms,
        max_abs=max_abs,
        latency_s=time.monotonic() - t0,
    )


def _pcm16_quality(pcm: bytes) -> tuple[float, int]:
    """计算 PCM 的 RMS 与峰值绝对值，给 silence 检测用。

    空 PCM 返回 (0.0, 0)；numpy 缺失时降级为 (0.0, 0) 而不抛——质量指标
    缺失只会导致 silence 误判保守化，不应让整个调用链炸掉。
    """
    if not pcm:
        return 0.0, 0
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return 0.0, 0
    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size == 0:
        return 0.0, 0
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
    max_abs = int(np.max(np.abs(arr)))
    return rms, max_abs


def is_silent(result: SynthesisResult, floor: float = SILENCE_RMS_FLOOR) -> bool:
    """根据 RMS 判断是否静音（cold-start 偶尔会返回全 0 PCM）。"""
    return bool(result.pcm) and result.rms < floor


# 历史兼容别名：仍允许 from app.adapters.tts import CosyVoiceTTS。
# Deprecated since 2026-05-27. 新代码请用 Qwen3TTS。
CosyVoiceTTS = Qwen3TTS

__all__ = [
    "SILENCE_RMS_FLOOR",
    "CosyVoiceTTS",
    "Qwen3TTS",
    "SynthesisResult",
    "TTSError",
    "is_silent",
]
