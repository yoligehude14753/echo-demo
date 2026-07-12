"""Durable device identity, rotating access sessions, and resource tickets."""

from __future__ import annotations

import hashlib
import hmac
import math
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
from app.security.models import (
    IssuedDeviceIdentity,
    IssuedSession,
    Principal,
    local_principal,
)


class SessionError(RuntimeError):
    """Session or device identity validation failed."""


class InvalidSessionError(SessionError):
    """Access token is missing, forged, or unknown."""


class ExpiredSessionError(SessionError):
    """Access token has expired."""


class RevokedSessionError(SessionError):
    """Access token, family, device, user, or tenant is revoked/suspended."""


class DeviceCredentialError(SessionError):
    """Device credential validation failed."""


class InvalidDeviceCredentialError(DeviceCredentialError):
    """Device credential is missing, forged, or unknown."""


class ExpiredDeviceCredentialError(DeviceCredentialError):
    """Device credential has expired."""


class RevokedDeviceCredentialError(DeviceCredentialError):
    """Device credential or its owning identity is revoked."""


class DeviceIdentityAlreadyClaimedError(DeviceCredentialError):
    """A migrated legacy identity already owns a durable credential."""


class IdentityAlreadyEnrolledError(DeviceCredentialError):
    """A stable installation enrollment id has already been consumed."""


class EnrollmentAdmissionLimitError(SessionError):
    """Durable new-identity admission rejected an enrollment."""

    def __init__(self, reason: str, *, retry_after_s: int) -> None:
        super().__init__("public enrollment admission limit exceeded")
        self.reason = reason
        self.retry_after_s = max(1, retry_after_s)


