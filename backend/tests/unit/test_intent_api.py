"""HTTP /intent/route 单测（注入 mock LLM）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.api.deps import get_llm_singleton as get_llm
from app.api.intent import reset_intent_router
from app.main import create_app
from app.schemas.llm import ChatMessage, LLMResponse
from fastapi.testclient import TestClient


class _StubLLM:
    def __init__(self, content: str = "") -> None:
        self.content = content

    async def chat(self, _messages: list[ChatMessage], **_kw: Any) -> LLMResponse:
        return LLMResponse(content=self.content, model="stub")

    def chat_stream(self, _messages: list[ChatMessage], **_kw: Any) -> AsyncIterator[str]:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _reset_router() -> None:
    reset_intent_router()


@pytest.fixture
def client_kw_hit() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: _StubLLM(content="不会被调用")
    return TestClient(app)


@pytest.fixture
def client_llm_json() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: _StubLLM(
        content='{"kind":"search_rag","confidence":0.7,"rationale":"找之前的"}'
    )
    return TestClient(app)


@pytest.mark.unit
def test_intent_route_keyword_pptx(client_kw_hit: TestClient) -> None:
    r = client_kw_hit.post(
        "/intent/route",
        json={"text": "@生成 PPT 英伟达 2025 投资展望", "current_meeting_id": "m1"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "generate_pptx"
    assert data["params"]["artifact_type"] == "pptx"
    assert "英伟达" in data["params"]["brief"]
    assert data["confidence"] >= 0.8


@pytest.mark.unit
def test_intent_route_no_at_returns_chat(client_kw_hit: TestClient) -> None:
    r = client_kw_hit.post("/intent/route", json={"text": "今天天气真好"})
    assert r.status_code == 200
    assert r.json()["kind"] == "chat"


@pytest.mark.unit
def test_intent_route_llm_search_rag(client_llm_json: TestClient) -> None:
    r = client_llm_json.post(
        "/intent/route",
        json={"text": "@翻一下我们上周对那个方案的讨论"},  # 命中“上周”=>无关键词、走 LLM
    )
    assert r.status_code == 200
    body = r.json()
    # 走 LLM 返回的 JSON
    assert body["kind"] in {"search_rag", "search_web", "chat"}


@pytest.mark.unit
def test_intent_route_empty_400(client_kw_hit: TestClient) -> None:
    r = client_kw_hit.post("/intent/route", json={"text": "   "})
    assert r.status_code == 400


@pytest.mark.unit
def test_intent_route_html(client_kw_hit: TestClient) -> None:
    r = client_kw_hit.post(
        "/intent/route",
        json={"text": "@生成 HTML 投资周报 含 SVG 柱图"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "generate_html"
    assert data["params"]["artifact_type"] == "html"
