"""Hub sync endpoints plus the desktop-local Hub lifecycle façade."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import get_request_principal, get_sync_hub_store, require_admin_access
from app.hub.runtime import HubRuntime, HubRuntimeError
from app.security.models import Principal
from app.sync_hub import (
    DeviceAlreadyExistsError,
    PairingNotFoundError,
    SyncDeviceNotFoundError,
    SyncHubStore,
)
from app.sync_hub.store import ClaimedDevice, PairingRecord, SyncDeviceRecord

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
    principal: Principal = Depends(get_request_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> list[DeviceResponse]:
    records = await store.list_devices(principal)
    return [DeviceResponse.from_record(record) for record in records]


@sync_router.delete("/devices/{device_id}", response_model=DeviceResponse)
async def revoke_device(
    device_id: str = Path(min_length=1, max_length=128),
    principal: Principal = Depends(get_request_principal),
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
async def create_pairing(request: Request) -> dict[str, Any]:
    runtime = _runtime_from_request(request)
    try:
        return await runtime.create_pairing()
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc


@host_router.post("/pairings/claim", status_code=204)
async def claim_pairing(request: Request, body: HostPairingClaimRequest) -> Response:
    runtime = _runtime_from_request(request)
    try:
        await runtime.claim_pairing(body.pairing_code)
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc
    return Response(status_code=204)


@host_router.get("/devices")
async def list_devices(request: Request) -> dict[str, Any]:
    runtime = _runtime_from_request(request)
    try:
        return {"items": await runtime.list_devices()}
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc


@host_router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(request: Request, device_id: str) -> Response:
    runtime = _runtime_from_request(request)
    try:
        await runtime.revoke_device(device_id)
    except HubRuntimeError as exc:
        raise _runtime_error(exc) from exc
    return Response(status_code=204)


# Keep the existing ``app.api.hub.router`` import stable while exposing both
# the remote sync Hub API and the desktop-local lifecycle API.
router = APIRouter()
router.include_router(sync_router)
router.include_router(host_router)


__all__ = ["host_router", "router", "sync_router"]
