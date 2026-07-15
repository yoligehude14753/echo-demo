"""EmbeddedTaskStreamBridge 集成测试：typed runtime events → EchoTaskEvent。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.agents.events import EchoTaskEvent
from app.agents.stream_bridge import EmbeddedTaskStreamBridge


class _EmbeddedRuntime:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def events(self, _task_id: str, *, after_seq: int = 0):
        del after_seq
        for event in self._events:
            yield event


@pytest.mark.integration
async def test_bridge_translates_agentos_ws_events_and_stops_on_terminal() -> None:
    raw_events: list[dict[str, Any]] = [
        {
            "kind": "task_state",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:00+00:00",
            "payload": {"status": "running"},
        },
        {
            "kind": "assistant_text",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:01+00:00",
            "payload": {"text": "第一段", "stream": True},
        },
        {
            "kind": "assistant_text",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:01+00:00",
            "payload": {"text": "第一段", "stream": True},
        },
        {
            "kind": "artifact_change",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:03+00:00",
            "payload": {"artifacts": [{"name": "report.pdf", "relpath": "out/report.pdf"}]},
        },
        {
            "kind": "result",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:02+00:00",
            "payload": {"is_error": False, "result_text": "完成", "duration_ms": 3000},
        },
        {
            "kind": "task_state",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:04+00:00",
            "payload": {"status": "succeeded", "duration_ms": 3000},
        },
    ]

    recorded: list[EchoTaskEvent] = []
    seen_hashes: set[str] = set()

    async def recorder(
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
        raw_kind: str | None = None,
    ) -> EchoTaskEvent | None:
        del raw_kind
        if raw_hash in seen_hashes:
            return None
        if raw_hash:
            seen_hashes.add(raw_hash)
        stored = event.model_copy(update={"seq": len(recorded) + 1})
        recorded.append(stored)
        return stored

    bridge = EmbeddedTaskStreamBridge(
        task_id="echo_task_1",
        runner_task_id="runner_1",
        runtime=_EmbeddedRuntime(raw_events),
        recorder=recorder,
        title="测试任务",
    )
    completed = await asyncio.wait_for(bridge.run(), timeout=3.0)

    assert [event.event for event in recorded] == [
        "task.started",
        "task.text_delta",
        "task.artifact_updated",
        "task.completed",
    ]
    assert completed is True
    assert recorded[-1].state == "succeeded"
    assert recorded[-1].message == "完成"
    assert (
        recorded[2]
        .artifacts[0]["url"]
        .endswith("/agents/tasks/echo_task_1/artifacts/out/report.pdf")
    )
    assert "runner_1" not in recorded[2].artifacts[0]["url"]


@pytest.mark.integration
async def test_embedded_runtime_preserves_artifact_and_terminal_order() -> None:
    tail = [
        {
            "kind": "artifact_change",
            "task_id": "runner_tail",
            "ts": "2026-07-08T00:00:03+00:00",
            "payload": {"artifacts": [{"name": "tail.pdf", "relpath": "out/tail.pdf"}]},
        },
        {
            "kind": "result",
            "task_id": "runner_tail",
            "ts": "2026-07-08T00:00:02+00:00",
            "payload": {"is_error": False, "result_text": "完成"},
        },
    ]
    recorded_kinds: list[str] = []

    async def recorder(
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
        raw_kind: str | None = None,
    ) -> EchoTaskEvent | None:
        del raw_hash
        recorded_kinds.append(raw_kind or "")
        return event

    bridge = EmbeddedTaskStreamBridge(
        task_id="echo_tail",
        runner_task_id="runner_tail",
        runtime=_EmbeddedRuntime(tail),
        recorder=recorder,
        title="尾流恢复",
    )
    completed = await asyncio.wait_for(bridge.run(), timeout=3.0)

    assert completed is True
    assert recorded_kinds == ["artifact_change", "result"]
