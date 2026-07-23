"""Focused contract tests: every chat entry must pass the V4 Flash plan gate."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.adapters.intent.llm_router import LLMIntentRouter
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "echo",
        rag_index_dir=tmp_path / "rag",
        llm_main_model="deepseek-v4-flash",
        # This is deliberately not a verifiable Qwen3 8B id.
        llm_fast_model="gpt-5.4-nano",
        yunwu_api_key="test",
        yunwu_base_url="http://localhost",
        heyi_base_url="http://localhost",
    )


class _MockLLM:
    def __init__(self, content: str | None = None, *, error: Exception | None = None) -> None:
        self.content = content or ""
        self.error = error
        self.options: list[dict[str, object]] = []
        self.messages: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], **kwargs: object) -> LLMResponse:
        self.messages.append(messages)
        self.options.append(kwargs)
        if self.error:
            raise self.error
        return LLMResponse(content=self.content, model=str(kwargs.get("model") or "mock"))

    def chat_stream(self, _messages: list[ChatMessage], **_kwargs: object) -> AsyncIterator[str]:
        raise NotImplementedError


def _plan(
    target: str,
    *,
    builtin: str | None = None,
    missing: list[str] | None = None,
    questions: list[str] | None = None,
    authorized: bool = True,
    confidence: float = 0.91,
) -> str:
    return json.dumps(
        {
            "goal": "完成用户请求",
            "execution_target": target,
            "builtin_intent": builtin,
            "available_context": [],
            "steps": ["根据已给信息执行"],
            "critical_constraints": ["不虚构资料"],
            "missing_constraints": missing or [],
            "assumptions": ["仅在用户确认前作为草案"],
            "clarification_questions": questions or [],
            "confidence": confidence,
            "execution_authorized": authorized,
        },
        ensure_ascii=False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_at_keyword_only_hints_and_main_plan_authorizes_skill(
    tmp_path: Path,
) -> None:
    llm = _MockLLM(_plan("builtin_skill", builtin="generate_pptx"))
    result = await LLMIntentRouter(_settings(tmp_path), llm).route("@生成 PPT 投资复盘")

    assert result.kind == "generate_pptx"
    assert result.params["ready_to_execute"] is True
    assert result.params["artifact_type"] == "pptx"
    assert llm.options == [
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 1600,
            "temperature": 0.0,
            "timeout_s": 12.0,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("builtin", "command"),
    [
        ("generate_html", "@生成 HTML 周报"),
        ("generate_pptx", "@生成 PPT 周报"),
        ("generate_xlsx", "@生成 Excel 预算表"),
        ("generate_word", "@生成 Word 方案"),
        ("generate_markdown", "@生成 Markdown 笔记"),
        ("generate_pdf", "@生成 PDF 简历"),
        ("generate_txt", "@生成 TXT 清单"),
        ("summarize_meeting", "@总结当前会议"),
        ("search_web", "@查 最新市场数据"),
        ("search_rag", "@查 当前会议要点"),
    ],
)
async def test_every_builtin_entry_needs_a_valid_main_plan(
    tmp_path: Path, builtin: str, command: str
) -> None:
    llm = _MockLLM(_plan("builtin_skill", builtin=builtin))
    result = await LLMIntentRouter(_settings(tmp_path), llm).route(command)

    assert result.kind == builtin
    assert result.params["ready_to_execute"] is True
    assert len(llm.options) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_constraints_never_authorize_keyword_skill(tmp_path: Path) -> None:
    llm = _MockLLM(
        _plan(
            "builtin_skill",
            builtin="generate_word",
            missing=["受众"],
            questions=["这份文档给谁使用？"],
            authorized=False,
            confidence=0.42,
        )
    )
    result = await LLMIntentRouter(_settings(tmp_path), llm).route("@生成 Word 方案")

    assert result.kind == "generate_word"
    assert result.params["ready_to_execute"] is False
    assert "artifact_type" not in result.params
    assert "给谁" in str(result.params["required_clarification"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_builtin_execution_routes_to_real_claude_runtime_target(tmp_path: Path) -> None:
    llm = _MockLLM(_plan("claude_code_runtime"))
    result = await LLMIntentRouter(_settings(tmp_path), llm).route(
        "用浏览器整理竞品并写入工作区",
        current_meeting_id="meeting-42",
    )

    assert result.kind == "agent_task"
    assert result.params["ready_to_execute"] is True
    assert result.params["execution_target"] == "claude_code_runtime"
    assert result.params["text"] == "用浏览器整理竞品并写入工作区"
    envelope = json.loads(llm.messages[0][1].content)
    assert "当前会议：已选定，可用于会议总结" in envelope["available_context"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pure_conversation_has_no_builtin_skill_dispatch(tmp_path: Path) -> None:
    llm = _MockLLM(_plan("conversation"))
    result = await LLMIntentRouter(_settings(tmp_path), llm).route("你好，今天怎么样？")

    assert result.kind == "chat"
    assert result.params["ready_to_execute"] is True
    assert result.params["execution_target"] == "conversation"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_conversation_target_is_normalized_without_bypassing_the_plan(
    tmp_path: Path,
) -> None:
    llm = _MockLLM(_plan("conversational_response"))

    result = await LLMIntentRouter(_settings(tmp_path), llm).route("你好，今天怎么样？")

    assert result.kind == "chat"
    assert result.params["execution_target"] == "conversation"
    assert len(llm.options) == 1
    assert "artifact_type" not in result.params


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_or_failed_llm_plan_fails_closed(tmp_path: Path) -> None:
    llm = _MockLLM("not json")
    result = await LLMIntentRouter(_settings(tmp_path), llm).route("@生成 HTML 周报")

    assert result.params["ready_to_execute"] is False
    assert result.params["execution_target"] == "clarification"
    assert "artifact_type" not in result.params


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_v4_flash_main_model_is_not_used_as_a_planner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.llm_main_model = "gpt-5.4-nano"
    llm = _MockLLM(_plan("builtin_skill", builtin="generate_html"))
    result = await LLMIntentRouter(settings, llm).route("@生成 HTML 周报")

    assert result.params["ready_to_execute"] is False
    assert llm.options == []
