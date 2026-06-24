"""AmbientStats 7 道门 in-memory 计数器单测（M_diag_brake）。

每条路径单独 mock 出特定行为，断言唯一对应的末态 counter +1，
其它 counter 不动。这样回归保证：未来重构 ingest_chunk 时如果末态分流
逻辑变了，单测会立刻红。

设计：用极小的 1KB 静音 audio + Settings 把所有 gate / hallu 阈值降到 0
作为 baseline；每个 test fixture 用 monkeypatch 单独把 stt mock 成对应
行为，验证 _stats 的具体字段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline, AmbientStats

# 1KB 全零 PCM ≈ 31ms @ 16kHz mono 16bit；足够触发 ingest_chunk
SILENT_1KB = b"\x00" * 1000
# 几乎全零但插一个尖峰：让 integer_rms 仍极低
QUIET_NOISE_1KB = b"\x00\x00" * 500


def _make_pipeline(
    *,
    tmp_path: Path,
    stt: AsyncMock,
    rms_gate: int = 0,
    min_speech_frame_ratio: float = 0.0,
    min_stt_chars: int = 0,
    max_cps: float = 1000.0,
) -> AmbientCapturePipeline:
    """构造 ambient pipeline，所有阈值默认放开（让 baseline 路径直通入库）。"""
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=rms_gate,
        ambient_min_speech_frame_ratio=min_speech_frame_ratio,
        ambient_min_stt_chars=min_stt_chars,
        ambient_max_cps=max_cps,
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-0")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])
    return AmbientCapturePipeline(
        settings=settings,
        stt=stt,
        rag=rag,
        meeting=meeting,
    )


def _stats_dict(stats: AmbientStats) -> dict[str, Any]:
    """slots dataclass → dict（避免 dataclasses.asdict 在 PyPI mypy 严格场景报警）。"""
    return {
        "chunks_total": stats.chunks_total,
        "gated_rms": stats.gated_rms,
        "gated_low_speech": stats.gated_low_speech,
        "stt_circuit_open": stats.stt_circuit_open,
        "stt_failed": stats.stt_failed,
        "stt_empty": stats.stt_empty,
        "hallu_dropped": stats.hallu_dropped,
        "diarize_failed": stats.diarize_failed,
        "stored": stats.stored,
    }


@pytest.mark.asyncio
async def test_stats_initial_state_is_zero(tmp_path: Path) -> None:
    pipe = _make_pipeline(tmp_path=tmp_path, stt=AsyncMock())
    s = pipe.get_stats()
    assert _stats_dict(s) == {
        "chunks_total": 0,
        "gated_rms": 0,
        "gated_low_speech": 0,
        "stt_circuit_open": 0,
        "stt_failed": 0,
        "stt_empty": 0,
        "hallu_dropped": 0,
        "diarize_failed": 0,
        "stored": 0,
    }
    assert s.last_chunk_at is None
    assert s.last_stored_at is None
    assert s.last_rms == 0
    assert s.last_speech_ratio == 0
    assert s.last_gate_reason is None


@pytest.mark.asyncio
async def test_gated_rms_increment(tmp_path: Path) -> None:
    """整段 RMS 不达标 → gated_rms +1，其它末态全 0；不调 STT。"""
    stt = AsyncMock()
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt, rms_gate=10_000)
    result = await pipe.ingest_chunk(SILENT_1KB)
    assert result.stt_status == "gated"
    assert result.ambient_stored is False
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.gated_rms == 1
    assert s.gated_low_speech == 0
    assert s.stored == 0
    assert s.last_rms == 0
    assert s.last_speech_ratio == 0
    assert s.last_gate_reason == "rms_too_low"
    stt.transcribe.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stt_circuit_open_increment(tmp_path: Path) -> None:
    """STT 抛含 'circuit open' 的异常 → stt_circuit_open +1，stt_status='circuit_open'。"""
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        side_effect=RuntimeError("firered circuit open (3 consecutive failures)"),
    )
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt)
    result = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert result.stt_status == "circuit_open"
    assert result.ambient_stored is False
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.stt_circuit_open == 1
    assert s.stt_failed == 0
    assert s.stored == 0


@pytest.mark.asyncio
async def test_stt_failed_increment(tmp_path: Path) -> None:
    """STT 普通异常（非熔断）→ stt_failed +1，stt_status='failed'，前端可继续上传。"""
    stt = AsyncMock()
    stt.transcribe = AsyncMock(side_effect=RuntimeError("connection refused"))
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt)
    result = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert result.stt_status == "failed"
    assert result.ambient_stored is False
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.stt_failed == 1
    assert s.stt_circuit_open == 0


@pytest.mark.asyncio
async def test_stt_busy_is_failed_not_circuit_open(tmp_path: Path) -> None:
    """上一条 STT 未结束时，新 chunk 快速失败，不打 eight，也不触发前端熔断。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_transcribe(*_args: Any, **_kwargs: Any) -> list[TranscriptSegment]:
        started.set()
        await release.wait()
        return [TranscriptSegment(text="这是一段有效转写", start_ms=0, end_ms=1000)]

    stt = AsyncMock()
    stt.transcribe = AsyncMock(side_effect=_slow_transcribe)
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt)

    first_task = asyncio.create_task(pipe.ingest_chunk(QUIET_NOISE_1KB))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    second = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert second.stt_status == "failed"
    assert second.ambient_stored is False
    assert stt.transcribe.await_count == 1

    release.set()
    first = await first_task
    assert first.stt_status == "ok"
    assert first.ambient_stored is True

    s = pipe.get_stats()
    assert s.chunks_total == 2
    assert s.stt_failed == 1
    assert s.stt_circuit_open == 0
    assert s.stored == 1


