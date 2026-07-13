"""Fail-closed application gate for public deployment cutovers."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEPLOYMENT_GATE_HEADER = b"x-echo-deployment-gate"
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{43,128}\Z")
_PUBLIC_PROBES = frozenset({"/healthz", "/readyz"})


@dataclass(frozen=True, slots=True)
class _GateState:
    is_open: bool
    token: str | None = None


def _read_gate_state(path: Path) -> _GateState:  # noqa: PLR0911
    """Treat every unreadable or malformed gate as closed without a bypass."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return _GateState(is_open=True)
    except OSError:
        return _GateState(is_open=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > 256
    ):
        return _GateState(is_open=False)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        # An atomic open operation removes the file only after validation.
        return _GateState(is_open=True)
    except OSError:
        return _GateState(is_open=False)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return _GateState(is_open=False)
        raw = os.read(descriptor, 257)
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return _GateState(is_open=False)
    if not _TOKEN_RE.fullmatch(token):
        return _GateState(is_open=False)
    return _GateState(is_open=False, token=token)


def _presented_token(scope: Scope) -> str | None:
    values = [
        value for name, value in scope.get("headers", []) if name.lower() == DEPLOYMENT_GATE_HEADER
    ]
    if len(values) != 1:
        return None
    try:
        return cast(bytes, values[0]).decode("ascii")
    except UnicodeDecodeError:
        return None


class DeploymentGateMiddleware:
    """Block public business traffic while an owner-only gate file exists."""

    def __init__(self, app: ASGIApp, *, gate_file: Path | None) -> None:
        self.app = app
        self.gate_file = gate_file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.gate_file is None or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and str(scope.get("path", "")) in _PUBLIC_PROBES:
            await self.app(scope, receive, send)
            return

        state = _read_gate_state(self.gate_file)
        presented = _presented_token(scope)
        if state.is_open or (
            state.token is not None
            and presented is not None
            and hmac.compare_digest(state.token, presented)
        ):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "deployment gate"})
            return

        body = json.dumps(
            {"detail": "服务正在安全切换，请稍后重试"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        start: Message = {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", b"5"),
                (b"cache-control", b"no-store"),
            ],
        }
        await send(start)
        await send({"type": "http.response.body", "body": body})


__all__ = ["DEPLOYMENT_GATE_HEADER", "DeploymentGateMiddleware"]
