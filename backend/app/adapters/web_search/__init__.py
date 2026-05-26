"""Web Search adapter。"""

from app.adapters.web_search.tavily import TavilyWebSearch, WebSearchError

__all__ = ["TavilyWebSearch", "WebSearchError"]
