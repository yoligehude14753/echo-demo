"""会议 HTTP API 单测：覆盖 start/chunk/finalize 三段闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(storage_dir=tmp_path)
    app.dependency_overrides[get_meeting_pipeline] = lambda: pipe
    return TestClient(app)


@pytest.mark.unit
def test_meeting_full_flow(client: TestClient) -> None:
    r = client.post("/meetings/mtg-1/start")
    assert r.status_code == 204

    r = client.post(
        "/meetings/mtg-1/chunk",
        files={"audio": ("c.wav", b"\x00" * 16_000, "audio/wav")},
        data={"sample_rate": "16000"},
    )
    assert r.status_code == 200
    segs = r.json()
    assert segs[0]["text"] == "hi"
    assert segs[0]["speaker_label"] == "说话人1"

    r = client.get("/meetings/mtg-1/segments")
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.post("/meetings/mtg-1/finalize", data={"title": "test meeting"})
    assert r.status_code == 200, r.text
    minutes = r.json()
    assert minutes["title"] == "test meeting"
    assert minutes["decisions"] == ["d1"]
    assert minutes["speakers"] == ["说话人1"]


@pytest.mark.unit
def test_chunk_empty_audio_400(client: TestClient) -> None:
    r = client.post(
        "/meetings/mtg-x/chunk",
        files={"audio": ("c.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400
