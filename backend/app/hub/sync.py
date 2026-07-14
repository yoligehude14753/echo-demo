"""Durable Hub sync outbox and repository projection bridge."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.adapters.repo.connection import configure_aiosqlite_connection
from app.adapters.repo.migrator import run_migrations
from app.security.models import local_principal

EntityType = Literal["transcript_segment", "meeting_summary", "memory"]
_ENTITY_TYPES = {"transcript_segment", "meeting_summary", "memory"}
_VALID_MEETING_STATES = {"in_meeting", "ended", "finalized"}
_VALID_MINUTES_STATES = {"generating", "ok", "generation_failed", "cancelled"}
_VALID_MEMORY_STATES = {"active", "superseded", "deleted"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _comparison_payload(entity_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if entity_type == "transcript_segment":
        # SQLite assigns a new local integer id when a remote segment is
        # applied.  The Hub entity id remains the source device's id.
        stable_keys = {
            "meeting_id",
            "text",
            "start_ms",
            "end_ms",
            "speaker_id",
            "speaker_label",
            "captured_at",
        }
        return {key: value for key, value in payload.items() if key in stable_keys}
    return payload


def _load(value: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(value: object, *, limit: int = 8_000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _required_text(value: object, *, limit: int = 512) -> str:
    text = _text(value, limit=limit)
    if not text:
        raise ValueError("missing sync field")
    return text


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class SyncOperation:
    operation_id: str
    device_id: str
    entity_type: EntityType
    entity_id: str
    base_revision: int
    updated_at: str
    payload: dict[str, Any]
    attempts: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SyncOperation:
        return cls(
            operation_id=str(row["operation_id"]),
            device_id=str(row["device_id"]),
            entity_type=str(row["entity_type"]),  # type: ignore[arg-type]
            entity_id=str(row["entity_id"]),
            base_revision=int(row["base_revision"]),
            updated_at=str(row["updated_at"]),
            payload=_load(row["payload_json"]),
            attempts=int(row["attempts"]),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "device_id": self.device_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "base_revision": self.base_revision,
            "updated_at": self.updated_at,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class SyncApplySummary:
    applied: int = 0
    duplicate: int = 0
    conflict: int = 0


class HubSyncStore:
    """Bridge existing SQLite repository tables to the Hub sync protocol."""

    def __init__(self, db_path: str | Path, *, device_id: str) -> None:
        self.db_path = Path(db_path).expanduser()
        self.device_id = device_id
        principal = local_principal()
        self.tenant_id = principal.tenant_id
        self.owner_id = principal.owner_id
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        migration = await run_migrations(self.db_path)
        if migration.errors:
            raise RuntimeError(f"hub sync migration failed: {migration.errors}")
        async with self._lock:
            if self._conn is not None:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            await configure_aiosqlite_connection(self._conn)
            self._conn.row_factory = sqlite3.Row
            await self._conn.execute(
                "UPDATE hub_sync_outbox SET state = 'pending', state_updated_at = ? "
                "WHERE state = 'sending'",
                (_now(),),
            )
            await self._conn.commit()

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("HubSyncStore.init() not called")
        return self._conn

    async def reconcile_local_changes(self, *, limit: int = 200) -> int:
        """Create minimal outbox rows for newly persisted local repository data."""

        limit = max(1, min(limit, 2_000))
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                queued = 0
                queued += await self._reconcile_segments(conn, limit)
                queued += await self._reconcile_summaries(conn, limit)
                queued += await self._reconcile_memories(conn, limit)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return queued

    async def _reconcile_segments(self, conn: aiosqlite.Connection, limit: int) -> int:
        cursor = await conn.execute(
            "SELECT id, meeting_id, text, start_ms, end_ms, speaker_id, speaker_label, "
            "captured_at FROM meeting_segments WHERE tenant_id = ? AND owner_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (self.tenant_id, self.owner_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        queued = 0
        for row in rows:
            payload = {
                "segment_id": str(row["id"]),
                "meeting_id": str(row["meeting_id"]),
                "text": str(row["text"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "speaker_id": row["speaker_id"],
                "speaker_label": row["speaker_label"],
                "captured_at": str(row["captured_at"]),
            }
            queued += await self._queue_local_entity(
                conn,
                entity_type="transcript_segment",
                raw_entity_id=str(row["id"]),
                updated_at=str(row["captured_at"]),
                payload=payload,
            )
        return queued

    async def _reconcile_summaries(self, conn: aiosqlite.Connection, limit: int) -> int:
        cursor = await conn.execute(
            "SELECT id, title, display_title, started_at, ended_at, finalized_at, "
            "minutes_json, minutes_status, minutes_error, minutes_cleared_at "
            "FROM meetings WHERE tenant_id = ? AND owner_id = ? "
            "AND (minutes_json IS NOT NULL OR minutes_status IS NOT NULL "
            "OR minutes_cleared_at IS NOT NULL) "
            "ORDER BY COALESCE(finalized_at, ended_at, started_at) ASC LIMIT ?",
            (self.tenant_id, self.owner_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        queued = 0
        for row in rows:
            updated_at = str(
                row["finalized_at"]
                or row["ended_at"]
                or row["minutes_cleared_at"]
                or row["started_at"]
            )
            payload = {
                "meeting_id": str(row["id"]),
                "title": row["title"],
                "display_title": row["display_title"],
                "started_at": str(row["started_at"]),
                "ended_at": row["ended_at"],
                "finalized_at": row["finalized_at"],
                "minutes_json": row["minutes_json"],
                "minutes_status": row["minutes_status"],
                "minutes_error": row["minutes_error"],
                "minutes_cleared_at": row["minutes_cleared_at"],
                "deleted": row["minutes_json"] is None,
            }
            queued += await self._queue_local_entity(
                conn,
                entity_type="meeting_summary",
                raw_entity_id=str(row["id"]),
                updated_at=updated_at,
                payload=payload,
            )
        return queued

    async def _reconcile_memories(self, conn: aiosqlite.Connection, limit: int) -> int:
        cursor = await conn.execute(
            "SELECT memory_id, kind, content, normalized_content, canonical_key, subject, "
            "confidence, salience, scope, status, hit_count, source_count, "
            "user_confirmed, created_at, last_seen_at, updated_at, confirmed_at, "
            "superseded_at, superseded_by, deleted_at, revision, metadata_json "
            "FROM memory_nodes WHERE tenant_id = ? AND owner_id = ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (self.tenant_id, self.owner_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        queued = 0
        for row in rows:
            payload = {
                "memory_id": str(row["memory_id"]),
                "kind": str(row["kind"]),
                "content": str(row["content"]),
                "normalized_content": str(row["normalized_content"]),
                "canonical_key": str(row["canonical_key"]),
                "subject": row["subject"],
                "confidence": float(row["confidence"]),
                "salience": float(row["salience"]),
                "scope": str(row["scope"]),
                "status": str(row["status"]),
                "hit_count": int(row["hit_count"]),
                "source_count": int(row["source_count"]),
                "user_confirmed": bool(row["user_confirmed"]),
                "created_at": str(row["created_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "updated_at": str(row["updated_at"]),
                "confirmed_at": row["confirmed_at"],
                "superseded_at": row["superseded_at"],
                "superseded_by": row["superseded_by"],
                "deleted_at": row["deleted_at"],
                "revision": int(row["revision"]),
                "metadata": _load(row["metadata_json"]),
            }
            queued += await self._queue_local_entity(
                conn,
                entity_type="memory",
                raw_entity_id=str(row["memory_id"]),
                updated_at=str(row["updated_at"]),
                payload=payload,
            )
        return queued

    async def _queue_local_entity(
        self,
        conn: aiosqlite.Connection,
        *,
        entity_type: EntityType,
        raw_entity_id: str,
        updated_at: str,
        payload: dict[str, Any],
    ) -> int:
        entity_id = f"{self.device_id}:{raw_entity_id}"
        payload_json = _dump(payload)
        cursor = await conn.execute(
            "SELECT revision, payload_json FROM hub_sync_entities "
            "WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
        current = await cursor.fetchone()
        await cursor.close()
        if current is not None and str(current["payload_json"]) == payload_json:
            return 0

        # A remote entity may have the same content but a source-prefixed id.
        # Record a local alias so the next reconcile does not create a loop.
        cursor = await conn.execute(
            "SELECT revision, source_device_id FROM hub_sync_entities "
            "WHERE entity_type = ? AND payload_json = ? LIMIT 1",
            (entity_type, payload_json),
        )
        equivalent = await cursor.fetchone()
        await cursor.close()
        if current is None and equivalent is not None:
            await conn.execute(
                "INSERT OR IGNORE INTO hub_sync_entities "
                "(entity_type, entity_id, revision, updated_at, payload_json, "
                "source_device_id, operation_id) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    entity_type,
                    entity_id,
                    int(equivalent["revision"]),
                    updated_at,
                    payload_json,
                    str(equivalent["source_device_id"]),
                ),
            )
            return 0

        if current is None:
            comparison = _dump(_comparison_payload(entity_type, payload))
            cursor = await conn.execute(
                "SELECT revision, source_device_id, payload_json FROM hub_sync_entities "
                "WHERE entity_type = ?",
                (entity_type,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                if (
                    _dump(_comparison_payload(entity_type, _load(row["payload_json"])))
                    != comparison
                ):
                    continue
                await conn.execute(
                    "INSERT OR IGNORE INTO hub_sync_entities "
                    "(entity_type, entity_id, revision, updated_at, payload_json, "
                    "source_device_id, operation_id) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        entity_type,
                        entity_id,
                        int(row["revision"]),
                        updated_at,
                        payload_json,
                        str(row["source_device_id"]),
                    ),
                )
                return 0

        base_revision = int(current["revision"]) if current is not None else 0
        operation_id = f"sync:{self.device_id}:{uuid4().hex}"
        now = _now()
        await conn.execute(
            "INSERT INTO hub_sync_outbox (operation_id, device_id, entity_type, entity_id, "
            "base_revision, updated_at, payload_json, state, attempts, created_at, state_updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                operation_id,
                self.device_id,
                entity_type,
                entity_id,
                base_revision,
                updated_at,
                payload_json,
                now,
                now,
            ),
        )
        await conn.execute(
            "INSERT INTO hub_sync_entities (entity_type, entity_id, revision, updated_at, "
            "payload_json, source_device_id, operation_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_type, entity_id) DO UPDATE SET revision = excluded.revision, "
            "updated_at = excluded.updated_at, payload_json = excluded.payload_json, "
            "source_device_id = excluded.source_device_id, operation_id = excluded.operation_id",
            (
                entity_type,
                entity_id,
                base_revision + 1,
                updated_at,
                payload_json,
                self.device_id,
                operation_id,
            ),
        )
        return 1

    async def claim_pending(self, *, limit: int = 50) -> list[SyncOperation]:
        limit = max(1, min(limit, 200))
        async with self._lock:
            conn = self._require_conn()
            now = _now()
            await conn.execute(
                "UPDATE hub_sync_outbox SET state = 'sending', attempts = attempts + 1, "
                "state_updated_at = ? WHERE state IN ('pending', 'failed') "
                "AND attempts < 10",
                (now,),
            )
            cursor = await conn.execute(
                "SELECT * FROM hub_sync_outbox WHERE state = 'sending' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            await conn.commit()
        return [SyncOperation.from_row(row) for row in rows]

    async def mark_push_results(
        self,
        *,
        applied: list[str],
        duplicate: list[str],
        conflict: list[str],
    ) -> None:
        async with self._lock:
            conn = self._require_conn()
            now = _now()
            for state, operation_ids in (
                ("applied", applied),
                ("duplicate", duplicate),
                ("conflict", conflict),
            ):
                if not operation_ids:
                    continue
                placeholders = ",".join("?" for _ in operation_ids)
                await conn.execute(
                    f"UPDATE hub_sync_outbox SET state = ?, last_error = ?, state_updated_at = ? "
                    f"WHERE operation_id IN ({placeholders})",
                    (state, "conflict" if state == "conflict" else None, now, *operation_ids),
                )
            await conn.commit()

    async def mark_failed(self, operation_ids: list[str], *, error: str = "sync_failed") -> None:
        if not operation_ids:
            return
        async with self._lock:
            conn = self._require_conn()
            placeholders = ",".join("?" for _ in operation_ids)
            await conn.execute(
                f"UPDATE hub_sync_outbox SET state = 'failed', last_error = ?, state_updated_at = ? "
                f"WHERE operation_id IN ({placeholders})",
                (error, _now(), *operation_ids),
            )
            await conn.commit()

    async def apply_changes(
        self,
        changes: list[dict[str, Any]],
        *,
        snapshot: bool = False,
    ) -> SyncApplySummary:
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            applied = duplicate = conflict = 0
            try:
                for index, change in enumerate(changes):
                    result = await self._apply_change_tx(
                        conn,
                        change,
                        snapshot=snapshot,
                        fallback_operation_id=f"sync:snapshot:{index}:{uuid4().hex}",
                    )
                    if result == "applied":
                        applied += 1
                    elif result == "duplicate":
                        duplicate += 1
                    else:
                        conflict += 1
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return SyncApplySummary(applied=applied, duplicate=duplicate, conflict=conflict)

    async def _apply_change_tx(
        self,
        conn: aiosqlite.Connection,
        change: dict[str, Any],
        *,
        snapshot: bool,
        fallback_operation_id: str,
    ) -> Literal["applied", "duplicate", "conflict"]:
        try:
            operation_id = _required_text(change.get("operation_id") or fallback_operation_id)
            entity_type = _required_text(change.get("entity_type"))
            entity_id = _required_text(change.get("entity_id"))
            source_device_id = _required_text(
                change.get("device_id") or change.get("source_device_id")
            )
            updated_at = _required_text(change.get("updated_at"), limit=128)
            payload = change.get("payload")
            if entity_type not in _ENTITY_TYPES or not isinstance(payload, dict):
                raise ValueError("invalid sync payload")
            base_revision = max(0, _int(change.get("base_revision")))
        except ValueError:
            return "conflict"

        cursor = await conn.execute(
            "SELECT 1 FROM hub_sync_applied_operations WHERE operation_id = ?",
            (operation_id,),
        )
        seen = await cursor.fetchone()
        await cursor.close()
        if seen is not None:
            return "duplicate"

        payload_json = _dump(payload)
        cursor = await conn.execute(
            "SELECT revision, payload_json, source_device_id FROM hub_sync_entities "
            "WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
        current = await cursor.fetchone()
        await cursor.close()
        incoming_revision = max(1, _int(change.get("revision"), base_revision + 1))
        if (
            entity_type == "transcript_segment"
            and current is not None
            and str(current["source_device_id"]) == source_device_id
            and incoming_revision <= int(current["revision"])
        ):
            await conn.execute(
                "INSERT OR IGNORE INTO hub_sync_applied_operations (operation_id, applied_at) "
                "VALUES (?, ?)",
                (operation_id, _now()),
            )
            return "duplicate"

        if current is not None and str(current["payload_json"]) == payload_json:
            await conn.execute(
                "INSERT OR IGNORE INTO hub_sync_applied_operations (operation_id, applied_at) "
                "VALUES (?, ?)",
                (operation_id, _now()),
            )
            return "duplicate"
        if current is not None and not snapshot and base_revision < int(current["revision"]):
            return "conflict"

        previous_payload = _load(current["payload_json"]) if current is not None else None
        await self._apply_entity_tx(
            conn,
            entity_type,
            payload,
            previous_payload=previous_payload if entity_type == "transcript_segment" else None,
        )
        revision = (
            incoming_revision
            if entity_type == "transcript_segment"
            else max(base_revision + 1, int(current["revision"]) + 1 if current else 1)
        )
        await conn.execute(
            "INSERT INTO hub_sync_entities (entity_type, entity_id, revision, updated_at, "
            "payload_json, source_device_id, operation_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_type, entity_id) DO UPDATE SET revision = excluded.revision, "
            "updated_at = excluded.updated_at, payload_json = excluded.payload_json, "
            "source_device_id = excluded.source_device_id, operation_id = excluded.operation_id",
            (
                entity_type,
                entity_id,
                revision,
                updated_at,
                payload_json,
                source_device_id,
                operation_id,
            ),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO hub_sync_applied_operations (operation_id, applied_at) "
            "VALUES (?, ?)",
            (operation_id, _now()),
        )
        return "applied"

    async def _apply_entity_tx(
        self,
        conn: aiosqlite.Connection,
        entity_type: str,
        payload: dict[str, Any],
        *,
        previous_payload: dict[str, Any] | None = None,
    ) -> None:
        if entity_type == "transcript_segment":
            await self._apply_segment_tx(conn, payload, previous_payload=previous_payload)
        elif entity_type == "meeting_summary":
            await self._apply_summary_tx(conn, payload)
        elif entity_type == "memory":
            await self._apply_memory_tx(conn, payload)
        else:
            raise ValueError("unsupported sync entity")

    async def _ensure_meeting_tx(
        self,
        conn: aiosqlite.Connection,
        meeting_id: str,
        *,
        started_at: str,
        title: str | None = None,
        state: str = "in_meeting",
    ) -> None:
        safe_state = state if state in _VALID_MEETING_STATES else "in_meeting"
        await conn.execute(
            "INSERT INTO meetings (id, title, state, started_at, tenant_id, device_id, owner_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, owner_id, id) DO NOTHING",
            (
                meeting_id,
                title,
                safe_state,
                started_at,
                self.tenant_id,
                "hub-remote",
                self.owner_id,
            ),
        )

    async def _apply_segment_tx(
        self,
        conn: aiosqlite.Connection,
        payload: dict[str, Any],
        *,
        previous_payload: dict[str, Any] | None = None,
    ) -> None:
        meeting_id = _required_text(payload.get("meeting_id"))
        captured_at = _required_text(payload.get("captured_at"), limit=128)
        text = _required_text(payload.get("text"), limit=32_000)
        start_ms = _int(payload.get("start_ms"))
        end_ms = _int(payload.get("end_ms"))
        speaker_id = _text(payload.get("speaker_id"))
        speaker_label = _text(payload.get("speaker_label"))
        await self._ensure_meeting_tx(
            conn,
            meeting_id,
            started_at=captured_at,
            title=_text(payload.get("meeting_title"), limit=512),
            state=str(payload.get("meeting_state") or "in_meeting"),
        )

        if previous_payload is not None:
            try:
                previous_identity = (
                    _required_text(previous_payload.get("meeting_id")),
                    _required_text(previous_payload.get("text"), limit=32_000),
                    _int(previous_payload.get("start_ms")),
                    _int(previous_payload.get("end_ms")),
                    _text(previous_payload.get("speaker_id")),
                    _text(previous_payload.get("speaker_label")),
                    _required_text(previous_payload.get("captured_at"), limit=128),
                )
            except ValueError:
                previous_identity = None
            if previous_identity is not None:
                cursor = await conn.execute(
                    "SELECT id FROM meeting_segments WHERE meeting_id = ? AND text = ? "
                    "AND start_ms = ? AND end_ms = ? AND speaker_id IS ? "
                    "AND speaker_label IS ? AND captured_at = ? AND tenant_id = ? "
                    "AND owner_id = ? ORDER BY id LIMIT 1",
                    (*previous_identity, self.tenant_id, self.owner_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    await conn.execute(
                        "UPDATE meeting_segments SET meeting_id = ?, text = ?, start_ms = ?, "
                        "end_ms = ?, speaker_id = ?, speaker_label = ?, captured_at = ? "
                        "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                        (
                            meeting_id,
                            text,
                            start_ms,
                            end_ms,
                            speaker_id,
                            speaker_label,
                            captured_at,
                            int(existing["id"]),
                            self.tenant_id,
                            self.owner_id,
                        ),
                    )
                    return

        await conn.execute(
            "INSERT INTO meeting_segments (meeting_id, text, start_ms, end_ms, speaker_id, "
            "speaker_label, captured_at, tenant_id, device_id, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id,
                text,
                start_ms,
                end_ms,
                speaker_id,
                speaker_label,
                captured_at,
                self.tenant_id,
                "hub-remote",
                self.owner_id,
            ),
        )

    async def _apply_summary_tx(self, conn: aiosqlite.Connection, payload: dict[str, Any]) -> None:
        meeting_id = _required_text(payload.get("meeting_id"))
        started_at = _required_text(
            payload.get("started_at") or payload.get("updated_at"), limit=128
        )
        deleted = bool(payload.get("deleted")) or payload.get("minutes_json") is None
        minutes_status = _text(payload.get("minutes_status"), limit=64)
        if minutes_status not in _VALID_MINUTES_STATES:
            minutes_status = None
        state = "finalized" if payload.get("finalized_at") else "ended"
        await self._ensure_meeting_tx(
            conn,
            meeting_id,
            started_at=started_at,
            title=_text(payload.get("title"), limit=512),
            state=state,
        )
        await conn.execute(
            "UPDATE meetings SET state = ?, title = ?, display_title = ?, ended_at = ?, finalized_at = ?, "
            "minutes_json = ?, minutes_status = ?, minutes_error = ?, minutes_cleared_at = ? "
            "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
            (
                state,
                _text(payload.get("title"), limit=512),
                _text(payload.get("display_title"), limit=512),
                _text(payload.get("ended_at"), limit=128),
                _text(payload.get("finalized_at"), limit=128),
                None if deleted else _text(payload.get("minutes_json"), limit=1_000_000),
                None if deleted else minutes_status,
                None if deleted else _text(payload.get("minutes_error"), limit=4_000),
                _text(payload.get("minutes_cleared_at"), limit=128) if deleted else None,
                meeting_id,
                self.tenant_id,
                self.owner_id,
            ),
        )

    async def _apply_memory_tx(self, conn: aiosqlite.Connection, payload: dict[str, Any]) -> None:
        memory_id = _required_text(payload.get("memory_id"))
        status = str(payload.get("status") or "active")
        if status not in _VALID_MEMORY_STATES:
            status = "active"
        metadata = payload.get("metadata")
        metadata_json = _dump(metadata) if isinstance(metadata, dict) else "{}"
        await conn.execute(
            "INSERT INTO memory_nodes (tenant_id, owner_id, memory_id, kind, content, "
            "normalized_content, canonical_key, subject, confidence, salience, scope, status, "
            "hit_count, source_count, user_confirmed, created_at, last_seen_at, updated_at, "
            "confirmed_at, superseded_at, superseded_by, deleted_at, revision, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, owner_id, memory_id) DO UPDATE SET kind = excluded.kind, "
            "content = excluded.content, normalized_content = excluded.normalized_content, "
            "canonical_key = excluded.canonical_key, subject = excluded.subject, "
            "confidence = excluded.confidence, salience = excluded.salience, scope = excluded.scope, "
            "status = excluded.status, hit_count = excluded.hit_count, source_count = excluded.source_count, "
            "user_confirmed = excluded.user_confirmed, created_at = excluded.created_at, "
            "last_seen_at = excluded.last_seen_at, updated_at = excluded.updated_at, "
            "confirmed_at = excluded.confirmed_at, superseded_at = excluded.superseded_at, "
            "superseded_by = excluded.superseded_by, deleted_at = excluded.deleted_at, "
            "revision = excluded.revision, metadata_json = excluded.metadata_json",
            (
                self.tenant_id,
                self.owner_id,
                memory_id,
                _required_text(payload.get("kind"), limit=64),
                _required_text(payload.get("content"), limit=8_000),
                _required_text(
                    payload.get("normalized_content") or payload.get("content"), limit=8_000
                ),
                _required_text(payload.get("canonical_key"), limit=512),
                _text(payload.get("subject"), limit=512),
                float(payload.get("confidence") or 0.0),
                float(payload.get("salience") or 0.0),
                _required_text(payload.get("scope") or "owner", limit=128),
                status,
                max(1, _int(payload.get("hit_count"), 1)),
                max(1, _int(payload.get("source_count"), 1)),
                1 if payload.get("user_confirmed") else 0,
                _required_text(payload.get("created_at"), limit=128),
                _required_text(payload.get("last_seen_at"), limit=128),
                _required_text(payload.get("updated_at"), limit=128),
                _text(payload.get("confirmed_at"), limit=128),
                _text(payload.get("superseded_at"), limit=128),
                _text(payload.get("superseded_by"), limit=512),
                _text(payload.get("deleted_at"), limit=128),
                max(0, _int(payload.get("revision"))),
                metadata_json,
            ),
        )

    async def pending_count(self) -> int:
        async with self._lock:
            conn = self._require_conn()
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM hub_sync_outbox WHERE state IN ('pending', 'failed') "
                "AND attempts < 10"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def list_outbox(self) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._require_conn()
            cursor = await conn.execute(
                "SELECT operation_id, entity_type, entity_id, base_revision, payload_json, state "
                "FROM hub_sync_outbox ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            {
                "operation_id": str(row["operation_id"]),
                "entity_type": str(row["entity_type"]),
                "entity_id": str(row["entity_id"]),
                "base_revision": int(row["base_revision"]),
                "payload": _load(row["payload_json"]),
                "state": str(row["state"]),
            }
            for row in rows
        ]
