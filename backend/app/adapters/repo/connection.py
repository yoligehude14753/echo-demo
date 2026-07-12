"""Shared SQLite connection invariants."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiosqlite


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
    """Enable constraints on every async connection before its first query."""

    await conn.execute("PRAGMA foreign_keys=ON")


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    """Enable constraints on every synchronous connection before its first query."""

    conn.execute("PRAGMA foreign_keys=ON")


__all__ = [
    "configure_aiosqlite_connection",
    "configure_sqlite_connection",
    "open_aiosqlite_connection",
]
