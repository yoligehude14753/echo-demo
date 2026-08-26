"""Pure audit-event redaction for agent capability decisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|"
    r"refresh[_-]?token|secret|token|value)(?:$|[_-])",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)\s+)(?P<value>[^\s,;]+)"
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|password|refresh[_-]?token|secret|token|value)"
    r"\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s,;&\"']+)(?P=quote)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?:api[_-]?key|password|refresh[_-]?token|secret|token|value)=)"
    r"[^&\s\"']+"
)
_ABSOLUTE_URL_RE = re.compile(r"(?P<url>(?:https?|wss?)://[^\s\"'<>]+)", re.IGNORECASE)


def redact_secret(value: Any) -> str:
    """Return no portion of a secret, including no identifying suffix."""

    if value is None or value == "":
        return ""
    return REDACTED


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return REDACTED
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"} or not parts.hostname:
        return REDACTED
    hostname = parts.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit(
        (parts.scheme, f"{hostname}{port}", parts.path, "redacted" if parts.query else "", "")
    )


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group("url")
    suffix = ""
    while raw and raw[-1] in ".,;)]}":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    return _safe_url(raw) + suffix


def redact_text(value: str) -> str:
    """Remove credentials and secret-bearing query/assignment values from text."""

    text = _ABSOLUTE_URL_RE.sub(_redact_url, value)
    text = _AUTH_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = _ASSIGNMENT_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    return _QUERY_SECRET_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)


def redact_audit_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively make an audit payload safe without performing I/O."""

    if key is not None and _SENSITIVE_KEY_RE.search(key):
        return redact_secret(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_audit_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_audit_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_audit_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project a mapping into an audit-safe mapping; raw input is never mutated."""

    return cast(dict[str, Any], redact_audit_value(event))


redact_audit = redact_audit_event


__all__ = [
    "REDACTED",
    "redact_audit",
    "redact_audit_event",
    "redact_audit_value",
    "redact_secret",
    "redact_text",
]
