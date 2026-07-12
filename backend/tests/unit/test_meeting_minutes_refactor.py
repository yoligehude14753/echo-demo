"""M_minutes_refactor 新增单测：

覆盖：
1. ``MeetingPipeline._extract_display_title``：LLM 返 / 不返 / 返垃圾 时的兜底
2. ``MeetingPipeline._parse_todos``：合规 / 缺字段 / 非法 kind / 非 @ command
3. ``finalize_meeting`` 一次跑：LLM 同时返 title + todos，验证
   - ``MeetingMinutes.title`` 用 LLM 给的语义化标题
   - ``MeetingMinutes.todos`` 含 actionable + info 各 1 条
   - ``minutes_json.todos`` 与 ``meetings.display_title`` 都正确落库（sqlite repo 真跑）
4. ``attach_artifact_to_todo``：成功路径回写 status=done + artifact_id，事件发出
5. ``attach_artifact_to_todo``：未知 meeting / todo_id → 返 False 且不抛错
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.schemas.meeting import TranscriptSegment
from app.schemas.rag import RagChunk
from app.use_cases.meeting_pipeline import MeetingPipeline


class _FakeSTT:
    async def transcribe(
        self, audio_bytes: bytes, *, sample_rate: int = 16_000, language: str = "zh"
    ) -> list[TranscriptSegment]:
        return []


class _FakeDiarizer:
    def __init__(self, ids: list[str | None]) -> None:
        self._q = list(ids)

    async def identify(self, audio_bytes: bytes, *, sample_rate: int = 16_000) -> str | None:
        if not self._q:
            return None
        return self._q.pop(0)

    async def reset(self) -> None:
        return None


class _FakeRag:
    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        return "pdf"

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        return f"doc-{meeting_id}"

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        return []

    async def delete(self, doc_id: str) -> None:
        return None


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
        return LLMResponse(
            content=self.content,
            model="MiniMax-M2.7",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=11.0,
        )

    async def chat_stream(self, _messages: list[ChatMessage], **_: Any):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


def _settings(tmp_path: Path) -> Settings:
    return Settings(storage_dir=tmp_path / "storage")


# ── _extract_display_title ────────────────────────────────────────────


@pytest.mark.unit
def test_extract_display_title_uses_fallback_when_empty_or_none() -> None:
    assert MeetingPipeline._extract_display_title(None, fallback="预算评审") == "预算评审"
    assert MeetingPipeline._extract_display_title("", fallback="预算评审") == "预算评审"
    assert MeetingPipeline._extract_display_title("   ", fallback="预算评审") == "预算评审"
    assert MeetingPipeline._extract_display_title(123, fallback="预算评审") == "预算评审"


@pytest.mark.unit
def test_extract_display_title_rejects_meeting_id_lookalikes() -> None:
    # m-bdd1da4e7e21 / auto-xxx → 视为无效，走 fallback
    assert (
        MeetingPipeline._extract_display_title("m-bdd1da4e7e21", fallback="销售例会") == "销售例会"
    )
    assert (
        MeetingPipeline._extract_display_title("auto-1234abcd", fallback="销售例会") == "销售例会"
    )


@pytest.mark.unit
def test_extract_display_title_truncates_at_18_chars() -> None:
    long = "直播带货话术与AI编程营销讨论的二期复盘"  # > 18 字
    out = MeetingPipeline._extract_display_title(long, fallback="x")
    assert len(out) == 18
    assert out == long[:18]


@pytest.mark.unit
def test_extract_display_title_preserves_valid_semantic_title() -> None:
    title = "直播带货话术 + AI 编程营销讨论"
    assert MeetingPipeline._extract_display_title(title, fallback="x") == title


# ── _parse_todos ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_todos_normalizes_well_formed_payload() -> None:
    todos = MeetingPipeline._parse_todos(
        [
            {
                "text": "生成 Q3 销售拆解 PPT",
                "assignee": "说话人1",
                "kind": "actionable",
                "suggested_command": "@生成 PPT Q3 销售拆解",
            },
            {
                "text": "下周再讨论价格策略",
                "assignee": None,
                "kind": "info",
                "suggested_command": None,
            },
        ]
    )
    assert len(todos) == 2
    t1, t2 = todos
    assert t1.text == "生成 Q3 销售拆解 PPT"
    assert t1.kind == "actionable"
    assert t1.status == "pending"
    assert t1.id.startswith("t-")
    assert t1.assignee == "说话人1"
    assert t1.suggested_command == "@生成 PPT Q3 销售拆解"
    assert t2.kind == "info"
    assert t2.suggested_command is None


@pytest.mark.unit
def test_parse_todos_skips_invalid_entries() -> None:
    todos = MeetingPipeline._parse_todos(
        [
            None,
            {},  # 缺 text
            {"text": "   "},  # 空白
            "not a dict",
            {"text": "ok 待办", "kind": "weird-kind"},
        ]
    )
    assert len(todos) == 1
    assert todos[0].text == "ok 待办"
    # 非法 kind → 兜底 info
    assert todos[0].kind == "info"


@pytest.mark.unit
def test_parse_todos_strips_non_at_prefix_suggested_command() -> None:
    todos = MeetingPipeline._parse_todos(
        [
            {
                "text": "做 Q3 表",
                "kind": "actionable",
                # 不以 @ 开头 → 视为无效 prefill，丢弃 suggested_command
                "suggested_command": "生成 PPT",
            },
            {
                "text": "info 类不应有 prefill",
                "kind": "info",
                "suggested_command": "@应该被丢弃",
            },
        ]
    )
    assert todos[0].suggested_command is None
    assert todos[1].suggested_command is None


@pytest.mark.unit
def test_parse_todos_returns_empty_for_non_list() -> None:
    assert MeetingPipeline._parse_todos(None) == []
    assert MeetingPipeline._parse_todos("nope") == []
    assert MeetingPipeline._parse_todos({"todos": []}) == []


# ── finalize_meeting 真跑（sqlite repo + bus）──────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_finalize_persists_display_title_and_todos_to_sqlite(tmp_path: Path) -> None:
    """端到端：finalize → sqlite display_title + minutes_json.todos 都对。"""
    db_path = tmp_path / "echo.db"
    repo = SQLiteRepository(db_path)
    await repo.init()
    bus = InMemoryEventBus()

    payload = {
        "title": "直播带货话术 + AI 编程营销讨论",
        "summary": "讨论了直播话术和 AI 编程演示的串联。",
        "sections": [
            {"heading": "话术结构", "bullets": ["开场 3 秒抓注意", "中段做对比"]},
            {"heading": "AI 编程演示", "bullets": ["现场 Cursor 跑通", "强调上线 5 分钟"]},
        ],
        "decisions": ["下周一上线 v0.2"],
        "todos": [
            {
                "text": "生成 Q3 销售拆解 PPT",
                "assignee": "说话人1",
                "kind": "actionable",
                "suggested_command": "@生成 PPT Q3 销售拆解",
            },
            {
                "text": "整理产品 FAQ 文档",
                "assignee": "说话人2",
                "kind": "actionable",
                "suggested_command": "@生成 Word 产品 FAQ",
            },
            {
                "text": "下周再讨论佣金分配",
                "assignee": None,
                "kind": "info",
                "suggested_command": None,
            },
        ],
    }
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=_FakeSTT(),
        diarizer=_FakeDiarizer([]),
        rag=_FakeRag(),
        llm=_FakeLLM(json.dumps(payload, ensure_ascii=False)),
        event_bus=bus,
        repository=repo,
    )

    # 先 start，让 segments 走 append_segment（避免依赖真 STT）
    await pipe.start_meeting("m-test1", title="先用 meeting_id 兜底")
    await pipe.append_segment(
        "m-test1",
        TranscriptSegment(
            text="今天聊一下直播带货话术", start_ms=0, end_ms=2000, speaker_id="spk-A"
        ),
    )
    await pipe.append_segment(
        "m-test1",
        TranscriptSegment(
            text="还有 AI 编程营销切入", start_ms=2000, end_ms=4000, speaker_id="spk-B"
        ),
    )

    minutes = await pipe.finalize_meeting("m-test1", title="先用 meeting_id 兜底")

    # 1) 返回值
    assert minutes.title == "直播带货话术 + AI 编程营销讨论"
    assert len(minutes.todos) == 3
    actionable = [t for t in minutes.todos if t.kind == "actionable"]
    assert len(actionable) == 2
    assert all(t.suggested_command and t.suggested_command.startswith("@") for t in actionable)
    # 兼容 action_items 字段（用于旧 UI）
    assert minutes.action_items == [t.text for t in minutes.todos]

    # 2) sqlite display_title
    rec = await repo.get_meeting("m-test1")
    assert rec is not None
    assert rec.display_title == "直播带货话术 + AI 编程营销讨论"
    # title 仍然是用户/系统传入的原始值
    assert rec.title == "先用 meeting_id 兜底"
    assert rec.minutes_status == "ok"
    assert rec.state == "finalized"

    # 3) minutes_json.todos
    assert rec.minutes_json
    minutes_data = json.loads(rec.minutes_json)
    assert minutes_data["title"] == "直播带货话术 + AI 编程营销讨论"
    assert len(minutes_data["todos"]) == 3
    assert minutes_data["todos"][0]["status"] == "pending"
    assert minutes_data["todos"][0]["id"].startswith("t-")

    # 4) minutes.ready 事件已发
    assert any(e.type == "minutes.ready" for e in bus.recent_events_for_current_scope())

    await repo.aclose()


# ── attach_artifact_to_todo ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_attach_artifact_to_todo_marks_done_and_publishes_event(tmp_path: Path) -> None:
    db_path = tmp_path / "echo.db"
    repo = SQLiteRepository(db_path)
    await repo.init()
    bus = InMemoryEventBus()

    payload = {
        "title": "x",
        "summary": "y",
        "sections": [{"heading": "h", "bullets": ["1", "2"]}],
        "decisions": [],
        "todos": [
            {
                "text": "生成 PPT",
                "kind": "actionable",
                "suggested_command": "@生成 PPT",
            }
        ],
    }
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=_FakeSTT(),
        diarizer=_FakeDiarizer([]),
        rag=_FakeRag(),
        llm=_FakeLLM(json.dumps(payload, ensure_ascii=False)),
        event_bus=bus,
        repository=repo,
    )
    await pipe.start_meeting("m-att1", title="x")
    await pipe.append_segment(
        "m-att1",
        TranscriptSegment(text="一段", start_ms=0, end_ms=1000, speaker_id="spk-A"),
    )
    minutes = await pipe.finalize_meeting("m-att1", title="x")
    todo_id = minutes.todos[0].id

    # 真正触发回写
    ok = await pipe.attach_artifact_to_todo("m-att1", todo_id, "art-123")
    assert ok is True

    rec = await repo.get_meeting("m-att1")
    assert rec is not None and rec.minutes_json
    data = json.loads(rec.minutes_json)
    assert data["todos"][0]["status"] == "done"
    assert data["todos"][0]["artifact_id"] == "art-123"
    assert data["todos"][0]["done_at"]  # 任何 iso 字符串

    todo_events = [
        e for e in bus.recent_events_for_current_scope() if e.type == "meeting.todo.completed"
    ]
    assert len(todo_events) == 1
    assert todo_events[0].payload["todo_id"] == todo_id
    assert todo_events[0].payload["artifact_id"] == "art-123"

    await repo.aclose()


# ── GET /meetings 返 display_title ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_meetings_surfaces_display_title(tmp_path: Path) -> None:
    """meetings.display_title 列写入后，GET /meetings 必须返这个字段。

    防御场景（用户需求 #2）：左侧列表当前显示 ``m-bdd1da4e7e21``，必须改成
    展示 LLM 生成的语义化标题。
    """
    from datetime import UTC, datetime

    from app.api.deps import (
        get_repository,
        reset_deps_for_test,
    )
    from app.api.meetings import reset_meeting_pipeline
    from app.config import get_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    db_path = tmp_path / "echo.db"
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        await repo.create_meeting(
            "m-llmtitle",
            started_at=datetime(2026, 5, 28, 9, 0, tzinfo=UTC),
            title="兜底原始 title",
        )
        await repo.update_meeting_state(
            "m-llmtitle",
            state="finalized",
            display_title="直播带货话术 + AI 编程营销讨论",
            minutes_json='{"meeting_id":"m-llmtitle","title":"直播带货话术 + AI 编程营销讨论","duration_sec":120,"summary":"x","sections":[],"decisions":[],"todos":[],"action_items":[]}',
            minutes_status="ok",
        )

        reset_deps_for_test()
        reset_meeting_pipeline()
        app = create_app()
        settings = _settings(tmp_path)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo
        client = TestClient(app)
        r = client.get("/meetings")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["meeting_id"] == "m-llmtitle"
        assert row["display_title"] == "直播带货话术 + AI 编程营销讨论"
        # title 字段保留原值
        assert row["title"] == "兜底原始 title"
    finally:
        await repo.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_attach_artifact_to_todo_returns_false_on_unknown_targets(tmp_path: Path) -> None:
    db_path = tmp_path / "echo.db"
    repo = SQLiteRepository(db_path)
    await repo.init()
    pipe = MeetingPipeline(
        settings=_settings(tmp_path),
        stt=_FakeSTT(),
        diarizer=_FakeDiarizer([]),
        rag=_FakeRag(),
        llm=_FakeLLM("{}"),
        repository=repo,
    )
    # 完全不存在的 meeting
    assert await pipe.attach_artifact_to_todo("nope", "t-x", "art-1") is False

    # 存在 meeting 但 minutes_json 为空
    from datetime import UTC, datetime

    await repo.create_meeting(
        "m-empty",
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
    )
    assert await pipe.attach_artifact_to_todo("m-empty", "t-x", "art-1") is False

    await repo.aclose()
