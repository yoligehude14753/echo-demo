"""独立的生产 SQLite telemetry adapter。

该模块只接受 ``TelemetryObservation``，先通过注入的伪名化器 materialize，
再把 ``TelemetryEvent`` 的 allowlisted 字段写入独立数据库。数据库不属于
EchoDesk 主业务数据库，也不保存 raw identity、音频、transcript 或错误正文。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.telemetry.contracts import (
    DEFAULT_K_THRESHOLD,
    DeletionReceipt,
    FailureReason,
    FailureReasonCount,
    PseudonymousIdentity,
    TelemetryAggregate,
    TelemetryDeleteRequest,
    TelemetryEvent,
    TelemetryObservation,
    TelemetryOperation,
    TelemetryPlatform,
    TelemetryProvider,
    TelemetryQuery,
    utc_now,
)
from app.telemetry.ports import TelemetryPort
from app.telemetry.pseudonym import HmacPseudonymizer

TELEMETRY_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5_000
_EVENT_COLUMNS = (
    "event_id, occurred_at, epoch, key_version, tenant_pseudonym, user_pseudonym, "
    "device_pseudonym, operation, platform, app_version, provider, success, "
    "failure_reason, end_to_end_latency_ms, queue_wait_ms, audio_duration_ms"
)
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS telemetry_schema_version (
        version INTEGER PRIMARY KEY CHECK (version > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telemetry_events (
        event_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        key_version TEXT NOT NULL,
        tenant_pseudonym TEXT NOT NULL,
        user_pseudonym TEXT NOT NULL,
        device_pseudonym TEXT NOT NULL,
        operation TEXT NOT NULL,
        platform TEXT NOT NULL,
        app_version TEXT NOT NULL,
        provider TEXT NOT NULL,
        success INTEGER NOT NULL CHECK (success IN (0, 1)),
        failure_reason TEXT,
        end_to_end_latency_ms INTEGER NOT NULL,
        queue_wait_ms INTEGER NOT NULL,
        audio_duration_ms INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_telemetry_events_occurred_at ON telemetry_events (occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_events_cohort "
    "ON telemetry_events (epoch, key_version, tenant_pseudonym, operation, platform, "
    "app_version, provider)",
    "CREATE TABLE IF NOT EXISTS telemetry_deletion_audit ("
    "audit_id TEXT PRIMARY KEY, deleted_event_count INTEGER NOT NULL CHECK "
    "(deleted_event_count >= 0), deleted_at TEXT NOT NULL, reason TEXT NOT NULL)",
)

_AggregateKey = tuple[
    int,
    str,
    str,
    TelemetryOperation,
    TelemetryPlatform,
    str,
    TelemetryProvider,
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _event_key(event: TelemetryEvent) -> _AggregateKey:
    identity = event.identity
    return (
        identity.epoch,
        identity.key_version,
        identity.tenant_pseudonym,
        event.operation,
        event.platform,
        event.app_version,
        event.provider,
    )


def _aggregate(key: _AggregateKey, events: tuple[TelemetryEvent, ...]) -> TelemetryAggregate:
    request_count = len(events)
    success_count = sum(event.success for event in events)
    audio_events = tuple(event for event in events if event.audio_duration_ms is not None)
    failure_counts: dict[FailureReason, int] = defaultdict(int)
    for event in events:
        if not event.success:
            failure_counts[event.failure_reason or FailureReason.UNKNOWN] += 1
    return TelemetryAggregate(
        epoch=key[0],
        key_version=key[1],
        tenant_pseudonym=key[2],
        operation=key[3],
        platform=key[4],
        app_version=key[5],
        provider=key[6],
        distinct_user_count=len({event.identity.user_pseudonym for event in events}),
        failure_reason_counts=tuple(
            FailureReasonCount(reason=reason, event_count=count)
            for reason, count in sorted(failure_counts.items(), key=lambda item: item[0].value)
        ),
        request_count=request_count,
        success_count=success_count,
        failure_count=request_count - success_count,
        success_rate=success_count / request_count,
        latency_sum_ms=sum(event.end_to_end_latency_ms for event in events),
        queue_wait_sum_ms=sum(event.queue_wait_ms for event in events),
        audio_duration_sum_ms=sum(event.audio_duration_ms or 0 for event in audio_events),
        audio_duration_event_count=len(audio_events),
    )


def _event_from_row(row: sqlite3.Row) -> TelemetryEvent:
    identity = PseudonymousIdentity(
        tenant_pseudonym=row["tenant_pseudonym"],
        user_pseudonym=row["user_pseudonym"],
        device_pseudonym=row["device_pseudonym"],
        key_version=row["key_version"],
        epoch=row["epoch"],
    )
    return TelemetryEvent(
        event_id=row["event_id"],
        occurred_at=_parse_timestamp(row["occurred_at"]),
        identity=identity,
        operation=row["operation"],
        platform=row["platform"],
        app_version=row["app_version"],
        provider=row["provider"],
        success=bool(row["success"]),
        failure_reason=row["failure_reason"],
        end_to_end_latency_ms=row["end_to_end_latency_ms"],
        queue_wait_ms=row["queue_wait_ms"],
        audio_duration_ms=row["audio_duration_ms"],
    )


def _event_values(event: TelemetryEvent) -> tuple[object, ...]:
    identity = event.identity
    return (
        event.event_id,
        _timestamp(event.occurred_at),
        identity.epoch,
        identity.key_version,
        identity.tenant_pseudonym,
        identity.user_pseudonym,
        identity.device_pseudonym,
        event.operation.value,
        event.platform.value,
        event.app_version,
        event.provider.value,
        int(event.success),
        event.failure_reason.value if event.failure_reason is not None else None,
        event.end_to_end_latency_ms,
        event.queue_wait_ms,
        event.audio_duration_ms,
    )


def _query_sql(query: TelemetryQuery) -> tuple[str, tuple[object, ...]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if query.start_at is not None:
        clauses.append("occurred_at >= ?")
        params.append(_timestamp(query.start_at))
    if query.end_at is not None:
        clauses.append("occurred_at < ?")
        params.append(_timestamp(query.end_at))
    for column, value in (
        ("epoch", query.epoch),
        ("key_version", query.key_version),
        ("tenant_pseudonym", query.tenant_pseudonym),
        ("operation", query.operation.value if query.operation else None),
        ("platform", query.platform.value if query.platform else None),
        ("app_version", query.app_version),
        ("provider", query.provider.value if query.provider else None),
        ("failure_reason", query.failure_reason.value if query.failure_reason else None),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    return (
        f"SELECT {_EVENT_COLUMNS} FROM telemetry_events WHERE {' AND '.join(clauses)} "
        "ORDER BY epoch, key_version, tenant_pseudonym, operation, platform, app_version, provider, event_id",
        tuple(params),
    )


def _matches_delete_sql(request: TelemetryDeleteRequest) -> tuple[str, tuple[object, ...]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    for column, value in (
        ("tenant_pseudonym", request.tenant_pseudonym),
        ("user_pseudonym", request.user_pseudonym),
        ("device_pseudonym", request.device_pseudonym),
        ("key_version", request.key_version),
        ("epoch", request.epoch),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(clauses), tuple(params)


class SQLiteTelemetryAdapter(TelemetryPort):
    """持久化 typed materialized telemetry 的独立 SQLite adapter。"""

    schema_version = TELEMETRY_SCHEMA_VERSION

    def __init__(
        self,
        db_path: str | Path,
        pseudonymizer: HmacPseudonymizer | None = None,
        *,
        retention_s: int,
        k_threshold: int = DEFAULT_K_THRESHOLD,
    ) -> None:
        if retention_s <= 0 or k_threshold < 1:
            raise ValueError("retention_s and k_threshold must be positive")
        self._db_path = Path(db_path)
        self._pseudonymizer = pseudonymizer
        self._retention_s = retention_s
        self._k_threshold = k_threshold
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO telemetry_schema_version (version) VALUES (?)",
                (TELEMETRY_SCHEMA_VERSION,),
            )
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM telemetry_schema_version ORDER BY version"
                )
            )
            if versions != (TELEMETRY_SCHEMA_VERSION,):
                raise RuntimeError("unsupported telemetry schema version")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def stored_event_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM telemetry_events").fetchone()
            return int(row[0])
        finally:
            connection.close()

    @property
    def deletion_audit(self) -> tuple[DeletionReceipt, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT audit_id, deleted_event_count, deleted_at, reason "
                "FROM telemetry_deletion_audit ORDER BY deleted_at, audit_id"
            ).fetchall()
            return tuple(
                DeletionReceipt(
                    audit_id=row["audit_id"],
                    deleted_event_count=row["deleted_event_count"],
                    deleted_at=_parse_timestamp(row["deleted_at"]),
                    reason=row["reason"],
                )
                for row in rows
            )
        finally:
            connection.close()

    async def record(self, observation: TelemetryObservation) -> None:
        await asyncio.to_thread(self._record, observation)

    def _record(self, observation: TelemetryObservation) -> None:
        if self._pseudonymizer is None:
            raise RuntimeError("record requires an injected pseudonymizer")
        event = self._pseudonymizer.materialize(observation)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM telemetry_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if row is not None:
                if _event_from_row(row) != event:
                    raise ValueError("event_id was reused with a different telemetry payload")
            else:
                connection.execute(
                    "INSERT INTO telemetry_events ("
                    "event_id, occurred_at, epoch, key_version, tenant_pseudonym, "
                    "user_pseudonym, device_pseudonym, operation, platform, app_version, "
                    "provider, success, failure_reason, end_to_end_latency_ms, "
                    "queue_wait_ms, audio_duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_values(event),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def query(self, query: TelemetryQuery) -> tuple[TelemetryAggregate, ...]:
        return await asyncio.to_thread(self._query, query)

    def _query(self, query: TelemetryQuery) -> tuple[TelemetryAggregate, ...]:
        connection = self._connect()
        try:
            sql, params = _query_sql(query)
            events = tuple(_event_from_row(row) for row in connection.execute(sql, params))
        finally:
            connection.close()
        groups: dict[_AggregateKey, list[TelemetryEvent]] = defaultdict(list)
        for event in events:
            groups[_event_key(event)].append(event)
        threshold = max(self._k_threshold, query.k_threshold)
        return tuple(
            _aggregate(key, tuple(grouped_events))
            for key, grouped_events in sorted(groups.items())
            if len({event.identity.user_pseudonym for event in grouped_events}) >= threshold
        )

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        return await asyncio.to_thread(self._purge_expired, now or utc_now())

    def _purge_expired(self, now: datetime) -> int:
        cutoff = _timestamp(_as_utc(now) - timedelta(seconds=self._retention_s))
        return self._delete_rows("occurred_at < ?", (cutoff,))

    async def delete(self, request: TelemetryDeleteRequest) -> DeletionReceipt:
        return await asyncio.to_thread(self._delete, request)

    def _delete(self, request: TelemetryDeleteRequest) -> DeletionReceipt:
        where, params = _matches_delete_sql(request)
        deleted_at = utc_now()
        receipt = DeletionReceipt(
            audit_id=f"delete-{uuid4().hex}",
            deleted_event_count=0,
            deleted_at=deleted_at,
            reason=request.reason,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(f"DELETE FROM telemetry_events WHERE {where}", params)
            deleted_count = int(cursor.rowcount)
            receipt = receipt.model_copy(update={"deleted_event_count": deleted_count})
            connection.execute(
                "INSERT INTO telemetry_deletion_audit "
                "(audit_id, deleted_event_count, deleted_at, reason) VALUES (?, ?, ?, ?)",
                (
                    receipt.audit_id,
                    receipt.deleted_event_count,
                    _timestamp(receipt.deleted_at),
                    receipt.reason.value,
                ),
            )
            connection.commit()
            return receipt
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _delete_rows(self, where: str, params: tuple[object, ...]) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(f"DELETE FROM telemetry_events WHERE {where}", params)
            deleted_count = int(cursor.rowcount)
            connection.commit()
            return deleted_count
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = ["TELEMETRY_SCHEMA_VERSION", "SQLiteTelemetryAdapter"]
