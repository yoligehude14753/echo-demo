"""共享 FastAPI 依赖（LLM 单例 + 事件总线 + Repository + 清理钩子）。"""

from __future__ import annotations

from fastapi import Depends

from app.adapters.diarizer import make_diarizer
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.adapters.repo import make_repository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.use_cases.speaker_registry import SpeakerRegistry

_llm_singleton: OpenAICompatibleLLM | None = None
_event_bus_singleton: InMemoryEventBus | None = None
_repo_singleton: RepositoryPort | None = None
_diarizer_singleton: DiarizerPort | None = None
_speaker_registry_singleton: SpeakerRegistry | None = None


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


def get_repository(
    settings: Settings = Depends(get_settings),
) -> RepositoryPort:
    """SQLite 仓储单例。lifespan 调 ``init()``，关停在 ``aclose_repository()``。"""
    global _repo_singleton  # noqa: PLW0603
    if _repo_singleton is None:
        _repo_singleton = make_repository(settings)
    return _repo_singleton


def get_diarizer_singleton(
    settings: Settings = Depends(get_settings),
) -> DiarizerPort:
    """声纹单例：meeting 与 ambient 共享，保证 speaker_id 跨链路一致。"""
    global _diarizer_singleton  # noqa: PLW0603
    if _diarizer_singleton is None:
        _diarizer_singleton = make_diarizer(settings)
    return _diarizer_singleton


def get_speaker_registry(
    repository: RepositoryPort = Depends(get_repository),
) -> SpeakerRegistry:
    """跨进程持久化的全局说话人编号 + 用户改名 cache。"""
    global _speaker_registry_singleton  # noqa: PLW0603
    if _speaker_registry_singleton is None:
        _speaker_registry_singleton = SpeakerRegistry(repository)
    return _speaker_registry_singleton


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


async def aclose_repository() -> None:
    global _repo_singleton  # noqa: PLW0603
    if _repo_singleton is not None:
        await _repo_singleton.aclose()
        _repo_singleton = None


def reset_deps_for_test() -> None:
    """测试用：清掉所有单例缓存。"""
    global _llm_singleton, _event_bus_singleton, _repo_singleton  # noqa: PLW0603
    global _diarizer_singleton, _speaker_registry_singleton  # noqa: PLW0603
    _llm_singleton = None
    _event_bus_singleton = None
    _repo_singleton = None
    _diarizer_singleton = None
    _speaker_registry_singleton = None
