"""Web Search Port：Tavily(主) → DDG(兜底)。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.rag import WebHit


@runtime_checkable
class WebSearchPort(Protocol):
    async def search(self, query: str, *, top_n: int = 5) -> list[WebHit]: ...
