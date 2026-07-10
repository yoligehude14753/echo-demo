"""Workflow 0.3 DTOs.

后端内部以这些 DTO 作为 REST 与 WebSocket 的公共契约；数据库层保持普通
dataclass/row 映射，避免把 FastAPI schema 泄漏到 repo 实现里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

WorkflowState = Literal[
    "pending",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "timeout",
    "cancelled",
    "cancel_failed",
]

WorkflowVisibility = Literal["user", "debug", "hidden"]

TERMINAL_WORKFLOW_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "timeout", "cancelled", "cancel_failed"}
)


class WorkflowRunDTO(BaseModel):
    run_id: str
    kind: str
    source: str
    state: WorkflowState
    title: str | None = None
    intent_text: str
    meeting_id: str | None = None
    todo_id: str | None = None
    agent_task_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    timeout_s: float | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str


class WorkflowEventDTO(BaseModel):
    run_id: str
    seq: int
    event_type: str
    state: WorkflowState
    visibility: WorkflowVisibility = "user"
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class WorkflowRunCreate(BaseModel):
    kind: str
    source: str = "manual"
    title: str | None = None
    intent_text: str
    meeting_id: str | None = None
    todo_id: str | None = None
    agent_task_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None


class WorkflowRetryRequest(BaseModel):
    reason: str | None = None


class WorkflowCancelRequest(BaseModel):
    reason: str | None = None


class WorkflowEventsResponse(BaseModel):
    run_id: str
    events: list[WorkflowEventDTO]
    snapshot: WorkflowRunDTO
