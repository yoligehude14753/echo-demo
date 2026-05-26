"""内存事件总线：每个订阅者一个 asyncio.Queue。

设计要点：
- ``publish`` fan-out 到所有订阅者，对慢消费者用 ``put_nowait`` + ``drop`` 防止阻塞
- 队列容量上限避免内存爆掉；溢出时丢弃最旧（推送 ``error`` 提示）
- 单进程范围（多副本时换 Redis pub/sub，由 Port 隔离）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from app.schemas.events import EchoEvent

logger = logging.getLogger(__name__)


class InMemoryEventBus:
    """实现 ports.event_bus.EventBusPort。"""

    def __init__(
        self,
        *,
        per_subscriber_queue: int = 256,
        replay_buffer: int = 200,
    ) -> None:
        self._subscribers: set[asyncio.Queue[EchoEvent]] = set()
        self._lock = asyncio.Lock()
        self._seq = 0
        self._cap = per_subscriber_queue
        self._history: list[EchoEvent] = []
        self._replay_cap = replay_buffer

    async def publish(self, event: EchoEvent) -> None:
        async with self._lock:
            self._seq += 1
            evt = event.model_copy(update={"seq": self._seq})
            self._history.append(evt)
            if len(self._history) > self._replay_cap:
                self._history = self._history[-self._replay_cap :]
            stale: list[asyncio.Queue[EchoEvent]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    logger.warning("event bus subscriber queue full, dropping subscriber")
                    stale.append(q)
            for q in stale:
                self._subscribers.discard(q)

    async def subscribe(self) -> AsyncIterator[EchoEvent]:
        """新订阅者先补发 history 内的事件，再开始接收实时事件。

        Demo 友好：UI 后开也能看到刚发生的会议；生产环境若换 Redis 则用 stream id 续传。
        """
        q: asyncio.Queue[EchoEvent] = asyncio.Queue(maxsize=self._cap)
        async with self._lock:
            replay = list(self._history)
            self._subscribers.add(q)
        for evt in replay:
            yield evt
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def aclose(self) -> None:
        """主动关闭所有订阅者（lifespan shutdown）。"""
        async with self._lock:
            for q in self._subscribers:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(
                        EchoEvent(type="error", payload={"reason": "server shutting down"})
                    )
            self._subscribers.clear()


__all__ = ["InMemoryEventBus"]
