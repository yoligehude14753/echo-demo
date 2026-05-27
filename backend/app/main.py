"""FastAPI 入口：仅做装配，不写业务逻辑。

启动：
    cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.artifacts import router as artifacts_router
from app.api.capture import router as capture_router
from app.api.chat import router as chat_router
from app.api.deps import aclose_event_bus, aclose_llm_singleton
from app.api.intent import router as intent_router
from app.api.meetings import router as meetings_router
from app.api.retrieval import get_rag
from app.api.retrieval import router as retrieval_router
from app.api.workspace import router as workspace_router
from app.api.ws import router as ws_router
from app.config import get_settings

logger = logging.getLogger("echodesk")

# 持有 lifespan 期间 fire-and-forget 任务的强引用，避免被 GC
_LIFESPAN_TASKS: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "echodesk 启动: version=%s llm_main=%s llm_fast=%s stt=%s tts=%s",
        __version__,
        settings.llm_main_model,
        settings.llm_fast_model,
        settings.stt_backend,
        settings.tts_provider if settings.tts_enabled else "off",
    )
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.rag_index_dir.mkdir(parents=True, exist_ok=True)

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

    yield
    await aclose_llm_singleton()
    await aclose_event_bus()
    logger.info("echodesk 关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

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

    app.include_router(capture_router)
    app.include_router(chat_router)
    app.include_router(retrieval_router)
    app.include_router(workspace_router)
    app.include_router(artifacts_router)
    app.include_router(meetings_router)
    app.include_router(intent_router)
    app.include_router(ws_router)
    return app


app = create_app()
