"""Pairing and device lifecycle endpoints for the sync hub."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.api.deps import get_request_principal, get_sync_hub_store
from app.security.models import Principal
from app.sync_hub import (
    DeviceAlreadyExistsError,
    PairingNotFoundError,
    SyncDeviceNotFoundError,
    SyncHubStore,
)
from app.sync_hub.store import ClaimedDevice, PairingRecord, SyncDeviceRecord

router = APIRouter(prefix="/hub/v1", tags=["sync-hub"])


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


@router.post(
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


@router.post(
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


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    principal: Principal = Depends(get_request_principal),
    store: SyncHubStore = Depends(get_sync_hub_store),
) -> list[DeviceResponse]:
    records = await store.list_devices(principal)
    return [DeviceResponse.from_record(record) for record in records]


@router.delete("/devices/{device_id}", response_model=DeviceResponse)
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


__all__ = ["router"]
