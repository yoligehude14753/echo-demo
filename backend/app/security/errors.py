"""Transport markers for server-side failures that are unsafe to expose publicly."""

from __future__ import annotations

from fastapi import HTTPException


class InternalHTTPException(HTTPException):
    """An HTTP-shaped internal failure whose diagnostic detail is local-only.

    Business/protocol errors must continue to use :class:`HTTPException` so their
    intentional machine-readable details survive in public mode.  This subtype is
    reserved for messages derived from server exceptions, workflow handlers, file
    parsers, providers, or other implementation details.
    """


__all__ = ["InternalHTTPException"]
