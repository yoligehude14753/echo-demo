"""
Web search tool — Inspiro.top AI Agent Search Engine.

Supports /search (with AI answer summary), /extract (single-page content).
Used by LLM as a function call tool.

Docs: https://inspiro.top/dashboard/docs
"""
import httpx
from loguru import logger
from app.config import get_config

# Cloudflare blocks python-httpx/* UA; use a neutral browser UA to pass CF checks
_CF_SAFE_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; EchoAgent/1.0)",
}

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取实时信息。当用户问到时事、天气、最新资讯、需要查资料时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，默认5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


async def web_search(query: str, max_results: int = 5) -> str:
    """
    Execute a web search via Inspiro.top API.
    Uses include_answer=true to get AI-generated summary in addition to raw results.
    Returns formatted string for LLM consumption.
    """
    cfg = get_config()
    if not cfg.INSPIRO_API_KEY:
        return "（网络搜索未配置，无法获取实时信息）"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            trust_env=False,  # bypass macOS system proxy (PAC) to avoid DNS delay
        ) as client:
            response = await client.post(
                f"{cfg.INSPIRO_BASE_URL}/search",
                headers={
                    **_CF_SAFE_HEADERS,
                    "Authorization": f"Bearer {cfg.INSPIRO_API_KEY}",
                },
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": True,   # AI-generated summary answer
                },
            )
            response.raise_for_status()
            data = response.json()

    except httpx.TimeoutException:
        logger.warning(f"Web search timeout: {query}")
        return f"（搜索超时，无法获取 '{query}' 的结果）"
    except Exception as e:
        logger.warning(f"Web search error: {e}")
        return f"（搜索失败：{e}）"

    # Prefer AI-generated answer if available
    answer = data.get("answer") or ""
    results = data.get("results", [])

    if not results and not answer:
        return f"（没有找到 '{query}' 的相关结果）"

    lines = [f"关于「{query}」的搜索结果：\n"]

    # Prepend AI answer summary when available
    if answer:
        lines.append(f"**摘要**：{answer}\n")

    # Append individual source results (filter score < 0.3 if score present)
    for i, r in enumerate(results[:max_results], 1):
        score = r.get("score", 1.0)
        if score < 0.3:
            continue
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("content", r.get("snippet", ""))[:250]
        lines.append(f"{i}. **{title}**\n   {snippet}\n   来源：{url}\n")

    return "\n".join(lines)
