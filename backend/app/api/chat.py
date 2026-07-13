"""HTTP API: /chat — 流式问答端点。

POST /chat
  body: { "question": str, "model": "MAIN" | "FAST" | <实际模型名> | null }
  resp: text/event-stream（SSE，逐 token 推送，结束发 [DONE]）
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.llm import LLMError
from app.api.deps import aclose_llm_singleton, get_llm_singleton
from app.api.memory import get_memory_dependency
from app.config import Settings, get_settings
from app.memory import MemoryScope, MemoryService
from app.memory.models import RecallResult
from app.ports.llm import LLMPort
from app.security.context import current_principal
from app.use_cases.ask_question import answer_question_once

router = APIRouter(tags=["chat"])
logger = logging.getLogger("echodesk.chat")
MAX_CHAT_QUESTION_CHARS = 32_000
MAX_MODEL_NAME_CHARS = 256

__all__ = ["aclose_llm_singleton", "router"]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_CHAT_QUESTION_CHARS)
    model: str | None = Field(default=None, max_length=MAX_MODEL_NAME_CHARS)
    conversation_id: str = Field(default="default", min_length=1, max_length=128)
    message_id: str | None = Field(default=None, max_length=128)


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


def _memory_event(event: str, payload: dict[str, object]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n".encode()


def _source_title(level: str, kind: str, metadata: dict[str, object]) -> str:
    for key in ("title", "meeting_title", "artifact_name", "name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    level_names = {"L0": "当前对话", "L1": "历史会议与产物", "L2": "长期记忆", "L3": "个人配置"}
    kind_names = {
        "meeting_segment": "会议原文",
        "meeting_minutes": "会议纪要",
        "ambient_segment": "环境记录",
        "artifact": "工作产物",
        "fact": "事实记忆",
        "preference": "偏好记忆",
        "decision": "决策记忆",
        "todo": "待办记忆",
        "relationship": "人物关系",
    }
    return kind_names.get(kind) or level_names.get(level, "相关信息")


def _memory_sources(result: RecallResult) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    for index, match in enumerate(result.matches, start=1):
        candidate = match.candidate
        metadata = dict(candidate.metadata)
        sources.append(
            {
                "index": index,
                "candidate_id": candidate.candidate_id,
                "memory_id": candidate.memory_id,
                "level": candidate.level,
                "kind": candidate.kind,
                "title": _source_title(candidate.level, candidate.kind, metadata),
                "excerpt": candidate.content[:1_500],
                "source_ref": candidate.source_ref,
                "occurred_at": candidate.occurred_at.isoformat(),
                "confidence": candidate.confidence,
                "relevance": match.relevance,
                "score": match.score,
                "relation": match.relation,
                "manageable": candidate.level in {"L2", "L3"},
                "metadata": metadata,
            }
        )
    return sources


async def _memory_answer_sse(
    llm: LLMPort,
    memory: MemoryService,
    settings: Settings,
    scope: MemoryScope,
    question: str,
    *,
    conversation_id: str,
    turn_id: str,
    model: str | None,
) -> AsyncIterator[bytes]:
    overall_started = perf_counter()
    yield _memory_event(
        "memory.status",
        {
            "type": "memory.status",
            "state": "recalling",
            "label": "正在关联历史信息",
            "model_display_name": settings.llm_fast_display_name,
            "conversation_id": conversation_id,
            "message_id": turn_id,
        },
    )
    recall_started = perf_counter()
    try:
        history, recall = await asyncio.gather(
            memory.history_messages(scope, conversation_id),
            memory.recall(scope, question, conversation_id=conversation_id),
        )
    except Exception as error:
        logger.warning("chat memory recall failed; answer continues", exc_info=error)
        history = []
        recall = RecallResult(query=question)
    sources = _memory_sources(recall)
    logger.info(
        "chat latency memory_ms=%.1f sources=%d",
        (perf_counter() - recall_started) * 1000,
        len(sources),
    )
    yield _memory_event(
        "memory.sources",
        {
            "type": "memory.sources",
            "state": "found" if sources else "empty",
            "label": f"找到 {len(sources)} 条相关信息" if sources else "未找到相关历史信息",
            "model_display_name": settings.llm_fast_display_name,
            "latency_ms": recall.latency_ms,
            "sources": sources,
            "conversation_id": conversation_id,
            "message_id": turn_id,
        },
    )
    try:
        llm_started = perf_counter()
        response = await answer_question_once(
            llm,
            question,
            history=history,
            memory_context=recall.prompt_context(),
            model=model,
        )
        logger.info("chat latency llm_ms=%.1f", (perf_counter() - llm_started) * 1000)
        if response.content:
            yield _memory_event(
                "answer.delta",
                {
                    "type": "answer.delta",
                    "delta": response.content,
                    "conversation_id": conversation_id,
                    "message_id": turn_id,
                },
            )
        try:
            await memory.remember_chat_turn(
                scope,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_text=question,
                assistant_text=response.content,
            )
        except Exception as error:
            logger.warning("chat memory write failed after answer", exc_info=error)
        yield _memory_event(
            "memory.status",
            {
                "type": "memory.status",
                "state": "complete",
                "label": "关联完成",
                "source_count": len(sources),
                "model_display_name": settings.llm_fast_display_name,
                "conversation_id": conversation_id,
                "message_id": turn_id,
            },
        )
        yield b"data: [DONE]\n\n"
        logger.info("chat latency render_ms=%.1f", (perf_counter() - overall_started) * 1000)
    except LLMError as error:
        logger.warning("chat answer generation failed", exc_info=error)
        yield _generation_error_frame()


@router.post("/chat")
async def chat_endpoint(
    body: ChatRequest,
    llm: LLMPort = Depends(get_llm_singleton),
    settings: Settings = Depends(get_settings),
    memory: MemoryService = Depends(get_memory_dependency),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")
    model_arg = _resolve_model_alias(body.model, settings)
    scope = MemoryScope.from_principal(current_principal())
    return StreamingResponse(
        _memory_answer_sse(
            llm,
            memory,
            settings,
            scope,
            body.question,
            conversation_id=body.conversation_id,
            turn_id=body.message_id or f"turn_{uuid4().hex}",
            model=model_arg,
        )
        if settings.memory_enabled
        else _single_answer_sse(llm, body.question, model=model_arg),
        media_type="text/event-stream",
    )
