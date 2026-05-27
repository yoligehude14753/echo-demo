"""Capture API schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.meeting import TranscriptSegment


class CaptureChunkResult(BaseModel):
    """POST /capture/chunk 响应。"""

    ambient_stored: bool = False
    ambient_text: str | None = None
    audio_ref: str = ""
    speaker_id: str | None = None
    speaker_label: str | None = None
    meeting_id: str | None = None  # 当前 chunk 被叠加进的 meeting（手动 / 自动 / 无）
    meeting_segments: list[TranscriptSegment] = Field(default_factory=list)
