"""HTTP API: /chat — 流式问答端点。

POST /chat
  body: { "question": str, "model": "MAIN" | "FAST" | <实际模型名> | null }
  resp: text/event-stream（SSE，逐 token 推送，结束发 [DONE]）
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters.llm import LLMError, OpenAICompatibleLLM
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.use_cases.ask_question import ask_question

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    question: str
    model: str | None = None


_llm_singleton: OpenAICompatibleLLM | None = None


def get_llm(settings: Settings = Depends(get_settings)) -> LLMPort:
    """LLM 单例依赖（按需懒加载，生命周期由 lifespan 关闭）。"""
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is None:
        _llm_singleton = OpenAICompatibleLLM(settings)
    return _llm_singleton


async def aclose_llm_singleton() -> None:
    """供 main.py lifespan 调用。"""
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is not None:
        await _llm_singleton.aclose()
        _llm_singleton = None


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
        err = json.dumps({"error": str(e)}, ensure_ascii=False)
        yield f"data: {err}\n\n".encode()
        yield b"data: [DONE]\n\n"


@router.post("/chat")
async def chat_endpoint(
    body: ChatRequest,
    llm: LLMPort = Depends(get_llm),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")
    model_arg = _resolve_model_alias(body.model, settings)
    stream = ask_question(llm, body.question, model=model_arg)
    return StreamingResponse(_sse(stream), media_type="text/event-stream")
