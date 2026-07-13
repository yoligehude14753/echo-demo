"""Integration: 真实访问 eight STT (8090 firered) / TTS (8094) / FAST LLM fallback。

不可达自动 skip（demo 网络下 eight 经常分阶段拉起）。

PR `echodesk-remove-sensevoice`：原本测的是 :8093 SenseVoice，删除后改测主
STT backend :8090 FireRed。
"""

from __future__ import annotations

import math
import socket

import numpy as np
import pytest
from app.adapters.stt import FireRedSTT
from app.adapters.tts import SILENCE_RMS_FLOOR, Qwen3TTS
from app.config import Settings

pytestmark = pytest.mark.integration


def _can_connect(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _sine_pcm16(freq_hz: float = 440.0, dur_s: float = 1.0, sr: int = 16_000) -> bytes:
    """生成测试用 16kHz 正弦波 PCM16（用于 STT 也能转出非空文本，因为有底噪声）。"""
    t = np.arange(int(sr * dur_s), dtype=np.float32) / sr
    arr = (np.sin(2 * math.pi * freq_hz * t) * 0.3 * 32767).astype(np.int16)
    return arr.tobytes()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.skipif(not _can_connect("100.76.3.59", 8090), reason="eight 8090 (firered) 不可达")
async def test_real_stt_handshake(settings: Settings) -> None:
    """STT 接口可达且能接受合法 WAV（不强求识别质量，只验证 HTTP 协议握手）。"""
    stt = FireRedSTT(settings, timeout_s=15.0)
    pcm = _sine_pcm16(dur_s=2.0)
    segs = await stt.transcribe(pcm, sample_rate=16_000)
    # 纯正弦波可能转空文本；这里只验证不抛 STTError
    assert isinstance(segs, list)


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.skipif(not _can_connect("100.76.3.59", 8094), reason="eight 8094 (qwen3_tts) 不可达")
async def test_real_tts_synthesize(settings: Settings) -> None:
    """TTS 真实合成 → 返回非空 PCM 字节（≥ 0.1s 音频）。"""
    tts = Qwen3TTS(settings, timeout_s=30.0)
    result = await tts.synthesize_detailed("你好,我是 Echo")
    # Adapter contract is raw 16 kHz int16 mono PCM.  A 200 response containing
    # empty or silent bytes is a provider failure, not a successful handshake.
    assert len(result.pcm) >= 3_200, "TTS returned less than 100 ms of PCM"
    assert len(result.pcm) % 2 == 0, "PCM16 response must contain complete samples"
    assert result.rms >= SILENCE_RMS_FLOOR, "TTS returned silent PCM"
    assert result.max_abs > 0


@pytest.mark.asyncio
@pytest.mark.live
async def test_real_fast_llm_fallback() -> None:
    """Fast 通道使用当前配置模型，可完整流式。"""
    from app.adapters.llm import OpenAICompatibleLLM
    from app.schemas.llm import ChatMessage

    s = Settings()
    llm = OpenAICompatibleLLM(s)
    try:
        chunks: list[str] = []
        async for c in llm.chat_stream(
            [ChatMessage(role="user", content="一句话回答:1+1=?")],
            model=s.llm_fast_model,
            max_tokens=200,
            timeout_s=60.0,
        ):
            chunks.append(c)
        joined = "".join(chunks)
        assert joined.strip(), "Qwen3 returned empty"
        assert "2" in joined
    finally:
        await llm.aclose()
