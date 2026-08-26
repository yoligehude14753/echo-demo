"""StepFun ASR capability adapters.

SSE one-shot and WebSocket stream are intentionally separate classes. They
share only typed final/partial result contracts; unfinished payloads and
session state never cross the transport boundary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import websockets

from app.adapters.audio import normalize_audio_bytes
from app.adapters.stt.contracts import ASRFinalTranscript, ASRPartialEvent
from app.adapters.stt.errors import (
    ASRDeadlineExceeded,
    ASRError,
    ASRProviderAuthError,
    ASRProviderMidstreamError,
    ASRProviderPermanentError,
    ASRProviderProtocolError,
    ASRProviderRateLimited,
    ASRProviderSessionCapacity,
    ASRProviderTransientError,
)
from app.schemas.meeting import TranscriptSegment


@dataclass(frozen=True, slots=True)
class StepFunSettings:
    api_key: str
    sse_url: str = "https://api.stepfun.com/v1/audio/asr/sse"
    websocket_url: str = "wss://api.stepfun.com/v1/realtime/asr/stream"
    sse_model: str = "stepaudio-2.5-asr"
    websocket_model: str = "stepaudio-2.5-asr-stream"
    timeout_s: float = 30.0
    idle_timeout_s: float = 10.0
    max_duration_s: float = 120.0
    max_sessions: int = 4
    send_queue_size: int = 8

    def __post_init__(self) -> None:
        if not self.sse_url.strip() or not self.websocket_url.strip():
            raise ValueError("StepFun transport URLs must not be empty")
        if not self.sse_model.strip() or not self.websocket_model.strip():
            raise ValueError("StepFun models must not be empty")
        if self.timeout_s <= 0 or self.idle_timeout_s <= 0 or self.max_duration_s <= 0:
            raise ValueError("StepFun timeouts must be positive")
        if self.max_sessions < 1:
            raise ValueError("StepFun max_sessions must be >= 1")
        if self.send_queue_size < 1:
            raise ValueError("StepFun send_queue_size must be >= 1")


@dataclass(frozen=True, slots=True)
class StepFunStreamResult:
    final: ASRFinalTranscript
    partial_events: tuple[ASRPartialEvent, ...]


def _duration_ms(audio_bytes: bytes, sample_rate: int) -> int:
    return int(len(audio_bytes) / max(1, sample_rate * 2) * 1000)


def _final_result(text: str, audio_bytes: bytes, sample_rate: int) -> ASRFinalTranscript:
    normalized = text.strip()
    if not normalized:
        return ASRFinalTranscript(segments=())
    return ASRFinalTranscript(
        segments=(
            TranscriptSegment(
                text=normalized,
                start_ms=0,
                end_ms=_duration_ms(audio_bytes, sample_rate),
                speaker_id=None,
                speaker_label=None,
            ),
        )
    )


def _event_text(payload: dict[str, Any]) -> str:
    for key in ("text", "transcript", "delta"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


class StepFunSSEOneShotSTT:
    """Capability adapter for one-shot HTTP SSE transcription."""

    transport = "sse_one_shot"

    def __init__(self, settings: StepFunSettings) -> None:
        self._settings = settings

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        result = await self.stream(audio_bytes, sample_rate=sample_rate, language=language)
        return list(result.final.segments)

    async def stream(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
        request_id: str = "",
    ) -> StepFunStreamResult:
        if not self._settings.api_key.strip():
            raise ASRProviderAuthError()
        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        if not normalized.pcm:
            return StepFunStreamResult(ASRFinalTranscript(segments=()), ())
        payload = {
            "model": self._settings.sse_model,
            "audio": {
                "data": base64.b64encode(normalized.pcm).decode("ascii"),
                "format": "pcm",
                "sample_rate": normalized.sample_rate,
            },
            "language": language,
        }
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._settings.timeout_s,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    self._settings.sse_url,
                    headers=headers,
                    json=payload,
                ) as response,
            ):
                self._raise_http_status(response)
                return await self._parse_sse(
                    response.aiter_lines(),
                    request_id=request_id,
                    audio_bytes=normalized.pcm,
                    sample_rate=normalized.sample_rate,
                )
        except ASRError:
            raise
        except (httpx.TimeoutException, TimeoutError, ConnectionError, OSError) as error:
            raise ASRProviderTransientError() from error
        except Exception as error:
            raise ASRProviderPermanentError() from error

    @staticmethod
    def _raise_http_status(response: Any) -> None:
        status_code = int(getattr(response, "status_code", 200))
        if status_code in {401, 403}:
            raise ASRProviderAuthError()
        if status_code == 429:
            raise ASRProviderRateLimited(
                retry_after_s=StepFunSSEOneShotSTT._retry_after_s(response),
            )
        if status_code >= 500:
            raise ASRProviderTransientError()
        if status_code >= 400:
            raise ASRProviderPermanentError()
        response.raise_for_status()

    @staticmethod
    def _retry_after_s(response: Any) -> float:
        """Parse bounded provider retry advice without exposing response data."""

        headers = getattr(response, "headers", {})
        raw_value = headers.get("Retry-After") if hasattr(headers, "get") else None
        if raw_value is None:
            return 1.0
        try:
            value = float(str(raw_value).strip())
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(raw_value).strip())
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                value = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return 1.0
        if value < 0:
            return 0.0
        return min(value, 60.0)

    async def _parse_sse(
        self,
        lines: AsyncIterator[str],
        *,
        request_id: str,
        audio_bytes: bytes,
        sample_rate: int,
    ) -> StepFunStreamResult:
        event_name = "message"
        data_lines: list[str] = []
        partials: list[ASRPartialEvent] = []
        final_text = ""
        sequence = 0

        async for line in lines:
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and data_lines:
                event_name, payload = self._decode_sse_event(event_name, data_lines)
                if event_name == "error":
                    raise ASRProviderTransientError()
                if event_name.endswith(".delta"):
                    partials.append(
                        ASRPartialEvent(
                            request_id=request_id,
                            sequence=sequence,
                            text=_event_text(payload),
                        )
                    )
                    sequence += 1
                elif event_name.endswith(".done") or event_name.endswith(".completed"):
                    final_text = _event_text(payload)
                event_name = "message"
                data_lines = []
        if data_lines:
            event_name, payload = self._decode_sse_event(event_name, data_lines)
            if event_name.endswith(".done") or event_name.endswith(".completed"):
                final_text = _event_text(payload)
        if not final_text and not partials:
            raise ASRProviderProtocolError()
        return StepFunStreamResult(
            final=_final_result(final_text, audio_bytes, sample_rate),
            partial_events=tuple(partials),
        )

    @staticmethod
    def _decode_sse_event(event_name: str, data_lines: list[str]) -> tuple[str, dict[str, Any]]:
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError) as error:
            raise ASRProviderProtocolError() from error
        if not isinstance(payload, dict):
            raise ASRProviderProtocolError()
        return event_name, payload


class StepFunWebSocketStreamSTT:
    """Capability adapter for session-scoped WebSocket ASR streaming."""

    transport = "websocket_stream"

    def __init__(
        self, settings: StepFunSettings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._session_slots = asyncio.Semaphore(settings.max_sessions)

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
    ) -> list[TranscriptSegment]:
        result = await self.stream([audio_bytes], sample_rate=sample_rate, language=language)
        return list(result.final.segments)

    async def stream(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
        *,
        sample_rate: int = 16_000,
        language: str = "zh",
        request_id: str = "",
    ) -> StepFunStreamResult:
        if not self._settings.api_key.strip():
            raise ASRProviderAuthError()
        try:
            await asyncio.wait_for(
                self._session_slots.acquire(),
                timeout=min(1.0, self._settings.timeout_s),
            )
        except TimeoutError as error:
            raise ASRProviderSessionCapacity(retry_after_s=1.0) from error

        audio_sent = False
        started = self._clock()
        try:
            headers = {"Authorization": f"Bearer {self._settings.api_key}"}
            async with websockets.connect(
                self._settings.websocket_url,
                additional_headers=headers,
                open_timeout=self._settings.timeout_s,
            ) as websocket:
                await self._send_json(
                    websocket,
                    {
                        "type": "session.update",
                        "session": {
                            "model": self._settings.websocket_model,
                            "input_audio_format": "pcm16",
                            "turn_detection": None,
                            "language": language,
                        },
                    },
                    started,
                )
                audio_sent = await self._send_audio_chunks(
                    websocket,
                    chunks,
                    sample_rate=sample_rate,
                    started=started,
                )
                await self._send_json(
                    websocket,
                    {"type": "input_audio_buffer.commit"},
                    started,
                )
                return await self._receive_result(
                    websocket,
                    request_id=request_id,
                    audio_bytes=b"",
                    sample_rate=sample_rate,
                    started=started,
                    audio_sent=audio_sent,
                )
        except ASRError:
            raise
        except TimeoutError as error:
            raise ASRDeadlineExceeded() from error
        except (ConnectionError, OSError, RuntimeError) as error:
            if audio_sent:
                raise ASRProviderMidstreamError() from error
            raise ASRProviderTransientError() from error
        except Exception as error:
            if audio_sent:
                raise ASRProviderMidstreamError() from error
            raise ASRProviderTransientError() from error
        finally:
            self._session_slots.release()

    async def _send_audio_chunks(
        self,
        websocket: Any,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
        *,
        sample_rate: int,
        started: float,
    ) -> bool:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._settings.send_queue_size)
        sent_any = False

        async def send_loop() -> None:
            nonlocal sent_any
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    normalized = normalize_audio_bytes(item, sample_rate=sample_rate)
                    if not normalized.pcm:
                        continue
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(normalized.pcm).decode("ascii"),
                            }
                        )
                    )
                    sent_any = True
                finally:
                    queue.task_done()

        sender = asyncio.create_task(send_loop())
        try:
            iterator = chunks.__aiter__() if hasattr(chunks, "__aiter__") else None
            if iterator is not None:
                async for chunk in iterator:
                    await self._put_with_deadline(queue, chunk, started)
            else:
                for chunk in chunks:  # type: ignore[union-attr]
                    await self._put_with_deadline(queue, chunk, started)
            await self._wait_with_deadline(queue.join(), started)
            await self._put_with_deadline(queue, None, started)
            await self._wait_with_deadline(sender, started)
            return sent_any
        except BaseException:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            raise

    async def _receive_result(
        self,
        websocket: Any,
        *,
        request_id: str,
        audio_bytes: bytes,
        sample_rate: int,
        started: float,
        audio_sent: bool,
    ) -> StepFunStreamResult:
        partials: list[ASRPartialEvent] = []
        final_text = ""
        sequence = 0
        while True:
            remaining = self._remaining(started)
            if remaining <= 0:
                raise ASRDeadlineExceeded()
            try:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=min(self._settings.idle_timeout_s, remaining),
                )
            except TimeoutError as error:
                raise ASRDeadlineExceeded() from error
            try:
                event = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise ASRProviderProtocolError() from error
            if not isinstance(event, dict):
                raise ASRProviderProtocolError()
            event_type = str(event.get("type", ""))
            if event_type == "error":
                if audio_sent:
                    raise ASRProviderMidstreamError()
                raise ASRProviderTransientError()
            if event_type.endswith(".delta"):
                partials.append(
                    ASRPartialEvent(
                        request_id=request_id,
                        sequence=sequence,
                        text=_event_text(event),
                        correction_tail=str(event.get("stash") or ""),
                    )
                )
                sequence += 1
            elif event_type.endswith(".done") or event_type.endswith(".completed"):
                final_text = _event_text(event)
                return StepFunStreamResult(
                    final=_final_result(final_text, audio_bytes, sample_rate),
                    partial_events=tuple(partials),
                )

    async def _send_json(self, websocket: Any, payload: dict[str, Any], started: float) -> None:
        await self._wait_with_deadline(
            websocket.send(json.dumps(payload)),
            started,
        )

    async def _put_with_deadline(
        self,
        queue: asyncio.Queue[bytes | None],
        item: bytes | None,
        started: float,
    ) -> None:
        await self._wait_with_deadline(queue.put(item), started)

    async def _wait_with_deadline(self, awaitable: Any, started: float) -> Any:
        remaining = self._remaining(started)
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise ASRDeadlineExceeded()
        return await asyncio.wait_for(awaitable, timeout=remaining)

    def _remaining(self, started: float) -> float:
        return self._settings.max_duration_s - (self._clock() - started)


__all__ = [
    "StepFunSSEOneShotSTT",
    "StepFunSettings",
    "StepFunStreamResult",
    "StepFunWebSocketStreamSTT",
]
