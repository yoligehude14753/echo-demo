"""Security headers for responses containing capability URLs or private exports."""

from __future__ import annotations

from typing import Protocol


class HeaderSink(Protocol):
    def __setitem__(self, key: str, value: str) -> None: ...


PRIVATE_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def apply_private_no_store(headers: HeaderSink) -> None:
    for name, value in PRIVATE_NO_STORE_HEADERS.items():
        headers[name] = value


__all__ = ["PRIVATE_NO_STORE_HEADERS", "apply_private_no_store"]
