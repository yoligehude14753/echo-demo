"""EchoTaskStreamBridge 集成测试：Mock AgentOS WS → EchoTaskEvent。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
from app.agents.events import EchoTaskEvent
from app.agents.stream_bridge import EchoTaskStreamBridge


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
            "kind": "result",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:02+00:00",
            "payload": {"is_error": False, "result_text": "完成", "duration_ms": 3000},
        },
        {
            "kind": "artifact_change",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:03+00:00",
            "payload": {"artifacts": [{"name": "report.pdf", "relpath": "out/report.pdf"}]},
        },
        {
            "kind": "task_state",
            "task_id": "runner_1",
            "ts": "2026-07-08T00:00:04+00:00",
            "payload": {"status": "succeeded", "duration_ms": 3000},
        },
    ]

    async def handler(websocket: Any, *_args: Any) -> None:
        for raw in raw_events:
            await websocket.send(json.dumps(raw))
        await websocket.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    recorded: list[EchoTaskEvent] = []
    seen_hashes: set[str] = set()

    async def recorder(
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
    ) -> EchoTaskEvent | None:
        if raw_hash in seen_hashes:
            return None
        if raw_hash:
            seen_hashes.add(raw_hash)
        stored = event.model_copy(update={"seq": len(recorded) + 1})
        recorded.append(stored)
        return stored

    try:
        bridge = EchoTaskStreamBridge(
            task_id="echo_task_1",
            runner_task_id="runner_1",
            agentos_base_url=f"http://127.0.0.1:{port}",
            recorder=recorder,
            title="测试任务",
        )
        await asyncio.wait_for(bridge.run(), timeout=3.0)
    finally:
        server.close()
        await server.wait_closed()

    assert [event.event for event in recorded] == [
        "task.started",
        "task.text_delta",
        "task.completed",
        "task.artifact_updated",
        "task.completed",
    ]
    assert recorded[-1].state == "succeeded"
    assert recorded[2].message == "完成"
    assert recorded[-1].message == "任务完成"
    assert recorded[3].artifacts[0]["url"].endswith(
        "/agents/tasks/echo_task_1/artifacts/out/report.pdf"
    )
    assert "runner_1" not in recorded[3].artifacts[0]["url"]
