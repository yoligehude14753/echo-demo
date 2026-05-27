"""SpeakUseCase 单测：合成 PCM + 推 WS 事件。"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.events import EchoEvent
from app.use_cases.speak import SpeakUseCase


class FakeTTS:
    def __init__(self, audio: bytes = b"PCM_BYTES") -> None:
        self._audio = audio
        self.calls: list[tuple[str, str | None]] = []

    async def synthesize(
        self, text: str, *, voice: str | None = None, sample_rate: int = 16_000
    ) -> bytes:
        self.calls.append((text, voice))
        return self._audio


class FakeBus:
    def __init__(self) -> None:
        self.events: list[EchoEvent] = []

    async def publish(self, ev: EchoEvent) -> None:
        self.events.append(ev)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_returns_pcm() -> None:
    tts = FakeTTS(b"hello-pcm")
    speak = SpeakUseCase(tts=tts, event_bus=None)  # type: ignore[arg-type]
    out = await speak.synthesize("你好")
    assert out == b"hello-pcm"
    assert tts.calls == [("你好", None)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_empty_returns_empty_bytes() -> None:
    tts = FakeTTS()
    speak = SpeakUseCase(tts=tts, event_bus=None)  # type: ignore[arg-type]
    out = await speak.synthesize("   ")
    assert out == b""
    assert tts.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suggest_publishes_event() -> None:
    tts = FakeTTS()
    bus = FakeBus()
    speak = SpeakUseCase(tts=tts, event_bus=bus)  # type: ignore[arg-type]
    await speak.suggest("会议已结束", kind="minutes", meeting_id="m1")
    assert len(bus.events) == 1
    e = bus.events[0]
    assert e.type == "tts.suggested"
    assert e.meeting_id == "m1"
    assert e.payload["text"] == "会议已结束"
    assert e.payload["kind"] == "minutes"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suggest_empty_text_no_event() -> None:
    bus = FakeBus()
    speak = SpeakUseCase(tts=FakeTTS(), event_bus=bus)  # type: ignore[arg-type]
    await speak.suggest("   ", kind="chat")
    assert bus.events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suggest_swallows_bus_errors() -> None:
    class BrokenBus:
        async def publish(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("bus down")

    speak = SpeakUseCase(tts=FakeTTS(), event_bus=BrokenBus())  # type: ignore[arg-type]
    # 不应抛错（业务层不能因为 TTS 提示失败而崩）
    await speak.suggest("hi")
