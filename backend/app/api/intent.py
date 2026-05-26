"""HTTP API: 意图路由 /intent/route

POST /intent/route
  body: {"text": "...", "current_meeting_id": str?}
  resp: IntentResult { kind, confidence, params, rationale }
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.adapters.intent.llm_router import LLMIntentRouter
from app.api.deps import get_llm_singleton as get_llm
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.schemas.intent import IntentRequest, IntentResult
from app.use_cases.intent_router import route_intent

router = APIRouter(tags=["intent"])

_router_singleton: LLMIntentRouter | None = None


def get_intent_router(
    settings: Settings = Depends(get_settings),
    llm: LLMPort = Depends(get_llm),
) -> LLMIntentRouter:
    global _router_singleton  # noqa: PLW0603
    if _router_singleton is None:
        _router_singleton = LLMIntentRouter(settings, llm)
    return _router_singleton


def reset_intent_router() -> None:
    global _router_singleton  # noqa: PLW0603
    _router_singleton = None


@router.post("/intent/route", response_model=IntentResult)
async def route(
    body: IntentRequest,
    intent_router: LLMIntentRouter = Depends(get_intent_router),
) -> IntentResult:
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text empty")
    return await route_intent(
        router=intent_router,
        text=body.text,
        current_meeting_id=body.current_meeting_id,
    )
