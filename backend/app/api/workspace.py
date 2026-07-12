"""HTTP API: 授权工作区（M6 + P4-fix-rag-chat UX 收口）。

GET  /workspace/status     — 配置 / 已索引文件数 / 上次扫描时间
POST /workspace/scan       — 触发一次全量扫描（增量同步）
POST /workspace/clear      — 清空 workspace 来源的索引（不影响 upload/meeting）
POST /workspace/add-dir    — 把一个目录追加到 workspace_dirs（持久化 + 立即扫描）
POST /workspace/remove-dir — 把一个目录从 workspace_dirs 摘掉（不删 RAG 数据）

P4-fix-rag-chat（2026-05-28）：用户痛点是 workspace_dirs 配置入口隐藏在
~/.echodesk/config.json 里、字段名不直观、改完还要重启 backend。新增 add-dir
endpoint 让 SettingsPanel GUI（dialog.showOpenDialog → POST）一键完成；后端
持久化到 user.json 同时原地更新 settings.workspace_dirs，scanner 下次 scan
立即生效，不需要重启。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.adapters.rag.workspace_scanner import WorkspaceScanner
from app.api.deps import get_workflow_dispatcher, require_admin_access
from app.api.retrieval import get_rag
from app.config import Settings, get_settings
from app.config_io import write_user_config_json
from app.ports.rag import RagPort
from app.schemas.workflow import WorkflowRunCreate
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError

logger = logging.getLogger("echodesk.workspace")

router = APIRouter(
    prefix="/workspace",
    tags=["workspace"],
    dependencies=[Depends(require_admin_access)],
)


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


def bind_workspace_workflow_handlers(
    dispatcher: WorkflowDispatcher,
    scanner: WorkspaceScanner,
    settings: Settings | None = None,
) -> None:
    async def scan_handler(context: WorkflowContext, _payload: dict[str, Any]) -> dict[str, Any]:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        result = await scanner.scan()
        return {
            "n_total": result.n_total,
            "n_added": result.n_added,
            "n_updated": result.n_updated,
            "n_removed": result.n_removed,
            "n_skipped": result.n_skipped,
            "n_failed": result.n_failed,
            "duration_s": result.duration_s,
            "errors": result.errors[:10],
        }

    async def clear_handler(context: WorkflowContext, _payload: dict[str, Any]) -> dict[str, Any]:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        return {"n_removed": await scanner.clear()}

    if dispatcher.registry.resolve("workspace.scan") is None:
        dispatcher.registry.register("workspace.scan", scan_handler)
    if dispatcher.registry.resolve("workspace.clear") is None:
        dispatcher.registry.register("workspace.clear", clear_handler)
    if settings is not None:

        async def config_handler(
            context: WorkflowContext,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            if context.cancel_event.is_set():
                raise asyncio.CancelledError
            new_csv = str(payload["workspace_dirs"])
            write_user_config_json({"workspace_dirs": new_csv})
            settings.workspace_dirs = new_csv
            scan_run = await dispatcher.dispatch(
                WorkflowRunCreate(
                    kind="workspace.scan",
                    source=str(payload["source"]),
                    intent_text="Scan workspace after durable configuration update",
                    timeout_s=600,
                    active_key="workspace.scan",
                )
            )
            return {
                "workspace_dirs": new_csv,
                "scan_run_id": scan_run.run_id,
            }

        for kind in ("workspace.config.add", "workspace.config.remove"):
            if dispatcher.registry.resolve(kind) is None:
                dispatcher.registry.register(kind, config_handler)


async def _run_workspace_scan(
    dispatcher: WorkflowDispatcher,
    scanner: WorkspaceScanner,
    *,
    source: str,
) -> dict[str, object]:
    bind_workspace_workflow_handlers(dispatcher, scanner)
    done = await dispatcher.execute(
        WorkflowRunCreate(
            kind="workspace.scan",
            source=source,
            intent_text="Scan authorized workspace directories",
            timeout_s=600,
            active_key="workspace.scan",
        )
    )
    return done.output


@router.get("/status")
async def workspace_status(
    scanner: WorkspaceScanner = Depends(get_scanner),
) -> dict[str, object]:
    return await scanner.status()


@router.post("/scan")
async def workspace_scan(
    scanner: WorkspaceScanner = Depends(get_scanner),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
) -> dict[str, object]:
    try:
        return await _run_workspace_scan(dispatcher, scanner, source="workspace_api")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/clear")
async def workspace_clear(
    scanner: WorkspaceScanner = Depends(get_scanner),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
) -> dict[str, int]:
    bind_workspace_workflow_handlers(dispatcher, scanner)
    try:
        done = await dispatcher.execute(
            WorkflowRunCreate(
                kind="workspace.clear",
                source="workspace_api",
                intent_text="Clear workspace RAG documents",
                timeout_s=120,
                active_key="workspace.clear",
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"n_removed": int(done.output["n_removed"])}


class _DirRequest(BaseModel):
    path: str


def _parsed_dirs(csv: str) -> list[Path]:
    """把 workspace_dirs CSV 解析成绝对路径列表（去重 + expanduser + resolve）。

    用于 add-dir / remove-dir 比较 / 去重；resolve 让 "~/Documents" 跟
    "/Users/foo/Documents" 视为同一项。
    """
    out: list[Path] = []
    seen: set[str] = set()
    for raw in csv.split(","):
        s = raw.strip()
        if not s:
            continue
        try:
            p = Path(s).expanduser().resolve(strict=False)
        except OSError:
            continue
        if str(p) in seen:
            continue
        seen.add(str(p))
        out.append(p)
    return out


def _dirs_to_csv(dirs: list[Path]) -> str:
    return ",".join(str(d) for d in dirs)


@router.post("/add-dir")
async def workspace_add_dir(
    body: _DirRequest,
    settings: Settings = Depends(get_settings),
    scanner: WorkspaceScanner = Depends(get_scanner),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
) -> dict[str, object]:
    """把一个目录加入 workspace_dirs：持久化 user.json + 原地更新 settings + fire-and-forget scan。

    返回值：
      ``{added: bool, path: str, configured_dirs: [str], message: str}``

    ``added=False`` 的合法场景：该路径已经在配置里（幂等）。HTTP 仍 200。

    400 / 422 错误：
      - path 为空 / 非字符串 → 422（pydantic）
      - 路径不存在或不是目录 → 400
      - resolve 失败（permission denied 等）→ 400
    """
    raw = body.path.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path 不能为空")
    try:
        p = Path(raw).expanduser().resolve(strict=False)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"路径解析失败: {e}") from e
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"目录不存在: {p}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {p}")

    # 1. 合并到 dirs 列表（去重）
    existing = _parsed_dirs(settings.workspace_dirs)
    if p in existing:
        return {
            "added": False,
            "path": str(p),
            "configured_dirs": [str(d) for d in existing],
            "message": "该目录已在 workspace_dirs 中",
        }
    new_dirs = [*existing, p]
    new_csv = _dirs_to_csv(new_dirs)

    # 2. 先落 durable config run；handler 幂等写配置并在同一执行路径创建 scan run。
    bind_workspace_workflow_handlers(dispatcher, scanner, settings)
    digest = hashlib.sha256(new_csv.encode()).hexdigest()
    try:
        configured = await dispatcher.execute(
            WorkflowRunCreate(
                kind="workspace.config.add",
                source="workspace_add_dir",
                intent_text=f"Add workspace directory {p}",
                input={"workspace_dirs": new_csv, "source": "workspace_add_dir"},
                timeout_s=30,
                active_key=f"workspace.config.add:{digest}",
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    scan_run_id = str(configured.output["scan_run_id"])
    logger.info("workspace add-dir: %s; current dirs=%s", p, new_csv)

    async def _bg_scan() -> None:
        try:
            done = await dispatcher.wait_succeeded(scan_run_id)
            output = done.output
            logger.info(
                "workspace add-dir scan: added=%d updated=%d skipped=%d removed=%d failed=%d",
                output["n_added"],
                output["n_updated"],
                output["n_skipped"],
                output["n_removed"],
                output["n_failed"],
            )
        except Exception as e:
            logger.warning("workspace add-dir scan failed: %s", e)

    asyncio.create_task(_bg_scan())  # noqa: RUF006 - fire-and-forget by design

    return {
        "added": True,
        "path": str(p),
        "configured_dirs": [str(d) for d in new_dirs],
        "workflow_run_id": scan_run_id,
        "config_workflow_run_id": configured.run_id,
        "message": "已加入工作区目录，正在后台扫描索引…",
    }


@router.post("/remove-dir")
async def workspace_remove_dir(
    body: _DirRequest,
    settings: Settings = Depends(get_settings),
    scanner: WorkspaceScanner = Depends(get_scanner),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
) -> dict[str, object]:
    """把一个目录从 workspace_dirs 摘掉。

    设计：不主动删除已索引的 doc。下次 scan 会发现该目录文件"消失"
    → WorkspaceScanner 走标准的 "gone files → rag.delete" 路径。
    若用户想立刻清空，调 ``POST /workspace/clear``。
    """
    raw = body.path.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path 不能为空")
    try:
        target = Path(raw).expanduser().resolve(strict=False)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"路径解析失败: {e}") from e

    existing = _parsed_dirs(settings.workspace_dirs)
    if target not in existing:
        return {
            "removed": False,
            "path": str(target),
            "configured_dirs": [str(d) for d in existing],
            "message": "该目录不在 workspace_dirs 中",
        }
    new_dirs = [d for d in existing if d != target]
    new_csv = _dirs_to_csv(new_dirs)

    bind_workspace_workflow_handlers(dispatcher, scanner, settings)
    digest = hashlib.sha256(new_csv.encode()).hexdigest()
    try:
        configured = await dispatcher.execute(
            WorkflowRunCreate(
                kind="workspace.config.remove",
                source="workspace_remove_dir",
                intent_text=f"Remove workspace directory {target}",
                input={"workspace_dirs": new_csv, "source": "workspace_remove_dir"},
                timeout_s=30,
                active_key=f"workspace.config.remove:{digest}:{hashlib.sha256(str(target).encode()).hexdigest()[:12]}",
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    scan_run_id = str(configured.output["scan_run_id"])
    logger.info("workspace remove-dir: %s; current dirs=%s", target, new_csv)

    async def _bg_scan() -> None:
        try:
            await dispatcher.wait_succeeded(scan_run_id)
        except Exception as e:
            logger.warning("workspace remove-dir scan failed: %s", e)

    asyncio.create_task(_bg_scan())  # noqa: RUF006

    return {
        "removed": True,
        "path": str(target),
        "configured_dirs": [str(d) for d in new_dirs],
        "workflow_run_id": scan_run_id,
        "config_workflow_run_id": configured.run_id,
        "message": "已从工作区目录移除（已索引的文件将在下次扫描时清理）",
    }
