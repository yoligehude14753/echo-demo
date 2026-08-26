"""SQLite-backed, scope-safe execution leases with fencing tokens."""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """Proof that one holder owns a scoped resource for a bounded term."""

    tenant_id: str
    owner_id: str
    resource_kind: str
    resource_id: str
    holder_id: str
    fence_token: int
    expires_at: float
    heartbeat_at: float


class LeaseOwnershipError(RuntimeError):
    """Raised when a lease token no longer owns its resource."""


class ExecutionLeaseStore:
    """Coordinate single-owner execution across processes through SQLite.

    Mutations on store-owned connections use ``BEGIN IMMEDIATE`` and commit
    atomically.  A supplied connection is never committed by this store.  If
    that connection has no transaction yet, a mutation starts an immediate
    transaction and deliberately leaves it open so the caller can perform a
    fenced state mutation before committing.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = Path(db_path).expanduser()
        self._clock = clock

    async def acquire(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        resource_kind: str,
        resource_id: str,
        holder_id: str,
        ttl_seconds: float,
        conn: aiosqlite.Connection | None = None,
    ) -> LeaseToken | None:
        """Acquire a fresh fenced term, or return ``None`` while another holder owns it."""

        self._validate_identity(tenant_id, owner_id, resource_kind, resource_id, holder_id)
        now, expires_at = self._term(ttl_seconds)
        async with self._write_connection(conn) as active_conn:
            cursor = await active_conn.execute(
                """INSERT INTO execution_leases
                   (tenant_id, owner_id, resource_kind, resource_id, holder_id,
                    fence_token, expires_at, heartbeat_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT (tenant_id, owner_id, resource_kind, resource_id)
                   DO UPDATE SET
                       holder_id = excluded.holder_id,
                       fence_token = execution_leases.fence_token + 1,
                       expires_at = excluded.expires_at,
                       heartbeat_at = excluded.heartbeat_at
                   WHERE execution_leases.expires_at <= excluded.heartbeat_at
                   RETURNING tenant_id, owner_id, resource_kind, resource_id, holder_id,
                             fence_token, expires_at, heartbeat_at""",
                (
                    tenant_id,
                    owner_id,
                    resource_kind,
                    resource_id,
                    holder_id,
                    expires_at,
                    now,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._row_to_token(row)

    async def renew(
        self,
        token: LeaseToken,
        *,
        ttl_seconds: float,
        conn: aiosqlite.Connection | None = None,
    ) -> LeaseToken | None:
        """Extend a live term without changing its fence; expired terms cannot revive."""

        now, expires_at = self._term(ttl_seconds)
        async with self._write_connection(conn) as active_conn:
            cursor = await active_conn.execute(
                """UPDATE execution_leases
                   SET expires_at = ?, heartbeat_at = ?
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = ? AND resource_id = ?
                     AND holder_id = ? AND fence_token = ?
                     AND expires_at > ?
                   RETURNING tenant_id, owner_id, resource_kind, resource_id, holder_id,
                             fence_token, expires_at, heartbeat_at""",
                (
                    expires_at,
                    now,
                    token.tenant_id,
                    token.owner_id,
                    token.resource_kind,
                    token.resource_id,
                    token.holder_id,
                    token.fence_token,
                    now,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._row_to_token(row)

    async def release(
        self,
        token: LeaseToken,
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> bool:
        """Release only the exact holder and fenced term represented by ``token``."""

        async with self._write_connection(conn) as active_conn:
            now = self._now()
            cursor = await active_conn.execute(
                """UPDATE execution_leases
                   SET expires_at = 0, heartbeat_at = ?
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = ? AND resource_id = ?
                     AND holder_id = ? AND fence_token = ?
                     AND expires_at > ?""",
                (now, *self._token_key(token), now),
            )
            released = cursor.rowcount == 1
            await cursor.close()
        return released

    async def check_owned(
        self,
        token: LeaseToken,
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> bool:
        """Return whether ``token`` is still the live fenced owner at read time."""

        now = self._now()
        async with self._read_connection(conn) as active_conn:
            cursor = await active_conn.execute(
                """SELECT 1 FROM execution_leases
                   WHERE tenant_id = ? AND owner_id = ?
                     AND resource_kind = ? AND resource_id = ?
                     AND holder_id = ? AND fence_token = ?
                     AND expires_at > ?""",
                (*self._token_key(token), now),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def assert_owned(
        self,
        token: LeaseToken,
        *,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        """Raise :class:`LeaseOwnershipError` unless ``token`` still owns the resource."""

        if not await self.check_owned(token, conn=conn):
            raise LeaseOwnershipError(
                "execution lease is absent, expired, released, or superseded: "
                f"{token.resource_kind}/{token.resource_id} fence={token.fence_token}"
            )

    @asynccontextmanager
    async def _write_connection(
        self,
        conn: aiosqlite.Connection | None,
    ) -> AsyncIterator[aiosqlite.Connection]:
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            async with open_aiosqlite_connection(self._db_path) as owned_conn:
                await configure_aiosqlite_connection(owned_conn)
                await owned_conn.execute("BEGIN IMMEDIATE")
                try:
                    yield owned_conn
                except BaseException:
                    await owned_conn.rollback()
                    raise
                else:
                    await owned_conn.commit()
            return

        await configure_aiosqlite_connection(conn)
        started_transaction = not conn.in_transaction
        if started_transaction:
            await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if started_transaction:
                await conn.rollback()
            raise

    @asynccontextmanager
    async def _read_connection(
        self,
        conn: aiosqlite.Connection | None,
    ) -> AsyncIterator[aiosqlite.Connection]:
        if conn is not None:
            await configure_aiosqlite_connection(conn)
            yield conn
            return
        async with open_aiosqlite_connection(self._db_path) as owned_conn:
            await configure_aiosqlite_connection(owned_conn)
            yield owned_conn

    def _term(self, ttl_seconds: float) -> tuple[float, float]:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")
        now = self._now()
        expires_at = now + ttl_seconds
        if not math.isfinite(expires_at):
            raise ValueError("lease expiration must be finite")
        return now, expires_at

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite non-negative epoch value")
        return now

    @staticmethod
    def _validate_identity(*values: str) -> None:
        if any(not value for value in values):
            raise ValueError("lease scope, resource, and holder identifiers must be non-empty")

    @staticmethod
    def _token_key(token: LeaseToken) -> tuple[str, str, str, str, str, int]:
        return (
            token.tenant_id,
            token.owner_id,
            token.resource_kind,
            token.resource_id,
            token.holder_id,
            token.fence_token,
        )

    @staticmethod
    def _row_to_token(row: aiosqlite.Row | tuple[Any, ...] | None) -> LeaseToken | None:
        if row is None:
            return None
        return LeaseToken(
            tenant_id=str(row[0]),
            owner_id=str(row[1]),
            resource_kind=str(row[2]),
            resource_id=str(row[3]),
            holder_id=str(row[4]),
            fence_token=int(row[5]),
            expires_at=float(row[6]),
            heartbeat_at=float(row[7]),
        )


__all__ = ["ExecutionLeaseStore", "LeaseOwnershipError", "LeaseToken"]
