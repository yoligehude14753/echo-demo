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
def test_intent_route_no_at_defaults_to_search_rag(client_kw_hit: TestClient) -> None:
    """2026-05-28：不带 @ 不命中关键字 → 默认 search_rag（=问 echo）。"""
    r = client_kw_hit.post("/intent/route", json={"text": "今天天气真好"})
    assert r.status_code == 200
    assert r.json()["kind"] == "search_rag"


@pytest.mark.unit
def test_intent_route_unknown_at_defaults_to_search_rag(client_llm_json: TestClient) -> None:
    """2026-05-28：@<未注册关键字> 也默认 search_rag，不再调 Fast LLM 误判。"""
    r = client_llm_json.post(
        "/intent/route",
        json={"text": "@发 项目申报书模板到内部群"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "search_rag"


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
