"""授权工作区扫描器（M6）。

- 用户在 settings.workspace_dirs 配置可索引目录范围（多目录逗号分隔）
- 启动时（可选）扫描全部目录，按支持的扩展名筛选 → 通用 ingest_file
- 增量策略：state file 记录 {source_path: {mtime, size, sha1, doc_id}}
  · 文件不变（mtime + size 都匹配）→ 跳过
  · 文件变化 → delete(doc_id) + ingest_file
  · 文件消失 → delete(doc_id) 并从 state 移除
  · 新文件 → ingest_file

不阻塞主循环：在 FastAPI startup 中 fire-and-forget；失败容错（单文件失败不影响其他）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.rag.parsers import SUPPORTED_EXTS
from app.config import Settings
from app.ports.rag import RagPort

log = logging.getLogger("echodesk.workspace")


@dataclass
class _FileState:
    source_path: str
    mtime: float
    size: int
    doc_id: str
    ingested_at: float
    sha1: str = ""


@dataclass
class WorkspaceScanResult:
    n_total: int = 0
    n_added: int = 0
    n_updated: int = 0
    n_removed: int = 0
    n_skipped: int = 0
    n_failed: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


class WorkspaceScanner:
    """扫描 settings.workspace_dirs，把支持类型的文件 ingest 到 RAG。"""

    def __init__(self, settings: Settings, rag: RagPort) -> None:
        self._settings = settings
        self._rag = rag
        self._state_file = Path(settings.workspace_state_file).expanduser()
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = int(settings.workspace_max_file_mb * 1024 * 1024)
        self._lock = asyncio.Lock()

    def _load_state(self) -> dict[str, _FileState]:
        if not self._state_file.exists():
            return {}
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            log.warning("workspace state file corrupt, ignoring")
            return {}
        return {k: _FileState(**v) for k, v in raw.items()}

    def _save_state(self, state: dict[str, _FileState]) -> None:
        payload = {k: asdict(v) for k, v in state.items()}
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    @staticmethod
    def _sha1_head(path: Path, max_bytes: int = 1 << 20) -> str:
        """读前 1MB 算 sha1（增量判断够用，不读整文件省 I/O）。"""
        h = hashlib.sha1()
        try:
            with path.open("rb") as f:
                h.update(f.read(max_bytes))
        except OSError as e:
            # 读不出 sha1 不算致命（fall back 走 mtime/size 增量），但要记一行
            log.warning("workspace sha1_head read failed: %s → %s", path, e)
            return ""
        return h.hexdigest()

    def list_authorized_dirs(self) -> list[Path]:
        return [d for d in self._settings.workspace_dirs_list if d.exists() and d.is_dir()]

    def _iter_files(self) -> tuple[list[Path], list[tuple[Path, str]]]:
        """返回 ``(valid_files, iter_errors)``。

        历史问题：原来逐文件 ``p.stat()`` / ``p.relative_to(root)`` 任一抛错（macOS
        权限文件夹、stale symlink、特殊文件名等）整个 rglob 循环挂掉，但 scanner
        没有任何日志 —— 表现为"目录里部分文件被静默吞"。现在按文件粒度 try/except，
        失败的文件作为 ``iter_errors`` 返回，``_scan_impl`` 累计到 ``result.n_failed``
        并写 errors 列表，确保 UI 看到 ``failed=K``。
        """
        out: list[Path] = []
        errors: list[tuple[Path, str]] = []
        for root in self.list_authorized_dirs():
            try:
                walker = root.rglob("*")
            except OSError as e:
                log.warning("workspace rglob failed on %s: %s", root, e)
                errors.append((root, f"rglob: {e}"))
                continue
            for p in walker:
                try:
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in SUPPORTED_EXTS:
                        continue
                    # 排除点开头的隐藏目录/系统文件（.git, .DS_Store, .venv 等）
                    if any(part.startswith(".") for part in p.relative_to(root).parts):
                        continue
                    if p.stat().st_size > self._max_bytes:
                        continue
                    out.append(p.resolve())
                except (OSError, ValueError) as e:
                    log.warning("workspace iter skip file %s: %s", p, e)
                    errors.append((p, f"iter: {e}"))
                    continue
        return out, errors

    async def scan(self) -> WorkspaceScanResult:
        """全量扫描 + 增量同步。返回统计。"""
        async with self._lock:
            return await self._scan_impl()

    async def _scan_impl(self) -> WorkspaceScanResult:  # noqa: PLR0912, PLR0915
        t0 = time.monotonic()
        result = WorkspaceScanResult()
        state = self._load_state()
        current_files, iter_errors = self._iter_files()
        current_paths = {str(p) for p in current_files}
        result.n_total = len(current_files)
        # 遍历期就失败的文件（权限 / 坏 symlink 等）单独计入 failed，避免"被静默丢"
        for bad_path, err in iter_errors:
            result.errors.append(f"iter {bad_path}: {err}")
            result.n_failed += 1

        # 1. 移除已消失的文件
        gone = [k for k in state if k not in current_paths]
        for k in gone:
            try:
                await self._rag.delete(state[k].doc_id)
                result.n_removed += 1
            except Exception as e:
                result.errors.append(f"delete {k}: {e}")
                result.n_failed += 1
                log.warning("workspace delete failed: %s → %s", k, e)
            state.pop(k, None)

        # 2. 新增 / 更新
        for path in current_files:
            key = str(path)
            try:
                stat = path.stat()
            except OSError as e:
                result.errors.append(f"stat {path}: {e}")
                result.n_failed += 1
                log.warning("workspace stat failed: %s → %s", path, e)
                continue
            mtime = stat.st_mtime
            size = stat.st_size
            prev = state.get(key)
            if prev and prev.mtime == mtime and prev.size == size:
                result.n_skipped += 1
                continue

            sha1 = self._sha1_head(path)
            if prev and prev.sha1 == sha1 and prev.size == size:
                # mtime 变了但内容没变（如 touch）
                state[key] = _FileState(
                    source_path=key,
                    mtime=mtime,
                    size=size,
                    sha1=sha1,
                    doc_id=prev.doc_id,
                    ingested_at=prev.ingested_at,
                )
                result.n_skipped += 1
                continue

            try:
                # 若存在旧 doc，先删除再入库（覆盖更新）
                if prev:
                    try:
                        await self._rag.delete(prev.doc_id)
                    except Exception as e:
                        # 老 doc 删除失败不阻塞新 ingest，但要让用户看到
                        log.warning(
                            "workspace delete old doc %s before re-ingest %s failed: %s",
                            prev.doc_id,
                            path.name,
                            e,
                        )
                doc_id = await self._rag.ingest_file(
                    str(path),
                    doc_title=path.stem,
                    source="workspace",
                    source_path=key,
                    operation_id=f"workspace:{key}:{sha1}:{size}",
                )
                state[key] = _FileState(
                    source_path=key,
                    mtime=mtime,
                    size=size,
                    sha1=sha1,
                    doc_id=doc_id,
                    ingested_at=time.time(),
                )
                if prev:
                    result.n_updated += 1
                else:
                    result.n_added += 1
            except Exception as e:
                result.errors.append(f"ingest {path.name}: {e}")
                result.n_failed += 1
                log.warning("workspace ingest failed: %s → %s", path, e)

        try:
            self._save_state(state)
        except OSError as e:
            # 状态文件写不进去意味着下一轮全量重新 ingest，必须可见
            result.errors.append(f"save_state: {e}")
            result.n_failed += 1
            log.warning("workspace save_state failed: %s → %s", self._state_file, e)
        result.duration_s = round(time.monotonic() - t0, 3)
        log.info(
            "workspace scan: total=%d added=%d updated=%d removed=%d skipped=%d failed=%d in %.2fs",
            result.n_total,
            result.n_added,
            result.n_updated,
            result.n_removed,
            result.n_skipped,
            result.n_failed,
            result.duration_s,
        )
        if result.errors:
            log.warning(
                "workspace scan errors (showing up to 10 of %d): %s",
                len(result.errors),
                result.errors[:10],
            )
        return result

    async def status(self) -> dict[str, Any]:
        dirs = self.list_authorized_dirs()
        # The state file is only an incremental-scan cursor. It can legitimately
        # outlive a rebuilt/cleared owner-scoped RAG index, so exposing its row
        # count as "indexed documents" makes the UI contradict /rag/docs.
        # Report the authoritative, current-scope RAG projection instead.
        docs = await self._rag.list_docs()
        n_indexed = sum(1 for doc in docs if doc.get("source") == "workspace")
        return {
            "configured_dirs": [str(p) for p in self._settings.workspace_dirs_list],
            "authorized_dirs": [str(p) for p in dirs],
            "n_indexed": n_indexed,
            "max_file_mb": self._settings.workspace_max_file_mb,
            "scan_on_startup": self._settings.workspace_scan_on_startup,
        }

    async def clear(self) -> int:
        """清空 workspace 索引（保留 upload/meeting 来源的 doc）。"""
        async with self._lock:
            state = self._load_state()
            n = 0
            for fs in state.values():
                try:
                    await self._rag.delete(fs.doc_id)
                    n += 1
                except Exception as e:
                    log.warning(
                        "workspace clear: delete doc %s failed: %s",
                        fs.doc_id,
                        e,
                    )
            self._save_state({})
            return n
