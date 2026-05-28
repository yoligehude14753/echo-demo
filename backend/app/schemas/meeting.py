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


# ── 待办（TodoList，M_minutes_refactor 引入）─────────────────────
# 设计意图：
# - 替代以前的 action_items 纯字符串列表，给每条带 assignee（说话人 N / 人名）和
#   kind（actionable | info）；UI 据此渲染 checkbox + tag + 一键执行。
# - actionable 类（含动词「生成 / 查 / 做 / 发 / 计算」等）→ 给 suggested_command
#   让 MinutesView 把短语预填到 CommandBar，一键触发对应指令。
# - 指令完成后回写到 todos[id]：status="done" + done_at + artifact_id，
#   再由前端展示「🔗 已生成 → 下载/预览」。
# - info 类（"下周再讨论"）只展示文字，不给执行按钮。
TodoKind = Literal["actionable", "info"]
TodoStatus = Literal["pending", "done", "cancelled"]


class TodoItem(BaseModel):
    id: str  # 服务端生成 uuid（pipeline 生成时填）
    text: str  # 待办描述（含触发短语，如"生成 Q3 销售拆解 PPT"）
    assignee: str | None = None  # 说话人标签（"说话人 1"）或人名
    kind: TodoKind = "info"
    status: TodoStatus = "pending"
    done_at: datetime | None = None
    artifact_id: str | None = None  # done 时关联交付物
    suggested_command: str | None = None  # actionable 时给 CommandBar 预填


class MeetingMinutes(BaseModel):
    meeting_id: str
    title: str  # ← 语义化标题（LLM 生成，≤ 18 字，中文）
    duration_sec: int
    # 兼容字段：旧 fixture / 已落库 minutes_json 仍有 speakers；保留但 UI 不再展示
    speakers: list[str] = Field(default_factory=list)
    summary: str
    sections: list[MinutesSection] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    # ── M_minutes_refactor：新的 todos 字段 ─────────────────────────
    # action_items 保留作向后兼容；UI 优先渲染 todos，前端不再展示 action_items
    todos: list[TodoItem] = Field(default_factory=list)
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


class MeetingSummary(BaseModel):
    """会议列表条目（左侧面板用）。

    与 ``MeetingRecord`` 的区别：聚合了 segments / speakers 计数，前端无需再
    join。``state`` 沿用 repository 的三态（in_meeting / ended / finalized），
    前端会把 finalized 视作 ended。

    M_minutes_refactor 引入 ``display_title``：LLM 生成的语义化标题
    （如「直播带货话术 + AI 编程营销讨论」），替代左侧列表里显示的 ``meeting_id``
    （如 ``m-bdd1da4e7e21``）。优先级：display_title > title > meeting_id。
    """

    meeting_id: str
    title: str | None = None
    display_title: str | None = None  # ← 新增：LLM 生成的语义化标题
    state: Literal["in_meeting", "ended", "finalized"]
    started_at: datetime
    ended_at: datetime | None = None
    finalized_at: datetime | None = None
    n_segments: int = 0
    n_speakers: int = 0
    has_minutes: bool = False
