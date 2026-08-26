"""EchoDesk 请求身份值对象。

本模块不依赖 FastAPI，供后续 HTTP / WebSocket 适配层统一注入。身份由服务端
解析后传给业务层；客户端提供的 tenant/device/owner 字段不能直接构造授权上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PrincipalMode = Literal["local", "public"]

LEGACY_TENANT_ID = "legacy-local"
LEGACY_DEVICE_ID = "legacy-local"
LEGACY_OWNER_ID = "legacy-local"
LOCAL_SESSION_ID = "local-fixed"


@dataclass(frozen=True, slots=True)
class Principal:
    """服务端已验证的资源访问主体。"""

    tenant_id: str
    device_id: str
    owner_id: str
    session_id: str
    mode: PrincipalMode
    family_id: str | None = None

    @property
    def user_id(self) -> str:
        """Stable user id; ``owner_id`` remains the resource-schema name."""

        return self.owner_id


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """公共 session 的一次性签发结果。

    ``token`` 只在签发响应返回一次；数据库只保存其 SHA-256 hash。
    """

    token: str
    principal: Principal
    expires_at: str


@dataclass(frozen=True, slots=True)
class IssuedDeviceIdentity:
    """Enrollment/claim result containing the only plaintext credential copy."""

    session: IssuedSession
    device_credential: str
    credential_id: str
    credential_expires_at: str


_LOCAL_PRINCIPAL = Principal(
    tenant_id=LEGACY_TENANT_ID,
    device_id=LEGACY_DEVICE_ID,
    owner_id=LEGACY_OWNER_ID,
    session_id=LOCAL_SESSION_ID,
    mode="local",
)


def local_principal() -> Principal:
    """local-first 模式的固定单用户 principal。"""

    return _LOCAL_PRINCIPAL


__all__ = [
    "LEGACY_DEVICE_ID",
    "LEGACY_OWNER_ID",
    "LEGACY_TENANT_ID",
    "LOCAL_SESSION_ID",
    "IssuedDeviceIdentity",
    "IssuedSession",
    "Principal",
    "PrincipalMode",
    "local_principal",
]
