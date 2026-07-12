"""内存事件总线单测。"""

from __future__ import annotations

import asyncio

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.schemas.events import EchoEvent


@pytest.mark.asyncio
@pytest.mark.unit
async def test_publish_fanout_to_multiple_subscribers() -> None:
    bus = InMemoryEventBus()
    sub1_events: list[EchoEvent] = []
    sub2_events: list[EchoEvent] = []

    async def _consume(sink: list[EchoEvent], stop_after: int) -> None:
        async for e in bus.subscribe():
            sink.append(e)
            if len(sink) >= stop_after:
                return

    consumer1 = asyncio.create_task(_consume(sub1_events, 2))
    consumer2 = asyncio.create_task(_consume(sub2_events, 2))
    await asyncio.sleep(0.01)

    await bus.publish(EchoEvent(type="meeting.started", meeting_id="m1"))
    await bus.publish(EchoEvent(type="meeting.ended", meeting_id="m1"))
    await asyncio.wait_for(asyncio.gather(consumer1, consumer2), timeout=2.0)

    assert [e.type for e in sub1_events] == ["meeting.started", "meeting.ended"]
    assert [e.type for e in sub2_events] == ["meeting.started", "meeting.ended"]
    assert sub1_events[0].seq == 1 and sub1_events[1].seq == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_same_scope_subscriber_joins_while_first_admission_grant_is_in_flight() -> None:
    class _PausedFirstGrantBus(InMemoryEventBus):
        def __init__(self) -> None:
            super().__init__()
            self.first_grant_ready = asyncio.Event()
            self.release_first_grant = asyncio.Event()
            self._pause_first_grant = True

        async def _await_scope_admission(
            self,
            scope: tuple[str, str],
        ) -> asyncio.Future[None] | None:
            admission = await super()._await_scope_admission(scope)
            if admission is not None and self._pause_first_grant:
                self._pause_first_grant = False
                self.first_grant_ready.set()
                await self.release_first_grant.wait()
            return admission

    bus = _PausedFirstGrantBus()
    first = bus.subscribe()
    second = bus.subscribe()
    first_receive = asyncio.create_task(first.__anext__())
    second_receive: asyncio.Task[EchoEvent] | None = None
    try:
        await asyncio.wait_for(bus.first_grant_ready.wait(), timeout=1.0)
        second_receive = asyncio.create_task(second.__anext__())
        await bus.publish(EchoEvent(type="meeting.started", meeting_id="m1"))
        second_event = await asyncio.wait_for(second_receive, timeout=1.0)
        assert second_event.type == "meeting.started"

        bus.release_first_grant.set()
        first_event = await asyncio.wait_for(first_receive, timeout=1.0)
        assert first_event.type == "meeting.started"
    finally:
        bus.release_first_grant.set()
        for task in (first_receive, second_receive):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            first_receive,
            *(task for task in (second_receive,) if task is not None),
            return_exceptions=True,
        )
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_subscription_count_tracks_active() -> None:
    bus = InMemoryEventBus()
    assert bus.subscriber_count() == 0

    async def _open_and_close() -> None:
        agen = bus.subscribe()
        # 拉一次启动订阅
        recv = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0.01)
        assert bus.subscriber_count() == 1
        await bus.publish(EchoEvent(type="chat.delta"))
        await asyncio.wait_for(recv, timeout=1.0)
        await agen.aclose()

    await _open_and_close()
    await asyncio.sleep(0.01)
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_full_subscriber_is_evicted() -> None:
    bus = InMemoryEventBus(per_subscriber_queue=2)
    agen = bus.subscribe()
    recv = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.01)

    for _ in range(5):
        await bus.publish(EchoEvent(type="chat.delta"))

    await asyncio.wait_for(recv, timeout=1.0)
    await asyncio.sleep(0.01)
    # 第一次拿到的事件之后队列已满，订阅者被踢
    assert bus.subscriber_count() == 0
    await agen.aclose()
