"""真实 ASR scheduler/capture telemetry wiring 的聚焦测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from app.adapters.stt.contracts import ASRRequestContext
from app.adapters.stt.errors import ASRProviderTransientError
from app.adapters.stt.scheduler import ASRProviderBinding, ASRScheduler, ASRSchedulerConfig
from app.config import Settings
from app.schemas.meeting import TranscriptSegment
from app.telemetry.adapters import NoopTelemetryAdapter
from app.telemetry.contracts import TelemetryQuery
from app.telemetry.runtime import TelemetryRuntime, build_telemetry_runtime


class FakeProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def transcribe(
        self,
        _audio: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        del sample_rate, language
        if self.error is not None:
            raise self.error
        return [TranscriptSegment(text="ok", start_ms=0, end_ms=100)]


def make_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    return Settings(
        telemetry_enabled=enabled,
        telemetry_db_path=tmp_path / "independent-telemetry.sqlite3",
        telemetry_hmac_key_ring={"v1": "telemetry-test-key-" + "x" * 32},
        telemetry_hmac_current_key_version="v1",
        telemetry_k_threshold=1,
        asr_job_deadline_s=0.2,
    )


def context(request_id: str) -> ASRRequestContext:
    return ASRRequestContext(
        request_id=request_id,
        tenant_id="tenant-server",
        principal_id="user-server",
        device_id="device-server",
        platform="desktop",
        app_version="0.3.3",
    )


def make_scheduler(runtime: TelemetryRuntime, provider: FakeProvider) -> ASRScheduler:
    return ASRScheduler(
        {"firered": ASRProviderBinding(name="firered", adapter=provider)},
        ASRSchedulerConfig(
            enabled=True,
            eligible_providers=("firered",),
            max_concurrency=1,
            queue_size=1,
            job_deadline_s=0.2,
            max_attempts=1,
            scope_max_concurrency=2,
            scope_rate_limit_per_minute=20,
        ),
        telemetry=runtime,
    )


@pytest.mark.unit
def test_telemetry_off_is_strict_noop(tmp_path: Path) -> None:
    runtime = build_telemetry_runtime(Settings(telemetry_enabled=False))
    assert isinstance(runtime.sink, NoopTelemetryAdapter)
    assert not (tmp_path / "independent-telemetry.sqlite3").exists()
    assert runtime.sink_failure_count == 0


@pytest.mark.unit
def test_enabled_telemetry_missing_key_or_path_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="HMAC"):
        build_telemetry_runtime(
            Settings(
                telemetry_enabled=True,
                telemetry_db_path=tmp_path / "telemetry.sqlite3",
            )
        )
    with pytest.raises(RuntimeError, match="DB path"):
        build_telemetry_runtime(
            Settings(
                telemetry_enabled=True,
                telemetry_db_path=None,
                telemetry_hmac_key_ring={"v1": "x" * 40},
                telemetry_hmac_current_key_version="v1",
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_scheduler_success_and_typed_failure_are_instrumented(
    tmp_path: Path,
) -> None:
    runtime = build_telemetry_runtime(make_settings(tmp_path, enabled=True))
    success_scheduler = make_scheduler(runtime, FakeProvider())
    failure_scheduler = make_scheduler(
        runtime,
        FakeProvider(error=ASRProviderTransientError()),
    )
    audio = b"\x10\x10" * 800
    try:
        result = await success_scheduler.transcribe(audio, context=context("success"))
        assert result[0].text == "ok"
        with pytest.raises(ASRProviderTransientError):
            await failure_scheduler.transcribe(audio, context=context("failure"))
        await asyncio.sleep(0.05)
        aggregates = await runtime.sink.query(TelemetryQuery(k_threshold=1))
        assert len(aggregates) == 1
        aggregate = aggregates[0]
        assert aggregate.request_count == 2
        assert aggregate.success_count == 1
        assert aggregate.failure_count == 1
        assert aggregate.provider.value == "firered"
        assert aggregate.audio_duration_event_count == 2
        assert aggregate.distinct_user_count == 1
    finally:
        await success_scheduler.close(grace_period_s=0.2)
        await failure_scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telemetry_sink_failure_does_not_break_transcription(tmp_path: Path) -> None:
    class BrokenSink:
        async def record(self, _observation: object) -> None:
            raise RuntimeError("sink body must not escape")

    runtime = TelemetryRuntime(BrokenSink())  # type: ignore[arg-type]
    scheduler = make_scheduler(runtime, FakeProvider())
    try:
        result = await scheduler.transcribe(b"\x10\x10" * 800, context=context("sink-fail"))
        assert result[0].text == "ok"
        await asyncio.sleep(0.05)
        assert runtime.sink_failure_count == 1
    finally:
        await scheduler.close(grace_period_s=0.2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telemetry_db_is_independent_from_sync_outbox(tmp_path: Path) -> None:
    runtime = build_telemetry_runtime(make_settings(tmp_path, enabled=True))
    await runtime.sink.purge_expired()
    connection = sqlite3.connect(tmp_path / "independent-telemetry.sqlite3")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert tables == {
        "telemetry_schema_version",
        "telemetry_events",
        "telemetry_deletion_audit",
    }
