"""公共后端的 principal 级资源治理。

累计预算写入 SQLite，重启后不会清零；并发、昂贵任务与 WebSocket 是
进程内租约，连接或请求结束即释放。所有键都来自服务端验证后的 Principal，
绝不信任客户端传入 tenant/owner 字段。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.config import Settings
from app.security.models import Principal

LedgerMetric = Literal["requests", "upload_bytes", "storage_bytes", "llm_tokens"]
LeaseMetric = Literal["requests", "expensive_tasks", "websockets"]

_EXPENSIVE_ROUTES = (
    re.compile(r"^/capture/chunk$"),
    re.compile(r"^/chat(?:/.*)?$"),
    re.compile(r"^/retrieval(?:/.*)?$"),
    re.compile(r"^/rag(?:/.*)?$"),
    re.compile(r"^/artifacts/generate$"),
    re.compile(r"^/meetings/[^/]+/(?:chunk|finalize)$"),
    re.compile(r"^/workflows(?:/.*)?$"),
    re.compile(r"^/tts(?:/.*)?$"),
)


class QuotaExceeded(RuntimeError):
    """某一 principal 的资源预算已经用尽。"""

    def __init__(
        self,
        metric: str,
        *,
        limit: int,
        used: int,
        retry_after_s: int = 1,
    ) -> None:
        super().__init__(f"quota exceeded: {metric}")
        self.metric = metric
        self.limit = limit
        self.used = used
        self.retry_after_s = max(1, retry_after_s)


class _Lease:
    def __init__(
        self,
        release: Callable[[], None] | None = None,
    ) -> None:
        self._release = release
        self._released = False

    async def __aenter__(self) -> _Lease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._release is not None:
            self._release()


class QuotaReservation:
    """可结算的持久预算预留；失败或实际用量更小时退回差额。"""

    def __init__(
        self,
        governor: PrincipalGovernor,
        principal: Principal,
        metric: LedgerMetric,
        window_key: str,
        reserved: int,
        limit: int,
    ) -> None:
        self._governor = governor
        self._principal = principal
        self._metric = metric
        self._window_key = window_key
        self._reserved = reserved
        self._limit = limit
        self._settled = False

    async def settle(self, actual: int) -> None:
        if self._settled or self._reserved == 0:
            self._settled = True
            return
        self._settled = True
        delta = max(0, actual) - self._reserved
        if delta:
            await self._governor._adjust_ledger(
                self._principal,
                self._metric,
                self._window_key,
                delta,
                self._limit,
            )

    async def release(self) -> None:
        await self.settle(0)


class PrincipalGovernor:
    """请求、并发、昂贵任务、WS、上传、存储与 LLM token 总闸。"""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.db_path = Path(settings.db_path).expanduser()
        self._now = now or (lambda: datetime.now(UTC))
        self._active: dict[tuple[str, str, LeaseMetric], int] = {}
        self._active_lock = asyncio.Lock()

    @staticmethod
    def _scope(principal: Principal) -> tuple[str, str]:
        return principal.tenant_id, principal.owner_id

    @staticmethod
    def _is_governed(principal: Principal) -> bool:
        return principal.mode == "public"

    def _window_key(self, metric: LedgerMetric) -> tuple[str, int]:
        now = self._now().astimezone(UTC)
        if metric == "requests":
            return now.strftime("minute:%Y%m%d%H%M"), max(1, 60 - now.second)
        if metric in {"upload_bytes", "llm_tokens"}:
            remaining = 86_400 - (now.hour * 3600 + now.minute * 60 + now.second)
            return now.strftime("day:%Y%m%d"), max(1, remaining)
        return "lifetime", 3600

    @staticmethod
    def is_expensive(method: str, path: str) -> bool:
        return method.upper() not in {"GET", "HEAD", "OPTIONS"} and any(
            pattern.fullmatch(path) for pattern in _EXPENSIVE_ROUTES
        )

    async def _acquire_active(
        self,
        principal: Principal,
        metric: LeaseMetric,
        limit: int,
    ) -> _Lease:
        if not self._is_governed(principal):
            return _Lease()
        key = (*self._scope(principal), metric)
        async with self._active_lock:
            used = self._active.get(key, 0)
            if used >= limit:
                raise QuotaExceeded(metric, limit=limit, used=used)
            self._active[key] = used + 1

        def release() -> None:
            current = self._active.get(key, 0)
            if current <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = current - 1

        return _Lease(release)

    async def _adjust_ledger(
        self,
        principal: Principal,
        metric: LedgerMetric,
        window_key: str,
        delta: int,
        limit: int,
    ) -> int:
        if not self._is_governed(principal) or delta == 0:
            return 0
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            current = await self._read_usage(conn, principal, metric, window_key)
            updated = max(0, current + delta)
            if delta > 0 and updated > limit:
                await conn.rollback()
                _, retry_after = self._window_key(metric)
                raise QuotaExceeded(
                    metric,
                    limit=limit,
                    used=current,
                    retry_after_s=retry_after,
                )
            await self._write_usage(conn, principal, metric, window_key, updated)
            await conn.commit()
        return updated

    @staticmethod
    async def _read_usage(
        conn: aiosqlite.Connection,
        principal: Principal,
        metric: LedgerMetric,
        window_key: str,
    ) -> int:
        cursor = await conn.execute(
            """SELECT used FROM principal_quota_ledger
               WHERE tenant_id = ? AND owner_id = ? AND metric = ? AND window_key = ?""",
            (principal.tenant_id, principal.owner_id, metric, window_key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else 0

    async def _write_usage(
        self,
        conn: aiosqlite.Connection,
        principal: Principal,
        metric: LedgerMetric,
        window_key: str,
        used: int,
    ) -> None:
        await conn.execute(
            """INSERT INTO principal_quota_ledger
               (tenant_id, owner_id, metric, window_key, used, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, owner_id, metric, window_key)
               DO UPDATE SET used = excluded.used, updated_at = excluded.updated_at""",
            (
                principal.tenant_id,
                principal.owner_id,
                metric,
                window_key,
                used,
                self._now().astimezone(UTC).isoformat(),
            ),
        )

    async def _reserve(
        self,
        principal: Principal,
        metric: LedgerMetric,
        amount: int,
        limit: int,
    ) -> QuotaReservation:
        amount = max(0, amount)
        window_key, _retry_after = self._window_key(metric)
        if amount:
            await self._adjust_ledger(principal, metric, window_key, amount, limit)
        return QuotaReservation(self, principal, metric, window_key, amount, limit)

    @asynccontextmanager
    async def request(
        self,
        principal: Principal,
        *,
        method: str,
        path: str,
    ) -> AsyncIterator[None]:
        if not self._is_governed(principal):
            yield
            return
        window_key, _retry_after = self._window_key("requests")
        await self._adjust_ledger(
            principal,
            "requests",
            window_key,
            1,
            self.settings.quota_requests_per_minute,
        )
        request_lease = await self._acquire_active(
            principal,
            "requests",
            self.settings.quota_concurrent_requests,
        )
        expensive_lease = _Lease()
        try:
            if self.is_expensive(method, path):
                expensive_lease = await self._acquire_active(
                    principal,
                    "expensive_tasks",
                    self.settings.quota_concurrent_expensive_tasks,
                )
            yield
        finally:
            expensive_lease.release()
            request_lease.release()

    async def websocket(self, principal: Principal) -> _Lease:
        return await self._acquire_active(
            principal,
            "websockets",
            self.settings.quota_websocket_connections,
        )

    async def reserve_upload(
        self,
        principal: Principal,
        amount: int,
        *,
        persistent: bool,
    ) -> QuotaReservation | None:
        upload = await self.reserve_upload_bytes(principal, amount)
        await upload.settle(amount)
        if not persistent:
            return None
        return await self._reserve(
            principal,
            "storage_bytes",
            amount,
            self.settings.quota_storage_bytes,
        )

    async def reserve_upload_bytes(
        self,
        principal: Principal,
        amount: int,
    ) -> QuotaReservation:
        """Reserve declared body bytes before multipart parsing, then settle actual bytes."""

        return await self._reserve(
            principal,
            "upload_bytes",
            amount,
            self.settings.quota_upload_bytes_per_day,
        )

    async def charge_upload_bytes(self, principal: Principal, amount: int) -> None:
        """Settle one handler-read chunk into the durable daily upload budget."""

        reservation = await self._reserve(
            principal,
            "upload_bytes",
            amount,
            self.settings.quota_upload_bytes_per_day,
        )
        await reservation.settle(amount)

    async def reserve_storage(
        self,
        principal: Principal,
        amount: int,
    ) -> QuotaReservation:
        return await self._reserve(
            principal,
            "storage_bytes",
            amount,
            self.settings.quota_storage_bytes,
        )

    async def release_storage_bytes(self, principal: Principal, amount: int) -> None:
        window_key, _retry_after = self._window_key("storage_bytes")
        await self._adjust_ledger(
            principal,
            "storage_bytes",
            window_key,
            -max(0, amount),
            self.settings.quota_storage_bytes,
        )

    async def release_registered_ambient_storage(
        self,
        principal: Principal,
        audio_ref: str,
    ) -> int | None:
        """Atomically forget one public WAV and release its durable byte charge.

        The filesystem unlink happens before this call.  Keeping the inventory
        row, transcript ref cleanup and lifetime storage ledger in one SQLite
        transaction makes a retry after DB failure safe: either all three
        changes commit or none of them do.

        Returns the number of charged bytes released, ``0`` for an explicitly
        uncharged inventory row, or ``None`` when no owner-scoped row exists.
        """

        if not self._is_governed(principal):
            return None
        window_key, _retry_after = self._window_key("storage_bytes")
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """SELECT size_bytes, quota_charged
                   FROM ambient_audio_files
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (principal.tenant_id, principal.owner_id, audio_ref),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await conn.commit()
                return None
            size_bytes = max(0, int(row[0]))
            charged_bytes = size_bytes if bool(row[1]) else 0
            if charged_bytes:
                current = await self._read_usage(
                    conn,
                    principal,
                    "storage_bytes",
                    window_key,
                )
                await self._write_usage(
                    conn,
                    principal,
                    "storage_bytes",
                    window_key,
                    max(0, current - charged_bytes),
                )
            await conn.execute(
                """DELETE FROM ambient_audio_files
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (principal.tenant_id, principal.owner_id, audio_ref),
            )
            await conn.execute(
                """UPDATE ambient_segments SET audio_ref = ''
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (principal.tenant_id, principal.owner_id, audio_ref),
            )
            await conn.commit()
        return charged_bytes

    async def reserve_llm_tokens(
        self,
        principal: Principal,
        amount: int,
    ) -> QuotaReservation:
        return await self._reserve(
            principal,
            "llm_tokens",
            amount,
            self.settings.quota_llm_tokens_per_day,
        )

    async def usage(self, principal: Principal, metric: LedgerMetric) -> int:
        window_key, _retry_after = self._window_key(metric)
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            return await self._read_usage(conn, principal, metric, window_key)


__all__ = ["PrincipalGovernor", "QuotaExceeded", "QuotaReservation"]
