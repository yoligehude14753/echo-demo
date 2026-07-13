"""Stable device enrollment and rotating public-session endpoints."""

from __future__ import annotations

from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import get_access_policy, get_request_principal
from app.config import Settings, get_settings
from app.security import (
    AccessPolicy,
    DeviceCredentialError,
    DeviceIdentityAlreadyClaimedError,
    IdentityAlreadyEnrolledError,
    IssuedDeviceIdentity,
    IssuedSession,
    Principal,
    SessionIssueRateLimitError,
    local_principal,
)
from app.security.sessions import EnrollmentAdmissionLimitError

router = APIRouter(tags=["session"])


class PrincipalDTO(BaseModel):
    tenant_id: str
    user_id: str
    device_id: str
    owner_id: str
    session_id: str
    family_id: str | None
    mode: str

    @classmethod
    def from_principal(cls, principal: Principal) -> PrincipalDTO:
        return cls(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            device_id=principal.device_id,
            owner_id=principal.owner_id,
            session_id=principal.session_id,
            family_id=principal.family_id,
            mode=principal.mode,
        )


class EnrollmentRequest(BaseModel):
    enrollment_id: str = Field(min_length=32, max_length=512)
    device_secret: str = Field(min_length=32, max_length=512)
    display_name: str | None = Field(default=None, max_length=120)


class RenewRequest(BaseModel):
    device_credential: str = Field(min_length=20, max_length=512)


class RevokeRequest(BaseModel):
    scope: Literal["family", "device"] = "family"
    current_device_credential: str | None = Field(default=None, min_length=32, max_length=512)


class RotateCredentialRequest(BaseModel):
    current_device_credential: str = Field(min_length=32, max_length=512)
    new_device_credential: str = Field(min_length=32, max_length=512)


class AdditionalDeviceEnrollmentRequest(BaseModel):
    current_device_credential: str = Field(min_length=32, max_length=512)
    enrollment_id: str = Field(min_length=32, max_length=512)
    device_secret: str = Field(min_length=32, max_length=512)
    display_name: str | None = Field(default=None, max_length=120)


class SessionResponse(BaseModel):
    token: str | None
    expires_at: str | None
    principal: PrincipalDTO
    device_credential: str | None = None
    credential_id: str | None = None
    credential_expires_at: str | None = None


class CredentialResponse(BaseModel):
    credential_id: str
    credential_expires_at: str


class RevokeResponse(BaseModel):
    revoked: bool
    scope: Literal["family", "device"]


def _session_response(
    issued: IssuedSession,
    *,
    identity: IssuedDeviceIdentity | None = None,
) -> SessionResponse:
    return SessionResponse(
        token=issued.token,
        expires_at=issued.expires_at,
        principal=PrincipalDTO.from_principal(issued.principal),
        device_credential=identity.device_credential if identity else None,
        credential_id=identity.credential_id if identity else None,
        credential_expires_at=identity.credential_expires_at if identity else None,
    )


def _local_response() -> SessionResponse:
    return SessionResponse(
        token=None,
        expires_at=None,
        principal=PrincipalDTO.from_principal(local_principal()),
    )


def _raise_rate_limit(exc: SessionIssueRateLimitError) -> NoReturn:
    raise HTTPException(
        status_code=exc.status_code,
        detail=exc.detail,
        headers={"Retry-After": str(exc.retry_after_s)},
    ) from exc


def _raise_enrollment_admission_limit(exc: EnrollmentAdmissionLimitError) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="enrollment_admission_limit_exceeded",
        headers={"Retry-After": str(exc.retry_after_s)},
    ) from exc


async def _enroll(
    request: Request,
    body: EnrollmentRequest | None,
    settings: Settings,
    policy: AccessPolicy,
) -> SessionResponse:
    if not settings.public_demo_mode:
        return _local_response()
    if body is None:
        raise HTTPException(status_code=422, detail="enrollment_credentials_required")
    try:
        identity = await policy.enroll_public_device(
            client_key=policy.client_host(request.client),
            enrollment_id=body.enrollment_id,
            device_secret=body.device_secret,
            display_name=body.display_name,
        )
    except EnrollmentAdmissionLimitError as exc:
        _raise_enrollment_admission_limit(exc)
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except IdentityAlreadyEnrolledError as exc:
        raise HTTPException(status_code=409, detail="enrollment_conflict") from exc
    return _session_response(identity.session)


