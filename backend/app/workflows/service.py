"""Workflow 0.3 状态机与事件投影。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.config import Settings
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseOwnershipError, LeaseToken
from app.schemas.events import EchoEvent
from app.schemas.workflow import (
    TERMINAL_WORKFLOW_STATES,
    WorkflowEventDTO,
    WorkflowRunCreate,
    WorkflowRunDTO,
    WorkflowState,
    WorkflowVisibility,
)
from app.security import LEGACY_OWNER_ID, Principal
from app.security.context import current_principal

log = logging.getLogger("echodesk.workflow")

_OUTBOX_RETRY_BASE_S = 0.25
_OUTBOX_RETRY_MAX_S = 30.0
_OUTBOX_RETRY_MAX_EXPONENT = 16
_OUTBOX_GLOBAL_RECOVERY_LEASE_S = 15.0
_OutboxDeliveryLane = Literal["main", "legacy", "scope"]

_WORKFLOW_EXECUTION_LEASE: ContextVar[LeaseToken | None] = ContextVar(
    "workflow_execution_lease",
    default=None,
)


def bind_workflow_execution_lease(lease: LeaseToken) -> Token[LeaseToken | None]:
    return _WORKFLOW_EXECUTION_LEASE.set(lease)


def reset_workflow_execution_lease(token: Token[LeaseToken | None]) -> None:
    _WORKFLOW_EXECUTION_LEASE.reset(token)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_workflow_run_id() -> str:
    return f"run_{uuid4().hex}"


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _scope() -> tuple[str, str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


class InvalidWorkflowTransition(RuntimeError):
    pass


class WorkflowConflictError(RuntimeError):
    pass


class _GlobalRecoveryLeaseLost(RuntimeError):
    pass


_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancel_requested", "cancelled", "failed", "timeout"}),
    "running": frozenset({"cancel_requested", "succeeded", "failed", "timeout"}),
    "cancel_requested": frozenset({"cancelled", "cancel_failed", "succeeded", "failed", "timeout"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "timeout": frozenset(),
    "cancelled": frozenset(),
    "cancel_failed": frozenset(),
}


@dataclass(slots=True)
class WorkflowRunRecord:
    run_id: str
    kind: str
    source: str
    state: str
    title: str | None
    intent_text: str
    meeting_id: str | None = None
    todo_id: str | None = None
    agent_task_id: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timeout_s: float | None = None
    revision: int = 0
    idempotency_key: str | None = None
    active_key: str | None = None
    attempt: int = 1
    parent_run_id: str | None = None
    deadline_at: str | None = None
    cancel_requested_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_WORKFLOW_STATES

    def to_dto(self) -> WorkflowRunDTO:
        return WorkflowRunDTO(
            run_id=self.run_id,
            kind=self.kind,
            source=self.source,
            state=self.state,  # type: ignore[arg-type]
            title=self.title,
            intent_text=self.intent_text,
            meeting_id=self.meeting_id,
            todo_id=self.todo_id,
            agent_task_id=self.agent_task_id,
            input=self.input,
            output=self.output,
            error=self.error,
            timeout_s=self.timeout_s,
            revision=self.revision,
            idempotency_key=self.idempotency_key,
            active_key=self.active_key,
            attempt=self.attempt,
            parent_run_id=self.parent_run_id,
            deadline_at=self.deadline_at,
            cancel_requested_at=self.cancel_requested_at,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            updated_at=self.updated_at,
        )


@dataclass(slots=True)
class WorkflowEventRecord:
    run_id: str
    seq: int
    event_type: str
    state: str
    visibility: str
    message: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dto(self) -> WorkflowEventDTO:
        return WorkflowEventDTO(
            run_id=self.run_id,
            seq=self.seq,
            event_type=self.event_type,
            state=self.state,  # type: ignore[arg-type]
            visibility=self.visibility,  # type: ignore[arg-type]
            message=self.message,
            payload=self.payload,
            created_at=self.created_at,
        )


def _row_to_run(row: aiosqlite.Row) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=row["run_id"],
        kind=row["kind"],
        source=row["source"],
        state=row["state"],
        title=row["title"],
        intent_text=row["intent_text"],
        meeting_id=row["meeting_id"],
        todo_id=row["todo_id"],
        agent_task_id=row["agent_task_id"],
        input=_json_loads(row["input_json"], {}),
        output=_json_loads(row["output_json"], {}),
        error=row["error"],
        timeout_s=float(row["timeout_s"]) if row["timeout_s"] is not None else None,
        revision=int(row["revision"]),
        idempotency_key=row["idempotency_key"],
        active_key=row["active_key"],
        attempt=int(row["attempt"]),
        parent_run_id=row["parent_run_id"],
        deadline_at=row["deadline_at"],
        cancel_requested_at=row["cancel_requested_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row: aiosqlite.Row) -> WorkflowEventRecord:
    return WorkflowEventRecord(
        run_id=row["run_id"],
        seq=int(row["seq"]),
        event_type=row["event_type"],
        state=row["state"],
        visibility=row["visibility"],
        message=row["message"],
        payload=_json_loads(row["payload_json"], {}),
        created_at=row["created_at"],
    )


class WorkflowService:
    """持久化 workflow run/event，并把状态投影到主 WebSocket。"""

    def __init__(
        self,
        settings: Settings,
        event_bus: InMemoryEventBus,
        *,
        consumer_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self._lock = asyncio.Lock()
        self._outbox_lock = asyncio.Lock()
        # Each backend process consumes the shared SQLite outbox independently.
        # Cursor/heartbeat persistence prevents infinite history replay and lets
        # pruning respect the slowest live process.
        self._outbox_consumer_id = consumer_id or f"workflow:{os.getpid()}:{uuid4().hex}"
        self._outbox_global_lease_owner = f"{self._outbox_consumer_id}:instance:{uuid4().hex}"
        self._outbox_global_lease_fence: int | None = None
        self._outbox_cursor = 0
        self._outbox_registered = False
        self._outbox_next_cleanup = 0.0
        self._outbox_poller_task: asyncio.Task[None] | None = None
        self._outbox_scan_saturated = False
        self.execution_leases = ExecutionLeaseStore(settings.db_path)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.settings.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    @staticmethod
    def _workflow_lease(lease: LeaseToken | None = None) -> LeaseToken | None:
        active = lease or _WORKFLOW_EXECUTION_LEASE.get()
        if active is not None and active.resource_kind != "workflow":
            raise LeaseOwnershipError("non-workflow lease used for workflow mutation")
        return active

    async def _assert_workflow_lease(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        lease: LeaseToken | None = None,
    ) -> LeaseToken | None:
        active = self._workflow_lease(lease)
        if active is None:
            return None
        tenant_id, _device_id, owner_id = _scope()
        if (
            active.tenant_id != tenant_id
            or active.owner_id != owner_id
            or active.resource_id != run_id
        ):
            raise LeaseOwnershipError("workflow lease scope does not match the requested run")
        await self.execution_leases.assert_owned(active, conn=conn)
        return active

    async def claim_run_for_execution(
        self,
        run_id: str,
        *,
        holder_id: str,
    ) -> tuple[WorkflowRunRecord, LeaseToken] | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            lease = await self.execution_leases.acquire(
                tenant_id=tenant_id,
                owner_id=owner_id,
                resource_kind="workflow",
                resource_id=run_id,
                holder_id=holder_id,
                ttl_seconds=self.settings.execution_lease_ttl_s,
                conn=conn,
            )
            if lease is None:
                await conn.commit()
                return None
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE run_id = ? AND tenant_id = ? AND owner_id = ?
                     AND state IN ('pending', 'running', 'cancel_requested')""",
                (run_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                await self.execution_leases.release(lease, conn=conn)
                await conn.commit()
                return None
            await conn.commit()
        return _row_to_run(row), lease

    async def renew_run_lease(self, lease: LeaseToken) -> LeaseToken | None:
        return await self.execution_leases.renew(
            lease,
            ttl_seconds=self.settings.execution_lease_ttl_s,
        )

    async def release_run_lease(self, lease: LeaseToken) -> bool:
        return await self.execution_leases.release(lease)

    async def _append_event_tx(
        self,
        conn: aiosqlite.Connection,
        run: WorkflowRunRecord,
        event_type: str,
        *,
        message: str | None,
        payload: dict[str, Any],
        visibility: WorkflowVisibility,
        tenant_id: str,
        device_id: str,
        owner_id: str,
    ) -> WorkflowEventRecord:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM workflow_events "
            "WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
            (run.run_id, tenant_id, owner_id),
        )
        seq_row = await cur.fetchone()
        await cur.close()
        event = WorkflowEventRecord(
            run_id=run.run_id,
            seq=int(seq_row["next_seq"] if seq_row else 1),
            event_type=event_type,
            state=run.state,
            visibility=visibility,
            message=message,
            payload=payload,
            created_at=utc_now_iso(),
        )
        await conn.execute(
            """INSERT INTO workflow_events
               (run_id, seq, event_type, state, visibility, message, payload_json, created_at,
                tenant_id, device_id, owner_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.run_id,
                event.seq,
                event.event_type,
                event.state,
                event.visibility,
                event.message,
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at,
                tenant_id,
                device_id,
                owner_id,
            ),
        )
        for topic, body in (
            ("workflow.event", event.to_dto().model_dump(mode="json")),
            ("workflow.snapshot", run.to_dto().model_dump(mode="json")),
        ):
            await conn.execute(
                """INSERT INTO workflow_outbox
                   (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                    event_type, payload_json, created_at)
                   VALUES (?, ?, ?, 'workflow', ?, ?, ?, ?)""",
                (
                    tenant_id,
                    device_id,
                    owner_id,
                    run.run_id,
                    topic,
                    json.dumps(
                        {"meeting_id": run.meeting_id, "payload": body},
                        ensure_ascii=False,
                    ),
                    event.created_at,
                ),
            )
        return event

    async def _append_domain_outbox_tx(
        self,
        conn: aiosqlite.Connection,
        event: EchoEvent,
        *,
        aggregate_id: str,
        tenant_id: str,
        device_id: str,
        owner_id: str,
    ) -> None:
        await conn.execute(
            """INSERT INTO workflow_outbox
               (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at)
               VALUES (?, ?, ?, 'domain', ?, ?, ?, ?)""",
            (
                tenant_id,
                device_id,
                owner_id,
                aggregate_id,
                event.type,
                json.dumps(
                    {"meeting_id": event.meeting_id, "payload": event.payload},
                    ensure_ascii=False,
                ),
                event.ts.isoformat(),
            ),
        )

    async def _outbox_replay_floor(self, conn: aiosqlite.Connection, rows: int) -> int:
        if rows == 0:
            cur = await conn.execute("SELECT COALESCE(MAX(outbox_id), 0) FROM workflow_outbox")
        else:
            cur = await conn.execute(
                """SELECT outbox_id FROM workflow_outbox
                   ORDER BY outbox_id DESC LIMIT 1 OFFSET ?""",
                (rows,),
            )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row is not None else 0

    async def _ensure_outbox_consumer(self, conn: aiosqlite.Connection) -> None:
        """Register in constant metadata without scanning events under a write lock.

        Ancient unpublished rows are covered by the singleton global recovery
        watermark.  Migration-034 sparse rows are read only for upgrade drain;
        new random consumer ids never copy one row per historical event.
        """

        now_epoch = time.time()
        cur = await conn.execute(
            """SELECT cursor_outbox_id FROM workflow_outbox_consumers
               WHERE consumer_id = ?""",
            (self._outbox_consumer_id,),
        )
        existing_before_lock = await cur.fetchone()
        await cur.close()
        candidate_cursor: int | None = None
        if existing_before_lock is None:
            # This bounded read deliberately happens before BEGIN IMMEDIATE.
            # A concurrent append receives a higher id and is consumed through
            # the normal main cursor after registration.
            candidate_cursor = await self._outbox_replay_floor(
                conn,
                self.settings.workflow_outbox_replay_window_rows,
            )
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                """SELECT cursor_outbox_id FROM workflow_outbox_consumers
                   WHERE consumer_id = ?""",
                (self._outbox_consumer_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                cursor = candidate_cursor if candidate_cursor is not None else 0
                await conn.execute(
                    """INSERT INTO workflow_outbox_consumers
                       (consumer_id, cursor_outbox_id, started_at, heartbeat_at)
                       VALUES (?, ?, ?, ?)""",
                    (self._outbox_consumer_id, cursor, utc_now_iso(), now_epoch),
                )
                await conn.execute(
                    """UPDATE workflow_outbox_global_recovery_state
                       SET recovery_through_outbox_id = MAX(recovery_through_outbox_id, ?),
                           updated_at = ?
                       WHERE singleton = 1""",
                    (cursor, utc_now_iso()),
                )
            else:
                cursor = int(row["cursor_outbox_id"])
                await conn.execute(
                    """UPDATE workflow_outbox_consumers SET heartbeat_at = ?
                       WHERE consumer_id = ?""",
                    (now_epoch, self._outbox_consumer_id),
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        self._outbox_cursor = cursor
        self._outbox_registered = True

    async def _has_earlier_scope_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> bool:
        cur = await conn.execute(
            """SELECT 1
               FROM workflow_outbox_consumer_scope_recovery
               WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?
                 AND next_outbox_id < ?
               LIMIT 1""",
            (
                self._outbox_consumer_id,
                str(row["tenant_id"]),
                str(row["owner_id"]),
                int(row["outbox_id"]),
            ),
        )
        compact_blocked = await cur.fetchone()
        await cur.close()
        if compact_blocked is not None:
            return True
        cur = await conn.execute(
            """SELECT 1 FROM workflow_outbox_global_scope_recovery
               WHERE tenant_id = ? AND owner_id = ? AND next_outbox_id < ?
               LIMIT 1""",
            (
                str(row["tenant_id"]),
                str(row["owner_id"]),
                int(row["outbox_id"]),
            ),
        )
        global_blocked = await cur.fetchone()
        await cur.close()
        if global_blocked is not None:
            return True
        # Rows already present before migration 035 remain exact sparse upgrade
        # state and are drained without creating any new per-event snapshots.
        cur = await conn.execute(
            """SELECT 1
               FROM workflow_outbox_consumer_recovery AS recovery
               JOIN workflow_outbox AS earlier
                 ON earlier.outbox_id = recovery.outbox_id
               WHERE recovery.consumer_id = ?
                 AND recovery.outbox_id < ?
                 AND earlier.tenant_id = ? AND earlier.owner_id = ?
               LIMIT 1""",
            (
                self._outbox_consumer_id,
                int(row["outbox_id"]),
                str(row["tenant_id"]),
                str(row["owner_id"]),
            ),
        )
        blocked = await cur.fetchone()
        await cur.close()
        return blocked is not None

    async def _queue_scope_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        error: str | None,
        attempted: bool,
    ) -> None:
        """Persist one compact ordered lane for an arbitrarily long failed scope."""

        row_id = int(row["outbox_id"])
        cur = await conn.execute(
            """SELECT next_outbox_id, attempts
               FROM workflow_outbox_consumer_scope_recovery
               WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?""",
            (
                self._outbox_consumer_id,
                str(row["tenant_id"]),
                str(row["owner_id"]),
            ),
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            attempts = 1 if attempted else 0
            next_retry_at = time.time() + self._outbox_retry_delay(attempts) if attempted else 0.0
            await conn.execute(
                """INSERT INTO workflow_outbox_consumer_scope_recovery
                   (consumer_id, tenant_id, owner_id, next_outbox_id,
                    attempts, next_retry_at, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._outbox_consumer_id,
                    str(row["tenant_id"]),
                    str(row["owner_id"]),
                    row_id,
                    attempts,
                    next_retry_at,
                    error[:500] if error else None,
                ),
            )
            return
        existing_id = int(existing["next_outbox_id"])
        if row_id < existing_id:
            attempts = 1 if attempted else 0
            await conn.execute(
                """UPDATE workflow_outbox_consumer_scope_recovery
                   SET next_outbox_id = ?, attempts = ?, next_retry_at = ?, last_error = ?
                   WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?""",
                (
                    row_id,
                    attempts,
                    time.time() + self._outbox_retry_delay(attempts) if attempted else 0.0,
                    error[:500] if error else None,
                    self._outbox_consumer_id,
                    str(row["tenant_id"]),
                    str(row["owner_id"]),
                ),
            )
            return
        if attempted and row_id == existing_id:
            attempts = int(existing["attempts"]) + 1
            await conn.execute(
                """UPDATE workflow_outbox_consumer_scope_recovery
                   SET attempts = ?, next_retry_at = ?, last_error = ?
                   WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?""",
                (
                    attempts,
                    time.time() + self._outbox_retry_delay(attempts),
                    error[:500] if error else None,
                    self._outbox_consumer_id,
                    str(row["tenant_id"]),
                    str(row["owner_id"]),
                ),
            )

    async def _retry_legacy_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        error: str,
    ) -> None:
        cur = await conn.execute(
            """SELECT attempts FROM workflow_outbox_consumer_recovery
               WHERE consumer_id = ? AND outbox_id = ?""",
            (self._outbox_consumer_id, int(row["outbox_id"])),
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            raise RuntimeError("legacy outbox recovery row disappeared")
        attempts = int(existing["attempts"]) + 1
        await conn.execute(
            """UPDATE workflow_outbox_consumer_recovery
               SET attempts = ?, next_retry_at = ?, last_error = ?
               WHERE consumer_id = ? AND outbox_id = ?""",
            (
                attempts,
                time.time() + self._outbox_retry_delay(attempts),
                error[:500],
                self._outbox_consumer_id,
                int(row["outbox_id"]),
            ),
        )

    @staticmethod
    def _outbox_retry_delay(attempts: int) -> float:
        exponent = min(max(0, attempts - 1), _OUTBOX_RETRY_MAX_EXPONENT)
        return float(min(_OUTBOX_RETRY_MAX_S, _OUTBOX_RETRY_BASE_S * (2**exponent)))

    async def _advance_outbox_cursor(
        self,
        conn: aiosqlite.Connection,
        row_id: int,
    ) -> None:
        self._outbox_cursor = max(self._outbox_cursor, row_id)
        await conn.execute(
            """UPDATE workflow_outbox_consumers
               SET cursor_outbox_id = MAX(cursor_outbox_id, ?), heartbeat_at = ?
               WHERE consumer_id = ?""",
            (self._outbox_cursor, time.time(), self._outbox_consumer_id),
        )

    async def _publish_outbox_row(self, row: aiosqlite.Row) -> None:
        body = _json_loads(row["payload_json"], {})
        await self.event_bus.publish_to(
            (str(row["tenant_id"]), str(row["owner_id"])),
            EchoEvent(
                type=row["event_type"],
                meeting_id=body.get("meeting_id"),
                payload=body.get("payload") or {},
                tenant_id=row["tenant_id"],
                owner_id=row["owner_id"],
            ),
        )

    async def _mark_outbox_published(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        lane: _OutboxDeliveryLane,
    ) -> None:
        row_id = int(row["outbox_id"])
        await conn.execute(
            """UPDATE workflow_outbox
               SET published_at = COALESCE(published_at, ?),
                   attempts = attempts + 1, last_error = NULL
               WHERE outbox_id = ?""",
            (utc_now_iso(), row_id),
        )
        if lane == "main":
            await self._advance_outbox_cursor(conn, row_id)
        elif lane == "legacy":
            await conn.execute(
                """DELETE FROM workflow_outbox_consumer_recovery
                   WHERE consumer_id = ? AND outbox_id = ?""",
                (self._outbox_consumer_id, row_id),
            )
        else:
            await self._advance_scope_recovery(conn, row)
        if lane != "main":
            await conn.execute(
                """UPDATE workflow_outbox_consumers SET heartbeat_at = ?
                   WHERE consumer_id = ?""",
                (time.time(), self._outbox_consumer_id),
            )

    async def _advance_scope_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> None:
        tenant_id = str(row["tenant_id"])
        owner_id = str(row["owner_id"])
        cur = await conn.execute(
            """SELECT 1
               FROM workflow_outbox_consumer_scope_recovery
               WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?
                 AND next_outbox_id = ?""",
            (self._outbox_consumer_id, tenant_id, owner_id, int(row["outbox_id"])),
        )
        lane_row = await cur.fetchone()
        await cur.close()
        if lane_row is None:
            raise RuntimeError("outbox scope recovery lane disappeared")
        cur = await conn.execute(
            """SELECT outbox_id FROM workflow_outbox
               WHERE tenant_id = ? AND owner_id = ?
                 AND outbox_id > ? AND outbox_id <= ?
               ORDER BY outbox_id ASC LIMIT 1""",
            (
                tenant_id,
                owner_id,
                int(row["outbox_id"]),
                self._outbox_cursor,
            ),
        )
        next_row = await cur.fetchone()
        await cur.close()
        if next_row is None:
            await conn.execute(
                """DELETE FROM workflow_outbox_consumer_scope_recovery
                   WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?""",
                (self._outbox_consumer_id, tenant_id, owner_id),
            )
            return
        await conn.execute(
            """UPDATE workflow_outbox_consumer_scope_recovery
               SET next_outbox_id = ?, attempts = 0, next_retry_at = 0, last_error = NULL
               WHERE consumer_id = ? AND tenant_id = ? AND owner_id = ?""",
            (int(next_row["outbox_id"]), self._outbox_consumer_id, tenant_id, owner_id),
        )

    async def _mark_outbox_failed(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        exc: Exception,
        *,
        lane: _OutboxDeliveryLane,
    ) -> None:
        error = str(exc)[:500]
        await conn.execute(
            """UPDATE workflow_outbox
               SET attempts = attempts + 1, last_error = ?
               WHERE outbox_id = ?""",
            (error, int(row["outbox_id"])),
        )
        if lane == "main":
            await self._queue_scope_recovery(conn, row, error=error, attempted=True)
            await self._advance_outbox_cursor(conn, int(row["outbox_id"]))
        elif lane == "legacy":
            await self._retry_legacy_recovery(conn, row, error=error)
        else:
            await self._queue_scope_recovery(conn, row, error=error, attempted=True)
        if lane != "main":
            await conn.execute(
                """UPDATE workflow_outbox_consumers SET heartbeat_at = ?
                   WHERE consumer_id = ?""",
                (time.time(), self._outbox_consumer_id),
            )

    async def _due_legacy_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        cur = await conn.execute(
            """SELECT outbox.*
               FROM workflow_outbox_consumer_recovery AS recovery
               JOIN workflow_outbox AS outbox
                 ON outbox.outbox_id = recovery.outbox_id
               WHERE recovery.consumer_id = ? AND recovery.next_retry_at <= ?
                 AND NOT EXISTS (
                     SELECT 1
                     FROM workflow_outbox_consumer_recovery AS prior
                     JOIN workflow_outbox AS prior_outbox
                       ON prior_outbox.outbox_id = prior.outbox_id
                     WHERE prior.consumer_id = recovery.consumer_id
                       AND prior.outbox_id < recovery.outbox_id
                       AND prior_outbox.tenant_id = outbox.tenant_id
                       AND prior_outbox.owner_id = outbox.owner_id
                 )
               ORDER BY outbox.outbox_id ASC LIMIT ?""",
            (self._outbox_consumer_id, time.time(), limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def _due_scope_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        cur = await conn.execute(
            """SELECT outbox.*
               FROM workflow_outbox_consumer_scope_recovery AS recovery
               JOIN workflow_outbox AS outbox
                 ON outbox.outbox_id = recovery.next_outbox_id
               WHERE recovery.consumer_id = ? AND recovery.next_retry_at <= ?
                 AND NOT EXISTS (
                     SELECT 1
                     FROM workflow_outbox_consumer_recovery AS legacy
                     JOIN workflow_outbox AS legacy_outbox
                       ON legacy_outbox.outbox_id = legacy.outbox_id
                     WHERE legacy.consumer_id = recovery.consumer_id
                       AND legacy.outbox_id < recovery.next_outbox_id
                       AND legacy_outbox.tenant_id = recovery.tenant_id
                       AND legacy_outbox.owner_id = recovery.owner_id
                 )
                 AND NOT EXISTS (
                     SELECT 1
                     FROM workflow_outbox_global_scope_recovery AS global_recovery
                     WHERE global_recovery.tenant_id = recovery.tenant_id
                       AND global_recovery.owner_id = recovery.owner_id
                       AND global_recovery.next_outbox_id < recovery.next_outbox_id
                 )
               ORDER BY recovery.next_outbox_id ASC LIMIT ?""",
            (self._outbox_consumer_id, time.time(), limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def _flush_outbox_main_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int]:
        cur = await conn.execute(
            """SELECT * FROM workflow_outbox
               WHERE outbox_id > ? ORDER BY outbox_id ASC LIMIT ?""",
            (self._outbox_cursor, limit),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        published = 0
        for row in rows:
            if await self._has_earlier_scope_recovery(conn, row):
                await self._queue_scope_recovery(
                    conn,
                    row,
                    error=None,
                    attempted=False,
                )
                await self._advance_outbox_cursor(conn, int(row["outbox_id"]))
                await conn.commit()
                continue
            try:
                await self._publish_outbox_row(row)
            except Exception as exc:
                await self._mark_outbox_failed(
                    conn,
                    row,
                    exc,
                    lane="main",
                )
                await conn.commit()
                continue
            await self._mark_outbox_published(conn, row, lane="main")
            await conn.commit()
            published += 1
        return published, len(rows)

    async def _flush_legacy_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int]:
        published = 0
        processed = 0
        while processed < limit:
            rows = await self._due_legacy_recovery_rows(conn, limit=limit - processed)
            if not rows:
                break
            processed += len(rows)
            for row in rows:
                try:
                    await self._publish_outbox_row(row)
                except Exception as exc:
                    await self._mark_outbox_failed(
                        conn,
                        row,
                        exc,
                        lane="legacy",
                    )
                    await conn.commit()
                    continue
                await self._mark_outbox_published(conn, row, lane="legacy")
                await conn.commit()
                published += 1
        return published, processed

    async def _flush_scope_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int]:
        published = 0
        processed = 0
        while processed < limit:
            rows = await self._due_scope_recovery_rows(conn, limit=limit - processed)
            if not rows:
                break
            processed += len(rows)
            for row in rows:
                try:
                    await self._publish_outbox_row(row)
                except Exception as exc:
                    await self._mark_outbox_failed(conn, row, exc, lane="scope")
                    await conn.commit()
                    continue
                await self._mark_outbox_published(conn, row, lane="scope")
                await conn.commit()
                published += 1
        return published, processed

    async def _flush_outbox_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int]:
        legacy_published, legacy_processed = await self._flush_legacy_recovery_rows(
            conn,
            limit=limit,
        )
        remaining = max(0, limit - legacy_processed)
        scope_published, scope_processed = await self._flush_scope_recovery_rows(
            conn,
            limit=remaining,
        )
        return legacy_published + scope_published, legacy_processed + scope_processed

    async def _try_acquire_global_recovery_lease(
        self,
        conn: aiosqlite.Connection,
    ) -> bool:
        """Acquire the singleton ancient-row scanner without holding the DB lock."""

        now_epoch = time.time()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                """UPDATE workflow_outbox_global_recovery_state
                   SET lease_fence = CASE
                           WHEN lease_owner = ? THEN lease_fence
                           ELSE lease_fence + 1
                       END,
                       lease_owner = ?, lease_expires_at = ?, updated_at = ?
                   WHERE singleton = 1
                     AND (
                         lease_owner IS NULL OR lease_owner = ? OR lease_expires_at <= ?
                     )
                     AND (
                         last_failed_owner IS NULL OR last_failed_owner <> ?
                         OR failed_owner_retry_at <= ?
                     )""",
                (
                    self._outbox_global_lease_owner,
                    self._outbox_global_lease_owner,
                    now_epoch + _OUTBOX_GLOBAL_RECOVERY_LEASE_S,
                    utc_now_iso(),
                    self._outbox_global_lease_owner,
                    now_epoch,
                    self._outbox_global_lease_owner,
                    now_epoch,
                ),
            )
            acquired = cur.rowcount == 1
            await cur.close()
            if acquired:
                cur = await conn.execute(
                    """SELECT lease_fence FROM workflow_outbox_global_recovery_state
                       WHERE singleton = 1 AND lease_owner = ?""",
                    (self._outbox_global_lease_owner,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise _GlobalRecoveryLeaseLost("global recovery lease disappeared")
                self._outbox_global_lease_fence = int(row["lease_fence"])
            else:
                self._outbox_global_lease_fence = None
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        return acquired

    async def _renew_global_recovery_lease(self, conn: aiosqlite.Connection) -> None:
        fence = self._outbox_global_lease_fence
        if fence is None:
            raise _GlobalRecoveryLeaseLost("global recovery lease is not owned")
        now_epoch = time.time()
        cur = await conn.execute(
            """UPDATE workflow_outbox_global_recovery_state
               SET lease_expires_at = ?, updated_at = ?
               WHERE singleton = 1 AND lease_owner = ? AND lease_fence = ?
                 AND lease_expires_at > ?""",
            (
                now_epoch + _OUTBOX_GLOBAL_RECOVERY_LEASE_S,
                utc_now_iso(),
                self._outbox_global_lease_owner,
                fence,
                now_epoch,
            ),
        )
        renewed = cur.rowcount == 1
        await cur.close()
        if not renewed:
            raise _GlobalRecoveryLeaseLost("global recovery lease expired or was fenced")

    async def _assert_global_recovery_lease(self, conn: aiosqlite.Connection) -> None:
        fence = self._outbox_global_lease_fence
        if fence is None:
            raise _GlobalRecoveryLeaseLost("global recovery lease is not owned")
        cur = await conn.execute(
            """SELECT 1 FROM workflow_outbox_global_recovery_state
               WHERE singleton = 1 AND lease_owner = ? AND lease_fence = ?
                 AND lease_expires_at > ?""",
            (self._outbox_global_lease_owner, fence, time.time()),
        )
        owned = await cur.fetchone()
        await cur.close()
        if owned is None:
            raise _GlobalRecoveryLeaseLost("global recovery lease expired or was fenced")

    async def _begin_global_recovery_mutation(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await self._assert_global_recovery_lease(conn)
        except BaseException:
            await conn.rollback()
            raise

    async def _release_global_recovery_lease(self, conn: aiosqlite.Connection) -> None:
        fence = self._outbox_global_lease_fence
        if fence is None:
            return
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                """UPDATE workflow_outbox_global_recovery_state
                   SET lease_owner = NULL, lease_expires_at = 0, updated_at = ?
                   WHERE singleton = 1 AND lease_owner = ? AND lease_fence = ?""",
                (utc_now_iso(), self._outbox_global_lease_owner, fence),
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            self._outbox_global_lease_fence = None

    async def _global_recovery_position(
        self,
        conn: aiosqlite.Connection,
    ) -> tuple[int, int]:
        cur = await conn.execute(
            """SELECT scan_cursor_outbox_id, recovery_through_outbox_id
               FROM workflow_outbox_global_recovery_state WHERE singleton = 1"""
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RuntimeError("workflow outbox global recovery state is missing")
        return int(row["scan_cursor_outbox_id"]), int(row["recovery_through_outbox_id"])

    async def _advance_global_scan_cursor(
        self,
        conn: aiosqlite.Connection,
        outbox_id: int,
    ) -> None:
        """Advance discovery only while this exact, unexpired fence owns the write lock."""

        fence = self._outbox_global_lease_fence
        if fence is None:
            raise _GlobalRecoveryLeaseLost("global recovery lease is not owned")
        now_epoch = time.time()
        cur = await conn.execute(
            """UPDATE workflow_outbox_global_recovery_state
               SET scan_cursor_outbox_id = MAX(scan_cursor_outbox_id, ?),
                   lease_expires_at = ?, updated_at = ?
               WHERE singleton = 1 AND lease_owner = ? AND lease_fence = ?
                 AND lease_expires_at > ?""",
            (
                outbox_id,
                now_epoch + _OUTBOX_GLOBAL_RECOVERY_LEASE_S,
                utc_now_iso(),
                self._outbox_global_lease_owner,
                fence,
                now_epoch,
            ),
        )
        advanced = cur.rowcount == 1
        await cur.close()
        if not advanced:
            raise _GlobalRecoveryLeaseLost("global recovery scan cursor was fenced")

    async def _queue_global_scope_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        error: str | None,
        attempted: bool,
    ) -> None:
        tenant_id = str(row["tenant_id"])
        owner_id = str(row["owner_id"])
        row_id = int(row["outbox_id"])
        cur = await conn.execute(
            """SELECT next_outbox_id, attempts
               FROM workflow_outbox_global_scope_recovery
               WHERE tenant_id = ? AND owner_id = ?""",
            (tenant_id, owner_id),
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is None:
            attempts = 1 if attempted else 0
            await conn.execute(
                """INSERT INTO workflow_outbox_global_scope_recovery
                   (tenant_id, owner_id, next_outbox_id, attempts, next_retry_at, last_error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    tenant_id,
                    owner_id,
                    row_id,
                    attempts,
                    time.time() + self._outbox_retry_delay(attempts) if attempted else 0.0,
                    error[:500] if error else None,
                ),
            )
            return
        existing_id = int(existing["next_outbox_id"])
        if row_id < existing_id:
            attempts = 1 if attempted else 0
            await conn.execute(
                """UPDATE workflow_outbox_global_scope_recovery
                   SET next_outbox_id = ?, attempts = ?, next_retry_at = ?, last_error = ?
                   WHERE tenant_id = ? AND owner_id = ?""",
                (
                    row_id,
                    attempts,
                    time.time() + self._outbox_retry_delay(attempts) if attempted else 0.0,
                    error[:500] if error else None,
                    tenant_id,
                    owner_id,
                ),
            )
            return
        if attempted and row_id == existing_id:
            attempts = int(existing["attempts"]) + 1
            await conn.execute(
                """UPDATE workflow_outbox_global_scope_recovery
                   SET attempts = ?, next_retry_at = ?, last_error = ?
                   WHERE tenant_id = ? AND owner_id = ?""",
                (
                    attempts,
                    time.time() + self._outbox_retry_delay(attempts),
                    error[:500] if error else None,
                    tenant_id,
                    owner_id,
                ),
            )

    async def _normalize_global_scope_recovery(
        self,
        conn: aiosqlite.Connection,
    ) -> None:
        """Advance heads already projected by another process; never skip unpublished rows."""

        await self._begin_global_recovery_mutation(conn)
        _scan_cursor, recovery_through = await self._global_recovery_position(conn)
        cur = await conn.execute(
            """SELECT tenant_id, owner_id, next_outbox_id
               FROM workflow_outbox_global_scope_recovery
               ORDER BY next_outbox_id"""
        )
        lanes = await cur.fetchall()
        await cur.close()
        for lane in lanes:
            tenant_id = str(lane["tenant_id"])
            owner_id = str(lane["owner_id"])
            current_id = int(lane["next_outbox_id"])
            cur = await conn.execute(
                """SELECT MIN(outbox_id) FROM workflow_outbox
                   WHERE tenant_id = ? AND owner_id = ?
                     AND outbox_id >= ? AND outbox_id <= ?
                     AND published_at IS NULL""",
                (tenant_id, owner_id, current_id, recovery_through),
            )
            next_row = await cur.fetchone()
            await cur.close()
            next_id = int(next_row[0]) if next_row and next_row[0] is not None else None
            if next_id is None:
                await conn.execute(
                    """DELETE FROM workflow_outbox_global_scope_recovery
                       WHERE tenant_id = ? AND owner_id = ?""",
                    (tenant_id, owner_id),
                )
            elif next_id != current_id:
                await conn.execute(
                    """UPDATE workflow_outbox_global_scope_recovery
                       SET next_outbox_id = ?, attempts = 0,
                           next_retry_at = 0, last_error = NULL
                       WHERE tenant_id = ? AND owner_id = ?""",
                    (next_id, tenant_id, owner_id),
                )
        await self._renew_global_recovery_lease(conn)
        await conn.commit()

    async def _advance_global_scope_recovery(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> None:
        tenant_id = str(row["tenant_id"])
        owner_id = str(row["owner_id"])
        _scan_cursor, recovery_through = await self._global_recovery_position(conn)
        cur = await conn.execute(
            """SELECT MIN(outbox_id) FROM workflow_outbox
               WHERE tenant_id = ? AND owner_id = ?
                 AND outbox_id > ? AND outbox_id <= ?
                 AND published_at IS NULL""",
            (tenant_id, owner_id, int(row["outbox_id"]), recovery_through),
        )
        next_row = await cur.fetchone()
        await cur.close()
        if next_row is None or next_row[0] is None:
            await conn.execute(
                """DELETE FROM workflow_outbox_global_scope_recovery
                   WHERE tenant_id = ? AND owner_id = ?""",
                (tenant_id, owner_id),
            )
            return
        await conn.execute(
            """UPDATE workflow_outbox_global_scope_recovery
               SET next_outbox_id = ?, attempts = 0, next_retry_at = 0, last_error = NULL
               WHERE tenant_id = ? AND owner_id = ?""",
            (int(next_row[0]), tenant_id, owner_id),
        )

    async def _mark_global_outbox_published(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        advance_lane: bool,
    ) -> None:
        await self._begin_global_recovery_mutation(conn)
        await conn.execute(
            """UPDATE workflow_outbox
               SET published_at = COALESCE(published_at, ?),
                   attempts = attempts + 1, last_error = NULL
               WHERE outbox_id = ?""",
            (utc_now_iso(), int(row["outbox_id"])),
        )
        if advance_lane:
            await self._advance_global_scope_recovery(conn, row)
        # Ancient recovery is global-at-least-once, but an active pre-v035
        # consumer may still have event-only UI state that REST cannot rebuild.
        # This process has just projected to *its* local bus, so only its own
        # sparse pointer is redundant; every other consumer drains independently
        # or is removed by the existing heartbeat TTL cascade.
        await conn.execute(
            """DELETE FROM workflow_outbox_consumer_recovery
               WHERE consumer_id = ? AND outbox_id = ?""",
            (self._outbox_consumer_id, int(row["outbox_id"])),
        )
        await self._renew_global_recovery_lease(conn)

    async def _mark_global_outbox_failed(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        exc: Exception,
    ) -> None:
        await self._begin_global_recovery_mutation(conn)
        error = str(exc)[:500]
        await conn.execute(
            """UPDATE workflow_outbox
               SET attempts = attempts + 1, last_error = ?
               WHERE outbox_id = ?""",
            (error, int(row["outbox_id"])),
        )
        await self._queue_global_scope_recovery(
            conn,
            row,
            error=error,
            attempted=True,
        )
        fence = self._outbox_global_lease_fence
        if fence is None:
            raise _GlobalRecoveryLeaseLost("global recovery lease is not owned")
        await conn.execute(
            """UPDATE workflow_outbox_global_recovery_state
               SET last_failed_owner = ?, failed_owner_retry_at = ?, updated_at = ?
               WHERE singleton = 1 AND lease_owner = ? AND lease_fence = ?""",
            (
                self._outbox_global_lease_owner,
                time.time() + self._outbox_retry_delay(1),
                utc_now_iso(),
                self._outbox_global_lease_owner,
                fence,
            ),
        )
        await self._renew_global_recovery_lease(conn)

    async def _global_scope_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> list[aiosqlite.Row]:
        cur = await conn.execute(
            """SELECT outbox.*
               FROM workflow_outbox_global_scope_recovery AS recovery
               JOIN workflow_outbox AS outbox
                 ON outbox.outbox_id = recovery.next_outbox_id
               WHERE recovery.next_retry_at <= ?
               ORDER BY recovery.next_outbox_id ASC LIMIT ?""",
            (time.time(), limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def _flush_global_scope_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int]:
        published = 0
        processed = 0
        while processed < limit:
            rows = await self._global_scope_recovery_rows(conn, limit=limit - processed)
            if not rows:
                break
            processed += len(rows)
            for row in rows:
                try:
                    await self._publish_outbox_row(row)
                except Exception as exc:
                    await self._mark_global_outbox_failed(conn, row, exc)
                    await conn.commit()
                    continue
                await self._mark_global_outbox_published(conn, row, advance_lane=True)
                await conn.commit()
                published += 1
        return published, processed

    async def _flush_global_discovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int, bool]:
        scan_cursor, recovery_through = await self._global_recovery_position(conn)
        if scan_cursor >= recovery_through:
            return 0, 0, True
        cur = await conn.execute(
            """SELECT outbox.*
               FROM workflow_outbox AS outbox
               WHERE outbox.published_at IS NULL
                 AND outbox.outbox_id > ? AND outbox.outbox_id <= ?
               ORDER BY outbox.outbox_id ASC LIMIT ?""",
            (scan_cursor, recovery_through, limit),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        cur = await conn.execute(
            """SELECT tenant_id, owner_id
               FROM workflow_outbox_global_scope_recovery"""
        )
        blocked_scopes = {(str(row[0]), str(row[1])) for row in await cur.fetchall()}
        await cur.close()
        published = 0
        for row in rows:
            scope = (str(row["tenant_id"]), str(row["owner_id"]))
            if scope not in blocked_scopes:
                try:
                    await self._publish_outbox_row(row)
                except Exception as exc:
                    await self._mark_global_outbox_failed(conn, row, exc)
                    blocked_scopes.add(scope)
                else:
                    await self._mark_global_outbox_published(
                        conn,
                        row,
                        advance_lane=False,
                    )
                    published += 1
            else:
                await self._begin_global_recovery_mutation(conn)
            await self._advance_global_scan_cursor(conn, int(row["outbox_id"]))
            await conn.commit()
        if len(rows) < limit:
            # The indexed unpublished query exhausted this immutable id range;
            # published rows before the cutoff need no per-event scan.
            await self._begin_global_recovery_mutation(conn)
            await self._advance_global_scan_cursor(conn, recovery_through)
            await conn.commit()
        current_scan, current_through = await self._global_recovery_position(conn)
        return published, len(rows), current_scan >= current_through

    async def _flush_global_recovery_rows(
        self,
        conn: aiosqlite.Connection,
        *,
        limit: int,
    ) -> tuple[int, int, bool, bool]:
        acquired = await self._try_acquire_global_recovery_lease(conn)
        if not acquired:
            scan_cursor, recovery_through = await self._global_recovery_position(conn)
            return 0, 0, scan_cursor >= recovery_through, False
        result: tuple[int, int, bool, bool]
        try:
            await self._normalize_global_scope_recovery(conn)
            (
                discovery_published,
                discovery_processed,
                discovery_complete,
            ) = await self._flush_global_discovery_rows(conn, limit=limit)
            remaining = max(0, limit - discovery_processed)
            scope_published = 0
            scope_processed = 0
            if remaining > 0:
                scope_published, scope_processed = await self._flush_global_scope_recovery_rows(
                    conn,
                    limit=remaining,
                )
            result = (
                discovery_published + scope_published,
                discovery_processed + scope_processed,
                discovery_complete,
                True,
            )
        except _GlobalRecoveryLeaseLost:
            await conn.rollback()
            scan_cursor, recovery_through = await self._global_recovery_position(conn)
            result = (0, 0, scan_cursor >= recovery_through, False)
        except BaseException:
            # Cancellation can arrive after a global mutation opened
            # BEGIN IMMEDIATE but before its caller committed.  Release starts
            # its own transaction, so always clear the half-open transaction
            # first and never let cleanup errors mask the original cancellation.
            try:
                await conn.rollback()
            except Exception as cleanup_exc:  # pragma: no cover - defensive shutdown path
                log.warning("global recovery rollback during shutdown failed: %s", cleanup_exc)
            try:
                await self._release_global_recovery_lease(conn)
            except Exception as cleanup_exc:  # pragma: no cover - defensive shutdown path
                log.warning("global recovery lease release during shutdown failed: %s", cleanup_exc)
            raise
        await conn.rollback()
        try:
            await self._release_global_recovery_lease(conn)
        except Exception as cleanup_exc:  # pragma: no cover - lease expires safely
            log.warning("global recovery lease release failed: %s", cleanup_exc)
        return result

    async def flush_outbox(self, *, limit: int = 500) -> int:
        """Publish a bounded main page plus due per-scope recovery heads.

        A failed scope is moved behind the durable main cursor.  Later rows for
        that same tenant/owner join its ordered recovery lane, while unrelated
        scopes continue immediately.  Only the head of each scope lane is ever
        retried, preserving that principal's event order.
        """

        if limit < 0:
            raise ValueError("outbox flush limit must be non-negative")
        published = 0
        self._outbox_scan_saturated = False
        async with self._outbox_lock, self._conn() as conn:
            await self._ensure_outbox_consumer(conn)
            if limit > 0:
                # Upgrade-era exact sparse rows are consumed first.  The global
                # scanner then discovers every ancient unpublished id through
                # the registration watermark before recent main rows may pass.
                legacy_published, legacy_count = await self._flush_legacy_recovery_rows(
                    conn, limit=limit
                )
                (
                    global_published,
                    global_count,
                    discovery_complete,
                    global_acquired,
                ) = await self._flush_global_recovery_rows(conn, limit=limit)
                main_published = 0
                main_count = 0
                scope_published = 0
                scope_count = 0
                if discovery_complete:
                    main_published, main_count = await self._flush_outbox_main_rows(
                        conn,
                        limit=limit,
                    )
                    scope_published, scope_count = await self._flush_scope_recovery_rows(
                        conn,
                        limit=limit,
                    )
                published = legacy_published + global_published + main_published + scope_published
                self._outbox_scan_saturated = (
                    legacy_count >= limit
                    or global_count >= limit
                    or main_count >= limit
                    or scope_count >= limit
                    or (global_acquired and not discovery_complete)
                )
        if time.monotonic() >= self._outbox_next_cleanup:
            self._outbox_next_cleanup = (
                time.monotonic() + self.settings.workflow_outbox_cleanup_interval_s
            )
            try:
                await self.prune_outbox()
            except Exception as exc:  # pragma: no cover - delivery must survive cleanup failure
                log.warning("workflow outbox cleanup failed: %s", exc)
        return published

    async def prune_outbox(self) -> int:
        """Delete safe published history while preserving lagging/recovery consumers."""

        now_epoch = time.time()
        active_after = now_epoch - self.settings.workflow_outbox_consumer_ttl_s
        age_cutoff = datetime.fromtimestamp(
            now_epoch - self.settings.workflow_outbox_retention_s,
            tz=UTC,
        ).isoformat()
        async with self._outbox_lock, self._conn() as conn:
            await self._ensure_outbox_consumer(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    "DELETE FROM workflow_outbox_consumers WHERE heartbeat_at < ?",
                    (active_after,),
                )
                cur = await conn.execute(
                    """SELECT MIN(cursor_outbox_id) FROM workflow_outbox_consumers
                       WHERE heartbeat_at >= ?""",
                    (active_after,),
                )
                safe_row = await cur.fetchone()
                await cur.close()
                safe_cursor = int(safe_row[0]) if safe_row and safe_row[0] is not None else 0
                replay_floor = await self._outbox_replay_floor(
                    conn,
                    self.settings.workflow_outbox_replay_window_rows,
                )
                cur = await conn.execute(
                    """SELECT outbox_id FROM workflow_outbox
                       ORDER BY outbox_id DESC LIMIT 1 OFFSET ?""",
                    (self.settings.workflow_outbox_max_rows,),
                )
                count_row = await cur.fetchone()
                await cur.close()
                count_cutoff = int(count_row[0]) if count_row is not None else 0
                deletion_cursor = min(safe_cursor, replay_floor)
                deleted = 0
                if deletion_cursor > 0:
                    cur = await conn.execute(
                        """DELETE FROM workflow_outbox
                           WHERE published_at IS NOT NULL
                             AND outbox_id <= ?
                             AND (published_at < ? OR outbox_id <= ?)
                             AND NOT EXISTS (
                                 SELECT 1 FROM workflow_outbox_consumer_recovery AS recovery
                                 WHERE recovery.outbox_id = workflow_outbox.outbox_id
                             )
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM workflow_outbox_consumer_scope_recovery AS recovery
                                 JOIN workflow_outbox_consumers AS consumer
                                   ON consumer.consumer_id = recovery.consumer_id
                                 WHERE recovery.tenant_id = workflow_outbox.tenant_id
                                   AND recovery.owner_id = workflow_outbox.owner_id
                                   AND workflow_outbox.outbox_id >= recovery.next_outbox_id
                                   AND workflow_outbox.outbox_id <= consumer.cursor_outbox_id
                             )""",
                        (deletion_cursor, age_cutoff, count_cutoff),
                    )
                    deleted = max(0, cur.rowcount)
                    await cur.close()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return deleted

    def start_outbox_poller(self, *, interval_s: float = 0.1) -> None:
        """Continuously project commits from every backend instance to this process' WS bus."""

        if self._outbox_poller_task is not None and not self._outbox_poller_task.done():
            return
        self._outbox_poller_task = asyncio.create_task(
            self._outbox_poller_loop(interval_s=interval_s),
            name="workflow-outbox-poller",
        )

    async def _outbox_poller_loop(self, *, interval_s: float) -> None:
        while True:
            try:
                published = await self.drain_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive long-running loop
                log.warning("workflow outbox poll failed: %s", exc)
                published = 0
            if published == 0:
                await asyncio.sleep(interval_s)

    async def aclose(self) -> None:
        task = self._outbox_poller_task
        self._outbox_poller_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not self._outbox_registered:
            return
        async with self._outbox_lock, self._conn() as conn:
            await conn.execute(
                """UPDATE workflow_outbox_global_recovery_state
                   SET lease_owner = NULL, lease_expires_at = 0, updated_at = ?
                   WHERE singleton = 1 AND lease_owner = ?""",
                (utc_now_iso(), self._outbox_global_lease_owner),
            )
            await conn.execute(
                "DELETE FROM workflow_outbox_consumers WHERE consumer_id = ?",
                (self._outbox_consumer_id,),
            )
            await conn.commit()
        self._outbox_registered = False

    async def drain_outbox(self, *, batch_size: int = 500) -> int:
        """Drain this consumer's recovery/window/new rows without a batch ceiling."""

        if batch_size <= 0:
            raise ValueError("outbox drain batch_size must be positive")
        total = 0
        while True:
            cursor_before = self._outbox_cursor
            published = await self.flush_outbox(limit=batch_size)
            total += published
            if self._outbox_cursor > cursor_before or self._outbox_scan_saturated:
                continue
            if published < batch_size:
                return total

    async def create_run(
        self,
        body: WorkflowRunCreate,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        attempt: int = 1,
    ) -> WorkflowRunRecord:
        try:
            async with self._conn() as conn:
                record, _created = await self.create_run_in_transaction(
                    conn,
                    body,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    attempt=attempt,
                )
                await conn.commit()
        except aiosqlite.IntegrityError:
            # The permanent unique index is authoritative. A concurrent caller
            # may win after our optimistic pre-check; return that run regardless
            # of whether it already reached a terminal state.
            if body.idempotency_key:
                existing = await self.get_by_idempotency(body.idempotency_key)
                if existing is not None:
                    return existing
            if body.active_key:
                active = await self.get_active_by_active_key(body.active_key)
                if active is not None:
                    return active
            raise
        await self.flush_outbox()
        return await self.get_run(record.run_id) or record

    async def create_run_in_transaction(
        self,
        conn: aiosqlite.Connection,
        body: WorkflowRunCreate,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        attempt: int = 1,
    ) -> tuple[WorkflowRunRecord, bool]:
        """Create a run on a caller-owned SQLite transaction.

        The caller owns commit/rollback and must flush the outbox only after a
        successful commit. This is the Unit-of-Work boundary used when a domain
        authority and its workflow projection must become durable together.
        """

        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        if body.idempotency_key:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                   ORDER BY created_at ASC LIMIT 1""",
                (tenant_id, owner_id, body.idempotency_key),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                return _row_to_run(row), False
        if body.active_key:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ? AND active_key = ?
                     AND state IN ('pending', 'running', 'cancel_requested')
                   ORDER BY created_at ASC LIMIT 1""",
                (tenant_id, owner_id, body.active_key),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                return _row_to_run(row), False
        deadline_at = (
            (datetime.now(UTC) + timedelta(seconds=body.timeout_s)).isoformat()
            if body.timeout_s is not None
            else None
        )
        record = WorkflowRunRecord(
            run_id=run_id or new_workflow_run_id(),
            kind=body.kind,
            source=body.source,
            state="pending",
            title=body.title,
            intent_text=body.intent_text,
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            agent_task_id=body.agent_task_id,
            input=dict(body.input),
            output={},
            timeout_s=body.timeout_s,
            idempotency_key=body.idempotency_key,
            active_key=body.active_key,
            attempt=attempt,
            parent_run_id=parent_run_id,
            deadline_at=deadline_at,
            created_at=now,
            updated_at=now,
        )
        await conn.execute(
            """INSERT INTO workflow_runs
                   (run_id, kind, source, state, title, intent_text, meeting_id, todo_id,
                    agent_task_id, input_json, output_json, error, timeout_s, created_at,
                    started_at, finished_at, updated_at, tenant_id, device_id, owner_id,
                    revision, idempotency_key, active_key, attempt, parent_run_id, deadline_at,
                    cancel_requested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, ?, ?, NULL, NULL, ?,
                           ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL)""",
            (
                record.run_id,
                record.kind,
                record.source,
                record.state,
                record.title,
                record.intent_text,
                record.meeting_id,
                record.todo_id,
                record.agent_task_id,
                json.dumps(record.input, ensure_ascii=False),
                record.timeout_s,
                record.created_at,
                record.updated_at,
                tenant_id,
                device_id,
                owner_id,
                record.idempotency_key,
                record.active_key,
                record.attempt,
                record.parent_run_id,
                record.deadline_at,
            ),
        )
        await self._append_event_tx(
            conn,
            record,
            "workflow.created",
            message=record.title or record.intent_text[:120],
            payload={"kind": record.kind, "source": record.source},
            visibility="debug",
            tenant_id=tenant_id,
            device_id=device_id,
            owner_id=owner_id,
        )
        return record, True

    async def get_active_by_idempotency(self, key: str) -> WorkflowRunRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                     AND state IN ('pending', 'running', 'cancel_requested')
                   ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, owner_id, key),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_run(row) if row else None

    async def get_by_idempotency(self, key: str) -> WorkflowRunRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                   ORDER BY created_at ASC LIMIT 1""",
                (tenant_id, owner_id, key),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_run(row) if row else None

    async def get_active_by_active_key(self, key: str) -> WorkflowRunRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ? AND active_key = ?
                     AND state IN ('pending', 'running', 'cancel_requested')
                   ORDER BY created_at ASC LIMIT 1""",
                (tenant_id, owner_id, key),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_run(row) if row else None

    async def get_run(self, run_id: str) -> WorkflowRunRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_run(row) if row else None

    async def list_runs(
        self,
        *,
        meeting_id: str | None = None,
        todo_id: str | None = None,
        agent_task_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[WorkflowRunRecord]:
        tenant_id, _device_id, owner_id = _scope()
        clauses: list[str] = ["tenant_id = ?", "owner_id = ?"]
        args: list[Any] = [tenant_id, owner_id]
        if meeting_id:
            clauses.append("meeting_id = ?")
            args.append(meeting_id)
        if todo_id:
            clauses.append("todo_id = ?")
            args.append(todo_id)
        if agent_task_id:
            clauses.append("agent_task_id = ?")
            args.append(agent_task_id)
        if state:
            clauses.append("state = ?")
            args.append(state)
        sql = "SELECT * FROM workflow_runs WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        async with self._conn() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run(row) for row in rows]

    async def list_unfinished_principals(self) -> list[Principal]:
        """Return server-authored scopes that own resumable workflow runs.

        This is deliberately an internal recovery primitive, not an HTTP query:
        request APIs remain constrained by ``current_principal``.  Startup needs
        the persisted scope so each dispatcher task captures the same principal
        that originally created the run instead of accidentally replaying all
        work as the local owner.
        """

        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT tenant_id, device_id, owner_id
                   FROM workflow_runs
                   WHERE state IN ('pending', 'running', 'cancel_requested')
                   GROUP BY tenant_id, device_id, owner_id
                   ORDER BY tenant_id, owner_id, device_id"""
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            Principal(
                tenant_id=str(row["tenant_id"]),
                device_id=str(row["device_id"]),
                owner_id=str(row["owner_id"]),
                session_id=f"workflow-restore:{row['owner_id']}",
                mode="local" if row["owner_id"] == LEGACY_OWNER_ID else "public",
            )
            for row in rows
        ]

    async def list_unfinished_runs(self) -> list[WorkflowRunRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE tenant_id = ? AND owner_id = ?
                     AND state IN ('pending', 'running', 'cancel_requested')
                   ORDER BY created_at ASC, run_id ASC""",
                (tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run(row) for row in rows]

    async def list_events(self, run_id: str, *, after_seq: int = 0) -> list[WorkflowEventRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_events
                   WHERE run_id = ? AND seq > ? AND tenant_id = ? AND owner_id = ?
                   ORDER BY seq ASC""",
                (run_id, after_seq, tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_event(row) for row in rows]

    async def record_event(
        self,
        run_id: str,
        event_type: str,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: WorkflowVisibility = "user",
        lease: LeaseToken | None = None,
    ) -> WorkflowEventRecord | None:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock, self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await self._assert_workflow_lease(conn, run_id, lease)
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            run_row = await cur.fetchone()
            await cur.close()
            if run_row is None:
                return None
            run = _row_to_run(run_row)
            event = await self._append_event_tx(
                conn,
                run,
                event_type,
                message=message,
                payload=dict(payload or {}),
                visibility=visibility,
                tenant_id=tenant_id,
                device_id=device_id,
                owner_id=owner_id,
            )
            await conn.commit()
        await self.flush_outbox()
        return event

    async def _set_state(
        self,
        run_id: str,
        state: WorkflowState,
        *,
        event_type: str,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        finished: bool = False,
        domain_writer: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        domain_events: list[EchoEvent] | None = None,
        lease: LeaseToken | None = None,
    ) -> WorkflowRunRecord | None:
        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await self._assert_workflow_lease(conn, run_id, lease)
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None
            rec = _row_to_run(row)
            if state == rec.state:
                return rec
            if state not in _LEGAL_TRANSITIONS.get(rec.state, frozenset()):
                raise InvalidWorkflowTransition(
                    f"illegal workflow transition: {rec.state} -> {state}"
                )
            if domain_writer is not None:
                await domain_writer(conn)
            started_at = now if started and rec.started_at is None else rec.started_at
            finished_at = now if finished else rec.finished_at
            next_output = rec.output if output is None else output
            cancel_requested_at = now if state == "cancel_requested" else rec.cancel_requested_at
            changed = await conn.execute(
                """UPDATE workflow_runs
                   SET state = ?, output_json = ?, error = ?, started_at = ?,
                       finished_at = ?, updated_at = ?, revision = revision + 1,
                       cancel_requested_at = ?
                   WHERE run_id = ? AND tenant_id = ? AND owner_id = ? AND revision = ?""",
                (
                    state,
                    json.dumps(next_output, ensure_ascii=False),
                    error,
                    started_at,
                    finished_at,
                    now,
                    cancel_requested_at,
                    run_id,
                    tenant_id,
                    owner_id,
                    rec.revision,
                ),
            )
            if changed.rowcount != 1:
                raise WorkflowConflictError(f"workflow revision conflict: {run_id}")
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            updated_row = await cur.fetchone()
            await cur.close()
            if updated_row is None:
                raise WorkflowConflictError(f"workflow disappeared during update: {run_id}")
            updated = _row_to_run(updated_row)
            await self._append_event_tx(
                conn,
                updated,
                event_type,
                message=message,
                payload=payload or {},
                visibility="user",
                tenant_id=tenant_id,
                device_id=device_id,
                owner_id=owner_id,
            )
            for domain_event in domain_events or []:
                await self._append_domain_outbox_tx(
                    conn,
                    domain_event,
                    aggregate_id=run_id,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    owner_id=owner_id,
                )
            await conn.commit()
        await self.flush_outbox()
        return updated

    async def start_run(self, run_id: str) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "running",
            event_type="workflow.started",
            message="任务开始执行",
            started=True,
        )

    async def complete_run(
        self,
        run_id: str,
        *,
        output: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "succeeded",
            event_type="workflow.succeeded",
            message=message or "任务完成",
            output=output or {},
            finished=True,
        )

    async def complete_run_atomic(
        self,
        run_id: str,
        *,
        output: dict[str, Any],
        domain_writer: Callable[[aiosqlite.Connection], Awaitable[None]],
        domain_events: list[EchoEvent],
        message: str = "任务完成",
    ) -> WorkflowRunRecord | None:
        """Commit domain projection, terminal run/event and all WS events as one UoW."""

        return await self._set_state(
            run_id,
            "succeeded",
            event_type="workflow.succeeded",
            message=message,
            output=output,
            finished=True,
            domain_writer=domain_writer,
            domain_events=domain_events,
        )

    async def merge_output(
        self,
        run_id: str,
        patch: dict[str, Any],
        *,
        event_type: str = "workflow.output_updated",
        message: str | None = None,
        lease: LeaseToken | None = None,
    ) -> WorkflowRunRecord | None:
        """Merge late projection data without reopening or re-transitioning a run."""
        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await self._assert_workflow_lease(conn, run_id, lease)
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None
            rec = _row_to_run(row)
            output = {**rec.output, **patch}
            changed = await conn.execute(
                """UPDATE workflow_runs
                   SET output_json = ?, updated_at = ?, revision = revision + 1
                   WHERE run_id = ? AND tenant_id = ? AND owner_id = ? AND revision = ?""",
                (
                    json.dumps(output, ensure_ascii=False),
                    now,
                    run_id,
                    tenant_id,
                    owner_id,
                    rec.revision,
                ),
            )
            if changed.rowcount != 1:
                raise WorkflowConflictError(f"workflow revision conflict: {run_id}")
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
                (run_id, tenant_id, owner_id),
            )
            updated_row = await cur.fetchone()
            await cur.close()
            if updated_row is None:
                raise WorkflowConflictError(f"workflow disappeared during update: {run_id}")
            updated = _row_to_run(updated_row)
            await self._append_event_tx(
                conn,
                updated,
                event_type,
                message=message,
                payload={"output": patch},
                visibility="debug",
                tenant_id=tenant_id,
                device_id=device_id,
                owner_id=owner_id,
            )
            await conn.commit()
        await self.flush_outbox()
        return updated

    async def fail_run(
        self,
        run_id: str,
        *,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "failed",
            event_type="workflow.failed",
            message=error,
            payload=payload or {"error": error},
            error=error,
            finished=True,
        )

    async def fail_run_atomic(
        self,
        run_id: str,
        *,
        error: str,
        domain_events: list[EchoEvent],
        domain_writer: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "failed",
            event_type="workflow.failed",
            message=error,
            payload=payload or {"error": error},
            error=error,
            finished=True,
            domain_writer=domain_writer,
            domain_events=domain_events,
        )

    async def timeout_run(
        self, run_id: str, *, error: str = "workflow timeout"
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "timeout",
            event_type="workflow.timeout",
            message=error,
            payload={"error": error},
            error=error,
            finished=True,
        )

    async def request_cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancel_requested",
            event_type="workflow.cancel_requested",
            message=reason or "已请求取消",
            payload={"reason": reason} if reason else {},
        )

    async def request_cancel_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> WorkflowRunRecord | None:
        """Persist cancellation on a caller-owned Unit of Work without flushing."""

        tenant_id, device_id, owner_id = _scope()
        await self._assert_workflow_lease(conn, run_id)
        cur = await conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
            (run_id, tenant_id, owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        current = _row_to_run(row)
        if current.state == "cancel_requested":
            return current
        if "cancel_requested" not in _LEGAL_TRANSITIONS.get(current.state, frozenset()):
            raise InvalidWorkflowTransition(
                f"illegal workflow transition: {current.state} -> cancel_requested"
            )
        now = utc_now_iso()
        changed = await conn.execute(
            """UPDATE workflow_runs
               SET state = 'cancel_requested', updated_at = ?, revision = revision + 1,
                   cancel_requested_at = COALESCE(cancel_requested_at, ?)
               WHERE run_id = ? AND tenant_id = ? AND owner_id = ? AND revision = ?""",
            (now, now, run_id, tenant_id, owner_id, current.revision),
        )
        if changed.rowcount != 1:
            raise WorkflowConflictError(f"workflow revision conflict: {run_id}")
        current.state = "cancel_requested"
        current.updated_at = now
        current.cancel_requested_at = current.cancel_requested_at or now
        current.revision += 1
        await self._append_event_tx(
            conn,
            current,
            "workflow.cancel_requested",
            message=reason or "已请求取消",
            payload={"reason": reason} if reason else {},
            visibility="user",
            tenant_id=tenant_id,
            device_id=device_id,
            owner_id=owner_id,
        )
        return current

    async def settle_agent_terminal_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        state: WorkflowState,
        message: str | None = None,
        output: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowRunRecord | None:
        """Atomically arbitrate a linked Agent terminal state with Workflow.

        A pre-existing Workflow terminal state wins. Otherwise this writes the
        legal intermediate state, terminal snapshot, event, and outbox on the
        caller-owned transaction before the Agent row is made terminal.
        """

        if state not in TERMINAL_WORKFLOW_STATES:
            raise ValueError("Agent terminal arbitration requires a terminal state")
        tenant_id, _device_id, owner_id = _scope()
        await self._assert_workflow_lease(conn, run_id)
        cur = await conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
            (run_id, tenant_id, owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        current = _row_to_run(row)
        if current.is_terminal:
            return current

        if state == "succeeded" and current.state == "pending":
            current = await self._transition_agent_run_in_transaction(
                conn,
                current,
                state="running",
                event_type="workflow.started",
                message="任务开始执行",
                payload={},
                started=True,
            )
        if state in {"cancelled", "cancel_failed"} and current.state != "cancel_requested":
            requested = await self.request_cancel_in_transaction(
                conn,
                run_id,
                reason="Agent Runner 已返回取消终态",
            )
            if requested is None:
                return None
            current = requested

        event_type = {
            "succeeded": "workflow.succeeded",
            "failed": "workflow.failed",
            "timeout": "workflow.timeout",
            "cancelled": "workflow.cancelled",
            "cancel_failed": "workflow.cancel_failed",
        }[state]
        default_message = {
            "succeeded": "任务完成",
            "failed": "任务失败",
            "timeout": "任务超时",
            "cancelled": "任务已取消",
            "cancel_failed": "取消失败",
        }[state]
        return await self._transition_agent_run_in_transaction(
            conn,
            current,
            state=state,
            event_type=event_type,
            message=message or default_message,
            payload=payload or {},
            output=output,
            error=(message or default_message)
            if state in {"failed", "timeout", "cancel_failed"}
            else None,
            finished=True,
        )

    async def _transition_agent_run_in_transaction(
        self,
        conn: aiosqlite.Connection,
        current: WorkflowRunRecord,
        *,
        state: WorkflowState,
        event_type: str,
        message: str,
        payload: dict[str, Any],
        output: dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> WorkflowRunRecord:
        if state not in _LEGAL_TRANSITIONS.get(current.state, frozenset()):
            raise InvalidWorkflowTransition(
                f"illegal workflow transition: {current.state} -> {state}"
            )
        now = utc_now_iso()
        tenant_id, device_id, owner_id = _scope()
        changed = await conn.execute(
            """UPDATE workflow_runs
               SET state = ?, output_json = ?, error = ?,
                   started_at = ?, finished_at = ?, updated_at = ?,
                   revision = revision + 1,
                   cancel_requested_at = ?
               WHERE run_id = ? AND tenant_id = ? AND owner_id = ? AND revision = ?""",
            (
                state,
                json.dumps(current.output if output is None else output, ensure_ascii=False),
                error,
                now if started and current.started_at is None else current.started_at,
                now if finished else current.finished_at,
                now,
                now if state == "cancel_requested" else current.cancel_requested_at,
                current.run_id,
                tenant_id,
                owner_id,
                current.revision,
            ),
        )
        if changed.rowcount != 1:
            raise WorkflowConflictError(f"workflow revision conflict: {current.run_id}")
        await changed.close()
        cur = await conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ? AND tenant_id = ? AND owner_id = ?",
            (current.run_id, tenant_id, owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise WorkflowConflictError(f"workflow disappeared during update: {current.run_id}")
        updated = _row_to_run(row)
        await self._append_event_tx(
            conn,
            updated,
            event_type,
            message=message,
            payload=payload,
            visibility="user",
            tenant_id=tenant_id,
            device_id=device_id,
            owner_id=owner_id,
        )
        return updated

    async def mark_cancelled(
        self,
        run_id: str,
        *,
        message: str = "任务已取消",
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancelled",
            event_type="workflow.cancelled",
            message=message,
            finished=True,
        )

    async def mark_cancel_failed(
        self,
        run_id: str,
        *,
        error: str,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancel_failed",
            event_type="workflow.cancel_failed",
            message=error,
            payload={"error": error},
            error=error,
            finished=True,
        )

    async def retry_run(
        self,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> WorkflowRunRecord | None:
        old = await self.get_run(run_id)
        if old is None:
            return None
        if not old.is_terminal:
            raise InvalidWorkflowTransition(
                f"cannot retry non-terminal workflow in state {old.state}"
            )
        if old.state == "succeeded":
            raise InvalidWorkflowTransition("cannot retry a succeeded workflow")
        retry_input = dict(old.input)
        retry_input["retry_of"] = old.run_id
        if reason:
            retry_input["retry_reason"] = reason
        new_run = await self.create_run(
            WorkflowRunCreate(
                kind=old.kind,
                source=old.source,
                title=old.title,
                intent_text=old.intent_text,
                meeting_id=old.meeting_id,
                todo_id=old.todo_id,
                agent_task_id=old.agent_task_id,
                input=retry_input,
                timeout_s=old.timeout_s,
                idempotency_key=(
                    f"{old.idempotency_key}:retry:{old.attempt + 1}"
                    if old.idempotency_key
                    else None
                ),
            ),
            parent_run_id=old.run_id,
            attempt=old.attempt + 1,
        )
        await self.record_event(
            old.run_id,
            "workflow.retry_created",
            message="已创建重试任务",
            payload={"retry_run_id": new_run.run_id, "reason": reason},
            visibility="debug",
        )
        return new_run

    async def restore_unfinished(self) -> int:
        await self.drain_outbox()
        runs = await self.list_unfinished_runs()
        count = 0
        for rec in runs:
            await self.record_event(
                rec.run_id,
                "workflow.restored",
                message="任务已从本地历史恢复",
                payload={"state": rec.state},
                visibility="debug",
            )
            count += 1
        return count


_service: WorkflowService | None = None


def get_workflow_service(settings: Settings, event_bus: InMemoryEventBus) -> WorkflowService:
    global _service  # noqa: PLW0603
    if _service is None:
        _service = WorkflowService(settings, event_bus)
    return _service


def reset_workflow_service_for_test() -> None:
    global _service  # noqa: PLW0603
    _service = None
