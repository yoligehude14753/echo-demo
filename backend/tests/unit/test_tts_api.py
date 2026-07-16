"""HTTP /tts/* 端点单测（M_tts_check 复盘新增）。

覆盖 4 个 spec 出来的失败模式：
1. Happy path：上游 wav → backend 返回 PCM bytes，200
2. 上游返回静音 PCM（heyi cold-start 实测）→ backend 502 ``tts_silent_output``
3. 上游 httpx 直接报错 → backend 502 ``tts_upstream_error``
4. /tts/diag：state=ok / silent_output / upstream_error / disabled 四种 cache+freshness

old 实现的 silent output 会被原样返回给前端 → 前端 console.warn 吞掉 →
用户看到"绿灯按了播放没声音"。本套测试保证这条静默退路永远关上。
"""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import Callable, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from app.adapters.tts import Qwen3TTS, SynthesisResult, TTSError
from app.api.tts import _reset_diag_cache_for_tests, get_tts_singleton, tts_diag
from app.config import Settings
from app.main import create_app
from app.security.context import bind_principal, reset_principal
from app.security.models import Principal
from fastapi.testclient import TestClient
from numpy.typing import NDArray


# ── 工具：构造 heyi 返回的 wav bytes ─────────────────────────────────
#
# heyi 真实 wav 头部有 ``0xFFFFFFFF`` 长度（流式 placeholder），但 Python
# ``wave`` 模块仍能把 PCM 字节读出来，所以构造正常 wav 即可——验证 backend
# 对 wav→pcm 的转换链路。
def _make_wav(samples: NDArray[np.int16]) -> bytes:
    assert samples.dtype == np.int16
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def _make_voiced_wav(n_samples: int = 16_000) -> bytes:
    """大约 1s 的正弦波 16k mono；RMS 远大于 SILENCE_RMS_FLOOR。"""
    t = np.arange(n_samples, dtype=np.float32) / 16_000.0
    sig = (np.sin(2 * np.pi * 440 * t) * 0.4 * 32767).astype(np.int16)
    return _make_wav(sig)


def _make_silent_wav(n_samples: int = 8_000) -> bytes:
    return _make_wav(np.zeros(n_samples, dtype=np.int16))


def _patch_httpx_with_response(wav: bytes, *, content_type: str = "audio/wav") -> Any:
    resp = MagicMock()
    resp.content = wav
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    fake = MagicMock()
    fake.post = AsyncMock(return_value=resp)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    return patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake)


def _patch_httpx_raising(exc: Exception) -> Any:
    fake = MagicMock()
    fake.post = AsyncMock(side_effect=exc)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    return patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake)


def _mock_probe_tts(
    *,
    result: SynthesisResult | None = None,
    error: Exception | None = None,
) -> tuple[Qwen3TTS, AsyncMock]:
    tts = MagicMock(spec=Qwen3TTS)
    tts.default_voice = "aiden"
    tts.base_url = "http://10.20.30.40:8094"
    synthesize = AsyncMock(return_value=result, side_effect=error)
    tts.synthesize_detailed = synthesize
    return cast(Qwen3TTS, tts), synthesize


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_tts_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "tts_enabled": True,
        "tts_provider": "qwen3_tts",
        "tts_qwen3_url": "http://100.76.3.59:8094",
        "tts_qwen3_voice": "aiden",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def tts_singleton() -> Qwen3TTS:
    return Qwen3TTS(_make_tts_settings())


@pytest.fixture
def make_client(tts_singleton: Qwen3TTS) -> Callable[..., TestClient]:
    """生成 TestClient 工厂：每个测试可独立覆写 tts dep 或 settings。"""

    def _make(
        *,
        tts: Qwen3TTS | None = None,
        settings: Settings | None = None,
    ) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_tts_singleton] = lambda: tts or tts_singleton
        if settings is not None:
            from app.config import get_settings

            app.dependency_overrides[get_settings] = lambda: settings
        return TestClient(app)

    return _make


