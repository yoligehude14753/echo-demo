"""
File Context — fetches workspace context from the Electron desktop app.

The Electron main process runs a local IPC HTTP server on DESKTOP_MCP_URL
(default http://localhost:17890) that exposes:

  GET  /workspaces          → list of authorised workspace paths
  GET  /file-context?path=  → reads echo-context.md from the workspace root
  POST /read-file           → reads an arbitrary file within an authorised workspace
  POST /search-files        → searches files matching a query within a workspace

When no desktop app is running (server unreachable), all functions silently
return empty / None so the pipeline continues without file context.

File context injection strategy:
  1. Fetch `echo-context.md` from each active workspace (cheap, always present)
  2. If user query matches a file mention, fetch that specific file via nano model
     for summarisation before injecting into the prompt.
"""
from __future__ import annotations
import asyncio
from loguru import logger

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from app.config import get_config


async def fetch_workspace_context() -> str:
    """
    Fetch echo-context.md from all authorised workspaces.
    Returns combined context string, or "" if desktop is not running.
    """
    if not HTTPX_AVAILABLE:
        return ""

    cfg = get_config()
    base = cfg.DESKTOP_MCP_URL.rstrip("/")
    timeout = cfg.DESKTOP_MCP_TIMEOUT_S

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1. List workspaces
            resp = await client.get(f"{base}/workspaces")
            resp.raise_for_status()
            workspaces: list[dict] = resp.json()

            if not workspaces:
                return ""

            # 2. Fetch echo-context.md from each workspace (parallel)
            async def _fetch_one(ws_path: str) -> str:
                try:
                    r = await client.get(
                        f"{base}/file-context",
                        params={"path": ws_path},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        content = data.get("content", "").strip()
                        if content:
                            return f"## 工作区：{ws_path}\n{content}"
                except Exception:
                    pass
                return ""

            results = await asyncio.gather(
                *[_fetch_one(ws["path"]) for ws in workspaces]
            )
            combined = "\n\n".join(r for r in results if r)
            if combined:
                logger.debug(f"File context loaded: {len(combined)} chars")
            return combined

    except (httpx.ConnectError, httpx.TimeoutException):
        # Desktop app not running — silent fallback
        return ""
    except Exception as e:
        logger.debug(f"File context fetch error: {e}")
        return ""


async def read_file_for_prompt(file_path: str) -> str:
    """
    Read a specific file from an authorised workspace.
    Returns file content (truncated to FILE_READ_MAX_BYTES), or "".
    Uses nano model for summarisation if content exceeds FILE_CHUNK_MAX_TOKENS.
    """
    if not HTTPX_AVAILABLE:
        return ""

    cfg = get_config()
    base = cfg.DESKTOP_MCP_URL.rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=cfg.DESKTOP_MCP_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base}/read-file",
                json={"path": file_path},
            )
            if resp.status_code != 200:
                return ""
            data = resp.json()
            content: str = data.get("content", "")

        if not content:
            return ""

        # Rough token estimate (4 chars ≈ 1 token)
        estimated_tokens = len(content) // 4
        if estimated_tokens > cfg.FILE_CHUNK_MAX_TOKENS:
            # Summarise with nano model to stay within prompt budget
            from app.llm import complete_nano
            summary = await complete_nano(messages=[{
                "role": "user",
                "content": (
                    f"请将以下文件内容提炼为简洁摘要（不超过300字），"
                    f"保留关键事实、数字和结论：\n\n{content[:cfg.FILE_READ_MAX_BYTES]}"
                ),
            }])
            return f"[文件摘要: {file_path}]\n{summary}"

        return f"[文件: {file_path}]\n{content}"

    except (httpx.ConnectError, httpx.TimeoutException):
        return ""
    except Exception as e:
        logger.debug(f"read_file error ({file_path}): {e}")
        return ""


async def search_workspace_files(query: str, top_k: int = 5) -> list[dict]:
    """
    Search files across authorised workspaces matching a query.
    Returns list of {"path": str, "snippet": str} dicts.
    """
    if not HTTPX_AVAILABLE:
        return []

    cfg = get_config()
    base = cfg.DESKTOP_MCP_URL.rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=cfg.DESKTOP_MCP_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base}/search-files",
                json={"query": query, "top_k": top_k},
            )
            if resp.status_code != 200:
                return []
            return resp.json()
    except Exception:
        return []
