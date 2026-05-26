"""共享 FastAPI 依赖（LLM 单例 + 事件总线 + 清理钩子）。"""

from __future__ import annotations

from fastapi import Depends

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.config import Settings, get_settings

_llm_singleton: OpenAICompatibleLLM | None = None
_event_bus_singleton: InMemoryEventBus | None = None


def get_llm_singleton(
    settings: Settings = Depends(get_settings),
) -> OpenAICompatibleLLM:
    """所有需要 LLM 的路由共用单例（lifespan 中关闭）。"""
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is None:
        _llm_singleton = OpenAICompatibleLLM(settings)
    return _llm_singleton


def get_event_bus() -> InMemoryEventBus:
    """事件总线单例。"""
    global _event_bus_singleton  # noqa: PLW0603
    if _event_bus_singleton is None:
        _event_bus_singleton = InMemoryEventBus()
    return _event_bus_singleton


async def aclose_llm_singleton() -> None:
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is not None:
        await _llm_singleton.aclose()
        _llm_singleton = None


async def aclose_event_bus() -> None:
    global _event_bus_singleton  # noqa: PLW0603
    if _event_bus_singleton is not None:
        await _event_bus_singleton.aclose()
        _event_bus_singleton = None


def reset_deps_for_test() -> None:
    """测试用：清掉所有单例缓存。"""
    global _llm_singleton, _event_bus_singleton  # noqa: PLW0603
    _llm_singleton = None
    _event_bus_singleton = None
