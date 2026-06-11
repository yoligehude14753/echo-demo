"""HTTP API: 今日回顾。

GET /recap/today — 把今天被动记录的 ambient 转录 + 会议纪要汇成一份回顾。
这是 EchoDesk 的"主动陪伴"能力入口（被动记忆 → 主动总结）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import get_llm_singleton as get_llm
from app.api.deps import get_repository
from app.ports.llm import LLMPort
from app.ports.repository import RepositoryPort
from app.use_cases.daily_recap import generate_daily_recap

_log = logging.getLogger("echodesk.recap")

router = APIRouter(tags=["recap"])
_RECAP_CACHE_TTL_S = 180.0


@dataclass(slots=True)
class _RecapCacheEntry:
    date: str
    expires_at: float
    response: DailyRecapResponse


_recap_cache: _RecapCacheEntry | None = None
_recap_cache_lock = asyncio.Lock()


class DailyRecapResponse(BaseModel):
    date: str
    recap_markdown: str
    n_ambient_segments: int
    n_meetings: int
    empty: bool
    todos: list[str] = Field(default_factory=list)
    cached: bool = False


@router.get("/recap/today", response_model=DailyRecapResponse)
async def recap_today(
    repository: RepositoryPort = Depends(get_repository),
    llm: LLMPort = Depends(get_llm),
    force: bool = Query(default=False, description="跳过短 TTL 缓存，强制重新生成"),
) -> DailyRecapResponse:
    now = datetime.now(UTC).astimezone()
    date_str = now.strftime("%Y-%m-%d")
    cached = _get_cached_recap(date_str) if not force else None
    if cached is not None:
        return cached

    async with _recap_cache_lock:
        cached = _get_cached_recap(date_str) if not force else None
        if cached is not None:
            return cached

        recap = await generate_daily_recap(repository=repository, llm=llm, now=now)
        response = DailyRecapResponse(
            date=recap.date,
            recap_markdown=recap.recap_markdown,
            n_ambient_segments=recap.n_ambient_segments,
            n_meetings=recap.n_meetings,
            empty=recap.empty,
            todos=recap.todos,
        )
        _set_cached_recap(response)
        return response


def _get_cached_recap(date_str: str) -> DailyRecapResponse | None:
    """返回同日未过期缓存；标记 cached=True 供诊断/前端观测。"""
    if _recap_cache is None:
        return None
    if _recap_cache.date != date_str or _recap_cache.expires_at <= time.monotonic():
        return None
    return _recap_cache.response.model_copy(update={"cached": True})


def _set_cached_recap(response: DailyRecapResponse) -> None:
    global _recap_cache  # noqa: PLW0603 - module-level short TTL cache
    _recap_cache = _RecapCacheEntry(
        date=response.date,
        expires_at=time.monotonic() + _RECAP_CACHE_TTL_S,
        response=response.model_copy(update={"cached": False}),
    )


def _clear_recap_cache_for_tests() -> None:
    global _recap_cache  # noqa: PLW0603
    _recap_cache = None
