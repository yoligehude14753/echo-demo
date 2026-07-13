from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo import connection as connection_module
from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.adapters.repo.migrator import run_migrations
from app.runtime.execution_lease import (
    ExecutionLeaseStore,
    LeaseOwnershipError,
    LeaseToken,
)


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def _store(db_path: Path, clock: _Clock) -> ExecutionLeaseStore:
    result = await run_migrations(db_path)
    assert result.errors == []
    assert result.current_version >= 23
    return ExecutionLeaseStore(db_path, clock=clock)


async def _acquire(
    store: ExecutionLeaseStore,
    holder_id: str,
    *,
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    resource_id: str = "run-1",
    ttl_seconds: float = 10.0,
) -> LeaseToken | None:
    return await store.acquire(
        tenant_id=tenant_id,
        owner_id=owner_id,
        resource_kind="workflow",
        resource_id=resource_id,
        holder_id=holder_id,
        ttl_seconds=ttl_seconds,
    )


@pytest.mark.asyncio
async def test_cancel_during_connection_open_closes_non_daemon_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_entered = threading.Event()
    allow_connector = threading.Event()

    def delayed_connector() -> sqlite3.Connection:
        connector_entered.set()
        if not allow_connector.wait(timeout=1.0):
            raise TimeoutError("test did not release delayed SQLite connector")
        return sqlite3.connect(tmp_path / "cancel-safe.db")

    connection = aiosqlite.Connection(delayed_connector, iter_chunk_size=64)
    monkeypatch.setattr(
        connection_module.aiosqlite,
        "connect",
        lambda _database, **_kwargs: connection,
    )
    entered_context = False

    async def open_until_cancelled() -> None:
        nonlocal entered_context
        async with open_aiosqlite_connection(tmp_path / "ignored.db"):
            entered_context = True

    task = asyncio.create_task(open_until_cancelled())
    assert await asyncio.wait_for(
        asyncio.to_thread(connector_entered.wait, 1.0),
        timeout=2.0,
    )
    task.cancel()
    allow_connector.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    connection.join(timeout=1.0)
    assert entered_context is False
    assert connection.is_alive() is False


@pytest.mark.asyncio
async def test_two_stores_compete_atomically_and_scope_is_part_of_the_key(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    first = await _store(db_path, clock)
    second = ExecutionLeaseStore(db_path, clock=clock)

    contenders = await asyncio.gather(
        _acquire(first, "process-a"),
        _acquire(second, "process-b"),
    )
    winners = [token for token in contenders if token is not None]
    assert len(winners) == 1
    assert winners[0].fence_token == 1

    other_owner = await _acquire(second, "process-b", owner_id="owner-b")
    other_tenant = await _acquire(second, "process-b", tenant_id="tenant-b")
    assert other_owner is not None
    assert other_tenant is not None
    assert other_owner.fence_token == other_tenant.fence_token == 1


@pytest.mark.asyncio
async def test_expired_lease_is_taken_over_with_higher_fence_and_old_token_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    first = await _store(db_path, clock)
    second = ExecutionLeaseStore(db_path, clock=clock)
    old_token = await _acquire(first, "process-a", ttl_seconds=5.0)
    assert old_token is not None
    assert await _acquire(second, "process-b") is None

    clock.now = old_token.expires_at
    new_token = await _acquire(second, "process-b")
    assert new_token is not None
    assert new_token.fence_token == old_token.fence_token + 1
    assert await first.check_owned(old_token) is False
    assert await first.renew(old_token, ttl_seconds=10.0) is None
    assert await first.release(old_token) is False
    with pytest.raises(LeaseOwnershipError):
        await first.assert_owned(old_token)
    await second.assert_owned(new_token)


@pytest.mark.asyncio
async def test_renew_cannot_resurrect_an_expired_term(tmp_path: Path) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    store = await _store(db_path, clock)
    token = await _acquire(store, "process-a", ttl_seconds=2.0)
    assert token is not None

    clock.now = token.expires_at
    assert await store.renew(token, ttl_seconds=20.0) is None
    assert await store.check_owned(token) is False

    reacquired = await _acquire(store, "process-a")
    assert reacquired is not None
    assert reacquired.fence_token == token.fence_token + 1


@pytest.mark.asyncio
async def test_active_term_cannot_be_reacquired_even_by_the_same_holder(tmp_path: Path) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    store = await _store(db_path, clock)
    token = await _acquire(store, "process-a")
    assert token is not None

    assert await _acquire(store, "process-a") is None
    assert await store.check_owned(token) is True


@pytest.mark.asyncio
async def test_release_requires_exact_holder_and_fence_token(tmp_path: Path) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    store = await _store(db_path, clock)
    token = await _acquire(store, "process-a")
    assert token is not None

    assert await store.release(replace(token, holder_id="process-b")) is False
    assert await store.release(replace(token, fence_token=token.fence_token + 1)) is False
    assert await store.check_owned(token) is True
    assert await store.release(token) is True
    assert await store.release(token) is False
    assert await store.check_owned(token) is False

    reacquired = await _acquire(store, "process-b")
    assert reacquired is not None
    assert reacquired.fence_token == token.fence_token + 1


@pytest.mark.asyncio
async def test_caller_owned_connection_keeps_fence_check_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "leases.db"
    clock = _Clock()
    store = await _store(db_path, clock)

    async with aiosqlite.connect(str(db_path)) as conn:
        await configure_aiosqlite_connection(conn)
        token = await store.acquire(
            tenant_id="tenant-a",
            owner_id="owner-a",
            resource_kind="agent",
            resource_id="task-1",
            holder_id="process-a",
            ttl_seconds=10.0,
            conn=conn,
        )
        assert token is not None
        assert conn.in_transaction is True
        await store.assert_owned(token, conn=conn)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS fenced_effect (fence_token INTEGER NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO fenced_effect (fence_token) VALUES (?)",
            (token.fence_token,),
        )
        await conn.commit()

    assert await store.check_owned(token) is True
