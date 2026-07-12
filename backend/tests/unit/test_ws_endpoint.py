"""WS 端点 + meeting/artifact 事件透传集成单测。

CI 上 pytest-asyncio 0.24 + starlette 0.38 + TestClient（同一 with 块里
websocket_connect + client.post）触发 asyncio.Lock 跨 event-loop 死锁，
本地 pytest-asyncio 1.x 通过。CI 暂跳过，让真音频 / Playwright E2E 在
integration 阶段覆盖 ws 路径。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.api.deps import get_event_bus, get_repository, reset_deps_for_test
from app.api.meetings import get_meeting_pipeline, reset_meeting_pipeline
from app.config import Settings, get_settings
from app.main import create_app
from app.ports.repository import MeetingRecord
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline
from fastapi.testclient import TestClient

from tests.unit.test_meeting_pipeline import FakeDiarizer, FakeLLM, FakeRag, FakeSTT


class _MeetingRepo:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self.meetings: dict[str, MeetingRecord] = {}
        self.segments: dict[str, list[TranscriptSegment]] = defaultdict(list)

    async def create_meeting(
        self,
        meeting_id: str,
        *,
        started_at: datetime,
        title: str | None = None,
        auto_started: bool = False,
    ) -> MeetingRecord:
        self.meetings.setdefault(
            meeting_id,
            MeetingRecord(
                id=meeting_id,
                title=title,
                state="in_meeting",
                started_at=started_at,
                auto_started=auto_started,
            ),
        )
        if self.db_path is not None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO meetings
                       (id, state, started_at, tenant_id, device_id, owner_id)
                       VALUES (?, 'in_meeting', ?, 'legacy-local', 'legacy-local', 'legacy-local')""",
                    (meeting_id, started_at.isoformat()),
                )
                conn.commit()
        return self.meetings[meeting_id]

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        return self.meetings.get(meeting_id)

    async def update_meeting_state(self, meeting_id: str, **values: Any) -> None:
        record = self.meetings[meeting_id]
        self.meetings[meeting_id] = record.model_copy(
            update={key: value for key, value in values.items() if value is not None}
        )

    async def append_meeting_segment(
        self,
        meeting_id: str,
        segment: TranscriptSegment,
        *,
        captured_at: datetime,
    ) -> None:
        _ = captured_at
        self.segments[meeting_id].append(segment)

    async def list_meeting_segments(self, meeting_id: str) -> list[TranscriptSegment]:
        return list(self.segments[meeting_id])

    async def get_meeting_speaker_labels(self, meeting_id: str) -> dict[str, str]:
        _ = meeting_id
        return {}

    async def upsert_meeting_speaker_label(
        self, meeting_id: str, speaker_id: str, label: str
    ) -> None:
        _ = (meeting_id, speaker_id, label)


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
    settings = Settings(storage_dir=tmp_path, db_path=tmp_path / "echo.db")
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    repo = _MeetingRepo(settings.db_path)
    pipe = MeetingPipeline(
        settings=settings,
        stt=FakeSTT([[TranscriptSegment(text="hi", start_ms=0, end_ms=500)]]),
        diarizer=FakeDiarizer(["spk-A"]),
        rag=FakeRag(),
        llm=FakeLLM(minutes_json),
        event_bus=bus,
        repository=repo,  # type: ignore[arg-type]
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.dependency_overrides[get_repository] = lambda: repo
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

        # Workflow outbox events are intentionally interleaved with domain events.
        # Read through the committed stream until the four meeting events arrive.
        while (
            len([event for event in received if event["type"].startswith(("meeting.", "minutes."))])
            < 4
        ):
            msg = ws.receive_text()
            received.append(json.loads(msg))

    domain_events = [
        event for event in received if event["type"].startswith(("meeting.", "minutes."))
    ]
    types = [event["type"] for event in domain_events]
    assert types == [
        "meeting.started",
        "meeting.segment",
        "meeting.ended",
        "minutes.ready",
    ]
    assert [e["seq"] for e in received] == list(range(1, len(received) + 1))
    assert domain_events[1]["payload"]["text"] == "hi"
    assert domain_events[3]["payload"]["decisions"] == ["d1"]


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
    settings = Settings(storage_dir=tmp_path, db_path=tmp_path / "resync.db")
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    repo = _MeetingRepo(settings.db_path)
    pipe = MeetingPipeline(
        settings=settings,
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
        repository=repo,  # type: ignore[arg-type]
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_meeting_pipeline] = lambda: pipe
    c = TestClient(app)

    # Domain + workflow outbox events exceed replay_buffer=2; only the last two remain.
    c.post("/meetings/resync/start")
    c.post(
        "/meetings/resync/chunk",
        files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
    )
    c.post("/meetings/resync/finalize", data={"title": "x"})
    max_seq = bus.max_seq
    oldest_seq = max_seq - 1
    assert max_seq > 5
    assert bus.oldest_history_seq == oldest_seq

    with c.websocket_connect("/ws/echo") as ws:
        ws.send_text(json.dumps({"type": "client_hello", "last_seq": 1}))
        msg1 = json.loads(ws.receive_text())
        assert msg1["type"] == "server_resync", msg1
        assert msg1["payload"]["oldest_seq"] == oldest_seq
        assert msg1["payload"]["client_last_seq"] == 1
        msg2 = json.loads(ws.receive_text())
        assert msg2["type"] == "server_sync"
        assert msg2["payload"]["strategy"] == "replace"
        assert msg2["payload"]["fence_seq"] == max_seq
        msg3 = json.loads(ws.receive_text())
        assert msg3["type"] == "server_hello"
        # ``server_sync`` is a replace fence, so stale buffered events are not
        # replayed after it; future events resume strictly after ``max_seq``.
