"""Agent task 基础模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AgentTaskState(StrEnum):
    WAITING_PERMISSION = "waiting_permission"
    PENDING = "pending"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCEL_FAILED = "cancel_failed"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AgentTaskState.SUCCEEDED,
            AgentTaskState.FAILED,
            AgentTaskState.CANCELLED,
            AgentTaskState.CANCEL_FAILED,
            AgentTaskState.TIMEOUT,
        }


def new_echo_task_id() -> str:
    return f"echo_task_{uuid4().hex}"


@dataclass(slots=True)
class AgentIntent:
    text: str
    device_id: str
    echo_task_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    title: str | None = None
    task_kind: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    permission_profile: str | None = None
    grant_id: str | None = None
    timeout_s: float = 1800.0
    priority: int = 60
    runner_model: str | None = None
    runner_base_url: str | None = None
    runner_operation_key: str | None = None


@dataclass(slots=True)
class AgentSubmitResult:
    task_id: str
    accepted: bool
    provider: str
    error: str | None = None
    runner_task_id: str | None = None
    runner_base_url: str | None = None
