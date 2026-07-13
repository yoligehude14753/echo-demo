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
from app.schemas.llm import ChatMessage, LLMResponse

SYSTEM_PROMPT = """你是 EchoDesk，会议+办公场景下的个人数字分身。
特点：
- 中文优先，回答简洁清晰，必要时分点
- 不知道就说不知道，不要编造
- 需要最新信息时不要编造；如当前上下文没有证据，建议用户使用联网检索或知识库检索入口
"""


def build_question_messages(
    question: str,
    *,
    history: list[ChatMessage] | None = None,
    memory_context: str | None = None,
) -> list[ChatMessage]:
    """构造 EchoDesk 纯对话消息。"""
    messages: list[ChatMessage] = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
    if memory_context:
        messages.append(
            ChatMessage(
                role="system",
                content=(
                    "下面是按当前问题关联出的历史信息。回答时只能把原文中明确表达的内容"
                    "当作事实；关联原因不是事实。引用历史会议、产物或长期记忆时，用其"
                    "来源标记，无法由原文支持的内容静默省略。\n\n" + memory_context
                ),
            )
        )
    if history:
        messages.extend(history)
    messages.append(ChatMessage(role="user", content=question))
    return messages


async def answer_question_once(
    llm: LLMPort,
    question: str,
    *,
    history: list[ChatMessage] | None = None,
    memory_context: str | None = None,
    model: str | None = None,
    max_tokens: int | None = 768,
    timeout_s: float = 45.0,
) -> LLMResponse:
    """短对话一次性回答。

    /chat 用它而不是 streaming create：当前公开模型的 streaming 首包偶发超过
    60s，但非流式短回答稳定得多。前端仍收到 SSE，只是服务端一帧返回完整回答。
    """
    return await llm.chat(
        build_question_messages(
            question,
            history=history,
            memory_context=memory_context,
        ),
        model=model,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


async def ask_question(
    llm: LLMPort,
    question: str,
    *,
    history: list[ChatMessage] | None = None,
    model: str | None = None,
    max_tokens: int | None = 768,
) -> AsyncIterator[str]:
    """流式回答用户问题。

    Args:
        llm: 注入的 LLM Port 实现
        question: 用户问题
        history: 可选历史消息（多轮上下文）
        model: 显式指定模型（None → MAIN 默认）
        max_tokens: 纯对话默认收紧输出预算，避免简单问题走长生成慢路径
    """
    messages = build_question_messages(question, history=history)

    async for chunk in llm.chat_stream(messages, model=model, max_tokens=max_tokens):
        yield chunk
