"""Embedded Electron runtime event bridge for AgentTaskService."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from app.agents.claude_code_adapter import ClaudeCodeRunnerAdapter, RunnerEventContext
from app.agents.events import TERMINAL_EVENTS, TERMINAL_STATES, EchoTaskEvent


class TaskEventRecorder(Protocol):
    async def __call__(
        self,
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
        raw_kind: str | None = None,
    ) -> EchoTaskEvent | None: ...


def raw_event_hash(raw: dict[str, Any]) -> str:
    """Return raw runtime identity; durable seq remains an Echo concern."""

    body = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class EmbeddedRuntimeEvents(Protocol):
    def events(self, task_id: str, *, after_seq: int = 0) -> Any: ...


class EmbeddedTaskStreamBridge:
    """Consume typed events from the inherited Electron runtime port."""

    def __init__(
        self,
        *,
        task_id: str,
        runner_task_id: str,
        runtime: EmbeddedRuntimeEvents,
        recorder: TaskEventRecorder,
        conversation_id: str | None = None,
        message_id: str | None = None,
        title: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.runner_task_id = runner_task_id
        self.runtime = runtime
        self.recorder = recorder
        self.context = RunnerEventContext(
            task_id=task_id,
            runner_task_id=runner_task_id,
            conversation_id=conversation_id,
            message_id=message_id,
            title=title,
            agentos_base_url=None,
        )
        self.adapter = ClaudeCodeRunnerAdapter()

    async def run(self) -> bool:
        async for raw in self.runtime.events(self.task_id, after_seq=0):
            digest = raw_event_hash(raw)
            event = self.adapter.translate(raw, context=self.context, raw_ref=digest)
            if event is None:
                continue
            await self.recorder(
                event,
                raw_hash=digest,
                raw_kind=str(raw.get("kind") or ""),
            )
            if event.event in TERMINAL_EVENTS or event.state in TERMINAL_STATES:
                return True
        return False
