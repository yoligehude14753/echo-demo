"""AgentOS / Claude Code event → EchoTaskEvent 翻译层。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from app.agents.events import EchoTaskEvent, utc_now_iso


@dataclass(slots=True)
class RunnerEventContext:
    task_id: str
    runner_task_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    title: str | None = None
    agentos_base_url: str | None = None


_TOOL_LABELS = {
    "Read": "正在读取文件",
    "Write": "正在生成文件",
    "Edit": "正在修改文件",
    "MultiEdit": "正在修改文件",
    "Bash": "正在运行命令",
    "WebFetch": "正在检查网页",
    "WebSearch": "正在检查网页",
}

_ARTIFACT_KIND_SUFFIXES = (
    ((".pptx",), "pptx"),
    ((".docx",), "word"),
    ((".xlsx", ".csv"), "xlsx"),
    ((".png", ".jpg", ".jpeg", ".gif", ".webp"), "image"),
    ((".pdf",), "pdf"),
    ((".py", ".js", ".ts", ".tsx", ".sh", ".go", ".rs"), "code"),
)

_STATUS_EVENTS = {
    "pending": ("task.queued", "pending", "任务已提交，等待执行"),
    "running": ("task.started", "running", "任务开始执行"),
    "succeeded": ("task.completed", "succeeded", "任务完成"),
    "failed": ("task.failed", "failed", "任务失败"),
    "cancelled": ("task.cancelled", "cancelled", "任务已取消"),
    "timeout": ("task.timeout", "timeout", "任务超时"),
}


def _event_ts(raw: dict[str, Any]) -> str:
    return str(raw.get("ts") or utc_now_iso())


def _artifact_kind(name: str) -> str:
    lower = name.lower()
    for suffixes, kind in _ARTIFACT_KIND_SUFFIXES:
        if lower.endswith(suffixes):
            return kind
    return "other"


def _echo_artifact_url(task_id: str, relpath: str) -> str:
    path = PurePosixPath(relpath)
    if path.is_absolute():
        return ""
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    encoded = "/".join(quote(part, safe="") for part in parts)
    return f"/agents/tasks/{quote(task_id, safe='')}/artifacts/{encoded}"


def _status_event(status: str) -> tuple[str, str, str]:
    value = (status or "").lower()
    return _STATUS_EVENTS.get(value, ("task.runner_status", value or "running", "任务状态更新"))


def _is_timeout_message(message: str) -> bool:
    value = " ".join((message or "").strip().lower().split())
    return value.startswith(("timeout", "timed out", "deadline exceeded")) or " timed out" in value


class ClaudeCodeRunnerAdapter:
    """把 AgentOS EventEnvelope 翻译成 EchoDesk 自己的任务事件。"""

    def __init__(self) -> None:
        self._tool_labels_by_id: dict[str, str] = {}

    def translate(
        self,
        raw: dict[str, Any],
        *,
        context: RunnerEventContext,
        raw_ref: str | None = None,
    ) -> EchoTaskEvent | None:
        kind = str(raw.get("kind") or "")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        base = {
            "task_id": context.task_id,
            "runner_task_id": context.runner_task_id or raw.get("task_id"),
            "conversation_id": context.conversation_id,
            "message_id": context.message_id,
            "title": context.title,
            "raw_ref": raw_ref,
            "ts": _event_ts(raw),
        }

        handler = {
            "system": self._translate_system,
            "assistant_text": self._translate_assistant_text,
            "tool_use": self._translate_tool_use,
            "tool_result": self._translate_tool_result,
            "artifact_change": self._translate_artifact_change,
            "result": self._translate_result,
            "task_state": self._translate_task_state,
        }.get(kind)
        if handler is not None:
            return handler(payload, base, raw_ref, context)
        return self._debug_event(base, f"unknown event: {kind}")

    def _translate_system(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        _raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        subtype = str(inner.get("subtype") or inner.get("type") or "")
        if subtype == "init":
            return EchoTaskEvent(
                **base,
                event="task.started",
                state="running",
                visibility="user",
                message="任务开始执行",
            )
        return self._debug_event(base, subtype or "runner status")

    def _translate_assistant_text(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        _raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent | None:
        text = str(payload.get("text") or "")
        if not text:
            return None
        if bool(payload.get("stream")):
            return EchoTaskEvent(
                **base,
                event="task.text_delta",
                state="running",
                visibility="user",
                text_delta=text,
            )
        return EchoTaskEvent(
            **base,
            event="task.message",
            state="running",
            visibility="user",
            message=text,
        )

    def _translate_tool_use(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent:
        tool_use_id = str(payload.get("tool_use_id") or "")
        tool_name = str(payload.get("name") or "")
        label = _TOOL_LABELS.get(tool_name, "正在执行步骤")
        if tool_use_id:
            self._tool_labels_by_id[tool_use_id] = label
        return EchoTaskEvent(
            **base,
            event="task.step_started",
            state="running",
            visibility="user",
            step={"id": tool_use_id or raw_ref or "step", "label": label, "status": "running"},
        )

    def _translate_tool_result(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent:
        tool_use_id = str(payload.get("tool_use_id") or "")
        is_error = bool(payload.get("is_error"))
        label = self._tool_labels_by_id.get(tool_use_id, "步骤已完成")
        return EchoTaskEvent(
            **base,
            event="task.step_finished",
            state="running",
            visibility="user",
            step={
                "id": tool_use_id or raw_ref or "step",
                "label": "步骤执行失败" if is_error else label,
                "status": "failed" if is_error else "succeeded",
            },
        )

    def _translate_artifact_change(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        _raw_ref: str | None,
        context: RunnerEventContext,
    ) -> EchoTaskEvent:
        artifacts = self._translate_artifacts(payload.get("artifacts") or [], context)
        return EchoTaskEvent(
            **base,
            event="task.artifact_updated",
            state="running",
            visibility="user",
            message="产物已更新",
            artifacts=artifacts,
        )

    def _translate_result(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        _raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent:
        is_error = bool(payload.get("is_error"))
        result_text = str(payload.get("result_text") or "")
        is_timeout = is_error and _is_timeout_message(result_text)
        return EchoTaskEvent(
            **base,
            event="task.timeout" if is_timeout else ("task.failed" if is_error else "task.completed"),
            state="timeout" if is_timeout else ("failed" if is_error else "succeeded"),
            visibility="user",
            message=result_text or ("任务超时" if is_timeout else ("任务失败" if is_error else "任务完成")),
            snapshot={
                "duration_ms": payload.get("duration_ms"),
                "num_turns": payload.get("num_turns"),
            },
        )

    def _translate_task_state(
        self,
        payload: dict[str, Any],
        base: dict[str, Any],
        _raw_ref: str | None,
        _context: RunnerEventContext,
    ) -> EchoTaskEvent:
        event, state, message = _status_event(str(payload.get("status") or "running"))
        error = str(payload.get("error") or "")
        if event == "task.failed" and _is_timeout_message(error):
            event, state, message = _STATUS_EVENTS["timeout"]
        elif event == "task.failed" and error:
            message = error
        return EchoTaskEvent(
            **base,
            event=event,
            state=state,
            visibility="user" if event != "task.runner_status" else "debug",
            message=message,
            snapshot={
                "duration_ms": payload.get("duration_ms"),
                "num_turns": payload.get("num_turns"),
                "tool_use_count": payload.get("tool_use_count"),
            },
        )

    def _debug_event(self, base: dict[str, Any], message: str) -> EchoTaskEvent:
        return EchoTaskEvent(
            **base,
            event="task.runner_status",
            state="running",
            visibility="debug",
            message=message,
        )

    def _translate_artifacts(
        self,
        artifacts: list[Any],
        context: RunnerEventContext,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("relpath") or "artifact")
            relpath = str(item.get("relpath") or name)
            url = _echo_artifact_url(context.task_id, relpath)
            out.append(
                {
                    "name": name,
                    "url": url,
                    "kind": item.get("kind") or _artifact_kind(name),
                    "size_bytes": item.get("size_bytes"),
                    "relpath": relpath,
                    "has_preview": item.get("has_preview"),
                }
            )
        return out
