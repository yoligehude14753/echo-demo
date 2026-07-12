"""Server-authored physical storage scope helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.security.context import current_principal
from app.security.models import Principal

SCOPES_DIRECTORY = "scopes"


def scope_storage_key_for(tenant_id: str, owner_id: str) -> str:
    """Return the opaque directory key for an explicit authoritative scope."""

    raw = f"{tenant_id}\0{owner_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def scope_storage_key(principal: Principal | None = None) -> str:
    """Return an opaque, stable directory key for one tenant/user scope."""

    resolved = principal or current_principal()
    return scope_storage_key_for(resolved.tenant_id, resolved.owner_id)


def scoped_directory(root: Path | str, principal: Principal | None = None) -> Path:
    """Return ``root/scopes/<opaque-scope>`` without performing filesystem I/O."""

    return Path(root).expanduser() / SCOPES_DIRECTORY / scope_storage_key(principal)


def scoped_directory_for(root: Path | str, tenant_id: str, owner_id: str) -> Path:
    """Return a scoped directory for a tenant/user tuple loaded from storage."""

    return Path(root).expanduser() / SCOPES_DIRECTORY / scope_storage_key_for(tenant_id, owner_id)


def physical_resource_id(
    logical_id: str,
    *,
    kind: str,
    principal: Principal | None = None,
) -> str:
    """Map an untrusted logical id to a path-safe, scope-bound physical id."""

    resolved = principal or current_principal()
    return physical_resource_id_for(
        logical_id,
        kind=kind,
        tenant_id=resolved.tenant_id,
        owner_id=resolved.owner_id,
    )


def physical_resource_id_for(
    logical_id: str,
    *,
    kind: str,
    tenant_id: str,
    owner_id: str,
) -> str:
    """Derive a physical id for one authoritative persisted principal scope."""

    raw = f"{tenant_id}\0{owner_id}\0{kind}\0{logical_id}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:32]
    safe_kind = "".join(ch for ch in kind.lower() if ch.isalnum() or ch == "-") or "resource"
    return f"{safe_kind}-{digest}"


__all__ = [
    "SCOPES_DIRECTORY",
    "physical_resource_id",
    "physical_resource_id_for",
    "scope_storage_key",
    "scope_storage_key_for",
    "scoped_directory",
    "scoped_directory_for",
]
