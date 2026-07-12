"""授权工作区扫描器（M6）。

- 用户在 settings.workspace_dirs 配置可索引目录范围（多目录逗号分隔）
- 启动时（可选）扫描全部目录，按支持的扩展名筛选 → 通用 ingest_file
- 增量策略：state file schema 2 记录 files + pending_cleanup
  · 每轮流式读取完整 SHA-256；仅 digest 相同才跳过
  · 文件变化 → 先持久化旧 doc cleanup intent，删除成功后再 ingest 新 doc
  · 文件消失 / clear → 先持久化 cleanup intent，再删除 doc
  · 删除失败保留 attempts/last_error，后续 scan/clear 重试
  · 新文件 → ingest_file

不阻塞主循环：在 FastAPI startup 中 fire-and-forget；失败容错（单文件失败不影响其他）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat as stat_module
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.rag.parsers import SUPPORTED_EXTS
from app.config import Settings
from app.ports.rag import RagPort

log = logging.getLogger("echodesk.workspace")

_STATE_SCHEMA_VERSION = 2
_DIGEST_CHUNK_BYTES = 1 << 20


class WorkspaceStateError(RuntimeError):
    """Existing workspace cursor evidence is unreadable or structurally corrupt."""


def _fsync_directory(path: Path) -> None:
    """Durably persist a preceding rename on POSIX filesystems."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@dataclass
class _FileState:
    source_path: str
    mtime: float
    size: int
    doc_id: str
    ingested_at: float
    digest: str = ""


@dataclass
class _PendingCleanup:
    doc_id: str
    source_path: str
    reason: str
    queued_at: float
    attempts: int = 0
    last_error: str = ""
    last_attempt_at: float | None = None


