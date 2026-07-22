"""Capture API schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.meeting import TranscriptSegment

# STT 处理结果分流标签（M_diag_brake / 7 道门诊断）。
#   ok            → STT 成功返回非空文本
#   empty         → STT 成功但返回空字符串（静音/底噪上 ASR 没"听到"内容）
#   failed        → STT 调用本身失败（超时、网络、5xx）
#   circuit_open  → 兼容旧版本/上游显式熔断信号，未发起或拒绝本次 STT
#   gated         → 前置 RMS/帧级 VAD 把整段挡在 STT 之前
#   unknown       → 兼容缺少 stt_status 的旧响应；不能被当作 ready
SttStatus = Literal["ok", "empty", "failed", "circuit_open", "gated", "unknown"]
CaptureStreamMode = Literal["free", "formal", "auto"]


class CaptureChunkResult(BaseModel):
    """POST /capture/chunk 响应。"""

    # 客户端生成的关联标识；仅用于跨 native upload 与 recent 的可观测性。
    segment_id: str | None = None
    ambient_stored: bool = False
    ambient_text: str | None = None
    # Internal/local responses retain the durable file reference. Public
    # transports project it to null because it is a server filesystem path.
    audio_ref: str | None = ""
    speaker_id: str | None = None
    speaker_label: str | None = None
    meeting_id: str | None = None  # 当前 chunk 被叠加进的 meeting（手动 / 自动 / 无）
    meeting_segments: list[TranscriptSegment] = Field(default_factory=list)
    # 7 道门处理结果分流标签；前端对 circuit_open 去抖后才触发短退避。
    stt_status: SttStatus = "unknown"
    device_id: str | None = None
    capture_mode: CaptureStreamMode = "free"


class CaptureControlUpdate(BaseModel):
    mode: Literal["single", "multi"]
    selectedDeviceIds: list[str] = Field(min_length=1)
    expectedRevision: int = Field(ge=0)


class CaptureAuthorizeRequest(BaseModel):
    deviceId: str = Field(min_length=1)
    revision: int = Field(ge=0)
