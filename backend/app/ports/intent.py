"""Intent Router Port：把用户文本路由到 9 类意图之一。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.intent import IntentResult


@runtime_checkable
class IntentRouterPort(Protocol):
    async def route(
        self,
        text: str,
        *,
        current_meeting_id: str | None = None,
    ) -> IntentResult: ...
