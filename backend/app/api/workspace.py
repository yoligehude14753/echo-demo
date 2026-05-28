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
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.adapters.rag.workspace_scanner import WorkspaceScanner
from app.api.retrieval import get_rag
from app.config import Settings, get_settings
from app.config_io import write_user_config_json
from app.ports.rag import RagPort

logger = logging.getLogger("echodesk.workspace")

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

    # 2. 持久化到 ~/.echodesk/config.json（merge 模式只动 workspace_dirs 字段）
    try:
        write_user_config_json({"workspace_dirs": new_csv})
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"写入 user config 失败: {e}",
        ) from e

    # 3. 原地更新 live settings 实例（lru_cache 单例，所有 get_settings 调用立即看到新值）
    settings.workspace_dirs = new_csv
    logger.info("workspace add-dir: %s; current dirs=%s", p, new_csv)

    # 4. fire-and-forget 扫描；不阻塞 HTTP 响应（10MB+ 文件夹可能跑 10+s）
    async def _bg_scan() -> None:
        try:
            r = await scanner.scan()
            logger.info(
                "workspace add-dir scan: added=%d updated=%d skipped=%d removed=%d failed=%d",
                r.n_added,
                r.n_updated,
                r.n_skipped,
                r.n_removed,
                r.n_failed,
            )
        except Exception as e:
            logger.warning("workspace add-dir scan failed: %s", e)

    asyncio.create_task(_bg_scan())  # noqa: RUF006 - fire-and-forget by design

    return {
        "added": True,
        "path": str(p),
        "configured_dirs": [str(d) for d in new_dirs],
        "message": "已加入工作区目录，正在后台扫描索引…",
    }


@router.post("/remove-dir")
async def workspace_remove_dir(
    body: _DirRequest,
    settings: Settings = Depends(get_settings),
    scanner: WorkspaceScanner = Depends(get_scanner),
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

    try:
        write_user_config_json({"workspace_dirs": new_csv})
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"写入 user config 失败: {e}",
        ) from e
    settings.workspace_dirs = new_csv
    logger.info("workspace remove-dir: %s; current dirs=%s", target, new_csv)

    # 触发 scan 让 scanner 把"消失"的文件清掉
    async def _bg_scan() -> None:
        try:
            await scanner.scan()
        except Exception as e:
            logger.warning("workspace remove-dir scan failed: %s", e)

    asyncio.create_task(_bg_scan())  # noqa: RUF006

    return {
        "removed": True,
        "path": str(target),
        "configured_dirs": [str(d) for d in new_dirs],
        "message": "已从工作区目录移除（已索引的文件将在下次扫描时清理）",
    }
