"""Central secret and URL redaction for logs and diagnostic exports."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|(?:^|[_-])key(?:$|[_-])|authorization|cookie|credential|password|"
    r"refresh|share|secret|token)",
    re.IGNORECASE,
)
_ABSOLUTE_URL_RE = re.compile(r"(?P<url>(?:https?|wss?)://[^\s\"'<>]+)", re.IGNORECASE)
_RELATIVE_QUERY_RE = re.compile(r"(?P<path>/[^\s?\"']*)\?[^\s\"']+")
_AUTH_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)\s+)(?P<value>[^\s,;]+)"
)
_NAMED_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|password|refresh[_-]?token|share|secret|token)"
    r"\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s,;&\"']+)(?P=quote)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?:api[_-]?key|password|refresh[_-]?token|share|secret|token)=)"
    r"[^&\s\"']+"
)


def redact_secret(value: Any) -> str:
    """Fully redact a secret; never retain identifying prefixes or suffixes."""

    if value is None or value == "":
        return ""
    return REDACTED


def sanitize_url(value: str) -> str:
    """Drop URL userinfo, query values, and fragments while preserving routing context."""

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
    query = "redacted" if parts.query else ""
    return urlunsplit((parts.scheme, f"{hostname}{port}", parts.path, query, ""))


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw = match.group("url")
    suffix = ""
    while raw and raw[-1] in ".,;)]}":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    return sanitize_url(raw) + suffix


def sanitize_text(value: str) -> str:
    """Redact credentials and query strings from arbitrary diagnostic/log text."""

    text = _ABSOLUTE_URL_RE.sub(_sanitize_url_match, value)
    text = _RELATIVE_QUERY_RE.sub(lambda match: f"{match.group('path')}?redacted", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    return _QUERY_SECRET_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)


def redact_structure(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact sensitive fields and sanitize URLs embedded in strings."""

    if key is not None and SENSITIVE_KEY_RE.search(key):
        return redact_secret(value)
    if isinstance(value, Mapping):
        return {str(k): redact_structure(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structure(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


class RedactingLogFilter(logging.Filter):
    """Sanitize lazy logging arguments, including Uvicorn access-log request targets."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_text(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {
                key: redact_structure(value, key=str(key)) for key, value in record.args.items()
            }
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_structure(value) for value in record.args)
        return True


class RedactingFormatter(logging.Formatter):
    """Last-line defense for exception text and preformatted third-party records."""

    def format(self, record: logging.LogRecord) -> str:
        return sanitize_text(super().format(record))


def install_redaction_filter(logger: logging.Logger) -> None:
    """Attach the filter idempotently to a logger and each of its current handlers."""

    if not any(isinstance(item, RedactingLogFilter) for item in logger.filters):
        logger.addFilter(RedactingLogFilter())
    for handler in logger.handlers:
        if not any(isinstance(item, RedactingLogFilter) for item in handler.filters):
            handler.addFilter(RedactingLogFilter())


__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_RE",
    "RedactingFormatter",
    "RedactingLogFilter",
    "install_redaction_filter",
    "redact_secret",
    "redact_structure",
    "sanitize_text",
    "sanitize_url",
]
