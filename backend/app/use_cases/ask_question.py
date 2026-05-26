"""use_case: ask_question — 通用问答（流式）。

输入：用户问题（自然语言）
输出：LLM 增量回答（AsyncIterator[str]）

约束：
- 只依赖 ports.LLMPort（架构 Fitness Function 强制）
- system prompt 在此层定义（属于业务编排，不属于 adapter）
- 后续 PR-4 接入 RAG/Web 仲裁后，本 use_case 升级为 RAG-grounded 回答
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.ports.llm import LLMPort
from app.schemas.llm import ChatMessage

SYSTEM_PROMPT = """你是 Echo，会议+办公场景下的个人数字分身。
特点：
- 中文优先，回答简洁清晰，必要时分点
- 不知道就说不知道，不要编造
- 任何涉及"最新""今天""现在"的问题应说明你没有联网（本 PR 阶段 RAG/Web 未接入）
"""


async def ask_question(
    llm: LLMPort,
    question: str,
    *,
    history: list[ChatMessage] | None = None,
    model: str | None = None,
) -> AsyncIterator[str]:
    """流式回答用户问题。

    Args:
        llm: 注入的 LLM Port 实现
        question: 用户问题
        history: 可选历史消息（多轮上下文）
        model: 显式指定模型（None → MAIN 默认）
    """
    messages: list[ChatMessage] = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
    if history:
        messages.extend(history)
    messages.append(ChatMessage(role="user", content=question))

    async for chunk in llm.chat_stream(messages, model=model):
        yield chunk
