"""Telemetry port boundary.

The core contract is deliberately narrower than a logging interface: callers
can submit only a typed observation and can only receive typed aggregates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.telemetry.contracts import (
    DeletionReceipt,
    TelemetryAggregate,
    TelemetryDeleteRequest,
    TelemetryObservation,
    TelemetryQuery,
)


class TelemetryPort(Protocol):
    """Privacy-preserving usage telemetry boundary."""

    async def record(self, observation: TelemetryObservation) -> None:
        """Accept one allowlisted observation, or no-op when disabled."""

    async def query(self, query: TelemetryQuery) -> tuple[TelemetryAggregate, ...]:
        """Return only k-thresholded aggregates."""

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Apply retention and return the number of removed events."""

    async def delete(self, request: TelemetryDeleteRequest) -> DeletionReceipt:
        """Delete matching pseudonymous events and return an audit receipt."""


__all__ = ["TelemetryPort"]
