"""会议 / 转写 / 纪要 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    """一段 STT 转写结果。"""

    text: str
    start_ms: int
    end_ms: int
    speaker_id: str | None = None  # 由 Diarizer 填
    speaker_label: str | None = None  # "说话人1" / "说话人2" 等可读名


class MinutesSection(BaseModel):
    heading: str
    bullets: list[str] = Field(default_factory=list)


class MeetingMinutes(BaseModel):
    meeting_id: str
    title: str
    duration_sec: int
    speakers: list[str] = Field(default_factory=list)
    summary: str
    sections: list[MinutesSection] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    raw_transcript_ref: str | None = None  # 落盘文件 ref
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MeetingStatus(BaseModel):
    meeting_id: str
    state: Literal["idle", "in_meeting", "ended"]
    started_at: datetime | None = None
    ended_at: datetime | None = None


class MeetingSummary(BaseModel):
    """会议列表条目（左侧面板用）。

    与 ``MeetingRecord`` 的区别：聚合了 segments / speakers 计数，前端无需再
    join。``state`` 沿用 repository 的三态（in_meeting / ended / finalized），
    前端会把 finalized 视作 ended。
    """

    meeting_id: str
    title: str | None = None
    state: Literal["in_meeting", "ended", "finalized"]
    started_at: datetime
    ended_at: datetime | None = None
    finalized_at: datetime | None = None
    n_segments: int = 0
    n_speakers: int = 0
    has_minutes: bool = False
