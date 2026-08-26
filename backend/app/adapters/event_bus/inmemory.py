"""有界、principal-scoped 的进程内事件流。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.schemas.events import EchoEvent
from app.security.context import current_principal

logger = logging.getLogger(__name__)
ScopeKey = tuple[str, str]


class SlowConsumerError(RuntimeError):
    """Subscriber queue overflowed and the connection must restart from a fence."""


class EventStreamCapacityExceeded(RuntimeError):
    """The bounded scope stream capacity or fair admission queue is full."""


@dataclass(frozen=True, slots=True)
class StreamState:
    epoch: str
    max_seq: int
    oldest_seq: int


@dataclass(slots=True)
class FencedEventSubscription:
    """Atomically registered replay + live-event cursor for one principal stream."""

    state: StreamState
    gap_reason: str | None
    requested_seq: int
    resume_seq: int
    _bus: InMemoryEventBus
    _scope: ScopeKey
    _queue: asyncio.Queue[EchoEvent]
    _replay: list[EchoEvent]
    _replay_index: int = 0
    _closed: bool = False
    _slow_consumer_seen: bool = False

    def __aiter__(self) -> FencedEventSubscription:
        return self

    async def __anext__(self) -> EchoEvent:
        if self._closed:
            raise StopAsyncIteration
        if self._slow_consumer_seen:
            raise SlowConsumerError("subscriber queue overflowed")
        if self._replay_index < len(self._replay):
            event = self._replay[self._replay_index]
            self._replay_index += 1
        else:
            event = await self._queue.get()
        if event.type == "error" and event.payload.get("reason") == "slow_consumer":
            self._slow_consumer_seen = True
        return event

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._bus._close_fenced_subscription(self._scope, self._queue)


@dataclass(slots=True)
class _ScopeStream:
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = 0
    history: list[EchoEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[EchoEvent]] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)


class InMemoryEventBus:
    """每个 owner 独立 epoch/seq/replay，避免租户之间共享流坐标。"""

    def __init__(
        self,
        *,
        per_subscriber_queue: int = 256,
        replay_buffer: int = 200,
        max_scope_streams: int = 512,
        max_admission_waiters: int = 128,
        admission_wait_timeout_s: float = 2.0,
    ) -> None:
        if (
            per_subscriber_queue < 1
            or replay_buffer < 1
            or max_scope_streams < 1
            or max_admission_waiters < 1
            or admission_wait_timeout_s <= 0
        ):
            raise ValueError("event bus bounds must be positive")
        self._lock = asyncio.Lock()
        self._cap = per_subscriber_queue
        self._replay_cap = replay_buffer
        self._max_scope_streams = max_scope_streams
        self._max_admission_waiters = max_admission_waiters
        self._admission_wait_timeout_s = admission_wait_timeout_s
        self._streams: OrderedDict[ScopeKey, _ScopeStream] = OrderedDict()
        self._admission_waiters: OrderedDict[ScopeKey, asyncio.Future[None]] = OrderedDict()
        self._admission_grants: dict[ScopeKey, asyncio.Future[None]] = {}
        self._admission_changed = asyncio.Event()

    def _signal_admission_changed_locked(self) -> None:
        changed = self._admission_changed
        self._admission_changed = asyncio.Event()
        changed.set()

    @staticmethod
    def _current_scope() -> ScopeKey:
        principal = current_principal()
        return principal.tenant_id, principal.owner_id

    def _stream_locked(self, scope: ScopeKey) -> _ScopeStream:
        stream = self._streams.pop(scope, None)
        if stream is None:
            self._make_room_locked()
            stream = _ScopeStream()
        stream.last_used = time.monotonic()
        self._streams[scope] = stream
        return stream

    def _make_room_locked(self) -> None:
        if len(self._streams) < self._max_scope_streams:
            return
        for scope, stream in self._streams.items():
            if not stream.subscribers and scope not in self._admission_grants:
                self._streams.pop(scope)
                return
        raise EventStreamCapacityExceeded("all scoped event streams have subscribers")

    def _has_scope_slot_locked(self) -> bool:
        if len(self._streams) < self._max_scope_streams:
            return True
        return any(
            not stream.subscribers and scope not in self._admission_grants
            for scope, stream in self._streams.items()
        )

    def _grant_admission_waiters_locked(self) -> None:
        while self._admission_waiters:
            scope = next(iter(self._admission_waiters))
            if scope not in self._streams and not self._has_scope_slot_locked():
                return
            future = self._admission_waiters.pop(scope)
            self._signal_admission_changed_locked()
            if future.cancelled():
                continue
            self._stream_locked(scope)
            self._admission_grants[scope] = future
            if not future.done():
                future.set_result(None)

    async def _cancel_scope_admission(
        self,
        scope: ScopeKey,
        future: asyncio.Future[None],
    ) -> None:
        async with self._lock:
            if self._admission_waiters.get(scope) is future:
                self._admission_waiters.pop(scope, None)
                self._signal_admission_changed_locked()
            if self._admission_grants.get(scope) is future:
                self._admission_grants.pop(scope, None)
            self._grant_admission_waiters_locked()

    async def _await_scope_admission(
        self,
        scope: ScopeKey,
    ) -> asyncio.Future[None] | None:
        async with self._lock:
            if scope in self._streams:
                return None
            if scope in self._admission_waiters:
                raise EventStreamCapacityExceeded("scope already has an admission waiter")
            if len(self._admission_waiters) >= self._max_admission_waiters:
                raise EventStreamCapacityExceeded("event stream admission queue is full")
            future = asyncio.get_running_loop().create_future()
            self._admission_waiters[scope] = future
            self._signal_admission_changed_locked()
            self._grant_admission_waiters_locked()
        try:
            await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._admission_wait_timeout_s,
            )
        except TimeoutError as exc:
            await self._cancel_scope_admission(scope, future)
            raise EventStreamCapacityExceeded("event stream admission timed out") from exc
        except BaseException:
            await self._cancel_scope_admission(scope, future)
            raise
        return future

    def _complete_scope_admission_locked(
        self,
        scope: ScopeKey,
        future: asyncio.Future[None] | None,
    ) -> None:
        if future is not None and self._admission_grants.get(scope) is future:
            self._admission_grants.pop(scope, None)

    @staticmethod
    def _state(stream: _ScopeStream) -> StreamState:
        oldest = stream.history[0].seq if stream.history else 0
        return StreamState(stream.epoch, stream.seq, oldest)

    async def publish(self, event: EchoEvent) -> None:
        principal = current_principal()
        await self.publish_to((principal.tenant_id, principal.owner_id), event)

    async def publish_to(self, scope: ScopeKey, event: EchoEvent) -> None:
        """Publish using an authoritative server-side scope.

        Background outbox consumers do not inherit the request ContextVar that
        created a row.  They must route from the committed row itself instead
        of whichever principal happens to be active while the poller runs.
        """

        tenant_id, owner_id = scope
        async with self._lock:
            self._grant_admission_waiters_locked()
            stream = self._stream_locked(scope)
            stream.seq += 1
            committed = event.model_copy(
                update={
                    "seq": stream.seq,
                    "stream_epoch": stream.epoch,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                }
            )
            stream.history.append(committed)
            if len(stream.history) > self._replay_cap:
                stream.history = stream.history[-self._replay_cap :]
            self._fan_out_locked(stream, committed)

    @staticmethod
    def _slow_consumer_frame(stream: _ScopeStream) -> EchoEvent:
        return EchoEvent(
            type="error",
            stream_epoch=stream.epoch,
            payload={
                "reason": "slow_consumer",
                "reconnect": True,
                "close_code": 4409,
                "fence_seq": stream.seq,
            },
        )

    def _fan_out_locked(self, stream: _ScopeStream, event: EchoEvent) -> None:
        stale: list[asyncio.Queue[EchoEvent]] = []
        for queue in stream.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("event bus subscriber queue full; fencing slow consumer")
                while not queue.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(self._slow_consumer_frame(stream))
                stale.append(queue)
        for queue in stale:
            stream.subscribers.discard(queue)

    async def subscribe(self, *, since_seq: int = 0) -> AsyncIterator[EchoEvent]:
        queue: asyncio.Queue[EchoEvent] = asyncio.Queue(maxsize=self._cap)
        scope = self._current_scope()
        admission = await self._await_scope_admission(scope)
        try:
            async with self._lock:
                stream = self._stream_locked(scope)
                replay = [event for event in stream.history if event.seq > since_seq]
                stream.subscribers.add(queue)
                self._complete_scope_admission_locked(scope, admission)
                self._grant_admission_waiters_locked()
        except BaseException:
            if admission is not None:
                await self._cancel_scope_admission(scope, admission)
            raise
        for event in replay:
            yield event
        try:
            while True:
                event = await queue.get()
                yield event
                if event.type == "error" and event.payload.get("reason") == "slow_consumer":
                    raise SlowConsumerError("subscriber queue overflowed")
        finally:
            async with self._lock:
                cleanup_stream = self._streams.get(scope)
                if cleanup_stream is not None:
                    cleanup_stream.subscribers.discard(queue)
                self._grant_admission_waiters_locked()

    @staticmethod
    def _gap_reason_for_state(
        state: StreamState,
        *,
        last_seq: int,
        stream_epoch: str | None,
    ) -> str | None:
        if stream_epoch and stream_epoch != state.epoch:
            return "stream_epoch_changed"
        if last_seq > state.max_seq:
            return "client_ahead_of_stream"
        if state.oldest_seq and last_seq + 1 < state.oldest_seq:
            return "history_expired"
        if last_seq < state.max_seq and not state.oldest_seq:
            return "history_unavailable"
        return None

    async def open_fenced_subscription(
        self,
        *,
        last_seq: int,
        stream_epoch: str | None,
        start_at_latest: bool = False,
    ) -> FencedEventSubscription:
        """Fence replay inspection and subscriber registration in one lock.

        A WebSocket must register before it emits any handshake frame. Events
        published while those frames are in flight then enter this bounded
        queue instead of falling into the former check-before-subscribe gap.
        """

        scope = self._current_scope()
        queue: asyncio.Queue[EchoEvent] = asyncio.Queue(maxsize=self._cap)
        admission = await self._await_scope_admission(scope)
        try:
            async with self._lock:
                stream = self._stream_locked(scope)
                state = self._state(stream)
                requested_seq = state.max_seq if start_at_latest else max(0, last_seq)
                gap_reason = self._gap_reason_for_state(
                    state,
                    last_seq=requested_seq,
                    stream_epoch=stream_epoch,
                )
                resume_seq = state.max_seq if gap_reason is not None else requested_seq
                replay = [event for event in stream.history if event.seq > resume_seq]
                stream.subscribers.add(queue)
                self._complete_scope_admission_locked(scope, admission)
                self._grant_admission_waiters_locked()
        except BaseException:
            if admission is not None:
                await self._cancel_scope_admission(scope, admission)
            raise
        return FencedEventSubscription(
            state=state,
            gap_reason=gap_reason,
            requested_seq=requested_seq,
            resume_seq=resume_seq,
            _bus=self,
            _scope=scope,
            _queue=queue,
            _replay=replay,
        )

    async def _close_fenced_subscription(
        self,
        scope: ScopeKey,
        queue: asyncio.Queue[EchoEvent],
    ) -> None:
        async with self._lock:
            stream = self._streams.get(scope)
            if stream is not None:
                stream.subscribers.discard(queue)
            self._grant_admission_waiters_locked()

    def stream_state_for_current_scope(self) -> StreamState:
        scope = self._current_scope()
        stream = self._streams.get(scope)
        if stream is None:
            stream = self._stream_locked(scope)
        return self._state(stream)

    def recent_events_for_current_scope(self, *, limit: int = 200) -> tuple[EchoEvent, ...]:
        """Return an immutable history snapshot for the active principal only."""

        if limit < 1:
            raise ValueError("event history limit must be positive")
        stream = self._streams.get(self._current_scope())
        if stream is None:
            return ()
        return tuple(stream.history[-limit:])

    def replay_gap_reason(
        self,
        *,
        last_seq: int,
        stream_epoch: str | None,
    ) -> str | None:
        state = self.stream_state_for_current_scope()
        return self._gap_reason_for_state(
            state,
            last_seq=last_seq,
            stream_epoch=stream_epoch,
        )

    @property
    def max_seq(self) -> int:
        return self.stream_state_for_current_scope().max_seq

    @property
    def stream_epoch(self) -> str:
        return self.stream_state_for_current_scope().epoch

    @property
    def oldest_history_seq(self) -> int:
        return self.stream_state_for_current_scope().oldest_seq

    def oldest_history_seq_for_current_scope(self) -> int:
        return self.oldest_history_seq

    def subscriber_count(self) -> int:
        return sum(len(stream.subscribers) for stream in self._streams.values())

    @property
    def admission_waiter_count(self) -> int:
        return len(self._admission_waiters)

    async def wait_for_admission_waiter_count(self, expected: int) -> None:
        while True:
            async with self._lock:
                if len(self._admission_waiters) == expected:
                    return
                changed = self._admission_changed
            await changed.wait()

    @property
    def scope_stream_count(self) -> int:
        return len(self._streams)

    async def aclose(self) -> None:
        async with self._lock:
            for future in self._admission_waiters.values():
                future.cancel()
            self._admission_waiters.clear()
            self._signal_admission_changed_locked()
            self._admission_grants.clear()
            for stream in self._streams.values():
                for queue in stream.subscribers:
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(
                            EchoEvent(
                                type="error",
                                stream_epoch=stream.epoch,
                                payload={"reason": "server_shutting_down"},
                            )
                        )
                stream.subscribers.clear()
            self._streams.clear()


__all__ = [
    "EventStreamCapacityExceeded",
    "FencedEventSubscription",
    "InMemoryEventBus",
    "SlowConsumerError",
    "StreamState",
]
