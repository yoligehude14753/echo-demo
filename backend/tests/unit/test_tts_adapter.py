"""TTS adapter 单测：mock httpx 返回 audio bytes，验证请求体与返回。"""

from __future__ import annotations

import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from app.adapters.tts import Qwen3TTS, TTSError
from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        tts_provider="qwen3_tts",
        tts_qwen3_url="http://100.76.3.59:8094",
        tts_qwen3_voice="aiden",
        heyi_gateway_token="gw-token",
    )


def _wav_bytes_of_silence(n_samples: int = 1600) -> bytes:
    samples = np.zeros(n_samples, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def _mock_client_returning_wav(wav: bytes) -> object:
    resp = MagicMock()
    resp.content = wav
    resp.headers = {"content-type": "audio/wav"}
    resp.raise_for_status = MagicMock()
    fake = MagicMock()
    fake.post = AsyncMock(return_value=resp)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    return fake


@pytest.mark.asyncio
@pytest.mark.unit
async def test_synthesize_returns_pcm_for_wav_response(settings: Settings) -> None:
    tts = Qwen3TTS(settings)
    wav = _wav_bytes_of_silence(1600)
    fake = _mock_client_returning_wav(wav)
    with patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake) as client:
        pcm = await tts.synthesize("你好")
    # 16k PCM 16-bit mono = 2 bytes/sample
    assert isinstance(pcm, bytes)
    assert len(pcm) == 1600 * 2
    assert client.call_args.kwargs["timeout"] == settings.tts_qwen3_timeout_s
    assert fake.post.await_args.kwargs["headers"] == {"Authorization": "Bearer gw-token"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_synthesize_empty_text_returns_empty(settings: Settings) -> None:
    tts = Qwen3TTS(settings)
    out = await tts.synthesize("  ")
    assert out == b""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_synthesize_http_error_raises_ttserror(settings: Settings) -> None:
    tts = Qwen3TTS(settings)
    fake = MagicMock()
    fake.post = AsyncMock(side_effect=RuntimeError("boom"))
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake),
        pytest.raises(TTSError, match="qwen3_tts synthesize failed"),
    ):
        await tts.synthesize("你好")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tts_specific_api_key_overrides_gateway_token() -> None:
    settings = Settings(
        tts_provider="qwen3_tts",
        tts_qwen3_url="http://100.76.3.59:8094",
        tts_qwen3_voice="aiden",
        heyi_gateway_token="gw-token",
        tts_qwen3_api_key="tts-token",
    )
    tts = Qwen3TTS(settings)
    fake = _mock_client_returning_wav(_wav_bytes_of_silence(1600))
    with patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake):
        await tts.synthesize("你好")
    assert fake.post.await_args.kwargs["headers"] == {"Authorization": "Bearer tts-token"}
