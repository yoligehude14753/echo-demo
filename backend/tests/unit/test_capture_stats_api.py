"""GET /capture/stats endpoint 单测（M_diag_brake）。

验证 endpoint 返回的字段集 + 类型 + 内容反映 in-memory counter。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.audio import pcm_to_wav
from app.api import capture as capture_api
from app.api.capture import reset_ambient_pipeline
from app.api.deps import get_repository
from app.config import Settings, get_settings
from app.main import create_app
from app.ports.repository import AmbientSegmentRecord
from app.schemas.capture import CaptureChunkResult
from app.use_cases.ambient_capture import AmbientPersistenceError
from fastapi.testclient import TestClient

_EXPECTED_FIELDS = {
    "chunks_total",
    "gated_rms",
    "gated_low_speech",
    "stt_circuit_open",
    "stt_failed",
    "stt_empty",
    "hallu_dropped",
    "repeat_dropped",
    "diarize_failed",
    # phase4-diar-deep：区分 diarizer 抛异常（failed） vs 正常返回 None
    "diarize_returned_none",
    "stored",
    "segment_store_failed",
    "audio_files_stored",
    "audio_bytes_stored",
    "audio_store_failed",
    "audio_quota_rejected",
    "audio_files_deleted",
    "audio_bytes_deleted",
    "audio_gc_failed",
    "audio_delete_failed",
    "audio_missing_reconciled",
    "last_chunk_at",
    "last_stored_at",
    "last_audio_stored_at",
    "last_rms",
    "last_speech_ratio",
    "last_gate_reason",
    "observed_audio_frames",
    "accepted_speech_frames",
    "accepted_speech_ratio",
    "stats_sequence",
}


def enable_local_capture(client: TestClient) -> None:
    current = client.get("/capture/control").json()
    response = client.put(
        "/capture/control",
        json={
            "mode": "single",
            "selectedDeviceIds": ["legacy-local"],
            "expectedRevision": current["revision"],
        },
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """构造 TestClient，注入 isolated Settings 让 /capture/stats 拿到新 pipeline。"""
    reset_ambient_pipeline()
    app = create_app()

    def _settings_override() -> Settings:
        return Settings(
            storage_dir=tmp_path / "storage",
            rag_index_dir=tmp_path / "rag",
            ambient_rms_gate=10_000,  # 让 SILENT_1KB 被 gated_rms 吃掉，测试方便
            ambient_min_speech_frame_ratio=0.0,
        )

    app.dependency_overrides[get_settings] = _settings_override
    with TestClient(app) as c:
        yield c
    reset_ambient_pipeline()


def test_get_stats_returns_expected_fields_on_fresh_pipeline(client: TestClient) -> None:
    """新 pipeline 所有 int counter 应为 0；timestamps 应为 None。"""
    r = client.get("/capture/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == _EXPECTED_FIELDS
    int_fields = _EXPECTED_FIELDS - {
        "last_chunk_at",
        "last_stored_at",
        "last_audio_stored_at",
        "last_gate_reason",
        "accepted_speech_ratio",
    }
    for f in int_fields:
        assert body[f] == 0, f"expect {f}=0 on fresh pipeline, got {body[f]}"
    assert body["last_chunk_at"] is None
    assert body["last_stored_at"] is None
    assert body["last_audio_stored_at"] is None
    assert body["last_gate_reason"] is None
    assert body["accepted_speech_ratio"] == 0.0


def test_get_stats_reflects_recent_ingests(client: TestClient) -> None:
    """喂几个 silent chunk → gated_rms 应等于 chunks_total，stored 应仍为 0。"""
    # 喂 2 个 SILENT chunk → 都被 rms_gate 拦
    silent = b"\x00" * 1000
    enable_local_capture(client)
    for _ in range(2):
        r = client.post(
            "/capture/chunk",
            files={"audio": ("c.wav", silent, "audio/wav")},
            data={
                "sample_rate": "16000",
                "deviceId": "legacy-local",
                "segmentId": "stats-silent",
            },
        )
        assert r.status_code == 200, r.text
        # 后端 SttStatus 应该是 'gated' 因为前置门拦了
        assert r.json()["stt_status"] == "gated"

    r = client.get("/capture/stats")
    body = r.json()
    assert body["chunks_total"] == 2
    assert body["gated_rms"] == 2
    assert body["stored"] == 0
    assert body["last_chunk_at"] is not None
    assert body["last_stored_at"] is None  # 没 stored 过
    assert body["last_gate_reason"] == "rms_too_low"
    assert body["observed_audio_frames"] == 2
    assert body["accepted_speech_frames"] == 0
    assert body["accepted_speech_ratio"] == 0.0
    assert body["stats_sequence"] == 2


def test_post_chunk_response_includes_stt_status(client: TestClient) -> None:
    """POST /capture/chunk response 必须包含 stt_status 字段（前端止血依赖此 field）。"""
    silent = b"\x00" * 1000
    enable_local_capture(client)
    r = client.post(
        "/capture/chunk",
        files={"audio": ("c.wav", silent, "audio/wav")},
        data={
            "sample_rate": "16000",
            "deviceId": "legacy-local",
            "segmentId": "status-silent",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "stt_status" in body
    assert body["stt_status"] in ("ok", "empty", "failed", "circuit_open", "gated")
    assert body["segment_id"] == "status-silent"


def test_capture_chunk_returns_503_when_authoritative_persistence_fails(
    client: TestClient,
) -> None:
    class _PersistenceFailureProbe:
        async def ingest_chunk(self, *_args: object, **_kwargs: object) -> CaptureChunkResult:
            raise AmbientPersistenceError("ambient persistence unavailable")

    client.app.dependency_overrides[capture_api.get_ambient_pipeline] = _PersistenceFailureProbe
    enable_local_capture(client)
    try:
        response = client.post(
            "/capture/chunk",
            files={"audio": ("c.wav", b"audio", "audio/wav")},
            data={
                "sample_rate": "16000",
                "deviceId": "legacy-local",
                "segmentId": "persist-failure",
            },
        )
    finally:
        client.app.dependency_overrides.pop(capture_api.get_ambient_pipeline, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "ambient persistence unavailable"


def test_capture_result_missing_stt_status_is_unknown() -> None:
    """schema 防御性默认不能把缺字段的旧响应伪装成 ready。"""
    assert CaptureChunkResult().stt_status == "unknown"


def test_recent_projects_client_segment_id(client: TestClient) -> None:
    class _RepositoryProbe:
        async def list_ambient_segments(self, **_kwargs: object) -> list[AmbientSegmentRecord]:
            return [
                AmbientSegmentRecord(
                    audio_ref="/private/probe.wav",
                    text="关联文本",
                    captured_at=datetime.now(UTC),
                    client_segment_id="device:native:recent-17",
                )
            ]

    client.app.dependency_overrides[get_repository] = lambda: _RepositoryProbe()
    try:
        response = client.get("/capture/recent")
    finally:
        client.app.dependency_overrides.pop(get_repository, None)

    assert response.status_code == 200
    assert response.json()[0]["segment_id"] == "device:native:recent-17"


def test_post_chunk_accepts_frontend_silent_wav_without_persisting(
    client: TestClient,
) -> None:
    """前端 WAV 会先解 PCM 做质量门控；静音必须产生零持久文件。"""
    wav = pcm_to_wav(b"\x00\x00" * 16_000, sample_rate=16_000)
    enable_local_capture(client)
    r = client.post(
        "/capture/chunk",
        files={"audio": ("chunk.wav", wav, "audio/wav")},
        data={
            "sample_rate": "16000",
            "deviceId": "legacy-local",
            "segmentId": "frontend-silent",
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stt_status"] == "gated"
    assert body["audio_ref"] == ""


def test_get_stats_endpoint_independent_of_chunk_count(
    client: TestClient,
) -> None:
    """连续调多次 /capture/stats 不应改变 counter（GET 是 idempotent）。"""
    r1 = client.get("/capture/stats")
    assert r1.json()["chunks_total"] == 0
    r2 = client.get("/capture/stats")
    assert r2.json()["chunks_total"] == 0
    r3 = client.get("/capture/stats")
    assert r3.json()["chunks_total"] == 0
