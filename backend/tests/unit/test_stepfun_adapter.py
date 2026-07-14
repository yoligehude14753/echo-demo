"""StepFun capability adapter contract tests using protocol fakes only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from app.adapters.stt.errors import (
    ASRDeadlineExceeded,
    ASRProviderMidstreamError,
    ASRProviderRateLimited,
    ASRProviderTransientError,
)
from app.adapters.stt.stepfun import (
    StepFunSettings,
    StepFunSSEOneShotSTT,
    StepFunWebSocketStreamSTT,
)

VALID_AUDIO = b"\x01\x00" * 80


class FakeSSEResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.lines = lines
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("provider status")

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self.lines:
            yield line


class FakeSSEStream:
    def __init__(self, response: FakeSSEResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeSSEResponse:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeHTTPClient:
    def __init__(self, response: FakeSSEResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self) -> FakeHTTPClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> FakeSSEStream:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeSSEStream(self.response)


class FakeWebSocket:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = [json.dumps(event) for event in events]
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.events:
            raise RuntimeError("unexpected recv")
        return self.events.pop(0)


class FakeWebSocketContext:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


def settings() -> StepFunSettings:
    return StepFunSettings(
        api_key="fixture-only",
        sse_url="https://provider.invalid/sse",
        websocket_url="wss://provider.invalid/stream",
        sse_model="stepaudio-2.5-asr",
        websocket_model="stepaudio-2.5-asr-stream",
        timeout_s=1.0,
        idle_timeout_s=0.2,
        max_duration_s=1.0,
        max_sessions=1,
        send_queue_size=1,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sse_one_shot_is_distinct_and_returns_final_plus_typed_partials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeSSEResponse(
        [
            "event: transcript.text.delta",
            'data: {"text":"partial"}',
            "",
            "event: transcript.text.done",
            'data: {"text":"final"}',
            "",
        ]
    )
    client = FakeHTTPClient(response)
    monkeypatch.setattr("app.adapters.stt.stepfun.httpx.AsyncClient", lambda **_kwargs: client)

    adapter = StepFunSSEOneShotSTT(settings())
    result = await adapter.stream(VALID_AUDIO, sample_rate=16_000, request_id="req-sse")

    assert adapter.transport == "sse_one_shot"
    assert result.final.segments[0].text == "final"
    assert result.partial_events[0].text == "partial"
    assert client.calls[0]["method"] == "POST"
    assert client.calls[0]["url"] == "https://provider.invalid/sse"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer fixture-only"
    assert client.calls[0]["headers"]["Content-Type"] == "application/json"
    assert client.calls[0]["headers"]["Accept"] == "text/event-stream"
    payload = client.calls[0]["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "stepaudio-2.5-asr"
    assert payload["audio"]["format"] == "pcm"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_stream_has_session_admission_and_cumulative_delta_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeWebSocket(
        [
            {"type": "session.created"},
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": "cumulative",
                "stash": "tail-correction",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "final",
            },
        ]
    )
    monkeypatch.setattr(
        "app.adapters.stt.stepfun.websockets.connect",
        lambda *_args, **_kwargs: FakeWebSocketContext(websocket),
    )

    adapter = StepFunWebSocketStreamSTT(settings())
    result = await adapter.stream([VALID_AUDIO], sample_rate=16_000, request_id="req-ws")

    assert adapter.transport == "websocket_stream"
    assert result.final.segments[0].text == "final"
    assert result.partial_events[0].text == "cumulative"
    assert result.partial_events[0].correction_tail == "tail-correction"
    sent_types = [json.loads(message).get("type") for message in websocket.sent]
    assert sent_types[0] == "session.update"
    assert "input_audio_buffer.append" in sent_types
    assert sent_types[-1] == "input_audio_buffer.commit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_midstream_error_is_fail_closed_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeWebSocket(
        [
            {"type": "session.created"},
            {"type": "error", "error": {"code": "internal_error"}},
        ]
    )
    monkeypatch.setattr(
        "app.adapters.stt.stepfun.websockets.connect",
        lambda *_args, **_kwargs: FakeWebSocketContext(websocket),
    )
    adapter = StepFunWebSocketStreamSTT(settings())

    with pytest.raises(ASRProviderMidstreamError):
        await adapter.stream([VALID_AUDIO], sample_rate=16_000, request_id="req-midstream")


@pytest.mark.unit
def test_sse_provider_429_is_retryable_and_retry_after_is_bounded() -> None:
    response = FakeSSEResponse([], status_code=429, headers={"Retry-After": "999999"})

    with pytest.raises(ASRProviderRateLimited) as error:
        StepFunSSEOneShotSTT._raise_http_status(response)

    assert error.value.machine_code == "provider_rate_limited"
    assert error.value.status_code == 503
    assert error.value.retryable is True
    assert error.value.retry_after_s == 60.0
    assert "stepfun" not in error.value.safe_detail.lower()
    assert "provider status" not in error.value.safe_detail.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_deadline_boundaries_do_not_send_after_expiry() -> None:
    websocket = FakeWebSocket([])
    expired = StepFunWebSocketStreamSTT(settings(), clock=lambda: 11.0)
    with pytest.raises(ASRDeadlineExceeded):
        await expired._send_json(websocket, {"type": "must-not-send"}, started=10.0)
    assert websocket.sent == []

    past = StepFunWebSocketStreamSTT(settings(), clock=lambda: 10.0)
    with pytest.raises(ASRDeadlineExceeded):
        await past._send_json(websocket, {"type": "must-not-send"}, started=9.0)
    assert websocket.sent == []

    normal = StepFunWebSocketStreamSTT(settings(), clock=lambda: 10.5)
    await normal._send_json(websocket, {"type": "send-ok"}, started=10.0)
    assert json.loads(websocket.sent[-1])["type"] == "send-ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_deadline_failure_releases_session_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StepFunWebSocketStreamSTT(settings(), clock=lambda: 10.0)

    def connect(*_args: object, **_kwargs: object) -> FakeWebSocketContext:
        raise RuntimeError("connect failed")

    monkeypatch.setattr("app.adapters.stt.stepfun.websockets.connect", connect)
    with pytest.raises(ASRProviderTransientError):
        await adapter.stream([VALID_AUDIO], sample_rate=16_000)
    assert adapter._session_slots._value == 1


@pytest.mark.unit
def test_stepfun_transport_limits_are_bounded() -> None:
    with pytest.raises(ValueError):
        StepFunSettings(api_key="fixture-only", max_sessions=0)
    with pytest.raises(ValueError):
        StepFunSettings(api_key="fixture-only", send_queue_size=0)
