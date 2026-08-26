"""
Tool registry — assembles all tool schemas and handlers for LLM function calling.

Usage in pipeline:
    from app.tools.registry import get_tools, get_handlers
    response = await complete_with_tools(messages, tool_handlers=get_handlers(device_id), tools=get_tools())
"""
from __future__ import annotations
from app.tools.web_search import TOOL_SCHEMA as WEB_SEARCH_SCHEMA, web_search
from app.tools.task_manager import (
    TOOL_SCHEMA as CREATE_TASK_SCHEMA,
    LIST_TOOL_SCHEMA as LIST_TASK_SCHEMA,
    create_task, list_tasks,
)


def get_tools() -> list[dict]:
    """Return all available tool schemas."""
    return [WEB_SEARCH_SCHEMA, CREATE_TASK_SCHEMA, LIST_TASK_SCHEMA]


def get_handlers(device_id: str) -> dict:
    """Return tool name → async handler mapping for a specific device."""
    return {
        "web_search": web_search,
        "create_task": lambda **kwargs: create_task(device_id=device_id, **kwargs),
        "list_tasks": lambda **kwargs: list_tasks(device_id=device_id, **kwargs),
    }
