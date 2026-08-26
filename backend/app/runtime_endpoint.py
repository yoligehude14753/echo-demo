"""Shared runtime endpoint record for local EchoDesk consumers.

The backend lifecycle owner injects ``ECHODESK_BASE_URL``.  Once ready, the
backend publishes that exact value for independently launched local harnesses
such as the meeting-recorder MCP sidecar.  This file is a published runtime
fact, never a configuration fallback.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from app.config_io import user_config_dir


def runtime_endpoint_path() -> Path:
    return user_config_dir() / "runtime" / "endpoint.json"


def publish_runtime_endpoint(base_url: str) -> Path:
    path = runtime_endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "base_url": base_url,
        "pid": os.getpid(),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=".endpoint.",
        suffix=".json.tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def clear_runtime_endpoint(path: Path, *, base_url: str) -> None:
    """Remove only the record published by this backend process."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    if payload.get("pid") != os.getpid() or payload.get("base_url") != base_url:
        return
    with contextlib.suppress(OSError):
        path.unlink()
