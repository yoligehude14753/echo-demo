"""HTTP API: /chat — 流式问答端点。

POST /chat
  body: { "question": str, "model": "MAIN" | "FAST" | <实际模型名> | null }
  resp: text/event-stream（SSE，逐 token 推送，结束发 [DONE]）
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.llm import LLMError
from app.api.deps import aclose_llm_singleton, get_llm_singleton
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.use_cases.ask_question import answer_question_once

router = APIRouter(tags=["chat"])
logger = logging.getLogger("echodesk.chat")
MAX_CHAT_QUESTION_CHARS = 32_000
MAX_MODEL_NAME_CHARS = 256

__all__ = ["aclose_llm_singleton", "router"]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_CHAT_QUESTION_CHARS)
    model: str | None = Field(default=None, max_length=MAX_MODEL_NAME_CHARS)


def _generation_error_frame() -> bytes:
    payload = json.dumps(
        {
            "type": "error",
            "code": "answer_generation_failed",
            "error": "暂时无法回复，请稍后重试",
        },
        ensure_ascii=False,
    )
    return f"event: error\ndata: {payload}\n\n".encode()


def _resolve_model_alias(alias: str | None, settings: Settings) -> str | None:
    if alias is None:
        return None
    up = alias.upper()
    if up == "MAIN":
        return settings.llm_main_model
    if up == "FAST":
        return settings.llm_fast_model
    return alias


async def _sse(stream: AsyncIterator[str]) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream:
            payload = json.dumps({"delta": chunk}, ensure_ascii=False)
            yield f"data: {payload}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except LLMError as e:
        logger.warning("chat stream generation failed", exc_info=e)
        yield _generation_error_frame()


async def _single_answer_sse(
    llm: LLMPort,
    question: str,
    *,
    model: str | None,
) -> AsyncIterator[bytes]:
    try:
        resp = await answer_question_once(llm, question, model=model)
        if resp.content:
            payload = json.dumps({"delta": resp.content}, ensure_ascii=False)
            yield f"data: {payload}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except LLMError as e:
        logger.warning("chat answer generation failed", exc_info=e)
        yield _generation_error_frame()


@router.post("/chat")
async def chat_endpoint(
    body: ChatRequest,
    llm: LLMPort = Depends(get_llm_singleton),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")
    model_arg = _resolve_model_alias(body.model, settings)
    return StreamingResponse(
        _single_answer_sse(llm, body.question, model=model_arg),
        media_type="text/event-stream",
    )
