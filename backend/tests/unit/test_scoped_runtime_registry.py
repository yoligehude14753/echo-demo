from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.runtime import RuntimeCapacityExceeded, ScopedRuntimeRegistry


@dataclass
class _Runtime:
    serial: int


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_is_lru_bounded_and_closes_evicted_runtime() -> None:
    serial = 0
    closed: list[int] = []

    def factory() -> _Runtime:
        nonlocal serial
        serial += 1
        return _Runtime(serial)

    async def close(runtime: _Runtime) -> None:
        closed.append(runtime.serial)

    registry = ScopedRuntimeRegistry[str, _Runtime](
        max_entries=2,
        idle_ttl_s=60,
        factory=factory,
        close=close,
    )
    assert registry.get_or_create("a").serial == 1
    assert registry.get_or_create("b").serial == 2
    registry.get_or_create("a")
    assert registry.get_or_create("c").serial == 3
    await registry.flush_closures()

    assert registry.peek("a") is not None
    assert registry.peek("b") is None
    assert closed == [2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_never_evicts_active_leases_and_releases_terminal_scope() -> None:
    closed: list[str] = []

    async def close(runtime: str) -> None:
        closed.append(runtime)

    values = iter(["runtime-a", "runtime-b"])
    registry = ScopedRuntimeRegistry[str, str](
        max_entries=1,
        idle_ttl_s=60,
        factory=lambda: next(values),
        close=close,
    )
    lease = registry.acquire("a")
    with pytest.raises(RuntimeCapacityExceeded):
        registry.get_or_create("b")

    await registry.mark_terminal("a")
    assert registry.peek("a") == "runtime-a"
    lease.release()
    await registry.flush_closures()
    assert registry.peek("a") is None
    assert closed == ["runtime-a"]
    assert registry.get_or_create("b") == "runtime-b"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_removes_only_idle_unleased_scopes() -> None:
    now = 0.0
    registry = ScopedRuntimeRegistry[str, str](
        max_entries=4,
        idle_ttl_s=10,
        factory=lambda: "runtime",
        close=lambda _runtime: _async_noop(),
        clock=lambda: now,
    )
    registry.get_or_create("idle")
    active = registry.acquire("active")
    now = 11.0
    assert await registry.sweep() == 1
    assert registry.peek("idle") is None
    assert registry.peek("active") == "runtime"
    active.release()
    now = 22.0
    assert await registry.sweep() == 1


async def _async_noop() -> None:
    return None
