"""Telemetry adapters.

``NoopTelemetryAdapter`` is the safe default. The in-memory adapter is a
deterministic test adapter and is intentionally not a production persistence
implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.telemetry.contracts import (
    DEFAULT_K_THRESHOLD,
    DeletionReceipt,
    FailureReason,
    FailureReasonCount,
    TelemetryAggregate,
    TelemetryDeleteRequest,
    TelemetryEvent,
    TelemetryObservation,
    TelemetryOperation,
    TelemetryPlatform,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRuntimeConfig,
    utc_now,
)
from app.telemetry.ports import TelemetryPort
from app.telemetry.pseudonym import HmacPseudonymizer

_AggregateKey = tuple[
    int,
    str,
    str,
    str,
    str,
    TelemetryOperation,
    TelemetryPlatform,
    str,
    TelemetryProvider,
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def _event_key(event: TelemetryEvent) -> _AggregateKey:
    identity = event.identity
    return (
        identity.epoch,
        identity.key_version,
        identity.tenant_pseudonym,
        identity.user_pseudonym,
        identity.device_pseudonym,
        event.operation,
        event.platform,
        event.app_version,
        event.provider,
    )


def _matches_query(event: TelemetryEvent, query: TelemetryQuery) -> bool:
    identity = event.identity
    if query.start_at is not None and event.occurred_at < query.start_at:
        return False
    if query.end_at is not None and event.occurred_at >= query.end_at:
        return False
    if query.epoch is not None and identity.epoch != query.epoch:
        return False
    if query.key_version is not None and identity.key_version != query.key_version:
        return False
    return _matches_identity_and_dimensions(event, query)


def _matches_identity_and_dimensions(event: TelemetryEvent, query: TelemetryQuery) -> bool:
    identity = event.identity
    return all(
        (
            query.tenant_pseudonym is None or identity.tenant_pseudonym == query.tenant_pseudonym,
            query.user_pseudonym is None or identity.user_pseudonym == query.user_pseudonym,
            query.device_pseudonym is None or identity.device_pseudonym == query.device_pseudonym,
            query.operation is None or event.operation == query.operation,
            query.platform is None or event.platform == query.platform,
            query.app_version is None or event.app_version == query.app_version,
            query.provider is None or event.provider == query.provider,
            query.failure_reason is None or event.failure_reason == query.failure_reason,
        )
    )


def _matches_delete(event: TelemetryEvent, request: TelemetryDeleteRequest) -> bool:
    identity = event.identity
    return all(
        (
            request.tenant_pseudonym is None
            or identity.tenant_pseudonym == request.tenant_pseudonym,
            request.user_pseudonym is None or identity.user_pseudonym == request.user_pseudonym,
            request.device_pseudonym is None
            or identity.device_pseudonym == request.device_pseudonym,
            request.key_version is None or identity.key_version == request.key_version,
            request.epoch is None or identity.epoch == request.epoch,
        )
    )


def _make_aggregate(key: _AggregateKey, events: Iterable[TelemetryEvent]) -> TelemetryAggregate:
    event_list = tuple(events)
    request_count = len(event_list)
    success_count = sum(event.success for event in event_list)
    audio_events = tuple(event for event in event_list if event.audio_duration_ms is not None)
    failure_reason_counts: dict[FailureReason, int] = {}
    for event in event_list:
        if not event.success:
            reason = event.failure_reason or FailureReason.UNKNOWN
            failure_reason_counts[reason] = failure_reason_counts.get(reason, 0) + 1
    return TelemetryAggregate(
        epoch=key[0],
        key_version=key[1],
        tenant_pseudonym=key[2],
        user_pseudonym=key[3],
        device_pseudonym=key[4],
        operation=key[5],
        platform=key[6],
        app_version=key[7],
        provider=key[8],
        failure_reason_counts=tuple(
            FailureReasonCount(reason=reason, event_count=count)
            for reason, count in sorted(
                failure_reason_counts.items(), key=lambda item: item[0].value
            )
        ),
        request_count=request_count,
        success_count=success_count,
        failure_count=request_count - success_count,
        success_rate=success_count / request_count,
        latency_sum_ms=sum(event.end_to_end_latency_ms for event in event_list),
        queue_wait_sum_ms=sum(event.queue_wait_ms for event in event_list),
        audio_duration_sum_ms=sum(event.audio_duration_ms or 0 for event in audio_events),
        audio_duration_event_count=len(audio_events),
    )


class NoopTelemetryAdapter(TelemetryPort):
    """Feature-off adapter with no mutable state and no writes."""

    @property
    def stored_event_count(self) -> int:
        return 0

    @property
    def deletion_audit(self) -> tuple[DeletionReceipt, ...]:
        return ()

    async def record(self, observation: TelemetryObservation) -> None:
        del observation

    async def query(self, query: TelemetryQuery) -> tuple[TelemetryAggregate, ...]:
        del query
        return ()

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        del now
        return 0

    async def delete(self, request: TelemetryDeleteRequest) -> DeletionReceipt:
        return DeletionReceipt(
            audit_id="noop-delete",
            deleted_event_count=0,
            reason=request.reason,
        )


class InMemoryAggregateTelemetry(TelemetryPort):
    """Deterministic aggregate adapter for local contract tests only."""

    def __init__(
        self,
        pseudonymizer: HmacPseudonymizer,
        *,
        retention_s: int,
        k_threshold: int = DEFAULT_K_THRESHOLD,
    ) -> None:
        if retention_s <= 0 or k_threshold < 1:
            raise ValueError("retention_s and k_threshold must be positive")
        self._pseudonymizer = pseudonymizer
        self._retention_s = retention_s
        self._k_threshold = k_threshold
        self._events: dict[str, TelemetryEvent] = {}
        self._deletion_audit: list[DeletionReceipt] = []

    @property
    def stored_event_count(self) -> int:
        return len(self._events)

    @property
    def deletion_audit(self) -> tuple[DeletionReceipt, ...]:
        return tuple(self._deletion_audit)

    def stored_events(self) -> tuple[TelemetryEvent, ...]:
        """Return pseudonymized events for deterministic privacy assertions."""

        return tuple(self._events.values())

    async def record(self, observation: TelemetryObservation) -> None:
        event = self._pseudonymizer.materialize(observation)
        previous = self._events.get(event.event_id)
        if previous is None:
            self._events[event.event_id] = event
            return
        if previous != event:
            raise ValueError("event_id was reused with a different telemetry payload")

    async def query(self, query: TelemetryQuery) -> tuple[TelemetryAggregate, ...]:
        groups: dict[_AggregateKey, list[TelemetryEvent]] = {}
        for event in self._events.values():
            if _matches_query(event, query):
                groups.setdefault(_event_key(event), []).append(event)
        aggregates = (
            _make_aggregate(key, events)
            for key, events in sorted(groups.items(), key=lambda item: item[0])
            if len(events) >= max(self._k_threshold, query.k_threshold)
        )
        return tuple(aggregates)

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        current = _as_utc(now or utc_now())
        cutoff = current - timedelta(seconds=self._retention_s)
        expired_ids = [
            event_id for event_id, event in self._events.items() if event.occurred_at < cutoff
        ]
        for event_id in expired_ids:
            del self._events[event_id]
        return len(expired_ids)

    async def delete(self, request: TelemetryDeleteRequest) -> DeletionReceipt:
        deleted_ids = [
            event_id for event_id, event in self._events.items() if _matches_delete(event, request)
        ]
        for event_id in deleted_ids:
            del self._events[event_id]
        receipt = DeletionReceipt(
            audit_id=f"delete-{uuid4().hex}",
            deleted_event_count=len(deleted_ids),
            reason=request.reason,
        )
        self._deletion_audit.append(receipt)
        return receipt


def build_test_telemetry_adapter(config: TelemetryRuntimeConfig) -> TelemetryPort:
    """Build an explicitly injected adapter for local tests, never production."""

    if not config.enabled or not config.hmac_secret:
        return NoopTelemetryAdapter()
    pseudonymizer = HmacPseudonymizer(
        {config.key_version: config.hmac_secret},
        current_key_version=config.key_version,
        rotation_period_s=config.rotation_period_s,
    )
    return InMemoryAggregateTelemetry(
        pseudonymizer,
        retention_s=config.retention_s,
        k_threshold=config.k_threshold,
    )


__all__ = [
    "InMemoryAggregateTelemetry",
    "NoopTelemetryAdapter",
    "build_test_telemetry_adapter",
]
