"""Durable, scope-safe command outbox for remote Agent side effects."""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.agents.events import utc_now_iso
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseToken

AgentCommandOutcome = Literal["cancelled", "cancel_failed", "terminal_won"]


@dataclass(frozen=True, slots=True)
class AgentCommandRecord:
    command_id: str
    tenant_id: str
    owner_id: str
    device_id: str
    task_id: str
    runner_task_id: str | None
    command_type: str
    operation_key: str
    attempts: int
    next_attempt_at: float
    last_error: str | None
    outcome: str | None
    force_remote: bool
    created_at: str
    updated_at: str
    completed_at: str | None


def cancel_operation_key(*, tenant_id: str, owner_id: str, task_id: str) -> str:
    """Return a stable, opaque key shared by every replay of one cancellation."""

    material = f"v1\0{tenant_id}\0{owner_id}\0{task_id}\0cancel".encode()
    return f"agent-cancel-{hashlib.sha256(material).hexdigest()}"


def _row_to_command(row: aiosqlite.Row) -> AgentCommandRecord:
    return AgentCommandRecord(
        command_id=str(row["command_id"]),
        tenant_id=str(row["tenant_id"]),
        owner_id=str(row["owner_id"]),
        device_id=str(row["device_id"]),
        task_id=str(row["task_id"]),
        runner_task_id=row["runner_task_id"],
        command_type=str(row["command_type"]),
        operation_key=str(row["operation_key"]),
        attempts=int(row["attempts"]),
        next_attempt_at=float(row["next_attempt_at"]),
        last_error=row["last_error"],
        outcome=row["outcome"],
        force_remote=bool(row["force_remote"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=row["completed_at"],
    )


class AgentCommandOutbox:
    """Persist and fence remote commands without owning their business policy."""

    def __init__(self, db_path: Path | str, lease_store: ExecutionLeaseStore) -> None:
        self._db_path = Path(db_path).expanduser()
        self._lease_store = lease_store

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    async def enqueue_cancel_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        owner_id: str,
        device_id: str,
        task_id: str,
        runner_task_id: str | None,
    ) -> AgentCommandRecord:
        operation_key = cancel_operation_key(
            tenant_id=tenant_id,
            owner_id=owner_id,
            task_id=task_id,
        )
        command_id = f"agent_cmd_{operation_key.removeprefix('agent-cancel-')[:32]}"
        now = utc_now_iso()
        await conn.execute(
            """INSERT INTO agent_command_outbox
               (command_id, tenant_id, owner_id, device_id, task_id, runner_task_id,
                command_type, operation_key, attempts, next_attempt_at, last_error,
                outcome, force_remote, created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'cancel', ?, 0, 0, NULL, NULL, 0, ?, ?, NULL)
               ON CONFLICT (tenant_id, owner_id, task_id, command_type)
               DO UPDATE SET
                   runner_task_id = COALESCE(
                       agent_command_outbox.runner_task_id,
                       excluded.runner_task_id
                   ),
                   updated_at = excluded.updated_at
               WHERE agent_command_outbox.completed_at IS NULL""",
            (
                command_id,
                tenant_id,
                owner_id,
                device_id,
                task_id,
                runner_task_id,
                operation_key,
                now,
                now,
            ),
        )
        row = await self._select_task_command(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            task_id=task_id,
        )
        if row is None:
            raise RuntimeError("cancel command enqueue did not produce a durable row")
        return _row_to_command(row)

    async def attach_runner_and_requeue_cancel_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        runner_task_id: str,
    ) -> bool:
        """Attach a late submit result without reopening the local task state."""

        cur = await conn.execute(
            """UPDATE agent_command_outbox
               SET runner_task_id = ?, attempts = 0, next_attempt_at = 0,
                   last_error = NULL, outcome = NULL, force_remote = 1,
                   completed_at = NULL, updated_at = ?
               WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                 AND command_type = 'cancel'""",
            (
                runner_task_id,
                utc_now_iso(),
                tenant_id,
                owner_id,
                task_id,
            ),
        )
        changed = cur.rowcount == 1
        await cur.close()
        return changed

    async def list_due(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str | None = None,
        limit: int = 100,
        now_epoch: float | None = None,
    ) -> list[AgentCommandRecord]:
        if limit < 1:
            raise ValueError("command outbox limit must be positive")
        clauses = [
            "tenant_id = ?",
            "owner_id = ?",
            "completed_at IS NULL",
            "next_attempt_at <= ?",
        ]
        args: list[object] = [tenant_id, owner_id, now_epoch or time.time()]
        if task_id is not None:
            clauses.append("task_id = ?")
            args.append(task_id)
        args.append(limit)
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_command_outbox WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC, command_id ASC LIMIT ?",
                args,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_command(row) for row in rows]

    async def mark_retry(
        self,
        command: AgentCommandRecord,
        lease: LeaseToken,
        *,
        next_attempt_at: float,
        error: str,
    ) -> AgentCommandRecord:
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await self._lease_store.assert_owned(lease, conn=conn)
            await conn.execute(
                """UPDATE agent_command_outbox
                   SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?,
                       updated_at = ?
                   WHERE tenant_id = ? AND owner_id = ? AND command_id = ?
                     AND completed_at IS NULL""",
                (
                    next_attempt_at,
                    error,
                    utc_now_iso(),
                    command.tenant_id,
                    command.owner_id,
                    command.command_id,
                ),
            )
            row = await self._select_command(conn, command)
            await conn.commit()
        if row is None:
            raise RuntimeError("cancel command disappeared during retry")
        return _row_to_command(row)

    async def mark_completed(
        self,
        command: AgentCommandRecord,
        lease: LeaseToken,
        *,
        outcome: AgentCommandOutcome,
    ) -> bool:
        now = utc_now_iso()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await self._lease_store.assert_owned(lease, conn=conn)
            changed = await self.mark_completed_in_transaction(
                conn,
                command,
                outcome=outcome,
                completed_at=now,
            )
            await conn.commit()
        return changed

    async def mark_completed_in_transaction(
        self,
        conn: aiosqlite.Connection,
        command: AgentCommandRecord,
        *,
        outcome: AgentCommandOutcome,
        completed_at: str | None = None,
    ) -> bool:
        now = completed_at or utc_now_iso()
        cur = await conn.execute(
            """UPDATE agent_command_outbox
               SET outcome = ?, completed_at = ?, updated_at = ?, last_error = NULL,
                   force_remote = 0
               WHERE tenant_id = ? AND owner_id = ? AND command_id = ?
                 AND completed_at IS NULL""",
            (
                outcome,
                now,
                now,
                command.tenant_id,
                command.owner_id,
                command.command_id,
            ),
        )
        changed = cur.rowcount == 1
        await cur.close()
        return changed

    @staticmethod
    async def _select_task_command(
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
    ) -> aiosqlite.Row | None:
        cur = await conn.execute(
            """SELECT * FROM agent_command_outbox
               WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                 AND command_type = 'cancel'""",
            (tenant_id, owner_id, task_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return row

    @staticmethod
    async def _select_command(
        conn: aiosqlite.Connection,
        command: AgentCommandRecord,
    ) -> aiosqlite.Row | None:
        cur = await conn.execute(
            """SELECT * FROM agent_command_outbox
               WHERE tenant_id = ? AND owner_id = ? AND command_id = ?""",
            (command.tenant_id, command.owner_id, command.command_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return row
