"""FastAPI 入口：仅做装配，不写业务逻辑。

启动（canonical）：
    cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8769 --ws-max-size 4096

注：8769 是 EchoDesk 统一端口（P1.1 Phase 1 收口），main.cjs / runtime.ts
/ vite.config / playwright 配置 / install-backend.sh 都对齐这个值。改前先
确认所有地方一起改。
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.adapters.repo.migrator import run_migrations
from app.api.admin import router as admin_router
from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.capture import router as capture_router
from app.api.chat import router as chat_router
from app.api.deps import (
    aclose_agents,
    aclose_event_bus,
    aclose_llm_singleton,
    aclose_repository,
    aclose_workflow_service,
    configure_event_bus,
    get_access_policy,
    get_diarizer_singleton,
    get_quota_governor,
    get_repository,
    get_scope_runtime_registry,
    get_session_store,
    get_speaker_registry,
    require_admin_access,
    start_runtime_janitor,
    stop_runtime_janitor,
)
from app.api.diagnostics import router as diagnostics_router
from app.api.health import router as health_router
from app.api.health import start_prober, stop_prober
from app.api.hub import router as hub_router
from app.api.intent import router as intent_router
from app.api.meetings import get_meeting_pipeline_for_lifespan
from app.api.meetings import router as meetings_router
from app.api.memory import router as memory_router
from app.api.retrieval import get_rag
from app.api.retrieval import router as retrieval_router
from app.api.sessions import router as sessions_router
from app.api.speakers import router as speakers_router
from app.api.tts import router as tts_router
from app.api.workflows import router as workflows_router
from app.api.workspace import router as workspace_router
from app.api.ws import router as ws_router
from app.build_contract import backend_build_contract
from app.config import Settings, get_settings
from app.config_io import user_config_dir
from app.memory import aclose_memory_service
from app.ports.repository import RepositoryPort
from app.runtime import RuntimeCapacityExceeded, RuntimeLease, ScopeRuntime
from app.security import (
    LEGACY_OWNER_ID,
    AccessPolicy,
    AccessPolicyError,
    Principal,
    SessionError,
    local_principal,
    route_scope_path,
)
from app.security.access import PreAuthAdmissionError
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    PUBLIC_CLIENT_UPGRADE_URL,
    PUBLIC_CLIENT_VERSION_HEADER,
    PUBLIC_MINIMUM_CLIENT_VERSION_HEADER,
)
from app.security.context import bind_principal, reset_principal
from app.security.deployment_gate import DeploymentGateMiddleware
from app.security.errors import InternalHTTPException
from app.security.governor import PrincipalGovernor, QuotaExceeded, QuotaReservation
from app.security.headers import PRIVATE_NO_STORE_HEADERS, apply_private_no_store
from app.security.redaction import RedactingFormatter, install_redaction_filter
from app.upload import UploadIngressMiddleware, upload_body_limit

logger = logging.getLogger("echodesk")


class _StreamingBodyResponse(Protocol):
    body_iterator: AsyncIterator[Any]


class _AsyncReleaseLease(Protocol):
    async def release(self) -> None: ...


def _setup_logging(level: str) -> None:
    """P1.3：backend log 同时写 stdout + ~/.echodesk/logs/backend-YYYYMMDD.log。

    rotate：按天滚动，保留 14 天；超出自动删。stdout 仍然有（dev / Electron
    spawn 时能看实时输出），落盘是为了 Phase 2 诊断包导出 + 用户事后查问题。

    幂等：多次调用只会保留最后一次配置（清旧 handlers 再加）。
    """
    log_dir = user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"

    root = logging.getLogger()
    # 清掉之前 basicConfig 加的 stream handler，避免重复行
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    fmt = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_h = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=False,
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    install_redaction_filter(root)
    install_redaction_filter(logging.getLogger("uvicorn.access"))

    logger.info("logging: stdout + %s (rotate daily, keep 14 days)", log_path)


def _sweep_macos_dotfiles(target: object) -> None:
    """P1.8：用户数据目录里清掉 mac 自己写进去的 .DS_Store。

    Spotlight / Finder 在用户进入 ~/.echodesk/ 时会自动创建 .DS_Store；
    它对数据无害但污染目录列表（用户截图里能看到），启动时静默清掉。
    """
    from pathlib import Path

    target_path = target if isinstance(target, Path) else Path(str(target))
    if not target_path.exists():
        return
    cleaned = 0
    try:
        for dotfile in target_path.rglob(".DS_Store"):
            try:
                dotfile.unlink()
                cleaned += 1
            except OSError:
                pass
    except OSError:
        return
    if cleaned:
        logger.info(".DS_Store sweep: removed %d files under %s", cleaned, target_path)


async def _run_db_migrations(db_path: Path) -> None:
    """P2.4：跑 schema migration，失败直接 RuntimeError。

    抽成独立函数让 lifespan 里只占一行调用，避免拉爆 PLR0915 阈值。
    """
    mig = await run_migrations(db_path)
    if mig.errors:
        logger.error("db migrations failed: %s", mig.errors)
        raise RuntimeError(f"db migrations failed: {mig.errors}")
    logger.info(
        "db migrations: applied=%s skipped=%s current_version=%d",
        mig.applied,
        mig.skipped,
        mig.current_version,
    )


async def _reconcile_rag_storage(settings: Settings) -> None:
    """Repair interrupted RAG ownership/storage work before workflow replay."""

    from app.upload.ownership import reconcile_rag_content_storage

    report = await reconcile_rag_content_storage(
        settings.db_path,
        settings.storage_dir,
    )
    if any(
        (
            report.released_acls,
            report.ready_acls_repaired,
            report.canonicalized_blobs,
            report.orphan_blobs_deleted,
            report.temp_files_deleted,
            report.gc_tombstones_restored,
            report.quota_scopes_rebuilt,
            report.projections_deleted,
        )
    ):
        logger.info("RAG storage reconciliation: %s", report)


def _bind_workflow_handlers_for_current_principal(
    settings: Settings,
    repository: RepositoryPort,
) -> None:
    """Register every resumable built-in handler before startup replay.

    Request modules still bind lazily for isolated unit tests, but process
    startup must not wait for an HTTP request before it can recover durable
    work.  Principal-scoped handlers are registered while the persisted owner
    context is bound; global handlers are naturally de-duplicated.
    """

    from app.api.admin import bind_meeting_export_workflow_handler
    from app.api.artifacts import bind_artifact_workflow_handler, get_skill
    from app.api.deps import (
        get_artifact_repository,
        get_event_bus,
        get_llm_singleton,
        get_workflow_dispatcher,
        get_workflow_service,
    )
    from app.api.diagnostics import bind_diagnostics_workflow_handler
    from app.api.meetings import (
        bind_meeting_workflow_handlers,
        bind_output_cleanup_workflow_handler,
        bind_share_workflow_handler,
        get_meeting_pipeline_for_lifespan,
    )
    from app.api.retrieval import (
        bind_rag_query_workflow_handler,
        bind_rag_workflow_handlers,
        get_rag,
        get_web,
    )
    from app.api.workspace import bind_workspace_workflow_handlers, get_scanner

    bus = get_event_bus()
    service = get_workflow_service(settings, bus)
    dispatcher = get_workflow_dispatcher(service)
    rag = get_rag(settings)
    llm = get_llm_singleton(settings)
    bind_rag_workflow_handlers(dispatcher, rag, settings)
    bind_rag_query_workflow_handler(
        dispatcher,
        settings=settings,
        main_llm=llm,
        rag=rag,
        web=get_web(settings),
    )
    bind_workspace_workflow_handlers(dispatcher, get_scanner(settings, rag), settings)
    bind_share_workflow_handler(dispatcher, get_session_store(settings))
    bind_diagnostics_workflow_handler(dispatcher, settings)
    bind_meeting_export_workflow_handler(
        dispatcher,
        repository,
        settings,
        get_artifact_repository(settings),
    )
    pipeline = get_meeting_pipeline_for_lifespan(settings, repository)
    bind_output_cleanup_workflow_handler(
        dispatcher,
        repository,
        settings,
        get_artifact_repository(settings),
        pipeline,
    )
    bind_artifact_workflow_handler(
        dispatcher,
        settings=settings,
        llm=llm,
        runner=get_skill(settings),
        event_bus=bus,
        artifact_repo=get_artifact_repository(settings),
    )
    bind_meeting_workflow_handlers(dispatcher, pipeline)


# 持有 lifespan 期间 fire-and-forget 任务的强引用，避免被 GC
_LIFESPAN_TASKS: set[asyncio.Task[None]] = set()
_meeting_state_for_shutdown: object | None = None


async def _repair_meeting_rag_projections_once(
    settings: Settings,
    repository: RepositoryPort,
) -> tuple[int, int]:
    """Repair every persisted principal scope without widening request reads."""

    scope_loader = getattr(repository, "list_rag_projection_scopes", None)
    if scope_loader is None:
        scope_loader = getattr(repository, "list_meeting_rag_projection_scopes", None)
    if scope_loader is None:
        return 0, 0
    attempted = 0
    succeeded = 0
    for tenant_id, device_id, owner_id in await scope_loader():
        principal = Principal(
            tenant_id=tenant_id,
            device_id=device_id,
            owner_id=owner_id,
            session_id=f"meeting-rag-repair:{owner_id}",
            mode="local" if owner_id == LEGACY_OWNER_ID else "public",
        )
        principal_token = bind_principal(principal)
        try:
            pipeline = get_meeting_pipeline_for_lifespan(settings, repository)
            scope_attempted, scope_succeeded = await pipeline.repair_rag_projections()
            attempted += scope_attempted
            succeeded += scope_succeeded
        finally:
            reset_principal(principal_token)
    return attempted, succeeded


async def _meeting_rag_projection_repair_loop(
    settings: Settings,
    repository: RepositoryPort,
) -> None:
    while True:
        try:
            attempted, succeeded = await _repair_meeting_rag_projections_once(
                settings,
                repository,
            )
            if attempted:
                logger.info(
                    "meeting RAG projection repair: attempted=%d succeeded=%d",
                    attempted,
                    succeeded,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("meeting RAG projection repair failed: %s", exc)
        await asyncio.sleep(settings.meeting_rag_repair_interval_s)


def _start_lifespan_task(coro: Coroutine[Any, Any, None], *, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _LIFESPAN_TASKS.add(task)
    task.add_done_callback(_LIFESPAN_TASKS.discard)


async def _stop_lifespan_tasks() -> None:
    tasks = list(_LIFESPAN_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _LIFESPAN_TASKS.clear()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # noqa: PLR0912, PLR0915
    global _meeting_state_for_shutdown  # noqa: PLW0603
    settings = get_settings()
    logger.info(
        "echodesk 启动: version=%s port=%d llm_main=%s llm_fast=%s stt=%s tts=%s",
        __version__,
        settings.port,
        settings.llm_main_model,
        settings.llm_fast_model,
        settings.stt_backend,
        settings.tts_provider if settings.tts_enabled else "off",
    )
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.rag_index_dir.mkdir(parents=True, exist_ok=True)
    # P1.8：清理 mac Finder 在 ~/.echodesk/ 里写的 .DS_Store
    _sweep_macos_dotfiles(user_config_dir())

    # P2.4：DB schema migration 必须在 repo.init / hydrate 之前跑完，
    # 否则 hydrate 会基于半成品 schema 读到旧字段。
    await _run_db_migrations(settings.db_path)
    await _reconcile_rag_storage(settings)
    configure_event_bus(settings)

    # SQLite repository：连接 + hydrate 未结束的会议 + 加载已知说话人
    repo = get_repository(settings)
    await repo.init()
    try:
        from app.api.deps import get_artifact_repository as _get_artifact_repo
        from app.artifacts.recovery import (
            recover_skill_build_artifacts,
            replay_succeeded_artifact_file_cleanups,
        )

        recovery = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=_get_artifact_repo(settings),
        )
        cleanup_replayed = await replay_succeeded_artifact_file_cleanups(settings)
        if (
            recovery.discovered
            or recovery.workflow_managed
            or recovery.abandoned_builds_cleaned
            or cleanup_replayed
        ):
            logger.info(
                "artifact recovery: discovered=%d recovered=%d linked=%d existing=%d "
                "skipped=%d workflow_managed=%d abandoned_cleaned=%d cleanup_replayed=%d",
                recovery.discovered,
                recovery.recovered,
                recovery.linked,
                recovery.already_recorded,
                recovery.skipped,
                recovery.workflow_managed,
                recovery.abandoned_builds_cleaned,
                cleanup_replayed,
            )
    except Exception as e:
        logger.warning("artifact recovery failed: %s", e)
    try:
        registry = get_speaker_registry(settings, repo)
        await registry.hydrate()
        n_speakers = len(registry.known_speaker_ids())
        if n_speakers:
            logger.info("speaker registry: hydrated %d known speakers", n_speakers)
    except Exception as e:
        logger.warning("speaker registry hydrate failed: %s", e)

    # ECAPA diarizer：从 speakers.embedding_blob 恢复内存 centroids + counter
    # 修 ARCH-AUDIT §4 root #1 #9（embedding 不再随重启丢光）
    try:
        diarizer = get_diarizer_singleton(settings, repo)
        hydrate_fn = getattr(diarizer, "hydrate", None)
        if callable(hydrate_fn):
            await hydrate_fn()
    except Exception as e:
        logger.warning("diarizer hydrate failed: %s", e)
    try:
        pipeline = get_meeting_pipeline_for_lifespan(settings, repo)
        n_resumed = await pipeline.hydrate_from_repo()
        if n_resumed:
            logger.info("hydrated %d in-progress meeting(s) from %s", n_resumed, settings.db_path)
    except Exception as e:
        logger.warning("meeting hydrate failed: %s", e)

    try:
        from app.api.deps import (
            get_auto_meeting_detector as _get_det,
        )
        from app.api.deps import (
            get_event_bus as _get_bus,
        )
        from app.api.deps import (
            get_meeting_state as _get_state,
        )

        detector = _get_det(settings)
        bus = _get_bus()
        state = _get_state(settings, repo, bus, detector)
        await state.hydrate()
        state.start_watchdog()
        _meeting_state_for_shutdown = state
        if state.current is not None:
            logger.info(
                "meeting-state hydrated: %s started_by=%s",
                state.current.meeting_id,
                state.current.started_by,
            )

        # fire-and-forget：上次 finalize 失败留下的「ended 但纪要空」会议，主动重试一次
        async def _bg_recover_minutes() -> None:
            try:
                n = await state.recover_stuck_minutes()
                if n:
                    logger.info("recover_stuck_minutes: attempted retry on %d meeting(s)", n)
            except Exception as e:
                logger.warning("recover_stuck_minutes failed: %s", e)

        rec_task = asyncio.create_task(_bg_recover_minutes())
        _LIFESPAN_TASKS.add(rec_task)
        rec_task.add_done_callback(_LIFESPAN_TASKS.discard)
    except Exception as e:
        logger.warning("meeting-state hydrate failed: %s", e)

    try:
        from app.api.deps import (
            get_event_bus as _get_bus,
        )
        from app.api.deps import (
            get_workflow_dispatcher as _get_dispatcher,
        )
        from app.api.deps import (
            get_workflow_service as _get_workflow,
        )

        workflow = _get_workflow(settings, _get_bus())
        workflow.start_outbox_poller()
        dispatcher = _get_dispatcher(workflow)
        dispatcher.start_recovery_reaper(
            prepare_current_scope=lambda: _bind_workflow_handlers_for_current_principal(
                settings,
                repo,
            )
        )
        principals = await workflow.list_unfinished_principals()
        # Bind local handlers even on a clean database so startup workspace
        # work and the first request use the same registry.
        local = local_principal()
        if not any(
            (item.tenant_id, item.device_id, item.owner_id)
            == (local.tenant_id, local.device_id, local.owner_id)
            for item in principals
        ):
            principals.insert(0, local_principal())
        restored = 0
        for principal in principals:
            principal_token = bind_principal(principal)
            try:
                _bind_workflow_handlers_for_current_principal(settings, repo)
                restored += await dispatcher.restore_unfinished()
            finally:
                reset_principal(principal_token)
        if restored:
            logger.info("workflow runs restored: %d", restored)
    except Exception as e:
        logger.warning("workflow restore failed: %s", e)

    try:
        from app.agents.service import get_agent_task_service
        from app.api.deps import get_event_bus as _get_bus

        agent_service = get_agent_task_service(settings, _get_bus())
        restored = 0
        for principal in await agent_service.list_unfinished_principals():
            principal_token = bind_principal(principal)
            try:
                restored += await agent_service.restore_unfinished()
            finally:
                reset_principal(principal_token)
        if restored:
            logger.info("agent task bridges restored: %d", restored)
    except Exception as e:
        logger.warning("agent task bridge restore failed: %s", e)

    _start_lifespan_task(
        _meeting_rag_projection_repair_loop(settings, repo),
        name="meeting-rag-projection-repair",
    )

    # 授权工作区：启动后 fire-and-forget 扫描（不阻塞 startup）
    if settings.workspace_scan_on_startup and settings.workspace_dirs_list:
        from app.api.deps import get_event_bus as _get_bus
        from app.api.deps import get_workflow_dispatcher as _get_dispatcher
        from app.api.deps import get_workflow_service as _get_workflow
        from app.api.workspace import _run_workspace_scan, get_scanner

        rag = get_rag(settings)
        scanner = get_scanner(settings, rag)
        dispatcher = _get_dispatcher(_get_workflow(settings, _get_bus()))

        async def _bg_scan() -> None:
            try:
                r = await _run_workspace_scan(dispatcher, scanner, source="startup")
                logger.info(
                    "workspace startup scan done: added=%d updated=%d skipped=%d removed=%d failed=%d",
                    r["n_added"],
                    r["n_updated"],
                    r["n_skipped"],
                    r["n_removed"],
                    r["n_failed"],
                )
            except Exception as e:
                logger.warning("workspace startup scan failed: %s", e)

        task = asyncio.create_task(_bg_scan())
        _LIFESPAN_TASKS.add(task)
        task.add_done_callback(_LIFESPAN_TASKS.discard)
        logger.info(
            "workspace startup scan kicked off: dirs=%s",
            [str(d) for d in settings.workspace_dirs_list],
        )

    # P1.4：启动远程依赖探针后台 task
    try:
        await start_prober()
    except Exception as e:
        logger.warning("health prober start failed: %s", e)

    await start_runtime_janitor(settings)
    _app.state.ready = True
    yield

    # 顺序：先停恢复器/执行器，再销毁它们依赖的 LLM、event bus 与 repository。
    _app.state.ready = False
    state_for_shutdown = _meeting_state_for_shutdown
    if state_for_shutdown is not None:
        stop_watchdog = getattr(state_for_shutdown, "stop_watchdog", None)
        if callable(stop_watchdog):
            await stop_watchdog()
        _meeting_state_for_shutdown = None
    await stop_prober()
    await stop_runtime_janitor()
    await _stop_lifespan_tasks()
    await aclose_agents()
    await aclose_workflow_service()
    await aclose_memory_service()
    await aclose_llm_singleton()
    await aclose_event_bus()
    await aclose_repository()
    logger.info("echodesk 关闭")


def _request_scope_path(request: Request) -> str:
    """Return the router-owned ASGI path, never a Host-derived URL path."""

    return route_scope_path(request.scope)


def _is_session_path(path: str) -> bool:
    return path == "/session" or path.startswith("/session/")


async def _reserve_request_upload(
    request: Request,
    *,
    settings: Settings,
    governor: PrincipalGovernor,
    principal: Principal,
) -> None:
    if (
        principal.mode != "public"
        or upload_body_limit(settings, _request_scope_path(request)) is None
    ):
        return
    declared_body_bytes = int(request.headers.get("content-length", "0"))
    if declared_body_bytes <= 0:
        return
    reservation = await governor.reserve_upload_bytes(principal, declared_body_bytes)
    request.state.upload_quota_reservation = reservation


async def _release_request_leases(
    request: Request,
    runtime_lease: RuntimeLease[tuple[str, str], ScopeRuntime] | None,
) -> None:
    reservation = getattr(request.state, "upload_quota_reservation", None)
    if isinstance(reservation, QuotaReservation):
        await reservation.release()
    if runtime_lease is not None:
        runtime_lease.release()


async def _guard_sse_body(
    body_iterator: AsyncIterator[Any],
    *,
    quota_context: Any,
    runtime_lease: RuntimeLease[tuple[str, str], ScopeRuntime] | None,
    runtime_registry: Any,
    principal: Principal,
) -> AsyncIterator[Any]:
    """Keep public request/runtime leases until the SSE iterator really ends."""

    stream_context_token = bind_principal(principal)
    try:
        async for chunk in body_iterator:
            yield chunk
    finally:
        try:
            await quota_context.__aexit__(None, None, None)
        finally:
            if runtime_lease is not None:
                runtime_lease.release()
            try:
                await runtime_registry.flush_closures()
            finally:
                reset_principal(stream_context_token)


def _defer_sse_lifecycle(
    response: Response,
    *,
    quota_context: Any,
    runtime_lease: RuntimeLease[tuple[str, str], ScopeRuntime] | None,
    runtime_registry: Any,
    principal: Principal,
) -> bool:
    body_iterator = getattr(response, "body_iterator", None)
    if (
        not response.headers.get("content-type", "").startswith("text/event-stream")
        or body_iterator is None
    ):
        return False
    cast(_StreamingBodyResponse, response).body_iterator = _guard_sse_body(
        cast(AsyncIterator[Any], body_iterator),
        quota_context=quota_context,
        runtime_lease=runtime_lease,
        runtime_registry=runtime_registry,
        principal=principal,
    )
    return True


def _include_api_routers(app: FastAPI) -> None:
    for api_router in (
        health_router,
        sessions_router,
        capture_router,
        chat_router,
        memory_router,
        retrieval_router,
        workspace_router,
        artifacts_router,
        workflows_router,
        meetings_router,
        speakers_router,
        intent_router,
        tts_router,
        agents_router,
        hub_router,
        ws_router,
    ):
        app.include_router(api_router)
    app.include_router(
        admin_router,
        prefix="/admin",
        tags=["admin"],
        dependencies=[Depends(require_admin_access)],
    )
    app.include_router(
        diagnostics_router,
        prefix="/admin",
        tags=["admin"],
        dependencies=[Depends(require_admin_access)],
    )


def _install_transport_guards(app: FastAPI, settings: Settings) -> None:
    """Install body, CORS and Host guards outside the policy middleware chain."""

    # UploadIngress sees body frames before request.form()/UploadFile invokes
    # Starlette's multipart parser.  The deployment gate blocks business body
    # IO while a target is being validated. CORS wraps both early responses so
    # clients can read them. TrustedHost is added last and is therefore
    # outermost, rejecting malformed Host headers before body IO.
    app.add_middleware(UploadIngressMiddleware, settings=settings)
    app.add_middleware(
        DeploymentGateMiddleware,
        gate_file=(settings.deployment_gate_file if settings.public_demo_mode else None),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[PUBLIC_MINIMUM_CLIENT_VERSION_HEADER, "Link"],
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_hosts_list,
    )


def _new_fastapi(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="EchoDesk",
        version=__version__,
        description="个人数字分身 · 会议+办公（API 后端）",
        lifespan=lifespan,
        docs_url=None if settings.public_demo_mode else "/docs",
        redoc_url=None if settings.public_demo_mode else "/redoc",
        openapi_url=None if settings.public_demo_mode else "/openapi.json",
    )
    app.state.ready = False
    return app


def _install_error_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        headers = dict(exc.headers or {})
        if settings.public_demo_mode and isinstance(exc, InternalHTTPException):
            if exc.status_code == 502:
                code = "upstream_unavailable"
                message = "上游服务暂时不可用，请稍后重试"
            else:
                code = "request_failed"
                message = "请求未能完成，请稍后重试"
            headers.update(PRIVATE_NO_STORE_HEADERS)
            logger.warning(
                "public internal error detail suppressed: status=%d path=%s",
                exc.status_code,
                request.url.path,
            )
            return JSONResponse(
                {"error": {"code": code, "message": message}},
                status_code=exc.status_code,
                headers=headers,
            )
        if not settings.public_demo_mode or exc.status_code < 500:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=headers,
            )

        if exc.status_code == 502:
            code = "upstream_unavailable"
            message = "上游服务暂时不可用，请稍后重试"
        elif exc.status_code in {503, 504}:
            code = "service_unavailable"
            message = "服务暂时不可用，请稍后重试"
        else:
            code = "internal_error"
            message = "请求未能完成，请稍后重试"
        headers.update(PRIVATE_NO_STORE_HEADERS)
        logger.warning(
            "public server error detail suppressed: status=%d path=%s",
            exc.status_code,
            request.url.path,
        )
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if not settings.public_demo_mode:
            return await request_validation_exception_handler(request, exc)
        logger.warning(
            "public request validation detail suppressed: path=%s error_count=%d",
            request.url.path,
            len(exc.errors()),
        )
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "请求参数无效，请检查后重试",
                }
            },
            status_code=422,
            headers=PRIVATE_NO_STORE_HEADERS,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        path = _request_scope_path(request)
        protect_private_response = settings.public_demo_mode or _is_session_path(path)
        logger.error(
            "unhandled request error suppressed: path=%s type=%s",
            path,
            type(exc).__name__,
            exc_info=exc,
        )
        if protect_private_response:
            return JSONResponse(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "请求未能完成，请稍后重试",
                    }
                },
                status_code=500,
                headers=PRIVATE_NO_STORE_HEADERS,
            )
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


def _bootstrap_payload(settings: Settings) -> dict[str, object]:
    capabilities: dict[str, object] = {
        "principal_sessions": True,
        "owner_isolation": True,
        "workflow_kernel": "dispatcher-v1",
        "ws_owner_filtering": True,
        "ws_stream_epoch": True,
        "ws_hello_bearer": settings.public_demo_mode,
        "server_resync_rehydrate_required": True,
        "host_runtime_requires_admin": settings.public_demo_mode,
    }
    response: dict[str, object] = {
        "schema_version": 1,
        "api_version": "0.3",
        "session_required": settings.public_demo_mode,
        "ws_path": "/ws/echo",
        "session_path": "/session",
        "capabilities": capabilities,
    }
    if settings.public_demo_mode:
        response["minimum_client_version"] = MINIMUM_PUBLIC_CLIENT_VERSION
    else:
        response.update(
            {
                "backend_version": __version__,
                "build_contract": backend_build_contract(),
                "ws_url": settings.public_ws_url,
                "http_url": settings.public_http_url,
                "app_version": settings.app_version,
                "stt_enabled": True,
                "tts_enabled": settings.tts_enabled,
                "diarizer_enabled": settings.diarizer_enabled,
                "web_search_enabled": settings.web_search_enabled,
            }
        )
    return response


async def _resolve_request_principal(
    request: Request,
    access_policy: AccessPolicy,
    policy_path: str,
) -> Principal | JSONResponse:
    client_key = access_policy.client_host(request.client)
    preauth_lease = None
    session_body_lease = None
    try:
        try:
            session_body_lease = await access_policy.admit_session_body(
                method=request.method,
                path=policy_path,
                client_key=client_key,
            )
            preauth_lease = await access_policy.admit_http(client_key)
        except PreAuthAdmissionError as exc:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={"Retry-After": str(exc.retry_after_s)},
            )
        try:
            access_policy.require_allowed_origin(
                request.headers.getlist("origin"),
                client_host=client_key,
            )
            principal = await access_policy.resolve_http_principal(
                method=request.method,
                path=policy_path,
                client_host=client_key,
                authorization=request.headers.get("Authorization", ""),
                x_echo_admin_token=request.headers.get("X-Echo-Admin-Token", ""),
                sync_token=request.headers.get("X-Echo-Sync-Token", ""),
                share_token=request.query_params.get("share", ""),
                client_version=request.headers.get(PUBLIC_CLIENT_VERSION_HEADER, ""),
            )
            if session_body_lease is not None:
                request.state.session_body_admission_lease = session_body_lease
                session_body_lease = None
            return principal
        except AccessPolicyError as exc:
            if exc.status_code == 426:
                return JSONResponse(
                    {
                        "detail": exc.detail,
                        "error": {
                            "code": "client_upgrade_required",
                            "minimum_client_version": MINIMUM_PUBLIC_CLIENT_VERSION,
                            "upgrade_url": PUBLIC_CLIENT_UPGRADE_URL,
                        },
                    },
                    status_code=exc.status_code,
                    headers={
                        **PRIVATE_NO_STORE_HEADERS,
                        PUBLIC_MINIMUM_CLIENT_VERSION_HEADER: (MINIMUM_PUBLIC_CLIENT_VERSION),
                        "Link": f'<{PUBLIC_CLIENT_UPGRADE_URL}>; rel="upgrade"',
                    },
                )
            if exc.status_code == 401 and exc.detail == "session required":
                return JSONResponse(
                    {
                        "detail": exc.detail,
                        "error": {
                            "code": "session_required",
                            "minimum_client_version": MINIMUM_PUBLIC_CLIENT_VERSION,
                            "upgrade_url": PUBLIC_CLIENT_UPGRADE_URL,
                        },
                    },
                    status_code=exc.status_code,
                    headers={
                        **PRIVATE_NO_STORE_HEADERS,
                        PUBLIC_MINIMUM_CLIENT_VERSION_HEADER: (MINIMUM_PUBLIC_CLIENT_VERSION),
                        "Link": f'<{PUBLIC_CLIENT_UPGRADE_URL}>; rel="upgrade"',
                    },
                )
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        except SessionError:
            return JSONResponse(
                {"detail": "invalid or expired authorization"},
                status_code=401,
            )
    finally:
        if preauth_lease is not None:
            await preauth_lease.release()
        if session_body_lease is not None:
            await session_body_lease.release()


def create_app() -> FastAPI:  # noqa: PLR0915 - application composition root
    settings = get_settings()
    access_policy = get_access_policy(settings, get_session_store(settings))
    governor = get_quota_governor(settings)
    runtime_registry = get_scope_runtime_registry(settings)
    # P1.3：取代 basicConfig，让日志同时进文件
    _setup_logging(settings.log_level)

    app = _new_fastapi(settings)
    _install_error_handlers(app, settings)

    @app.middleware("http")
    async def bind_request_identity(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        policy_path = _request_scope_path(request)
        resolved = await _resolve_request_principal(request, access_policy, policy_path)
        if isinstance(resolved, JSONResponse):
            return resolved
        principal = resolved
        request.state.principal = principal
        session_body_admission_lease: _AsyncReleaseLease | None = getattr(
            request.state,
            "session_body_admission_lease",
            None,
        )
        context_token = bind_principal(principal)
        runtime_lease = None
        quota_context = governor.request(
            principal,
            method=request.method,
            path=policy_path,
        )
        quota_entered = False
        response: Response | None = None
        try:
            await _reserve_request_upload(
                request,
                settings=settings,
                governor=governor,
                principal=principal,
            )
            runtime_lease = runtime_registry.acquire((principal.tenant_id, principal.owner_id))
            await quota_context.__aenter__()
            quota_entered = True
            response = await call_next(request)

            # BaseHTTPMiddleware returns the response as soon as headers are
            # available.  SSE work continues in body_iterator, so transferring
            # both leases to the iterator is required for concurrent-request,
            # expensive-task and scoped-runtime bounds to cover real work.
            if _defer_sse_lifecycle(
                response,
                quota_context=quota_context,
                runtime_lease=runtime_lease,
                runtime_registry=runtime_registry,
                principal=principal,
            ):
                runtime_lease = None
                quota_entered = False
        except QuotaExceeded as exc:
            response = JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": "resource quota exceeded",
                        "metric": exc.metric,
                        "limit": exc.limit,
                        "used": exc.used,
                    }
                },
                status_code=429,
                headers={"Retry-After": str(exc.retry_after_s)},
            )
        except RuntimeCapacityExceeded:
            response = JSONResponse(
                {
                    "error": {
                        "code": "runtime_capacity_exceeded",
                        "message": "server runtime capacity is temporarily full",
                    }
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        finally:
            if quota_entered:
                await quota_context.__aexit__(None, None, None)
            if session_body_admission_lease is not None:
                await session_body_admission_lease.release()
            await _release_request_leases(request, runtime_lease)
            # A meeting/task reaching a terminal state does not terminate the
            # principal runtime. Diarizer, speaker registry, ambient capture and
            # other meetings share this bounded scope and remain live until the
            # normal LRU/idle-TTL janitor evicts it.
            await runtime_registry.flush_closures()
            reset_principal(context_token)
        return response

    @app.middleware("http")
    async def restrict_lan_api_access(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        client_host = access_policy.client_host(request.client)
        policy_path = _request_scope_path(request)
        if access_policy.is_lan_request_allowed(
            method=request.method,
            path=policy_path,
            client_host=client_host,
        ):
            response = await call_next(request)
            content_type = response.headers.get("content-type", "")
            if request.method in {"GET", "HEAD"} and "application/json" in content_type:
                response.headers.setdefault("Cache-Control", "no-store")
            return response
        return PlainTextResponse("EchoDesk LAN share only", status_code=403)

    _install_transport_guards(app, settings)

    @app.middleware("http")
    async def protect_session_responses(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if _is_session_path(_request_scope_path(request)):
            apply_private_no_store(response.headers)
        return response

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        if settings.public_demo_mode:
            return {"status": "ok"}
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["meta"])
    async def readyz(request: Request) -> Response:
        ready = bool(getattr(request.app.state, "ready", False))
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    @app.get("/bootstrap", tags=["meta"])
    async def bootstrap() -> dict[str, object]:
        return _bootstrap_payload(settings)

    _include_api_routers(app)
    return app


app = create_app()
