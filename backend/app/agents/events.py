"""EchoTaskEvent schema 与快照投影。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Visibility = Literal["user", "debug", "hidden"]

TERMINAL_STATES = {"succeeded", "failed", "cancelled", "cancel_failed", "timeout"}
TERMINAL_EVENTS = {
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.cancel_failed",
    "task.timeout",
}
FAILURE_PROGRESS_TEXT = {
    "task.failed": "任务失败",
    "task.timeout": "任务超时",
    "task.cancelled": "任务已取消",
    "task.cancel_failed": "取消失败",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EchoTaskEvent(BaseModel):
    """EchoDesk UI/API 使用的稳定任务事件。"""

    type: Literal["echo_task_event"] = "echo_task_event"
    task_id: str
    runner_task_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    seq: int = 0
    event: str
    state: str
    visibility: Visibility = "user"
    title: str | None = None
    text_delta: str | None = None
    message: str | None = None
    step: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    permission: dict[str, Any] | None = None
    raw_ref: str | None = None
    ts: str = Field(default_factory=utc_now_iso)


def default_snapshot(*, title: str | None = None, status: str = "pending") -> dict[str, Any]:
    return {
        "status": status,
        "title": title or "EchoDesk 正在执行",
        "progress_text": None,
        "final_text": None,
        "text_buffer": "",
        "steps": [],
        "artifacts": [],
        "duration_ms": None,
        "error": None,
        "actions": [],
        "permission": None,
    }


def _start_step(snapshot: dict[str, Any], step: dict[str, Any]) -> None:
    steps = [s for s in snapshot.get("steps", []) if s.get("id") != step.get("id")]
    steps.append(step)
    snapshot["steps"] = steps
    snapshot["progress_text"] = step.get("label") or snapshot.get("progress_text")


def _finish_step(snapshot: dict[str, Any], step: dict[str, Any]) -> None:
    steps = []
    found = False
    for existing in snapshot.get("steps", []):
        if existing.get("id") == step.get("id"):
            merged = dict(existing)
            merged.update(step)
            steps.append(merged)
            found = True
        else:
            steps.append(existing)
    if not found:
        steps.append(step)
    snapshot["steps"] = steps
    snapshot["progress_text"] = step.get("label") or snapshot.get("progress_text")


def _complete_snapshot(snapshot: dict[str, Any], event: EchoTaskEvent) -> None:
    snapshot["status"] = "succeeded"
    snapshot["final_text"] = event.message or snapshot.get("final_text") or snapshot.get("text_buffer")
    snapshot["progress_text"] = "任务完成"
    snapshot["actions"] = []
    snapshot["permission"] = None
    if event.artifacts:
        snapshot["artifacts"] = event.artifacts


def _fail_snapshot(snapshot: dict[str, Any], event: EchoTaskEvent) -> None:
    snapshot["progress_text"] = event.message or FAILURE_PROGRESS_TEXT[event.event]
    snapshot["error"] = event.message or snapshot.get("error")
    snapshot["actions"] = []


def reduce_snapshot(  # noqa: PLR0912
    previous: dict[str, Any] | None, event: EchoTaskEvent
) -> dict[str, Any]:
    """把事件折叠成最新 UI 快照，支持 seq replay 后快速校正。"""

    snapshot = dict(previous or default_snapshot(title=event.title, status=event.state))
    snapshot["status"] = event.state or snapshot.get("status") or "running"
    if event.title:
        snapshot["title"] = event.title

    if event.event == "task.permission_required":
        snapshot["progress_text"] = event.message or "等待授权"
        snapshot["actions"] = event.actions
        snapshot["permission"] = event.permission
    elif event.event == "task.queued":
        snapshot["progress_text"] = event.message or "任务已提交，等待执行"
        snapshot["actions"] = []
        snapshot["permission"] = None
    elif event.event == "task.started":
        snapshot["progress_text"] = event.message or "任务开始执行"
    elif event.event == "task.text_delta" and event.text_delta:
        snapshot["text_buffer"] = f"{snapshot.get('text_buffer') or ''}{event.text_delta}"
        snapshot["progress_text"] = "正在整理结果"
    elif event.event == "task.message" and event.message:
        snapshot["final_text"] = event.message
        snapshot["progress_text"] = event.message[:120]
    elif event.event == "task.step_started" and event.step:
        _start_step(snapshot, event.step)
    elif event.event == "task.step_finished" and event.step:
        _finish_step(snapshot, event.step)
    elif event.event == "task.artifact_updated":
        snapshot["artifacts"] = event.artifacts
        snapshot["progress_text"] = event.message or "产物已更新"
    elif event.event == "task.completed":
        _complete_snapshot(snapshot, event)
    elif event.event in FAILURE_PROGRESS_TEXT:
        _fail_snapshot(snapshot, event)

    if event.message and event.event in {"task.failed", "task.timeout"}:
        snapshot["error"] = event.message
    if event.snapshot:
        duration_ms = event.snapshot.get("duration_ms")
        if duration_ms is not None:
            snapshot["duration_ms"] = duration_ms
    return snapshot
