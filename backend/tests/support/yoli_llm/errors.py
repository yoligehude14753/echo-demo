"""Error contract used by deterministic Echo model-gateway tests."""

from __future__ import annotations

from typing import Any


class YoliExternalError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        status: int | None = None,
        retryable: bool = True,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.details = details


class AuthError(YoliExternalError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, retryable=False, **details)


class BadRequestError(YoliExternalError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, retryable=False, **details)


class ProviderError(YoliExternalError):
    pass


class RateLimitError(YoliExternalError):
    pass


class TimeoutError_(YoliExternalError):
    pass


class StreamCancelledError(YoliExternalError):
    def __init__(self, message: str = "stream cancelled", **details: Any) -> None:
        super().__init__(message, retryable=False, **details)


__all__ = [
    "AuthError",
    "BadRequestError",
    "ProviderError",
    "RateLimitError",
    "StreamCancelledError",
    "TimeoutError_",
    "YoliExternalError",
]
