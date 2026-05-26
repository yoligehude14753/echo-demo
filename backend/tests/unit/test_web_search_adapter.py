"""Web Search adapter 单测：mock Tavily HTTP / DDG。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.web_search import TavilyWebSearch
from app.config import Settings


@pytest.fixture
def settings_with_key() -> Settings:
    return Settings(tavily_api_key="tvly-test", web_search_enabled=True)


@pytest.fixture
def settings_no_key() -> Settings:
    return Settings(tavily_api_key="", web_search_enabled=True)


def _mock_tavily_resp(results: list[dict]) -> object:
    resp = MagicMock()
    resp.json.return_value = {"results": results}
    resp.raise_for_status = MagicMock()
    fake = MagicMock()
    fake.post = AsyncMock(return_value=resp)
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    return fake


@pytest.mark.asyncio
@pytest.mark.unit
async def test_search_tavily_returns_hits(settings_with_key: Settings) -> None:
    fake = _mock_tavily_resp(
        [
            {
                "title": "Nvidia H100 报价",
                "url": "https://example.com/h100",
                "content": "Nvidia H100 8 GPU 集群价格...",
                "score": 0.95,
            }
        ]
    )
    with patch("app.adapters.web_search.tavily.httpx.AsyncClient", return_value=fake):
        web = TavilyWebSearch(settings_with_key)
        hits = await web.search("Nvidia H100 价格", top_n=3)
    assert len(hits) == 1
    assert hits[0].title == "Nvidia H100 报价"
    assert hits[0].url == "https://example.com/h100"
    assert hits[0].source == "tavily"
    assert hits[0].score == 0.95


@pytest.mark.asyncio
@pytest.mark.unit
async def test_search_empty_query_returns_empty(settings_with_key: Settings) -> None:
    web = TavilyWebSearch(settings_with_key)
    assert await web.search("  ") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tavily_failure_falls_back_to_ddg(settings_with_key: Settings) -> None:
    fake_tavily = MagicMock()
    fake_tavily.post = AsyncMock(side_effect=RuntimeError("boom"))
    fake_tavily.__aenter__ = AsyncMock(return_value=fake_tavily)
    fake_tavily.__aexit__ = AsyncMock(return_value=None)

    class _FakeDDGS:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def text(self, q: str, max_results: int = 5) -> list[dict]:
            return [{"title": "DDG ans", "href": "https://ddg.example/x", "body": "snippet"}]

    fake_ddg_mod = MagicMock()
    fake_ddg_mod.DDGS = _FakeDDGS

    with (
        patch("app.adapters.web_search.tavily.httpx.AsyncClient", return_value=fake_tavily),
        patch.dict("sys.modules", {"duckduckgo_search": fake_ddg_mod}),
    ):
        web = TavilyWebSearch(settings_with_key)
        hits = await web.search("ddg fallback test")
    assert len(hits) == 1
    assert hits[0].source == "ddg"
    assert hits[0].url == "https://ddg.example/x"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_tavily_key_skips_to_ddg(settings_no_key: Settings) -> None:
    class _FakeDDGS:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def text(self, q: str, max_results: int = 5) -> list[dict]:
            return [
                {"title": "x", "href": "https://x.example/", "body": "y"},
                {"title": "z", "href": "https://z.example/", "body": "w"},
            ]

    fake_ddg_mod = MagicMock()
    fake_ddg_mod.DDGS = _FakeDDGS

    with patch.dict("sys.modules", {"duckduckgo_search": fake_ddg_mod}):
        web = TavilyWebSearch(settings_no_key)
        hits = await web.search("no key path")
    assert len(hits) == 2
    assert all(h.source == "ddg" for h in hits)
