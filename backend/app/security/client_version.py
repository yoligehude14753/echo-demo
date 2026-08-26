"""Public-client compatibility gate shared by HTTP and WebSocket transports."""

from __future__ import annotations

import re

# The public v0.3.3 preview is the first client that implements durable device
# enrollment.  Keep the gate at that exact prerelease so v0.3.3-preview.2 and
# stable/newer clients are admitted while older previews remain blocked.
MINIMUM_PUBLIC_CLIENT_VERSION = "0.3.3-preview.2"
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


def _public_client_version_parts(
    raw: str | None,
) -> tuple[int, int, int, tuple[tuple[int, int | str], ...] | None] | None:
    value = (raw or "").strip()
    if not value or len(value) > 64:
        return None
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    prerelease = match.group("prerelease")
    prerelease_key = (
        tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in prerelease.removeprefix("-").split(".")
        )
        if prerelease
        else None
    )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease_key,
    )


def is_supported_public_client(raw: str | None) -> bool:
    parsed = _public_client_version_parts(raw)
    minimum = _public_client_version_parts(MINIMUM_PUBLIC_CLIENT_VERSION)
    if parsed is None or minimum is None:
        return False
    client_core, client_prerelease = parsed[:3], parsed[3]
    minimum_core, minimum_prerelease = minimum[:3], minimum[3]
    if client_core != minimum_core:
        return client_core > minimum_core
    if minimum_prerelease is None:
        return client_prerelease is None
    if client_prerelease is None:
        return True
    return client_prerelease >= minimum_prerelease


__all__ = [
    "MINIMUM_PUBLIC_CLIENT_VERSION",
    "PUBLIC_CLIENT_UPGRADE_URL",
    "PUBLIC_CLIENT_VERSION_HEADER",
    "PUBLIC_MINIMUM_CLIENT_VERSION_HEADER",
    "is_supported_public_client",
    "public_client_version_tuple",
]
