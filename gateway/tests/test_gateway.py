"""echo-gateway 单测：鉴权、限流、model 路由、STT/TTS/chat 转发（mock 上游）。"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import RateLimiter, extract_bearer
from app.config import GatewaySettings
from app.main import _router, app

client = TestClient(app)

GOOD = {"Authorization": "Bearer tok-good-1"}


def _fake_response(status: int, content: bytes, ctype: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        headers={"content-type": ctype},
        request=httpx.Request("POST", "https://upstream.test"),
    )


# ── 纯函数 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer xyz", "xyz"),
        ("Basic abc", None),
        ("abc", None),
        ("", None),
    ],
)
def test_extract_bearer(header: str, expected: str | None) -> None:
    req = httpx.Request("GET", "http://t", headers={"Authorization": header} if header else {})
    # 用 starlette Request 等价：直接构造最小对象
    from starlette.requests import Request as SReq

    scope = {
        "type": "http",
        "headers": [(b"authorization", header.encode())] if header else [],
    }
    assert extract_bearer(SReq(scope)) == expected
    _ = req


def test_rate_limiter_blocks_after_max() -> None:
    rl = RateLimiter(window_s=60, max_requests=3)
    assert rl.check("t") is True
    assert rl.check("t") is True
    assert rl.check("t") is True
    assert rl.check("t") is False  # 4th blocked
    assert rl.check("other") is True  # 不同 token 独立


def test_chat_upstream_routing_yunwu_vs_fast() -> None:
    s = GatewaySettings(
        yunwu_models="MiniMax-M2.7,GLM-4.6",
        yunwu_base_url="https://yunwu.test/v1",
        yunwu_open_key="sk-test-yunwu",
        heyi_fast_base_url="https://fast.test/v1",
        heyi_fast_key="EMPTY",
    )
    from app.upstream import UpstreamRouter

    r = UpstreamRouter(s)
    assert r._resolve_chat_upstream("MiniMax-M2.7") == ("https://yunwu.test/v1", "sk-test-yunwu")
    assert r._resolve_chat_upstream("Qwen3-1.7B") == ("https://fast.test/v1", "EMPTY")
    assert r._resolve_chat_upstream(None) == ("https://fast.test/v1", "EMPTY")


# ── 鉴权 ──────────────────────────────────────────────────
def test_health_no_auth() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "echo-gateway"


def test_chat_requires_token() -> None:
    r = client.post("/v1/chat/completions", json={"model": "MiniMax-M2.7"})
    assert r.status_code == 401


def test_chat_rejects_bad_token() -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "MiniMax-M2.7"},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


# ── 转发（mock 上游） ─────────────────────────────────────
def test_chat_forwards_and_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(body: dict) -> httpx.Response:
        assert body["model"] == "MiniMax-M2.7"
        return _fake_response(200, b'{"id":"x","choices":[]}', "application/json")

    monkeypatch.setattr(_router, "chat_completion", fake_chat)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "MiniMax-M2.7", "messages": [{"role": "user", "content": "hi"}]},
        headers=GOOD,
    )
    assert r.status_code == 200
    assert r.json()["id"] == "x"


def test_chat_stream_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stream(body: dict):
        assert body.get("stream") is True
        yield b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(_router, "chat_completion_stream", fake_stream)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "MiniMax-M2.7", "stream": True},
        headers=GOOD,
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert b"[DONE]" in body
    # 两个 delta chunk 原样透传
    assert b'"content":"he"' in body
    assert b'"content":"llo"' in body


def test_transcriptions_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_transcribe(*, file_bytes, filename, content_type, form):
        captured["bytes"] = file_bytes
        captured["form"] = form
        return _fake_response(200, b'{"text":"\xe4\xbd\xa0\xe5\xa5\xbd"}', "application/json")

    monkeypatch.setattr(_router, "transcribe", fake_transcribe)
    r = client.post(
        "/v1/audio/transcriptions",
        headers=GOOD,
        files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")},
        data={"language": "zh"},
    )
    assert r.status_code == 200
    assert captured["bytes"] == b"RIFFxxxx"
    assert captured["form"]["language"] == "zh"
    assert captured["form"]["model"] == "firered-asr-aed"  # default 注入


def test_embeddings_requires_token() -> None:
    r = client.post("/v1/embeddings", json={"model": "text-embedding-3-large", "input": "hi"})
    assert r.status_code == 401


def test_embeddings_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embed(body: dict) -> httpx.Response:
        assert body["model"] == "text-embedding-3-large"
        return _fake_response(200, b'{"data":[{"embedding":[0.1,0.2]}]}', "application/json")

    monkeypatch.setattr(_router, "embeddings", fake_embed)
    r = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-large", "input": "hi"},
        headers=GOOD,
    )
    assert r.status_code == 200
    assert r.json()["data"][0]["embedding"] == [0.1, 0.2]


def test_speech_forwards_audio_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_speech(body: dict) -> httpx.Response:
        assert body["input"] == "你好"
        return _fake_response(200, b"\x00\x01\x02\x03", "audio/wav")

    monkeypatch.setattr(_router, "speech", fake_speech)
    r = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "你好", "voice": "aiden"},
        headers=GOOD,
    )
    assert r.status_code == 200
    assert r.content == b"\x00\x01\x02\x03"
    assert r.headers["content-type"].startswith("audio/")


def test_rate_limit_429(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(body: dict) -> httpx.Response:
        return _fake_response(200, b"{}", "application/json")

    monkeypatch.setattr(_router, "chat_completion", fake_chat)
    # 用独立 token 避免污染其它用例（限额=5）
    hdr = {"Authorization": "Bearer tok-good-2"}
    codes = [
        client.post("/v1/chat/completions", json={"model": "x"}, headers=hdr).status_code
        for _ in range(7)
    ]
    assert codes.count(200) == 5
    assert codes.count(429) == 2
