"""use_case: 今日回顾（被动记忆 → 主动陪伴）。

把"今天"被动记录的 ambient 转录 + 已结束会议的纪要，喂给主 LLM 生成一份
结构化回顾（聊了什么 / 关键信息 / 待办 / 值得记住）。这是 EchoDesk 区别于
"任务执行型"助手的陪伴能力：用户不必发问，Echo 也能主动把一天串起来。

依赖 Port（架构 fitness：use_case 只看 ports + schemas）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.ports.llm import LLMPort
from app.ports.repository import RepositoryPort
from app.schemas.llm import ChatMessage

logger = logging.getLogger("echodesk.recap")

_MAX_AMBIENT_SEGMENTS = 200
_MAX_AMBIENT_CHARS = 8_000
_RECAP_MAX_TOKENS = 1_200
_RECAP_TIMEOUT_S = 60.0

_RECAP_SYSTEM = """你是 EchoDesk 桌面助手 Echo。下面是用户**今天**被动记录下来的对话转录与
会议纪要。请你像一个贴心的助理，把今天串成一份简洁、有条理的「今日回顾」。

要求：
- 用中文 markdown，分这几块（没内容的块就省略，不要硬凑）：
  ## 今天聊了什么（3-6 条要点）
  ## 关键信息 / 决定
  ## 待办与跟进（有就列：事项 + 谁/何时，没有就省略）
  ## 值得记住的点
- 只基于给定材料，不编造；材料零散/口语化是正常的，提炼成人话即可。
- 简洁，别注水；像同事帮你回顾一天，而不是流水账。
- 开头一句话总结今天的主题氛围。"""


@dataclass(slots=True)
class DailyRecap:
    date: str
    recap_markdown: str
    n_ambient_segments: int
    n_meetings: int
    empty: bool
    todos: list[str] = field(default_factory=list)


# 待办段标题里出现这些词就视为"待办与跟进"块
_TODO_HEADING_HINTS = ("待办", "跟进", "todo", "action")
# 列表项前缀：- / * / 1. / 1) / 1、
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)、])\s+(.*)$")


def _extract_todos(markdown: str) -> list[str]:
    """从回顾 markdown 的「待办与跟进」段抽出条目（供主动催办/语音播报用）。

    只在命中待办标题后、到下一个标题前的范围里取列表项；去掉行内 markdown 强调符号。
    """
    if not markdown:
        return []
    todos: list[str] = []
    in_section = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_section = any(h in heading for h in _TODO_HEADING_HINTS)
            continue
        if not in_section:
            continue
        m = _LIST_ITEM_RE.match(stripped)
        if m:
            # 去掉 markdown 强调符号（**bold** / `code`），保留正文
            item = re.sub(r"[*`]", "", m.group(1)).strip()
            if item:
                todos.append(item)
    return todos


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """返回 now 当天的 [00:00, now]（用 now 的 tzinfo，保持时区一致）。"""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


async def generate_daily_recap(
    *,
    repository: RepositoryPort,
    llm: LLMPort,
    now: datetime,
) -> DailyRecap:
    """生成今日回顾。无任何素材时返回 empty=True，不调用 LLM。"""
    start, until = _day_bounds(now)
    date_str = start.strftime("%Y-%m-%d")

    ambient = await repository.list_ambient_segments(
        since=start, until=until, limit=_MAX_AMBIENT_SEGMENTS
    )
    meetings = await repository.list_meetings(limit=50)
    today_meetings = [
        m for m in meetings if (m.started_at and m.started_at >= start)
    ]

    # 拼素材
    parts: list[str] = []
    if ambient:
        lines: list[str] = []
        used = 0
        for seg in ambient:
            text = seg.text.strip()
            if not text:
                continue
            who = seg.speaker_label or seg.speaker_id or "?"
            line = f"{who}: {text}"
            if used + len(line) > _MAX_AMBIENT_CHARS:
                break
            lines.append(line)
            used += len(line)
        if lines:
            parts.append("# 今日对话转录（节选）\n" + "\n".join(lines))

    for m in today_meetings:
        title = m.display_title or m.title or m.id
        if m.minutes_json:
            parts.append(f"# 会议：{title}\n{m.minutes_json[:2000]}")
        else:
            parts.append(f"# 会议：{title}（进行中或暂无纪要）")

    if not parts:
        return DailyRecap(
            date=date_str,
            recap_markdown="",
            n_ambient_segments=0,
            n_meetings=0,
            empty=True,
        )

    material = "\n\n".join(parts)
    try:
        resp = await llm.chat(
            [
                ChatMessage(role="system", content=_RECAP_SYSTEM),
                ChatMessage(role="user", content=material),
            ],
            max_tokens=_RECAP_MAX_TOKENS,
            temperature=0.4,
            timeout_s=_RECAP_TIMEOUT_S,
        )
        recap = (resp.content or "").strip()
    except Exception as e:
        logger.warning("daily recap LLM failed: %s", e)
        recap = ""

    if not recap:
        recap = "今天的回顾暂时生成失败，请稍后再试。"

    return DailyRecap(
        date=date_str,
        recap_markdown=recap,
        n_ambient_segments=len(ambient),
        n_meetings=len(today_meetings),
        empty=False,
        todos=_extract_todos(recap),
    )
