"""Web Search adapter: Tavily only.

Tavily：POST https://api.tavily.com/search
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.schemas.rag import WebHit


class WebSearchError(RuntimeError):
    pass


_CF_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; EchoDemo/1.0)",
}


class TavilyWebSearch:
    """实现 ports.web_search.WebSearchPort。

    无 Tavily key 或 Tavily 请求失败时返回空结果，让上层明确提示联网检索不可用。
    """

    def __init__(self, settings: Settings, *, timeout_s: float = 15.0) -> None:
        self._settings = settings
        self._tavily_key = settings.tavily_api_key
        self._timeout = timeout_s

    async def search(self, query: str, *, top_n: int = 5) -> list[WebHit]:
        if not query.strip():
            return []
        if not self._tavily_key:
            return []
        try:
            return await self._search_tavily(query, top_n)
        except Exception:
            return []

    async def _search_tavily(self, query: str, top_n: int) -> list[WebHit]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=self._timeout, write=5.0, pool=5.0),
            trust_env=False,
        ) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                headers=_CF_HEADERS,
                json={
                    "api_key": self._tavily_key,
                    "query": query,
                    "max_results": top_n,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            WebHit(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("content") or "")[:500],
                score=float(r.get("score") or 0.0),
                source="tavily",
            )
            for r in (data.get("results") or [])[:top_n]
        ]
