"""HTTP /chat 端点单测（用 FakeLLM 替换 LLM 依赖）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from app.adapters.llm import LLMError
from app.api.deps import get_llm_singleton as get_llm
from app.main import create_app
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from fastapi.testclient import TestClient


class FakeLLM:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.chat_kwargs: list[dict[str, Any]] = []
        self.stream_kwargs: list[dict[str, Any]] = []

    async def chat(self, _messages: list[ChatMessage], **_kw: Any) -> LLMResponse:
        self.chat_kwargs.append(_kw)
        return LLMResponse(
            content="".join(self.chunks),
            model=str(_kw.get("model") or "fake"),
            usage=LLMUsage(),
            latency_ms=1.0,
        )

    async def chat_stream(self, _messages: list[ChatMessage], **_kw: Any) -> AsyncIterator[str]:
        self.stream_kwargs.append(_kw)
        for c in self.chunks:
            yield c


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(["你", "好"])


@pytest.fixture
def client_with_fake(fake_llm: FakeLLM) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: fake_llm
    return TestClient(app)


@pytest.mark.unit
def test_chat_sse_streams_and_terminates(client_with_fake: TestClient) -> None:
    with client_with_fake.stream("POST", "/chat", json={"question": "你好"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes()).decode()
    lines = [ln for ln in body.split("\n") if ln.startswith("data:")]
    payloads = [ln[len("data: ") :] for ln in lines]
    assert payloads[-1] == "[DONE]"
    deltas = [json.loads(p)["delta"] for p in payloads[:-1]]
    assert deltas == ["你好"]


@pytest.mark.unit
def test_chat_sse_emits_error_event_without_done_after_llm_failure() -> None:
    class FailingLLM(FakeLLM):
        async def chat(self, _messages: list[ChatMessage], **_kw: Any) -> LLMResponse:
            raise LLMError("provider unavailable")

    app = create_app()
    app.dependency_overrides[get_llm] = lambda: FailingLLM([])

    with (
        TestClient(app) as client,
        client.stream("POST", "/chat", json={"question": "你好"}) as response,
    ):
        assert response.status_code == 200
        body = b"".join(response.iter_bytes()).decode()

    assert "event: error\n" in body
    assert '"type": "error"' in body
    assert '"code": "answer_generation_failed"' in body
    assert '"error": "暂时无法回复，请稍后重试"' in body
    assert "provider unavailable" not in body
    assert "data: [DONE]" not in body


@pytest.mark.unit
def test_chat_empty_question_400(client_with_fake: TestClient) -> None:
    r = client_with_fake.post("/chat", json={"question": "   "})
    assert r.status_code == 400


@pytest.mark.unit
def test_chat_rejects_two_megabyte_body_before_json_parsing(
    client_with_fake: TestClient,
) -> None:
    response = client_with_fake.post(
        "/chat",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


@pytest.mark.unit
def test_chat_rejects_chunked_body_without_content_length(
    client_with_fake: TestClient,
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 600_000
        yield b"y" * 600_000

    request = client_with_fake.build_request(
        "POST",
        "/chat",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"

    response = client_with_fake.send(request)

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


@pytest.mark.unit
def test_chat_model_alias_accepted(client_with_fake: TestClient) -> None:
    for alias in ("MAIN", "FAST", "Qwen3-1.7B", None):
        r = client_with_fake.post("/chat", json={"question": "hi", "model": alias})
        assert r.status_code == 200


@pytest.mark.unit
def test_chat_uses_short_generation_budget(
    client_with_fake: TestClient,
    fake_llm: FakeLLM,
) -> None:
    r = client_with_fake.post("/chat", json={"question": "只回答 pong"})
    assert r.status_code == 200
    assert fake_llm.chat_kwargs[-1]["max_tokens"] == 768
    assert fake_llm.chat_kwargs[-1]["timeout_s"] == 45.0
