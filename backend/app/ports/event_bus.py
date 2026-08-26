"""事件总线 Port。简单 pub/sub 广播。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.schemas.events import EchoEvent


@runtime_checkable
class EventBusPort(Protocol):
    async def publish(self, event: EchoEvent) -> None: ...

    async def publish_to(self, scope: tuple[str, str], event: EchoEvent) -> None:
        """Server-side projection to an explicit ``(tenant_id, owner_id)`` stream."""
        ...

    def subscribe(self) -> AsyncIterator[EchoEvent]:
        """订阅事件流，新订阅者从订阅时刻开始接收。"""
