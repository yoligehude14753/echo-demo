#!/usr/bin/env python3
"""Owner-only deployment ingress gate used by the public backend cutover tool.

The command deliberately prints only ``open`` or ``closed`` for ``status``.
The random bypass token stays in a mode-0600 regular file and is consumed by
the backend and local isolation smoke; it is never accepted on the command
line or written to deployment logs.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import NoReturn

_SERVICE_RE = re.compile(r"\A[A-Za-z0-9_.@-]{1,120}\.service\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{43,128}\Z")
_ENV_NAME = "ECHODESK_DEPLOYMENT_GATE_FILE"


def _die(message: str) -> NoReturn:
    raise SystemExit(message)


def _validate_invocation(argv: list[str]) -> tuple[str, Path]:
    if len(argv) != 4 or argv[1] not in {"status", "close", "open"}:
        _die("usage: echodesk-ingress-gate.py status|close|open SERVICE PORT")
    if not _SERVICE_RE.fullmatch(argv[2]):
        _die("invalid service name")
    try:
        port = int(argv[3])
    except ValueError:
        _die("invalid port")
    if not 1 <= port <= 65535:
        _die("invalid port")

    raw = os.environ.get(_ENV_NAME, "")
    if not raw or not os.path.isabs(raw):
        _die(f"{_ENV_NAME} must be an absolute path")
    path = Path(raw)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        _die("deployment gate path is not canonical")
    return argv[1], path


def _secure_parent(path: Path) -> Path:
    parent = path.parent
    resolved = parent.resolve(strict=True)
    if resolved != parent:
        _die("deployment gate parent must be canonical and symlink-free")
    info = parent.stat()
    if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
        _die("deployment gate parent must be an owner directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        _die("deployment gate parent must not be group/world writable")
    return parent


def _read_closed_token(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        _die("deployment gate must be an owner regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        _die("deployment gate must have mode 0600")
    token = path.read_text(encoding="ascii").strip()
    if not _TOKEN_RE.fullmatch(token):
        _die("deployment gate token is malformed")
    return token


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _close(path: Path) -> None:
    parent = _secure_parent(path)
    if _read_closed_token(path) is not None:
        return
    token = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, f"{token}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600, follow_symlinks=False)
    _read_closed_token(path)
    _fsync_directory(parent)


def _open(path: Path) -> None:
    parent = _secure_parent(path)
    if _read_closed_token(path) is None:
        return
    path.unlink()
    _fsync_directory(parent)


def main(argv: list[str] | None = None) -> int:
    action, path = _validate_invocation(argv or sys.argv)
    if action == "status":
        _secure_parent(path)
        print("closed" if _read_closed_token(path) is not None else "open")
    elif action == "close":
        _close(path)
    else:
        _open(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
