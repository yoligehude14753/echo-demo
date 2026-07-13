"""有界 principal-scoped runtime registry。"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class RuntimeCapacityExceeded(RuntimeError):
    """所有 runtime 都被活跃请求占用，无法安全驱逐。"""


@dataclass(slots=True)
class ScopeRuntime:
    """同一 principal scope 下的惰性组件容器。"""

    components: dict[str, object] = field(default_factory=dict)

    def get_or_create(self, name: str, factory: Callable[[], V]) -> V:
        current = self.components.get(name)
        if current is None:
            current = factory()
            self.components[name] = current
        return current  # type: ignore[return-value]

    def get(self, name: str) -> object | None:
        return self.components.get(name)

    def pop(self, name: str) -> object | None:
        return self.components.pop(name, None)

    async def aclose(self) -> None:
        values = list({id(value): value for value in self.components.values()}.values())
        self.components.clear()
        for value in reversed(values):
            await _close_runtime_value(value)


async def _close_runtime_value(value: object) -> None:
    for method_name in ("stop_watchdog", "aclose", "close"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        result = method()
        if inspect.isawaitable(result):
            await result
        return


@dataclass(slots=True)
class _Entry(Generic[V]):
    value: V
    last_used: float
    active_leases: int = 0
    terminal: bool = False


class RuntimeLease(Generic[K, V]):
    def __init__(self, registry: ScopedRuntimeRegistry[K, V], key: K, value: V) -> None:
        self._registry = registry
        self._key = key
        self.value = value
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._registry.release(self._key)

    def __enter__(self) -> V:
        return self.value

    def __exit__(self, *_args: object) -> None:
        self.release()


class ScopedRuntimeRegistry(Generic[K, V]):
    """LRU + idle TTL registry，活跃租约绝不被驱逐。"""

    def __init__(
        self,
        *,
        max_entries: int,
        idle_ttl_s: float,
        factory: Callable[[], V],
        close: Callable[[V], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1 or idle_ttl_s <= 0:
            raise ValueError("runtime registry bounds must be positive")
        self.max_entries = max_entries
        self.idle_ttl_s = idle_ttl_s
        self._factory = factory
        self._close = close
        self._clock = clock
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._lock = threading.RLock()
        self._pending_close: list[V] = []

    def _queue_close(self, value: V) -> None:
        self._pending_close.append(value)

    def _pop_entry(self, key: K) -> V | None:
        entry = self._entries.pop(key, None)
        return entry.value if entry is not None else None

    def _evict_one(self) -> bool:
        for key, entry in self._entries.items():
            if entry.active_leases == 0:
                value = self._pop_entry(key)
                if value is not None:
                    self._queue_close(value)
                return True
        return False

    def get_or_create(self, key: K) -> V:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                if len(self._entries) >= self.max_entries and not self._evict_one():
                    raise RuntimeCapacityExceeded("all scoped runtimes are active")
                entry = _Entry(value=self._factory(), last_used=self._clock())
            else:
                entry.last_used = self._clock()
            self._entries[key] = entry
            return entry.value

    def peek(self, key: K) -> V | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.value if entry is not None else None

    def acquire(self, key: K) -> RuntimeLease[K, V]:
        value = self.get_or_create(key)
        with self._lock:
            entry = self._entries[key]
            entry.active_leases += 1
            entry.last_used = self._clock()
        return RuntimeLease(self, key, value)

    def release(self, key: K) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.active_leases = max(0, entry.active_leases - 1)
            entry.last_used = self._clock()
            if entry.terminal and entry.active_leases == 0:
                value = self._pop_entry(key)
                if value is not None:
                    self._queue_close(value)

    async def mark_terminal(self, key: K) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.terminal = True
                if entry.active_leases == 0:
                    value = self._pop_entry(key)
                    if value is not None:
                        self._queue_close(value)
        await self.flush_closures()

    async def sweep(self) -> int:
        now = self._clock()
        removed = 0
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.active_leases or now - entry.last_used < self.idle_ttl_s:
                    continue
                value = self._pop_entry(key)
                if value is not None:
                    self._queue_close(value)
                    removed += 1
        await self.flush_closures()
        return removed

    async def flush_closures(self) -> None:
        with self._lock:
            pending, self._pending_close = self._pending_close, []
        for value in pending:
            if self._close is not None:
                await self._close(value)
            else:
                await _close_runtime_value(value)

    async def remove_component_all(self, name: str) -> None:
        with self._lock:
            values = [
                value
                for runtime in (entry.value for entry in self._entries.values())
                if isinstance(runtime, ScopeRuntime) and (value := runtime.pop(name)) is not None
            ]
        for value in values:
            await _close_runtime_value(value)

    def remove_component_all_for_test(self, name: str) -> None:
        with self._lock:
            for entry in self._entries.values():
                if isinstance(entry.value, ScopeRuntime):
                    entry.value.pop(name)

    async def aclose(self) -> None:
        with self._lock:
            values = [entry.value for entry in self._entries.values()]
            self._entries.clear()
            values.extend(self._pending_close)
            self._pending_close = []
        for value in values:
            if self._close is not None:
                await self._close(value)
            else:
                await _close_runtime_value(value)

    def clear_for_test(self) -> None:
        with self._lock:
            self._entries.clear()
            self._pending_close.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def active_leases(self, key: K) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return entry.active_leases if entry is not None else 0


async def run_registry_janitor(
    registry: ScopedRuntimeRegistry[object, object],
    *,
    interval_s: float,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await registry.sweep()


__all__ = [
    "RuntimeCapacityExceeded",
    "RuntimeLease",
    "ScopeRuntime",
    "ScopedRuntimeRegistry",
    "run_registry_janitor",
]
