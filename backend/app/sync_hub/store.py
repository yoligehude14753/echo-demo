"""Durable pairing and device state for the sync hub."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.security.models import Principal


class SyncHubError(RuntimeError):
    """Base class for sync hub storage failures."""


class PairingNotFoundError(SyncHubError):
    """Pairing code is unknown, expired, or already claimed."""


class DeviceAlreadyExistsError(SyncHubError):
    """The claimed device id is already owned by the user."""


class SyncDeviceNotFoundError(SyncHubError):
    """The requested sync device is not owned by the current user."""


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


class SyncHubStore:
    """SQLite adapter for pairing and sync-device lifecycle state."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self._now = now

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
                    _parse_datetime(row["revoked_at"])
                    if row["revoked_at"] is not None
                    else None
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
    "SyncDeviceNotFoundError",
    "SyncDeviceRecord",
    "SyncHubStore",
]
