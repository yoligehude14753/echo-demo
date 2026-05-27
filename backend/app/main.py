"""FastAPI 入口：仅做装配，不写业务逻辑。

启动（canonical）：
    cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8769

注：8769 是 EchoDesk 统一端口（P1.1 Phase 1 收口），main.cjs / runtime.ts
/ vite.config / playwright 配置 / install-backend.sh 都对齐这个值。改前先
确认所有地方一起改。
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.adapters.repo.migrator import run_migrations
from app.api.artifacts import router as artifacts_router
from app.api.capture import router as capture_router
from app.api.chat import router as chat_router
from app.api.deps import (
    aclose_event_bus,
    aclose_llm_singleton,
    aclose_repository,
    get_diarizer_singleton,
    get_repository,
    get_speaker_registry,
)
from app.api.health import router as health_router
from app.api.health import start_prober, stop_prober
from app.api.intent import router as intent_router
from app.api.meetings import get_meeting_pipeline_for_lifespan
from app.api.meetings import router as meetings_router
from app.api.retrieval import get_rag
from app.api.retrieval import router as retrieval_router
from app.api.speakers import router as speakers_router
from app.api.tts import router as tts_router
from app.api.workspace import router as workspace_router
from app.api.ws import router as ws_router
from app.config import get_settings
from app.config_io import user_config_dir

logger = logging.getLogger("echodesk")


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

    fmt = logging.Formatter(
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


# 持有 lifespan 期间 fire-and-forget 任务的强引用，避免被 GC
_LIFESPAN_TASKS: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # noqa: PLR0915
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

    # SQLite repository：连接 + hydrate 未结束的会议 + 加载已知说话人
    repo = get_repository(settings)
    await repo.init()
    try:
        registry = get_speaker_registry(repo)
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
        if state.current is not None:
            logger.info(
                "meeting-state hydrated: %s started_by=%s",
                state.current.meeting_id,
                state.current.started_by,
            )
    except Exception as e:
        logger.warning("meeting-state hydrate failed: %s", e)

    # 授权工作区：启动后 fire-and-forget 扫描（不阻塞 startup）
    if settings.workspace_scan_on_startup and settings.workspace_dirs_list:
        from app.adapters.rag.workspace_scanner import WorkspaceScanner

        rag = get_rag(settings)
        scanner = WorkspaceScanner(settings, rag)

        async def _bg_scan() -> None:
            try:
                r = await scanner.scan()
                logger.info(
                    "workspace startup scan done: added=%d updated=%d skipped=%d removed=%d failed=%d",
                    r.n_added,
                    r.n_updated,
                    r.n_skipped,
                    r.n_removed,
                    r.n_failed,
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

    yield

    # 顺序：探针先停（避免拿 settings 时 lifespan 已经清完）→ LLM → bus → repo
    await stop_prober()
    await aclose_llm_singleton()
    await aclose_event_bus()
    await aclose_repository()
    logger.info("echodesk 关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    # P1.3：取代 basicConfig，让日志同时进文件
    _setup_logging(settings.log_level)

    app = FastAPI(
        title="EchoDesk",
        version=__version__,
        description="个人数字分身 · 会议+办公（API 后端）",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/bootstrap", tags=["meta"])
    async def bootstrap() -> dict[str, str | bool]:
        return {
            "ws_url": settings.public_ws_url,
            "http_url": settings.public_http_url,
            "app_version": settings.app_version,
            "stt_enabled": True,
            "tts_enabled": settings.tts_enabled,
            "diarizer_enabled": settings.diarizer_enabled,
            "web_search_enabled": settings.web_search_enabled,
        }

    app.include_router(health_router)
    app.include_router(capture_router)
    app.include_router(chat_router)
    app.include_router(retrieval_router)
    app.include_router(workspace_router)
    app.include_router(artifacts_router)
    app.include_router(meetings_router)
    app.include_router(speakers_router)
    app.include_router(intent_router)
    app.include_router(tts_router)
    app.include_router(ws_router)
    return app


app = create_app()
