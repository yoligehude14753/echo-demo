from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.api.ws import ws_echo
from app.config import Settings
from app.schemas.events import EchoEvent
from app.security.access import AccessPolicy
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.models import Principal
from app.security.sessions import SessionStore
from fastapi import WebSocket, WebSocketDisconnect
from starlette.datastructures import Headers, QueryParams


class _Sessions:
    def __init__(self, principals: dict[str, Principal]) -> None:
        self._principals = principals

    async def validate_public_token(self, token: str) -> Principal:
        return self._principals[token]

    async def assert_active_principal(self, principal: Principal) -> None:
        assert principal in self._principals.values()


class _FakeWebSocket:
    def __init__(self, token: str, *, block_after_sends: int | None = None) -> None:
        hello = {
            "type": "client_hello",
            "last_seq": 0,
            "client_version": "0.3.3",
            "auth": {"type": "bearer", "token": token},
        }
        self.headers = Headers()
        self.query_params = QueryParams()
        self.scope: dict[str, object] = {
            "type": "websocket",
            "path": "/ws/echo",
            "root_path": "",
        }
        self.client = SimpleNamespace(host="testclient")
        self._hello_frame: dict[str, object] | None = {
            "type": "websocket.receive",
            "text": json.dumps(hello),
        }
        self._disconnect = asyncio.Event()
        self._never_send = asyncio.Event()
        self._block_after_sends = block_after_sends
        self.send_attempts = 0
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str | None]] = []
        self.server_hello_sent = asyncio.Event()
        self.blocked_send_started = asyncio.Event()

    async def accept(self) -> None:
        return

    async def receive(self) -> dict[str, object]:
        if self._hello_frame is None:
            await self._disconnect.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        frame = self._hello_frame
        self._hello_frame = None
        return frame

    async def receive_text(self) -> str:
        await self._disconnect.wait()
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, payload: str) -> None:
        self.send_attempts += 1
        if self._block_after_sends is not None and self.send_attempts > self._block_after_sends:
            self.blocked_send_started.set()
            await self._never_send.wait()
            return
        message = cast(dict[str, object], json.loads(payload))
        self.sent.append(message)
        if message.get("type") == "server_hello":
            self.server_hello_sent.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))
        self._disconnect.set()

    def disconnect(self) -> None:
        self._disconnect.set()


def _principal(name: str) -> Principal:
    return Principal(
        tenant_id=f"tenant-{name}",
        device_id=f"device-{name}",
        owner_id=f"owner-{name}",
        session_id=f"session-{name}",
        mode="public",
    )


def _runtime(
    tmp_path: Path,
    principals: dict[str, Principal],
    *,
    max_scope_streams: int,
    send_timeout_s: float = 0.05,
    admission_queue_size: int = 8,
    admission_wait_timeout_s: float = 0.01,
    quota_websocket_connections: int = 1,
) -> tuple[Settings, AccessPolicy, PrincipalGovernor, InMemoryEventBus]:
    settings = Settings(
        db_path=tmp_path / "ws.db",
        public_demo_mode=True,
        quota_websocket_connections=quota_websocket_connections,
        ws_scope_max_streams=max_scope_streams,
        ws_admission_queue_size=admission_queue_size,
        ws_admission_wait_timeout_s=admission_wait_timeout_s,
        ws_send_timeout_s=send_timeout_s,
        _env_file=None,  # type: ignore[call-arg]
    )
    sessions = _Sessions(principals)
    policy = AccessPolicy(settings, cast(SessionStore, sessions))
    governor = PrincipalGovernor(settings)
    bus = InMemoryEventBus(
        max_scope_streams=max_scope_streams,
        max_admission_waiters=admission_queue_size,
        admission_wait_timeout_s=admission_wait_timeout_s,
    )
    return settings, policy, governor, bus


