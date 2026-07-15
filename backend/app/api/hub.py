"""Hub sync endpoints plus the desktop-local Hub lifecycle façade."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from app.api.deps import (
    get_access_policy,
    get_request_principal,
    get_sync_hub_store,
    require_admin_access,
)
from app.config import Settings, get_settings
from app.hub.runtime import HubRuntime, HubRuntimeError
from app.security import AccessPolicy, AccessPolicyError, SessionError, route_scope_path
from app.security.context import bind_principal, reset_principal
from app.security.models import Principal
from app.sync_hub import (
    DeviceAlreadyExistsError,
    OperationIdCollisionError,
    PairingNotFoundError,
    PushResult,
    SnapshotResult,
    SyncDeviceNotFoundError,
    SyncEntityValidationError,
    SyncHubStore,
)
from app.sync_hub.store import (
    ClaimedDevice,
    PairingRecord,
    SyncChangeRecord,
    SyncDeviceRecord,
)

sync_router = APIRouter(prefix="/hub/v1", tags=["sync-hub"])


class PairingCreateRequest(BaseModel):
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class PairingResponse(BaseModel):
    pairing_id: str
    pairing_code: str
    source_device_id: str
    expires_at: str

    @classmethod
    def from_record(cls, record: PairingRecord) -> PairingResponse:
        return cls(
            pairing_id=record.pairing_id,
            pairing_code=record.pairing_code,
            source_device_id=record.source_device_id,
            expires_at=record.expires_at.isoformat(),
        )


class PairingClaimRequest(BaseModel):
    pairing_code: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=64)


class PairingClaimResponse(BaseModel):
    device_id: str
    sync_token: str
    cursor: int

    @classmethod
    def from_record(cls, record: ClaimedDevice) -> PairingClaimResponse:
        return cls(
            device_id=record.device_id,
            sync_token=record.sync_token,
            cursor=record.cursor,
        )


class DeviceResponse(BaseModel):
    device_id: str
    device_name: str
    platform: str
    created_at: str
    last_seen_at: str
    revoked_at: str | None = None
    cursor: int

    @classmethod
    def from_record(cls, record: SyncDeviceRecord) -> DeviceResponse:
        return cls(
            device_id=record.device_id,
            device_name=record.device_name,
            platform=record.platform,
            created_at=record.created_at.isoformat(),
            last_seen_at=record.last_seen_at.isoformat(),
            revoked_at=record.revoked_at.isoformat() if record.revoked_at else None,
            cursor=record.cursor,
        )


SyncEntityType = Literal["transcript_segment", "meeting_summary", "memory"]


class SyncPushRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    entity_type: SyncEntityType
    entity_id: str = Field(min_length=1, max_length=256)
    base_revision: int = Field(default=0, ge=0)
    updated_at: datetime
    payload: dict[str, Any]


class SyncPushResponse(BaseModel):
    status: Literal["applied", "duplicate", "conflict"]
    revision: int
    cursor: int | None
    current: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, record: PushResult) -> SyncPushResponse:
        return cls(
            status=record.status,
            revision=record.revision,
            cursor=record.cursor,
            current=record.current,
        )


class SyncChangeResponse(BaseModel):
    cursor: int
    source_device_id: str
    entity_type: SyncEntityType
    entity_id: str
    revision: int
    updated_at: str
    payload: dict[str, Any]

    @classmethod
    def from_record(cls, record: SyncChangeRecord) -> SyncChangeResponse:
        return cls(
            cursor=record.cursor,
            source_device_id=record.source_device_id,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            revision=record.revision,
            updated_at=record.updated_at.isoformat(),
            payload=record.payload,
        )


class SyncChangesResponse(BaseModel):
    cursor: int
    changes: list[SyncChangeResponse]


class SyncSnapshotResponse(BaseModel):
    cursor: int
    transcript_segments: list[SyncChangeResponse]
    meeting_summaries: list[SyncChangeResponse]
    memories: list[SyncChangeResponse]

    @classmethod
    def from_record(cls, record: SnapshotResult) -> SyncSnapshotResponse:
        def convert(items: list[SyncChangeRecord]) -> list[SyncChangeResponse]:
            return [SyncChangeResponse.from_record(item) for item in items]

        return cls(
            cursor=record.cursor,
            transcript_segments=convert(record.transcript_segments),
            meeting_summaries=convert(record.meeting_summaries),
            memories=convert(record.memories),
        )


def _get_sync_gateway_principal(
    principal: Principal = Depends(get_request_principal),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Keep public-service sessions out of the paired sync gateway routes."""

    if settings.public_demo_mode and not principal.session_id.startswith("sync:"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sync token required")
    return principal


@sync_router.post(
    "/pairings",
    response_model=PairingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pairing(
    body: PairingCreateRequest | None = None,
    principal: Principal = Depends(get_request_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> PairingResponse:
    record = await store.create_pairing(
        principal,
        ttl=timedelta(seconds=body.ttl_seconds if body else 300),
    )
    return PairingResponse.from_record(record)


@sync_router.post(
    "/pairings/claim",
    response_model=PairingClaimResponse,
)
async def claim_pairing(
    body: PairingClaimRequest,
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> PairingClaimResponse:
    try:
        record = await store.claim_pairing(
            pairing_code=body.pairing_code,
            device_id=body.device_id,
            device_name=body.device_name,
            platform=body.platform,
        )
    except PairingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DeviceAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PairingClaimResponse.from_record(record)


@sync_router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    principal: Principal = Depends(_get_sync_gateway_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> list[DeviceResponse]:
    records = await store.list_devices(principal)
    return [DeviceResponse.from_record(record) for record in records]


@sync_router.delete("/devices/{device_id}", response_model=DeviceResponse)
async def revoke_device(
    device_id: str = Path(min_length=1, max_length=128),
    principal: Principal = Depends(_get_sync_gateway_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> DeviceResponse:
    try:
        record = await store.revoke_device(principal, device_id)
    except SyncDeviceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DeviceResponse.from_record(record)


host_router = APIRouter(
    prefix="/hub",
    tags=["hub"],
    dependencies=[Depends(require_admin_access)],
)


class HostPairingClaimRequest(BaseModel):
    pairing_code: str = Field(min_length=1, max_length=128)


def _runtime_error(exc: HubRuntimeError) -> HTTPException:
    messages = {
        "pairing_failed": "配对失败，请重试",
        "connection_failed": "连接失败，请检查 Hub 地址",
        "sync_failed": "同步失败，请稍后重试",
    }
    return HTTPException(
        status_code=503,
        detail=messages.get(exc.code, "同步失败，请稍后重试"),
    )


def _runtime_from_request(request: Request) -> HubRuntime:
    runtime = getattr(request.app.state, "hub_runtime", None)
    if not isinstance(runtime, HubRuntime):
        raise HTTPException(status_code=503, detail="连接失败，请检查 Hub 地址")
    return runtime


@host_router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    return _runtime_from_request(request).status()


@host_router.post("/pairings")
async def create_host_pairing(request: Request) -> dict[str, Any]:
    runtime = _runtime_from_request(request)
    try:
        return await runtime.create_pairing()
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc


@host_router.post("/pairings/claim", status_code=204)
async def claim_host_pairing(request: Request, body: HostPairingClaimRequest) -> Response:
    runtime = _runtime_from_request(request)
    try:
        await runtime.claim_pairing(body.pairing_code)
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc
    return Response(status_code=204)


@host_router.get("/devices")
async def list_host_devices(request: Request) -> dict[str, Any]:
    runtime = _runtime_from_request(request)
    try:
        return {"items": await runtime.list_devices()}
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc


@host_router.delete("/devices/{device_id}", status_code=204)
async def revoke_host_device(request: Request, device_id: str) -> Response:
    runtime = _runtime_from_request(request)
    try:
        await runtime.revoke_device(device_id)
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc
    return Response(status_code=204)


@sync_router.post(
    "/sync/push",
    response_model=SyncPushResponse,
    response_model_exclude_none=True,
)
async def push_sync(
    body: SyncPushRequest,
    principal: Principal = Depends(_get_sync_gateway_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> SyncPushResponse:
    try:
        record = await store.push(
            principal,
            operation_id=body.operation_id,
            device_id=body.device_id,
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            base_revision=body.base_revision,
            updated_at=body.updated_at,
            payload=body.payload,
        )
    except OperationIdCollisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SyncEntityValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SyncPushResponse.from_record(record)


@sync_router.get("/sync/changes", response_model=SyncChangesResponse)
async def get_sync_changes(
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(_get_sync_gateway_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> SyncChangesResponse:
    returned_cursor, records = await store.list_changes(
        principal,
        cursor=cursor,
        limit=limit,
    )
    return SyncChangesResponse(
        cursor=returned_cursor,
        changes=[SyncChangeResponse.from_record(record) for record in records],
    )


@sync_router.get("/sync/snapshot", response_model=SyncSnapshotResponse)
async def get_sync_snapshot(
    principal: Principal = Depends(_get_sync_gateway_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> SyncSnapshotResponse:
    return SyncSnapshotResponse.from_record(await store.snapshot(principal))


async def _resolve_hub_websocket_principal(
    websocket: WebSocket,
    policy: AccessPolicy,
) -> tuple[Principal, str]:
    sync_token = (
        websocket.query_params.get("sync_token", "").strip()
        or websocket.headers.get("x-echo-sync-token", "").strip()
    )
    if sync_token:
        return await policy.sessions.validate_sync_token(sync_token), sync_token
    principal = await policy.resolve_websocket_principal(
        client_host=policy.client_host(websocket.client),
        path=route_scope_path(websocket.scope),
        authorization=websocket.headers.get("authorization", ""),
        query_token=websocket.query_params.get("session", "").strip(),
    )
    return principal, ""


@sync_router.websocket("/sync/events")
async def sync_events(  # noqa: PLR0912, PLR0915 - websocket lifecycle boundary
    websocket: WebSocket,
    store: SyncHubStore = Depends(get_sync_hub_store),
    settings: Settings = Depends(get_settings),
    policy: AccessPolicy = Depends(get_access_policy),
) -> None:
    client_key = policy.client_host(websocket.client)
    admission = None
    try:
        admission = await policy.admit_websocket(client_key)
        policy.require_allowed_origin(
            websocket.headers.getlist("origin"),
            client_host=client_key,
        )
        principal, sync_token = await _resolve_hub_websocket_principal(websocket, policy)
        if settings.public_demo_mode and not sync_token:
            raise AccessPolicyError("sync token required", status_code=401)
        try:
            cursor = int(websocket.query_params.get("cursor", "0") or "0")
        except ValueError as exc:
            raise AccessPolicyError("cursor must be an integer", status_code=400) from exc
        if cursor < 0:
            raise AccessPolicyError("cursor must be non-negative", status_code=400)
        await websocket.accept()
    except WebSocketDisconnect:
        return
    except AccessPolicyError as exc:
        code = 4403 if exc.status_code == 403 else 4401
        if exc.status_code == 400:
            code = 4400
        elif exc.status_code == 429:
            code = 4429
        with suppress(RuntimeError):
            await websocket.close(code=code, reason=exc.detail)
        return
    except SessionError:
        with suppress(RuntimeError):
            await websocket.close(code=4401, reason="session required")
        return
    finally:
        if admission is not None:
            await admission.release()

    context_token = bind_principal(principal)
    try:
        while True:
            if sync_token:
                principal = await policy.sessions.validate_sync_token(sync_token)
            _, records = await store.list_changes(
                principal,
                cursor=cursor,
                limit=100,
            )
            if records:
                for record in records:
                    await asyncio.wait_for(
                        websocket.send_json(SyncChangeResponse.from_record(record).model_dump()),
                        timeout=settings.ws_send_timeout_s,
                    )
                cursor = records[-1].cursor
                continue
            await store.wait_for_change(principal, cursor=cursor, timeout_s=15.0)
    except WebSocketDisconnect:
        return
    except (AccessPolicyError, SessionError):
        with suppress(RuntimeError):
            await websocket.close(code=4401, reason="session required")
    except TimeoutError:
        with suppress(RuntimeError):
            await websocket.close(code=1011, reason="sync event send timeout")
    finally:
        reset_principal(context_token)


# Keep the existing ``app.api.hub.router`` import stable while exposing both
# the remote sync Hub API and the desktop-local lifecycle API.
router = APIRouter()
router.include_router(sync_router)
router.include_router(host_router)


__all__ = ["host_router", "router", "sync_router"]
