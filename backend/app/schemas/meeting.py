"""会议 / 转写 / 纪要 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 纪要生成生命周期（与 ports.repository.MinutesStatus 对齐）：
#   None              → 会议进行中（state="in_meeting"）/ 尚未触发 finalize
#   "generating"      → finalize 正在跑（兜底，正常路径上 in_meeting 直接转 ok/failed）
#   "ok"              → 已成功生成（与 state="finalized" 同步）
#   "generation_failed" → LLM/JSON 失败；UI 应给「重试」入口
MinutesStatus = Literal["generating", "ok", "generation_failed"]


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
    minutes_status: MinutesStatus | None = None
    minutes_error: str | None = None