async def _serve(
    websocket: _FakeWebSocket,
    bus: InMemoryEventBus,
    settings: Settings,
    policy: AccessPolicy,
    governor: PrincipalGovernor,
) -> None:
    await ws_echo(
        cast(WebSocket, websocket),
        bus,
        settings,
        policy,
        governor,
    )


async def _wait_for_admission_waiters(bus: InMemoryEventBus, expected: int) -> None:
    async with asyncio.timeout(1.0):
        await bus.wait_for_admission_waiter_count(expected)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_capacity_closes_b_then_allows_b_after_a_releases(tmp_path: Path) -> None:
    principal_a = _principal("a")
    principal_b = _principal("b")
    settings, policy, governor, bus = _runtime(
        tmp_path,
        {"token-a": principal_a, "token-b": principal_b},
        max_scope_streams=1,
    )

    socket_a = _FakeWebSocket("token-a")
    task_a = asyncio.create_task(
        _serve(socket_a, bus, settings, policy, governor),
        name="test-ws-scope-a",
    )
    try:
        await asyncio.wait_for(socket_a.server_hello_sent.wait(), timeout=1.0)
        assert bus.subscriber_count() == 1

        socket_b_rejected = _FakeWebSocket("token-b")
        await asyncio.wait_for(
            _serve(socket_b_rejected, bus, settings, policy, governor),
            timeout=1.0,
        )
        assert socket_b_rejected.closed == [(4429, "event stream capacity exceeded")]
        assert bus.subscriber_count() == 1

        released_b_lease = await governor.websocket(principal_b)
        released_b_lease.release()

        socket_a.disconnect()
        await asyncio.wait_for(task_a, timeout=1.0)
        assert bus.subscriber_count() == 0

        released_a_lease = await governor.websocket(principal_a)
        released_a_lease.release()

        socket_b = _FakeWebSocket("token-b")
        task_b = asyncio.create_task(
            _serve(socket_b, bus, settings, policy, governor),
            name="test-ws-scope-b",
        )
        try:
            await asyncio.wait_for(socket_b.server_hello_sent.wait(), timeout=1.0)
            assert bus.subscriber_count() == 1
        finally:
            socket_b.disconnect()
            await asyncio.wait_for(task_b, timeout=1.0)
        assert bus.subscriber_count() == 0
    finally:
        if not task_a.done():
            socket_a.disconnect()
            await asyncio.gather(task_a, return_exceptions=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_admission_serves_distinct_principals_in_fifo_order(tmp_path: Path) -> None:
    principal_a = _principal("fair-a")
    principal_b = _principal("fair-b")
    principal_c = _principal("fair-c")
    settings, policy, governor, bus = _runtime(
        tmp_path,
        {
            "token-a": principal_a,
            "token-b": principal_b,
            "token-c": principal_c,
        },
        max_scope_streams=1,
        admission_queue_size=2,
        admission_wait_timeout_s=0.5,
    )
    sockets = {name: _FakeWebSocket(f"token-{name}") for name in ("a", "b", "c")}
    tasks: dict[str, asyncio.Task[None]] = {}
    try:
        tasks["a"] = asyncio.create_task(
            _serve(sockets["a"], bus, settings, policy, governor),
            name="test-ws-fair-a",
        )
        await asyncio.wait_for(sockets["a"].server_hello_sent.wait(), timeout=1.0)

        tasks["b"] = asyncio.create_task(
            _serve(sockets["b"], bus, settings, policy, governor),
            name="test-ws-fair-b",
        )
        await _wait_for_admission_waiters(bus, 1)
        tasks["c"] = asyncio.create_task(
            _serve(sockets["c"], bus, settings, policy, governor),
            name="test-ws-fair-c",
        )
        await _wait_for_admission_waiters(bus, 2)

        sockets["a"].disconnect()
        await asyncio.wait_for(tasks["a"], timeout=1.0)
        await asyncio.wait_for(sockets["b"].server_hello_sent.wait(), timeout=1.0)
        assert not sockets["c"].server_hello_sent.is_set()
        assert bus.admission_waiter_count == 1

        sockets["b"].disconnect()
        await asyncio.wait_for(tasks["b"], timeout=1.0)
        await asyncio.wait_for(sockets["c"].server_hello_sent.wait(), timeout=1.0)
        assert bus.admission_waiter_count == 0
    finally:
        for socket in sockets.values():
            socket.disconnect()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_admission_queue_is_bounded_and_one_waiter_per_principal(
    tmp_path: Path,
) -> None:
    principal_a = _principal("bounded-a")
    principal_b = _principal("bounded-b")
    principal_c = _principal("bounded-c")
    settings, policy, governor, bus = _runtime(
        tmp_path,
        {
            "token-a": principal_a,
            "token-b": principal_b,
            "token-b-duplicate": principal_b,
            "token-c": principal_c,
        },
        max_scope_streams=1,
        admission_queue_size=1,
        admission_wait_timeout_s=0.5,
        quota_websocket_connections=3,
    )
    socket_a = _FakeWebSocket("token-a")
    socket_b = _FakeWebSocket("token-b")
    task_a = asyncio.create_task(
        _serve(socket_a, bus, settings, policy, governor),
        name="test-ws-bounded-a",
    )
    task_b: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(socket_a.server_hello_sent.wait(), timeout=1.0)
        task_b = asyncio.create_task(
            _serve(socket_b, bus, settings, policy, governor),
            name="test-ws-bounded-b",
        )
        await _wait_for_admission_waiters(bus, 1)

        duplicate = _FakeWebSocket("token-b-duplicate")
        await asyncio.wait_for(
            _serve(duplicate, bus, settings, policy, governor),
            timeout=1.0,
        )
        assert duplicate.closed == [(4429, "event stream capacity exceeded")]
        assert bus.admission_waiter_count == 1

        overflow = _FakeWebSocket("token-c")
        await asyncio.wait_for(
            _serve(overflow, bus, settings, policy, governor),
            timeout=1.0,
        )
        assert overflow.closed == [(4429, "event stream capacity exceeded")]
        assert bus.admission_waiter_count == 1

        socket_a.disconnect()
        await asyncio.wait_for(task_a, timeout=1.0)
        await asyncio.wait_for(socket_b.server_hello_sent.wait(), timeout=1.0)
    finally:
        socket_a.disconnect()
        socket_b.disconnect()
        if not task_a.done():
            task_a.cancel()
        if task_b is not None and not task_b.done():
            task_b.cancel()
        await asyncio.gather(
            task_a,
            *(task for task in (task_b,) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocked_event_send_times_out_and_releases_subscription_and_lease(
    tmp_path: Path,
) -> None:
    principal = _principal("blocked")
    settings, policy, governor, bus = _runtime(
        tmp_path,
        {"token-blocked": principal},
        max_scope_streams=1,
        send_timeout_s=0.02,
    )
    socket = _FakeWebSocket("token-blocked", block_after_sends=1)
    task = asyncio.create_task(
        _serve(socket, bus, settings, policy, governor),
        name="test-ws-blocked-send",
    )
    try:
        await asyncio.wait_for(socket.server_hello_sent.wait(), timeout=1.0)
        assert bus.subscriber_count() == 1
        with pytest.raises(QuotaExceeded, match="websockets"):
            await governor.websocket(principal)

        await bus.publish_to(
            (principal.tenant_id, principal.owner_id),
            EchoEvent(type="workflow.snapshot", payload={"state": "running"}),
        )
        await asyncio.wait_for(socket.blocked_send_started.wait(), timeout=1.0)
        await asyncio.wait_for(task, timeout=1.0)

        assert socket.closed == [(1013, "websocket send timeout")]
        assert bus.subscriber_count() == 0
        released_lease = await governor.websocket(principal)
        released_lease.release()
    finally:
        if not task.done():
            socket.disconnect()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
