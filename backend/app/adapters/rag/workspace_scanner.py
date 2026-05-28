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
import contextlib
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
        except Exception:
            return ""
        return h.hexdigest()

    def list_authorized_dirs(self) -> list[Path]:
        return [d for d in self._settings.workspace_dirs_list if d.exists() and d.is_dir()]

    def _iter_files(self) -> tuple[list[Path], list[tuple[Path, int]]]:
        """扫描可索引文件 + 超过大小限制的文件（用于上报 failed）。

        P11 修复（2026-05-28）：以前超大文件 `continue` 静默跳过 →
        n_total/n_failed 都不算它们，用户看到 `added=2 failed=0` 但实际
        8 个文件只有 2 个真入库。改成返回 oversized 列表，scan() 把它们
        计入 n_failed + errors 让 UI 能看见原因。
        """
        ok: list[Path] = []
        oversized: list[tuple[Path, int]] = []
        for root in self.list_authorized_dirs():
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                if any(part.startswith(".") for part in p.relative_to(root).parts):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size > self._max_bytes:
                    oversized.append((p.resolve(), size))
                    continue
                ok.append(p.resolve())
        return ok, oversized

    async def scan(self) -> WorkspaceScanResult:
        """全量扫描 + 增量同步。返回统计。"""
        async with self._lock:
            return await self._scan_impl()

    async def _scan_impl(self) -> WorkspaceScanResult:
        t0 = time.monotonic()
        result = WorkspaceScanResult()
        state = self._load_state()
        current_files, oversized = self._iter_files()
        current_paths = {str(p) for p in current_files}
        # n_total 包含 oversized（让用户能看到"扫了 8 个但有 2 个超限"，
        # 而不是误以为目录只有 6 个候选文件）。
        result.n_total = len(current_files) + len(oversized)

        # 0. 超大文件：不 ingest 但要报告 failed，附原因 + 大小，让用户知道
        # 可以调 workspace_max_file_mb 或分割文件。这是 P11 静默漏文件 bug 的核心修复点。
        max_mb = self._settings.workspace_max_file_mb
        for p, size in oversized:
            size_mb = round(size / 1024 / 1024, 1)
            msg = f"oversized {p.name}: {size_mb} MB > {max_mb} MB cap"
            result.errors.append(msg)
            result.n_failed += 1
            log.warning("workspace skip oversized: %s (%.1f MB > %.0f MB)", p, size_mb, max_mb)

        # 1. 移除已消失的文件
        gone = [k for k in state if k not in current_paths]
        for k in gone:
            try:
                await self._rag.delete(state[k].doc_id)
                result.n_removed += 1
            except Exception as e:
                result.errors.append(f"delete {k}: {e}")
                result.n_failed += 1
            state.pop(k, None)

        # 2. 新增 / 更新
        for path in current_files:
            key = str(path)
            stat = path.stat()
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
                    with contextlib.suppress(Exception):
                        await self._rag.delete(prev.doc_id)
                doc_id = await self._rag.ingest_file(
                    str(path),
                    doc_title=path.stem,
                    source="workspace",
                    source_path=key,
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

        self._save_state(state)
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
        return result

    async def status(self) -> dict[str, Any]:
        dirs = self.list_authorized_dirs()
        state = self._load_state()
        return {
            "configured_dirs": [str(p) for p in self._settings.workspace_dirs_list],
            "authorized_dirs": [str(p) for p in dirs],
            "n_indexed": len(state),
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
                except Exception:
                    pass
            self._save_state({})
            return n
