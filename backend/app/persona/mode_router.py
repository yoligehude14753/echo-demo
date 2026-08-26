"""
Conversation Mode Router — selects one of 5 modes based on user intent + soul state.

Modes:
  chat      — casual conversation, emotional support
  task      — goal-oriented help, step-by-step execution
  explore   — curiosity-driven discussion, no fixed goal
  reflect   — introspective, user venting or processing emotions
  focus     — distraction-free, minimal responses (user in work mode)

Mode selection uses keyword heuristics + soul state (PAD), not LLM scoring.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from app.soul.state import SoulState


@dataclass
class ConversationMode:
    name: str
    system_suffix: str      # appended to base system prompt
    filler_allowed: bool
    response_length: str    # short / medium / long


MODES: dict[str, ConversationMode] = {
    "chat": ConversationMode(
        name="chat",
        system_suffix="保持轻松自然的聊天风格，情感优先。",
        filler_allowed=True,
        response_length="short",
    ),
    "task": ConversationMode(
        name="task",
        system_suffix="聚焦完成用户的具体目标，步骤清晰，信息准确。",
        filler_allowed=False,
        response_length="medium",
    ),
    "explore": ConversationMode(
        name="explore",
        system_suffix="保持好奇心，引导深入讨论，分享有趣视角。",
        filler_allowed=True,
        response_length="long",
    ),
    "reflect": ConversationMode(
        name="reflect",
        system_suffix="以倾听为主，情感共鸣优先，不急于给出建议。",
        filler_allowed=True,
        response_length="short",
    ),
    "focus": ConversationMode(
        name="focus",
        system_suffix="极简响应，只回答直接问题，不闲聊。",
        filler_allowed=False,
        response_length="short",
    ),
}

# Keyword patterns for mode detection
_TASK_PATTERNS = re.compile(
    r"帮我|帮忙|怎么|如何|步骤|计划|搜索|查一下|提醒|设置|写|生成|翻译", re.IGNORECASE
)
_REFLECT_PATTERNS = re.compile(
    r"很难过|很累|压力|不开心|委屈|崩溃|焦虑|烦死了|不知道怎么|好迷茫", re.IGNORECASE
)
_EXPLORE_PATTERNS = re.compile(
    r"为什么|你觉得|你认为|有没有|好奇|有意思|聊聊|讲讲|比较", re.IGNORECASE
)
_FOCUS_PATTERNS = re.compile(
    r"专注模式|别打扰|不要主动|安静一下", re.IGNORECASE
)


def route_mode(user_text: str, soul: SoulState | None = None) -> ConversationMode:
    """
    Select conversation mode from user text + optional soul state.
    Priority: focus > reflect > task > explore > chat
    """
    if _FOCUS_PATTERNS.search(user_text):
        return MODES["focus"]

    if _REFLECT_PATTERNS.search(user_text):
        return MODES["reflect"]

    # Low pleasure in soul → lean toward reflect
    if soul and soul.pleasure < -0.4:
        return MODES["reflect"]

    if _TASK_PATTERNS.search(user_text):
        return MODES["task"]

    if _EXPLORE_PATTERNS.search(user_text):
        return MODES["explore"]

    return MODES["chat"]
