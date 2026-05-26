"""共享 FastAPI 依赖（LLM 单例 + 清理钩子）。"""

from __future__ import annotations

from fastapi import Depends

from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings, get_settings

_llm_singleton: OpenAICompatibleLLM | None = None


def get_llm_singleton(
    settings: Settings = Depends(get_settings),
) -> OpenAICompatibleLLM:
    """所有需要 LLM 的路由共用单例（lifespan 中关闭）。"""
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is None:
        _llm_singleton = OpenAICompatibleLLM(settings)
    return _llm_singleton


async def aclose_llm_singleton() -> None:
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is not None:
        await _llm_singleton.aclose()
        _llm_singleton = None
