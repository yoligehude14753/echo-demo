"""HTTP API: 授权工作区（M6）。

GET  /workspace/status   — 配置 / 已索引文件数 / 上次扫描时间
POST /workspace/scan     — 触发一次全量扫描（增量同步）
POST /workspace/clear    — 清空 workspace 来源的索引（不影响 upload/meeting）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.adapters.rag.workspace_scanner import WorkspaceScanner
from app.api.retrieval import get_rag
from app.config import Settings, get_settings
from app.ports.rag import RagPort

router = APIRouter(prefix="/workspace", tags=["workspace"])


_scanner_singleton: WorkspaceScanner | None = None


def get_scanner(
    settings: Settings = Depends(get_settings),
    rag: RagPort = Depends(get_rag),
) -> WorkspaceScanner:
    global _scanner_singleton  # noqa: PLW0603
    if _scanner_singleton is None:
        _scanner_singleton = WorkspaceScanner(settings, rag)
    return _scanner_singleton


def reset_singleton() -> None:
    """测试用。"""
    global _scanner_singleton  # noqa: PLW0603
    _scanner_singleton = None


@router.get("/status")
async def workspace_status(
    scanner: WorkspaceScanner = Depends(get_scanner),
) -> dict[str, object]:
    return await scanner.status()


@router.post("/scan")
async def workspace_scan(
    scanner: WorkspaceScanner = Depends(get_scanner),
) -> dict[str, object]:
    r = await scanner.scan()
    return {
        "n_total": r.n_total,
        "n_added": r.n_added,
        "n_updated": r.n_updated,
        "n_removed": r.n_removed,
        "n_skipped": r.n_skipped,
        "n_failed": r.n_failed,
        "duration_s": r.duration_s,
        "errors": r.errors[:10],
    }


@router.post("/clear")
async def workspace_clear(
    scanner: WorkspaceScanner = Depends(get_scanner),
) -> dict[str, int]:
    n = await scanner.clear()
    return {"n_removed": n}
