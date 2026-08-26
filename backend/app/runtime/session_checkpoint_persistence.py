"""Durable session/checkpoint persistence for the embedded agent runtime.

This module is deliberately a persistence core, not a session API adapter.  It
owns the versioned JSON contract, integrity checks, and the SQLite transaction
boundary used by the runtime host.  The caller still owns the current grant,
model configuration, and kernel build facts; resume is accepted only when the
caller supplies values matching the persisted identity.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, TypeAlias

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)

PERSISTENCE_SCHEMA_VERSION: Final = 1
SUPPORTED_EVENT_TYPES: Final = frozenset(
    {
        "agent.brief",
        "agent.summary.updated",
        "agent.compaction.started",
        "agent.compaction.completed",
        "agent.compaction.failed",
    }
)
RESUMABLE_SESSION_STATES: Final = frozenset({"open", "paused"})
FORBIDDEN_KEYS: Final = frozenset(
    {
        "apiKey",
        "api_key",
        "credential",
        "globalConfig",
        "global_config",
        "HOME",
        "PATH",
        "pid",
        "processId",
        "rawCredential",
        "raw_credential",
        "sessionFile",
        "sessionPath",
        "temporaryPort",
        "tempPort",
    }
)

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class PersistenceError(RuntimeError):
    """Stable, non-secret persistence failure."""

    def __init__(self, code: str, message: str = "persistence operation rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResumeIdentity:
    """The immutable identity that a checkpoint is allowed to resume under."""

    session_id: str
    task_id: str
    operation_key: str
    model_config_revision: int
    grant_snapshot: Mapping[str, Any]
    kernel_build_identity: Mapping[str, Any]

    def as_json(self) -> JsonObject:
        return {
            "schemaVersion": PERSISTENCE_SCHEMA_VERSION,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "operationKey": self.operation_key,
            "modelConfigRevision": self.model_config_revision,
            "grantSnapshot": _json_object(self.grant_snapshot, "grant snapshot"),
            "kernelBuildIdentity": _json_object(
                self.kernel_build_identity, "kernel build identity"
            ),
        }


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    session_id: str
    event_seq: int
    event_type: str
    payload: JsonObject
    occurred_at: str
    durable_event_seq: int
    checksum: str


def _json_object(value: Mapping[str, Any], label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise PersistenceError("PERSISTENCE_INVALID_INPUT", f"{label} must be an object")
    try:
        encoded = json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise PersistenceError("PERSISTENCE_INVALID_INPUT", f"{label} is not JSON") from exc
    if not isinstance(encoded, dict):
        raise PersistenceError("PERSISTENCE_INVALID_INPUT", f"{label} must be an object")
    _reject_forbidden(encoded)
    return encoded


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PersistenceError("PERSISTENCE_INVALID_INPUT", "value is not canonical JSON") from exc


def _checksum(value: Any) -> str:
    return f"sha256:{sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _reject_forbidden(value: Any, seen: set[int] | None = None) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    visited = seen if seen is not None else set()
    marker = id(value)
    if marker in visited:
        return
    visited.add(marker)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in FORBIDDEN_KEYS:
                raise PersistenceError(
                    "CHECKPOINT_CORRUPT",
                    f"persisted payload contains forbidden field: {key}",
                )
            _reject_forbidden(child, visited)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden(child, visited)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} is invalid")
    return value


def _required_revision(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} is invalid")
    return value


def _required_non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} is invalid")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"checkpoint {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _lookup(mapping: Mapping[str, Any], camel: str, snake: str) -> Any:
    if camel in mapping:
        return mapping[camel]
    if snake in mapping:
        return mapping[snake]
    return None


def _identity_from_mapping(value: ResumeIdentity | Mapping[str, Any]) -> ResumeIdentity:
    if isinstance(value, ResumeIdentity):
        identity = value
    else:
        identity = ResumeIdentity(
            session_id=_required_string(_lookup(value, "sessionId", "session_id"), "session_id"),
            task_id=_required_string(_lookup(value, "taskId", "task_id"), "task_id"),
            operation_key=_required_string(
                _lookup(value, "operationKey", "operation_key"), "operation_key"
            ),
            model_config_revision=_required_revision(
                _lookup(value, "modelConfigRevision", "model_config_revision"),
                "model_config_revision",
            ),
            grant_snapshot=_json_object(
                _lookup(value, "grantSnapshot", "grant_snapshot"), "grant snapshot"
            ),
            kernel_build_identity=_json_object(
                _lookup(value, "kernelBuildIdentity", "kernel_build_identity"),
                "kernel build identity",
            ),
        )
    _required_string(identity.session_id, "session_id")
    _required_string(identity.task_id, "task_id")
    _required_string(identity.operation_key, "operation_key")
    _required_revision(identity.model_config_revision, "model_config_revision")
    grant = _json_object(identity.grant_snapshot, "grant snapshot")
    build = _json_object(identity.kernel_build_identity, "kernel build identity")
    grant_task = _lookup(grant, "taskId", "task_id")
    grant_operation = _lookup(grant, "operationKey", "operation_key")
    if grant_task != identity.task_id or grant_operation != identity.operation_key:
        raise PersistenceError(
            "CHECKPOINT_TASK_MISMATCH", "grant snapshot is not bound to the task"
        )
    grant_revision = _lookup(grant, "revision", "grant_revision")
    _required_revision(grant_revision, "grant revision")
    if not build:
        raise PersistenceError("RUNTIME_BUILD_MISMATCH", "kernel build identity is empty")
    return ResumeIdentity(
        session_id=identity.session_id,
        task_id=identity.task_id,
        operation_key=identity.operation_key,
        model_config_revision=identity.model_config_revision,
        grant_snapshot=grant,
        kernel_build_identity=build,
    )


def _checkpoint_grant_snapshot(
    payload: Mapping[str, Any],
    *,
    task_id: str,
    operation_key: str,
    grant_revision: int,
) -> JsonObject:
    raw = payload.get("grantSnapshot")
    if not isinstance(raw, Mapping):
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint grantSnapshot must be an object")
    grant = _json_object(raw, "checkpoint grantSnapshot")
    if grant.get("schemaVersion") != PERSISTENCE_SCHEMA_VERSION:
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint grantSnapshot schema is invalid")
    grant_id = _required_string(_lookup(grant, "grantId", "grant_id"), "grantSnapshot.grantId")
    grant_task = _required_string(_lookup(grant, "taskId", "task_id"), "grantSnapshot.taskId")
    grant_operation = _required_string(
        _lookup(grant, "operationKey", "operation_key"), "grantSnapshot.operationKey"
    )
    persisted_revision = _required_revision(
        _lookup(grant, "revision", "grant_revision"), "grantSnapshot.revision"
    )
    if (
        grant_task != task_id
        or grant_operation != operation_key
        or persisted_revision != grant_revision
        or not grant_id
    ):
        raise PersistenceError(
            "CHECKPOINT_IDENTITY_MISMATCH", "checkpoint grantSnapshot identity does not match"
        )
    return grant


def _stored_json_object(raw: str, label: str) -> JsonObject:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PersistenceError("CHECKPOINT_CORRUPT", f"stored {label} is not JSON") from exc
    if not isinstance(value, Mapping):
        raise PersistenceError("CHECKPOINT_CORRUPT", f"stored {label} is not an object")
    return _json_object(value, label)


def _validate_checkpoint_identity(
    payload: Mapping[str, Any],
    *,
    stored: ResumeIdentity,
    expected: ResumeIdentity,
) -> None:
    payload_task_id = _required_string(_lookup(payload, "taskId", "task_id"), "task_id")
    payload_operation_key = _required_string(
        _lookup(payload, "operationKey", "operation_key"), "operation_key"
    )
    payload_model_revision = _required_revision(
        _lookup(payload, "modelConfigRevision", "model_config_revision"), "model revision"
    )
    payload_grant_revision = _required_revision(
        _lookup(payload, "grantRevision", "grant_revision"), "grant revision"
    )
    payload_grant_snapshot = _checkpoint_grant_snapshot(
        payload,
        task_id=payload_task_id,
        operation_key=payload_operation_key,
        grant_revision=payload_grant_revision,
    )
    stored_grant_snapshot = _json_object(stored.grant_snapshot, "session grant snapshot")
    expected_grant_snapshot = _json_object(expected.grant_snapshot, "current grant snapshot")
    if payload_task_id != stored.task_id or payload_task_id != expected.task_id:
        raise PersistenceError(
            "CHECKPOINT_TASK_MISMATCH", "checkpoint task identity does not match"
        )
    if (
        payload_operation_key != stored.operation_key
        or payload_operation_key != expected.operation_key
    ):
        raise PersistenceError(
            "CHECKPOINT_OPERATION_MISMATCH",
            "checkpoint operation identity does not match",
        )
    if (
        payload_model_revision != stored.model_config_revision
        or payload_model_revision != expected.model_config_revision
    ):
        raise PersistenceError(
            "CHECKPOINT_MODEL_REVISION_MISSING",
            "checkpoint model revision does not match",
        )
    expected_grant_revision = _required_revision(
        _lookup(expected_grant_snapshot, "revision", "grant_revision"),
        "current grant revision",
    )
    stored_grant_revision = _required_revision(
        _lookup(stored_grant_snapshot, "revision", "grant_revision"),
        "stored grant revision",
    )
    if (
        payload_grant_revision != stored_grant_revision
        or payload_grant_revision != expected_grant_revision
    ):
        raise PersistenceError(
            "GRANT_REVISION_MISMATCH", "checkpoint grant revision does not match"
        )
    if (
        payload_grant_snapshot != stored_grant_snapshot
        or payload_grant_snapshot != expected_grant_snapshot
    ):
        raise PersistenceError(
            "CHECKPOINT_IDENTITY_MISMATCH",
            "checkpoint grantSnapshot does not match the current identity",
        )


def serialize_checkpoint(checkpoint: Mapping[str, Any]) -> tuple[JsonObject, str]:
    """Validate and canonicalize a v1 kernel checkpoint without changing its semantics."""

    payload = _json_object(checkpoint, "checkpoint")
    if payload.get("schemaVersion", payload.get("schema_version")) != PERSISTENCE_SCHEMA_VERSION:
        raise PersistenceError(
            "PERSISTENCE_SCHEMA_UNSUPPORTED", "checkpoint schema version is unsupported"
        )
    for camel, snake in (
        ("checkpointId", "checkpoint_id"),
        ("taskId", "task_id"),
        ("operationKey", "operation_key"),
        ("createdAt", "created_at"),
    ):
        _required_string(_lookup(payload, camel, snake), camel)
    _required_revision(
        _lookup(payload, "modelConfigRevision", "model_config_revision"), "model revision"
    )
    grant_revision = _required_revision(
        _lookup(payload, "grantRevision", "grant_revision"), "grant revision"
    )
    task_id = _required_string(_lookup(payload, "taskId", "task_id"), "task_id")
    operation_key = _required_string(
        _lookup(payload, "operationKey", "operation_key"), "operation_key"
    )
    _checkpoint_grant_snapshot(
        payload,
        task_id=task_id,
        operation_key=operation_key,
        grant_revision=grant_revision,
    )
    _required_non_negative_int(
        _lookup(payload, "lastDurableEventSeq", "last_durable_event_seq"),
        "last durable event sequence",
    )
    _parse_timestamp(_lookup(payload, "createdAt", "created_at"), "created_at")
    if not isinstance(_lookup(payload, "messages", "messages"), list):
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint messages are invalid")
    if not isinstance(_lookup(payload, "compactState", "compact_state"), Mapping):
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint compact state is invalid")
    if not isinstance(_lookup(payload, "budgetState", "budget_state"), Mapping):
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint budget state is invalid")
    supplied = payload.get("checksum")
    if supplied is not None and not isinstance(supplied, str):
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint checksum is invalid")
    body = dict(payload)
    body.pop("checksum", None)
    expected = _checksum(body)
    if supplied is not None and supplied not in {expected, expected.removeprefix("sha256:")}:
        raise PersistenceError("CHECKPOINT_CORRUPT", "checkpoint checksum mismatch")
    payload["checksum"] = supplied or expected
    return payload, _checksum(payload)


def deserialize_checkpoint(raw: str, expected_checksum: str) -> JsonObject:
    """Decode one stored checkpoint and reject malformed or tampered JSON."""

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PersistenceError("CHECKPOINT_CORRUPT", "stored checkpoint is not JSON") from exc
    if not isinstance(value, Mapping):
        raise PersistenceError("CHECKPOINT_CORRUPT", "stored checkpoint is not an object")
    payload, checksum = serialize_checkpoint(value)
    if checksum != expected_checksum:
        raise PersistenceError("CHECKPOINT_CORRUPT", "stored checkpoint checksum mismatch")
    return payload


def _event_envelope(
    *,
    event_seq: int,
    event_type: str,
    payload: JsonObject,
    occurred_at: str,
    durable_event_seq: int,
) -> JsonObject:
    return {
        "schemaVersion": PERSISTENCE_SCHEMA_VERSION,
        "eventSeq": event_seq,
        "eventType": event_type,
        "payload": payload,
        "occurredAt": occurred_at,
        "durableEventSeq": durable_event_seq,
    }


class SessionCheckpointRepository:
    """SQLite repository with transaction-scoped atomic checkpoint publication."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    @staticmethod
    async def _begin(conn: aiosqlite.Connection) -> None:
        await conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    async def _rollback(conn: aiosqlite.Connection) -> None:
        with suppress(aiosqlite.Error):
            await conn.rollback()

    async def create_session(
        self,
        identity: ResumeIdentity | Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> None:
        bound = _identity_from_mapping(identity)
        timestamp = created_at or datetime.now(UTC).isoformat()
        _parse_timestamp(timestamp, "created_at")
        grant = bound.as_json()["grantSnapshot"]
        assert isinstance(grant, dict)
        grant_id = _lookup(grant, "grantId", "grant_id")
        grant_revision = _required_revision(
            _lookup(grant, "revision", "grant_revision"), "grant revision"
        )
        build = bound.as_json()["kernelBuildIdentity"]
        assert isinstance(build, dict)
        build_id = _lookup(build, "buildId", "build_id")
        if not isinstance(build_id, str) or not build_id:
            raise PersistenceError("RUNTIME_BUILD_MISMATCH", "kernel build id is missing")
        async with self._conn() as conn:
            await self._begin(conn)
            try:
                await conn.execute(
                    """INSERT INTO agent_runtime_sessions (
                           session_id, task_id, operation_key, model_config_revision,
                           grant_id, grant_revision, grant_snapshot_json,
                           kernel_build_id, kernel_build_identity_json, state,
                           latest_checkpoint_id, last_durable_event_seq,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, 0, ?, ?)
                       ON CONFLICT(session_id) DO NOTHING""",
                    (
                        bound.session_id,
                        bound.task_id,
                        bound.operation_key,
                        bound.model_config_revision,
                        str(grant_id or ""),
                        grant_revision,
                        _canonical_json(grant),
                        build_id,
                        _canonical_json(build),
                        timestamp,
                        timestamp,
                    ),
                )
                cursor = await conn.execute(
                    "SELECT task_id, operation_key, model_config_revision, grant_revision, "
                    "grant_snapshot_json, kernel_build_identity_json FROM agent_runtime_sessions "
                    "WHERE session_id = ?",
                    (bound.session_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise PersistenceError("PERSISTENCE_WRITE_FAILED", "session was not created")
                stored = ResumeIdentity(
                    session_id=bound.session_id,
                    task_id=row["task_id"],
                    operation_key=row["operation_key"],
                    model_config_revision=row["model_config_revision"],
                    grant_snapshot=json.loads(row["grant_snapshot_json"]),
                    kernel_build_identity=json.loads(row["kernel_build_identity_json"]),
                )
                if stored.as_json() != bound.as_json():
                    raise PersistenceError(
                        "CHECKPOINT_IDENTITY_MISMATCH", "session identity is already bound"
                    )
                await conn.commit()
            except Exception:
                await self._rollback(conn)
                raise

    async def append_event(
        self,
        session_id: str,
        *,
        event_seq: int,
        event_type: str,
        payload: Mapping[str, Any],
        durable_event_seq: int | None = None,
        occurred_at: str | None = None,
    ) -> PersistedEvent:
        _required_string(session_id, "session_id")
        _required_non_negative_int(event_seq, "event sequence")
        if event_seq < 1:
            raise PersistenceError("PERSISTENCE_INVALID_INPUT", "event sequence must be positive")
        if event_type not in SUPPORTED_EVENT_TYPES:
            raise PersistenceError(
                "PERSISTENCE_EVENT_UNSUPPORTED", "event type is not durable in B11"
            )
        event_payload = _json_object(payload, "event payload")
        timestamp = occurred_at or datetime.now(UTC).isoformat()
        _parse_timestamp(timestamp, "occurred_at")
        durable_seq = durable_event_seq if durable_event_seq is not None else event_seq
        _required_non_negative_int(durable_seq, "durable event sequence")
        envelope = _event_envelope(
            event_seq=event_seq,
            event_type=event_type,
            payload=event_payload,
            occurred_at=timestamp,
            durable_event_seq=durable_seq,
        )
        event_checksum = _checksum(envelope)
        async with self._conn() as conn:
            await self._begin(conn)
            try:
                session = await self._session_row(conn, session_id)
                if session is None:
                    raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
                existing_cursor = await conn.execute(
                    "SELECT event_type, payload_json, occurred_at, durable_event_seq, checksum "
                    "FROM agent_runtime_events WHERE session_id = ? AND event_seq = ?",
                    (session_id, event_seq),
                )
                existing = await existing_cursor.fetchone()
                await existing_cursor.close()
                if existing is not None:
                    if existing["checksum"] != event_checksum:
                        raise PersistenceError(
                            "PERSISTENCE_CONFLICT", "event sequence is already bound"
                        )
                    result = PersistedEvent(
                        session_id=session_id,
                        event_seq=event_seq,
                        event_type=existing["event_type"],
                        payload=json.loads(existing["payload_json"]),
                        occurred_at=existing["occurred_at"],
                        durable_event_seq=existing["durable_event_seq"],
                        checksum=existing["checksum"],
                    )
                    await conn.commit()
                    return result
                await conn.execute(
                    """INSERT INTO agent_runtime_events (
                           session_id, event_seq, event_type, payload_json,
                           occurred_at, durable_event_seq, checksum
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        event_seq,
                        event_type,
                        _canonical_json(event_payload),
                        timestamp,
                        durable_seq,
                        event_checksum,
                    ),
                )
                await conn.execute(
                    """UPDATE agent_runtime_sessions
                       SET last_durable_event_seq = MAX(last_durable_event_seq, ?),
                           updated_at = ?
                       WHERE session_id = ?""",
                    (durable_seq, timestamp, session_id),
                )
                await conn.commit()
            except Exception:
                await self._rollback(conn)
                raise
        return PersistedEvent(
            session_id=session_id,
            event_seq=event_seq,
            event_type=event_type,
            payload=event_payload,
            occurred_at=timestamp,
            durable_event_seq=durable_seq,
            checksum=event_checksum,
        )

    async def save_checkpoint(
        self,
        session_id: str,
        checkpoint: Mapping[str, Any],
        *,
        saved_at: str | None = None,
    ) -> str:
        _required_string(session_id, "session_id")
        payload, checksum = serialize_checkpoint(checkpoint)
        timestamp = saved_at or datetime.now(UTC).isoformat()
        _parse_timestamp(timestamp, "saved_at")
        task_id = _required_string(_lookup(payload, "taskId", "task_id"), "task_id")
        operation_key = _required_string(
            _lookup(payload, "operationKey", "operation_key"), "operation_key"
        )
        model_revision = _required_revision(
            _lookup(payload, "modelConfigRevision", "model_config_revision"), "model revision"
        )
        grant_revision = _required_revision(
            _lookup(payload, "grantRevision", "grant_revision"), "grant revision"
        )
        event_seq = _required_non_negative_int(
            _lookup(payload, "lastDurableEventSeq", "last_durable_event_seq"),
            "last durable event sequence",
        )
        checkpoint_id = _required_string(
            _lookup(payload, "checkpointId", "checkpoint_id"), "checkpoint_id"
        )
        grant_snapshot = _checkpoint_grant_snapshot(
            payload,
            task_id=task_id,
            operation_key=operation_key,
            grant_revision=grant_revision,
        )
        async with self._conn() as conn:
            await self._begin(conn)
            try:
                session = await self._session_row(conn, session_id)
                if session is None:
                    raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
                if session["task_id"] != task_id or session["operation_key"] != operation_key:
                    raise PersistenceError(
                        "CHECKPOINT_TASK_MISMATCH", "checkpoint task identity does not match"
                    )
                if session["model_config_revision"] != model_revision:
                    raise PersistenceError(
                        "CHECKPOINT_MODEL_REVISION_MISSING",
                        "checkpoint model revision does not match",
                    )
                if session["grant_revision"] != grant_revision:
                    raise PersistenceError(
                        "GRANT_REVISION_MISMATCH", "checkpoint grant revision does not match"
                    )
                stored_grant_snapshot = _stored_json_object(
                    session["grant_snapshot_json"], "session grant snapshot"
                )
                if stored_grant_snapshot != grant_snapshot:
                    raise PersistenceError(
                        "CHECKPOINT_IDENTITY_MISMATCH",
                        "checkpoint grantSnapshot does not match the session identity",
                    )
                if event_seq > session["last_durable_event_seq"]:
                    raise PersistenceError(
                        "CHECKPOINT_EVENT_SEQ_AHEAD",
                        "checkpoint durable sequence is ahead of the session",
                    )
                existing_cursor = await conn.execute(
                    "SELECT checksum FROM agent_runtime_checkpoints "
                    "WHERE session_id = ? AND checkpoint_id = ?",
                    (session_id, checkpoint_id),
                )
                existing = await existing_cursor.fetchone()
                await existing_cursor.close()
                if existing is not None and existing["checksum"] != checksum:
                    raise PersistenceError("PERSISTENCE_CONFLICT", "checkpoint id is already bound")
                await conn.execute(
                    """INSERT INTO agent_runtime_checkpoints (
                           session_id, checkpoint_id, schema_version, task_id,
                           operation_key, model_config_revision, grant_revision,
                           last_durable_event_seq, payload_json, checksum,
                           created_at, saved_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_id, checkpoint_id) DO UPDATE SET
                           saved_at = excluded.saved_at
                       WHERE agent_runtime_checkpoints.checksum = excluded.checksum""",
                    (
                        session_id,
                        checkpoint_id,
                        PERSISTENCE_SCHEMA_VERSION,
                        task_id,
                        operation_key,
                        model_revision,
                        grant_revision,
                        event_seq,
                        _canonical_json(payload),
                        checksum,
                        _lookup(payload, "createdAt", "created_at"),
                        timestamp,
                    ),
                )
                await conn.execute(
                    """UPDATE agent_runtime_sessions
                       SET latest_checkpoint_id = ?, updated_at = ?
                       WHERE session_id = ?""",
                    (checkpoint_id, timestamp, session_id),
                )
                await conn.commit()
            except Exception:
                await self._rollback(conn)
                raise
        return checkpoint_id

    async def resume(
        self,
        session_id: str,
        current_identity: ResumeIdentity | Mapping[str, Any],
        *,
        current_durable_event_seq: int,
        now: str | None = None,
        max_age_seconds: int | None = None,
    ) -> JsonObject:
        expected = _identity_from_mapping(current_identity)
        if expected.session_id != session_id:
            raise PersistenceError(
                "CHECKPOINT_TASK_MISMATCH", "resume session identity does not match"
            )
        _required_non_negative_int(current_durable_event_seq, "current durable event sequence")
        raw_now = now or datetime.now(UTC).isoformat()
        now_dt = _parse_timestamp(raw_now, "now")
        if max_age_seconds is not None and (
            not isinstance(max_age_seconds, int)
            or isinstance(max_age_seconds, bool)
            or max_age_seconds < 1
        ):
            raise PersistenceError("PERSISTENCE_INVALID_INPUT", "max checkpoint age is invalid")
        async with self._conn() as conn:
            session = await self._session_row(conn, session_id)
            if session is None:
                raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
            if session["state"] not in RESUMABLE_SESSION_STATES:
                raise PersistenceError("SESSION_NOT_RESUMABLE", "session is not resumable")
            stored = ResumeIdentity(
                session_id=session_id,
                task_id=session["task_id"],
                operation_key=session["operation_key"],
                model_config_revision=session["model_config_revision"],
                grant_snapshot=_stored_json_object(
                    session["grant_snapshot_json"], "session grant snapshot"
                ),
                kernel_build_identity=_stored_json_object(
                    session["kernel_build_identity_json"], "kernel build identity"
                ),
            )
            if stored.as_json() != expected.as_json():
                raise PersistenceError(
                    "CHECKPOINT_IDENTITY_MISMATCH", "resume identity does not match"
                )
            if session["latest_checkpoint_id"] is None:
                raise PersistenceError("CHECKPOINT_NOT_FOUND", "session has no checkpoint")
            cursor = await conn.execute(
                "SELECT payload_json, checksum, last_durable_event_seq, created_at "
                "FROM agent_runtime_checkpoints WHERE session_id = ? AND checkpoint_id = ?",
                (session_id, session["latest_checkpoint_id"]),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise PersistenceError("CHECKPOINT_NOT_FOUND", "latest checkpoint is missing")
        payload = deserialize_checkpoint(row["payload_json"], row["checksum"])
        _validate_checkpoint_identity(payload, stored=stored, expected=expected)
        event_seq = _required_non_negative_int(
            _lookup(payload, "lastDurableEventSeq", "last_durable_event_seq"),
            "last durable event sequence",
        )
        if event_seq > current_durable_event_seq:
            raise PersistenceError(
                "CHECKPOINT_EVENT_SEQ_AHEAD", "checkpoint durable sequence is ahead"
            )
        created = _parse_timestamp(_lookup(payload, "createdAt", "created_at"), "created_at")
        if max_age_seconds is not None and (now_dt - created).total_seconds() > max_age_seconds:
            raise PersistenceError("CHECKPOINT_STALE", "checkpoint is stale")
        expires_at = _lookup(expected.grant_snapshot, "expiresAt", "expires_at")
        if expires_at is not None and _parse_timestamp(expires_at, "grant expiry") <= now_dt:
            raise PersistenceError("GRANT_EXPIRED", "grant snapshot is expired")
        return payload

    async def set_session_state(
        self, session_id: str, state: str, *, updated_at: str | None = None
    ) -> None:
        _required_string(session_id, "session_id")
        if state not in {"open", "paused", "closed", "stale"}:
            raise PersistenceError("PERSISTENCE_INVALID_INPUT", "session state is invalid")
        timestamp = updated_at or datetime.now(UTC).isoformat()
        _parse_timestamp(timestamp, "updated_at")
        async with self._conn() as conn:
            await self._begin(conn)
            try:
                cursor = await conn.execute(
                    "UPDATE agent_runtime_sessions SET state = ?, updated_at = ? WHERE session_id = ?",
                    (state, timestamp, session_id),
                )
                if cursor.rowcount != 1:
                    raise PersistenceError("SESSION_NOT_FOUND", "session does not exist")
                await conn.commit()
            except Exception:
                await self._rollback(conn)
                raise

    async def _session_row(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
    ) -> aiosqlite.Row | None:
        cursor = await conn.execute(
            "SELECT * FROM agent_runtime_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


__all__ = [
    "PERSISTENCE_SCHEMA_VERSION",
    "SUPPORTED_EVENT_TYPES",
    "PersistedEvent",
    "PersistenceError",
    "ResumeIdentity",
    "SessionCheckpointRepository",
    "deserialize_checkpoint",
    "serialize_checkpoint",
]
