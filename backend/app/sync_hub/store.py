"""Durable pairing and device state for the sync hub."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.memory.repository import normalize_key, normalize_text
from app.security.models import Principal
from app.security.scope import scoped_directory_for


class SyncHubError(RuntimeError):
    """Base class for sync hub storage failures."""


class PairingNotFoundError(SyncHubError):
    """Pairing code is unknown, expired, or already claimed."""


class DeviceAlreadyExistsError(SyncHubError):
    """The claimed device id is already owned by the user."""


class SyncDeviceNotFoundError(SyncHubError):
    """The requested sync device is not owned by the current user."""


class SyncEntityValidationError(SyncHubError):
    """The payload cannot be adapted to an existing business repository."""


@dataclass(frozen=True, slots=True)
class PairingRecord:
    pairing_id: str
    source_device_id: str
    pairing_code: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedDevice:
    device_id: str
    sync_token: str
    cursor: int


@dataclass(frozen=True, slots=True)
class SyncDeviceRecord:
    device_id: str
    device_name: str
    platform: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    cursor: int


SyncEntityType = Literal[
    "meeting",
    "transcript_segment",
    "meeting_summary",
    "artifact",
    "memory",
]
PushStatus = Literal["applied", "duplicate", "conflict"]


@dataclass(frozen=True, slots=True)
class SyncChangeRecord:
    cursor: int
    source_device_id: str
    entity_type: SyncEntityType
    entity_id: str
    revision: int
    updated_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PushResult:
    status: PushStatus
    revision: int
    cursor: int | None
    current: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    cursor: int
    meetings: list[SyncChangeRecord]
    transcript_segments: list[SyncChangeRecord]
    meeting_summaries: list[SyncChangeRecord]
    artifacts: list[SyncChangeRecord]
    memories: list[SyncChangeRecord]


@dataclass(frozen=True, slots=True)
class SyncHistoryManifestRecord:
    cursor: int
    meeting_count: int
    transcript_segment_count: int
    meeting_summary_count: int
    artifact_count: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _as_utc(parsed)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: object) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_field(payload: dict[str, Any], name: str, default: int | None = None) -> int:
    value = payload.get(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncEntityValidationError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise SyncEntityValidationError(f"{name} must be non-negative")
    return parsed


_SYNC_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SYNC_ARTIFACT_EXTENSIONS = {
    "html": "html",
    "markdown": "md",
    "pdf": "pdf",
    "pptx": "pptx",
    "txt": "txt",
    "word": "docx",
    "xlsx": "xlsx",
}


def _sync_artifact_extension(artifact_type: object) -> str:
    value = str(artifact_type or "").strip().lower()
    return _SYNC_ARTIFACT_EXTENSIONS.get(value, "bin")


def _sync_artifact_metadata(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("metadata")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


class SyncHubStore:
    """SQLite adapter for pairing and sync-device lifecycle state."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        artifact_build_dir: str | Path | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.artifact_build_dir = Path(
            artifact_build_dir or self.db_path.parent / "skill_build"
        ).expanduser()
        self._now = now
        self._change_waiters: dict[tuple[str, str], asyncio.Event] = {}

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    async def _ensure_principal_device_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        *,
        now: str,
    ) -> None:
        """Keep the existing identity tables authoritative for hub scope."""

        await conn.execute(
            """INSERT OR IGNORE INTO tenants (tenant_id, status, created_at, updated_at)
               VALUES (?, 'active', ?, ?)""",
            (principal.tenant_id, now, now),
        )
        await conn.execute(
            """INSERT OR IGNORE INTO users (tenant_id, user_id, status, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?)""",
            (principal.tenant_id, principal.owner_id, now, now),
        )
        await conn.execute(
            """INSERT OR IGNORE INTO devices (
                   tenant_id, user_id, device_id, display_name,
                   created_at, last_seen_at, legacy_claimed_at, revoked_at
               ) VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL)""",
            (
                principal.tenant_id,
                principal.owner_id,
                principal.device_id,
                now,
                now,
            ),
        )

    async def create_pairing(
        self,
        principal: Principal,
        *,
        ttl: timedelta,
    ) -> PairingRecord:
        if ttl <= timedelta(0):
            raise ValueError("pairing ttl must be positive")
        now = _as_utc(self._now())
        pairing_id = _new_secret("pairing")
        pairing_code = _new_secret("pair")
        expires_at = now + ttl
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                now_iso = now.isoformat()
                await self._ensure_principal_device_tx(conn, principal, now=now_iso)
                await conn.execute(
                    """INSERT INTO device_pairings (
                           pairing_id, tenant_id, owner_id, source_device_id,
                           pairing_code_hash, created_at, expires_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pairing_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                        _hash_secret(pairing_code),
                        now_iso,
                        expires_at.isoformat(),
                    ),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return PairingRecord(
            pairing_id=pairing_id,
            source_device_id=principal.device_id,
            pairing_code=pairing_code,
            created_at=now,
            expires_at=expires_at,
        )

    async def claim_pairing(
        self,
        *,
        pairing_code: str,
        device_id: str,
        device_name: str,
        platform: str,
    ) -> ClaimedDevice:
        now = _as_utc(self._now())
        sync_token = _new_secret("sync")
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        """SELECT pairing_id, tenant_id, owner_id, expires_at, claimed_at
                           FROM device_pairings WHERE pairing_code_hash = ?""",
                        (_hash_secret(pairing_code),),
                    )
                ).fetchone()
                if row is None or row["claimed_at"] is not None:
                    raise PairingNotFoundError("pairing code invalid")
                if now >= _parse_datetime(row["expires_at"]):
                    raise PairingNotFoundError("pairing code expired")
                existing = await (
                    await conn.execute(
                        """SELECT 1 FROM devices
                           WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
                        (row["tenant_id"], row["owner_id"], device_id),
                    )
                ).fetchone()
                if existing is not None:
                    raise DeviceAlreadyExistsError("device id already exists")
                now_iso = now.isoformat()
                await conn.execute(
                    """INSERT INTO devices (
                           tenant_id, user_id, device_id, display_name,
                           created_at, last_seen_at, legacy_claimed_at, revoked_at
                       ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                    (
                        row["tenant_id"],
                        row["owner_id"],
                        device_id,
                        device_name,
                        now_iso,
                        now_iso,
                    ),
                )
                await conn.execute(
                    """INSERT INTO sync_devices (
                           tenant_id, owner_id, device_id, device_name,
                           platform, sync_token_hash, created_at, last_seen_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["tenant_id"],
                        row["owner_id"],
                        device_id,
                        device_name,
                        platform,
                        _hash_secret(sync_token),
                        now_iso,
                        now_iso,
                    ),
                )
                cursor_row = await (
                    await conn.execute(
                        """SELECT COALESCE(MAX(cursor), 0) AS cursor
                           FROM sync_events WHERE tenant_id = ? AND owner_id = ?""",
                        (row["tenant_id"], row["owner_id"]),
                    )
                ).fetchone()
                cursor = int(cursor_row["cursor"] if cursor_row is not None else 0)
                await conn.execute(
                    """INSERT INTO device_cursors (
                           tenant_id, owner_id, device_id, cursor, updated_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (row["tenant_id"], row["owner_id"], device_id, cursor, now_iso),
                )
                await conn.execute(
                    """UPDATE device_pairings
                       SET claimed_at = ?, claimed_device_id = ?
                       WHERE pairing_id = ? AND claimed_at IS NULL""",
                    (now_iso, device_id, row["pairing_id"]),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return ClaimedDevice(device_id=device_id, sync_token=sync_token, cursor=cursor)

    @staticmethod
    async def _fetchone(
        conn: aiosqlite.Connection,
        query: str,
        args: tuple[object, ...] = (),
    ) -> aiosqlite.Row | None:
        cursor = await conn.execute(query, args)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    async def _fetchall(
        conn: aiosqlite.Connection,
        query: str,
        args: tuple[object, ...] = (),
    ) -> list[aiosqlite.Row]:
        cursor = await conn.execute(query, args)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    @staticmethod
    def _memory_payload(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "memory_id": str(row["memory_id"]),
            "kind": str(row["kind"]),
            "content": str(row["content"]),
            "canonical_key": str(row["canonical_key"]),
            "subject": row["subject"],
            "confidence": float(row["confidence"]),
            "salience": float(row["salience"]),
            "scope": str(row["scope"]),
            "status": str(row["status"]),
            "user_confirmed": bool(row["user_confirmed"]),
            "revision": int(row["revision"]),
            "metadata": _json_load(row["metadata_json"]) or {},
        }

    async def _latest_event_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_type: SyncEntityType,
        entity_id: str,
    ) -> aiosqlite.Row | None:
        return await self._fetchone(
            conn,
            """SELECT cursor, source_device_id, revision, updated_at, payload_json
               FROM sync_events
               WHERE tenant_id = ? AND owner_id = ?
                 AND entity_type = ? AND entity_id = ?
               ORDER BY cursor DESC LIMIT 1""",
            (principal.tenant_id, principal.owner_id, entity_type, entity_id),
        )

    async def _current_entity_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_type: SyncEntityType,
        entity_id: str,
    ) -> tuple[int, int, dict[str, Any] | None]:
        event = await self._latest_event_tx(conn, principal, entity_type, entity_id)
        if event is not None:
            return (
                int(event["revision"]),
                int(event["cursor"]),
                _json_load(event["payload_json"]),
            )
        scope = (principal.tenant_id, principal.owner_id)
        if entity_type == "meeting":
            row = await self._fetchone(
                conn,
                """SELECT id, title, state, started_at, ended_at, finalized_at,
                          minutes_status, minutes_error, display_title
                   FROM meetings
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                (entity_id, *scope),
            )
            if row is not None:
                return (
                    0,
                    0,
                    {
                        "meeting_id": str(row["id"]),
                        "title": row["title"],
                        "display_title": row["display_title"],
                        "state": row["state"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "finalized_at": row["finalized_at"],
                        "minutes_status": row["minutes_status"],
                        "minutes_error": row["minutes_error"],
                    },
                )
        if entity_type == "transcript_segment" and entity_id.isdigit():
            row = await self._fetchone(
                conn,
                """SELECT id, meeting_id, text, start_ms, end_ms,
                          speaker_id, speaker_label, captured_at
                   FROM meeting_segments
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                (int(entity_id), *scope),
            )
            if row is not None:
                return (
                    0,
                    0,
                    {
                        "meeting_id": str(row["meeting_id"]),
                        "segment_id": int(row["id"]),
                        "text": str(row["text"]),
                        "start_ms": int(row["start_ms"]),
                        "end_ms": int(row["end_ms"]),
                        "speaker_id": row["speaker_id"],
                        "speaker_label": row["speaker_label"],
                        "captured_at": str(row["captured_at"]),
                    },
                )
        if entity_type == "meeting_summary":
            row = await self._fetchone(
                conn,
                """SELECT id, minutes_json, finalized_at, ended_at, started_at
                   FROM meetings
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?
                     AND minutes_json IS NOT NULL""",
                (entity_id, *scope),
            )
            if row is not None:
                payload = _json_load(row["minutes_json"]) or {}
                payload["meeting_id"] = str(row["id"])
                occurred_at = row["finalized_at"] or row["ended_at"] or row["started_at"]
                payload["updated_at"] = str(occurred_at)
                return 0, 0, payload
        if entity_type == "artifact":
            row = await self._fetchone(
                conn,
                """SELECT artifact_id, artifact_type, title, mime_type,
                          size_bytes, generation_latency_ms, model,
                          metadata_json, created_at, updated_at
                   FROM artifacts
                   WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?""",
                (entity_id, *scope),
            )
            if row is not None:
                return (
                    0,
                    0,
                    {
                        "artifact_id": str(row["artifact_id"]),
                        "artifact_type": str(row["artifact_type"]),
                        "title": row["title"] or "",
                        "mime_type": row["mime_type"],
                        "size_bytes": int(row["size_bytes"] or 0),
                        "generation_latency_ms": float(row["generation_latency_ms"] or 0),
                        "model": row["model"] or "",
                        "metadata": _json_load(row["metadata_json"]) or {},
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "content_base64": None,
                    },
                )
        if entity_type == "memory":
            row = await self._fetchone(
                conn,
                """SELECT * FROM memory_nodes
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (*scope, entity_id),
            )
            if row is not None:
                return int(row["revision"]), 0, self._memory_payload(row)
        return 0, 0, None

    async def _ensure_meeting_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        meeting_id: str,
        *,
        occurred_at: str,
    ) -> None:
        existing = await self._fetchone(
            conn,
            """SELECT 1 FROM meetings
               WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
            (meeting_id, principal.tenant_id, principal.owner_id),
        )
        if existing is not None:
            return
        await conn.execute(
            """INSERT INTO meetings (
                   id, state, started_at, ended_at,
                   tenant_id, device_id, owner_id
               ) VALUES (?, 'ended', ?, ?, ?, ?, ?)""",
            (
                meeting_id,
                occurred_at,
                occurred_at,
                principal.tenant_id,
                principal.device_id,
                principal.owner_id,
            ),
        )

    async def _apply_meeting_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_id: str,
        payload: dict[str, Any],
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        meeting_id = str(payload.get("meeting_id") or entity_id).strip()
        if not meeting_id:
            raise SyncEntityValidationError("meeting requires meeting_id")
        state = str(payload.get("state") or "ended")
        if state not in {"in_meeting", "ended", "finalized"}:
            state = "ended"
        started_at = str(payload.get("started_at") or updated_at)
        ended_at = payload.get("ended_at") or payload.get("finalized_at")
        await self._ensure_meeting_tx(
            conn,
            principal,
            meeting_id,
            occurred_at=started_at,
        )
        title = str(payload.get("title") or "").strip() or None
        display_title = str(payload.get("display_title") or "").strip() or None
        await conn.execute(
            """UPDATE meetings SET
                      title = COALESCE(?, title),
                      state = ?,
                      started_at = COALESCE(started_at, ?),
                      ended_at = COALESCE(ended_at, ?),
                      finalized_at = COALESCE(finalized_at, ?),
                      minutes_status = COALESCE(?, minutes_status),
                      minutes_error = COALESCE(?, minutes_error),
                      display_title = COALESCE(?, display_title)
                WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
            (
                title,
                state,
                started_at,
                ended_at,
                payload.get("finalized_at"),
                payload.get("minutes_status"),
                payload.get("minutes_error"),
                display_title,
                meeting_id,
                principal.tenant_id,
                principal.owner_id,
            ),
        )
        return {
            "meeting_id": meeting_id,
            "title": title or "",
            "display_title": display_title,
            "state": state,
            "started_at": started_at,
            "ended_at": ended_at,
            "finalized_at": payload.get("finalized_at"),
            "minutes_status": payload.get("minutes_status"),
            "minutes_error": payload.get("minutes_error"),
        }

    async def _apply_transcript_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_id: str,
        payload: dict[str, Any],
        *,
        updated_at: str,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        meeting_id = str(payload.get("meeting_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not meeting_id or not text:
            raise SyncEntityValidationError("transcript_segment requires meeting_id and text")
        start_ms = _int_field(payload, "start_ms", 0)
        end_ms = _int_field(payload, "end_ms", start_ms)
        if end_ms < start_ms:
            raise SyncEntityValidationError("end_ms must be greater than start_ms")
        segment_id = None
        for candidate in (
            payload.get("segment_id"),
            current.get("segment_id") if current else None,
            entity_id if entity_id.isdigit() else None,
        ):
            if candidate is not None and str(candidate).isdigit():
                segment_id = int(candidate)
                break
        captured_at = str(payload.get("captured_at") or updated_at)
        await self._ensure_meeting_tx(
            conn,
            principal,
            meeting_id,
            occurred_at=captured_at,
        )
        values = (
            text,
            start_ms,
            end_ms,
            payload.get("speaker_id"),
            payload.get("speaker_label"),
            captured_at,
            principal.tenant_id,
            principal.device_id,
            principal.owner_id,
        )
        if segment_id is None:
            cursor = await conn.execute(
                """INSERT INTO meeting_segments (
                       meeting_id, text, start_ms, end_ms, speaker_id,
                       speaker_label, captured_at, tenant_id, device_id, owner_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (meeting_id, *values),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("SQLite did not return inserted segment id")
            segment_id = int(lastrowid)
            await cursor.close()
        else:
            cursor = await conn.execute(
                """UPDATE meeting_segments SET meeting_id = ?, text = ?, start_ms = ?,
                          end_ms = ?, speaker_id = ?, speaker_label = ?,
                          captured_at = ?, tenant_id = ?, device_id = ?, owner_id = ?
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                (
                    meeting_id,
                    *values,
                    segment_id,
                    principal.tenant_id,
                    principal.owner_id,
                ),
            )
            updated = cursor.rowcount == 1
            await cursor.close()
            if not updated:
                legacy_id = await self._fetchone(
                    conn,
                    "SELECT 1 FROM meeting_segments WHERE id = ?",
                    (segment_id,),
                )
                if legacy_id is None:
                    cursor = await conn.execute(
                        """INSERT INTO meeting_segments (
                               id, meeting_id, text, start_ms, end_ms, speaker_id,
                               speaker_label, captured_at, tenant_id, device_id, owner_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (segment_id, meeting_id, *values),
                    )
                else:
                    cursor = await conn.execute(
                        """INSERT INTO meeting_segments (
                               meeting_id, text, start_ms, end_ms, speaker_id,
                               speaker_label, captured_at, tenant_id, device_id, owner_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (meeting_id, *values),
                    )
                    if cursor.lastrowid is None:
                        await cursor.close()
                        raise RuntimeError("SQLite did not return inserted segment id")
                    segment_id = int(cursor.lastrowid)
                await cursor.close()
        return {
            "meeting_id": meeting_id,
            "segment_id": segment_id,
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "speaker_id": payload.get("speaker_id"),
            "speaker_label": payload.get("speaker_label"),
            "captured_at": captured_at,
        }

    async def _apply_summary_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_id: str,
        payload: dict[str, Any],
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        meeting_id = str(payload.get("meeting_id") or entity_id).strip()
        if not meeting_id:
            raise SyncEntityValidationError("meeting_summary requires meeting_id")
        canonical = dict(payload)
        canonical["meeting_id"] = meeting_id
        canonical["updated_at"] = updated_at
        await self._ensure_meeting_tx(
            conn,
            principal,
            meeting_id,
            occurred_at=updated_at,
        )
        title = str(canonical.get("title") or "").strip() or None
        display_title = str(canonical.get("display_title") or "").strip() or None
        cursor = await conn.execute(
            """UPDATE meetings SET state = 'finalized', minutes_json = ?,
                      minutes_status = 'ok', minutes_error = NULL,
                      ended_at = COALESCE(ended_at, ?), finalized_at = ?,
                      title = COALESCE(?, title), display_title = COALESCE(?, display_title)
               WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
            (
                _json_dump(canonical),
                updated_at,
                updated_at,
                title,
                display_title,
                meeting_id,
                principal.tenant_id,
                principal.owner_id,
            ),
        )
        if cursor.rowcount != 1:
            await cursor.close()
            raise SyncEntityValidationError("meeting_summary meeting not found")
        await cursor.close()
        return canonical

    async def _apply_artifact_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_id: str,
        payload: dict[str, Any],
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        artifact_id = str(payload.get("artifact_id") or entity_id).strip()
        if not _SYNC_ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise SyncEntityValidationError("artifact id is invalid")
        encoded = payload.get("content_base64")
        if not isinstance(encoded, str) or not encoded:
            raise SyncEntityValidationError("artifact content is required")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SyncEntityValidationError("artifact content is invalid") from exc
        if len(content) > 256 * 1024 * 1024:
            raise SyncEntityValidationError("artifact content is too large")
        declared_size = _int_field(payload, "size_bytes", len(content))
        if declared_size != len(content):
            raise SyncEntityValidationError("artifact size does not match content")
        declared_sha = payload.get("content_sha256")
        actual_sha = hashlib.sha256(content).hexdigest()
        if declared_sha is not None and str(declared_sha).lower() != actual_sha:
            raise SyncEntityValidationError("artifact content checksum does not match")

        artifact_type = str(payload.get("artifact_type") or "file").strip() or "file"
        title = str(payload.get("title") or "")
        mime_type = str(payload.get("mime_type") or "application/octet-stream")
        model = str(payload.get("model") or "")
        try:
            latency = float(payload.get("generation_latency_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise SyncEntityValidationError("artifact latency is invalid") from exc
        metadata = _sync_artifact_metadata(payload)
        created_at = str(payload.get("created_at") or updated_at)
        artifact_root = scoped_directory_for(
            self.artifact_build_dir,
            principal.tenant_id,
            principal.owner_id,
        ).resolve()
        artifact_dir = (artifact_root / artifact_id).resolve()
        if artifact_root not in artifact_dir.parents:
            raise SyncEntityValidationError("artifact storage path escaped scope")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        output_path = artifact_dir / f"output.{_sync_artifact_extension(artifact_type)}"
        temporary_path = artifact_dir / f".sync-{secrets.token_hex(12)}.tmp"
        try:
            temporary_path.write_bytes(content)
            os.replace(temporary_path, output_path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

        await conn.execute(
            """INSERT INTO artifacts (
                       artifact_id, artifact_type, title, file_path, mime_type,
                       size_bytes, generation_latency_ms, model, metadata_json,
                       run_id, created_at, updated_at, tenant_id, device_id, owner_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id, artifact_id) DO UPDATE SET
                       artifact_type = excluded.artifact_type,
                       title = excluded.title,
                       file_path = excluded.file_path,
                       mime_type = excluded.mime_type,
                       size_bytes = excluded.size_bytes,
                       generation_latency_ms = excluded.generation_latency_ms,
                       model = excluded.model,
                       metadata_json = excluded.metadata_json,
                       updated_at = excluded.updated_at""",
            (
                artifact_id,
                artifact_type,
                title,
                str(output_path),
                mime_type,
                len(content),
                latency,
                model,
                _json_dump(metadata),
                created_at,
                updated_at,
                principal.tenant_id,
                principal.device_id,
                principal.owner_id,
            ),
        )

        raw_links = payload.get("links")
        if isinstance(raw_links, list):
            for raw_link in raw_links:
                if not isinstance(raw_link, dict):
                    continue
                meeting_id = (
                    str(raw_link["meeting_id"]).strip()
                    if raw_link.get("meeting_id")
                    else None
                )
                if meeting_id:
                    await self._ensure_meeting_tx(
                        conn,
                        principal,
                        meeting_id,
                        occurred_at=created_at,
                    )
                source = str(raw_link.get("source") or "legacy-history")[:120]
                todo_id = str(raw_link["todo_id"]).strip() if raw_link.get("todo_id") else None
                link_run_id = None
                link_key = "\x1f".join(
                    [
                        principal.tenant_id,
                        principal.owner_id,
                        artifact_id,
                        source,
                        meeting_id or "",
                        todo_id or "",
                        link_run_id or "",
                    ]
                )
                link_id = f"alink_{hashlib.sha1(link_key.encode('utf-8')).hexdigest()[:24]}"
                await conn.execute(
                    """INSERT INTO artifact_links (
                               link_id, artifact_id, source, meeting_id, todo_id,
                               run_id, created_at, tenant_id, device_id, owner_id
                           ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                           ON CONFLICT(tenant_id, owner_id, link_id) DO NOTHING""",
                    (
                        link_id,
                        artifact_id,
                        source,
                        meeting_id,
                        todo_id,
                        created_at,
                        principal.tenant_id,
                        principal.device_id,
                        principal.owner_id,
                    ),
                )

        return {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "title": title,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "generation_latency_ms": latency,
            "model": model,
            "metadata": metadata,
            "created_at": created_at,
            "updated_at": updated_at,
            "content_sha256": actual_sha,
            "content_base64": None,
        }

    async def _apply_memory_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_id: str,
        payload: dict[str, Any],
        *,
        revision: int,
        updated_at: str,
    ) -> dict[str, Any]:
        memory_id = str(payload.get("memory_id") or entity_id).strip()
        kind = str(payload.get("kind") or "fact")
        if kind not in {"fact", "preference", "decision", "todo", "relationship"}:
            raise SyncEntityValidationError("memory kind is invalid")
        content = str(payload.get("content") or "").strip()
        if not memory_id or not content:
            raise SyncEntityValidationError("memory requires memory_id and content")
        canonical_key = normalize_key(str(payload.get("canonical_key") or memory_id))
        status = str(payload.get("status") or "active")
        if status not in {"active", "superseded", "deleted"}:
            raise SyncEntityValidationError("memory status is invalid")
        try:
            confidence = float(payload.get("confidence", 1.0))
            salience = float(payload.get("salience", 0.5))
        except (TypeError, ValueError) as exc:
            raise SyncEntityValidationError(
                "memory confidence and salience must be numbers"
            ) from exc
        if not 0 <= confidence <= 1 or not 0 <= salience <= 1:
            raise SyncEntityValidationError("memory confidence and salience must be in [0, 1]")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        current = await self._fetchone(
            conn,
            """SELECT memory_id FROM memory_nodes
               WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
            (principal.tenant_id, principal.owner_id, memory_id),
        )
        if current is None:
            await conn.execute(
                """INSERT INTO memory_nodes (
                       tenant_id, owner_id, memory_id, kind, content,
                       normalized_content, canonical_key, subject, confidence,
                       salience, scope, status, created_at, last_seen_at,
                       updated_at, revision, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    principal.tenant_id,
                    principal.owner_id,
                    memory_id,
                    kind,
                    content,
                    normalize_text(content),
                    canonical_key,
                    payload.get("subject"),
                    confidence,
                    salience,
                    str(payload.get("scope") or "owner"),
                    status,
                    updated_at,
                    updated_at,
                    updated_at,
                    revision,
                    _json_dump(metadata),
                ),
            )
        else:
            await conn.execute(
                """UPDATE memory_nodes SET kind = ?, content = ?, normalized_content = ?,
                          canonical_key = ?, subject = ?, confidence = ?, salience = ?,
                          scope = ?, status = ?, updated_at = ?, last_seen_at = ?,
                          revision = ?, metadata_json = ?,
                          deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE NULL END
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (
                    kind,
                    content,
                    normalize_text(content),
                    canonical_key,
                    payload.get("subject"),
                    confidence,
                    salience,
                    str(payload.get("scope") or "owner"),
                    status,
                    updated_at,
                    updated_at,
                    revision,
                    _json_dump(metadata),
                    status,
                    updated_at,
                    principal.tenant_id,
                    principal.owner_id,
                    memory_id,
                ),
            )
        return {
            "memory_id": memory_id,
            "kind": kind,
            "content": content,
            "canonical_key": canonical_key,
            "subject": payload.get("subject"),
            "confidence": confidence,
            "salience": salience,
            "scope": str(payload.get("scope") or "owner"),
            "status": status,
            "metadata": metadata,
            "revision": revision,
        }

    async def _next_cursor_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
    ) -> int:
        row = await self._fetchone(
            conn,
            """SELECT COALESCE(MAX(cursor), 0) AS cursor
               FROM sync_events WHERE tenant_id = ? AND owner_id = ?""",
            (principal.tenant_id, principal.owner_id),
        )
        return int(row["cursor"] if row is not None else 0) + 1

    async def _apply_entity_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        entity_type: SyncEntityType,
        entity_id: str,
        payload: dict[str, Any],
        *,
        revision: int,
        updated_at: str,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if entity_type == "meeting":
            return await self._apply_meeting_tx(
                conn,
                principal,
                entity_id,
                payload,
                updated_at=updated_at,
            )
        if entity_type == "transcript_segment":
            return await self._apply_transcript_tx(
                conn,
                principal,
                entity_id,
                payload,
                updated_at=updated_at,
                current=current,
            )
        if entity_type == "meeting_summary":
            return await self._apply_summary_tx(
                conn,
                principal,
                entity_id,
                payload,
                updated_at=updated_at,
            )
        if entity_type == "artifact":
            return await self._apply_artifact_tx(
                conn,
                principal,
                entity_id,
                payload,
                updated_at=updated_at,
            )
        return await self._apply_memory_tx(
            conn,
            principal,
            entity_id,
            payload,
            revision=revision,
            updated_at=updated_at,
        )

    def _notify_scope(self, principal: Principal) -> None:
        waiter = self._change_waiters.get((principal.tenant_id, principal.owner_id))
        if waiter is not None:
            waiter.set()

    async def push(
        self,
        principal: Principal,
        *,
        operation_id: str,
        device_id: str,
        entity_type: SyncEntityType,
        entity_id: str,
        base_revision: int,
        updated_at: datetime,
        payload: dict[str, Any],
    ) -> PushResult:
        if device_id != principal.device_id:
            raise SyncEntityValidationError("device_id does not match authenticated device")
        if operation_id.startswith("capture:"):
            raise SyncEntityValidationError("sync operation_id uses a reserved namespace")
        updated_iso = _as_utc(updated_at).isoformat()
        result: PushResult
        changed = False
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                operation = await self._fetchone(
                    conn,
                    """SELECT tenant_id, owner_id, status, revision, cursor, current_json
                       FROM sync_operations
                      WHERE tenant_id = ? AND owner_id = ? AND operation_id = ?""",
                    (principal.tenant_id, principal.owner_id, operation_id),
                )
                if operation is not None:
                    result = PushResult(
                        status="duplicate",
                        revision=int(operation["revision"]),
                        cursor=(
                            int(operation["cursor"]) if operation["cursor"] is not None else None
                        ),
                        current=_json_load(operation["current_json"]),
                    )
                    await conn.commit()
                    return result
                current_revision, current_cursor, current = await self._current_entity_tx(
                    conn,
                    principal,
                    entity_type,
                    entity_id,
                )
                if base_revision != current_revision:
                    await conn.execute(
                        """INSERT INTO sync_operations (
                               operation_id, tenant_id, owner_id, source_device_id,
                               entity_type, entity_id, base_revision, updated_at,
                               payload_json, status, revision, cursor, current_json, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'conflict', ?, ?, ?, ?)""",
                        (
                            operation_id,
                            principal.tenant_id,
                            principal.owner_id,
                            principal.device_id,
                            entity_type,
                            entity_id,
                            base_revision,
                            updated_iso,
                            _json_dump(payload),
                            current_revision,
                            current_cursor,
                            _json_dump(current) if current is not None else None,
                            updated_iso,
                        ),
                    )
                    result = PushResult(
                        status="conflict",
                        revision=current_revision,
                        cursor=current_cursor,
                        current=current,
                    )
                    await conn.commit()
                    return result
                revision = current_revision + 1
                canonical = await self._apply_entity_tx(
                    conn,
                    principal,
                    entity_type,
                    entity_id,
                    payload,
                    revision=revision,
                    updated_at=updated_iso,
                    current=current,
                )
                cursor = await self._next_cursor_tx(conn, principal)
                await conn.execute(
                    """INSERT INTO sync_events (
                           tenant_id, owner_id, cursor, source_device_id,
                           entity_type, entity_id, revision, updated_at, payload_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        principal.tenant_id,
                        principal.owner_id,
                        cursor,
                        principal.device_id,
                        entity_type,
                        entity_id,
                        revision,
                        updated_iso,
                        _json_dump(canonical),
                    ),
                )
                await conn.execute(
                    """INSERT INTO sync_operations (
                           operation_id, tenant_id, owner_id, source_device_id,
                           entity_type, entity_id, base_revision, updated_at,
                           payload_json, status, revision, cursor, current_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?)""",
                    (
                        operation_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                        entity_type,
                        entity_id,
                        base_revision,
                        updated_iso,
                        _json_dump(payload),
                        revision,
                        cursor,
                        _json_dump(canonical),
                        updated_iso,
                    ),
                )
                await conn.execute(
                    """UPDATE device_cursors SET cursor = MAX(cursor, ?), updated_at = ?
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?""",
                    (
                        cursor,
                        updated_iso,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                    ),
                )
                await conn.commit()
                result = PushResult(status="applied", revision=revision, cursor=cursor)
                changed = True
            except BaseException:
                await conn.rollback()
                raise
        if changed:
            self._notify_scope(principal)
        return result

    async def list_changes(
        self,
        principal: Principal,
        *,
        cursor: int,
        limit: int,
    ) -> tuple[int, list[SyncChangeRecord]]:
        async with self._conn() as conn:
            rows = await self._fetchall(
                conn,
                """SELECT cursor, source_device_id, entity_type, entity_id,
                          revision, updated_at, payload_json
                   FROM sync_events
                   WHERE tenant_id = ? AND owner_id = ? AND cursor > ?
                   ORDER BY cursor ASC LIMIT ?""",
                (principal.tenant_id, principal.owner_id, cursor, limit),
            )
            latest = await self._fetchone(
                conn,
                """SELECT COALESCE(MAX(cursor), 0) AS cursor
                   FROM sync_events WHERE tenant_id = ? AND owner_id = ?""",
                (principal.tenant_id, principal.owner_id),
            )
            next_cursor = int(latest["cursor"] if latest is not None else cursor)
            returned_cursor = int(rows[-1]["cursor"]) if rows else next_cursor
            await conn.execute(
                """UPDATE device_cursors SET cursor = MAX(cursor, ?), updated_at = ?
                   WHERE tenant_id = ? AND owner_id = ? AND device_id = ?""",
                (
                    returned_cursor,
                    _as_utc(self._now()).isoformat(),
                    principal.tenant_id,
                    principal.owner_id,
                    principal.device_id,
                ),
            )
            await conn.commit()
        return returned_cursor, [self._change_from_row(row) for row in rows]

    async def history_manifest(self, principal: Principal) -> SyncHistoryManifestRecord:
        async with self._conn() as conn:
            row = await self._fetchone(
                conn,
                """SELECT COALESCE(MAX(cursor), 0) AS cursor,
                          COUNT(DISTINCT CASE WHEN entity_type = 'meeting'
                                             THEN entity_id END) AS meeting_count,
                          COUNT(DISTINCT CASE WHEN entity_type = 'transcript_segment'
                                             THEN entity_id END) AS transcript_segment_count,
                          COUNT(DISTINCT CASE WHEN entity_type = 'meeting_summary'
                                             THEN entity_id END) AS meeting_summary_count,
                          COUNT(DISTINCT CASE WHEN entity_type = 'artifact'
                                             THEN entity_id END) AS artifact_count
                   FROM sync_events
                   WHERE tenant_id = ? AND owner_id = ?""",
                (principal.tenant_id, principal.owner_id),
            )
        return SyncHistoryManifestRecord(
            cursor=int(row["cursor"] if row is not None else 0),
            meeting_count=int(row["meeting_count"] if row is not None else 0),
            transcript_segment_count=int(
                row["transcript_segment_count"] if row is not None else 0
            ),
            meeting_summary_count=int(
                row["meeting_summary_count"] if row is not None else 0
            ),
            artifact_count=int(row["artifact_count"] if row is not None else 0),
        )

    @staticmethod
    def _change_from_row(row: aiosqlite.Row) -> SyncChangeRecord:
        return SyncChangeRecord(
            cursor=int(row["cursor"]),
            source_device_id=str(row["source_device_id"]),
            entity_type=str(row["entity_type"]),  # type: ignore[arg-type]
            entity_id=str(row["entity_id"]),
            revision=int(row["revision"]),
            updated_at=_parse_datetime(row["updated_at"]),
            payload=_json_load(row["payload_json"]) or {},
        )

    async def wait_for_change(
        self,
        principal: Principal,
        *,
        cursor: int,
        timeout_s: float = 15.0,
    ) -> None:
        key = (principal.tenant_id, principal.owner_id)
        waiter = self._change_waiters.setdefault(key, asyncio.Event())
        waiter.clear()
        async with self._conn() as conn:
            row = await self._fetchone(
                conn,
                """SELECT 1 FROM sync_events
                   WHERE tenant_id = ? AND owner_id = ? AND cursor > ?
                   LIMIT 1""",
                (principal.tenant_id, principal.owner_id, cursor),
            )
        if row is not None:
            return
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_s)
        except TimeoutError:
            return
        finally:
            waiter.clear()

    async def snapshot(self, principal: Principal) -> SnapshotResult:
        scope = (principal.tenant_id, principal.owner_id)
        async with self._conn() as conn:
            event_rows = await self._fetchall(
                conn,
                """SELECT cursor, source_device_id, entity_type, entity_id,
                          revision, updated_at, payload_json
                   FROM sync_events WHERE tenant_id = ? AND owner_id = ?
                   ORDER BY cursor ASC""",
                scope,
            )
            latest_events: dict[tuple[str, str], aiosqlite.Row] = {}
            for row in event_rows:
                latest_events[(str(row["entity_type"]), str(row["entity_id"]))] = row
            transcript_events = sorted(
                (
                    row
                    for (entity_type, _entity_id), row in latest_events.items()
                    if entity_type == "transcript_segment"
                ),
                key=lambda row: int(row["cursor"]),
            )
            meeting_events = sorted(
                (
                    row
                    for (entity_type, _entity_id), row in latest_events.items()
                    if entity_type == "meeting"
                ),
                key=lambda row: int(row["cursor"]),
            )
            artifact_events = sorted(
                (
                    row
                    for (entity_type, _entity_id), row in latest_events.items()
                    if entity_type == "artifact"
                ),
                key=lambda row: int(row["cursor"]),
            )
            transcript_rows = await self._fetchall(
                conn,
                """SELECT id, meeting_id, text, start_ms, end_ms,
                          speaker_id, speaker_label, captured_at, device_id
                   FROM meeting_segments
                   WHERE tenant_id = ? AND owner_id = ? ORDER BY id ASC""",
                scope,
            )
            summary_rows = await self._fetchall(
                conn,
                """SELECT id, minutes_json, finalized_at, ended_at, started_at, device_id
                   FROM meetings
                   WHERE tenant_id = ? AND owner_id = ? AND minutes_json IS NOT NULL
                   ORDER BY COALESCE(finalized_at, ended_at, started_at) ASC, id ASC""",
                scope,
            )
            memory_rows = await self._fetchall(
                conn,
                """SELECT * FROM memory_nodes
                   WHERE tenant_id = ? AND owner_id = ? AND status = 'active'
                   ORDER BY updated_at ASC, memory_id ASC""",
                scope,
            )
            max_row = await self._fetchone(
                conn,
                """SELECT COALESCE(MAX(cursor), 0) AS cursor
                   FROM sync_events WHERE tenant_id = ? AND owner_id = ?""",
                scope,
            )
        transcript = self._snapshot_transcripts(transcript_rows, transcript_events)
        meetings = [self._change_from_row(row) for row in meeting_events]
        summaries = [self._snapshot_summary(row, latest_events) for row in summary_rows]
        artifacts = [self._change_from_row(row) for row in artifact_events]
        memories = [self._snapshot_memory(row, latest_events) for row in memory_rows]
        return SnapshotResult(
            cursor=int(max_row["cursor"] if max_row is not None else 0),
            meetings=meetings,
            transcript_segments=transcript,
            meeting_summaries=summaries,
            artifacts=artifacts,
            memories=memories,
        )

    @staticmethod
    def _transcript_event_matches_row(
        event: aiosqlite.Row,
        row: aiosqlite.Row,
    ) -> bool:
        payload = _json_load(event["payload_json"]) or {}
        try:
            if payload.get("segment_id") is not None and int(payload["segment_id"]) == int(
                row["id"]
            ):
                return True
        except (TypeError, ValueError):
            pass
        try:
            return (
                str(payload.get("meeting_id")) == str(row["meeting_id"])
                and int(payload["start_ms"]) == int(row["start_ms"])
                and int(payload["end_ms"]) == int(row["end_ms"])
                and str(payload.get("text")) == str(row["text"])
            )
        except (KeyError, TypeError, ValueError):
            return False

    @classmethod
    def _snapshot_transcripts(
        cls,
        rows: list[aiosqlite.Row],
        events: list[aiosqlite.Row],
    ) -> list[SyncChangeRecord]:
        consumed_cursors: set[int] = set()
        emitted_cursors: set[int] = set()
        records: list[SyncChangeRecord] = []
        for row in rows:
            matching = [event for event in events if cls._transcript_event_matches_row(event, row)]
            consumed_cursors.update(int(event["cursor"]) for event in matching)
            if not matching:
                records.append(cls._snapshot_transcript(row, {}))
                continue
            event = max(matching, key=lambda candidate: int(candidate["cursor"]))
            cursor = int(event["cursor"])
            if cursor in emitted_cursors:
                continue
            emitted_cursors.add(cursor)
            records.append(cls._change_from_row(event))

        for event in events:
            if int(event["cursor"]) not in consumed_cursors:
                records.append(cls._change_from_row(event))
        return records

    @staticmethod
    def _snapshot_transcript(
        row: aiosqlite.Row,
        events: dict[tuple[str, str], aiosqlite.Row],
    ) -> SyncChangeRecord:
        entity_id = str(row["id"])
        event = events.get(("transcript_segment", entity_id))
        return SyncChangeRecord(
            cursor=int(event["cursor"]) if event is not None else 0,
            source_device_id=(
                str(event["source_device_id"]) if event is not None else str(row["device_id"])
            ),
            entity_type="transcript_segment",
            entity_id=entity_id,
            revision=int(event["revision"]) if event is not None else 0,
            updated_at=_parse_datetime(row["captured_at"]),
            payload={
                "meeting_id": str(row["meeting_id"]),
                "segment_id": int(row["id"]),
                "text": str(row["text"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "speaker_id": row["speaker_id"],
                "speaker_label": row["speaker_label"],
                "captured_at": str(row["captured_at"]),
            },
        )

    @staticmethod
    def _snapshot_summary(
        row: aiosqlite.Row,
        events: dict[tuple[str, str], aiosqlite.Row],
    ) -> SyncChangeRecord:
        entity_id = str(row["id"])
        event = events.get(("meeting_summary", entity_id))
        if event is not None:
            return SyncHubStore._change_from_row(event)
        payload = _json_load(row["minutes_json"]) or {}
        payload["meeting_id"] = entity_id
        occurred_at = row["finalized_at"] or row["ended_at"] or row["started_at"]
        payload["updated_at"] = str(occurred_at)
        return SyncChangeRecord(
            cursor=0,
            source_device_id=str(row["device_id"]),
            entity_type="meeting_summary",
            entity_id=entity_id,
            revision=0,
            updated_at=_parse_datetime(occurred_at),
            payload=payload,
        )

    @staticmethod
    def _snapshot_memory(
        row: aiosqlite.Row,
        events: dict[tuple[str, str], aiosqlite.Row],
    ) -> SyncChangeRecord:
        entity_id = str(row["memory_id"])
        event = events.get(("memory", entity_id))
        if event is not None:
            return SyncHubStore._change_from_row(event)
        return SyncChangeRecord(
            cursor=0,
            source_device_id="sync-snapshot",
            entity_type="memory",
            entity_id=entity_id,
            revision=int(row["revision"]),
            updated_at=_parse_datetime(row["updated_at"]),
            payload=SyncHubStore._memory_payload(row),
        )

    async def list_devices(self, principal: Principal) -> list[SyncDeviceRecord]:
        async with self._conn() as conn:
            rows = await (
                await conn.execute(
                    """SELECT d.device_id, d.device_name, d.platform,
                              d.created_at, d.last_seen_at, d.revoked_at,
                              COALESCE(c.cursor, 0) AS cursor
                       FROM sync_devices d
                       LEFT JOIN device_cursors c
                         ON c.tenant_id = d.tenant_id
                        AND c.owner_id = d.owner_id
                        AND c.device_id = d.device_id
                       WHERE d.tenant_id = ? AND d.owner_id = ?
                       ORDER BY d.created_at ASC, d.device_id ASC""",
                    (principal.tenant_id, principal.owner_id),
                )
            ).fetchall()
        return [
            SyncDeviceRecord(
                device_id=str(row["device_id"]),
                device_name=str(row["device_name"]),
                platform=str(row["platform"]),
                created_at=_parse_datetime(row["created_at"]),
                last_seen_at=_parse_datetime(row["last_seen_at"]),
                revoked_at=(
                    _parse_datetime(row["revoked_at"]) if row["revoked_at"] is not None else None
                ),
                cursor=int(row["cursor"]),
            )
            for row in rows
        ]

    async def revoke_device(self, principal: Principal, device_id: str) -> SyncDeviceRecord:
        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """UPDATE sync_devices SET revoked_at = COALESCE(revoked_at, ?),
                              last_seen_at = ?
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?""",
                    (
                        now,
                        now,
                        principal.tenant_id,
                        principal.owner_id,
                        device_id,
                    ),
                )
                changed = cur.rowcount
                await cur.close()
                if changed != 1:
                    raise SyncDeviceNotFoundError("sync device not found")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        devices = await self.list_devices(principal)
        return next(item for item in devices if item.device_id == device_id)


__all__ = [
    "ClaimedDevice",
    "DeviceAlreadyExistsError",
    "PairingNotFoundError",
    "PairingRecord",
    "PushResult",
    "SnapshotResult",
    "SyncChangeRecord",
    "SyncDeviceNotFoundError",
    "SyncDeviceRecord",
    "SyncEntityValidationError",
    "SyncHubStore",
]
