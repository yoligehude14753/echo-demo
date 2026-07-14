"""Public-client compatibility gate shared by HTTP and WebSocket transports."""

from __future__ import annotations

import re

MINIMUM_PUBLIC_CLIENT_VERSION = "0.3.2"
PUBLIC_CLIENT_VERSION_HEADER = "X-EchoDesk-Client-Version"
PUBLIC_MINIMUM_CLIENT_VERSION_HEADER = "X-EchoDesk-Minimum-Client-Version"
PUBLIC_CLIENT_UPGRADE_URL = "https://github.com/yoligehude14753/echo-demo/releases"

_SEMVER_RE = re.compile(
    r"\A(?:echodesk-)?v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<prerelease>-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z",
    re.IGNORECASE,
)


def public_client_version_tuple(raw: str | None) -> tuple[int, int, int, int] | None:
    """Parse the bounded product version formats emitted by supported clients."""

    value = (raw or "").strip()
    if not value or len(value) > 64:
        return None
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    return (major, minor, patch, -1 if match.group("prerelease") else 0)


def is_supported_public_client(raw: str | None) -> bool:
    parsed = public_client_version_tuple(raw)
    minimum = public_client_version_tuple(MINIMUM_PUBLIC_CLIENT_VERSION)
    return parsed is not None and minimum is not None and parsed >= minimum


__all__ = [
    "MINIMUM_PUBLIC_CLIENT_VERSION",
    "PUBLIC_CLIENT_UPGRADE_URL",
    "PUBLIC_CLIENT_VERSION_HEADER",
    "PUBLIC_MINIMUM_CLIENT_VERSION_HEADER",
    "is_supported_public_client",
    "public_client_version_tuple",
]
