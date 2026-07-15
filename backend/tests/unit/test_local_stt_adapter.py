"""Local ASR worker isolation tests with an in-process executor fake."""

from __future__ import annotations

from concurrent.futures import Future

import pytest
from app.adapters.stt.errors import ASRLocalUnavailable
from app.adapters.stt.local import LocalSTT
from app.schemas.meeting import TranscriptSegment


class RecordingExecutor:
    def __init__(self) -> None:
        self.max_workers = 1
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, fn: object, *args: object, **_kwargs: object) -> Future[object]:
        self.calls.append((fn, args))
        future: Future[object] = Future()
        future.set_result(fn(*args))  # type: ignore[operator]
        return future


def fake_worker(
    audio_bytes: bytes,
    sample_rate: int,
    language: str,
) -> list[TranscriptSegment]:
    assert audio_bytes
    return [TranscriptSegment(text=f"{language}:{sample_rate}", start_ms=0, end_ms=100)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_adapter_uses_one_isolated_worker_and_keeps_event_loop_async() -> None:
    executor = RecordingExecutor()
    adapter = LocalSTT(
        model_path="/models/unit",
        executor=executor,
        worker_fn=fake_worker,
    )
    try:
        result = await adapter.transcribe(b"\x01\x00" * 80, sample_rate=16_000, language="zh")
        assert adapter.transport == "local_worker"
        assert adapter.worker_count == 1
        assert executor.max_workers == 1
        assert result[0].text == "zh:16000"
        assert len(executor.calls) == 1
    finally:
        await adapter.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_adapter_fails_closed_when_model_runtime_is_not_ready() -> None:
    executor = RecordingExecutor()
    adapter = LocalSTT(model_path="", executor=executor, worker_fn=fake_worker)
    try:
        with pytest.raises(ASRLocalUnavailable):
            await adapter.transcribe(b"\x01\x00" * 80)
        assert executor.calls == []
    finally:
        await adapter.aclose()


@pytest.mark.unit
def test_local_worker_count_cannot_be_scaled_by_accident() -> None:
    with pytest.raises(ValueError):
        LocalSTT(model_path="/models/unit", worker_count=2)
