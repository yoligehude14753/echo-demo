"""端到端集成：HTTP + WS + 真 Yunwu LLM 全链路。

使用 FakeSTT/FakeDiarizer 替代 STT/Diarizer（远端 STT 不要求在 demo 环境可达），其它真实：
- LLM：真 Yunwu MiniMax-M2.7
- RAG：真 BM25 + jieba（内存索引）
- 事件总线：真 InMemoryEventBus
- WS：真 Starlette TestClient.websocket_connect

跳过：YUNWU_OPEN_KEY 未配置时跳过。
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.rag.bm25 import BM25Rag
from app.api.deps import get_event_bus, get_llm_singleton, reset_deps_for_test
from app.api.meetings import get_meeting_pipeline, reset_meeting_pipeline
from app.config import Settings, get_settings
from app.main import create_app
from app.schemas.meeting import TranscriptSegment
from app.use_cases.meeting_pipeline import MeetingPipeline
from fastapi.testclient import TestClient

from tests.unit.test_meeting_pipeline import FakeDiarizer, FakeSTT


def _yunwu_alive() -> bool:
    if not os.getenv("YUNWU_OPEN_KEY"):
        return False
    try:
        with socket.create_connection(("yunwu.ai", 443), timeout=3):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _yunwu_alive(), reason="Yunwu 不可达"),
]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reset_meeting_pipeline()
    reset_deps_for_test()
    s = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        skill_executor_build_dir=tmp_path / "skill",
        skill_executor_timeout_s=120,
        skill_executor_max_tokens=8_000,
    )
    bus = InMemoryEventBus()
    llm = OpenAICompatibleLLM(s)
    rag = BM25Rag(s)
    pipe = MeetingPipeline(
        settings=s,
        stt=FakeSTT(
            [
                [
                    TranscriptSegment(
                        text="今天讨论 Q3 预算，原方案 100 万。", start_ms=0, end_ms=2500
                    )
                ],
                [
                    TranscriptSegment(
                        text="我建议砍 30%。Q2 销售不及预期。", start_ms=0, end_ms=2800
                    )
                ],
                [
                    TranscriptSegment(
                        text="同意 70 万方案，Alice 周五出修订版。", start_ms=0, end_ms=2700
                    )
                ],
            ]
        ),
        diarizer=FakeDiarizer(["A", "B", "A"]),
        rag=rag,
        llm=llm,
        event_bus=bus,
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: s
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.dependency_overrides[get_llm_singleton] = lambda: llm
    app.dependency_overrides[get_meeting_pipeline] = lambda: pipe
    return TestClient(app)


@pytest.mark.integration
def test_full_meeting_flow_via_http_and_ws_with_real_llm(client: TestClient) -> None:
    received: list[dict] = []
    with client.websocket_connect("/ws/echo") as ws:
        r = client.post("/meetings/e2e-1/start")
        assert r.status_code == 200

        for _ in range(3):
            r = client.post(
                "/meetings/e2e-1/chunk",
                files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
            )
            assert r.status_code == 200

        r = client.post("/meetings/e2e-1/finalize", data={"title": "Q3 预算 e2e"})
        assert r.status_code == 200, r.text
        minutes = r.json()
        assert minutes["title"] == "Q3 预算 e2e"
        assert minutes["decisions"] or minutes["action_items"]
        assert minutes["summary"]

        # 6 个事件：started + 3 segments + ended + minutes.ready
        for _ in range(6):
            msg = ws.receive_text()
            received.append(json.loads(msg))

    types = [e["type"] for e in received]
    assert types == [
        "meeting.started",
        "meeting.segment",
        "meeting.segment",
        "meeting.segment",
        "meeting.ended",
        "minutes.ready",
    ]
    # RAG 回查（SSE 流）
    r = client.post("/rag/ask", json={"question": "Q3 预算讨论的结论是什么"})
    assert r.status_code == 200
    body = r.text
    assert "chosen_source" in body  # done event 透出来源与引用 trace
    assert "event: done" in body


@pytest.mark.integration
def test_artifact_generation_via_http_with_real_llm(client: TestClient) -> None:
    received: list[dict] = []
    with client.websocket_connect("/ws/echo") as ws:
        r = client.post(
            "/artifacts/generate",
            json={
                "artifact_type": "html",
                "brief": "生成一个简单的英伟达营收快照单文件 HTML，深色主题。",
            },
        )
        assert r.status_code == 200, r.text
        art = r.json()
        assert art["artifact_type"] == "html"
        assert art["size_bytes"] > 1500

        # 期望 generating + ready
        for _ in range(2):
            received.append(json.loads(ws.receive_text()))
        types = [e["type"] for e in received]
        assert types == ["artifact.generating", "artifact.ready"]

        # 验证下载
        r2 = client.get(f"/artifacts/{art['artifact_id']}/download")
        assert r2.status_code == 200
        assert len(r2.content) > 1500
