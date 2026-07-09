"""共享 FastAPI 依赖（LLM 单例 + 事件总线 + Repository + 清理钩子）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.adapters.diarizer import make_diarizer
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.adapters.repo import make_repository
from app.agents.service import aclose_agent_task_service
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry

_llm_singleton: OpenAICompatibleLLM | None = None
_event_bus_singleton: InMemoryEventBus | None = None
_repo_singleton: RepositoryPort | None = None
_diarizer_singleton: DiarizerPort | None = None
_speaker_registry_singleton: SpeakerRegistry | None = None
_auto_detector_singleton: AutoMeetingDetector | None = None
_meeting_state_singleton: MeetingState | None = None


def require_admin_access(
    settings: Settings = Depends(get_settings),
    authorization: Annotated[str | None, Header()] = None,
    x_echo_admin_token: Annotated[str | None, Header(alias="X-Echo-Admin-Token")] = None,
) -> None:
    """Protect local-admin endpoints when the backend is exposed as a public demo."""
    if not settings.public_demo_mode:
        return

    expected = settings.debug_token.strip()
    if expected:
        if x_echo_admin_token == expected:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and token == expected:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin endpoints are disabled in public demo mode",
    )


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
    repository: RepositoryPort = Depends(get_repository),
) -> DiarizerPort:
    """声纹单例：meeting 与 ambient 共享，保证 speaker_id 跨链路一致。

    接 repository 让 ECAPA 把 centroid embedding 持久化到 speakers 表，
    重启后通过 ``await diarizer.hydrate()`` 恢复（修 ARCH-AUDIT §4 root #1 #9）。
    """
    global _diarizer_singleton  # noqa: PLW0603
    if _diarizer_singleton is None:
        _diarizer_singleton = make_diarizer(settings, repository=repository)
    return _diarizer_singleton


def get_speaker_registry(
    settings: Settings = Depends(get_settings),
    repository: RepositoryPort = Depends(get_repository),
) -> SpeakerRegistry:
    """说话人编号 + 用户改名 cache。

    phase4-speaker-reset：注入 settings 让 registry 按 ``diarizer_persist_speakers``
    切换 per-meeting / legacy 路径。默认 False → per-meeting 从 1 开始。
    """
    global _speaker_registry_singleton  # noqa: PLW0603
    if _speaker_registry_singleton is None:
        _speaker_registry_singleton = SpeakerRegistry(repository, settings=settings)
    return _speaker_registry_singleton


def get_auto_meeting_detector(
    settings: Settings = Depends(get_settings),
) -> AutoMeetingDetector:
    """自动会议检测器单例（process-local state）；参数来自 Settings。"""
    global _auto_detector_singleton  # noqa: PLW0603
    if _auto_detector_singleton is None:
        _auto_detector_singleton = AutoMeetingDetector(
            window_s=settings.automeet_window_s,
            min_distinct_speakers=settings.automeet_min_distinct_speakers,
            min_active_seconds=settings.automeet_min_active_seconds,
            silence_timeout_s=settings.automeet_silence_timeout_s,
            cooldown_s=settings.automeet_cooldown_s,
            max_meeting_duration_s=settings.automeet_max_meeting_duration_s,
        )
    return _auto_detector_singleton


def get_meeting_state(
    settings: Settings = Depends(get_settings),
    repository: RepositoryPort = Depends(get_repository),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    detector: AutoMeetingDetector = Depends(get_auto_meeting_detector),
) -> MeetingState:
    """全局会议状态机单例（idle / in_meeting）。

    依赖 MeetingPipeline，但为防循环 import，pipeline 在首次调用时延迟导入。
    """
    from app.api.meetings import get_meeting_pipeline_for_lifespan  # 局部导入避免循环

    global _meeting_state_singleton  # noqa: PLW0603
    if _meeting_state_singleton is None:
        pipeline = get_meeting_pipeline_for_lifespan(settings, repository)
        _meeting_state_singleton = MeetingState(
            pipeline=pipeline,
            detector=detector,
            repository=repository,
            event_bus=event_bus,
            max_meeting_duration_s=settings.automeet_max_meeting_duration_s,
        )
    return _meeting_state_singleton


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


async def aclose_agents() -> None:
    await aclose_agent_task_service()


def reset_deps_for_test() -> None:
    """测试用：清掉所有单例缓存。"""
    global _llm_singleton, _event_bus_singleton, _repo_singleton  # noqa: PLW0603
    global _diarizer_singleton, _speaker_registry_singleton  # noqa: PLW0603
    global _auto_detector_singleton, _meeting_state_singleton  # noqa: PLW0603
    _llm_singleton = None
    _event_bus_singleton = None
    _repo_singleton = None
    _diarizer_singleton = None
    _speaker_registry_singleton = None
    _auto_detector_singleton = None
    _meeting_state_singleton = None
