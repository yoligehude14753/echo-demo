"""Web Search adapter: Tavily 主 + DDG 兜底（2026-05-26 用户决策放弃 Inspiro）。

Tavily：POST https://api.tavily.com/search
DDG：duckduckgo_search 库（无 key 但不稳定，仅做兜底）
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

    Tavily 失败/无 key → DDG 兜底。
    """

    def __init__(self, settings: Settings, *, timeout_s: float = 15.0) -> None:
        self._settings = settings
        self._tavily_key = settings.tavily_api_key
        self._timeout = timeout_s

    async def search(self, query: str, *, top_n: int = 5) -> list[WebHit]:
        if not query.strip():
            return []
        if self._tavily_key:
            try:
                hits = await self._search_tavily(query, top_n)
                if hits:
                    return hits
            except Exception:
                # Tavily 挂了 → DDG 兜底
                pass
        return await self._search_ddg(query, top_n)

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

    async def _search_ddg(self, query: str, top_n: int) -> list[WebHit]:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            raise WebSearchError(
                "ddg fallback unavailable; pip install duckduckgo-search"
            ) from None

        def _do() -> list[WebHit]:
            out: list[WebHit] = []
            with DDGS() as ddgs:
                for i, r in enumerate(ddgs.text(query, max_results=top_n)):
                    out.append(
                        WebHit(
                            title=str(r.get("title") or ""),
                            url=str(r.get("href") or r.get("url") or ""),
                            snippet=str(r.get("body") or "")[:500],
                            score=float(top_n - i) / float(top_n),
                            source="ddg",
                        )
                    )
                    if len(out) >= top_n:
                        break
            return out

        import asyncio

        return await asyncio.to_thread(_do)
