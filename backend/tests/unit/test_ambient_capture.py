"""AmbientCapturePipeline 单测。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline, AmbientPersistenceError


@pytest.fixture
def ambient_pipeline(tmp_path: Path) -> AmbientCapturePipeline:
    # text-clarity PR：默认关 punctuator（已有测试单独覆盖），保持本文件历史
    # 用例的"不调用 LLM"假设；punctuator 集成路径见 test_ambient_punctuator_integration。
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=0,
        ambient_min_speech_frame_ratio=0.0,
        ambient_min_stt_chars=0,
        ambient_llm_punctuate=False,
    )
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[
            TranscriptSegment(text="ambient hello", start_ms=0, end_ms=1000),
        ]
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-20260527")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])
    return AmbientCapturePipeline(
        settings=settings,
        stt=stt,
        rag=rag,
        meeting=meeting,
    )


@pytest.mark.asyncio
async def test_ambient_chunk_always_persisted_and_ingested(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    result = await ambient_pipeline.ingest_chunk(b"\x00" * 1000, sample_rate=16_000)
    assert result.audio_ref
    assert Path(result.audio_ref).exists()
    assert result.ambient_stored is True
    assert result.ambient_text == "ambient hello"
    # M_diag_brake：成功入库的 chunk stt_status="ok"
    assert result.stt_status == "ok"
    ambient_pipeline._rag.ingest_ambient_segment.assert_awaited_once()  # type: ignore[attr-defined]
    ambient_pipeline._meeting.ingest_from_stt.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambient_chunk_preserves_client_segment_correlation(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    repository = AsyncMock()
    repository.append_ambient_segment = AsyncMock(return_value=17)
    ambient_pipeline._repo = repository  # type: ignore[assignment]

    result = await ambient_pipeline.ingest_chunk(
        b"\x00" * 1000,
        sample_rate=16_000,
        client_segment_id="device:native:segment-17",
    )

    assert result.segment_id == "device:native:segment-17"
    repository.append_ambient_segment.assert_awaited_once()
    assert repository.append_ambient_segment.await_args.kwargs["client_segment_id"] == (
        "device:native:segment-17"
    )


@pytest.mark.asyncio
async def test_ambient_with_meeting_overlay(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    seg = TranscriptSegment(text="hi", start_ms=0, end_ms=500, speaker_label="说话人1")
    ambient_pipeline._meeting.ingest_from_stt = AsyncMock(return_value=[seg])  # type: ignore[method-assign]
    result = await ambient_pipeline.ingest_chunk(
        b"\x00" * 1000,
        sample_rate=16_000,
        meeting_id="m-test",
    )
    assert result.ambient_stored is True
    assert len(result.meeting_segments) == 1
    ambient_pipeline._meeting.ingest_from_stt.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambient_stt_fail_still_saves_audio(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    ambient_pipeline._stt.transcribe = AsyncMock(side_effect=RuntimeError("stt down"))  # type: ignore[method-assign]
    result = await ambient_pipeline.ingest_chunk(b"\x01" * 500)
    assert Path(result.audio_ref).exists()
    assert result.ambient_stored is False
    # M_diag_brake：普通失败标 "failed"（非熔断），让前端继续上传
    assert result.stt_status == "failed"
    ambient_pipeline._rag.ingest_ambient_segment.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambient_stored_uses_repository_even_when_rag_projection_fails(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    repository = AsyncMock()
    repository.append_ambient_segment = AsyncMock(return_value=1)
    ambient_pipeline._repo = repository  # type: ignore[assignment]
    ambient_pipeline._rag.ingest_ambient_segment = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("rag down")
    )

    result = await ambient_pipeline.ingest_chunk(b"\x01" * 1000)

    assert result.ambient_stored is True
    assert ambient_pipeline.get_stats().stored == 1
    assert ambient_pipeline.get_stats().segment_store_failed == 0
    repository.set_ambient_rag_projection.assert_awaited_once_with(
        1,
        state="index_failed",
        error="rag down",
    )


@pytest.mark.asyncio
async def test_ambient_stored_does_not_report_rag_only_success_when_repository_fails(
    ambient_pipeline: AmbientCapturePipeline,
) -> None:
    repository = AsyncMock()
    repository.append_ambient_segment = AsyncMock(side_effect=RuntimeError("db down"))
    ambient_pipeline._repo = repository  # type: ignore[assignment]

    with pytest.raises(AmbientPersistenceError, match="ambient persistence unavailable"):
        await ambient_pipeline.ingest_chunk(b"\x01" * 1000)

    assert ambient_pipeline.get_stats().stored == 0
    assert ambient_pipeline.get_stats().segment_store_failed == 1
    ambient_pipeline._rag.ingest_ambient_segment.assert_not_awaited()  # type: ignore[attr-defined]
    repository.set_ambient_rag_projection.assert_not_awaited()


# ── text-clarity PR：LLM punctuator 集成 ───────────────────────────


@pytest.fixture
def ambient_pipeline_with_punctuator(tmp_path: Path) -> AmbientCapturePipeline:
    """与上面 fixture 同构，但启用 LLM punctuator。"""
    from app.adapters.stt.llm_punctuator import LLMPunctuator

    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=0,
        ambient_min_speech_frame_ratio=0.0,
        ambient_min_stt_chars=0,
        ambient_llm_punctuate=True,
        ambient_punctuator_timeout_s=2.0,
    )
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[
            TranscriptSegment(text="我现在身份不是打字员我是代码总导演", start_ms=0, end_ms=2000),
        ]
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-test")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])

    llm = MagicMock()
    from app.schemas.llm import LLMResponse, LLMUsage

    llm.chat = AsyncMock(
        return_value=LLMResponse(
            content='{"items": [{"id": 0, "text": "我现在身份不是打字员，我是代码总导演。"}]}',
            model="fast-test-model",
            finish_reason="stop",
            usage=LLMUsage(),
        )
    )
    punctuator = LLMPunctuator(llm, settings)
    repo = AsyncMock()
    repo.append_ambient_segment = AsyncMock(return_value=1)
    return AmbientCapturePipeline(
        settings=settings,
        stt=stt,
        rag=rag,
        meeting=meeting,
        repository=repo,
        punctuator=punctuator,
    )


@pytest.mark.asyncio
async def test_ambient_punctuator_rewrites_text_before_storage(
    ambient_pipeline_with_punctuator: AmbientCapturePipeline,
) -> None:
    """LLM punctuator 加标点 → 写入 RAG / Repo 的文本已带标点。"""
    result = await ambient_pipeline_with_punctuator.ingest_chunk(b"\x00" * 1000)
    assert result.ambient_stored is True
    assert result.ambient_text == "我现在身份不是打字员，我是代码总导演。"

    rag_call = ambient_pipeline_with_punctuator._rag.ingest_ambient_segment  # type: ignore[attr-defined]
    rag_call.assert_awaited_once()
    args, kwargs = rag_call.call_args
    written_text = args[0] if args else kwargs.get("text", "")
    assert "，" in written_text and "。" in written_text


@pytest.mark.asyncio
async def test_ambient_punctuator_failure_falls_back_to_raw(
    ambient_pipeline_with_punctuator: AmbientCapturePipeline,
) -> None:
    """LLM raise → 主链路不中断；写入的是原 STT 文本（未加标点）。"""
    pipeline = ambient_pipeline_with_punctuator
    # 让 punctuator 的 LLM 报错
    pipeline._punctuator._llm.chat = AsyncMock(side_effect=RuntimeError("yunwu 502"))  # type: ignore[union-attr]

    result = await pipeline.ingest_chunk(b"\x00" * 1000)
    assert result.ambient_stored is True
    # 退回原文本（无标点）
    assert result.ambient_text == "我现在身份不是打字员我是代码总导演"
