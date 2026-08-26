"""Host-independent ASGI route path normalization."""

from __future__ import annotations

from collections.abc import Mapping


def route_scope_path(scope: Mapping[str, object]) -> str:
    """Return the router-visible path without consulting URL/Host metadata.

    ASGI servers may keep an application mount prefix in both ``path`` and
    ``root_path``. Starlette removes that prefix before routing, so every
    authorization, quota, LAN and upload policy must apply the same transform.
    """

    raw_path = scope.get("path")
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return "/"

    raw_root = scope.get("root_path")
    if not isinstance(raw_root, str) or not raw_root or raw_root == "/":
        return raw_path
    root_path = f"/{raw_root.strip('/')}"
    if raw_path == root_path:
        return "/"
    if raw_path.startswith(f"{root_path}/"):
        return raw_path[len(root_path) :]
    return raw_path


__all__ = ["route_scope_path"]
