"""EchoDesk 身份与 session 公共接口。"""

from app.security.access import (
    AccessPolicy,
    AccessPolicyError,
    SessionIssueLimiter,
    SessionIssueRateLimitError,
)
from app.security.models import (
    LEGACY_DEVICE_ID,
    LEGACY_OWNER_ID,
    LEGACY_TENANT_ID,
    LOCAL_SESSION_ID,
    IssuedDeviceIdentity,
    IssuedSession,
    Principal,
    PrincipalMode,
    local_principal,
)
from app.security.paths import route_scope_path
from app.security.sessions import (
    DeviceCredentialError,
    DeviceIdentityAlreadyClaimedError,
    ExpiredDeviceCredentialError,
    ExpiredSessionError,
    IdentityAlreadyEnrolledError,
    InvalidDeviceCredentialError,
    InvalidSessionError,
    ResourceTicketError,
    RevokedDeviceCredentialError,
    RevokedSessionError,
    SessionError,
    SessionStore,
)

__all__ = [
    "LEGACY_DEVICE_ID",
    "LEGACY_OWNER_ID",
    "LEGACY_TENANT_ID",
    "LOCAL_SESSION_ID",
    "AccessPolicy",
    "AccessPolicyError",
    "DeviceCredentialError",
    "DeviceIdentityAlreadyClaimedError",
    "ExpiredDeviceCredentialError",
    "ExpiredSessionError",
    "IdentityAlreadyEnrolledError",
    "InvalidDeviceCredentialError",
    "InvalidSessionError",
    "IssuedDeviceIdentity",
    "IssuedSession",
    "Principal",
    "PrincipalMode",
    "ResourceTicketError",
    "RevokedDeviceCredentialError",
    "RevokedSessionError",
    "SessionError",
    "SessionIssueLimiter",
    "SessionIssueRateLimitError",
    "SessionStore",
    "local_principal",
    "route_scope_path",
]
