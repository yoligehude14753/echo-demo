"""Credential-handle boundary for the model runtime.

The model config owns only an opaque handle.  Resolving the provider secret is
an injected concern so this package never discovers credentials from HOME,
environment variables, provider SDK state, or a second config file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.model_runtime.errors import (
    MODEL_CREDENTIAL_HANDLE_INVALID,
    MODEL_CREDENTIAL_UNAVAILABLE,
    ModelRuntimeError,
)

_HANDLE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:(?://)?|(?:cred|handle)_)"
    r"[A-Za-z0-9._~:/-]{2,120}$"
)


class CredentialHandleError(ModelRuntimeError):
    """The handle is malformed or cannot be resolved."""


@dataclass(frozen=True, slots=True)
class CredentialHandle:
    """A validated opaque reference; its repr never includes the raw secret."""

    value: str = ""

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not _HANDLE_RE.fullmatch(normalized) or normalized.lower().startswith(
            ("http:", "https:")
        ):
            raise CredentialHandleError(MODEL_CREDENTIAL_HANDLE_INVALID, field="credential_handle")
        object.__setattr__(self, "value", normalized)

    def __repr__(self) -> str:
        return "CredentialHandle(<opaque>)"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedCredential:
    """A short-lived resolved secret held only at the transport boundary."""

    handle: CredentialHandle
    value: str

    def __repr__(self) -> str:
        return "ResolvedCredential(<redacted>)"

    def __str__(self) -> str:
        return "[REDACTED]"


@runtime_checkable
class CredentialResolver(Protocol):
    """Injected credential vault contract; implementations own secret storage."""

    def resolve(self, handle: CredentialHandle) -> ResolvedCredential:
        """Resolve one handle or raise ``CredentialHandleError``."""


class InMemoryCredentialResolver:
    """Deterministic resolver for unit tests and explicit local adapters.

    It intentionally has no persistence or environment fallback.  Production
    callers should inject an OS/keychain-backed implementation through the
    same protocol.
    """

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {str(key): str(value) for key, value in (values or {}).items()}

    def resolve(self, handle: CredentialHandle) -> ResolvedCredential:
        value = self._values.get(handle.value)
        if value is None or not value:
            raise CredentialHandleError(MODEL_CREDENTIAL_UNAVAILABLE, field="credential_handle")
        return ResolvedCredential(handle=handle, value=value)

    def put(self, handle: CredentialHandle | str, value: str) -> None:
        parsed = handle if isinstance(handle, CredentialHandle) else CredentialHandle(handle)
        if not value:
            raise CredentialHandleError(MODEL_CREDENTIAL_UNAVAILABLE, field="credential")
        self._values[parsed.value] = value

    def revoke(self, handle: CredentialHandle | str) -> None:
        parsed = handle if isinstance(handle, CredentialHandle) else CredentialHandle(handle)
        self._values.pop(parsed.value, None)


def validate_credential_handle(value: str) -> CredentialHandle:
    """Validate a raw config value without retaining it in an exception."""

    try:
        return CredentialHandle(value)
    except CredentialHandleError:
        raise
    except (AttributeError, TypeError):
        raise CredentialHandleError(
            MODEL_CREDENTIAL_HANDLE_INVALID, field="credential_handle"
        ) from None


__all__ = [
    "CredentialHandle",
    "CredentialHandleError",
    "CredentialResolver",
    "InMemoryCredentialResolver",
    "ResolvedCredential",
    "validate_credential_handle",
]
