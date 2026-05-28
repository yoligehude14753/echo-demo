"""Capture API schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.meeting import TranscriptSegment

# STT 处理结果分流标签（M_diag_brake / 7 道门诊断）。
#   ok            → STT 成功返回非空文本
#   empty         → STT 成功但返回空字符串（静音/底噪上 ASR 没"听到"内容）
#   failed        → STT 调用本身失败（超时、网络、5xx）
#   circuit_open  → 上游 STT 熔断器开（连续 3 次失败 + 冷却中），未发起请求
#   gated         → 前置 RMS/帧级 VAD 把整段挡在 STT 之前
SttStatus = Literal["ok", "empty", "failed", "circuit_open", "gated"]


class CaptureChunkResult(BaseModel):
    """POST /capture/chunk 响应。"""

    ambient_stored: bool = False
    ambient_text: str | None = None
    audio_ref: str = ""
    speaker_id: str | None = None
    speaker_label: str | None = None
    meeting_id: str | None = None  # 当前 chunk 被叠加进的 meeting（手动 / 自动 / 无）
    meeting_segments: list[TranscriptSegment] = Field(default_factory=list)
    # 7 道门处理结果分流标签，前端 captureChunkRouter 用 circuit_open 触发优雅止血
    stt_status: SttStatus = "ok"
