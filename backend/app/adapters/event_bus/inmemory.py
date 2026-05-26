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

    async def subscribe(self, *, since_seq: int = 0) -> AsyncIterator[EchoEvent]:
        """订阅事件流，从 ``since_seq`` 之后开始 replay。

        Demo 友好：UI 后开也能看到刚发生的会议；生产环境若换 Redis 则用 stream id 续传。

        - since_seq=0 → 全量 replay（最多 replay_buffer 条）
        - since_seq > _seq → 客户端 last_seq 跑超了，等价全量 replay
        - since_seq 落在 history 内 → 仅 replay seq > since_seq 的部分
        - since_seq < oldest_seq_in_history → 调用方应识别"history 已淘汰"决定要不要 resync
        """
        q: asyncio.Queue[EchoEvent] = asyncio.Queue(maxsize=self._cap)
        async with self._lock:
            replay = [evt for evt in self._history if evt.seq > since_seq]
            self._subscribers.add(q)
        for evt in replay:
            yield evt
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._subscribers.discard(q)

    @property
    def max_seq(self) -> int:
        return self._seq

    @property
    def oldest_history_seq(self) -> int:
        """history 内最旧事件的 seq；空则 0。"""
        return self._history[0].seq if self._history else 0

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
