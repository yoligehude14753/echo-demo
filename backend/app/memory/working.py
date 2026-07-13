"""Bounded process-local L0 working memory."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from app.memory.models import MemoryScope, RecallCandidate
from app.schemas.llm import ChatMessage


@dataclass(frozen=True, slots=True)
class WorkingItem:
    item_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class WorkingMemoryStore:
    """Small LRU-like turn windows; shutdown drops every item by design."""

    def __init__(self, *, ttl_s: float, max_items: int, max_chars: int) -> None:
        self._ttl_s = max(60.0, ttl_s)
        self._max_items = max(2, max_items)
        self._max_chars = max(1_000, max_chars)
        self._items: dict[tuple[str, str, str, str], deque[WorkingItem]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(scope: MemoryScope, conversation_id: str) -> tuple[str, str, str, str]:
        return (
            scope.tenant_id,
            scope.owner_id,
            scope.session_id,
            conversation_id[:128] or "default",
        )

    def _prune_locked(self, key: tuple[str, str, str, str], now: datetime) -> None:
        window = self._items.get(key)
        if window is None:
            return
        cutoff = now - timedelta(seconds=self._ttl_s)
        while window and window[0].created_at < cutoff:
            window.popleft()
        while len(window) > self._max_items:
            window.popleft()
        total_chars = sum(len(item.content) for item in window)
        while window and total_chars > self._max_chars:
            total_chars -= len(window.popleft().content)
        if not window:
            self._items.pop(key, None)

    async def append_turn(
        self,
        scope: MemoryScope,
        conversation_id: str,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        now = datetime.now(UTC)
        key = self._key(scope, conversation_id)
        async with self._lock:
            window = self._items.setdefault(key, deque())
            window.extend(
                (
                    WorkingItem(
                        item_id=f"work_{uuid4().hex}",
                        role="user",
                        content=user_text[:8_000],
                        created_at=now,
                    ),
                    WorkingItem(
                        item_id=f"work_{uuid4().hex}",
                        role="assistant",
                        content=assistant_text[:8_000],
                        created_at=now,
                    ),
                )
            )
            self._prune_locked(key, now)

    async def history_messages(
        self,
        scope: MemoryScope,
        conversation_id: str,
    ) -> list[ChatMessage]:
        now = datetime.now(UTC)
        key = self._key(scope, conversation_id)
        async with self._lock:
            self._prune_locked(key, now)
            snapshot = list(self._items.get(key, ()))
        return [ChatMessage(role=item.role, content=item.content) for item in snapshot]

    async def candidates(
        self,
        scope: MemoryScope,
        conversation_id: str,
    ) -> list[RecallCandidate]:
        now = datetime.now(UTC)
        key = self._key(scope, conversation_id)
        async with self._lock:
            self._prune_locked(key, now)
            snapshot = list(self._items.get(key, ()))
        return [
            RecallCandidate(
                candidate_id=f"l0-conversation:{item.item_id}",
                level="L0",
                content=f"用户：{item.content}",
                source_ref=f"conversation:{conversation_id}#{item.item_id}",
                occurred_at=item.created_at,
                salience=0.66,
                kind="conversation_turn",
                metadata={"conversation_id": conversation_id, "role": item.role},
            )
            for item in snapshot
            if item.role == "user"
        ]

    async def clear(self, scope: MemoryScope, conversation_id: str) -> bool:
        key = self._key(scope, conversation_id)
        async with self._lock:
            return self._items.pop(key, None) is not None

    async def aclose(self) -> None:
        async with self._lock:
            self._items.clear()


__all__ = ["WorkingMemoryStore"]