@pytest.mark.asyncio
async def test_stt_empty_increment(tmp_path: Path) -> None:
    """STT 调用成功但返回空 segs → stt_empty +1，stt_status='empty'。"""
    stt = AsyncMock()
    stt.transcribe = AsyncMock(return_value=[])
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt)
    result = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert result.stt_status == "empty"
    assert result.ambient_stored is False
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.stt_empty == 1
    assert s.stt_failed == 0
    assert s.stored == 0


@pytest.mark.asyncio
async def test_hallu_dropped_increment(tmp_path: Path) -> None:
    """STT 返回文本但过短 → hallu_dropped +1。stt_status 保留 'ok'。

    幻觉门的语义是「STT 健康但内容被过滤」，故 stt_status='ok' 让前端能
    区分「STT 没听到」vs「STT 听到但被丢弃」（前者用户该担心，后者通常
    正常 — 噪声背景音里偶发 'うん' 这种 ASR 幻觉）。
    """
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[TranscriptSegment(text="嗯", start_ms=0, end_ms=100)],
    )
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt, min_stt_chars=5)
    result = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert result.stt_status == "ok"
    assert result.ambient_stored is False
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.hallu_dropped == 1
    assert s.stored == 0


@pytest.mark.asyncio
async def test_stored_increment_and_timestamps(tmp_path: Path) -> None:
    """正常路径：STT 返回合规文本 → stored +1，last_stored_at 更新。"""
    stt = AsyncMock()
    stt.transcribe = AsyncMock(
        return_value=[
            TranscriptSegment(text="今天天气真不错", start_ms=0, end_ms=2000),
        ],
    )
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt)
    assert pipe.get_stats().last_chunk_at is None
    assert pipe.get_stats().last_stored_at is None
    result = await pipe.ingest_chunk(QUIET_NOISE_1KB)
    assert result.stt_status == "ok"
    assert result.ambient_stored is True
    s = pipe.get_stats()
    assert s.chunks_total == 1
    assert s.stored == 1
    assert s.last_chunk_at is not None
    assert s.last_stored_at is not None
    assert s.last_chunk_at == s.last_stored_at  # 同步路径，timestamps 应一致


@pytest.mark.asyncio
async def test_chunks_total_sums_all_paths(tmp_path: Path) -> None:
    """末态计数器之和 == chunks_total（除了 diarize_failed 是 side-channel）。

    这是 invariant 验证：用户翻 stats 求和应等于 chunks_total，否则有
    chunk 被"吃掉"没归类，调试时会很困惑。
    """
    stt = AsyncMock()
    pipe = _make_pipeline(tmp_path=tmp_path, stt=stt, rms_gate=10_000)
    # 3 个 chunk 全走 gated_rms 路径
    for _ in range(3):
        await pipe.ingest_chunk(SILENT_1KB)
    s = pipe.get_stats()
    end_states_sum = (
        s.gated_rms
        + s.gated_low_speech
        + s.stt_circuit_open
        + s.stt_failed
        + s.stt_empty
        + s.hallu_dropped
        + s.stored
    )
    assert end_states_sum == s.chunks_total == 3
