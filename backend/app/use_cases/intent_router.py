"""use_case: route_intent — 调 IntentRouterPort 把文本路由到意图。"""

from __future__ import annotations

from app.ports.intent import IntentRouterPort
from app.schemas.intent import IntentResult


async def route_intent(
    *,
    router: IntentRouterPort,
    text: str,
    current_meeting_id: str | None = None,
    available_context: list[str] | None = None,
) -> IntentResult:
    return await router.route(
        text,
        current_meeting_id=current_meeting_id,
        available_context=available_context,
    )
