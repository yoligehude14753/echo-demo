"""真跑：M_minutes_refactor 端到端 smoke。

跑法（在 worktree 根目录）::

    PYTHONPATH=backend /Users/yoligehude/Desktop/all/echo-demo/.venv/bin/python \
        scripts/smoke_minutes_refactor.py

输出：
- 临时 SQLite 路径
- finalize 后 ``meetings`` 表 tail（id / display_title / minutes_status / title）
- minutes_json 的 ``title`` / 前 3 个 todos
- artifact_id 回写到 todos[0] 后的最终 minutes_json todos 状态
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    async def identify(self, audio_bytes: bytes, *, sample_rate: int = 16_000) -> str | None:
        return None

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
            latency_ms=12.0,
        )

    async def chat_stream(self, _messages, **_):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="echo-smoke-"))
    db_path = tmp / "echo.db"
    print(f"sqlite : {db_path}")

    settings = Settings(storage_dir=tmp / "storage")
    repo = SQLiteRepository(db_path)
    await repo.init()
    bus = InMemoryEventBus()

    payload = {
        "title": "直播带货话术 + AI 编程营销讨论",
        "summary": "围绕直播带货话术与 AI 编程营销切入展开。",
        "sections": [
            {"heading": "话术结构", "bullets": ["开场抓注意", "中段对比"]},
            {"heading": "AI 编程演示", "bullets": ["现场跑通", "强调上线 5 分钟"]},
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
        settings=settings,
        stt=_FakeSTT(),
        diarizer=_FakeDiarizer(),
        rag=_FakeRag(),
        llm=_FakeLLM(json.dumps(payload, ensure_ascii=False)),
        event_bus=bus,
        repository=repo,
    )

    mid = f"m-{datetime.now(UTC).strftime('%H%M%S')}"
    await pipe.start_meeting(mid, title="先用 meeting_id 兜底")
    await pipe.append_segment(
        mid,
        TranscriptSegment(
            text="今天聊一下直播带货话术",
            start_ms=0,
            end_ms=2000,
            speaker_id="spk-A",
        ),
    )
    await pipe.append_segment(
        mid,
        TranscriptSegment(
            text="还有 AI 编程营销切入",
            start_ms=2000,
            end_ms=4000,
            speaker_id="spk-B",
        ),
    )
    minutes = await pipe.finalize_meeting(mid, title="先用 meeting_id 兜底")

    rec = await repo.get_meeting(mid)
    assert rec is not None
    print("\n=== meetings (sqlite) tail ===")
    print(
        json.dumps(
            {
                "id": rec.id,
                "title": rec.title,
                "display_title": rec.display_title,
                "minutes_status": rec.minutes_status,
                "state": rec.state,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print("\n=== minutes_json head ===")
    data = json.loads(rec.minutes_json or "{}")
    print(
        json.dumps(
            {
                "title": data.get("title"),
                "summary": data.get("summary"),
                "todos": data.get("todos", [])[:3],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # 回写第一条 todo 的 artifact
    first_todo_id = minutes.todos[0].id
    ok = await pipe.attach_artifact_to_todo(mid, first_todo_id, "art-demo-1")
    print(f"\nattach_artifact_to_todo(...): {ok}")
    rec2 = await repo.get_meeting(mid)
    data2 = json.loads(rec2.minutes_json or "{}")
    print("\n=== minutes_json.todos after artifact attach ===")
    print(json.dumps(data2["todos"], ensure_ascii=False, indent=2))

    # 验证事件
    todo_events = [e for e in bus._history if e.type == "meeting.todo.completed"]
    print(f"\nmeeting.todo.completed events: {len(todo_events)}")
    if todo_events:
        print(
            json.dumps(
                {
                    "type": todo_events[0].type,
                    "meeting_id": todo_events[0].meeting_id,
                    "payload": todo_events[0].payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    await repo.aclose()


if __name__ == "__main__":
    asyncio.run(main())
