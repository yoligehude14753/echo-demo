"""Optional local ASR adapter isolated in a single worker process."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path
from typing import Any

from app.adapters.audio import normalize_audio_bytes
from app.adapters.stt.errors import ASRLocalUnavailable
from app.schemas.meeting import TranscriptSegment

_LOCAL_MODEL: Any = None


def _initialize_local_worker(model_path: str, device: str, compute_type: str) -> None:
    global _LOCAL_MODEL  # noqa: PLW0603
    if not Path(model_path).exists():
        raise RuntimeError("configured local ASR model path does not exist")
    try:
        from faster_whisper import WhisperModel
    except Exception as error:  # pragma: no cover - optional runtime boundary
        raise RuntimeError("local ASR runtime is unavailable") from error
    _LOCAL_MODEL = WhisperModel(model_path, device=device, compute_type=compute_type)


def _transcribe_local_worker(
    audio_bytes: bytes,
    sample_rate: int,
    language: str,
) -> list[TranscriptSegment]:
    if _LOCAL_MODEL is None:
        raise RuntimeError("local ASR worker is not initialized")
    try:
        import numpy as np

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32_768.0
        segments, _info = _LOCAL_MODEL.transcribe(
            audio,
            language=language,
            vad_filter=True,
            beam_size=1,
        )
        result: list[TranscriptSegment] = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            result.append(
                TranscriptSegment(
                    text=text,
                    start_ms=max(0, int(float(getattr(segment, "start", 0.0)) * 1000)),
                    end_ms=max(0, int(float(getattr(segment, "end", 0.0)) * 1000)),
                    speaker_id=None,
                    speaker_label=None,
                )
            )
        return result
    except Exception as error:  # pragma: no cover - optional runtime boundary
        raise RuntimeError("local ASR inference failed") from error


class LocalSTT:
    """STTPort implementation backed by one isolated local model worker."""

    transport = "local_worker"

    def __init__(
        self,
        *,
        model_path: str,
        device: str = "cpu",
        compute_type: str = "int8",
        worker_count: int = 1,
        executor: Executor | None = None,
        worker_fn: Callable[[bytes, int, str], list[TranscriptSegment]] | None = None,
    ) -> None:
        if worker_count != 1:
            raise ValueError("local ASR worker_count must remain 1")
        self._model_path = model_path.strip()
        self._device = device
        self._compute_type = compute_type
        self._worker_fn = worker_fn or _transcribe_local_worker
        self._owns_executor = executor is None and bool(self._model_path)
        self._executor: Executor | None = executor
        if self._executor is None and self._model_path:
            self._executor = ProcessPoolExecutor(
                max_workers=1,
                initializer=_initialize_local_worker,
                initargs=(self._model_path, self._device, self._compute_type),
            )

    @property
    def worker_count(self) -> int:
        return 1

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        if not self._model_path or self._executor is None:
            raise ASRLocalUnavailable()
        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        if not normalized.pcm:
            return []
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor,
                self._worker_fn,
                normalized.pcm,
                normalized.sample_rate,
                language,
            )
        except ASRLocalUnavailable:
            raise
        except Exception as error:
            raise ASRLocalUnavailable() from error
        if not isinstance(result, list) or any(
            not isinstance(segment, TranscriptSegment) for segment in result
        ):
            raise ASRLocalUnavailable()
        return result

    async def aclose(self) -> None:
        if not self._owns_executor or self._executor is None:
            return
        executor = self._executor
        self._executor = None
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


__all__ = ["LocalSTT"]