class ResourceTicketError(SessionError):
    """Resource ticket is invalid, expired, or bound to another resource."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _binding_hash(value: str, *, domain: str) -> str:
    return hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True, slots=True)
class EnrollmentAdmissionPolicy:
    window_s: float = 60 * 60
    peer_max_per_window: int = 12
    global_max_per_window: int = 1_000
    peer_max_per_day: int = 64
    global_max_per_day: int = 10_000
    total_active_max: int = 10_000
    cleanup_batch_size: int = 100

    def __post_init__(self) -> None:
        if (
            self.window_s <= 0
            or self.peer_max_per_window < 1
            or self.global_max_per_window < 1
            or self.peer_max_per_day < 1
            or self.global_max_per_day < 1
            or self.total_active_max < 1
            or self.cleanup_batch_size < 1
        ):
            raise ValueError("enrollment admission bounds must be positive")


_USER_RESOURCE_TABLES = (
    "meetings",
    "ambient_segments",
    "speakers",
    "workflow_runs",
    "workflow_outbox",
    "artifacts",
    "agent_tasks",
    "agent_runner_grants",
    "rag_documents",
    "rag_content_owners",
    "ambient_audio_files",
)


class SessionStore:
    """SQLite-backed stable identity and session-family store.

    Access tokens are deliberately short-lived. A separately rotated device
    credential lets an installed client renew while retaining the exact same
    tenant/user/device tuple. Only SHA-256 token hashes are persisted.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        default_ttl: timedelta = timedelta(hours=1),
        credential_ttl: timedelta = timedelta(days=180),
        admission_policy: EnrollmentAdmissionPolicy | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if default_ttl <= timedelta(0) or default_ttl > timedelta(hours=1):
            raise ValueError("default_ttl must be positive and no longer than one hour")
        if credential_ttl <= timedelta(0):
            raise ValueError("credential_ttl must be positive")
        self._db_path = Path(db_path).expanduser()
        self._default_ttl = default_ttl
        self._credential_ttl = credential_ttl
        self._admission_policy = admission_policy or EnrollmentAdmissionPolicy()
        self._now = now

    @property
    def db_path(self) -> Path:
        return self._db_path

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    async def _insert_session_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        family_id: str,
        generation: int,
        ttl: timedelta,
        renewed_from_session_id: str | None = None,
    ) -> IssuedSession:
        now = _as_utc(self._now())
        expires_at = now + ttl
        token = _new_secret("eds")
        principal = Principal(
            tenant_id=tenant_id,
            device_id=device_id,
            owner_id=user_id,
            session_id=_new_id("session"),
            mode="public",
            family_id=family_id,
        )
        await conn.execute(
            """INSERT INTO principal_sessions
               (session_id, token_hash, tenant_id, device_id, owner_id, mode,
                issued_at, expires_at, revoked_at, family_id, generation,
                renewed_from_session_id)
               VALUES (?, ?, ?, ?, ?, 'public', ?, ?, NULL, ?, ?, ?)""",
            (
                principal.session_id,
                _token_hash(token),
                tenant_id,
                device_id,
                user_id,
                now.isoformat(),
                expires_at.isoformat(),
                family_id,
                generation,
                renewed_from_session_id,
            ),
        )
        return IssuedSession(token=token, principal=principal, expires_at=expires_at.isoformat())

    async def _insert_credential_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        family_id: str,
        ttl: timedelta | None = None,
        credential: str | None = None,
    ) -> tuple[str, str, str]:
        now = _as_utc(self._now())
        expires_at = now + (ttl or self._credential_ttl)
        credential = credential or _new_secret("edc")
        credential_id = _new_id("credential")
        await conn.execute(
            """INSERT INTO device_credentials
               (credential_id, credential_hash, family_id, tenant_id, user_id,
                device_id, issued_at, expires_at, last_used_at, revoked_at,
                rotated_to_credential_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
            (
                credential_id,
                _token_hash(credential),
                family_id,
                tenant_id,
                user_id,
                device_id,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        return credential, credential_id, expires_at.isoformat()

    async def _check_admission_window_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        now_epoch: float,
        duration_s: float,
        limit: int,
        reason: str,
        peer_hash: str | None = None,
    ) -> None:
        cutoff = now_epoch - duration_s
        if peer_hash is None:
            cur = await conn.execute(
                """SELECT COUNT(*), MIN(admitted_at)
                   FROM public_enrollment_admissions WHERE admitted_at > ?""",
                (cutoff,),
            )
        else:
            cur = await conn.execute(
                """SELECT COUNT(*), MIN(admitted_at)
                   FROM public_enrollment_admissions
                   WHERE peer_key_hash = ? AND admitted_at > ?""",
                (peer_hash, cutoff),
            )
        row = await cur.fetchone()
        await cur.close()
        count = int(row[0]) if row is not None else 0
        if count < limit:
            return
        oldest = float(row[1]) if row is not None and row[1] is not None else cutoff
        retry_after = math.ceil(max(1.0, oldest + duration_s - now_epoch))
        raise EnrollmentAdmissionLimitError(reason, retry_after_s=retry_after)

    async def _active_enrollment_count_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        now_iso: str,
    ) -> int:
        cur = await conn.execute(
            """SELECT COUNT(*) FROM public_enrollments AS enrollment
               WHERE EXISTS (
                   SELECT 1 FROM device_credentials AS credential
                   WHERE credential.family_id = enrollment.family_id
                     AND credential.tenant_id = enrollment.tenant_id
                     AND credential.user_id = enrollment.user_id
                     AND credential.device_id = enrollment.device_id
                     AND credential.revoked_at IS NULL
                     AND credential.expires_at > ?
               ) OR EXISTS (
                   SELECT 1 FROM principal_sessions AS session
                   WHERE session.family_id = enrollment.family_id
                     AND session.tenant_id = enrollment.tenant_id
                     AND session.owner_id = enrollment.user_id
                     AND session.device_id = enrollment.device_id
                     AND session.revoked_at IS NULL
                     AND session.expires_at > ?
               )""",
            (now_iso, now_iso),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row is not None else 0

    async def _user_has_resources_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        for table in _USER_RESOURCE_TABLES:
            cur = await conn.execute(
                f"SELECT 1 FROM {table} WHERE tenant_id = ? AND owner_id = ? LIMIT 1",
                (tenant_id, user_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                return True
        return False

    async def _cleanup_orphaned_enrollments_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        policy: EnrollmentAdmissionPolicy,
        now_iso: str,
    ) -> int:
        cur = await conn.execute(
            """SELECT enrollment_id_hash, family_id, tenant_id, user_id, device_id
               FROM public_enrollments AS enrollment
               WHERE NOT EXISTS (
                   SELECT 1 FROM device_credentials AS credential
                   WHERE credential.family_id = enrollment.family_id
                     AND credential.tenant_id = enrollment.tenant_id
                     AND credential.user_id = enrollment.user_id
                     AND credential.device_id = enrollment.device_id
                     AND credential.revoked_at IS NULL
                     AND credential.expires_at > ?
               ) AND NOT EXISTS (
                   SELECT 1 FROM principal_sessions AS session
                   WHERE session.family_id = enrollment.family_id
                     AND session.tenant_id = enrollment.tenant_id
                     AND session.owner_id = enrollment.user_id
                     AND session.device_id = enrollment.device_id
                     AND session.revoked_at IS NULL
                     AND session.expires_at > ?
               )
               ORDER BY enrollment.created_at ASC
               LIMIT ?""",
            (now_iso, now_iso, policy.cleanup_batch_size * 10),
        )
        rows = await cur.fetchall()
        await cur.close()
        removed = 0
        for row in rows:
            if removed >= policy.cleanup_batch_size:
                break
            tenant_id = str(row["tenant_id"])
            user_id = str(row["user_id"])
            if await self._user_has_resources_tx(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
            ):
                continue
            family_id = str(row["family_id"])
            device_id = str(row["device_id"])
            await conn.execute(
                "DELETE FROM principal_sessions WHERE family_id = ?",
                (family_id,),
            )
            await conn.execute(
                """UPDATE device_credentials SET rotated_to_credential_id = NULL
                   WHERE family_id = ?""",
                (family_id,),
            )
            await conn.execute(
                "DELETE FROM device_credentials WHERE family_id = ?",
                (family_id,),
            )
            await conn.execute(
                "DELETE FROM public_enrollments WHERE enrollment_id_hash = ?",
                (row["enrollment_id_hash"],),
            )
            await conn.execute(
                "DELETE FROM session_families WHERE family_id = ?",
                (family_id,),
            )
            await conn.execute(
                """DELETE FROM devices
                   WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
                (tenant_id, user_id, device_id),
            )
            cur = await conn.execute(
                "SELECT 1 FROM devices WHERE tenant_id = ? AND user_id = ? LIMIT 1",
                (tenant_id, user_id),
            )
            has_device = await cur.fetchone()
            await cur.close()
            if has_device is None:
                await conn.execute(
                    "DELETE FROM execution_leases WHERE tenant_id = ? AND owner_id = ?",
                    (tenant_id, user_id),
                )
                await conn.execute(
                    """DELETE FROM principal_quota_ledger
                       WHERE tenant_id = ? AND owner_id = ?""",
                    (tenant_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM users WHERE tenant_id = ? AND user_id = ?",
                    (tenant_id, user_id),
                )
                await conn.execute(
                    """DELETE FROM tenants WHERE tenant_id = ?
                       AND NOT EXISTS (
                           SELECT 1 FROM users WHERE users.tenant_id = tenants.tenant_id
                       )""",
                    (tenant_id,),
                )
            removed += 1
        return removed

    async def cleanup_orphaned_enrollments(
        self,
        *,
        admission_policy: EnrollmentAdmissionPolicy | None = None,
    ) -> int:
        policy = admission_policy or self._admission_policy
        now = _as_utc(self._now())
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                removed = await self._cleanup_orphaned_enrollments_tx(
                    conn,
                    policy=policy,
                    now_iso=now.isoformat(),
                )
                await conn.execute(
                    "DELETE FROM public_enrollment_admissions WHERE admitted_at < ?",
                    (now.timestamp() - max(policy.window_s, 24 * 60 * 60),),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return removed

    async def _admit_new_enrollment_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        enrollment_hash: str,
        peer_hash: str,
        policy: EnrollmentAdmissionPolicy,
    ) -> None:
        now = _as_utc(self._now())
        now_epoch = now.timestamp()
        now_iso = now.isoformat()
        await self._cleanup_orphaned_enrollments_tx(
            conn,
            policy=policy,
            now_iso=now_iso,
        )
        await conn.execute(
            "DELETE FROM public_enrollment_admissions WHERE admitted_at < ?",
            (now_epoch - max(policy.window_s, 24 * 60 * 60),),
        )
        await self._check_admission_window_tx(
            conn,
            now_epoch=now_epoch,
            duration_s=policy.window_s,
            limit=policy.peer_max_per_window,
            reason="peer_window",
            peer_hash=peer_hash,
        )
        await self._check_admission_window_tx(
            conn,
            now_epoch=now_epoch,
            duration_s=policy.window_s,
            limit=policy.global_max_per_window,
            reason="global_window",
        )
        await self._check_admission_window_tx(
            conn,
            now_epoch=now_epoch,
            duration_s=24 * 60 * 60,
            limit=policy.peer_max_per_day,
            reason="peer_day",
            peer_hash=peer_hash,
        )
        await self._check_admission_window_tx(
            conn,
            now_epoch=now_epoch,
            duration_s=24 * 60 * 60,
            limit=policy.global_max_per_day,
            reason="global_day",
        )
        if await self._active_enrollment_count_tx(conn, now_iso=now_iso) >= policy.total_active_max:
            raise EnrollmentAdmissionLimitError(
                "total_active",
                retry_after_s=math.ceil(policy.window_s),
            )
        await conn.execute(
            """INSERT INTO public_enrollment_admissions
               (enrollment_id_hash, peer_key_hash, admitted_at)
               VALUES (?, ?, ?)""",
            (enrollment_hash, peer_hash, now_epoch),
        )

    async def enroll_public_device(
        self,
        *,
        enrollment_id: str | None = None,
        device_secret: str | None = None,
        peer_key: str = "internal",
        display_name: str | None = None,
        tenant_id: str | None = None,
        device_id: str | None = None,
        user_id: str | None = None,
        ttl: timedelta | None = None,
        admission_policy: EnrollmentAdmissionPolicy | None = None,
    ) -> IssuedDeviceIdentity:
        """Create or idempotently resume one installation-bound identity."""

        effective_ttl = ttl or self._default_ttl
        if effective_ttl <= timedelta(0) or effective_ttl > timedelta(hours=1):
            raise ValueError("ttl must be positive and no longer than one hour")
        enrollment = enrollment_id or _new_secret("enrollment")
        credential = device_secret or _new_secret("edc")
        if len(enrollment) < 32 or len(credential) < 32:
            raise ValueError("enrollment_id and device_secret must be at least 32 characters")
        enrollment_hash = _binding_hash(enrollment, domain="enrollment-id")
        credential_hash = _token_hash(credential)
        peer_hash = _binding_hash(peer_key, domain="enrollment-peer")
        now = _as_utc(self._now()).isoformat()
        tenant = tenant_id or _new_id("tenant")
        user = user_id or _new_id("owner")
        device = device_id or _new_id("device")
        family = _new_id("family")
        policy = admission_policy or self._admission_policy
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """SELECT enrollment_id_hash, device_secret_hash, family_id,
                              tenant_id, user_id, device_id
                       FROM public_enrollments WHERE enrollment_id_hash = ?""",
                    (enrollment_hash,),
                )
                existing = await cur.fetchone()
                await cur.close()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["device_secret_hash"]),
                        credential_hash,
                    ):
                        raise IdentityAlreadyEnrolledError(
                            "enrollment id is bound to another device secret"
                        )
                    credential_row = await self._credential_row_tx(conn, credential)
                    if (
                        str(credential_row["family_id"]) != str(existing["family_id"])
                        or str(credential_row["tenant_id"]) != str(existing["tenant_id"])
                        or str(credential_row["user_id"]) != str(existing["user_id"])
                        or str(credential_row["device_id"]) != str(existing["device_id"])
                    ):
                        raise InvalidDeviceCredentialError("enrollment scope mismatch")
                    session = await self._renew_credential_row_tx(
                        conn,
                        credential_row,
                        ttl=effective_ttl,
                    )
                    await conn.execute(
                        """UPDATE public_enrollments SET peer_key_hash = ?
                           WHERE enrollment_id_hash = ?""",
                        (peer_hash, enrollment_hash),
                    )
                    await conn.commit()
                    return IssuedDeviceIdentity(
                        session=session,
                        device_credential=credential,
                        credential_id=str(credential_row["credential_id"]),
                        credential_expires_at=str(credential_row["credential_expires_at"]),
                    )
                await self._admit_new_enrollment_tx(
                    conn,
                    enrollment_hash=enrollment_hash,
                    peer_hash=peer_hash,
                    policy=policy,
                )
                await conn.execute(
                    "INSERT INTO tenants(tenant_id, status, created_at, updated_at) "
                    "VALUES (?, 'active', ?, ?)",
                    (tenant, now, now),
                )
                await conn.execute(
                    """INSERT INTO users
                       (tenant_id, user_id, status, created_at, updated_at)
                       VALUES (?, ?, 'active', ?, ?)""",
                    (tenant, user, now, now),
                )
                await conn.execute(
                    """INSERT INTO devices
                       (tenant_id, user_id, device_id, display_name, created_at,
                        last_seen_at, legacy_claimed_at, revoked_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                    (tenant, user, device, display_name, now, now),
                )
                await conn.execute(
                    """INSERT INTO session_families
                       (family_id, tenant_id, user_id, device_id, created_at,
                        last_renewed_at, generation, revoked_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, NULL)""",
                    (family, tenant, user, device, now, now),
                )
                credential, credential_id, credential_expires_at = await self._insert_credential_tx(
                    conn,
                    tenant_id=tenant,
                    user_id=user,
                    device_id=device,
                    family_id=family,
                    credential=credential,
                )
                await conn.execute(
                    """INSERT INTO public_enrollments
                       (enrollment_id_hash, device_secret_hash, peer_key_hash,
                        family_id, tenant_id, user_id, device_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        enrollment_hash,
                        credential_hash,
                        peer_hash,
                        family,
                        tenant,
                        user,
                        device,
                        now,
                    ),
                )
                session = await self._insert_session_tx(
                    conn,
                    tenant_id=tenant,
                    user_id=user,
                    device_id=device,
                    family_id=family,
                    generation=0,
                    ttl=effective_ttl,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return IssuedDeviceIdentity(
            session=session,
            device_credential=credential,
            credential_id=credential_id,
            credential_expires_at=credential_expires_at,
        )

    async def issue_public_session(
        self,
        *,
        tenant_id: str | None = None,
        device_id: str | None = None,
        owner_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> IssuedSession:
        """Compatibility wrapper for callers that only consume an access token."""

        enrolled = await self.enroll_public_device(
            tenant_id=tenant_id,
            device_id=device_id,
            user_id=owner_id,
            ttl=ttl,
        )
        return enrolled.session

    async def enroll_additional_device(
        self,
        principal: Principal,
        *,
        current_credential: str,
        enrollment_id: str,
        device_secret: str,
        peer_key: str,
        display_name: str | None = None,
        ttl: timedelta | None = None,
        admission_policy: EnrollmentAdmissionPolicy | None = None,
    ) -> IssuedDeviceIdentity:
        """Enroll another device under the authenticated tenant/user."""

        if principal.mode != "public" or not principal.family_id:
            raise InvalidDeviceCredentialError("public session family required")
        effective_ttl = ttl or self._default_ttl
        if effective_ttl <= timedelta(0) or effective_ttl > timedelta(hours=1):
            raise ValueError("ttl must be positive and no longer than one hour")
        if len(enrollment_id) < 32 or len(device_secret) < 32:
            raise ValueError("enrollment_id and device_secret must be at least 32 characters")
        enrollment_hash = _binding_hash(enrollment_id, domain="enrollment-id")
        device_secret_hash = _token_hash(device_secret)
        peer_hash = _binding_hash(peer_key, domain="enrollment-peer")
        now = _as_utc(self._now()).isoformat()
        policy = admission_policy or self._admission_policy
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                authorizer = await self._credential_row_tx(conn, current_credential)
                if (
                    str(authorizer["family_id"]) != principal.family_id
                    or str(authorizer["tenant_id"]) != principal.tenant_id
                    or str(authorizer["user_id"]) != principal.owner_id
                    or str(authorizer["device_id"]) != principal.device_id
                ):
                    raise InvalidDeviceCredentialError("credential scope mismatch")
                cur = await conn.execute(
                    """SELECT device_secret_hash, family_id, tenant_id, user_id, device_id
                       FROM public_enrollments WHERE enrollment_id_hash = ?""",
                    (enrollment_hash,),
                )
                existing = await cur.fetchone()
                await cur.close()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["device_secret_hash"]), device_secret_hash
                    ):
                        raise IdentityAlreadyEnrolledError(
                            "enrollment id is bound to another device secret"
                        )
                    if (
                        str(existing["tenant_id"]) != principal.tenant_id
                        or str(existing["user_id"]) != principal.owner_id
                    ):
                        raise IdentityAlreadyEnrolledError("enrollment id is bound to another user")
                    credential_row = await self._credential_row_tx(conn, device_secret)
                    session = await self._renew_credential_row_tx(
                        conn,
                        credential_row,
                        ttl=effective_ttl,
                    )
                    await conn.commit()
                    return IssuedDeviceIdentity(
                        session=session,
                        device_credential=device_secret,
                        credential_id=str(credential_row["credential_id"]),
                        credential_expires_at=str(credential_row["credential_expires_at"]),
                    )
                await self._admit_new_enrollment_tx(
                    conn,
                    enrollment_hash=enrollment_hash,
                    peer_hash=peer_hash,
                    policy=policy,
                )
                device_id = _new_id("device")
                family_id = _new_id("family")
                await conn.execute(
                    """INSERT INTO devices
                       (tenant_id, user_id, device_id, display_name, created_at,
                        last_seen_at, legacy_claimed_at, revoked_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                    (
                        principal.tenant_id,
                        principal.owner_id,
                        device_id,
                        display_name,
                        now,
                        now,
                    ),
                )
                await conn.execute(
                    """INSERT INTO session_families
                       (family_id, tenant_id, user_id, device_id, created_at,
                        last_renewed_at, generation, revoked_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, NULL)""",
                    (
                        family_id,
                        principal.tenant_id,
                        principal.owner_id,
                        device_id,
                        now,
                        now,
                    ),
                )
                credential, credential_id, credential_expires_at = await self._insert_credential_tx(
                    conn,
                    tenant_id=principal.tenant_id,
                    user_id=principal.owner_id,
                    device_id=device_id,
                    family_id=family_id,
                    credential=device_secret,
                )
                await conn.execute(
                    """INSERT INTO public_enrollments
                       (enrollment_id_hash, device_secret_hash, peer_key_hash,
                        family_id, tenant_id, user_id, device_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        enrollment_hash,
                        device_secret_hash,
                        peer_hash,
                        family_id,
                        principal.tenant_id,
                        principal.owner_id,
                        device_id,
                        now,
                    ),
                )
                session = await self._insert_session_tx(
                    conn,
                    tenant_id=principal.tenant_id,
                    user_id=principal.owner_id,
                    device_id=device_id,
                    family_id=family_id,
                    generation=0,
                    ttl=effective_ttl,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return IssuedDeviceIdentity(
            session=session,
            device_credential=credential,
            credential_id=credential_id,
            credential_expires_at=credential_expires_at,
        )

    @staticmethod
    def _assert_credential_row(row: aiosqlite.Row | None, now: datetime) -> aiosqlite.Row:
        if row is None:
            raise InvalidDeviceCredentialError("device credential invalid")
        if (
            row["credential_revoked_at"] is not None
            or row["family_revoked_at"] is not None
            or row["device_revoked_at"] is not None
            or row["user_status"] != "active"
            or row["tenant_status"] != "active"
        ):
            raise RevokedDeviceCredentialError("device credential revoked")
        expires_at = _as_utc(datetime.fromisoformat(str(row["credential_expires_at"])))
        if now >= expires_at:
            raise ExpiredDeviceCredentialError("device credential expired")
        return row

    async def _credential_row_tx(
        self,
        conn: aiosqlite.Connection,
        credential: str,
    ) -> aiosqlite.Row:
        if not credential or not credential.strip():
            raise InvalidDeviceCredentialError("device credential missing")
        cur = await conn.execute(
            """SELECT
                   dc.credential_id,
                   dc.family_id,
                   dc.tenant_id,
                   dc.user_id,
                   dc.device_id,
                   dc.expires_at AS credential_expires_at,
                   dc.revoked_at AS credential_revoked_at,
                   sf.generation AS family_generation,
                   sf.revoked_at AS family_revoked_at,
                   d.revoked_at AS device_revoked_at,
                   u.status AS user_status,
                   t.status AS tenant_status
               FROM device_credentials dc
               JOIN session_families sf
                 ON sf.family_id = dc.family_id
                AND sf.tenant_id = dc.tenant_id
                AND sf.user_id = dc.user_id
                AND sf.device_id = dc.device_id
               JOIN devices d
                 ON d.tenant_id = dc.tenant_id
                AND d.user_id = dc.user_id
                AND d.device_id = dc.device_id
               JOIN users u
                 ON u.tenant_id = dc.tenant_id AND u.user_id = dc.user_id
               JOIN tenants t ON t.tenant_id = dc.tenant_id
               WHERE dc.credential_hash = ?""",
            (_token_hash(credential),),
        )
        row = await cur.fetchone()
        await cur.close()
        return self._assert_credential_row(row, _as_utc(self._now()))

    async def _renew_credential_row_tx(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        *,
        ttl: timedelta,
    ) -> IssuedSession:
        now = _as_utc(self._now()).isoformat()
        cur = await conn.execute(
            """SELECT session_id FROM principal_sessions
               WHERE family_id = ? AND revoked_at IS NULL
               ORDER BY generation DESC LIMIT 1""",
            (row["family_id"],),
        )
        previous = await cur.fetchone()
        await cur.close()
        previous_id = str(previous["session_id"]) if previous else None
        generation = int(row["family_generation"]) + 1
        await conn.execute(
            "UPDATE principal_sessions SET revoked_at = ? "
            "WHERE family_id = ? AND revoked_at IS NULL",
            (now, row["family_id"]),
        )
        await conn.execute(
            """UPDATE session_families
               SET generation = ?, last_renewed_at = ?
               WHERE family_id = ? AND revoked_at IS NULL""",
            (generation, now, row["family_id"]),
        )
        await conn.execute(
            "UPDATE device_credentials SET last_used_at = ? WHERE credential_id = ?",
            (now, row["credential_id"]),
        )
        await conn.execute(
            """UPDATE devices SET last_seen_at = ?
               WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
            (now, row["tenant_id"], row["user_id"], row["device_id"]),
        )
        return await self._insert_session_tx(
            conn,
            tenant_id=str(row["tenant_id"]),
            user_id=str(row["user_id"]),
            device_id=str(row["device_id"]),
            family_id=str(row["family_id"]),
            generation=generation,
            ttl=ttl,
            renewed_from_session_id=previous_id,
        )

    async def renew_public_session(
        self,
        credential: str,
        *,
        ttl: timedelta | None = None,
    ) -> IssuedSession:
        """Rotate the access bearer while preserving the stable identity."""

        effective_ttl = ttl or self._default_ttl
        if effective_ttl <= timedelta(0) or effective_ttl > timedelta(hours=1):
            raise ValueError("ttl must be positive and no longer than one hour")
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._credential_row_tx(conn, credential)
                issued = await self._renew_credential_row_tx(
                    conn,
                    row,
                    ttl=effective_ttl,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return issued

    async def rotate_device_credential(
        self,
        principal: Principal,
        *,
        current_credential: str,
        new_credential: str,
    ) -> tuple[str, str]:
        """Rotate the durable credential for the authenticated session family."""

        if principal.mode != "public" or not principal.family_id:
            raise InvalidDeviceCredentialError("public session family required")
        if len(new_credential) < 32 or hmac.compare_digest(
            _token_hash(current_credential), _token_hash(new_credential)
        ):
            raise InvalidDeviceCredentialError("a distinct 32-character new credential is required")
        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                old = await self._credential_row_tx(conn, current_credential)
                if (
                    str(old["family_id"]) != principal.family_id
                    or str(old["tenant_id"]) != principal.tenant_id
                    or str(old["user_id"]) != principal.owner_id
                    or str(old["device_id"]) != principal.device_id
                ):
                    raise InvalidDeviceCredentialError("credential scope mismatch")
                new_id = _new_id("credential")
                expires_at = (_as_utc(self._now()) + self._credential_ttl).isoformat()
                await conn.execute(
                    """UPDATE device_credentials
                       SET revoked_at = ?
                       WHERE credential_id = ? AND revoked_at IS NULL""",
                    (now, old["credential_id"]),
                )
                await conn.execute(
                    """INSERT INTO device_credentials
                       (credential_id, credential_hash, family_id, tenant_id, user_id,
                        device_id, issued_at, expires_at, last_used_at, revoked_at,
                        rotated_to_credential_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
                    (
                        new_id,
                        _token_hash(new_credential),
                        principal.family_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                        now,
                        expires_at,
                    ),
                )
                await conn.execute(
                    """UPDATE device_credentials SET rotated_to_credential_id = ?
                       WHERE credential_id = ?""",
                    (new_id, old["credential_id"]),
                )
                await conn.execute(
                    """UPDATE public_enrollments SET device_secret_hash = ?
                       WHERE family_id = ? AND tenant_id = ? AND user_id = ?
                         AND device_id = ?""",
                    (
                        _token_hash(new_credential),
                        principal.family_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                    ),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return new_id, expires_at

    async def claim_legacy_identity(self, principal: Principal) -> IssuedDeviceIdentity:
        """One-time upgrade from a pre-018 bearer to a durable device credential."""

        if principal.mode != "public" or not principal.family_id:
            raise InvalidSessionError("public session family required")
        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """SELECT d.legacy_claimed_at, sf.generation
                       FROM principal_sessions ps
                       JOIN session_families sf
                         ON sf.family_id = ps.family_id
                        AND sf.tenant_id = ps.tenant_id
                        AND sf.user_id = ps.owner_id
                        AND sf.device_id = ps.device_id
                       JOIN devices d
                         ON d.tenant_id = ps.tenant_id
                        AND d.user_id = ps.owner_id
                        AND d.device_id = ps.device_id
                       WHERE ps.session_id = ? AND ps.tenant_id = ?
                         AND ps.owner_id = ? AND ps.device_id = ?
                         AND ps.revoked_at IS NULL AND sf.revoked_at IS NULL""",
                    (
                        principal.session_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                    ),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise InvalidSessionError("session invalid")
                cur = await conn.execute(
                    "SELECT 1 FROM device_credentials WHERE family_id = ? LIMIT 1",
                    (principal.family_id,),
                )
                has_credential = await cur.fetchone()
                await cur.close()
                if row["legacy_claimed_at"] is not None or has_credential is not None:
                    raise DeviceIdentityAlreadyClaimedError("device identity already claimed")
                credential, credential_id, credential_expires_at = await self._insert_credential_tx(
                    conn,
                    tenant_id=principal.tenant_id,
                    user_id=principal.owner_id,
                    device_id=principal.device_id,
                    family_id=principal.family_id,
                )
                await conn.execute(
                    """UPDATE devices SET legacy_claimed_at = ?, last_seen_at = ?
                       WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
                    (
                        now,
                        now,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                    ),
                )
                generation = int(row["generation"]) + 1
                await conn.execute(
                    "UPDATE principal_sessions SET revoked_at = ? "
                    "WHERE family_id = ? AND revoked_at IS NULL",
                    (now, principal.family_id),
                )
                await conn.execute(
                    """UPDATE session_families
                       SET generation = ?, last_renewed_at = ? WHERE family_id = ?""",
                    (generation, now, principal.family_id),
                )
                session = await self._insert_session_tx(
                    conn,
                    tenant_id=principal.tenant_id,
                    user_id=principal.owner_id,
                    device_id=principal.device_id,
                    family_id=principal.family_id,
                    generation=generation,
                    ttl=self._default_ttl,
                    renewed_from_session_id=principal.session_id,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return IssuedDeviceIdentity(
            session=session,
            device_credential=credential,
            credential_id=credential_id,
            credential_expires_at=credential_expires_at,
        )

    async def validate_public_token(self, token: str) -> Principal:
        """Resolve an opaque access token to the server-authoritative identity."""

        if not token or not token.strip():
            raise InvalidSessionError("session token missing")
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT
                       ps.session_id, ps.tenant_id, ps.device_id, ps.owner_id,
                       ps.mode, ps.expires_at, ps.revoked_at, ps.family_id,
                       sf.revoked_at AS family_revoked_at,
                       d.revoked_at AS device_revoked_at,
                       u.status AS user_status,
                       t.status AS tenant_status
                   FROM principal_sessions ps
                   JOIN session_families sf
                     ON sf.family_id = ps.family_id
                    AND sf.tenant_id = ps.tenant_id
                    AND sf.user_id = ps.owner_id
                    AND sf.device_id = ps.device_id
                   JOIN devices d
                     ON d.tenant_id = ps.tenant_id
                    AND d.user_id = ps.owner_id
                    AND d.device_id = ps.device_id
                   JOIN users u
                     ON u.tenant_id = ps.tenant_id AND u.user_id = ps.owner_id
                   JOIN tenants t ON t.tenant_id = ps.tenant_id
                   WHERE ps.token_hash = ?""",
                (_token_hash(token),),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None or row["mode"] != "public":
            raise InvalidSessionError("session token invalid")
        if (
            row["revoked_at"] is not None
            or row["family_revoked_at"] is not None
            or row["device_revoked_at"] is not None
            or row["user_status"] != "active"
            or row["tenant_status"] != "active"
        ):
            raise RevokedSessionError("session revoked")
        expires_at = _as_utc(datetime.fromisoformat(str(row["expires_at"])))
        if _as_utc(self._now()) >= expires_at:
            raise ExpiredSessionError("session expired")
        return Principal(
            tenant_id=str(row["tenant_id"]),
            device_id=str(row["device_id"]),
            owner_id=str(row["owner_id"]),
            session_id=str(row["session_id"]),
            mode="public",
            family_id=str(row["family_id"]),
        )

    async def assert_active_principal(self, principal: Principal) -> None:
        """Revalidate an already-bound public principal without retaining its bearer."""

        if principal.mode != "public" or not principal.family_id:
            return
        async with self._conn() as conn:
            row = await (
                await conn.execute(
                    """SELECT
                           ps.expires_at, ps.revoked_at, ps.generation,
                           sf.generation AS family_generation,
                           sf.revoked_at AS family_revoked_at,
                           d.revoked_at AS device_revoked_at,
                           u.status AS user_status,
                           t.status AS tenant_status
                       FROM principal_sessions ps
                       JOIN session_families sf
                         ON sf.family_id = ps.family_id
                        AND sf.tenant_id = ps.tenant_id
                        AND sf.user_id = ps.owner_id
                        AND sf.device_id = ps.device_id
                       JOIN devices d
                         ON d.tenant_id = ps.tenant_id
                        AND d.user_id = ps.owner_id
                        AND d.device_id = ps.device_id
                       JOIN users u
                         ON u.tenant_id = ps.tenant_id AND u.user_id = ps.owner_id
                       JOIN tenants t ON t.tenant_id = ps.tenant_id
                       WHERE ps.session_id = ? AND ps.tenant_id = ?
                         AND ps.owner_id = ? AND ps.device_id = ?
                         AND ps.family_id = ?""",
                    (
                        principal.session_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                        principal.family_id,
                    ),
                )
            ).fetchone()
        if row is None:
            raise InvalidSessionError("session invalid")
        if (
            row["revoked_at"] is not None
            or row["family_revoked_at"] is not None
            or row["device_revoked_at"] is not None
            or row["user_status"] != "active"
            or row["tenant_status"] != "active"
            or int(row["generation"]) != int(row["family_generation"])
        ):
            raise RevokedSessionError("session revoked")
        expires_at = _as_utc(datetime.fromisoformat(str(row["expires_at"])))
        if _as_utc(self._now()) >= expires_at:
            raise ExpiredSessionError("session expired")

    async def revoke_session(self, session_id: str) -> bool:
        """Idempotently revoke one access token while keeping renewal possible."""

        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            cur = await conn.execute(
                """UPDATE principal_sessions SET revoked_at = ?
                   WHERE session_id = ? AND revoked_at IS NULL""",
                (now, session_id),
            )
            await conn.commit()
            changed = bool(cur.rowcount)
            await cur.close()
        return changed

    async def revoke_session_family(self, principal: Principal) -> bool:
        """Revoke all access and durable credentials in one family."""

        if principal.mode != "public" or not principal.family_id:
            return False
        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """UPDATE session_families SET revoked_at = ?
                       WHERE family_id = ? AND tenant_id = ? AND user_id = ?
                         AND device_id = ? AND revoked_at IS NULL""",
                    (
                        now,
                        principal.family_id,
                        principal.tenant_id,
                        principal.owner_id,
                        principal.device_id,
                    ),
                )
                changed = bool(cur.rowcount)
                await cur.close()
                await conn.execute(
                    "UPDATE principal_sessions SET revoked_at = COALESCE(revoked_at, ?) "
                    "WHERE family_id = ?",
                    (now, principal.family_id),
                )
                await conn.execute(
                    "UPDATE device_credentials SET revoked_at = COALESCE(revoked_at, ?) "
                    "WHERE family_id = ?",
                    (now, principal.family_id),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return changed

    async def revoke_device(
        self,
        principal: Principal,
        *,
        current_credential: str,
    ) -> bool:
        """Revoke the device and every session family issued for it."""

        if principal.mode != "public":
            return False
        now = _as_utc(self._now()).isoformat()
        scope = (principal.tenant_id, principal.owner_id, principal.device_id)
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                credential_row = await self._credential_row_tx(conn, current_credential)
                if (
                    str(credential_row["family_id"]) != principal.family_id
                    or str(credential_row["tenant_id"]) != principal.tenant_id
                    or str(credential_row["user_id"]) != principal.owner_id
                    or str(credential_row["device_id"]) != principal.device_id
                ):
                    raise InvalidDeviceCredentialError("credential scope mismatch")
                cur = await conn.execute(
                    """UPDATE devices SET revoked_at = ?
                       WHERE tenant_id = ? AND user_id = ? AND device_id = ?
                         AND revoked_at IS NULL""",
                    (now, *scope),
                )
                changed = bool(cur.rowcount)
                await cur.close()
                await conn.execute(
                    """UPDATE session_families SET revoked_at = COALESCE(revoked_at, ?)
                       WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
                    (now, *scope),
                )
                await conn.execute(
                    """UPDATE principal_sessions SET revoked_at = COALESCE(revoked_at, ?)
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?""",
                    (now, *scope),
                )
                await conn.execute(
                    """UPDATE device_credentials SET revoked_at = COALESCE(revoked_at, ?)
                       WHERE tenant_id = ? AND user_id = ? AND device_id = ?""",
                    (now, *scope),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return changed

    async def cleanup_expired_sessions(self) -> tuple[int, int]:
        """Delete expired/revoked tickets and access rows; identity remains durable."""

        now = _as_utc(self._now()).isoformat()
        async with self._conn() as conn:
            tickets = await conn.execute(
                """DELETE FROM resource_tickets
                   WHERE expires_at <= ? OR revoked_at IS NOT NULL
                      OR session_id IN (
                          SELECT session_id FROM principal_sessions
                          WHERE expires_at <= ? OR revoked_at IS NOT NULL
                      )""",
                (now, now),
            )
            sessions = await conn.execute(
                """DELETE FROM principal_sessions
                   WHERE expires_at <= ? OR revoked_at IS NOT NULL""",
                (now,),
            )
            await conn.commit()
            removed = (max(0, tickets.rowcount), max(0, sessions.rowcount))
            await tickets.close()
            await sessions.close()
        return removed

    async def issue_resource_ticket(
        self,
        principal: Principal,
        *,
        resource_type: str,
        resource_id: str,
        ttl: timedelta = timedelta(hours=1),
    ) -> str:
        if principal.mode != "public":
            raise ValueError("resource tickets are only needed for public principals")
        async with self._conn() as conn:
            token, _ticket_id, _expires_at = await self.issue_resource_ticket_tx(
                conn,
                principal,
                resource_type=resource_type,
                resource_id=resource_id,
                ttl=ttl,
            )
            await conn.commit()
        return token

    async def issue_resource_ticket_tx(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        *,
        resource_type: str,
        resource_id: str,
        ttl: timedelta = timedelta(hours=1),
    ) -> tuple[str, str, str]:
        if principal.mode != "public":
            raise ValueError("resource tickets are only needed for public principals")
        if resource_type not in {"meeting", "artifact"}:
            raise ValueError("unsupported resource ticket type")
        if not resource_id.strip() or ttl <= timedelta(0) or ttl > timedelta(hours=1):
            raise ValueError("resource id and ttl up to one hour are required")
        token = _new_secret("edt")
        ticket_id = _new_id("ticket")
        now = _as_utc(self._now())
        expires_at = now + ttl
        cur = await conn.execute(
            """INSERT INTO resource_tickets
               (ticket_id, token_hash, session_id, tenant_id, device_id, owner_id,
                resource_type, resource_id, capability, issued_at, expires_at, revoked_at)
               SELECT ?, ?, ps.session_id, ps.tenant_id, ps.device_id, ps.owner_id,
                      ?, ?, 'read', ?, ?, NULL
               FROM principal_sessions ps
               JOIN session_families sf
                 ON sf.family_id = ps.family_id
                AND sf.tenant_id = ps.tenant_id
                AND sf.user_id = ps.owner_id
                AND sf.device_id = ps.device_id
               WHERE ps.session_id = ? AND ps.tenant_id = ? AND ps.owner_id = ?
                 AND ps.device_id = ? AND ps.revoked_at IS NULL
                 AND ps.expires_at > ? AND sf.revoked_at IS NULL""",
            (
                ticket_id,
                _token_hash(token),
                resource_type,
                resource_id,
                now.isoformat(),
                expires_at.isoformat(),
                principal.session_id,
                principal.tenant_id,
                principal.owner_id,
                principal.device_id,
                now.isoformat(),
            ),
        )
        inserted = cur.rowcount == 1
        await cur.close()
        if not inserted:
            raise ResourceTicketError("issuing session is invalid or expired")
        return token, ticket_id, expires_at.isoformat()

    async def validate_resource_ticket(
        self,
        token: str,
        *,
        resource_type: str,
        resource_id: str,
    ) -> Principal:
        if not token:
            raise ResourceTicketError("resource ticket missing")
        now = _as_utc(self._now())
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT
                       rt.session_id, rt.tenant_id, rt.device_id, rt.owner_id,
                       rt.expires_at, rt.revoked_at,
                       ps.expires_at AS session_expires_at,
                       ps.revoked_at AS session_revoked_at,
                       ps.family_id,
                       sf.revoked_at AS family_revoked_at,
                       d.revoked_at AS device_revoked_at,
                       u.status AS user_status,
                       t.status AS tenant_status
                   FROM resource_tickets rt
                   JOIN principal_sessions ps
                     ON ps.session_id = rt.session_id
                    AND ps.tenant_id = rt.tenant_id
                    AND ps.owner_id = rt.owner_id
                    AND ps.device_id = rt.device_id
                   JOIN session_families sf
                     ON sf.family_id = ps.family_id
                    AND sf.tenant_id = ps.tenant_id
                    AND sf.user_id = ps.owner_id
                    AND sf.device_id = ps.device_id
                   JOIN devices d
                     ON d.tenant_id = ps.tenant_id
                    AND d.user_id = ps.owner_id
                    AND d.device_id = ps.device_id
                   JOIN users u
                     ON u.tenant_id = ps.tenant_id AND u.user_id = ps.owner_id
                   JOIN tenants t ON t.tenant_id = ps.tenant_id
                   WHERE rt.token_hash = ? AND rt.resource_type = ?
                     AND rt.resource_id = ? AND rt.capability = 'read'""",
                (_token_hash(token), resource_type, resource_id),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None or any(
            row[key] is not None
            for key in (
                "revoked_at",
                "session_revoked_at",
                "family_revoked_at",
                "device_revoked_at",
            )
        ):
            raise ResourceTicketError("resource ticket invalid or revoked")
        if row["user_status"] != "active" or row["tenant_status"] != "active":
            raise ResourceTicketError("resource ticket identity disabled")
        if now >= _as_utc(datetime.fromisoformat(str(row["expires_at"]))) or now >= _as_utc(
            datetime.fromisoformat(str(row["session_expires_at"]))
        ):
            raise ResourceTicketError("resource ticket expired")
        return Principal(
            tenant_id=str(row["tenant_id"]),
            device_id=str(row["device_id"]),
            owner_id=str(row["owner_id"]),
            session_id=str(row["session_id"]),
            mode="public",
            family_id=str(row["family_id"]),
        )

    async def resolve_principal(
        self,
        *,
        public_mode: bool,
        token: str | None = None,
    ) -> Principal:
        if not public_mode:
            return local_principal()
        return await self.validate_public_token(token or "")


__all__ = [
    "DeviceCredentialError",
    "DeviceIdentityAlreadyClaimedError",
    "EnrollmentAdmissionLimitError",
    "EnrollmentAdmissionPolicy",
    "ExpiredDeviceCredentialError",
    "ExpiredSessionError",
    "IdentityAlreadyEnrolledError",
    "InvalidDeviceCredentialError",
    "InvalidSessionError",
    "ResourceTicketError",
    "RevokedDeviceCredentialError",
    "RevokedSessionError",
    "SessionError",
    "SessionStore",
]
