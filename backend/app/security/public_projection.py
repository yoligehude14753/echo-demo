"""Public-client projection for server-private filesystem references.

Domain records and durable workflow/outbox payloads intentionally retain real
paths for local-first recovery and downloads.  Public transports must project
those payloads before serialization so historical rows and replayed events are
covered by the same privacy fence as newly-created values.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import Settings
from app.security.models import Principal

_SERVER_PRIVATE_PATH_KEYS = frozenset(
    {
        "audio_ref",
        "file_path",
        "raw_transcript_ref",
        "raw_ref",
        "relative_path",
        "source_path",
    }
)
_SERVER_PRIVATE_OMIT_KEYS = frozenset({"original_build_dir"})
_FAILURE_STATES = frozenset({"failed", "timeout", "cancel_failed"})
_PUBLIC_FAILURE_MESSAGE = "操作失败，请重试"
_PUBLIC_FILE_CLEANUP_FAILURE = "产物文件清理失败"
_PRIVATE_PATH_REPLACEMENT = "[SERVER_PATH]"
_FILE_URI_SERVER_PATH = re.compile(r"(?i)\bfile:(?:/{1,3})?(?:[A-Z]:[\\/]|/)?[^\s\"'<>;,\)\]}]+")
_TYPICAL_POSIX_SERVER_PATH = re.compile(
    r"(?<![A-Za-z0-9_/])/(?:Users|home|root|var|private|tmp|opt|srv|etc)"
    r"(?:/[^\s\"'<>;,\)\]}]*)?"
)
_TYPICAL_WINDOWS_SERVER_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_\\/])[A-Z]:\\(?:Users|ProgramData|Windows|Temp)"
    r"(?:\\[^\s\"'<>;,\)\]}]*)?"
)


def server_private_roots(settings: Settings) -> tuple[str, ...]:
    """Return canonical configured filesystem roots that public text must hide."""

    candidates = [
        Path(settings.db_path).expanduser().parent,
        Path(settings.rag_index_dir).expanduser(),
        Path(settings.workspace_state_file).expanduser().parent,
        Path(settings.skill_executor_build_dir).expanduser(),
        Path(settings.storage_dir).expanduser(),
        *settings.workspace_dirs_list,
    ]
    roots: set[str] = set()
    for candidate in candidates:
        try:
            root = str(candidate.resolve())
        except OSError:
            root = str(candidate.absolute())
        if root not in {"", "/"}:
            roots.add(root.rstrip("/\\"))
    return tuple(sorted(roots, key=len, reverse=True))


def _redact_private_root_text(value: str, private_roots: tuple[str, ...]) -> str:
    # File URIs commonly wrap absolute paths in one or three slashes.  Redact
    # them before root-boundary matching so the URI separator cannot hide the
    # first path slash from the configured-root and typical-path patterns.
    projected = _FILE_URI_SERVER_PATH.sub(_PRIVATE_PATH_REPLACEMENT, value)
    for root in private_roots:
        if not root:
            continue
        projected = re.sub(
            rf"(?<![A-Za-z0-9_/]){re.escape(root)}[^\s\"'<>;,\)\]\x7d]*",
            _PRIVATE_PATH_REPLACEMENT,
            projected,
        )
    projected = _TYPICAL_POSIX_SERVER_PATH.sub(_PRIVATE_PATH_REPLACEMENT, projected)
    return _TYPICAL_WINDOWS_SERVER_PATH.sub(_PRIVATE_PATH_REPLACEMENT, projected)


def _failure_projection(value: Any, *, message: str = _PUBLIC_FAILURE_MESSAGE) -> Any:
    if value is None or value == "":
        return value
    return message


def _redact_server_paths(value: Any, private_roots: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        state = value.get("state") or value.get("status")
        projected: dict[Any, Any] = {}
        for key, item in value.items():
            if key in _SERVER_PRIVATE_OMIT_KEYS:
                continue
            if key in _SERVER_PRIVATE_PATH_KEYS:
                projected[key] = None
            elif key in {"error", "minutes_error"}:
                projected[key] = _failure_projection(item)
            elif key == "file_cleanup_errors" and isinstance(item, dict):
                projected[key] = {
                    artifact_id: _failure_projection(
                        failure,
                        message=_PUBLIC_FILE_CLEANUP_FAILURE,
                    )
                    for artifact_id, failure in item.items()
                }
            elif key in {"message", "progress_text"} and state in _FAILURE_STATES:
                projected[key] = _failure_projection(item)
            else:
                projected[key] = _redact_server_paths(item, private_roots)
        return projected
    if isinstance(value, list):
        return [_redact_server_paths(item, private_roots) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_server_paths(item, private_roots) for item in value)
    if isinstance(value, str):
        return _redact_private_root_text(value, private_roots)
    return value


def project_client_payload(
    value: Any,
    principal: Principal,
    *,
    private_roots: tuple[str, ...] = (),
) -> Any:
    """Return a transport-safe view without mutating durable/internal values."""

    if principal.mode != "public":
        return value
    return _redact_server_paths(value, private_roots)


def project_client_dict(
    value: dict[str, Any],
    principal: Principal,
    *,
    private_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    projected = project_client_payload(value, principal, private_roots=private_roots)
    if not isinstance(projected, dict):  # pragma: no cover - structural invariant
        raise TypeError("client projection changed a mapping into a non-mapping")
    return projected


__all__ = ["project_client_dict", "project_client_payload", "server_private_roots"]
