"""HTTP /chat 端点单测（用 FakeLLM 替换 LLM 依赖）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.api.deps import get_llm_singleton as get_llm
from app.main import create_app
from app.schemas.llm import ChatMessage
from fastapi.testclient import TestClient


class FakeLLM:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def chat(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError

    async def chat_stream(self, _messages: list[ChatMessage], **_kw: Any) -> AsyncIterator[str]:
        for c in self.chunks:
            yield c


@pytest.fixture
def client_with_fake() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: FakeLLM(["你", "好"])
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
    assert deltas == ["你", "好"]


@pytest.mark.unit
def test_chat_empty_question_400(client_with_fake: TestClient) -> None:
    r = client_with_fake.post("/chat", json={"question": "   "})
    assert r.status_code == 400


@pytest.mark.unit
def test_chat_model_alias_accepted(client_with_fake: TestClient) -> None:
    for alias in ("MAIN", "FAST", "Qwen3-1.7B", None):
        r = client_with_fake.post("/chat", json={"question": "hi", "model": alias})
        assert r.status_code == 200
