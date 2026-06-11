"""HTTP API: agent loop (multi-tool chaining).

POST /agent/run — SSE 流, 让主 LLM 自己串联 rag_search / web_search /
generate_artifact / final_answer。详见 ``use_cases/agent_loop.py``。

SSE event 协议(与 retrieval / artifacts 风格一致):
- event: plan         data: {step, max_steps}
- event: tool_call    data: {name, args, reason, step}
- event: tool_result  data: {name, ok, summary, step}
- event: artifact     data: GeneratedArtifact dict
- event: delta        data: {text}
- event: final        data: {answer, artifact_ids}
- event: error        data: {error, stage}
- event: done         data: {}
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.artifacts import get_skill
from app.api.deps import get_llm_singleton as get_llm
from app.api.retrieval import get_rag, get_web
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.skill import SkillExecutorPort
from app.ports.web_search import WebSearchPort
from app.schemas.agent import AgentEvent
from app.use_cases.agent_loop import run_agent

_log = logging.getLogger("echodesk.agent")

router = APIRouter(tags=["agent"])


class AgentRunRequest(BaseModel):
    """用户问题 + 可选当前会议上下文。

    inline_context 与 /rag/ask 同语义: 前端把最近转录拼成可读字符串塞进来。
    max_iterations 兜底 6, 防止 LLM 死循环烧 token。
    """

    question: str
    inline_context: str | None = None
    max_iterations: int | None = None


@router.post("/agent/run")
async def agent_run(
    body: AgentRunRequest,
    settings: Settings = Depends(get_settings),
    main_llm: LLMPort = Depends(get_llm),
    rag: RagPort = Depends(get_rag),
    web: WebSearchPort = Depends(get_web),
    skill: SkillExecutorPort = Depends(get_skill),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")
    max_iter = body.max_iterations if body.max_iterations and body.max_iterations > 0 else 6

    async def _sse() -> AsyncIterator[bytes]:
        try:
            async for ev in run_agent(
                main_llm=main_llm,
                rag=rag,
                web=web,
                skill=skill,
                settings=settings,
                question=body.question,
                inline_context=body.inline_context,
                max_iterations=max_iter,
                enable_fast_path=True,
            ):
                yield _sse_frame(ev)
        except Exception as e:  # pragma: no cover - defensive: don't leave client hanging
            _log.exception("agent run crashed")
            yield _sse_frame(
                AgentEvent(type="error", payload={"error": f"agent crash: {e}", "stage": "loop"})
            )
            yield _sse_frame(AgentEvent(type="done"))

    return StreamingResponse(_sse(), media_type="text/event-stream")


def _sse_frame(ev: AgentEvent) -> bytes:
    """Serialize 单个 AgentEvent 成 SSE 帧。"""
    payload = json.dumps(ev.payload, ensure_ascii=False)
    return f"event: {ev.type}\ndata: {payload}\n\n".encode()