@pytest.fixture(autouse=True)
def _isolate_diag_cache() -> Iterator[None]:
    """每个 test 之间清掉 /tts/diag cache，避免 last test 污染。"""
    _reset_diag_cache_for_tests()
    yield
    _reset_diag_cache_for_tests()


# ── /tts/speak ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_speak_returns_pcm_for_normal_wav(
    make_client: Callable[..., TestClient],
) -> None:
    wav = _make_voiced_wav(16_000)
    with _patch_httpx_with_response(wav):
        client = make_client()
        r = client.post("/tts/speak", json={"text": "你好"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/pcm"
    # 16k mono 16-bit ≈ 32_000 bytes
    assert 16_000 <= len(r.content) <= 80_000


@pytest.mark.unit
def test_speak_rejects_silent_upstream_with_502(
    make_client: Callable[..., TestClient],
) -> None:
    """heyi 偶尔会回 200 + 全 0 PCM；以前 backend 直接转发 → 前端无错误显示。

    现在必须 502，前端能 message.error；这是"完全失效"症状的关键转折点。
    """
    wav = _make_silent_wav(8_000)
    with _patch_httpx_with_response(wav):
        client = make_client()
        r = client.post("/tts/speak", json={"text": "你好"})
    assert r.status_code == 502
    body = r.json()
    assert "tts_silent_output" in body["detail"]
    assert "rms" in body["detail"].lower()


@pytest.mark.unit
def test_speak_502_on_upstream_connection_error(
    make_client: Callable[..., TestClient],
) -> None:
    private_error = "connection refused by http://10.20.30.40:8094?token=secret"
    with _patch_httpx_raising(RuntimeError(private_error)):
        client = make_client()
        r = client.post("/tts/speak", json={"text": "你好"})
    assert r.status_code == 502
    assert r.json()["detail"] == "tts_upstream_error"
    assert "10.20.30.40" not in r.text
    assert "secret" not in r.text


@pytest.mark.unit
def test_speak_503_when_tts_disabled(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(settings=_make_tts_settings(tts_enabled=False))
    r = client.post("/tts/speak", json={"text": "你好"})
    assert r.status_code == 503
    assert "tts disabled" in r.json()["detail"]


# ── /tts/diag ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_diag_ok_when_upstream_returns_voiced_audio(
    make_client: Callable[..., TestClient],
) -> None:
    wav = _make_voiced_wav(16_000)
    with _patch_httpx_with_response(wav):
        client = make_client()
        r = client.get("/tts/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "ok"
    assert body["rms"] and body["rms"] > 50.0
    assert body["pcm_bytes"] and body["pcm_bytes"] > 0
    assert body["voice"] == "aiden"
    assert body["base_url"].endswith(":8094")


@pytest.mark.unit
def test_diag_silent_output(
    make_client: Callable[..., TestClient],
) -> None:
    wav = _make_silent_wav(8_000)
    with _patch_httpx_with_response(wav):
        client = make_client()
        r = client.get("/tts/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["state"] == "silent_output"
    assert "rms" in (body["detail"] or "").lower()


@pytest.mark.unit
def test_diag_upstream_error(
    make_client: Callable[..., TestClient],
) -> None:
    with _patch_httpx_raising(RuntimeError("network down")):
        client = make_client()
        r = client.get("/tts/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["state"] == "upstream_error"
    assert "network down" in (body["detail"] or "")


@pytest.mark.unit
def test_diag_not_configured_state(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(settings=_make_tts_settings(tts_enabled=False))
    r = client.get("/tts/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is None
    assert body["state"] == "not_configured"


@pytest.mark.unit
def test_diag_cache_avoids_repeated_upstream_calls(
    make_client: Callable[..., TestClient],
) -> None:
    """/tts/diag 30s cache；同一 cache 窗口里两次调用 → httpx 只发一次请求。

    防止 StatusBar 多客户端轮询打爆 heyi。
    """
    wav = _make_voiced_wav(16_000)
    resp = MagicMock()
    resp.content = wav
    resp.headers = {"content-type": "audio/wav"}
    resp.raise_for_status = MagicMock()
    fake = MagicMock()
    fake.post = AsyncMock(return_value=resp)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    with patch("app.adapters.tts.qwen3_tts.httpx.AsyncClient", return_value=fake):
        client = make_client()
        r1 = client.get("/tts/diag")
        r2 = client.get("/tts/diag")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["checked_at"] == r2.json()["checked_at"]
    # 关键断言：第二次走 cache，不发 httpx
    assert fake.post.await_count == 1


@pytest.mark.unit
def test_local_diag_fresh_query_param_bypasses_cache(
    make_client: Callable[..., TestClient],
) -> None:
    """?fresh=true 强刷绕过 cache（前端"重测合成"按钮用）。"""
    wav_voiced = _make_voiced_wav(16_000)
    wav_silent = _make_silent_wav(8_000)

    # 第一次返回有效音频
    with _patch_httpx_with_response(wav_voiced):
        client = make_client()
        r1 = client.get("/tts/diag")
    assert r1.json()["state"] == "ok"

    # 第二次（fresh）返回静音 → 没走 cache，结果反映新状态
    with _patch_httpx_with_response(wav_silent):
        r2 = client.get("/tts/diag?fresh=true")
    assert r2.json()["state"] == "silent_output"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_diag_concurrent_and_sequential_fresh_share_one_probe() -> None:
    result = SynthesisResult(
        pcm=b"\x01\x00" * 100,
        raw_bytes=b"wav",
        raw_content_type="audio/wav",
        rms=120.0,
        max_abs=256,
        latency_s=0.01,
    )
    tts, synthesize = _mock_probe_tts(result=result)
    settings = _make_tts_settings(public_demo_mode=True)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    context_token = bind_principal(principal)
    try:
        concurrent = await asyncio.gather(*(tts_diag(settings, tts, fresh=True) for _ in range(4)))
        sequential = await tts_diag(settings, tts, fresh=True)
    finally:
        reset_principal(context_token)

    assert synthesize.await_count == 1
    assert {item.checked_at for item in [*concurrent, sequential]} == {concurrent[0].checked_at}
    assert all(item.base_url is None for item in [*concurrent, sequential])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_diag_redacts_upstream_address_and_raw_error() -> None:
    tts, synthesize = _mock_probe_tts(
        error=TTSError("credential rejected by http://10.20.30.40:8094?token=secret")
    )
    settings = _make_tts_settings(public_demo_mode=True)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    context_token = bind_principal(principal)
    try:
        result = await tts_diag(settings, tts, fresh=True)
    finally:
        reset_principal(context_token)

    assert synthesize.await_count == 1
    assert result.state == "upstream_error"
    assert result.detail == "语音合成服务暂时不可用"
    assert result.base_url is None
    assert "10.20.30.40" not in result.model_dump_json()
    assert "secret" not in result.model_dump_json()


# ── 兼容性：silent_output 的 RMS 阈值能解释清楚 ───────────────────────


@pytest.mark.unit
def test_silence_threshold_below_real_voice() -> None:
    """SILENCE_RMS_FLOOR 必须远低于真实人声，避免误杀正常合成。"""
    from app.adapters.tts.qwen3_tts import SILENCE_RMS_FLOOR

    sig = (np.sin(np.linspace(0, 100 * np.pi, 16_000)) * 0.3 * 32767).astype(np.int16)
    rms = float(np.sqrt(np.mean(sig.astype(np.float64) ** 2)))
    # 正常合成 RMS 通常 ≥ 2000；阈值必须远小于这个，否则会误判
    assert rms / 10 > SILENCE_RMS_FLOOR
    assert SILENCE_RMS_FLOOR > 0
