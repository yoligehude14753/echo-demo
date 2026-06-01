"""Agent loop schemas.

Echo 的复合任务（"调研 X 并输出 HTML"、"先看本地手册再上网查再生成 PPT"）
不再走"单一 intent 选 1 个工具"的老路；改由主 LLM 在工具列表里串联调度。

本文件只定义类型/事件。具体循环逻辑在 ``app/use_cases/agent_loop.py``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentEventType = Literal[
    "plan",
    "tool_call",
    "tool_result",
    "delta",
    "artifact",
    "final",
    "error",
    "done",
]


class ToolCall(BaseModel):
    """LLM 发起的一次工具调用请求（已解析）。"""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ToolResult(BaseModel):
    """单次工具执行结果。

    - ``content`` 是回喂给 LLM 的文本（已限长 / 摘要化，避免 context 爆掉）
    - ``summary`` 是给 UI 显示的一行状态（"已检索 12 chunk"）
    - ``metadata`` 不会喂回 LLM，但会被 SSE 转给前端（如 artifact dict）
    """

    name: str
    ok: bool
    content: str
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentEvent(BaseModel):
    """Agent loop 对外（SSE）的统一事件。

    | type | payload 关键字段 |
    |---|---|
    | plan | step (int), max_steps (int) |
    | tool_call | name, args, reason, step |
    | tool_result | name, ok, summary, step |
    | delta | text |
    | artifact | artifact dict |
    | final | answer, artifact_ids |
    | error | error, stage |
    | done | (空) |
    """

    type: AgentEventType
    payload: dict[str, Any] = Field(default_factory=dict)
