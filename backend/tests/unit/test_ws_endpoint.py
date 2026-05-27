"""WS 端点 + meeting/artifact 事件透传集成单测。

CI 上 pytest-asyncio 0.24 + starlette 0.38 + TestClient（同一 with 块里
websocket_connect + client.post）触发 asyncio.Lock 跨 event-loop 死锁，
本地 pytest-asyncio 1.x 通过。CI 暂跳过，让真音频 / Playwright E2E 在
integration 阶段覆盖 ws 路径。
"""

from __future__ import annotations

import json
import os
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

# 全文件 CI 跳过（见 module docstring）
pytestmark = pytest.mark.skipif(
    "CI" in os.environ,
    reason="CI 上 starlette 0.38 + pytest-asyncio 0.24 websocket+POST 死锁，本地通过",
)


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
        # PR-14 协议：先发 client_hello，再收 server_hello
        ws.send_text(json.dumps({"type": "client_hello", "last_seq": 0}))
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "server_hello"

        # 触发会议流程
        client.post("/meetings/ws-1/start")
        client.post(
            "/meetings/ws-1/chunk",
            files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
        )
        client.post("/meetings/ws-1/finalize", data={"title": "demo"})

        # 期望 4 个业务事件
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
    assert [e["seq"] for e in received] == [1, 2, 3, 4]
    assert received[1]["payload"]["text"] == "hi"
    assert received[3]["payload"]["decisions"] == ["d1"]


@pytest.mark.unit
def test_ws_legacy_plain_ping_still_works(client: TestClient) -> None:
    """老客户端发文本 'ping'，服务端回 server_ping JSON。"""
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        # 第一条消息可能是 server_hello（hello timeout 后），也可能是 server_ping
        msgs: list[dict] = []
        for _ in range(2):
            msgs.append(json.loads(ws.receive_text()))
        types = {m["type"] for m in msgs}
        assert "server_hello" in types
        assert "server_ping" in types


@pytest.mark.unit
def test_ws_client_hello_handshake(client: TestClient) -> None:
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text(
            json.dumps({"type": "client_hello", "last_seq": 0, "client_version": "test-1.0"})
        )
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "server_hello"
        assert hello["payload"]["version"] == "1.0"
        assert hello["payload"]["max_seq"] == 0
        assert hello["payload"]["client_version"] == "test-1.0"


@pytest.mark.unit
def test_ws_resume_from_last_seq(client: TestClient) -> None:
    """先收前 2 个事件 → 断开 → 重连 last_seq=2 → 只收 seq>2 的事件。"""
    # 第 1 次连接，触发 started+segment+ended+minutes（4 个事件）
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text(json.dumps({"type": "client_hello", "last_seq": 0}))
        assert json.loads(ws.receive_text())["type"] == "server_hello"
        client.post("/meetings/ws-resume/start")
        client.post(
            "/meetings/ws-resume/chunk",
            files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
        )
        client.post("/meetings/ws-resume/finalize", data={"title": "x"})
        # 收前 2 个，假装客户端只到 seq=2
        seen = [json.loads(ws.receive_text()) for _ in range(2)]
        assert [e["seq"] for e in seen] == [1, 2]

    # 第 2 次连接，last_seq=2 → 应只 replay seq=3, 4
    with client.websocket_connect("/ws/echo") as ws2:
        ws2.send_text(json.dumps({"type": "client_hello", "last_seq": 2}))
        assert json.loads(ws2.receive_text())["type"] == "server_hello"
        replay = [json.loads(ws2.receive_text()) for _ in range(2)]
        seqs = [e["seq"] for e in replay]
        assert seqs == [3, 4], f"resume 应仅 replay seq>2，实际 {seqs}"


@pytest.mark.unit
def test_ws_server_resync_when_history_expired(tmp_path: Path) -> None:
    """history 被淘汰后客户端用旧 last_seq 重连 → 服务先发 server_resync。"""
    reset_meeting_pipeline()
    reset_deps_for_test()
    # 极小 replay_buffer，便于触发淘汰
    bus = InMemoryEventBus(replay_buffer=2)
    pipe = MeetingPipeline(
        settings=Settings(storage_dir=tmp_path),
        stt=FakeSTT([[TranscriptSegment(text="x", start_ms=0, end_ms=100)]]),
        diarizer=FakeDiarizer(["spk"]),
        rag=FakeRag(),
        llm=FakeLLM(
            json.dumps(
                {
                    "summary": "x",
                    "sections": [{"heading": "h", "bullets": ["a", "b"]}],
                    "decisions": [],
                    "action_items": [],
                }
            )
        ),
        event_bus=bus,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(storage_dir=tmp_path)
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.dependency_overrides[get_meeting_pipeline] = lambda: pipe
    c = TestClient(app)

    # 生成 5 个事件（meeting.started/segment/ended/minutes.ready/tts.suggested），
    # replay_buffer=2 → 只保留 seq=4,5
    c.post("/meetings/resync/start")
    c.post(
        "/meetings/resync/chunk",
        files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
    )
    c.post("/meetings/resync/finalize", data={"title": "x"})
    assert bus.max_seq == 5
    assert bus.oldest_history_seq == 4

    with c.websocket_connect("/ws/echo") as ws:
        ws.send_text(json.dumps({"type": "client_hello", "last_seq": 1}))
        msg1 = json.loads(ws.receive_text())
        assert msg1["type"] == "server_resync", msg1
        assert msg1["payload"]["oldest_seq"] == 4
        assert msg1["payload"]["client_last_seq"] == 1
        msg2 = json.loads(ws.receive_text())
        assert msg2["type"] == "server_hello"
        # 然后是 history 内剩余的 seq=4, 5
        m4 = json.loads(ws.receive_text())
        m5 = json.loads(ws.receive_text())
        assert m4["seq"] == 4 and m5["seq"] == 5