@dataclass
class _WorkspaceState:
    files: dict[str, _FileState] = field(default_factory=dict)
    pending_cleanup: dict[str, _PendingCleanup] = field(default_factory=dict)


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

    @staticmethod
    def _decode_files(raw: object) -> dict[str, _FileState]:
        if not isinstance(raw, dict):
            raise WorkspaceStateError("workspace state files must be an object")
        files: dict[str, _FileState] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise WorkspaceStateError(f"invalid workspace file row: {key!r}")
            try:
                files[key] = _FileState(
                    source_path=str(value.get("source_path") or key),
                    mtime=float(value["mtime"]),
                    size=int(value["size"]),
                    doc_id=str(value["doc_id"]),
                    ingested_at=float(value["ingested_at"]),
                    # Legacy rows contain a head-only SHA-1. It cannot prove the
                    # full file is unchanged, so leave digest empty and force a
                    # one-time full-digest reconciliation.
                    digest=str(value.get("digest") or ""),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkspaceStateError(f"invalid workspace file row {key}: {exc}") from exc
        return files

    @staticmethod
    def _decode_pending_cleanup(raw: object) -> dict[str, _PendingCleanup]:
        if not isinstance(raw, dict):
            raise WorkspaceStateError("workspace pending cleanup must be an object")
        pending: dict[str, _PendingCleanup] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise WorkspaceStateError(f"invalid workspace cleanup row: {key!r}")
            try:
                doc_id = str(value.get("doc_id") or key)
                if not doc_id:
                    raise ValueError("missing doc_id")
                pending[doc_id] = _PendingCleanup(
                    doc_id=doc_id,
                    source_path=str(value.get("source_path") or ""),
                    reason=str(value.get("reason") or "unknown"),
                    queued_at=float(value.get("queued_at") or 0.0),
                    attempts=max(0, int(value.get("attempts") or 0)),
                    last_error=str(value.get("last_error") or ""),
                    last_attempt_at=(
                        float(value["last_attempt_at"])
                        if value.get("last_attempt_at") is not None
                        else None
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise WorkspaceStateError(f"invalid workspace cleanup row {key}: {exc}") from exc
        return pending

    def _load_workspace_state(self) -> _WorkspaceState:
        try:
            serialized = self._state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _WorkspaceState()
        except OSError as exc:
            raise WorkspaceStateError(f"workspace state unreadable: {exc}") from exc
        try:
            raw = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise WorkspaceStateError(f"workspace state JSON corrupt: {exc}") from exc
        if not isinstance(raw, dict):
            raise WorkspaceStateError("workspace state root is not an object")
        schema_version = raw.get("schema_version")
        if schema_version == _STATE_SCHEMA_VERSION:
            return _WorkspaceState(
                files=self._decode_files(raw.get("files")),
                pending_cleanup=self._decode_pending_cleanup(raw.get("pending_cleanup")),
            )
        if schema_version is not None:
            raise WorkspaceStateError(
                f"unsupported workspace state schema: {schema_version!r}; refusing to lose cleanup evidence"
            )
        # Schema 1 was the bare source_path -> FileState mapping. Unknown
        # top-level metadata without an explicit schema is not interpreted as
        # a file row by _decode_files.
        return _WorkspaceState(files=self._decode_files(raw))

    def _load_state(self) -> dict[str, _FileState]:
        """Compatibility helper for existing diagnostics/tests."""
        return self._load_workspace_state().files

    def _save_workspace_state(self, state: _WorkspaceState) -> None:
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "files": {k: asdict(v) for k, v in state.files.items()},
            "pending_cleanup": {
                doc_id: asdict(cleanup) for doc_id, cleanup in state.pending_cleanup.items()
            },
        }
        target_fd, target_name = tempfile.mkstemp(
            prefix=f".{self._state_file.name}.",
            suffix=".tmp",
            dir=self._state_file.parent,
        )
        tmp = Path(target_name)
        try:
            with os.fdopen(target_fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, self._state_file)
            _fsync_directory(self._state_file.parent)
        except Exception:
            with suppress(OSError):
                os.close(target_fd)
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("workspace state temp cleanup failed: %s", exc)
            raise

    async def _save_workspace_state_off_loop(self, state: _WorkspaceState) -> None:
        """Commit state off-loop without releasing the scanner lock mid-write."""

        worker = asyncio.create_task(asyncio.to_thread(self._save_workspace_state, state))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            # asyncio.to_thread cannot stop a running filesystem operation.
            # Keep the owning scan/clear task alive (and therefore keep
            # ``self._lock`` held) until the writer is terminal. Otherwise a
            # subsequent scan can observe or overwrite stale cleanup evidence.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                try:
                    worker.result()
                except Exception as exc:
                    log.warning("workspace state save failed during cancellation: %s", exc)
            raise cancelled

    @staticmethod
    def _open_snapshot_source(path: Path) -> int:
        """Open the canonical regular file and reject symlink/swap races."""

        before = os.lstat(path)
        if not stat_module.S_ISREG(before.st_mode):
            raise ValueError("workspace snapshot source is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, flags)
        try:
            opened = os.fstat(source_fd)
            after = os.lstat(path)
            if (
                not stat_module.S_ISREG(opened.st_mode)
                or not stat_module.S_ISREG(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or path.resolve(strict=True) != path
            ):
                raise ValueError("workspace snapshot source changed or escaped authorization")
            return source_fd
        except Exception:
            os.close(source_fd)
            raise

    def _secure_digest(self, path: Path) -> tuple[str, int]:
        """Bounded read-only digest for the unchanged-file fast path."""

        source_fd = self._open_snapshot_source(path)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(source_fd, "rb") as source:
            while chunk := source.read(_DIGEST_CHUNK_BYTES):
                total += len(chunk)
                if total > self._max_bytes:
                    raise ValueError(
                        f"workspace file grew beyond {self._max_bytes} byte limit during digest"
                    )
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}", total

    def _snapshot_for_ingest(self, path: Path) -> tuple[Path, str, int]:
        """Copy one immutable parser input while hashing the exact same bytes."""

        source_fd = self._open_snapshot_source(path)
        try:
            target_fd, target_name = tempfile.mkstemp(
                prefix="echodesk-workspace-",
                suffix=path.suffix.lower(),
            )
        except Exception:
            os.close(source_fd)
            raise
        target = Path(target_name)
        digest = hashlib.sha256()
        copied = 0
        try:
            with os.fdopen(source_fd, "rb") as source, os.fdopen(target_fd, "wb") as output:
                while chunk := source.read(_DIGEST_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > self._max_bytes:
                        raise ValueError(
                            f"workspace file grew beyond {self._max_bytes} byte limit during snapshot"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("workspace snapshot cleanup failed after copy error: %s", exc)
            raise
        return target, f"sha256:{digest.hexdigest()}", copied

    async def _snapshot_for_ingest_off_loop(self, path: Path) -> tuple[Path, str, int]:
        """Create a snapshot off-loop and synchronously clean it if cancellation wins."""

        worker = asyncio.create_task(asyncio.to_thread(self._snapshot_for_ingest, path))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            # Shutdown cancellation must not abandon a worker that may still
            # create a plaintext temp file. Suppress further cancellation only
            # until the worker reaches a terminal state, then delete its result.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                try:
                    created = worker.result()
                except Exception:
                    pass
                else:
                    try:
                        await asyncio.to_thread(created[0].unlink, missing_ok=True)
                    except OSError as exc:
                        log.warning(
                            "workspace snapshot cleanup failed during cancellation: %s",
                            exc,
                        )
            raise cancelled

    @staticmethod
    def _queue_cleanup(
        state: _WorkspaceState,
        file_state: _FileState,
        *,
        reason: str,
    ) -> _PendingCleanup:
        cleanup = state.pending_cleanup.get(file_state.doc_id)
        if cleanup is None:
            cleanup = _PendingCleanup(
                doc_id=file_state.doc_id,
                source_path=file_state.source_path,
                reason=reason,
                queued_at=time.time(),
            )
            state.pending_cleanup[file_state.doc_id] = cleanup
        return cleanup

    async def _attempt_cleanup(
        self,
        state: _WorkspaceState,
        cleanup: _PendingCleanup,
        *,
        result: WorkspaceScanResult | None = None,
    ) -> bool:
        try:
            await self._rag.delete(cleanup.doc_id)
        except Exception as exc:
            cleanup.attempts += 1
            cleanup.last_attempt_at = time.time()
            cleanup.last_error = str(exc)
            message = f"cleanup {cleanup.reason} {cleanup.source_path} ({cleanup.doc_id}): {exc}"
            if result is not None:
                result.errors.append(message)
                result.n_failed += 1
            log.warning("workspace %s", message)
            return False
        state.pending_cleanup.pop(cleanup.doc_id, None)
        return True

    def list_authorized_dirs(self) -> list[Path]:
        return [d for d in self._settings.workspace_dirs_list if d.exists() and d.is_dir()]

    def _iter_files(  # noqa: PLR0912, PLR0915 - explicit fail-closed traversal gates
        self,
    ) -> tuple[list[Path], list[tuple[Path, str]]]:
        """返回 ``(valid_files, iter_errors)``。

        历史问题：原来逐文件 ``p.stat()`` / ``p.relative_to(root)`` 任一抛错（macOS
        权限文件夹、stale symlink、特殊文件名等）整个 rglob 循环挂掉，但 scanner
        没有任何日志 —— 表现为"目录里部分文件被静默吞"。现在按文件粒度 try/except，
        失败的文件作为 ``iter_errors`` 返回，``_scan_impl`` 累计到 ``result.n_failed``
        并写 errors 列表，确保 UI 看到 ``failed=K``。
        """
        out: list[Path] = []
        errors: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for root in self._settings.workspace_dirs_list:
            try:
                resolved_root = root.resolve(strict=True)
            except OSError as exc:
                log.warning("workspace root resolve failed on %s: %s", root, exc)
                errors.append((root, f"root resolve: {exc}"))
                continue
            if not resolved_root.is_dir():
                # ``Path.rglob`` returns an empty iterator for a regular file.
                # Treating that as a clean empty traversal would classify every
                # previously indexed source below this root as deleted.  A root
                # whose filesystem type changed is incomplete evidence, not a
                # destructive source-of-truth update.
                log.warning("workspace root is no longer a directory: %s", root)
                errors.append((root, "root is not a directory"))
                continue
            try:
                walker = root.rglob("*")
            except OSError as e:
                log.warning("workspace rglob failed on %s: %s", root, e)
                errors.append((root, f"rglob: {e}"))
                continue
            iterator = iter(walker)
            while True:
                try:
                    p = next(iterator)
                except StopIteration:
                    break
                except OSError as exc:
                    log.warning("workspace traversal failed on %s: %s", root, exc)
                    errors.append((root, f"traversal: {exc}"))
                    break
                try:
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in SUPPORTED_EXTS:
                        continue
                    # 排除点开头的隐藏目录/系统文件（.git, .DS_Store, .venv 等）
                    if any(part.startswith(".") for part in p.relative_to(root).parts):
                        continue
                    resolved = p.resolve(strict=True)
                    try:
                        resolved_relative = resolved.relative_to(resolved_root)
                    except ValueError:
                        raise ValueError(
                            f"resolved target escapes authorized workspace root {resolved_root}"
                        ) from None
                    if any(part.startswith(".") for part in resolved_relative.parts):
                        continue
                    if resolved.stat().st_size > self._max_bytes:
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    out.append(resolved)
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
        try:
            state = await asyncio.to_thread(self._load_workspace_state)
        except WorkspaceStateError as exc:
            result.errors.append(str(exc))
            result.n_failed = 1
            result.duration_s = round(time.monotonic() - t0, 3)
            log.warning("workspace scan refused unreadable state %s: %s", self._state_file, exc)
            return result
        current_files, iter_errors = await asyncio.to_thread(self._iter_files)
        current_paths = {str(p) for p in current_files}
        result.n_total = len(current_files)
        # 遍历期就失败的文件（权限 / 坏 symlink 等）单独计入 failed，避免"被静默丢"
        for bad_path, err in iter_errors:
            result.errors.append(f"iter {bad_path}: {err}")
            result.n_failed += 1

        # 1. Reconcile authoritative workspace documents that have no local
        # cursor (for example a crash after ingest but before final state save).
        # Persist every orphan handle before any delete so missing source files
        # are repaired too, not only paths that happen to remain on disk.
        try:
            rag_docs = await self._rag.list_docs()
        except Exception as exc:
            result.errors.append(f"list authoritative workspace docs: {exc}")
            result.n_failed += 1
            result.duration_s = round(time.monotonic() - t0, 3)
            log.warning("workspace authoritative reconciliation failed closed: %s", exc)
            return result
        tracked_doc_ids = {file_state.doc_id for file_state in state.files.values()} | set(
            state.pending_cleanup
        )
        authoritative_doc_ids = {
            str(raw_doc.get("doc_id") or "")
            for raw_doc in rag_docs
            if raw_doc.get("source") == "workspace" and raw_doc.get("doc_id")
        }
        for file_state in state.files.values():
            if file_state.doc_id not in authoritative_doc_ids:
                # The cursor cannot prove an index that the authoritative RAG
                # manifest no longer contains. Empty digest forces repair.
                file_state.digest = ""
        queued_orphans = False
        for raw_doc in rag_docs:
            if raw_doc.get("source") != "workspace":
                continue
            doc_id = str(raw_doc.get("doc_id") or "")
            if not doc_id or doc_id in tracked_doc_ids:
                continue
            state.pending_cleanup[doc_id] = _PendingCleanup(
                doc_id=doc_id,
                source_path=str(raw_doc.get("source_path") or ""),
                reason="orphan_reconcile",
                queued_at=time.time(),
            )
            tracked_doc_ids.add(doc_id)
            queued_orphans = True
        if queued_orphans:
            try:
                await self._save_workspace_state_off_loop(state)
            except OSError as exc:
                result.errors.append(f"save orphan cleanup intents: {exc}")
                result.n_failed += 1
                result.duration_s = round(time.monotonic() - t0, 3)
                log.warning("workspace save orphan cleanup intents failed closed: %s", exc)
                return result

        # 2. 先重试上轮持久化的 cleanup。成功后即使进程在 save 前崩溃，
        # 下轮重复 delete 也安全；失败则 attempts/last_error 继续保留。
        cleaned_replacements: set[str] = set()
        failed_replacements: set[str] = set()
        for cleanup in list(state.pending_cleanup.values()):
            if await self._attempt_cleanup(state, cleanup, result=result):
                if cleanup.reason == "replaced":
                    cleaned_replacements.add(cleanup.doc_id)
                else:
                    result.n_removed += 1
            elif cleanup.reason == "replaced":
                failed_replacements.add(cleanup.doc_id)

        # 3. 消失文件先把 cleanup intent 和 cursor 移除原子落盘；只有 intent
        # 已 durable 才执行 delete，避免 delete 失败/进程崩溃后彻底丢失 doc_id。
        # A partial traversal cannot distinguish a removed file from one hidden
        # by a transient permission/stat/walker failure. Preserve every
        # existing cursor and retry deletion on the next clean scan.
        gone = [] if iter_errors else [key for key in state.files if key not in current_paths]
        queued_gone: list[_PendingCleanup] = []
        for key in gone:
            file_state = state.files.pop(key)
            queued_gone.append(self._queue_cleanup(state, file_state, reason="source_missing"))
        gone_intents_durable = True
        if queued_gone:
            try:
                await self._save_workspace_state_off_loop(state)
            except OSError as exc:
                gone_intents_durable = False
                result.errors.append(f"save cleanup intents: {exc}")
                result.n_failed += 1
                log.warning("workspace save cleanup intents failed: %s", exc)
        if gone_intents_durable:
            for cleanup in queued_gone:
                if await self._attempt_cleanup(state, cleanup, result=result):
                    result.n_removed += 1

        # 4. 新增 / 更新
        blocked_paths = {
            cleanup.source_path
            for cleanup in state.pending_cleanup.values()
            if cleanup.reason != "replaced"
        }

        async def discard_snapshot(snapshot: Path, source: Path) -> None:
            try:
                await asyncio.to_thread(snapshot.unlink, missing_ok=True)
            except OSError as exc:
                result.errors.append(f"snapshot cleanup {source}: {exc}")
                result.n_failed += 1
                log.warning("workspace snapshot cleanup failed: %s → %s", source, exc)

        for path in current_files:
            key = str(path)
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError as e:
                result.errors.append(f"stat {path}: {e}")
                result.n_failed += 1
                log.warning("workspace stat failed: %s → %s", path, e)
                continue
            mtime = stat.st_mtime
            size = stat.st_size
            prev = state.files.get(key)
            if prev is None and key in blocked_paths:
                # source_missing/clear 的旧 doc 尚未删掉，不能再 ingest 一份制造
                # 临时重复；本轮 cleanup failure 已计入 n_failed/errors。
                continue
            if prev and prev.doc_id in failed_replacements:
                # This scan already retried and recorded the replacement
                # cleanup failure. Do not hammer the same backend twice or try
                # an ingest that the unique source_path constraint will reject.
                continue

            if prev and prev.doc_id not in cleaned_replacements:
                try:
                    digest, size = await asyncio.to_thread(self._secure_digest, path)
                except (OSError, ValueError) as exc:
                    result.errors.append(f"digest {path}: {exc}")
                    result.n_failed += 1
                    log.warning("workspace secure digest failed: %s → %s", path, exc)
                    continue
                if prev.digest == digest:
                    state.files[key] = _FileState(
                        source_path=key,
                        mtime=mtime,
                        size=size,
                        digest=digest,
                        doc_id=prev.doc_id,
                        ingested_at=prev.ingested_at,
                    )
                    result.n_skipped += 1
                    continue

            snapshot_path: Path | None = None
            try:
                snapshot_path, digest, size = await self._snapshot_for_ingest_off_loop(path)
            except (OSError, ValueError) as exc:
                result.errors.append(f"snapshot {path}: {exc}")
                result.n_failed += 1
                log.warning("workspace immutable snapshot failed: %s → %s", path, exc)
                continue
            try:
                if prev and prev.digest == digest and prev.doc_id not in cleaned_replacements:
                    # mtime 变化但完整内容未变（如 touch）只刷新 cursor。
                    state.files[key] = _FileState(
                        source_path=key,
                        mtime=mtime,
                        size=size,
                        digest=digest,
                        doc_id=prev.doc_id,
                        ingested_at=prev.ingested_at,
                    )
                    result.n_skipped += 1
                    continue

                if prev and prev.doc_id not in cleaned_replacements:
                    cleanup = self._queue_cleanup(state, prev, reason="replaced")
                    # The manifest has a unique source_path constraint, so the old
                    # document must be deleted before replacement ingest. Persist
                    # its id first: a failed delete or crash can then retry without
                    # losing the only handle to the orphan.
                    try:
                        await self._save_workspace_state_off_loop(state)
                    except OSError as exc:
                        result.errors.append(f"save replacement cleanup {path}: {exc}")
                        result.n_failed += 1
                        log.warning(
                            "workspace save replacement cleanup failed: %s → %s",
                            path,
                            exc,
                        )
                        continue
                    if not await self._attempt_cleanup(state, cleanup, result=result):
                        continue

                if prev:
                    # If ingest fails after the old delete, an empty digest forces
                    # the next scan to retry even when the source later reverts to
                    # its previous bytes. The obsolete doc_id remains an idempotent
                    # delete target, not proof that an index document still exists.
                    state.files[key] = _FileState(
                        source_path=prev.source_path,
                        mtime=prev.mtime,
                        size=prev.size,
                        digest="",
                        doc_id=prev.doc_id,
                        ingested_at=prev.ingested_at,
                    )

                try:
                    doc_id = await self._rag.ingest_file(
                        str(snapshot_path),
                        doc_title=path.stem,
                        source="workspace",
                        source_path=key,
                        operation_id=f"workspace:{key}:{digest}:{size}",
                    )
                    state.files[key] = _FileState(
                        source_path=key,
                        mtime=mtime,
                        size=size,
                        digest=digest,
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
            finally:
                await discard_snapshot(snapshot_path, path)

        try:
            await self._save_workspace_state_off_loop(state)
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
            state = await asyncio.to_thread(self._load_workspace_state)
            # The RAG manifest is authoritative: include documents created
            # before a state-save crash, even when no local cursor survived.
            authoritative_docs = await self._rag.list_docs()
            for raw_doc in authoritative_docs:
                if raw_doc.get("source") != "workspace":
                    continue
                doc_id = str(raw_doc.get("doc_id") or "")
                if not doc_id or doc_id in state.pending_cleanup:
                    continue
                state.pending_cleanup[doc_id] = _PendingCleanup(
                    doc_id=doc_id,
                    source_path=str(raw_doc.get("source_path") or ""),
                    reason="workspace_clear",
                    queued_at=time.time(),
                )
            # Clear intent must be durable before any destructive delete.
            for file_state in state.files.values():
                self._queue_cleanup(state, file_state, reason="workspace_clear")
            state.files.clear()
            await self._save_workspace_state_off_loop(state)
            n = 0
            for cleanup in list(state.pending_cleanup.values()):
                if await self._attempt_cleanup(state, cleanup):
                    n += 1
            await self._save_workspace_state_off_loop(state)
            return n