@router.post("/session", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def issue_session(
    request: Request,
    body: EnrollmentRequest | None = None,
    settings: Settings = Depends(get_settings),
    policy: AccessPolicy = Depends(get_access_policy),
) -> SessionResponse:
    """Compatibility alias for first-time device enrollment."""

    return await _enroll(request, body, settings, policy)


@router.post(
    "/session/enroll",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_session(
    request: Request,
    body: EnrollmentRequest | None = None,
    settings: Settings = Depends(get_settings),
    policy: AccessPolicy = Depends(get_access_policy),
) -> SessionResponse:
    return await _enroll(request, body, settings, policy)


@router.post("/session/renew", response_model=SessionResponse)
async def renew_session(
    request: Request,
    body: RenewRequest,
    settings: Settings = Depends(get_settings),
    policy: AccessPolicy = Depends(get_access_policy),
) -> SessionResponse:
    if not settings.public_demo_mode:
        return _local_response()
    try:
        issued = await policy.renew_public_session(
            client_key=policy.client_host(request.client),
            device_credential=body.device_credential,
        )
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except DeviceCredentialError as exc:
        raise HTTPException(status_code=401, detail="identity_lost") from exc
    return _session_response(issued)


@router.post("/session/claim", response_model=SessionResponse)
async def claim_legacy_session(
    request: Request,
    principal: Principal = Depends(get_request_principal),
    policy: AccessPolicy = Depends(get_access_policy),
) -> SessionResponse:
    try:
        policy.check_sensitive_action(
            client_key=policy.client_host(request.client),
            principal=principal,
            action="claim",
        )
        identity = await policy.sessions.claim_legacy_identity(principal)
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except DeviceIdentityAlreadyClaimedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeviceCredentialError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _session_response(identity.session, identity=identity)


@router.post("/session/credential/rotate", response_model=CredentialResponse)
async def rotate_device_credential(
    request: Request,
    body: RotateCredentialRequest,
    principal: Principal = Depends(get_request_principal),
    policy: AccessPolicy = Depends(get_access_policy),
) -> CredentialResponse:
    try:
        policy.check_sensitive_action(
            client_key=policy.client_host(request.client),
            principal=principal,
            action="rotate",
        )
        credential_id, expires_at = await policy.sessions.rotate_device_credential(
            principal,
            current_credential=body.current_device_credential,
            new_credential=body.new_device_credential,
        )
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except DeviceCredentialError as exc:
        raise HTTPException(status_code=401, detail="credential_reauth_failed") from exc
    return CredentialResponse(
        credential_id=credential_id,
        credential_expires_at=expires_at,
    )


@router.post(
    "/session/devices/enroll",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_additional_device(
    request: Request,
    body: AdditionalDeviceEnrollmentRequest,
    principal: Principal = Depends(get_request_principal),
    policy: AccessPolicy = Depends(get_access_policy),
) -> SessionResponse:
    try:
        policy.check_sensitive_action(
            client_key=policy.client_host(request.client),
            principal=principal,
            action="device-enroll",
        )
        identity = await policy.sessions.enroll_additional_device(
            principal,
            current_credential=body.current_device_credential,
            enrollment_id=body.enrollment_id,
            device_secret=body.device_secret,
            peer_key=policy.client_host(request.client),
            display_name=body.display_name,
            admission_policy=policy.enrollment_admission_policy,
        )
    except EnrollmentAdmissionLimitError as exc:
        _raise_enrollment_admission_limit(exc)
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except IdentityAlreadyEnrolledError as exc:
        raise HTTPException(status_code=409, detail="enrollment_conflict") from exc
    except DeviceCredentialError as exc:
        raise HTTPException(status_code=401, detail="credential_reauth_failed") from exc
    return _session_response(identity.session)


@router.post("/session/revoke", response_model=RevokeResponse)
async def revoke_identity(
    request: Request,
    body: RevokeRequest,
    principal: Principal = Depends(get_request_principal),
    policy: AccessPolicy = Depends(get_access_policy),
) -> RevokeResponse:
    try:
        policy.check_sensitive_action(
            client_key=policy.client_host(request.client),
            principal=principal,
            action=f"revoke-{body.scope}",
        )
        if body.scope == "device":
            if not body.current_device_credential:
                raise HTTPException(status_code=422, detail="device_credential_required")
            revoked = await policy.sessions.revoke_device(
                principal,
                current_credential=body.current_device_credential,
            )
        else:
            revoked = await policy.sessions.revoke_session_family(principal)
    except SessionIssueRateLimitError as exc:
        _raise_rate_limit(exc)
    except DeviceCredentialError as exc:
        raise HTTPException(status_code=401, detail="credential_reauth_failed") from exc
    return RevokeResponse(revoked=revoked, scope=body.scope)


__all__ = [
    "AdditionalDeviceEnrollmentRequest",
    "CredentialResponse",
    "EnrollmentRequest",
    "PrincipalDTO",
    "RenewRequest",
    "RevokeRequest",
    "RevokeResponse",
    "RotateCredentialRequest",
    "SessionResponse",
    "router",
]
