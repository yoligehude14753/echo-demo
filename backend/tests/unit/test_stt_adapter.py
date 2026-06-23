"""STT adapter 单测：mock httpx，验证请求体 + 熔断 + 错误包装。

PR `echodesk-remove-sensevoice` 之前测的是 SenseVoiceGPUSTT。SenseVoice 删
掉后，唯一 STT backend 是 FireRedSTT，本文件改测 FireRed。两个 adapter API
形态一致（都通过 OpenAI /v1/audio/transcriptions schema 发 multipart），所以
测试结构能整体复用。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.stt import FireRedSTT
from app.adapters.stt.firered import STTError
from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        stt_backend="firered",
        stt_firered_url="http://100.76.3.59:8090",
    )


def _mock_async_client_post(json_payload: dict, status: int = 200) -> object:
    resp = MagicMock()
    resp.json.return_value = json_payload
    resp.status_code = status
    resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    return fake_client


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transcribe_returns_segment_with_text(settings: Settings) -> None:
    stt = FireRedSTT(settings)
    fake = _mock_async_client_post({"text": "你好世界"})
    with patch("app.adapters.stt.firered.httpx.AsyncClient", return_value=fake):
        segs = await stt.transcribe(b"\x00\x01" * 8000, sample_rate=16_000)
    assert len(segs) == 1
    assert segs[0].text == "你好世界"
    assert segs[0].start_ms == 0
    assert segs[0].end_ms > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transcribe_empty_audio_returns_empty_list(settings: Settings) -> None:
    stt = FireRedSTT(settings)
    segs = await stt.transcribe(b"")
    assert segs == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transcribe_empty_text_returns_empty_list(settings: Settings) -> None:
    stt = FireRedSTT(settings)
    fake = _mock_async_client_post({"text": "  "})
    with patch("app.adapters.stt.firered.httpx.AsyncClient", return_value=fake):
        segs = await stt.transcribe(b"\x00\x01" * 8000)
    assert segs == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transcribe_http_error_raises_stterror(settings: Settings) -> None:
    stt = FireRedSTT(settings)
    fake = MagicMock()
    fake.post = AsyncMock(side_effect=RuntimeError("boom"))
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("app.adapters.stt.firered.httpx.AsyncClient", return_value=fake),
        pytest.raises(STTError, match=r"firered transcribe failed"),
    ):
        await stt.transcribe(b"\x00\x01" * 8000)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_circuit_breaker_opens_after_3_failures(settings: Settings) -> None:
    stt = FireRedSTT(settings)
    fake = MagicMock()
    fake.post = AsyncMock(side_effect=RuntimeError("boom"))
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.adapters.stt.firered.httpx.AsyncClient", return_value=fake):
        for _ in range(3):
            with pytest.raises(STTError):
                await stt.transcribe(b"\x00\x01" * 8000)
        # 第 4 次直接被熔断拒绝（不再发请求）
        with pytest.raises(STTError, match="circuit open"):
            await stt.transcribe(b"\x00\x01" * 8000)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_make_stt_returns_firered_regardless_of_legacy_backend_value() -> None:
    """老 .env 里 STT_BACKEND=sensevoice_gpu 等值应被忽略，回退 firered（不报错）。"""
    from app.adapters.stt import make_stt

    s = Settings(stt_backend="sensevoice_gpu", stt_firered_url="http://x:8090")
    adapter = make_stt(s)
    assert isinstance(adapter, FireRedSTT)
