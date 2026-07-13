"""GET /capture/stats endpoint 单测（M_diag_brake）。

验证 endpoint 返回的字段集 + 类型 + 内容反映 in-memory counter。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.adapters.audio import pcm_to_wav
from app.api.capture import reset_ambient_pipeline
from app.config import Settings, get_settings
from app.main import create_app
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
}


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
    }
    for f in int_fields:
        assert body[f] == 0, f"expect {f}=0 on fresh pipeline, got {body[f]}"
    assert body["last_chunk_at"] is None
    assert body["last_stored_at"] is None
    assert body["last_audio_stored_at"] is None
    assert body["last_gate_reason"] is None


def test_get_stats_reflects_recent_ingests(client: TestClient) -> None:
    """喂几个 silent chunk → gated_rms 应等于 chunks_total，stored 应仍为 0。"""
    # 喂 2 个 SILENT chunk → 都被 rms_gate 拦
    silent = b"\x00" * 1000
    for _ in range(2):
        r = client.post(
            "/capture/chunk",
            files={"audio": ("c.wav", silent, "audio/wav")},
            data={"sample_rate": "16000"},
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


def test_post_chunk_response_includes_stt_status(client: TestClient) -> None:
    """POST /capture/chunk response 必须包含 stt_status 字段（前端止血依赖此 field）。"""
    silent = b"\x00" * 1000
    r = client.post(
        "/capture/chunk",
        files={"audio": ("c.wav", silent, "audio/wav")},
        data={"sample_rate": "16000"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "stt_status" in body
    assert body["stt_status"] in ("ok", "empty", "failed", "circuit_open", "gated")


def test_post_chunk_accepts_frontend_silent_wav_without_persisting(
    client: TestClient,
) -> None:
    """前端 WAV 会先解 PCM 做质量门控；静音必须产生零持久文件。"""
    wav = pcm_to_wav(b"\x00\x00" * 16_000, sample_rate=16_000)
    r = client.post(
        "/capture/chunk",
        files={"audio": ("chunk.wav", wav, "audio/wav")},
        data={"sample_rate": "16000"},
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
