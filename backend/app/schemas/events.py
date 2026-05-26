"""WebSocket 事件 schema：UI 清单式渲染的数据契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "meeting.started",
    "meeting.segment",
    "meeting.ended",
    "minutes.ready",
    "artifact.generating",
    "artifact.ready",
    "artifact.failed",
    "rag.query",
    "rag.answer.delta",
    "rag.answer.done",
    "chat.delta",
    "chat.done",
    "error",
]


class EchoEvent(BaseModel):
    type: EventType
    seq: int = 0
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    meeting_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
