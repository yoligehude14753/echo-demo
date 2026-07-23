"""HTTP focused coverage for the strict intent-plan contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.api.deps import get_llm_singleton as get_llm
from app.api.intent import reset_intent_router
from app.main import create_app
from app.schemas.llm import ChatMessage, LLMResponse
from fastapi.testclient import TestClient


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, _messages: list[ChatMessage], **_kw: Any) -> LLMResponse:
        return LLMResponse(content=self.content, model="stub")

    def chat_stream(self, _messages: list[ChatMessage], **_kw: Any) -> AsyncIterator[str]:
        raise NotImplementedError


def _plan(target: str, builtin: str | None = None) -> str:
    return json.dumps(
        {
            "goal": "完成请求",
            "execution_target": target,
            "builtin_intent": builtin,
            "available_context": [],
            "steps": ["执行"],
            "critical_constraints": [],
            "missing_constraints": [],
            "assumptions": [],
            "clarification_questions": [],
            "confidence": 0.9,
            "execution_authorized": True,
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def _reset_router() -> None:
    reset_intent_router()


def test_explicit_at_enters_main_plan_gate() -> None:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: _StubLLM(_plan("builtin_skill", "generate_html"))
    with TestClient(app) as client:
        response = client.post("/intent/route", json={"text": "@生成 HTML 周报"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "generate_html"
    assert body["params"]["ready_to_execute"] is True


def test_http_invalid_plan_does_not_authorize_dispatch() -> None:
    app = create_app()
    app.dependency_overrides[get_llm] = lambda: _StubLLM("invalid")
    with TestClient(app) as client:
        response = client.post("/intent/route", json={"text": "@生成 PDF 合同"})
    assert response.status_code == 200
    assert response.json()["params"]["ready_to_execute"] is False
