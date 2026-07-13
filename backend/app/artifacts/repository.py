"""Artifact 0.3 metadata/link repository."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.security.context import current_principal


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _metadata(raw: str | None) -> dict[str, str]:
    data = _json_loads(raw, {})
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _scope() -> tuple[str, str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


@dataclass(slots=True)
class ArtifactLinkRecord:
    link_id: str
    artifact_id: str
    source: str
    meeting_id: str | None
    todo_id: str | None
    run_id: str | None
    created_at: str


class ArtifactFileUnavailableError(RuntimeError):
    """The local artifact disappeared before its metadata write lock was acquired."""


def _assert_artifact_file_available(artifact: GeneratedArtifact) -> None:
    path = Path(artifact.file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise ArtifactFileUnavailableError("artifact file is unavailable for registration")


class ArtifactRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.settings.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    async def save_artifact_tx(
        self,
        conn: aiosqlite.Connection,
        artifact: GeneratedArtifact,
        *,
        run_id: str | None = None,
    ) -> None:
        """Write metadata on a caller-owned SQLite transaction (no implicit commit)."""

        # Promote a caller's deferred transaction before checking the file.
        # Cleanup uses the same SQLite write lock around its protected-path
        # snapshot and deletion, so a file removed while this writer waited
        # cannot be registered afterwards as a missing artifact.
        await conn.execute("UPDATE artifacts SET updated_at = updated_at WHERE 0")
        _assert_artifact_file_available(artifact)
        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        changed = await conn.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, title, file_path, mime_type, size_bytes,
                generation_latency_ms, model, metadata_json, run_id, created_at, updated_at,
                tenant_id, device_id, owner_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, owner_id, artifact_id) DO UPDATE SET
                   artifact_type = excluded.artifact_type,
                   title = excluded.title,
                   file_path = excluded.file_path,
                   mime_type = excluded.mime_type,
                   size_bytes = excluded.size_bytes,
                   generation_latency_ms = excluded.generation_latency_ms,
                   model = excluded.model,
                   metadata_json = excluded.metadata_json,
                   run_id = COALESCE(excluded.run_id, artifacts.run_id),
                   updated_at = excluded.updated_at
               WHERE artifacts.tenant_id = excluded.tenant_id
                 AND artifacts.owner_id = excluded.owner_id""",
            (
                artifact.artifact_id,
                artifact.artifact_type,
                artifact.title,
                artifact.file_path,
                artifact.mime_type,
                artifact.size_bytes,
                artifact.generation_latency_ms,
                artifact.model,
                json.dumps(artifact.metadata, ensure_ascii=False),
                run_id,
                now,
                now,
                tenant_id,
                device_id,
                owner_id,
            ),
        )
        if changed.rowcount != 1:
            raise PermissionError("artifact id is unavailable in this principal scope")

    async def link_artifact_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        artifact_id: str,
        source: str,
        meeting_id: str | None = None,
        todo_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactLinkRecord:
        """Write one owner-scoped link on a caller-owned transaction."""

        tenant_id, device_id, owner_id = _scope()
        raw = "\x1f".join(
            [
                tenant_id,
                owner_id,
                artifact_id,
                source,
                meeting_id or "",
                todo_id or "",
                run_id or "",
            ]
        )
        link_id = f"alink_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"
        now = utc_now_iso()
        await conn.execute(
            """INSERT INTO artifact_links
               (link_id, artifact_id, source, meeting_id, todo_id, run_id, created_at,
                tenant_id, device_id, owner_id)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM artifacts
                   WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?)
               ON CONFLICT(tenant_id, owner_id, link_id) DO NOTHING""",
            (
                link_id,
                artifact_id,
                source,
                meeting_id,
                todo_id,
                run_id,
                now,
                tenant_id,
                device_id,
                owner_id,
                artifact_id,
                tenant_id,
                owner_id,
            ),
        )
        cur = await conn.execute(
            "SELECT * FROM artifact_links WHERE link_id = ? AND tenant_id = ? AND owner_id = ?",
            (link_id, tenant_id, owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RuntimeError(f"artifact link insert failed: {link_id}")
        return _row_to_link(row)

    async def save_artifact(
        self,
        artifact: GeneratedArtifact,
        *,
        run_id: str | None = None,
    ) -> GeneratedArtifact:
        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            _assert_artifact_file_available(artifact)
            await conn.execute(
                """INSERT INTO artifacts
                   (artifact_id, artifact_type, title, file_path, mime_type, size_bytes,
                    generation_latency_ms, model, metadata_json, run_id, created_at, updated_at,
                    tenant_id, device_id, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id, artifact_id) DO UPDATE SET
                       artifact_type = excluded.artifact_type,
                       title = excluded.title,
                       file_path = excluded.file_path,
                       mime_type = excluded.mime_type,
                       size_bytes = excluded.size_bytes,
                       generation_latency_ms = excluded.generation_latency_ms,
                       model = excluded.model,
                       metadata_json = excluded.metadata_json,
                       run_id = COALESCE(excluded.run_id, artifacts.run_id),
                       updated_at = excluded.updated_at
                   WHERE artifacts.tenant_id = excluded.tenant_id
                     AND artifacts.owner_id = excluded.owner_id""",
                (
                    artifact.artifact_id,
                    artifact.artifact_type,
                    artifact.title,
                    artifact.file_path,
                    artifact.mime_type,
                    artifact.size_bytes,
                    artifact.generation_latency_ms,
                    artifact.model,
                    json.dumps(artifact.metadata, ensure_ascii=False),
                    run_id,
                    now,
                    now,
                    tenant_id,
                    device_id,
                    owner_id,
                ),
            )
            await conn.commit()
        saved = await self.get_artifact(artifact.artifact_id)
        if saved is None:
            raise PermissionError("artifact id is unavailable in this principal scope")
        return saved

    async def link_artifact(
        self,
        *,
        artifact_id: str,
        source: str,
        meeting_id: str | None = None,
        todo_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactLinkRecord:
        tenant_id, device_id, owner_id = _scope()
        raw = "\x1f".join(
            [
                tenant_id,
                owner_id,
                artifact_id,
                source,
                meeting_id or "",
                todo_id or "",
                run_id or "",
            ]
        )
        link_id = f"alink_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"
        now = utc_now_iso()
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO artifact_links
                   (link_id, artifact_id, source, meeting_id, todo_id, run_id, created_at,
                    tenant_id, device_id, owner_id)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE EXISTS (SELECT 1 FROM artifacts
                       WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?)
                   ON CONFLICT(tenant_id, owner_id, link_id) DO NOTHING""",
                (
                    link_id,
                    artifact_id,
                    source,
                    meeting_id,
                    todo_id,
                    run_id,
                    now,
                    tenant_id,
                    device_id,
                    owner_id,
                    artifact_id,
                    tenant_id,
                    owner_id,
                ),
            )
            await conn.commit()
        link = await self.get_link(link_id)
        if link is None:
            raise RuntimeError(f"artifact link insert failed: {link_id}")
        return link

    async def get_artifact(self, artifact_id: str) -> GeneratedArtifact | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?",
                (artifact_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_artifact(row) if row else None

    async def get_link(self, link_id: str) -> ArtifactLinkRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM artifact_links WHERE link_id = ? AND tenant_id = ? AND owner_id = ?",
                (link_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_link(row) if row else None

    async def list_artifacts(self, *, limit: int = 100) -> list[GeneratedArtifact]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM artifacts WHERE tenant_id = ? AND owner_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, owner_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_artifact(row) for row in rows]

    async def list_meeting_artifacts(
        self,
        meeting_id: str,
        *,
        limit: int = 200,
    ) -> list[GeneratedArtifact]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT DISTINCT a.*
                   FROM artifacts a
                   JOIN artifact_links l
                     ON l.artifact_id = a.artifact_id
                    AND l.tenant_id = a.tenant_id
                    AND l.owner_id = a.owner_id
                   WHERE l.meeting_id = ?
                     AND l.tenant_id = ? AND l.owner_id = ?
                   ORDER BY a.created_at DESC
                   LIMIT ?""",
                (meeting_id, tenant_id, owner_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_artifact(row) for row in rows]

    async def list_todo_artifacts(
        self,
        meeting_id: str,
        todo_id: str,
        *,
        limit: int = 50,
    ) -> list[GeneratedArtifact]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT DISTINCT a.*
                   FROM artifacts a
                   JOIN artifact_links l
                     ON l.artifact_id = a.artifact_id
                    AND l.tenant_id = a.tenant_id
                    AND l.owner_id = a.owner_id
                   WHERE l.meeting_id = ? AND l.todo_id = ?
                     AND l.tenant_id = ? AND l.owner_id = ?
                   ORDER BY a.created_at DESC
                   LIMIT ?""",
                (meeting_id, todo_id, tenant_id, owner_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_artifact(row) for row in rows]

    async def list_links_for_artifact(self, artifact_id: str) -> list[ArtifactLinkRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM artifact_links WHERE artifact_id = ? "
                "AND tenant_id = ? AND owner_id = ? ORDER BY created_at ASC",
                (artifact_id, tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_link(row) for row in rows]

    async def count_links(self, artifact_id: str) -> int:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM artifact_links WHERE artifact_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (artifact_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0] if row else 0)

    async def unlink_meeting(self, meeting_id: str) -> list[GeneratedArtifact]:
        artifacts = await self.list_meeting_artifacts(meeting_id)
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            await conn.execute(
                "DELETE FROM artifact_links WHERE meeting_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (meeting_id, tenant_id, owner_id),
            )
            await conn.commit()
        return artifacts

    async def delete_artifact_metadata(self, artifact_id: str) -> bool:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?",
                (artifact_id, tenant_id, owner_id),
            )
            await conn.commit()
            return bool(cur.rowcount)


def _row_to_artifact(row: aiosqlite.Row) -> GeneratedArtifact:
    return GeneratedArtifact(
        artifact_id=row["artifact_id"],
        artifact_type=row["artifact_type"],
        title=row["title"] or "",
        file_path=row["file_path"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"] or 0),
        generation_latency_ms=float(row["generation_latency_ms"] or 0),
        model=row["model"] or "",
        metadata=_metadata(row["metadata_json"]),
    )


def _row_to_link(row: aiosqlite.Row) -> ArtifactLinkRecord:
    return ArtifactLinkRecord(
        link_id=row["link_id"],
        artifact_id=row["artifact_id"],
        source=row["source"],
        meeting_id=row["meeting_id"],
        todo_id=row["todo_id"],
        run_id=row["run_id"],
        created_at=row["created_at"],
    )


_repository: ArtifactRepository | None = None


def get_artifact_repository(settings: Settings) -> ArtifactRepository:
    global _repository  # noqa: PLW0603
    if _repository is None:
        _repository = ArtifactRepository(settings)
    return _repository


def reset_artifact_repository_for_test() -> None:
    global _repository  # noqa: PLW0603
    _repository = None
