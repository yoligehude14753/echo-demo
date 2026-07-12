from __future__ import annotations

import asyncio

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus, SlowConsumerError
from app.schemas.events import EchoEvent
from app.security.context import bind_principal, reset_principal
from app.security.models import Principal


def _principal(name: str) -> Principal:
    return Principal(f"tenant-{name}", f"device-{name}", f"owner-{name}", name, "public")


async def _publish_as(bus: InMemoryEventBus, principal: Principal, text: str) -> None:
    token = bind_principal(principal)
    try:
        await bus.publish(EchoEvent(type="chat.done", payload={"text": text}))
    finally:
        reset_principal(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_principal_has_independent_epoch_sequence_and_replay() -> None:
    bus = InMemoryEventBus()
    first = _principal("a")
    second = _principal("b")
    await _publish_as(bus, first, "a1")
    await _publish_as(bus, first, "a2")
    await _publish_as(bus, second, "b1")

    token = bind_principal(first)
    try:
        state_a = bus.stream_state_for_current_scope()
        stream_a = bus.subscribe(since_seq=1)
        replay_a = await anext(stream_a)
        await stream_a.aclose()
    finally:
        reset_principal(token)
    token = bind_principal(second)
    try:
        state_b = bus.stream_state_for_current_scope()
        stream_b = bus.subscribe(since_seq=0)
        replay_b = await anext(stream_b)
        await stream_b.aclose()
    finally:
        reset_principal(token)

    assert (state_a.max_seq, replay_a.seq, replay_a.payload["text"]) == (2, 2, "a2")
    assert (state_b.max_seq, replay_b.seq, replay_b.payload["text"]) == (1, 1, "b1")
    assert state_a.epoch != state_b.epoch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_publish_to_ignores_mismatched_context_principal() -> None:
    bus = InMemoryEventBus()
    target = _principal("target")
    wrong_context = _principal("wrong")
    token = bind_principal(wrong_context)
    try:
        await bus.publish_to(
            (target.tenant_id, target.owner_id),
            EchoEvent(
                type="workflow.event",
                tenant_id=target.tenant_id,
                owner_id=target.owner_id,
                payload={"run_id": "target-run"},
            ),
        )
        assert bus.stream_state_for_current_scope().max_seq == 0
    finally:
        reset_principal(token)

    token = bind_principal(target)
    try:
        stream = bus.subscribe(since_seq=0)
        received = await anext(stream)
        await stream.aclose()
    finally:
        reset_principal(token)

    assert received.payload["run_id"] == "target-run"
    assert received.tenant_id == target.tenant_id
    assert received.owner_id == target.owner_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_epoch_mismatch_and_expired_history_create_gap_fence() -> None:
    bus = InMemoryEventBus(replay_buffer=2)
    principal = _principal("gap")
    for index in range(4):
        await _publish_as(bus, principal, str(index))
    token = bind_principal(principal)
    try:
        state = bus.stream_state_for_current_scope()
        assert state.oldest_seq == 3
        assert bus.replay_gap_reason(last_seq=0, stream_epoch=state.epoch) == "history_expired"
        assert bus.replay_gap_reason(last_seq=2, stream_epoch=state.epoch) is None
        assert bus.replay_gap_reason(last_seq=1, stream_epoch=state.epoch) == "history_expired"
        assert bus.replay_gap_reason(last_seq=4, stream_epoch="old") == "stream_epoch_changed"
        assert bus.replay_gap_reason(last_seq=99, stream_epoch=state.epoch) == (
            "client_ahead_of_stream"
        )
    finally:
        reset_principal(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fenced_subscription_cannot_lose_events_during_handshake() -> None:
    """Replay snapshot and live registration form one atomic handoff."""

    bus = InMemoryEventBus(replay_buffer=2, per_subscriber_queue=8)
    principal = _principal("handshake-race")
    for index in range(1, 4):
        await _publish_as(bus, principal, f"event-{index}")

    token = bind_principal(principal)
    try:
        state = bus.stream_state_for_current_scope()
        subscription = await bus.open_fenced_subscription(
            last_seq=2,
            stream_epoch=state.epoch,
        )
        assert subscription.gap_reason is None
        assert subscription.state.max_seq == 3

        # Simulate events published while server_hello is blocked on the wire.
        for index in range(4, 7):
            await bus.publish(EchoEvent(type="chat.done", payload={"text": f"event-{index}"}))

        received = [await anext(subscription) for _ in range(4)]
        assert [event.seq for event in received] == [3, 4, 5, 6]
        assert [event.payload["text"] for event in received] == [
            "event-3",
            "event-4",
            "event-5",
            "event-6",
        ]
        await subscription.aclose()
        assert bus.subscriber_count() == 0
    finally:
        reset_principal(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_consumer_gets_fence_frame_then_disconnect_signal() -> None:
    bus = InMemoryEventBus(per_subscriber_queue=1)
    principal = _principal("slow")
    token = bind_principal(principal)
    try:
        stream = bus.subscribe()
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await bus.publish(EchoEvent(type="chat.done"))
        assert (await waiting).seq == 1
        await bus.publish(EchoEvent(type="chat.done"))
        await bus.publish(EchoEvent(type="chat.done"))
        fence = await anext(stream)
        assert fence.payload["reason"] == "slow_consumer"
        assert fence.payload["close_code"] == 4409
        with pytest.raises(SlowConsumerError):
            await anext(stream)
    finally:
        reset_principal(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_stream_registry_evicts_inactive_lru_scope() -> None:
    bus = InMemoryEventBus(max_scope_streams=2)
    principals = [_principal("a"), _principal("b"), _principal("c")]
    for principal in principals:
        await _publish_as(bus, principal, principal.owner_id)
    assert bus.scope_stream_count == 2

    token = bind_principal(principals[0])
    try:
        state = bus.stream_state_for_current_scope()
    finally:
        reset_principal(token)
    assert state.max_seq == 0
