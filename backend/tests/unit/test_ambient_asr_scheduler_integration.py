"""Ambient capture 到 ASR scheduler 的最小主线接线测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.stt import build_asr_scheduler
from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.errors import (
    ASRDeadlineExceeded,
    ASRIdempotencyConflict,
    ASRNoEligibleProvider,
    ASRQueueFull,
    ASRRateLimited,
)
from app.config import Settings
from app.main import _bootstrap_payload
from app.schemas.meeting import TranscriptSegment
from app.use_cases.ambient_capture import AmbientCapturePipeline

LOUD_PCM = b"\x10\x10" * 800
SILENT_PCM = b"\x00" * 1600


class FakeScheduler:
    def __init__(
        self, result: list[TranscriptSegment] | None = None, error: Exception | None = None
    ):
        self.result = result or [TranscriptSegment(text="scheduler text", start_ms=0, end_ms=100)]
        self.error = error
        self.calls: list[tuple[bytes, int, ASRRequestContext]] = []

    async def transcribe(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        context: ASRRequestContext,
    ) -> list[TranscriptSegment]:
        self.calls.append((audio, sample_rate, context))
        if self.error is not None:
            raise self.error
        return self.result


def make_pipeline(
    tmp_path: Path,
    *,
    scheduler: FakeScheduler | None,
    enabled: bool,
    stt: MagicMock | None = None,
    rms_gate: int = 0,
    event_bus: AsyncMock | None = None,
) -> AmbientCapturePipeline:
    settings = Settings(
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag",
        ambient_rms_gate=rms_gate,
        ambient_frame_rms_threshold=0,
        ambient_min_speech_frame_ratio=0.0,
        ambient_min_stt_chars=0,
        ambient_llm_punctuate=False,
        asr_scheduler_enabled=enabled,
        asr_job_deadline_s=0.4,
    )
    legacy_stt = stt or MagicMock()
    legacy_stt.transcribe = AsyncMock(
        return_value=[TranscriptSegment(text="legacy text", start_ms=0, end_ms=100)]
    )
    rag = AsyncMock()
    rag.ingest_ambient_segment = AsyncMock(return_value="ambient-1")
    meeting = MagicMock()
    meeting.ingest_from_stt = AsyncMock(return_value=[])
    return AmbientCapturePipeline(
        settings=settings,
        stt=legacy_stt,
        rag=rag,
        meeting=meeting,
        asr_scheduler=scheduler,  # type: ignore[arg-type]
        event_bus=event_bus,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_runs_before_scheduler_and_does_not_enqueue(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    pipeline = make_pipeline(tmp_path, scheduler=scheduler, enabled=True, rms_gate=10_000)

    result = await pipeline.ingest_chunk(SILENT_PCM)

    assert result.stt_status == "gated"
    assert scheduler.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enabled_scheduler_success_uses_authenticated_scope_and_bounded_deadline(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    pipeline = make_pipeline(tmp_path, scheduler=scheduler, enabled=True)
    incoming = ASRRequestContext(
        request_id="capture-request",
        idempotency_key="capture-idempotency",
        tenant_id="client-tenant-must-not-win",
        principal_id="client-owner-must-not-win",
        device_id="client-device-must-not-win",
        deadline_s=999.0,
    )

    result = await pipeline.ingest_chunk(LOUD_PCM, asr_context=incoming)

    assert result.ambient_text == "scheduler text"
    assert len(scheduler.calls) == 1
    _, sample_rate, context = scheduler.calls[0]
    assert sample_rate == 16_000
    assert context.request_id == "capture-request"
    assert context.idempotency_key == "capture-idempotency"
    assert context.tenant_id == "legacy-local"
    assert context.principal_id == "legacy-local"
    assert context.device_id == "legacy-local"
    assert context.deadline_s == 0.4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_scheduler_keeps_legacy_stt(tmp_path: Path) -> None:
    legacy_stt = MagicMock()
    legacy_stt.transcribe = AsyncMock(
        return_value=[TranscriptSegment(text="legacy text", start_ms=0, end_ms=100)]
    )
    scheduler = FakeScheduler()
    pipeline = make_pipeline(
        tmp_path,
        scheduler=scheduler,
        enabled=False,
        stt=legacy_stt,
    )

    result = await pipeline.ingest_chunk(LOUD_PCM)

    assert result.ambient_text == "legacy text"
    legacy_stt.transcribe.assert_awaited_once()
    assert scheduler.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ASRIdempotencyConflict(),
        ASRRateLimited(retry_after_s=2.0),
        ASRQueueFull(retry_after_s=1.0),
        ASRNoEligibleProvider(retry_after_s=3.0),
        ASRDeadlineExceeded(),
    ],
)
async def test_typed_scheduler_errors_cross_ambient_boundary(
    tmp_path: Path,
    error: Exception,
) -> None:
    pipeline = make_pipeline(
        tmp_path,
        scheduler=FakeScheduler(error=error),
        enabled=True,
    )

    with pytest.raises(type(error)) as raised:
        await pipeline.ingest_chunk(LOUD_PCM)

    assert raised.value is error


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_success_does_not_publish_sync_or_outbox_event(tmp_path: Path) -> None:
    event_bus = AsyncMock()
    pipeline = make_pipeline(
        tmp_path,
        scheduler=FakeScheduler(),
        enabled=True,
        event_bus=event_bus,
    )

    await pipeline.ingest_chunk(LOUD_PCM)

    event_bus.publish.assert_not_awaited()


@pytest.mark.unit
def test_bootstrap_readiness_is_public_and_versioned() -> None:
    payload = _bootstrap_payload(Settings(public_demo_mode=True))
    readiness = payload["capabilities"]["transcription_readiness"]  # type: ignore[index]

    assert readiness["schema_version"] == 1  # type: ignore[index]
    assert set(readiness) == {  # type: ignore[arg-type]
        "schema_version",
        "status",
        "accepting",
        "checked_at",
        "ttl_s",
        "reason_code",
        "retry_after_s",
    }
    assert "providers" not in readiness  # type: ignore[operator]
    assert "queue_capacity" not in readiness  # type: ignore[operator]


@pytest.mark.unit
def test_stale_readiness_is_unknown_without_internal_scheduler_shape() -> None:
    settings = Settings(asr_scheduler_enabled=True)
    scheduler = build_asr_scheduler(settings)
    try:
        scheduler.record_controlled_probe(
            True,
            checked_at=datetime.now(UTC)
            - timedelta(seconds=settings.asr_readiness_stale_after_s + 1),
        )
        readiness = scheduler.readiness().to_public(
            ttl_s=settings.asr_readiness_stale_after_s,
        )
        assert readiness.schema_version == 1
        assert readiness.status == "unknown"
        assert readiness.reason_code == "asr_probe_stale"
        assert not hasattr(readiness, "providers")
    finally:
        # This scheduler has no workers, but provider adapters may own clients.
        # The public readiness projection itself must not perform I/O.
        pass
