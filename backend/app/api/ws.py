"""Principal-scoped WebSocket event stream protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.adapters.event_bus.inmemory import (
    EventStreamCapacityExceeded,
    FencedEventSubscription,
    InMemoryEventBus,
    SlowConsumerError,
)
from app.api.deps import get_access_policy, get_event_bus, get_quota_governor
from app.config import Settings, get_settings
from app.schemas.events import (
    WS_PROTOCOL_VERSION,
    WS_SERVER_PING_INTERVAL_S,
    ClientHello,
    EchoEvent,
)
from app.security import (
    AccessPolicy,
    AccessPolicyError,
    Principal,
    SessionError,
    route_scope_path,
)
from app.security.access import PreAuthAdmissionError
from app.security.client_version import (
    MINIMUM_PUBLIC_CLIENT_VERSION,
    is_supported_public_client,
)
from app.security.context import bind_principal, reset_principal
from app.security.governor import PrincipalGovernor, QuotaExceeded
from app.security.public_projection import project_client_dict, server_private_roots

router = APIRouter(tags=["ws"])
logger = logging.getLogger(__name__)

_HELLO_TIMEOUT_S = 3.0
_MAX_CLIENT_FRAME_BYTES = 4096
_AUTH_CLOSE_CODE = 4401
_PROTOCOL_CLOSE_CODE = 4408
_UPGRADE_CLOSE_CODE = 4426
_SLOW_CONSUMER_CLOSE_CODE = 4409
_ADMISSION_CLOSE_CODE = 4429
_TEMPORARY_FAILURE_CLOSE_CODE = 1013


@dataclass(frozen=True, slots=True)
class _Handshake:
    hello: ClientHello
    legacy_ping: bool = False


class _ClientFrameProtocolError(RuntimeError):
    """One bounded client frame is missing, oversized, or malformed."""


class _WebSocketSendTimeout(RuntimeError):
    """One bounded server-to-client WebSocket write exceeded its deadline."""


async def _send_text(websocket: WebSocket, payload: str, *, timeout_s: float) -> None:
    try:
        await asyncio.wait_for(websocket.send_text(payload), timeout=timeout_s)
    except TimeoutError as exc:
        raise _WebSocketSendTimeout("websocket send timed out") from exc


async def _close_websocket(
    websocket: WebSocket,
    *,
    code: int,
    reason: str | None,
    timeout_s: float,
) -> None:
    with contextlib.suppress(TimeoutError, RuntimeError, WebSocketDisconnect):
        await asyncio.wait_for(
            websocket.close(code=code, reason=reason),
            timeout=timeout_s,
        )


def _decode_client_frame(frame: Mapping[str, Any], *, frame_name: str) -> str:
    if frame["type"] == "websocket.disconnect":
        code = frame.get("code")
        raise WebSocketDisconnect(code=code if isinstance(code, int) else 1000)
    raw = frame.get("text")
    if raw is None:
        raw = frame.get("bytes")
    if isinstance(raw, bytes):
        if len(raw) > _MAX_CLIENT_FRAME_BYTES:
            raise _ClientFrameProtocolError(f"{frame_name} too large")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ClientFrameProtocolError(f"{frame_name} must be UTF-8") from exc
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > _MAX_CLIENT_FRAME_BYTES:
            raise _ClientFrameProtocolError(f"{frame_name} too large")
        return raw
    raise _ClientFrameProtocolError(f"{frame_name} payload required")


async def _wait_client_hello(
    websocket: WebSocket,
    *,
    require_hello: bool,
) -> _Handshake:
    try:
        frame = await asyncio.wait_for(websocket.receive(), timeout=_HELLO_TIMEOUT_S)
    except TimeoutError:
        if require_hello:
            raise _ClientFrameProtocolError("client_hello timeout") from None
        return _Handshake(ClientHello())
    message = _decode_client_frame(frame, frame_name="client_hello")
    if message.strip() == "ping":
        if require_hello:
            raise _ClientFrameProtocolError("client_hello required")
        return _Handshake(ClientHello(client_version="legacy"), legacy_ping=True)
    try:
        payload = json.loads(message)
        hello = ClientHello.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        if require_hello:
            raise _ClientFrameProtocolError("invalid client_hello") from exc
        return _Handshake(ClientHello())
    return _Handshake(hello)


async def _authenticate(
    websocket: WebSocket,
    handshake: _Handshake,
    settings: Settings,
    policy: AccessPolicy,
) -> Principal:
    if settings.public_demo_mode:
        if not is_supported_public_client(handshake.hello.client_version):
            raise AccessPolicyError("client upgrade required", status_code=426)
        auth = handshake.hello.auth
        if auth is None:
            raise AccessPolicyError("client_hello bearer required", status_code=401)
        authorization = f"Bearer {auth.token}"
        query_token = ""
    else:
        authorization = (
            handshake.hello.authorization or websocket.headers.get("authorization") or ""
        )
        query_token = websocket.query_params.get("session", "")
    return await policy.resolve_websocket_principal(
        client_host=policy.client_host(websocket.client),
        path=route_scope_path(websocket.scope),
        authorization=authorization,
        query_token=query_token,
    )


def _sync_frame(subscription: FencedEventSubscription, *, reason: str) -> EchoEvent:
    state = subscription.state
    return EchoEvent(
        type="server_sync",
        seq=state.max_seq,
        stream_epoch=state.epoch,
        payload={
            "strategy": "replace",
            "reason": reason,
            "fence_seq": state.max_seq,
            "stream_epoch": state.epoch,
            "resources": {
                "meetings": "/meetings",
                "current_meeting": "/meetings/current",
                "workflows": "/workflows/runs",
                "artifacts": "/artifacts",
            },
        },
    )


async def _send_handshake_frames(
    websocket: WebSocket,
    subscription: FencedEventSubscription,
    handshake: _Handshake,
    *,
    send_timeout_s: float,
) -> None:
    hello = handshake.hello
    state = subscription.state
    gap_reason = subscription.gap_reason
    if gap_reason is not None:
        await _send_text(
            websocket,
            EchoEvent(
                type="server_resync",
                stream_epoch=state.epoch,
                payload={
                    "reason": gap_reason,
                    "oldest_seq": state.oldest_seq,
                    "max_seq": state.max_seq,
                    "fence_seq": state.max_seq,
                    "client_last_seq": subscription.requested_seq,
                    "client_stream_epoch": hello.stream_epoch,
                },
            ).model_dump_json(),
            timeout_s=send_timeout_s,
        )
        await _send_text(
            websocket,
            _sync_frame(subscription, reason=gap_reason).model_dump_json(),
            timeout_s=send_timeout_s,
        )
    await _send_text(
        websocket,
        EchoEvent(
            type="server_hello",
            stream_epoch=state.epoch,
            payload={
                "max_seq": state.max_seq,
                "stream_epoch": state.epoch,
                "version": WS_PROTOCOL_VERSION,
                "client_version": hello.client_version,
            },
        ).model_dump_json(),
        timeout_s=send_timeout_s,
    )
    if handshake.legacy_ping:
        await _send_text(
            websocket,
            EchoEvent(
                type="server_ping",
                stream_epoch=state.epoch,
                payload={"max_seq": state.max_seq},
            ).model_dump_json(),
            timeout_s=send_timeout_s,
        )


async def _send_ping(
    websocket: WebSocket,
    bus: InMemoryEventBus,
    *,
    send_timeout_s: float,
) -> None:
    state = bus.stream_state_for_current_scope()
    await _send_text(
        websocket,
        EchoEvent(
            type="server_ping",
            stream_epoch=state.epoch,
            payload={"max_seq": state.max_seq},
        ).model_dump_json(),
        timeout_s=send_timeout_s,
    )


async def _receive_authenticated_client_frame(
    websocket: WebSocket,
    bus: InMemoryEventBus,
    *,
    admit_frame: Callable[[], Awaitable[None]] | None,
    revalidate: Callable[[], Awaitable[None]] | None,
    send_timeout_s: float,
) -> None:
    frame = await websocket.receive()
    message = _decode_client_frame(frame, frame_name="client frame")
    if admit_frame is not None:
        await admit_frame()
    if message.strip() == "ping":
        await _send_ping(websocket, bus, send_timeout_s=send_timeout_s)
        return
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") != "client_ping":
        return
    if revalidate is not None:
        await revalidate()
    await _send_ping(websocket, bus, send_timeout_s=send_timeout_s)


async def _run_stream(
    websocket: WebSocket,
    bus: InMemoryEventBus,
    subscription: FencedEventSubscription,
    *,
    principal: Principal,
    private_roots: tuple[str, ...] = (),
    admit_frame: Callable[[], Awaitable[None]] | None = None,
    revalidate: Callable[[], Awaitable[None]] | None = None,
    revalidate_interval_s: float = WS_SERVER_PING_INTERVAL_S,
    send_timeout_s: float,
) -> int:
    async def sender() -> None:
        async for event in subscription:
            await _send_text(
                websocket,
                json.dumps(
                    project_client_dict(
                        event.model_dump(mode="json"),
                        principal,
                        private_roots=private_roots,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timeout_s=send_timeout_s,
            )

    async def ping_loop() -> None:
        while True:
            await asyncio.sleep(WS_SERVER_PING_INTERVAL_S)
            await _send_ping(websocket, bus, send_timeout_s=send_timeout_s)

    async def auth_loop() -> None:
        while True:
            await asyncio.sleep(revalidate_interval_s)
            if revalidate is not None:
                await revalidate()

    async def receiver() -> None:
        while True:
            await _receive_authenticated_client_frame(
                websocket,
                bus,
                admit_frame=admit_frame,
                revalidate=revalidate,
                send_timeout_s=send_timeout_s,
            )

    tasks = [
        asyncio.create_task(sender(), name="ws-sender"),
        asyncio.create_task(ping_loop(), name="ws-ping"),
        asyncio.create_task(receiver(), name="ws-receiver"),
    ]
    if revalidate is not None:
        tasks.append(asyncio.create_task(auth_loop(), name="ws-auth-revalidate"))
    close_code = 1000
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except SlowConsumerError:
                close_code = _SLOW_CONSUMER_CLOSE_CODE
            except _WebSocketSendTimeout:
                close_code = _TEMPORARY_FAILURE_CLOSE_CODE
            except _ClientFrameProtocolError:
                close_code = _PROTOCOL_CLOSE_CODE
            except QuotaExceeded:
                close_code = _ADMISSION_CLOSE_CODE
            except SessionError:
                close_code = _AUTH_CLOSE_CODE
            except (RuntimeError, WebSocketDisconnect):
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await subscription.aclose()
    return close_code


def _handshake_close_details(
    exc: _ClientFrameProtocolError | AccessPolicyError | SessionError,
) -> tuple[int, str]:
    if isinstance(exc, _ClientFrameProtocolError):
        return _PROTOCOL_CLOSE_CODE, "invalid client_hello"
    if isinstance(exc, SessionError):
        return _AUTH_CLOSE_CODE, "client_hello bearer required"
    if exc.status_code == 403:
        return 4403, exc.detail
    if exc.status_code == 426:
        return _UPGRADE_CLOSE_CODE, (f"client upgrade required:{MINIMUM_PUBLIC_CLIENT_VERSION}")
    return _AUTH_CLOSE_CODE, exc.detail


async def _open_authenticated_websocket(
    websocket: WebSocket,
    settings: Settings,
    policy: AccessPolicy,
) -> tuple[Principal, _Handshake] | None:
    """Apply transport admission and return only a fully authenticated socket."""

    client_key = policy.client_host(websocket.client)
    try:
        preauth_lease = await policy.admit_websocket(client_key)
    except PreAuthAdmissionError as exc:
        await _close_websocket(
            websocket,
            code=_ADMISSION_CLOSE_CODE,
            reason=exc.detail,
            timeout_s=settings.ws_send_timeout_s,
        )
        return None

    local_principal: Principal | None = None
    try:
        policy.require_allowed_origin(
            websocket.headers.getlist("origin"),
            client_host=client_key,
        )
        if not settings.public_demo_mode:
            local_principal = await policy.resolve_websocket_principal(
                client_host=client_key,
                path=route_scope_path(websocket.scope),
                authorization=websocket.headers.get("authorization", ""),
                query_token=websocket.query_params.get("session", ""),
            )
        await websocket.accept()
        handshake = await _wait_client_hello(
            websocket,
            require_hello=settings.public_demo_mode,
        )
        principal = local_principal or await _authenticate(websocket, handshake, settings, policy)
        return principal, handshake
    except WebSocketDisconnect:
        return None
    except (_ClientFrameProtocolError, AccessPolicyError, SessionError) as exc:
        code, reason = _handshake_close_details(exc)
        await _close_websocket(
            websocket,
            code=code,
            reason=reason,
            timeout_s=settings.ws_send_timeout_s,
        )
        return None
    finally:
        if preauth_lease is not None:
            await preauth_lease.release()


@router.websocket("/ws/echo")
async def ws_echo(  # noqa: PLR0912 - keep close-code and cleanup paths explicit
    websocket: WebSocket,
    bus: InMemoryEventBus = Depends(get_event_bus),
    settings: Settings = Depends(get_settings),
    policy: AccessPolicy = Depends(get_access_policy),
    governor: PrincipalGovernor = Depends(get_quota_governor),
) -> None:
    authenticated = await _open_authenticated_websocket(websocket, settings, policy)
    if authenticated is None:
        return
    principal, handshake = authenticated

    context_token = bind_principal(principal)
    ws_lease = None
    subscription: FencedEventSubscription | None = None
    close_code = 1000
    close_reason: str | None = None
    try:
        try:
            ws_lease = await governor.websocket(principal)
        except QuotaExceeded:
            close_code = _ADMISSION_CLOSE_CODE
            close_reason = "websocket quota exceeded"
            return
        hello = handshake.hello
        try:
            subscription = await bus.open_fenced_subscription(
                last_seq=hello.last_seq,
                stream_epoch=hello.stream_epoch,
                start_at_latest=bool(hello.client_version and "no-replay" in hello.client_version),
            )
        except EventStreamCapacityExceeded:
            close_code = _ADMISSION_CLOSE_CODE
            close_reason = "event stream capacity exceeded"
            return
        try:
            await _send_handshake_frames(
                websocket,
                subscription,
                handshake,
                send_timeout_s=settings.ws_send_timeout_s,
            )
        except _WebSocketSendTimeout:
            close_code = _TEMPORARY_FAILURE_CLOSE_CODE
            close_reason = "websocket send timeout"
            return

        async def revalidate() -> None:
            await policy.sessions.assert_active_principal(principal)

        async def admit_frame() -> None:
            await governor.admit_websocket_frame(principal)

        close_code = await _run_stream(
            websocket,
            bus,
            subscription,
            principal=principal,
            private_roots=server_private_roots(settings),
            admit_frame=admit_frame if principal.mode == "public" else None,
            revalidate=revalidate if principal.mode == "public" else None,
            revalidate_interval_s=settings.ws_auth_revalidate_interval_s,
            send_timeout_s=settings.ws_send_timeout_s,
        )
        if close_code == _SLOW_CONSUMER_CLOSE_CODE:
            close_reason = "slow consumer"
        elif close_code == _TEMPORARY_FAILURE_CLOSE_CODE:
            close_reason = "websocket send timeout"
        elif close_code == _AUTH_CLOSE_CODE:
            close_reason = "session no longer active"
        elif close_code == _PROTOCOL_CLOSE_CODE:
            close_reason = "invalid client frame"
        elif close_code == _ADMISSION_CLOSE_CODE:
            close_reason = "websocket frame rate exceeded"
    except WebSocketDisconnect:
        return
    finally:
        if subscription is not None:
            await subscription.aclose()
        if ws_lease is not None:
            ws_lease.release()
        reset_principal(context_token)
        await _close_websocket(
            websocket,
            code=close_code,
            reason=close_reason,
            timeout_s=settings.ws_send_timeout_s,
        )


__all__ = ["router", "ws_echo"]
