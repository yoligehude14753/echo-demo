"""本地日期时间确定性回答。

像「今天星期几 / 今天几号 / 现在几点」这种问题不能交给 LLM 猜，也不需要联网。
这里用系统本地时区直接回答，避免模型因 prompt 里的「没有联网」而说不知道。
"""

from __future__ import annotations

import re
from datetime import datetime

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_WEEKDAY_RE = re.compile(r"(今天|今日|现在|当前).{0,8}(星期几|周几)|(?:星期几|周几).{0,8}(今天|今日)")
_DATE_RE = re.compile(r"(今天|今日|现在|当前).{0,8}(几号|日期|年月日|哪天)|(?:几号|日期|哪天).{0,8}(今天|今日)")
_TIME_RE = re.compile(r"(现在|当前|此刻).{0,8}(几点|时间)|(?:几点|时间).{0,8}(现在|当前)")


def _format_date(now: datetime) -> str:
    return f"{now.year}年{now.month}月{now.day}日"


def _format_weekday(now: datetime) -> str:
    return _WEEKDAYS[now.weekday()]


def answer_local_datetime(question: str, *, now: datetime | None = None) -> str | None:
    """若问题可由本机时间确定性回答，返回中文答案；否则返回 None。"""
    q = question.strip()
    if not q:
        return None
    current = now or datetime.now().astimezone()
    date = _format_date(current)
    weekday = _format_weekday(current)
    if _WEEKDAY_RE.search(q):
        return f"今天是{date}，{weekday}。"
    if _DATE_RE.search(q):
        return f"今天是{date}，{weekday}。"
    if _TIME_RE.search(q):
        return f"现在是{date} {current:%H:%M}，{weekday}。"
    return None
