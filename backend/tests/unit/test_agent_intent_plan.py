"""Focused contract tests for the planner → embedded Claude task handoff."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.agents.base import AgentIntent
from app.api import agents as agents_api
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse
from fastapi import HTTPException


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "echo",
        rag_index_dir=tmp_path / "rag",
        llm_main_model="deepseek-v4-flash",
        llm_fast_model="gpt-5.4-nano",
        yunwu_api_key="test",
        yunwu_base_url="http://localhost",
        heyi_base_url="http://localhost",
    )


def _plan(
    target: str,
    *,
    available_context: list[str],
) -> str:
    return json.dumps(
        {
            "goal": "根据会议资料整理竞品并形成可交付报告",
            "execution_target": target,
            "builtin_intent": None,
            "available_context": available_context,
            "steps": ["阅读可用资料", "在工作区完成调研和报告"],
            "critical_constraints": ["仅使用已授权工作区和明确提供的资料"],
            "missing_constraints": [],
            "assumptions": [],
            "clarification_questions": [],
            "confidence": 0.91,
            "execution_authorized": True,
        },
        ensure_ascii=False,
    )


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[ChatMessage] = []

    async def chat(self, messages: list[ChatMessage], **_kwargs: object) -> LLMResponse:
        self.messages = messages
        return LLMResponse(content=self.content, model="deepseek-v4-flash")

    def chat_stream(self, _messages: list[ChatMessage], **_kwargs: object) -> AsyncIterator[str]:
        raise NotImplementedError


class _CapturingService:
    def __init__(self) -> None:
        self.intent: AgentIntent | None = None

    async def submit_task(self, intent: AgentIntent) -> object:
        self.intent = intent
        return object()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorized_claude_plan_reaches_agent_service_with_server_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ["当前会话：竞品评审", "可用资料：访谈纪要.md"]
    llm = _StubLLM(_plan("claude_code_runtime", available_context=context))
    service = _CapturingService()
    monkeypatch.setattr(agents_api, "_task_dto", lambda _rec, _settings: {"accepted": True})

    result = await agents_api.create_task(
        agents_api.AgentTaskCreateRequest(
            text="用浏览器整理竞品并在工作区写成报告",
            context={
                "meeting_id": "meeting-42",
                # A client-provided plan must never reach Claude Code unchanged.
                "intent_plan": {"execution_target": "builtin_skill"},
            },
            available_context=context,
        ),
        settings=_settings(tmp_path),
        llm=llm,
        service=service,  # type: ignore[arg-type]
    )

    assert result == {"accepted": True}
    assert service.intent is not None
    assert service.intent.context["meeting_id"] == "meeting-42"
    plan = service.intent.context["intent_plan"]
    assert isinstance(plan, dict)
    assert plan["execution_target"] == "claude_code_runtime"
    assert plan["available_context"] == context
    assert plan["steps"] == ["阅读可用资料", "在工作区完成调研和报告"]
    assert "meeting-42" not in llm.messages[1].content
    assert "竞品评审" in llm.messages[1].content
    assert "访谈纪要.md" in llm.messages[1].content
    assert "当前会议：已选定，可用于会议总结" in llm.messages[1].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_claude_plan_cannot_submit_an_agent_task(tmp_path: Path) -> None:
    llm = _StubLLM(_plan("conversation", available_context=[]))
    service = _CapturingService()

    with pytest.raises(HTTPException, match="authorized intent plan") as exc_info:
        await agents_api.create_task(
            agents_api.AgentTaskCreateRequest(text="你好"),
            settings=_settings(tmp_path),
            llm=llm,
            service=service,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert service.intent is None
