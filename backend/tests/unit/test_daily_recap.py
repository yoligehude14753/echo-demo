"""today recap 单测：待办抽取（结构化）+ 空素材短路。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.api.recap import _clear_recap_cache_for_tests, recap_today
from app.ports.repository import AmbientSegmentRecord, MeetingRecord
from app.schemas.llm import LLMResponse
from app.use_cases.daily_recap import (
    _extract_todos,
    generate_daily_recap,
)


@pytest.mark.unit
def test_extract_todos_picks_only_todo_section() -> None:
    md = """今天主要在推进 EchoDesk 的语音体验。

## 今天聊了什么
- 调试了唤醒词（不算待办）
- 讨论了 TTS 延迟

## 待办与跟进
- 把时间感知检索接到语音问答 / 我，本周
- *跟设计确认* PPT 封面样式
1. 回归测试 agent 闭环守卫

## 值得记住的点
- 用户偏好持续陪伴定位
"""
    todos = _extract_todos(md)
    assert todos == [
        "把时间感知检索接到语音问答 / 我，本周",
        "跟设计确认 PPT 封面样式",
        "回归测试 agent 闭环守卫",
    ]


@pytest.mark.unit
def test_extract_todos_empty_when_no_section() -> None:
    md = "## 今天聊了什么\n- 只是闲聊\n## 值得记住的点\n- 无"
    assert _extract_todos(md) == []
    assert _extract_todos("") == []


@pytest.mark.unit
def test_extract_todos_recognizes_english_headings() -> None:
    md = "## Action Items\n- ping vendor\n- review PR"
    assert _extract_todos(md) == ["ping vendor", "review PR"]


class _EmptyRepo:
    """只实现 recap 用到的两个方法，返回空。"""

    async def list_ambient_segments(self, **_: Any) -> list[Any]:
        return []

    async def list_meetings(self, **_: Any) -> list[Any]:
        return []


class _NoCallLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - 不应被调用
        self.calls += 1
        raise AssertionError("空素材不应调用 LLM")


class _OneAmbientRepo:
    async def list_ambient_segments(self, **_: Any) -> list[AmbientSegmentRecord]:
        return [
            AmbientSegmentRecord(
                audio_ref="a.wav",
                text="今天记得跟进客户方案。",
                captured_at=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
                speaker_label="我",
            )
        ]

    async def list_meetings(self, **_: Any) -> list[Any]:
        return []


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *_: Any, **__: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=(
                "今天主要在跟进客户方案。\n\n"
                "## 待办与跟进\n"
                "- 跟进客户方案 / 今天\n"
            ),
            model="test",
        )


class _UntitledMeetingRepo:
    async def list_ambient_segments(self, **_: Any) -> list[Any]:
        return []

    async def list_meetings(self, **_: Any) -> list[MeetingRecord]:
        return [
            MeetingRecord(
                id="meeting-no-title",
                title=None,
                display_title=None,
                state="in_meeting",
                started_at=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
                minutes_json=None,
            )
        ]


class _MeetingLLM:
    def __init__(self) -> None:
        self.last_user = ""

    async def chat(self, messages: list[Any], **__: Any) -> LLMResponse:
        self.last_user = messages[-1].content
        return LLMResponse(content="今天有一个进行中的会议。", model="test")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_material_short_circuits_without_llm() -> None:
    llm = _NoCallLLM()
    now = datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    recap = await generate_daily_recap(repository=_EmptyRepo(), llm=llm, now=now)  # type: ignore[arg-type]
    assert recap.empty is True
    assert recap.todos == []
    assert llm.calls == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_recap_api_reuses_short_ttl_cache() -> None:
    _clear_recap_cache_for_tests()
    llm = _CountingLLM()
    repo = _OneAmbientRepo()

    first = await recap_today(repository=repo, llm=llm, force=False)  # type: ignore[arg-type]
    second = await recap_today(repository=repo, llm=llm, force=False)  # type: ignore[arg-type]

    assert llm.calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.todos == ["跟进客户方案 / 今天"]
    _clear_recap_cache_for_tests()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_recap_api_force_bypasses_cache() -> None:
    _clear_recap_cache_for_tests()
    llm = _CountingLLM()
    repo = _OneAmbientRepo()

    await recap_today(repository=repo, llm=llm, force=False)  # type: ignore[arg-type]
    forced = await recap_today(repository=repo, llm=llm, force=True)  # type: ignore[arg-type]

    assert llm.calls == 2
    assert forced.cached is False
    _clear_recap_cache_for_tests()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_untitled_meeting_uses_id_fallback() -> None:
    llm = _MeetingLLM()
    recap = await generate_daily_recap(
        repository=_UntitledMeetingRepo(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        now=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )

    assert recap.empty is False
    assert "meeting-no-title" in llm.last_user
