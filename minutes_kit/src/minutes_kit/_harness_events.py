"""Claude Code subprocess 事件标准化（移植自 meetly）。

把 Claude Code stream-json 输出翻译成的内部事件类型；上层 runner 只依赖这套类型，
不依赖底层协议帧格式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SystemEvent:
    """会话初始化或工具列表声明等生命周期信息。"""

    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssistantTextEvent:
    """助手输出文本。stream=True 时是一段 delta。"""

    session_id: str
    text: str
    stream: bool = False


@dataclass(slots=True)
class ToolUseEvent:
    """助手决定调用工具。"""

    session_id: str
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ToolResultEvent:
    """工具调用结果回灌。"""

    session_id: str
    tool_use_id: str
    output: str
    is_error: bool = False


@dataclass(slots=True)
class ResultEvent:
    """会话结束总结：终止事件。"""

    session_id: str
    is_error: bool
    duration_ms: int
    num_turns: int
    result_text: str
    raw: dict[str, Any] = field(default_factory=dict)


HarnessEvent = (
    SystemEvent | AssistantTextEvent | ToolUseEvent | ToolResultEvent | ResultEvent
)
