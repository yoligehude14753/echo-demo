"""EchoTaskStreamBridge：订阅 AgentOS 任务事件并翻译为 EchoTaskEvent。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Protocol

import websockets

from app.agents.claude_code_adapter import ClaudeCodeRunnerAdapter, RunnerEventContext
from app.agents.events import TERMINAL_EVENTS, TERMINAL_STATES, EchoTaskEvent

_log = logging.getLogger("echodesk.agents.bridge")


class TaskEventRecorder(Protocol):
    async def __call__(
        self,
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
    ) -> EchoTaskEvent | None: ...


def raw_event_hash(raw: dict[str, Any]) -> str:
    body = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def agentos_ws_url(base_url: str, runner_task_id: str) -> str:
    root = base_url.rstrip("/")
    if root.startswith("https://"):
        root = "wss://" + root[len("https://") :]
    elif root.startswith("http://"):
        root = "ws://" + root[len("http://") :]
    return f"{root}/ws/tasks/{runner_task_id}"


def parse_bridge_message(message: Any) -> dict[str, Any] | None:
    try:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        raw = json.loads(str(message))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


class EchoTaskStreamBridge:
    """连接 AgentOS `/ws/tasks/{id}`，持久化并广播 EchoDesk 任务事件。"""

    def __init__(
        self,
        *,
        task_id: str,
        runner_task_id: str,
        agentos_base_url: str,
        recorder: TaskEventRecorder,
        conversation_id: str | None = None,
        message_id: str | None = None,
        title: str | None = None,
        adapter: ClaudeCodeRunnerAdapter | None = None,
    ) -> None:
        self.task_id = task_id
        self.runner_task_id = runner_task_id
        self.agentos_base_url = agentos_base_url.rstrip("/")
        self.recorder = recorder
        self.context = RunnerEventContext(
            task_id=task_id,
            runner_task_id=runner_task_id,
            conversation_id=conversation_id,
            message_id=message_id,
            title=title,
            agentos_base_url=self.agentos_base_url,
        )
        self.adapter = adapter or ClaudeCodeRunnerAdapter()

    @property
    def ws_url(self) -> str:
        return agentos_ws_url(self.agentos_base_url, self.runner_task_id)

    async def run(self) -> None:
        backoff = 1.0
        terminal = False
        while not terminal:
            result_terminal_seen = False
            try:
                async with websockets.connect(
                    self.ws_url,
                    open_timeout=10,
                    ping_interval=20,
                ) as ws:
                    backoff = 1.0
                    async for message in ws:
                        raw = parse_bridge_message(message)
                        if raw is None:
                            continue
                        digest = raw_event_hash(raw)
                        event = self.adapter.translate(raw, context=self.context, raw_ref=digest)
                        if event is None:
                            continue
                        stored = await self.recorder(event, raw_hash=digest)
                        if stored and (
                            stored.event in TERMINAL_EVENTS or stored.state in TERMINAL_STATES
                        ):
                            # AgentOS emits `result` before its final workspace scan, then sends
                            # `artifact_change` and `task_state`. Keep consuming this connection
                            # so freshly generated files are not lost from EchoDesk's archive.
                            if raw.get("kind") == "result":
                                result_terminal_seen = True
                                continue
                            terminal = True
                            return
                    if result_terminal_seen:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if terminal:
                    return
                _log.warning("agent bridge disconnected task=%s: %s", self.task_id, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
