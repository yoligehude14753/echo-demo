"""Atomic ownership, quota and physical lifecycle for RAG upload bytes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import TypeVar
from uuid import uuid4

import aiosqlite

from app.adapters.repo.connection import configure_aiosqlite_connection
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseToken
from app.security.governor import QuotaExceeded
from app.security.models import Principal

log = logging.getLogger("echodesk.rag.ownership")

_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,20}$")
_GC_TOMBSTONE_RE = re.compile(
    r"^\.(?P<original>[0-9a-f]{64}(?:\.[A-Za-z0-9]{1,20})?)\."
    r"[0-9a-f]{32}\.rag-gc$"
)
_RAG_ACTIVITY_RESOURCE_RE = re.compile(r"^(?P<digest>[0-9a-f]{64}):(?P<activity_id>[0-9a-f]{32})$")
_RAG_ACTIVITY_KINDS = ("rag-upload", "rag-view")
_RAG_ACTIVITY_LEASE_TTL_S = 30.0
_RAG_ACTIVITY_HEARTBEAT_S = 5.0
_RECONCILE_BATCH_SIZE = 128
_ACTIVE_WORKFLOW_STATES = frozenset({"pending", "running", "cancel_requested"})
_FAILED_WORKFLOW_STATES = frozenset({"failed", "timeout", "cancelled", "cancel_failed"})


class RagContentOwnershipError(RuntimeError):
    """The durable ACL and the requested RAG operation disagree."""


@dataclass(frozen=True, slots=True)
class RagContentClaim:
    created: bool
    content_hash: str
    size_bytes: int
    workflow_run_id: str
    file_suffix: str
    state: str
    doc_id: str | None
    quota_managed: bool

    def __bool__(self) -> bool:
        """Keep the historical ``if claim`` meaning: a newly charged ACL."""

        return self.created


@dataclass(frozen=True, slots=True)
class RagContentRelease:
    content_hash: str | None
    released_bytes: int
    remaining_owners: int
    physical_deleted: bool

    def __int__(self) -> int:
        return self.released_bytes


@dataclass(frozen=True, slots=True)
class RagContentReconcileReport:
    released_acls: int = 0
    ready_acls_repaired: int = 0
    canonicalized_blobs: int = 0
    orphan_blobs_deleted: int = 0
    temp_files_deleted: int = 0
    gc_tombstones_restored: int = 0
    quota_scopes_rebuilt: int = 0
    projections_deleted: int = 0


@dataclass(frozen=True, slots=True)
class _BlobObservation:
    valid: bool
    actual_size: int | None = None
    canonicalized_suffix: str | None = None
    canonicalized: int = 0


_T = TypeVar("_T")


class _RagActivityGuard:
    """Heartbeat one durable RAG activity and fail closed if its fence is lost."""

    def __init__(self, db_path: Path | str, token: LeaseToken) -> None:
        self._store = ExecutionLeaseStore(db_path)
        self._token = token
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task[object] | None = None
        self._lost_error: BaseException | None = None
        self._retain_until_expiry = False

    def retain_until_expiry(self) -> None:
        """Leave the current term as a short hand-off barrier after success."""

        self._retain_until_expiry = True

    async def __aenter__(self) -> _RagActivityGuard:
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - an async context always has a task
            raise RuntimeError("RAG activity must run inside an asyncio task")
        self._owner_task = owner
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(),
            name=f"rag-activity-heartbeat:{self._token.resource_kind}:{self._token.holder_id}",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        if not self._retain_until_expiry:
            try:
                await self._store.release(self._token)
            except Exception as release_error:  # expiry still provides crash cleanup
                log.warning(
                    "RAG activity lease release deferred kind=%s resource=%s: %s",
                    self._token.resource_kind,
                    self._token.resource_id,
                    release_error,
                )
        if self._lost_error is not None and (
            exc_type is None or issubclass(exc_type, asyncio.CancelledError)
        ):
            raise RagContentOwnershipError("RAG activity lease was lost") from self._lost_error
        return False

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(_RAG_ACTIVITY_HEARTBEAT_S)
                renewed = await self._store.renew(
                    self._token,
                    ttl_seconds=_RAG_ACTIVITY_LEASE_TTL_S,
                )
                if renewed is None:
                    raise RagContentOwnershipError("RAG activity fence expired")
                self._token = renewed
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._lost_error = exc
            if self._owner_task is not None:
                self._owner_task.cancel()


async def _await_blocking(call: Callable[[], _T]) -> _T:
    """Let a worker-thread file operation finish before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def rag_staging_root(storage_dir: Path | str) -> Path:
    return (Path(storage_dir).expanduser() / "workflow-inputs" / "rag").resolve()


def rag_blob_path(storage_dir: Path | str, content_hash: str) -> Path:
    return rag_staging_root(storage_dir) / _validate_content_hash(content_hash)


def _validate_content_hash(content_hash: str) -> str:
    normalized = content_hash.strip().lower()
    if _CONTENT_HASH_RE.fullmatch(normalized) is None:
        raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
    return normalized


def _validate_file_suffix(file_suffix: str) -> str:
    normalized = file_suffix.strip().lower()
    if _FILE_SUFFIX_RE.fullmatch(normalized) is None:
        raise ValueError("file_suffix must be one safe lowercase extension")
    return normalized


async def _open_connection(db_path: Path | str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(Path(db_path).expanduser()))
    await configure_aiosqlite_connection(conn)
    conn.row_factory = aiosqlite.Row
    return conn


