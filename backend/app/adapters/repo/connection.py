"""Shared SQLite connection invariants."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiosqlite

# Capture、Sync Hub 与 workflow worker 会跨进程共享同一个 WAL 数据库。
# sqlite3 默认 5 秒等待不足以覆盖突发串行写入，曾表现为间歇性 capture HTTP 500。
# SQLite busy handler 会在单条语句产生副作用前执行有界重试，比在 Python 层
# 重放可能已经完成的写操作安全；等待上限同时保持在桌面 capture 请求窗口内。
SQLITE_BUSY_TIMEOUT_MS = 15_000


async def _open_aiosqlite_connection(
    connection: aiosqlite.Connection,
) -> aiosqlite.Connection:
    return await connection


async def _close_aiosqlite_connection(connection: aiosqlite.Connection) -> None:
    """Finish ``close()`` even if shutdown delivers another cancellation."""

    close_task = asyncio.create_task(connection.close())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        while not close_task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(close_task)
        close_task.result()
        raise


async def _finish_open_after_cancellation(
    open_task: asyncio.Task[aiosqlite.Connection],
) -> aiosqlite.Connection | None:
    """Wait for a shielded local connector despite repeated task cancellation."""

    while not open_task.done():
        try:
            return await asyncio.shield(open_task)
        except asyncio.CancelledError:
            continue
        except Exception:
            return None
    if open_task.cancelled():
        return None
    with suppress(Exception):
        return open_task.result()
    return None


@asynccontextmanager
async def open_aiosqlite_connection(
    database: str | Path,
    **kwargs: Any,
) -> AsyncIterator[aiosqlite.Connection]:
    """Open an aiosqlite worker without leaking it on ``__aenter__`` cancellation.

    aiosqlite 0.20 starts its non-daemon worker before awaiting the connector and
    does not catch ``CancelledError`` in that await.  If a lifecycle task is
    cancelled in that window, a normal ``async with aiosqlite.connect(...)`` has
    not entered yet, so ``__aexit__`` never closes the worker.  Shield the open,
    wait for it after interruption, and close it before propagating cancellation.
    """

    kwargs.setdefault("timeout", SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection = aiosqlite.connect(database, **kwargs)
    open_task = asyncio.create_task(_open_aiosqlite_connection(connection))
    try:
        opened = await asyncio.shield(open_task)
    except asyncio.CancelledError:
        opened_after_cancel = await _finish_open_after_cancellation(open_task)
        if opened_after_cancel is not None:
            with suppress(Exception):
                await _close_aiosqlite_connection(opened_after_cancel)
        raise

    try:
        yield opened
    finally:
        await _close_aiosqlite_connection(opened)


async def configure_aiosqlite_connection(conn: aiosqlite.Connection) -> None:
    """Enable shared SQLite safety invariants before the first query."""

    await conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")


async def rollback_aiosqlite_connection(conn: aiosqlite.Connection) -> None:
    """即使请求取消触发失败，也确保 rollback 执行完毕。"""

    rollback_task = asyncio.create_task(conn.rollback())
    while True:
        try:
            await asyncio.shield(rollback_task)
            return
        except asyncio.CancelledError:
            if rollback_task.done():
                rollback_task.result()
                return


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    """Enable shared SQLite safety invariants before the first query."""

    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "configure_aiosqlite_connection",
    "configure_sqlite_connection",
    "open_aiosqlite_connection",
    "rollback_aiosqlite_connection",
]
