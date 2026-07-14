"""共享 FastAPI 依赖（LLM 单例 + 事件总线 + Repository + 清理钩子）。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketException

from app.adapters.diarizer import make_diarizer
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import OpenAICompatibleLLM
from app.adapters.repo import make_repository
from app.agents.service import aclose_agent_task_service
from app.artifacts.repository import ArtifactRepository, reset_artifact_repository_for_test
from app.artifacts.repository import get_artifact_repository as _make_artifact_repository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.runtime import ScopedRuntimeRegistry, ScopeRuntime, run_registry_janitor
from app.security import (
    AccessPolicy,
    AccessPolicyError,
    Principal,
    SessionError,
    SessionStore,
    route_scope_path,
)
from app.security.context import bind_principal, current_principal, reset_principal
from app.security.governor import PrincipalGovernor
from app.sync_hub import SyncHubStore
from app.use_cases.auto_meeting_detector import AutoMeetingDetector
from app.use_cases.meeting_state import MeetingState
from app.use_cases.speaker_registry import SpeakerRegistry
from app.workflows.kernel import WorkflowDispatcher, WorkflowHandlerRegistry
from app.workflows.service import WorkflowService, reset_workflow_service_for_test
from app.workflows.service import get_workflow_service as _make_workflow_service

_llm_singleton: OpenAICompatibleLLM | None = None
_event_bus_singleton: InMemoryEventBus | None = None
_repo_singleton: RepositoryPort | None = None
_workflow_service_singleton: WorkflowService | None = None
_workflow_dispatcher_singleton: WorkflowDispatcher | None = None
_artifact_repository_singleton: ArtifactRepository | None = None
_session_store_singleton: SessionStore | None = None
_sync_hub_store_singleton: SyncHubStore | None = None
_access_policy_singleton: AccessPolicy | None = None
_governor_singleton: PrincipalGovernor | None = None
_scope_runtime_registry: ScopedRuntimeRegistry[tuple[str, str], ScopeRuntime] | None = None
_runtime_janitor_task: asyncio.Task[None] | None = None


def _principal_scope_key() -> tuple[str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.owner_id


def get_llm_singleton(
    settings: Settings = Depends(get_settings),
) -> OpenAICompatibleLLM:
    """所有需要 LLM 的路由共用单例（lifespan 中关闭）。"""
    global _llm_singleton  # noqa: PLW0603
    if _llm_singleton is None:
        _llm_singleton = OpenAICompatibleLLM(settings, governor=get_quota_governor(settings))
    return _llm_singleton


def get_event_bus() -> InMemoryEventBus:
    """事件总线单例。"""
    global _event_bus_singleton  # noqa: PLW0603
    if _event_bus_singleton is None:
        _event_bus_singleton = InMemoryEventBus()
    return _event_bus_singleton


def configure_event_bus(settings: Settings) -> InMemoryEventBus:
    global _event_bus_singleton  # noqa: PLW0603
    if _event_bus_singleton is None:
        _event_bus_singleton = InMemoryEventBus(
            per_subscriber_queue=settings.ws_subscriber_queue_size,
            replay_buffer=settings.ws_replay_buffer_size,
            max_scope_streams=settings.ws_scope_max_streams,
            max_admission_waiters=settings.ws_admission_queue_size,
            admission_wait_timeout_s=settings.ws_admission_wait_timeout_s,
        )
    return _event_bus_singleton


def get_quota_governor(
    settings: Settings = Depends(get_settings),
) -> PrincipalGovernor:
    global _governor_singleton  # noqa: PLW0603
    if _governor_singleton is None or _governor_singleton.settings is not settings:
        _governor_singleton = PrincipalGovernor(settings)
    return _governor_singleton


def get_scope_runtime_registry(
    settings: Settings = Depends(get_settings),
) -> ScopedRuntimeRegistry[tuple[str, str], ScopeRuntime]:
    global _scope_runtime_registry  # noqa: PLW0603
    if _scope_runtime_registry is None:
        _scope_runtime_registry = ScopedRuntimeRegistry(
            max_entries=settings.runtime_scope_max_entries,
            idle_ttl_s=settings.runtime_scope_idle_ttl_s,
            factory=ScopeRuntime,
        )
    return _scope_runtime_registry


def get_scope_runtime(
    settings: Settings = Depends(get_settings),
) -> ScopeRuntime:
    return get_scope_runtime_registry(settings).get_or_create(_principal_scope_key())


def peek_scope_runtime() -> ScopeRuntime | None:
    if _scope_runtime_registry is None:
        return None
    return _scope_runtime_registry.peek(_principal_scope_key())


def reset_scope_runtime_component_for_test(name: str) -> None:
    if _scope_runtime_registry is not None:
        _scope_runtime_registry.remove_component_all_for_test(name)


async def start_runtime_janitor(settings: Settings) -> None:
    global _runtime_janitor_task  # noqa: PLW0603
    if _runtime_janitor_task is not None:
        return
    registry = get_scope_runtime_registry(settings)
    _runtime_janitor_task = asyncio.create_task(
        run_registry_janitor(
            registry,  # type: ignore[arg-type]
            interval_s=settings.runtime_scope_janitor_interval_s,
        ),
        name="scoped-runtime-janitor",
    )


async def stop_runtime_janitor() -> None:
    global _runtime_janitor_task  # noqa: PLW0603
    if _runtime_janitor_task is not None:
        _runtime_janitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _runtime_janitor_task
        _runtime_janitor_task = None
    if _scope_runtime_registry is not None:
        await _scope_runtime_registry.aclose()


def get_session_store(settings: Settings = Depends(get_settings)) -> SessionStore:
    global _session_store_singleton  # noqa: PLW0603
    configured_path = Path(settings.db_path).expanduser()
    if _session_store_singleton is None or _session_store_singleton.db_path != configured_path:
        _session_store_singleton = SessionStore(settings.db_path)
    return _session_store_singleton


def get_sync_hub_store(settings: Settings = Depends(get_settings)) -> SyncHubStore:
    global _sync_hub_store_singleton  # noqa: PLW0603
    configured_path = Path(settings.db_path).expanduser()
    if (
        _sync_hub_store_singleton is None
        or _sync_hub_store_singleton.db_path != configured_path
    ):
        _sync_hub_store_singleton = SyncHubStore(configured_path)
    return _sync_hub_store_singleton


def get_access_policy(
    settings: Settings = Depends(get_settings),
    session_store: SessionStore = Depends(get_session_store),
) -> AccessPolicy:
    global _access_policy_singleton  # noqa: PLW0603
    if _access_policy_singleton is None or _access_policy_singleton.settings is not settings:
        _access_policy_singleton = AccessPolicy(settings, session_store)
    return _access_policy_singleton


def require_admin_access(
    request: Request,
    policy: AccessPolicy = Depends(get_access_policy),
) -> None:
    """Require the centralized host-admin capability for this request."""

    try:
        policy.require_host_admin(
            client_host=policy.client_host(request.client),
            authorization=request.headers.get("Authorization", ""),
            x_echo_admin_token=request.headers.get("X-Echo-Admin-Token", ""),
        )
    except AccessPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def get_request_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="session required")
    return principal


async def bind_websocket_principal(
    websocket: WebSocket,
    policy: AccessPolicy = Depends(get_access_policy),
) -> AsyncIterator[Principal]:
    """Authenticate and bind a WS principal before the endpoint accepts the socket."""

    try:
        principal = await policy.resolve_websocket_principal(
            client_host=policy.client_host(websocket.client),
            path=route_scope_path(websocket.scope),
            authorization=websocket.headers.get("authorization", ""),
            query_token=websocket.query_params.get("session") or "",
        )
    except AccessPolicyError as exc:
        raise WebSocketException(
            code=4403 if exc.status_code == 403 else 4401,
            reason=exc.detail,
        ) from exc
    except SessionError as exc:
        raise WebSocketException(code=4401, reason="session required") from exc
    token = bind_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


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
    runtime = get_scope_runtime(settings)
    return runtime.get_or_create(
        "diarizer",
        lambda: make_diarizer(settings, repository=repository),
    )


def get_speaker_registry(
    settings: Settings = Depends(get_settings),
    repository: RepositoryPort = Depends(get_repository),
) -> SpeakerRegistry:
    """说话人编号 + 用户改名 cache。

    phase4-speaker-reset：注入 settings 让 registry 按 ``diarizer_persist_speakers``
    切换 per-meeting / legacy 路径。默认 False → per-meeting 从 1 开始。
    """
    runtime = get_scope_runtime(settings)
    return runtime.get_or_create(
        "speaker_registry",
        lambda: SpeakerRegistry(repository, settings=settings),
    )


def get_auto_meeting_detector(
    settings: Settings = Depends(get_settings),
) -> AutoMeetingDetector:
    """自动会议检测器单例（process-local state）；参数来自 Settings。"""
    runtime = get_scope_runtime(settings)
    return runtime.get_or_create(
        "auto_meeting_detector",
        lambda: AutoMeetingDetector(
            window_s=settings.automeet_window_s,
            min_distinct_speakers=settings.automeet_min_distinct_speakers,
            min_active_seconds=settings.automeet_min_active_seconds,
            unknown_speaker_min_active_seconds=(
                settings.automeet_unknown_speaker_min_active_seconds
            ),
            silence_timeout_s=settings.automeet_silence_timeout_s,
            cooldown_s=settings.automeet_cooldown_s,
            max_meeting_duration_s=settings.automeet_max_meeting_duration_s,
        ),
    )


def get_meeting_state(
    settings: Settings = Depends(get_settings),
    repository: RepositoryPort = Depends(get_repository),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    detector: AutoMeetingDetector = Depends(get_auto_meeting_detector),
) -> MeetingState:
    """全局会议状态机单例（idle / in_meeting）。

    依赖 MeetingPipeline，但为防循环 import，pipeline 在首次调用时延迟导入。
    """
    from app.api.meetings import (
        dispatch_meeting_finalize,
        get_meeting_pipeline_for_lifespan,
    )

    runtime = get_scope_runtime(settings)

    def make_state() -> MeetingState:
        pipeline = get_meeting_pipeline_for_lifespan(settings, repository)
        dispatcher = get_workflow_dispatcher(get_workflow_service(settings, event_bus))

        async def finalize_via_workflow(meeting_id: str, title: str) -> object:
            return await dispatch_meeting_finalize(
                dispatcher,
                pipeline,
                repository,
                meeting_id=meeting_id,
                title=title,
                source="meeting_state",
            )

        return MeetingState(
            pipeline=pipeline,
            detector=detector,
            repository=repository,
            event_bus=event_bus,
            max_meeting_duration_s=settings.automeet_max_meeting_duration_s,
            manual_max_meeting_duration_s=settings.manual_meeting_max_duration_s,
            manual_inactivity_timeout_s=settings.manual_meeting_inactivity_timeout_s,
            recovery_max_age_s=settings.meeting_recovery_max_age_s,
            finalize_callback=finalize_via_workflow,
        )

    return runtime.get_or_create("meeting_state", make_state)


def get_workflow_service(
    settings: Settings = Depends(get_settings),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
) -> WorkflowService:
    """Workflow 0.3 状态机单例。"""
    global _workflow_service_singleton  # noqa: PLW0603
    if _workflow_service_singleton is None:
        _workflow_service_singleton = _make_workflow_service(settings, event_bus)
    return _workflow_service_singleton


def get_workflow_dispatcher(
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDispatcher:
    global _workflow_dispatcher_singleton  # noqa: PLW0603
    if _workflow_dispatcher_singleton is None:
        _workflow_dispatcher_singleton = WorkflowDispatcher(
            service,
            WorkflowHandlerRegistry(max_scopes=service.settings.runtime_scope_max_entries),
            scope_lease_factory=lambda scope: get_scope_runtime_registry(service.settings).acquire(
                scope
            ),
        )
    return _workflow_dispatcher_singleton


def get_artifact_repository(
    settings: Settings = Depends(get_settings),
) -> ArtifactRepository:
    """Artifact 0.3 metadata/link repository 单例。"""
    global _artifact_repository_singleton  # noqa: PLW0603
    if _artifact_repository_singleton is None:
        _artifact_repository_singleton = _make_artifact_repository(settings)
    return _artifact_repository_singleton


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


async def aclose_workflow_service() -> None:
    global _workflow_dispatcher_singleton, _workflow_service_singleton  # noqa: PLW0603
    if _workflow_dispatcher_singleton is not None:
        await _workflow_dispatcher_singleton.aclose()
        _workflow_dispatcher_singleton = None
    if _workflow_service_singleton is not None:
        await _workflow_service_singleton.aclose()
        _workflow_service_singleton = None


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
    global _workflow_service_singleton, _artifact_repository_singleton  # noqa: PLW0603
    global _workflow_dispatcher_singleton  # noqa: PLW0603
    global _session_store_singleton, _sync_hub_store_singleton  # noqa: PLW0603
    global _access_policy_singleton  # noqa: PLW0603
    global _governor_singleton, _scope_runtime_registry  # noqa: PLW0603
    _llm_singleton = None
    _event_bus_singleton = None
    _repo_singleton = None
    if _scope_runtime_registry is not None:
        _scope_runtime_registry.clear_for_test()
    _scope_runtime_registry = None
    _workflow_service_singleton = None
    _workflow_dispatcher_singleton = None
    _artifact_repository_singleton = None
    _session_store_singleton = None
    _sync_hub_store_singleton = None
    _access_policy_singleton = None
    _governor_singleton = None
    # meetings/capture 为避免循环依赖各自在 API 模块维护缓存；统一重置必须连同
    # 这些缓存一起清掉，否则下一次 app lifespan 会复用已经关闭的 repository。
    from app.api.capture import reset_ambient_pipeline
    from app.api.meetings import reset_meeting_pipeline
    from app.api.retrieval import reset_singletons as reset_retrieval_singletons

    reset_meeting_pipeline()
    reset_ambient_pipeline()
    reset_retrieval_singletons()
    reset_workflow_service_for_test()
    reset_artifact_repository_for_test()
