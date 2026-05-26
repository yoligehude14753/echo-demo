"""WS 端点 + meeting/artifact 事件透传集成单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.api.deps import get_event_bus, reset_deps_for_test
from app.api.meetings import get_meeting_pipeline, reset_meeting_pipeline
from app.config import Settings, get_settings
from app.main import create_app
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline
from fastapi.testclient import TestClient

from tests.unit.test_meeting_pipeline import FakeDiarizer, FakeLLM, FakeRag, FakeSTT


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reset_meeting_pipeline()
    reset_deps_for_test()
    bus = InMemoryEventBus()
    minutes_json = json.dumps(
        {
            "summary": "测试",
            "sections": [{"heading": "h", "bullets": ["b1", "b2"]}],
            "decisions": ["d1"],
            "action_items": ["a1"],
        },
        ensure_ascii=False,
    )
    pipe = MeetingPipeline(
        settings=Settings(storage_dir=tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="hi", start_ms=0, end_ms=500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM(minutes_json),
        event_bus=bus,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(storage_dir=tmp_path)
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.dependency_overrides[get_meeting_pipeline] = lambda: pipe
    return TestClient(app)


@pytest.mark.unit
def test_ws_receives_meeting_lifecycle_events(client: TestClient) -> None:
    received: list[dict] = []
    with client.websocket_connect("/ws/echo") as ws:
        # 触发会议流程
        client.post("/meetings/ws-1/start")
        client.post(
            "/meetings/ws-1/chunk",
            files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
        )
        client.post("/meetings/ws-1/finalize", data={"title": "demo"})

        # 期望 4 个事件：started + segment + ended + minutes.ready
        for _ in range(4):
            msg = ws.receive_text()
            received.append(json.loads(msg))

    types = [e["type"] for e in received]
    assert types == [
        "meeting.started",
        "meeting.segment",
        "meeting.ended",
        "minutes.ready",
    ]
    # seq 单调递增
    assert [e["seq"] for e in received] == [1, 2, 3, 4]
    assert received[1]["payload"]["text"] == "hi"
    assert received[3]["payload"]["decisions"] == ["d1"]


@pytest.mark.unit
def test_ws_pong_handles_ping(client: TestClient) -> None:
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        msg = ws.receive_text()
        assert json.loads(msg) == {"type": "pong"}