async def _fetch_claim(
    conn: aiosqlite.Connection,
    principal: Principal,
    content_hash: str,
) -> aiosqlite.Row | None:
    cursor = await conn.execute(
        """SELECT content_hash, size_bytes, workflow_run_id, file_suffix, state,
                  doc_id, quota_managed
           FROM rag_content_owners
           WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
        (principal.tenant_id, principal.owner_id, content_hash),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


def _upload_activity_id(
    principal: Principal,
    digest: str,
    workflow_run_id: str,
) -> str:
    identity = "\0".join((principal.tenant_id, principal.owner_id, digest, workflow_run_id))
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


async def _register_activity_tx(
    conn: aiosqlite.Connection,
    principal: Principal,
    *,
    digest: str,
    activity_id: str,
    resource_kind: str,
    force_new_term: bool = False,
) -> LeaseToken:
    """Register or renew one logical activity inside the caller's write transaction."""

    if re.fullmatch(r"[0-9a-f]{32}", activity_id) is None:
        raise ValueError("RAG activity id must be 32 lowercase hex characters")
    if resource_kind not in _RAG_ACTIVITY_KINDS:
        raise ValueError("unsupported RAG activity kind")
    resource_id = f"{digest}:{activity_id}"
    now = time.time()
    cursor = await conn.execute(
        """SELECT holder_id, fence_token, expires_at
           FROM execution_leases
           WHERE tenant_id = ? AND owner_id = ?
             AND resource_kind = ? AND resource_id = ?""",
        (
            principal.tenant_id,
            principal.owner_id,
            resource_kind,
            resource_id,
        ),
    )
    existing = await cursor.fetchone()
    await cursor.close()
    if existing is None:
        holder_id = uuid4().hex
        fence_token = 1
        await conn.execute(
            """INSERT INTO execution_leases
               (tenant_id, owner_id, resource_kind, resource_id, holder_id,
                fence_token, expires_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                principal.tenant_id,
                principal.owner_id,
                resource_kind,
                resource_id,
                holder_id,
                fence_token,
                now + _RAG_ACTIVITY_LEASE_TTL_S,
                now,
            ),
        )
    else:
        existing_live = float(existing["expires_at"]) > now
        fence_token = int(existing["fence_token"])
        if force_new_term or not existing_live:
            fence_token += 1
            holder_id = uuid4().hex
        else:
            holder_id = str(existing["holder_id"])
        changed = await conn.execute(
            """UPDATE execution_leases
               SET holder_id = ?, fence_token = ?, expires_at = ?, heartbeat_at = ?
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = ? AND resource_id = ?""",
            (
                holder_id,
                fence_token,
                now + _RAG_ACTIVITY_LEASE_TTL_S,
                now,
                principal.tenant_id,
                principal.owner_id,
                resource_kind,
                resource_id,
            ),
        )
        await changed.close()
    return LeaseToken(
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        holder_id=holder_id,
        fence_token=fence_token,
        expires_at=now + _RAG_ACTIVITY_LEASE_TTL_S,
        heartbeat_at=now,
    )


async def _register_upload_activity_tx(
    conn: aiosqlite.Connection,
    principal: Principal,
    *,
    digest: str,
    workflow_run_id: str,
    force_new_term: bool = False,
) -> LeaseToken:
    return await _register_activity_tx(
        conn,
        principal,
        digest=digest,
        activity_id=_upload_activity_id(principal, digest, workflow_run_id),
        resource_kind="rag-upload",
        force_new_term=force_new_term,
    )


async def _digest_has_activity_conn(
    conn: aiosqlite.Connection,
    digest: str,
) -> bool:
    cursor = await conn.execute(
        """SELECT 1 FROM execution_leases
           WHERE resource_kind IN ('rag-upload', 'rag-view')
             AND substr(resource_id, 1, 65) = ?
           LIMIT 1""",
        (f"{digest}:",),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _digest_has_activity(db_path: Path | str, digest: str) -> bool:
    conn = await _open_connection(db_path)
    try:
        return await _digest_has_activity_conn(conn, digest)
    finally:
        await conn.close()


def _claim_from_row(row: aiosqlite.Row, *, created: bool) -> RagContentClaim:
    workflow_run_id = str(row["workflow_run_id"] or "")
    if not workflow_run_id:
        raise RagContentOwnershipError("RAG ACL is missing its workflow run id")
    return RagContentClaim(
        created=created,
        content_hash=str(row["content_hash"]),
        size_bytes=int(row["size_bytes"]),
        workflow_run_id=workflow_run_id,
        file_suffix=str(row["file_suffix"]),
        state=str(row["state"]),
        doc_id=str(row["doc_id"]) if row["doc_id"] is not None else None,
        quota_managed=bool(row["quota_managed"]),
    )


async def _actual_storage_usage(
    conn: aiosqlite.Connection,
    tenant_id: str,
    owner_id: str,
) -> int:
    cursor = await conn.execute(
        """SELECT COALESCE(SUM(size_bytes), 0)
           FROM rag_content_owners
           WHERE tenant_id = ? AND owner_id = ? AND quota_managed = 1""",
        (tenant_id, owner_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row else 0


async def _write_storage_usage(
    conn: aiosqlite.Connection,
    tenant_id: str,
    owner_id: str,
    used: int,
) -> None:
    await conn.execute(
        """INSERT INTO principal_quota_ledger
           (tenant_id, owner_id, metric, window_key, used, updated_at)
           VALUES (?, ?, 'storage_bytes', 'lifetime', ?, CURRENT_TIMESTAMP)
           ON CONFLICT(tenant_id, owner_id, metric, window_key)
           DO UPDATE SET used = excluded.used, updated_at = excluded.updated_at""",
        (tenant_id, owner_id, max(0, used)),
    )


async def claim_rag_content(  # noqa: PLR0912, PLR0915 - lifecycle matrix stays explicit
    db_path: Path | str,
    principal: Principal,
    *,
    content_hash: str,
    size_bytes: int,
    workflow_run_id: str,
    file_suffix: str,
    storage_limit: int | None = None,
) -> RagContentClaim:
    """Atomically create one ACL and charge its public principal exactly once."""

    digest = _validate_content_hash(content_hash)
    suffix = _validate_file_suffix(file_suffix)
    size = max(0, int(size_bytes))
    run_id = workflow_run_id.strip()
    if not run_id:
        raise ValueError("workflow_run_id must not be empty")
    if principal.mode == "public" and (storage_limit is None or storage_limit < 1):
        raise ValueError("a positive storage_limit is required for public principals")

    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _fetch_claim(conn, principal, digest)
        if existing is not None:
            if int(existing["size_bytes"]) != size:
                raise RagContentOwnershipError("content hash has inconsistent size metadata")
            if existing["workflow_run_id"] is None:
                await conn.execute(
                    """UPDATE rag_content_owners
                       SET workflow_run_id = ?, file_suffix = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
                    (
                        run_id,
                        suffix,
                        principal.tenant_id,
                        principal.owner_id,
                        digest,
                    ),
                )
            elif not str(existing["file_suffix"]):
                await conn.execute(
                    """UPDATE rag_content_owners
                       SET file_suffix = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
                    (suffix, principal.tenant_id, principal.owner_id, digest),
                )
            if principal.mode == "public":
                used = await _actual_storage_usage(conn, principal.tenant_id, principal.owner_id)
                await _write_storage_usage(conn, principal.tenant_id, principal.owner_id, used)
            refreshed = await _fetch_claim(conn, principal, digest)
            if refreshed is None:  # pragma: no cover - protected by write lock
                raise RagContentOwnershipError("RAG ACL disappeared while claimed")
            claim = _claim_from_row(refreshed, created=False)
            if claim.state in {"claimed", "staged"}:
                await _register_upload_activity_tx(
                    conn,
                    principal,
                    digest=digest,
                    workflow_run_id=claim.workflow_run_id,
                )
            await conn.commit()
            return claim

        cursor = await conn.execute(
            """SELECT size_bytes FROM rag_content_owners
               WHERE content_hash = ? LIMIT 1""",
            (digest,),
        )
        global_row = await cursor.fetchone()
        await cursor.close()
        if global_row is not None and int(global_row[0]) != size:
            raise RagContentOwnershipError("content hash has inconsistent global size metadata")
        if principal.mode == "public":
            used = await _actual_storage_usage(conn, principal.tenant_id, principal.owner_id)
            assert storage_limit is not None
            if used + size > storage_limit:
                raise QuotaExceeded(
                    "storage_bytes",
                    limit=storage_limit,
                    used=used,
                    retry_after_s=3600,
                )
        await conn.execute(
            """INSERT INTO rag_content_owners
               (tenant_id, owner_id, content_hash, size_bytes, workflow_run_id,
                file_suffix, state, quota_managed)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
            (
                principal.tenant_id,
                principal.owner_id,
                digest,
                size,
                run_id,
                suffix,
                1 if principal.mode == "public" else 0,
            ),
        )
        if principal.mode == "public":
            await _write_storage_usage(conn, principal.tenant_id, principal.owner_id, used + size)
        row = await _fetch_claim(conn, principal, digest)
        if row is None:  # pragma: no cover - protected by the transaction
            raise RagContentOwnershipError("RAG ACL insert was not durable")
        await _register_upload_activity_tx(
            conn,
            principal,
            digest=digest,
            workflow_run_id=run_id,
        )
        await conn.commit()
        return _claim_from_row(row, created=True)
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def get_rag_content_claim(
    db_path: Path | str,
    principal: Principal,
    *,
    content_hash: str,
) -> RagContentClaim | None:
    """Read the current owner ACL without changing quota or lifecycle state."""

    digest = _validate_content_hash(content_hash)
    conn = await _open_connection(db_path)
    try:
        row = await _fetch_claim(conn, principal, digest)
        return _claim_from_row(row, created=False) if row is not None else None
    finally:
        await conn.close()


def _blob_content_size(path: Path, digest: str) -> int | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        size_bytes = path.stat().st_size
        sha256 = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha256.update(chunk)
        return size_bytes if sha256.hexdigest() == digest else None
    except OSError:
        return None


def _blob_matches(path: Path, digest: str, size_bytes: int) -> bool:
    return _blob_content_size(path, digest) == size_bytes


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _upload_temp_path(root: Path, digest: str, token: LeaseToken) -> Path:
    return root / f".rag-upload-{digest}-{token.holder_id}-{token.fence_token}.tmp"


def _write_upload_temp(temp: Path, content: bytes) -> None:
    with temp.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


async def stage_rag_content_blob(  # noqa: PLR0912, PLR0915 - fenced publish sequence
    db_path: Path | str,
    storage_dir: Path | str,
    principal: Principal,
    *,
    content_hash: str,
    workflow_run_id: str,
    content: bytes,
) -> Path:
    """Publish ``rag/<sha>`` while a durable heartbeat protects its ACL and temp."""

    digest = _validate_content_hash(content_hash)
    if hashlib.sha256(content).hexdigest() != digest:
        raise RagContentOwnershipError("uploaded bytes do not match content_hash")
    root = rag_staging_root(storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / digest
    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        row = await _fetch_claim(conn, principal, digest)
        if row is None or str(row["workflow_run_id"] or "") != workflow_run_id:
            raise RagContentOwnershipError("RAG staging has no matching durable ACL")
        if str(row["state"]) not in {"claimed", "staged"}:
            raise RagContentOwnershipError(f"RAG ACL cannot be staged from state {row['state']}")
        if int(row["size_bytes"]) != len(content):
            raise RagContentOwnershipError("uploaded bytes do not match ACL size")
        token = await _register_upload_activity_tx(
            conn,
            principal,
            digest=digest,
            workflow_run_id=workflow_run_id,
            force_new_term=True,
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()

    temp = _upload_temp_path(root, digest, token)
    async with _RagActivityGuard(db_path, token) as activity:
        committed = False
        try:
            await _await_blocking(lambda: _write_upload_temp(temp, content))
            conn = await _open_connection(db_path)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row = await _fetch_claim(conn, principal, digest)
                if row is None or str(row["workflow_run_id"] or "") != workflow_run_id:
                    raise RagContentOwnershipError("RAG staging has no matching durable ACL")
                if str(row["state"]) not in {"claimed", "staged"}:
                    raise RagContentOwnershipError(
                        f"RAG ACL cannot be staged from state {row['state']}"
                    )
                if int(row["size_bytes"]) != len(content):
                    raise RagContentOwnershipError("uploaded bytes do not match ACL size")
                await ExecutionLeaseStore(db_path).assert_owned(token, conn=conn)
                if _blob_matches(target, digest, len(content)):
                    temp.unlink(missing_ok=True)
                else:
                    os.replace(temp, target)
                    _fsync_directory(root)
                changed = await conn.execute(
                    """UPDATE rag_content_owners
                       SET state = 'staged', updated_at = CURRENT_TIMESTAMP
                       WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?
                         AND workflow_run_id = ? AND state IN ('claimed', 'staged')""",
                    (
                        principal.tenant_id,
                        principal.owner_id,
                        digest,
                        workflow_run_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise RagContentOwnershipError("RAG ACL changed while staging")
                await changed.close()
                await conn.commit()
                committed = True
                activity.retain_until_expiry()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
            return target
        finally:
            if not committed or temp.exists():
                temp.unlink(missing_ok=True)
    raise RagContentOwnershipError("RAG staging ended without publishing the blob")


def _copy_parser_view(source: Path, target: Path) -> None:
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _prepare_parser_view(
    canonical: Path,
    view: Path,
    *,
    digest: str,
    size_bytes: int,
    root: Path,
) -> None:
    if not _blob_matches(canonical, digest, size_bytes):
        raise RagContentOwnershipError("canonical RAG workflow input is missing or corrupt")
    try:
        os.link(canonical, view)
    except OSError:
        _copy_parser_view(canonical, view)
    _fsync_directory(root)


@asynccontextmanager
async def open_rag_parser_input(
    db_path: Path | str,
    storage_dir: Path | str,
    principal: Principal,
    *,
    content_hash: str,
    workflow_run_id: str,
) -> AsyncIterator[Path]:
    """Create a suffix-bearing hardlink only for the lifetime of one parser call."""

    digest = _validate_content_hash(content_hash)
    root = rag_staging_root(storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        row = await _fetch_claim(conn, principal, digest)
        if row is None or str(row["workflow_run_id"] or "") != workflow_run_id:
            raise RagContentOwnershipError("RAG workflow has no matching durable ACL")
        if str(row["state"]) not in {"staged", "ready"}:
            raise RagContentOwnershipError("RAG workflow input is not durably staged")
        suffix = _validate_file_suffix(str(row["file_suffix"]))
        size_bytes = int(row["size_bytes"])
        lease = await _register_activity_tx(
            conn,
            principal,
            digest=digest,
            activity_id=uuid4().hex,
            resource_kind="rag-view",
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()

    canonical = root / digest
    view = root / (f".rag-view-{digest}-{lease.holder_id}-{lease.fence_token}{suffix}")
    async with _RagActivityGuard(db_path, lease):
        try:
            await _await_blocking(
                lambda: _prepare_parser_view(
                    canonical,
                    view,
                    digest=digest,
                    size_bytes=size_bytes,
                    root=root,
                )
            )
            yield view
        finally:
            view.unlink(missing_ok=True)


async def bind_rag_content_doc(
    db_path: Path | str,
    principal: Principal,
    *,
    content_hash: str,
    workflow_run_id: str,
    doc_id: str,
) -> None:
    digest = _validate_content_hash(content_hash)
    normalized_doc_id = doc_id.strip()
    if not normalized_doc_id:
        raise ValueError("doc_id must not be empty")
    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        changed = await conn.execute(
            """UPDATE rag_content_owners
               SET doc_id = ?, state = 'ready', updated_at = CURRENT_TIMESTAMP
               WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?
                 AND workflow_run_id = ? AND state IN ('staged', 'ready')""",
            (
                normalized_doc_id,
                principal.tenant_id,
                principal.owner_id,
                digest,
                workflow_run_id,
            ),
        )
        if changed.rowcount != 1:
            raise RagContentOwnershipError("RAG document has no matching staged ACL")
        await changed.close()
        activity_id = _upload_activity_id(principal, digest, workflow_run_id)
        await conn.execute(
            """UPDATE execution_leases
               SET expires_at = 0, heartbeat_at = ?
               WHERE tenant_id = ? AND owner_id = ?
                 AND resource_kind = 'rag-upload' AND resource_id = ?""",
            (
                time.time(),
                principal.tenant_id,
                principal.owner_id,
                f"{digest}:{activity_id}",
            ),
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()


def _candidate_blob_paths(root: Path, digest: str) -> list[Path]:
    candidates = [root / digest, *sorted(root.glob(f"{digest}.*"))]
    return [path for path in dict.fromkeys(candidates) if path.is_file() or path.is_symlink()]


def _move_to_gc(paths: Iterable[Path], root: Path) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    try:
        for original in paths:
            tombstone = root / f".{original.name}.{uuid4().hex}.rag-gc"
            os.replace(original, tombstone)
            moved.append((original, tombstone))
        if moved:
            _fsync_directory(root)
        return moved
    except BaseException:
        _restore_from_gc(moved, root)
        raise


def _restore_from_gc(moved: Iterable[tuple[Path, Path]], root: Path) -> None:
    restored = False
    for original, tombstone in reversed(list(moved)):
        if tombstone.exists() and not original.exists():
            os.replace(tombstone, original)
            restored = True
    if restored:
        _fsync_directory(root)


def _unlink_gc(moved: Iterable[tuple[Path, Path]], root: Path) -> int:
    deleted = 0
    for _original, tombstone in moved:
        try:
            tombstone.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            log.warning("RAG GC tombstone cleanup deferred for %s: %s", tombstone, exc)
    if deleted:
        _fsync_directory(root)
    return deleted


async def release_rag_content_claim(
    db_path: Path | str,
    storage_dir: Path | str,
    principal: Principal,
    *,
    content_hash: str | None = None,
    doc_id: str | None = None,
) -> RagContentRelease:
    """Delete one ACL, rebuild its quota and GC bytes only after the final ACL."""

    if (content_hash is None) == (doc_id is None):
        raise ValueError("exactly one content selector is required")
    column, value = (
        ("content_hash", _validate_content_hash(content_hash))
        if content_hash is not None
        else ("doc_id", str(doc_id).strip())
    )
    root = rag_staging_root(storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    conn = await _open_connection(db_path)
    committed = False
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            f"""SELECT content_hash, size_bytes, quota_managed
                FROM rag_content_owners
                WHERE tenant_id = ? AND owner_id = ? AND {column} = ?""",
            (principal.tenant_id, principal.owner_id, value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            await conn.rollback()
            return RagContentRelease(None, 0, 0, False)
        digest = str(row["content_hash"])
        size_bytes = int(row["size_bytes"])
        await conn.execute(
            """DELETE FROM rag_content_owners
               WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
            (principal.tenant_id, principal.owner_id, digest),
        )
        if bool(row["quota_managed"]):
            used = await _actual_storage_usage(conn, principal.tenant_id, principal.owner_id)
            await _write_storage_usage(conn, principal.tenant_id, principal.owner_id, used)
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM rag_content_owners WHERE content_hash = ?", (digest,)
        )
        count_row = await cursor.fetchone()
        await cursor.close()
        remaining = int(count_row[0]) if count_row else 0
        if remaining == 0:
            moved = _move_to_gc(_candidate_blob_paths(root, digest), root)
        await conn.commit()
        committed = True
    except BaseException:
        await conn.rollback()
        if moved:
            _restore_from_gc(moved, root)
        raise
    finally:
        await conn.close()
    if committed and moved:
        _unlink_gc(moved, root)
    return RagContentRelease(
        content_hash=digest,
        released_bytes=size_bytes,
        remaining_owners=remaining,
        physical_deleted=bool(moved),
    )


def _derived_doc_id(file_suffix: str, workflow_run_id: str) -> str | None:
    if not file_suffix or not workflow_run_id:
        return None
    kind = file_suffix.lstrip(".") or "doc"
    stable_id = hashlib.sha256(workflow_run_id.encode()).hexdigest()[:20]
    return f"{kind}-{stable_id}"


async def _delete_projection_tx(
    conn: aiosqlite.Connection,
    tenant_id: str,
    owner_id: str,
    doc_id: str | None,
) -> tuple[bool, Path | None]:
    if not doc_id:
        return False, None
    cursor = await conn.execute(
        """SELECT index_path FROM rag_documents
           WHERE tenant_id = ? AND owner_id = ? AND doc_id = ?""",
        (tenant_id, owner_id, doc_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    projection_path = (
        Path(str(row["index_path"])).expanduser() if row is not None and row["index_path"] else None
    )
    changed = await conn.execute(
        """DELETE FROM rag_documents
           WHERE tenant_id = ? AND owner_id = ? AND doc_id = ?""",
        (tenant_id, owner_id, doc_id),
    )
    deleted = changed.rowcount > 0
    await changed.close()
    return deleted, projection_path


def _unlink_one(path: Path) -> bool:
    try:
        existed = path.exists() or path.is_symlink()
        path.unlink(missing_ok=True)
        return existed
    except OSError as exc:
        log.warning("RAG deferred file cleanup for %s: %s", path, exc)
        return False


def _delete_stale_activity_files(
    root: Path,
    stale_rows: list[tuple[str, str, str, int]],
) -> int:
    deleted = 0
    for resource_kind, resource_id, holder_id, fence_token in stale_rows:
        match = _RAG_ACTIVITY_RESOURCE_RE.fullmatch(resource_id)
        if match is None or re.fullmatch(r"[0-9a-f]{32}", holder_id) is None:
            continue
        digest = match.group("digest")
        if resource_kind == "rag-upload":
            path = root / (f".rag-upload-{digest}-{holder_id}-{fence_token}.tmp")
            deleted += int(_unlink_one(path))
            continue
        prefix = f".rag-view-{digest}-{holder_id}-{fence_token}"
        for path in root.glob(f"{prefix}.*"):
            if path.name.startswith(f"{prefix}.") and _FILE_SUFFIX_RE.fullmatch(
                path.suffix.lower()
            ):
                deleted += int(_unlink_one(path))
    return deleted


async def _reap_stale_rag_activities(
    db_path: Path | str,
    root: Path,
) -> int:
    """Page expired explicit registrations and delete only their exact files."""

    files_deleted = 0
    while True:
        conn = await _open_connection(db_path)
        stale_rows: list[tuple[str, str, str, int]] = []
        try:
            await conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id, resource_kind, resource_id,
                          holder_id, fence_token
                   FROM execution_leases
                   WHERE resource_kind IN ('rag-upload', 'rag-view')
                     AND expires_at <= ?
                   ORDER BY expires_at, tenant_id, owner_id, resource_kind, resource_id
                   LIMIT ?""",
                (now, _RECONCILE_BATCH_SIZE),
            )
            rows = list(await cursor.fetchmany(_RECONCILE_BATCH_SIZE))
            await cursor.close()
            for row in rows:
                changed = await conn.execute(
                    """DELETE FROM execution_leases
                       WHERE tenant_id = ? AND owner_id = ?
                         AND resource_kind = ? AND resource_id = ?
                         AND holder_id = ? AND fence_token = ?
                         AND expires_at <= ?""",
                    (
                        str(row["tenant_id"]),
                        str(row["owner_id"]),
                        str(row["resource_kind"]),
                        str(row["resource_id"]),
                        str(row["holder_id"]),
                        int(row["fence_token"]),
                        now,
                    ),
                )
                if changed.rowcount == 1:
                    stale_rows.append(
                        (
                            str(row["resource_kind"]),
                            str(row["resource_id"]),
                            str(row["holder_id"]),
                            int(row["fence_token"]),
                        )
                    )
                await changed.close()
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()
        if stale_rows:
            files_deleted += await _await_blocking(
                partial(_delete_stale_activity_files, root, stale_rows)
            )
        if len(rows) < _RECONCILE_BATCH_SIZE:
            return files_deleted


async def _content_has_owner(db_path: Path | str, digest: str) -> bool:
    conn = await _open_connection(db_path)
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM rag_content_owners WHERE content_hash = ? LIMIT 1",
            (digest,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None
    finally:
        await conn.close()


def _restore_gc_tombstone(tombstone: Path, original: Path, root: Path) -> None:
    os.replace(tombstone, original)
    _fsync_directory(root)


async def _recover_gc_tombstones(
    db_path: Path | str,
    root: Path,
) -> tuple[int, int]:
    restored = 0
    deleted = 0
    for tombstone in root.iterdir():
        match = _GC_TOMBSTONE_RE.fullmatch(tombstone.name)
        if match is None or not tombstone.exists():
            continue
        original = root / match.group("original")
        digest = original.name[:64]
        if await _digest_has_activity(db_path, digest):
            continue
        owned = await _content_has_owner(db_path, digest)
        if owned and not original.exists():
            await _await_blocking(partial(_restore_gc_tombstone, tombstone, original, root))
            restored += 1
        else:
            deleted += int(await _await_blocking(partial(_unlink_one, tombstone)))
    return restored, deleted


def _inspect_and_repair_blob(root: Path, digest: str) -> _BlobObservation:
    canonical = root / digest
    legacy = [
        path
        for path in sorted(root.glob(f"{digest}.*"))
        if path.is_file()
        and not path.is_symlink()
        and _FILE_SUFFIX_RE.fullmatch(path.suffix.lower()) is not None
    ]
    actual_size = _blob_content_size(canonical, digest)
    suffix: str | None = None
    canonicalized = 0
    if actual_size is None:
        replacement = next(
            (path for path in legacy if _blob_content_size(path, digest) is not None),
            None,
        )
        if replacement is not None:
            suffix = replacement.suffix.lower()
            os.replace(replacement, canonical)
            _fsync_directory(root)
            canonicalized = 1
            actual_size = _blob_content_size(canonical, digest)
    redundant = [path for path in legacy if path.exists()]
    if redundant:
        moved = _move_to_gc(redundant, root)
        _unlink_gc(moved, root)
    return _BlobObservation(
        valid=actual_size is not None,
        actual_size=actual_size,
        canonicalized_suffix=suffix,
        canonicalized=canonicalized,
    )


async def _fetch_owner_page(
    db_path: Path | str,
    after: tuple[str, str, str] | None,
) -> list[aiosqlite.Row]:
    conn = await _open_connection(db_path)
    try:
        if after is None:
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id, content_hash
                   FROM rag_content_owners
                   ORDER BY content_hash, tenant_id, owner_id
                   LIMIT ?""",
                (_RECONCILE_BATCH_SIZE,),
            )
        else:
            digest, tenant_id, owner_id = after
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id, content_hash
                   FROM rag_content_owners
                   WHERE content_hash > ?
                      OR (content_hash = ? AND tenant_id > ?)
                      OR (content_hash = ? AND tenant_id = ? AND owner_id > ?)
                   ORDER BY content_hash, tenant_id, owner_id
                   LIMIT ?""",
                (
                    digest,
                    digest,
                    tenant_id,
                    digest,
                    tenant_id,
                    owner_id,
                    _RECONCILE_BATCH_SIZE,
                ),
            )
        rows = await cursor.fetchmany(_RECONCILE_BATCH_SIZE)
        await cursor.close()
        return list(rows)
    finally:
        await conn.close()


def _owner_key(row: aiosqlite.Row) -> tuple[str, str, str]:
    return str(row["content_hash"]), str(row["tenant_id"]), str(row["owner_id"])


@dataclass(frozen=True, slots=True)
class _MutationReport:
    released_acls: int = 0
    ready_acls_repaired: int = 0
    projections_deleted: int = 0
    projection_paths: tuple[Path, ...] = ()


async def _apply_owner_page(  # noqa: PLR0912, PLR0915 - lifecycle matrix stays explicit
    db_path: Path | str,
    rows: list[aiosqlite.Row],
    observations: dict[str, _BlobObservation | None],
) -> _MutationReport:
    released = 0
    repaired = 0
    projections_deleted = 0
    projection_paths: list[Path] = []
    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        deferred: set[str] = set()
        for digest in dict.fromkeys(str(row["content_hash"]) for row in rows):
            observation = observations[digest]
            if observation is None or await _digest_has_activity_conn(conn, digest):
                deferred.add(digest)
                continue
            if observation.actual_size is not None:
                await conn.execute(
                    """UPDATE rag_content_owners
                       SET size_bytes = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE content_hash = ? AND size_bytes <> ?""",
                    (observation.actual_size, digest, observation.actual_size),
                )
            if observation.canonicalized_suffix is not None:
                await conn.execute(
                    """UPDATE rag_content_owners
                       SET file_suffix = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE content_hash = ? AND file_suffix = ''""",
                    (observation.canonicalized_suffix, digest),
                )

        for snapshot in rows:
            digest, tenant_id, owner_id = _owner_key(snapshot)
            observation = observations[digest]
            if digest in deferred or observation is None:
                continue
            cursor = await conn.execute(
                """SELECT size_bytes, doc_id, workflow_run_id, file_suffix, state
                   FROM rag_content_owners
                   WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
                (tenant_id, owner_id, digest),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                continue
            state = str(row["state"])
            run_id = str(row["workflow_run_id"] or "")
            doc_id = str(row["doc_id"]) if row["doc_id"] is not None else None
            remove = False
            repair_doc_id: str | None = None

            if state == "claimed" or not observation.valid:
                remove = True
            elif state == "staged":
                workflow = None
                if run_id:
                    cursor = await conn.execute(
                        """SELECT state, output_json FROM workflow_runs
                           WHERE tenant_id = ? AND owner_id = ? AND run_id = ?""",
                        (tenant_id, owner_id, run_id),
                    )
                    workflow = await cursor.fetchone()
                    await cursor.close()
                if workflow is None:
                    remove = True
                else:
                    workflow_state = str(workflow["state"])
                    if workflow_state == "succeeded":
                        try:
                            output = json.loads(str(workflow["output_json"] or "{}"))
                        except (TypeError, ValueError):
                            output = {}
                        value = output.get("doc_id") if isinstance(output, dict) else None
                        repair_doc_id = str(value).strip() if value else None
                        remove = not repair_doc_id
                    elif workflow_state in _FAILED_WORKFLOW_STATES or (
                        workflow_state not in _ACTIVE_WORKFLOW_STATES
                    ):
                        remove = True

            if repair_doc_id:
                changed = await conn.execute(
                    """UPDATE rag_content_owners
                       SET doc_id = ?, state = 'ready', updated_at = CURRENT_TIMESTAMP
                       WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
                    (repair_doc_id, tenant_id, owner_id, digest),
                )
                repaired += int(changed.rowcount == 1)
                await changed.close()
                continue
            if not remove:
                continue
            cleanup_doc_id = doc_id or _derived_doc_id(str(row["file_suffix"]), run_id)
            deleted, projection_path = await _delete_projection_tx(
                conn, tenant_id, owner_id, cleanup_doc_id
            )
            projections_deleted += int(deleted)
            if projection_path is not None:
                projection_paths.append(projection_path)
            changed = await conn.execute(
                """DELETE FROM rag_content_owners
                   WHERE tenant_id = ? AND owner_id = ? AND content_hash = ?""",
                (tenant_id, owner_id, digest),
            )
            released += int(changed.rowcount == 1)
            await changed.close()
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()
    return _MutationReport(
        released_acls=released,
        ready_acls_repaired=repaired,
        projections_deleted=projections_deleted,
        projection_paths=tuple(projection_paths),
    )


async def _collect_orphan_blobs(db_path: Path | str, root: Path) -> int:
    deleted = 0
    for path in root.iterdir():
        if not path.exists() or path.name.startswith("."):
            continue
        digest = path.name[:64]
        if _CONTENT_HASH_RE.fullmatch(digest) is None:
            continue
        if await _digest_has_activity(db_path, digest):
            continue
        if await _content_has_owner(db_path, digest):
            continue
        candidates = _candidate_blob_paths(root, digest)
        if not candidates:
            continue
        moved = await _await_blocking(partial(_move_to_gc, candidates, root))
        if await _content_has_owner(db_path, digest) or await _digest_has_activity(db_path, digest):
            await _await_blocking(partial(_restore_from_gc, moved, root))
            continue
        await _await_blocking(partial(_unlink_gc, moved, root))
        deleted += len(moved)
    return deleted


async def _fetch_owner_scope_page(
    db_path: Path | str,
    after: tuple[str, str] | None,
) -> list[tuple[str, str]]:
    conn = await _open_connection(db_path)
    try:
        if after is None:
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id FROM rag_content_owners
                   WHERE quota_managed = 1
                   GROUP BY tenant_id, owner_id
                   ORDER BY tenant_id, owner_id LIMIT ?""",
                (_RECONCILE_BATCH_SIZE,),
            )
        else:
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id FROM rag_content_owners
                   WHERE quota_managed = 1
                     AND (tenant_id > ? OR (tenant_id = ? AND owner_id > ?))
                   GROUP BY tenant_id, owner_id
                   ORDER BY tenant_id, owner_id LIMIT ?""",
                (after[0], after[0], after[1], _RECONCILE_BATCH_SIZE),
            )
        rows = await cursor.fetchmany(_RECONCILE_BATCH_SIZE)
        await cursor.close()
        return [(str(row[0]), str(row[1])) for row in rows]
    finally:
        await conn.close()


async def _fetch_ledger_scope_page(
    db_path: Path | str,
    after: tuple[str, str] | None,
) -> list[tuple[str, str]]:
    conn = await _open_connection(db_path)
    try:
        if after is None:
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id FROM principal_quota_ledger
                   WHERE metric = 'storage_bytes' AND window_key = 'lifetime'
                   ORDER BY tenant_id, owner_id LIMIT ?""",
                (_RECONCILE_BATCH_SIZE,),
            )
        else:
            cursor = await conn.execute(
                """SELECT tenant_id, owner_id FROM principal_quota_ledger
                   WHERE metric = 'storage_bytes' AND window_key = 'lifetime'
                     AND (tenant_id > ? OR (tenant_id = ? AND owner_id > ?))
                   ORDER BY tenant_id, owner_id LIMIT ?""",
                (after[0], after[0], after[1], _RECONCILE_BATCH_SIZE),
            )
        rows = await cursor.fetchmany(_RECONCILE_BATCH_SIZE)
        await cursor.close()
        return [(str(row[0]), str(row[1])) for row in rows]
    finally:
        await conn.close()


async def _ledger_value(
    conn: aiosqlite.Connection,
    tenant_id: str,
    owner_id: str,
) -> int | None:
    cursor = await conn.execute(
        """SELECT used FROM principal_quota_ledger
           WHERE tenant_id = ? AND owner_id = ?
             AND metric = 'storage_bytes' AND window_key = 'lifetime'""",
        (tenant_id, owner_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row is not None else None


async def _repair_ledger_scope_page(
    db_path: Path | str,
    scopes: list[tuple[str, str]],
) -> int:
    changed_count = 0
    conn = await _open_connection(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        for tenant_id, owner_id in scopes:
            desired = await _actual_storage_usage(conn, tenant_id, owner_id)
            existing = await _ledger_value(conn, tenant_id, owner_id)
            if existing == desired:
                continue
            await _write_storage_usage(conn, tenant_id, owner_id, desired)
            changed_count += 1
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()
    return changed_count


async def _rebuild_storage_ledgers(db_path: Path | str) -> int:
    changed = 0
    after: tuple[str, str] | None = None
    while True:
        scopes = await _fetch_owner_scope_page(db_path, after)
        if not scopes:
            break
        changed += await _repair_ledger_scope_page(db_path, scopes)
        after = scopes[-1]
    after = None
    while True:
        scopes = await _fetch_ledger_scope_page(db_path, after)
        if not scopes:
            break
        changed += await _repair_ledger_scope_page(db_path, scopes)
        after = scopes[-1]

    while True:
        conn = await _open_connection(db_path)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """SELECT rowid FROM principal_quota_ledger
                   WHERE metric = 'storage_bytes' AND window_key <> 'lifetime'
                   LIMIT ?""",
                (_RECONCILE_BATCH_SIZE,),
            )
            rows = await cursor.fetchmany(_RECONCILE_BATCH_SIZE)
            await cursor.close()
            if rows:
                await conn.executemany(
                    "DELETE FROM principal_quota_ledger WHERE rowid = ?",
                    [(int(row[0]),) for row in rows],
                )
                await conn.commit()
            else:
                await conn.rollback()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()
        if not rows:
            break
    return changed


async def reconcile_rag_content_storage(
    db_path: Path | str,
    storage_dir: Path | str,
) -> RagContentReconcileReport:
    """Repair RAG storage without stealing another process's registered work.

    Every database mutation is bounded to one page.  Blob hashing, canonical
    moves, fsync and projection cleanup happen after the corresponding write
    transaction.  Activity rows left after the initial stale reaper are also a
    generation barrier: even if an operation finishes during this pass, its
    released row makes this pass defer that digest until the next reconcile.
    """

    root = rag_staging_root(storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    released_acls = 0
    repaired_ready = 0
    canonicalized = 0
    projections_deleted = 0
    temp_deleted = await _reap_stale_rag_activities(db_path, root)
    tombstones_restored, tombstones_deleted = await _recover_gc_tombstones(db_path, root)
    temp_deleted += tombstones_deleted

    after: tuple[str, str, str] | None = None
    while True:
        rows = await _fetch_owner_page(db_path, after)
        if not rows:
            break
        observations: dict[str, _BlobObservation | None] = {}
        for digest in dict.fromkeys(str(row["content_hash"]) for row in rows):
            if await _digest_has_activity(db_path, digest):
                observations[digest] = None
                continue
            observation = await _await_blocking(partial(_inspect_and_repair_blob, root, digest))
            observations[digest] = observation
            canonicalized += observation.canonicalized

        mutation = await _apply_owner_page(
            db_path,
            rows,
            observations,
        )
        released_acls += mutation.released_acls
        repaired_ready += mutation.ready_acls_repaired
        projections_deleted += mutation.projections_deleted
        for projection_path in mutation.projection_paths:
            await _await_blocking(partial(_unlink_one, projection_path))
        after = _owner_key(rows[-1])

    orphan_deleted = await _collect_orphan_blobs(db_path, root)
    quota_scopes = await _rebuild_storage_ledgers(db_path)
    return RagContentReconcileReport(
        released_acls=released_acls,
        ready_acls_repaired=repaired_ready,
        canonicalized_blobs=canonicalized,
        orphan_blobs_deleted=orphan_deleted,
        temp_files_deleted=temp_deleted,
        gc_tombstones_restored=tombstones_restored,
        quota_scopes_rebuilt=quota_scopes,
        projections_deleted=projections_deleted,
    )


__all__ = [
    "RagContentClaim",
    "RagContentOwnershipError",
    "RagContentReconcileReport",
    "RagContentRelease",
    "bind_rag_content_doc",
    "claim_rag_content",
    "get_rag_content_claim",
    "open_rag_parser_input",
    "rag_blob_path",
    "rag_staging_root",
    "reconcile_rag_content_storage",
    "release_rag_content_claim",
    "stage_rag_content_blob",
]
